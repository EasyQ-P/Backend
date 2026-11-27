"""Label candidates, datasets, training, models, deployments (API-53~63).

The training server (7.4) owns datasets, jobs, evaluations, models and the serving state; the service keeps copies.
The backend DB role may only read datasets/model_versions/evaluation_runs and insert training_runs, so every copy is
written by the worker (SYNC · TRAIN · DATASET · DEPLOY jobs, worker/easyq_worker/sync.py). Screen views enqueue a
SYNC job (8.2.25 "관리자 화면 조회 때"). Training-server calls go through the fixed adapter easyq_common.trainer only.
"""
import re

from easyq_common.contracts import APIError, Result, dt, dumps, fields, text, uid, uuid_value
from easyq_common.security import credential_hash
from easyq_common.trainer import (TERMINAL_RUN_STATES, TrainerError, TrainerUnavailable, candidate_session,
                                  queue_sync, trainer_for)

from .admin import decode_cursor, encode_cursor, flag, jsonb, keyset, limit_of, num, page_of, query_keys

LABELS = ('ANSWERABLE', 'INSUFFICIENT_EVIDENCE', 'OPINION', 'REJECT')
EVENT_TYPES = ('AUTO_ESCALATED', 'RETRACTED', 'AUDIENCE_HELPFUL', 'AUDIENCE_UNHELPFUL', 'SECOND_STAGE_INSUFFICIENT')
REVIEW_STATES = ('PENDING', 'DISAGREEMENT', 'APPROVED', 'REJECTED', 'MERGING')
UNUSABLE_DATASET_STATES = ('DELETED', 'FAILED', 'INVALID')   # datasets.status values are undefined (review A-12)
# HTTP status of the 8.4.2 codes a synchronous training-server call can produce.
HTTP_OF = {'VALIDATION_ERROR': 422, 'RESOURCE_NOT_FOUND': 404, 'MODEL_NOT_DEPLOYABLE': 409, 'DEPLOY_CRITERIA_NOT_MET': 409,
           'TRAINING_ALREADY_FINISHED': 409, 'TRAINER_BUSY': 409, 'DATASET_NOT_READY': 409, 'INTERNAL_ERROR': 500}


def dataset_view(r):
    return {'dataset_id': str(r['dataset_id']), 'version_name': r['version_name'], 'status': r['status'],
            'contains_aihub': bool(r['contains_aihub']), 'statistics': jsonb(r['statistics'], {}),
            'script_version': r['script_version'], 'retrieval_config_version': r['retrieval_config_version'],
            'test_set_version': r['test_set_version'], 'test_use_count': r['test_use_count'],
            'change_note': r['change_note'], 'synced_at': r['synced_at']}


def run_view(r):
    return {'training_run_id': str(r['training_run_id']), 'dataset_id': str(r['dataset_id']), 'job_id': r['job_id'],
            'status': r['status'], 'stage': r['stage'], 'hold_reason': r['hold_reason'], 'progress': num(r['progress']),
            'learning_curve': jsonb(r['learning_curve'], []), 'metrics': jsonb(r['metrics'], {}),
            'model_version': r['model_version'], 'deployable': flag(r['deployable']), 'error_code': r['error_code'],
            'synced_at': r['synced_at']}


def model_view(r):
    return {'model_version_id': str(r['model_version_id']), 'version_name': r['version_name'], 'status': r['status'],
            'threshold': num(r['threshold']), 'metrics': jsonb(r['metrics'], {}), 'deployable': bool(r['deployable']),
            'compared_with': r['compared_with'], 'serving': bool(r['serving']), 'served_before': bool(r['served_before']),
            'memo': r['memo'], 'synced_at': r['synced_at']}


def evaluation_view(r):
    return {'evaluation_id': str(r['evaluation_id']), 'dataset_id': str(r['dataset_id']),
            'model_version_id': str(r['model_version_id']) if r['model_version_id'] else None, 'evaluator': r['evaluator'],
            'scope': r['scope'], 'split': r['split'], 'metrics': jsonb(r['metrics'], {}), 'config': jsonb(r['config'], {}),
            'synced_at': r['synced_at']}


def serving_view(snapshot, synced_at, stale):
    s = snapshot or {}
    return {'mode': s.get('mode'), 'model_version': s.get('serving_version'), 'threshold': s.get('threshold'),
            'llm_only_threshold': s.get('llm_only_threshold'), 'llm_model_id': s.get('llm_model_id'),
            'llm_prompt_version': s.get('llm_prompt_version'), 'deployed_at': s.get('deployed_at'),
            'synced_at': synced_at, 'stale': stale}


SNAPSHOT_KEYS = ('mode', 'serving_version', 'previous_version', 'threshold', 'llm_only_threshold', 'llm_model_id',
                 'llm_prompt_version', 'deployed_at')


def snapshot_of(data, mode=None):
    """8.2.27 serving_snapshot from a 7.4.21/7.4.22/7.4.23 answer."""
    snap = {k: data.get(k) for k in SNAPSHOT_KEYS if k in data}
    if mode and not snap.get('mode'):
        snap['mode'] = mode
    return snap


class TrainingMixin:
    ROUTES = [
        ('GET', r'/admin/feedback-candidates', 'admin_candidates'),
        ('PUT', r'/admin/feedback-candidates/(?P<cid>[^/]+)/reviews/me', 'admin_review'),
        ('POST', r'/admin/datasets', 'admin_merge'),
        ('GET', r'/admin/datasets', 'admin_datasets'),
        ('POST', r'/admin/training-runs', 'admin_start_training'),
        ('GET', r'/admin/training-runs/(?P<run_id>[^/]+)', 'admin_training_run'),
        ('DELETE', r'/admin/training-runs/(?P<run_id>[^/]+)', 'admin_stop_training'),
        ('GET', r'/admin/model-versions', 'admin_models'),
        ('GET', r'/admin/evaluation-runs', 'admin_evaluations'),
        ('POST', r'/admin/deployments', 'admin_deploy'),
        ('GET', r'/admin/deployments', 'admin_deployments'),
    ]

    # ---- API-53 · 54 candidates ----------------------------------------------------------------------------------
    def reviewer_ref(self, user_id):
        return 'rv-' + credential_hash(self.settings.hmac_key, 'reviewer', 'label_reviews', str(user_id))[:12]

    def candidate_view(self, tx, c, title, admin):
        reviews = tx.all('SELECT * FROM label_reviews WHERE candidate_id=%s ORDER BY reviewed_at,reviewer_user_id', (c['candidate_id'],))
        # Another reviewer's result is hidden until the current admin has saved their own (8.2.21).
        if not any(str(r['reviewer_user_id']) == admin for r in reviews):
            reviews = []
        return {'candidate_id': str(c['candidate_id']), 'event_type': c['event_type'], 'session_title': title,
                'default_label': c['default_label'], 'sanitized_pair': jsonb(c['sanitized_pair'], {}),
                'retrieved_evidence': jsonb(c['retrieved_evidence'], []), 'judgment_snapshot': jsonb(c['judgment_snapshot'], {}),
                'review_status': c['review_status'], 'final_label': c['final_label'],
                'reviews': [{'reviewer_ref': self.reviewer_ref(r['reviewer_user_id']), 'label': r['label'],
                             'evidence_id': str(r['evidence_id']) if r['evidence_id'] else None, 'comment': r['comment'],
                             'reviewed_at': r['reviewed_at']} for r in reviews]}

    def admin_candidates(self, tx, req, p):
        admin = self.require_admin(p)
        q = req.query
        query_keys(q, ['review_status', 'event_type', 'sort', 'model_version', 'cursor', 'limit'])
        limit = limit_of(q)
        sort = q.get('sort', 'CREATED_AT')
        if sort not in ('CREATED_AT', 'NEAR_THRESHOLD'):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'sort'})
        model_version = q.get('model_version')
        if sort == 'NEAR_THRESHOLD' and not (model_version and re.fullmatch(r'judge-v[0-9]{1,6}', model_version)):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'model_version'})
        where, args = ' WHERE 1=1', []
        if 'review_status' in q:
            if q['review_status'] not in REVIEW_STATES:
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'review_status'})
            where += ' AND review_status=%s'
            args.append(q['review_status'])
        if 'event_type' in q:
            if q['event_type'] not in EVENT_TYPES:
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'event_type'})
            where += ' AND event_type=%s'
            args.append(q['event_type'])
        if sort == 'NEAR_THRESHOLD':
            where += " AND event_type='AUTO_ESCALATED'"
        listed = []
        for c in tx.all('SELECT * FROM feedback_candidates' + where + ' ORDER BY created_at,candidate_id', tuple(args)):
            title = candidate_session(self, tx, c['candidate_id'])
            if title is None:
                continue   # consent withdrawn or evaluation lecture: not shown (the worker deletes it later)
            created = dt(c['created_at']).isoformat()
            if sort == 'NEAR_THRESHOLD':
                snap = jsonb(c['judgment_snapshot'], {})
                score, tau = snap.get('score'), snap.get('threshold')
                if snap.get('model_version') != model_version or not all(isinstance(v, (int, float)) for v in (score, tau)):
                    continue
                key = [abs(tau - score), created, str(c['candidate_id'])]
            else:
                key = [created, str(c['candidate_id'])]
            listed.append((key, c, title))
        listed.sort(key=lambda item: item[0])
        after = decode_cursor(q)
        if after is not None:
            if not isinstance(after, dict) or not isinstance(after.get('k'), list):
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'cursor'})
            remaining = [item for item in listed if item[0] > after['k']]
        else:
            remaining = listed
        page = remaining[:limit]
        cursor = encode_cursor({'k': page[-1][0]}) if len(remaining) > limit else None
        return Result(200, {'items': [self.candidate_view(tx, c, title, admin) for _, c, title in page],
                            'next_cursor': cursor, 'total_count': len(listed)})

    def admin_review(self, tx, req, p, cid):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['label', 'evidence_id', 'comment'], ['label'])
        label = b['label']
        if label not in LABELS:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'label'})
        evidence = uuid_value(b['evidence_id']) if b.get('evidence_id') is not None else None
        comment = text(b['comment'], 1000, 0) if b.get('comment') is not None else None
        c = tx.one('SELECT * FROM feedback_candidates WHERE candidate_id=%s FOR UPDATE', (cid,))
        if not c:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if c['review_status'] == 'MERGING' or c['merge_request_id']:
            raise APIError(409, 'CANDIDATE_NOT_ELIGIBLE', {'reason': 'MERGING'})   # in a merge request: frozen
        title = candidate_session(self, tx, cid)
        if title is None:
            raise APIError(409, 'CANDIDATE_NOT_ELIGIBLE')
        if label == 'ANSWERABLE' and c['event_type'] == 'AUTO_ESCALATED' and not evidence:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'evidence_id'})
        if evidence and label != 'ANSWERABLE':
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'evidence_id'})
        if evidence:
            allowed = {str(e.get('evidence_id')) for e in jsonb(c['retrieved_evidence'], []) if isinstance(e, dict)}
            if evidence not in allowed:
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'evidence_id'})
        reviews = tx.all('SELECT * FROM label_reviews WHERE candidate_id=%s', (cid,))
        mine = next((r for r in reviews if str(r['reviewer_user_id']) == admin), None)
        if not mine and len(reviews) >= 2:
            # Exactly two cross reviewers (8.2.21); a disagreement is settled by one of them reviewing again (P-8).
            raise APIError(409, 'REVIEW_CONFLICT')
        moment = self.clock()
        if mine:
            tx.execute('UPDATE label_reviews SET label=%s,evidence_id=%s,comment=%s,reviewed_at=%s WHERE candidate_id=%s AND reviewer_user_id=%s',
                       (label, evidence, comment, moment, cid, admin))
        else:
            tx.execute('INSERT INTO label_reviews(candidate_id,reviewer_user_id,label,evidence_id,comment,reviewed_at) VALUES(%s,%s,%s,%s,%s,%s)',
                       (cid, admin, label, evidence, comment, moment))
        reviews = tx.all('SELECT label,evidence_id FROM label_reviews WHERE candidate_id=%s', (cid,))
        status, final = 'PENDING', None
        if len(reviews) == 2:
            a, b2 = reviews
            same = a['label'] == b2['label'] and (str(a['evidence_id']) if a['evidence_id'] else None) == (str(b2['evidence_id']) if b2['evidence_id'] else None)
            if not same:
                status = 'DISAGREEMENT'
            elif a['label'] == 'REJECT':
                status = 'REJECTED'   # no correct evidence among the candidates: excluded (6.3.2-2)
            else:
                status, final = 'APPROVED', a['label']
        tx.execute('UPDATE feedback_candidates SET review_status=%s,final_label=%s,review_started_at=COALESCE(review_started_at,%s) WHERE candidate_id=%s',
                   (status, final, moment, cid))
        c = tx.one('SELECT * FROM feedback_candidates WHERE candidate_id=%s', (cid,))
        return Result(200, self.candidate_view(tx, c, title, admin))

    # ---- API-55 · 56 datasets ------------------------------------------------------------------------------------
    def admin_merge(self, tx, req, p):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['base_dataset_version', 'candidate_ids', 'description'], ['base_dataset_version', 'candidate_ids'])
        client, prior = self.idempotent(tx, req, admin, 'DATASET', b)
        if prior:
            return prior
        base = b['base_dataset_version']
        if not isinstance(base, str) or not re.fullmatch(r'ds-v[0-9]{1,6}', base):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'base_dataset_version'})
        ids = b['candidate_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'candidate_ids'})
        ids = list(dict.fromkeys(uuid_value(v) for v in ids))
        description = text(b['description'], 500, 0) if b.get('description') is not None else None
        dataset = tx.one('SELECT dataset_id,status FROM datasets WHERE version_name=%s', (base,))
        if not dataset or dataset['status'] in UNUSABLE_DATASET_STATES:
            raise APIError(409, 'DATASET_NOT_READY')
        tx.lock('candidate-merge')
        bad = []
        for cid in ids:
            c = tx.one('SELECT review_status,merge_request_id FROM feedback_candidates WHERE candidate_id=%s FOR UPDATE', (cid,))
            if not c or c['review_status'] != 'APPROVED' or c['merge_request_id'] or candidate_session(self, tx, cid) is None:
                bad.append(cid)
        if bad:
            raise APIError(409, 'CANDIDATE_NOT_ELIGIBLE', {'candidate_ids': bad})
        merge_id, cutoff = uid(), self.clock()
        for cid in ids:
            tx.execute("UPDATE feedback_candidates SET review_status='MERGING',merge_request_id=%s WHERE candidate_id=%s", (merge_id, cid))
        # cleanup_cutoff_at has no column (review: "API-55의 cleanup_cutoff_at을 저장할 곳이 없다"): it is fixed in the job payload.
        payload = {'merge_request_id': merge_id, 'base_dataset_version': base, 'description': description,
                   'cleanup_cutoff_at': cutoff, 'candidate_count': len(ids)}
        job = self.job(tx, 'DATASET', merge_id, 'dataset:' + merge_id, payload, user=admin, client=client)
        self.audit(tx, req, admin, 'DATASET_MERGED', 'DATASET', base,
                   {'merge_request_id': merge_id, 'candidate_count': len(ids), 'status': 'MERGING'}, description)
        return self.accepted(merge_id, job)

    def admin_datasets(self, tx, req, p):
        admin = self.require_admin(p)
        q = req.query
        query_keys(q, ['status', 'cursor', 'limit'])
        limit = limit_of(q)
        where, args = ' WHERE 1=1', []
        if 'status' in q:
            where += ' AND status=%s'
            args.append(text(q['status'], 20))
        total = tx.one('SELECT count(*) AS n FROM datasets' + where, tuple(args))['n']
        cond, cargs = keyset(q, 'created_at', 'dataset_id')
        rows = tx.all('SELECT * FROM datasets' + where + cond + ' ORDER BY created_at DESC,dataset_id DESC LIMIT %s', (*args, *cargs, limit + 1))
        rows, cursor = page_of(rows, limit, 'created_at', 'dataset_id')
        queue_sync(self, tx, user=admin)
        return Result(200, {'items': [dataset_view(r) for r in rows], 'next_cursor': cursor, 'total_count': total})

    # ---- API-57 · 58 · 59 training -------------------------------------------------------------------------------
    @staticmethod
    def training_config(value):
        """7.4.11 ranges; only epochs · batch_size · learning_rate may be chosen (API-57)."""
        if value is None:
            return {}
        if not isinstance(value, dict) or set(value) - {'epochs', 'batch_size', 'learning_rate'}:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'config'})
        config = {}
        if 'epochs' in value:
            if type(value['epochs']) is not int or not 1 <= value['epochs'] <= 10:
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'config.epochs'})
            config['epochs'] = value['epochs']
        if 'batch_size' in value:
            if type(value['batch_size']) is not int or value['batch_size'] not in (8, 16):
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'config.batch_size'})
            config['batch_size'] = value['batch_size']
        if 'learning_rate' in value:
            rate = value['learning_rate']
            if type(rate) not in (int, float) or not 1e-6 <= rate <= 1e-4:
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'config.learning_rate'})
            config['learning_rate'] = rate
        return config

    def admin_start_training(self, tx, req, p):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['dataset_id', 'config'], ['dataset_id'])
        client, prior = self.idempotent(tx, req, admin, 'TRAIN', b)
        if prior:
            return prior
        dataset_id = uuid_value(b['dataset_id'])
        config = self.training_config(b.get('config'))
        # Training shares the judge GPU: refuse while any lecture is LIVE (6.3.3).
        if tx.one("SELECT session_id FROM lecture_sessions WHERE status='LIVE' LIMIT 1"):
            raise APIError(409, 'LIVE_SESSION_IN_PROGRESS')
        dataset = tx.one('SELECT dataset_id,version_name,status FROM datasets WHERE dataset_id=%s', (dataset_id,))
        if not dataset or dataset['status'] in UNUSABLE_DATASET_STATES:
            raise APIError(409, 'DATASET_NOT_READY')
        run_id = uid()
        tx.execute("""INSERT INTO training_runs(training_run_id,dataset_id,requested_by,status,config,learning_curve,metrics,created_at)
            VALUES(%s,%s,%s,'QUEUED',%s,%s,%s,%s)""", (run_id, dataset_id, admin, dumps(config), '[]', '{}', self.clock()))
        job = self.job(tx, 'TRAIN', run_id, 'train:' + run_id,
                       {'training_run_id': run_id, 'dataset_version': dataset['version_name'], 'config_override': config},
                       user=admin, client=client)
        self.audit(tx, req, admin, 'TRAINING_REQUESTED', 'DATASET', dataset['version_name'],
                   {'training_run_id': run_id, 'config': config})
        return self.accepted(run_id, job)

    def admin_training_run(self, tx, req, p, run_id):
        admin = self.require_admin(p)
        r = tx.one('SELECT * FROM training_runs WHERE training_run_id=%s', (run_id,))
        if not r:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if r['status'] not in TERMINAL_RUN_STATES and r['job_id']:
            queue_sync(self, tx, run_id=run_id, user=admin)
        return Result(200, run_view(r))

    def admin_stop_training(self, tx, req, p, run_id):
        admin = self.require_admin(p)
        fields(req.body, [])
        tx.lock('training-run:' + run_id)
        r = tx.one('SELECT * FROM training_runs WHERE training_run_id=%s', (run_id,))
        if not r:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if r['status'] in TERMINAL_RUN_STATES:
            raise APIError(409, 'TRAINING_ALREADY_FINISHED')
        if r['job_id']:
            try:
                trainer_for(self).stop_training(r['job_id'])   # 7.4.9; CANCELLED itself comes from 7.4.8 (SYNC)
            except TrainerError as exc:
                raise APIError(HTTP_OF.get(exc.code, 500), exc.code if exc.code in HTTP_OF else 'INTERNAL_ERROR') from None
            except (TrainerUnavailable, ValueError):
                raise APIError(503, 'SERVICE_UNAVAILABLE') from None
        else:
            # Not sent to the training server yet: withdraw the queued TRAIN job; SYNC then marks the run CANCELLED.
            cancelled = tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE job_type='TRAIN' AND target_id=%s
                AND status='QUEUED' AND attempts=0""", (self.clock(), run_id)).rowcount
            if not cancelled:
                raise APIError(503, 'SERVICE_UNAVAILABLE')   # the request is in flight; retry once it has a job number
        job = queue_sync(self, tx, run_id=run_id, user=admin, force=True)
        self.audit(tx, req, admin, 'TRAINING_STOPPED', 'MODEL', run_id, {'status': [r['status'], 'CANCELLED']})
        return self.accepted(run_id, job)

    # ---- API-60 · 61 models and evaluations ------------------------------------------------------------------------
    def admin_models(self, tx, req, p):
        admin = self.require_admin(p)
        q = req.query
        query_keys(q, ['status', 'cursor', 'limit'])
        limit = limit_of(q)
        where, args = ' WHERE 1=1', []
        if 'status' in q:
            if q['status'] not in ('REGISTERED', 'DELETED'):
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'status'})
            where += ' AND status=%s'
            args.append(q['status'])
        total = tx.one('SELECT count(*) AS n FROM model_versions' + where, tuple(args))['n']
        cond, cargs = keyset(q, 'synced_at', 'model_version_id')
        rows = tx.all('SELECT * FROM model_versions' + where + cond + ' ORDER BY synced_at DESC,model_version_id DESC LIMIT %s',
                      (*args, *cargs, limit + 1))
        rows, cursor = page_of(rows, limit, 'synced_at', 'model_version_id')
        queue_sync(self, tx, user=admin)
        return Result(200, {'items': [model_view(r) for r in rows], 'next_cursor': cursor, 'total_count': total})

    def admin_evaluations(self, tx, req, p):
        admin = self.require_admin(p)
        q = req.query
        query_keys(q, ['dataset_id', 'scope', 'split', 'model_version_ids'])
        if 'dataset_id' not in q:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'dataset_id'})
        where, args = ' WHERE dataset_id=%s', [uuid_value(q['dataset_id'])]
        for key, allowed in (('scope', ('QUESTION', 'PAIR', 'SERVICE')), ('split', ('VALIDATION', 'TEST'))):
            if key in q:
                if q[key] not in allowed:
                    raise APIError(422, 'VALIDATION_ERROR', {'field': key})
                where += f' AND {key}=%s'
                args.append(q[key])
        if q.get('model_version_ids'):
            ids = [uuid_value(v.strip()) for v in q['model_version_ids'].split(',') if v.strip()]
            if not ids or len(ids) > 20:
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'model_version_ids'})
            where += ' AND model_version_id IN (' + ','.join('%s' for _ in ids) + ')'
            args += ids
        rows = tx.all('SELECT * FROM evaluation_runs' + where + ' ORDER BY synced_at DESC,evaluation_id', tuple(args))
        queue_sync(self, tx, user=admin)
        return Result(200, {'items': [evaluation_view(r) for r in rows], 'next_cursor': None, 'total_count': len(rows)})

    # ---- API-62 · 63 serving -------------------------------------------------------------------------------------
    def admin_deploy(self, tx, req, p):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['mode', 'model_version_id', 'expected_active_deployment_id', 'reason'],
               ['mode', 'expected_active_deployment_id', 'reason'])
        client, prior = self.idempotent(tx, req, admin, 'DEPLOY', b)
        if prior:
            return prior
        mode = b['mode']
        if mode not in ('CUSTOM', 'LLM_ONLY'):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'mode'})
        if (mode == 'CUSTOM') != (b.get('model_version_id') is not None):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'model_version_id'})
        expected = uuid_value(b['expected_active_deployment_id']) if b['expected_active_deployment_id'] is not None else None
        reason = text(b['reason'], 500)
        tx.lock('deployments')
        active = tx.one("SELECT deployment_id FROM deployments WHERE status='ACTIVE'")
        # Before the first deployment there is no ACTIVE row, so null is the expected value then (review A-4).
        if (str(active['deployment_id']) if active else None) != expected:
            raise APIError(409, 'VERSION_CONFLICT')
        if tx.one("SELECT deployment_id FROM deployments WHERE status='PENDING' LIMIT 1"):
            raise APIError(409, 'VERSION_CONFLICT')   # another switch is still being confirmed; the active row will change
        model = None
        if mode == 'CUSTOM':
            model = tx.one('SELECT * FROM model_versions WHERE model_version_id=%s', (uuid_value(b['model_version_id']),))
            if not model or model['status'] != 'REGISTERED':
                raise APIError(409, 'MODEL_NOT_DEPLOYABLE')
            if not model['served_before'] and not model['deployable']:
                raise APIError(409, 'MODEL_NOT_DEPLOYABLE')
        operation = 'SWITCH' if model and not model['served_before'] else 'ROLLBACK'
        target = model['version_name'] if model else 'LLM_ONLY'
        deployment_id = uid()
        tx.execute("""INSERT INTO deployments(deployment_id,model_version_id,mode,operation,requested_by,previous_deployment_id,status,reason,serving_snapshot,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s)""",
            (deployment_id, model['model_version_id'] if model else None, mode, operation, admin,
             active['deployment_id'] if active else None, reason, '{}', self.clock()))
        changes = {'mode': mode, 'model_version': target if model else None, 'route': operation, 'deployment_id': deployment_id}
        self.audit(tx, req, admin, 'DEPLOYMENT_REQUESTED', 'DEPLOYMENT', deployment_id, changes, reason)
        payload = {'deployment_id': deployment_id, 'operation': operation, 'mode': mode, 'target': target, 'reason': reason, 'sent': True}
        # The backend calls 7.4.22 / 7.4.23 directly so a rejection (614 · 610) is a synchronous 409 (API-62);
        # the worker then confirms the real state with 7.4.21 (DEPLOY job) and marks the row ACTIVE.
        try:
            trainer = trainer_for(self)
            answer = trainer.switch(target) if operation == 'SWITCH' else trainer.rollback(target, reason)
            tx.execute('UPDATE deployments SET serving_snapshot=%s WHERE deployment_id=%s', (dumps(snapshot_of(answer, mode)), deployment_id))
        except TrainerError as exc:
            if exc.code == 'DEPLOYMENT_FAILED':
                # 605: load/check failed, existing serving kept. A job failure, not an HTTP error (8.4.1): 202 + FAILED job.
                tx.execute("UPDATE deployments SET status='FAILED',error_code='DEPLOYMENT_FAILED' WHERE deployment_id=%s", (deployment_id,))
                job = self.job(tx, 'DEPLOY', deployment_id, 'deploy:' + deployment_id, {**payload, 'sent': False}, user=admin, client=client)
                tx.execute("UPDATE jobs SET status='FAILED',error_code='DEPLOYMENT_FAILED',finished_at=%s WHERE job_id=%s", (self.clock(), job))
                return self.accepted(deployment_id, job, 'FAILED')
            code = exc.code if exc.code in HTTP_OF else 'INTERNAL_ERROR'
            tx.execute("UPDATE deployments SET status='FAILED',error_code=%s WHERE deployment_id=%s", (code, deployment_id))
            raise APIError(HTTP_OF[code], code, commit=True) from None   # keep the failed request history
        except TrainerUnavailable:
            # Unknown whether it was applied: do not resend here. The DEPLOY job checks 7.4.21 and resends only
            # when the change is not applied (API-62).
            payload['sent'] = False
        job = self.job(tx, 'DEPLOY', deployment_id, 'deploy:' + deployment_id, payload, user=admin, client=client)
        return self.accepted(deployment_id, job)

    def admin_deployments(self, tx, req, p):
        admin = self.require_admin(p)
        q = req.query
        query_keys(q, ['cursor', 'limit'])
        limit = limit_of(q)
        row = tx.one("SELECT serving_snapshot,synced_at FROM deployments WHERE status='ACTIVE'")
        try:
            live = trainer_for(self).serving()   # 7.4.21 is the source of truth
            active = serving_view(snapshot_of(live), self.clock(), False)
            if not row or {k: v for k, v in jsonb(row['serving_snapshot'], {}).items() if k != 'deployed_at'} != \
                    {k: v for k, v in snapshot_of(live).items() if k != 'deployed_at'}:
                queue_sync(self, tx, user=admin)   # the worker updates the ACTIVE snapshot and audits the change
        except (TrainerError, TrainerUnavailable):
            active = serving_view(jsonb(row['serving_snapshot'], {}), row['synced_at'], True) if row else None
        cond, cargs = keyset(q, 'd.created_at', 'd.deployment_id')
        rows = tx.all("""SELECT d.*,u.display_name,m.version_name FROM deployments d JOIN users u ON u.user_id=d.requested_by
            LEFT JOIN model_versions m ON m.model_version_id=d.model_version_id WHERE 1=1""" + cond +
            ' ORDER BY d.created_at DESC,d.deployment_id DESC LIMIT %s', (*cargs, limit + 1))
        rows, cursor = page_of(rows, limit, 'created_at', 'deployment_id')
        items = [{'deployment_id': str(r['deployment_id']), 'requested_by': r['display_name'], 'requested_mode': r['mode'],
                  'requested_model_version': r['version_name'], 'route': r['operation'], 'status': r['status'],
                  'reason': r['reason'], 'error_code': r['error_code'], 'created_at': r['created_at'], 'synced_at': r['synced_at']}
                 for r in rows]
        return Result(200, {'active': active, 'items': items, 'next_cursor': cursor})


OPENAPI = [
    ('GET', '/admin/feedback-candidates', 'API-53', '라벨 후보 목록'),
    ('PUT', '/admin/feedback-candidates/{cid}/reviews/me', 'API-54', '검수 결과 저장'),
    ('POST', '/admin/datasets', 'API-55', '승인 후보 병합(202)'),
    ('GET', '/admin/datasets', 'API-56', '데이터셋 버전 · 분포'),
    ('POST', '/admin/training-runs', 'API-57', '학습 · 재학습 요청(202)'),
    ('GET', '/admin/training-runs/{run_id}', 'API-58', '학습 진행 · 결과'),
    ('DELETE', '/admin/training-runs/{run_id}', 'API-59', '학습 중단(202)'),
    ('GET', '/admin/model-versions', 'API-60', '모델 버전 목록'),
    ('GET', '/admin/evaluation-runs', 'API-61', '평가 결과'),
    ('POST', '/admin/deployments', 'API-62', '서빙 전환 · 롤백 · LLM_ONLY(202)'),
    ('GET', '/admin/deployments', 'API-63', '현재 서빙 설정 · 전환 이력'),
]
