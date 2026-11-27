"""DATASET, TRAIN, DEPLOY and SYNC jobs against the training server (6.3.2~6.3.4, 7.4, 8.2.23~8.2.27).

All copies of the training registry (datasets, training_runs, model_versions, evaluation_runs) and the ACTIVE
deployments snapshot are written here with the easyq_worker DB role; values from the training server always win.
Training-server calls go through the fixed adapter easyq_common.trainer.

Failures that the design reports as job results (API-57: 603/609 -> FAILED VALIDATION_ERROR / TRAINER_BUSY,
605 -> DEPLOYMENT_FAILED, ...) are recorded here with fail_job() because the runner's generic retry path would turn
them into SERVICE_UNAVAILABLE. Network failures are re-raised so the runner retries with backoff; on the last attempt
the handler records the final state itself.
"""
from datetime import timedelta
import sys

from easyq_common.contracts import APIError, dt, dumps, obj, uid
from easyq_common.trainer import (SYNC_ALL_TARGET, TERMINAL_RUN_STATES, TrainerError, TrainerUnavailable,
                                  candidate_session, queue_sync, service_code, trainer_for)

RUN_STATES = ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'HELD', 'CANCELLED')
STAGES = ('train', 'export', 'eval', 'register')
HOLD_REASONS = ('precision_drop', 'below_target')
RUN_POLL = timedelta(seconds=30)
SYNC_EVERY = timedelta(hours=1)


def jsonb(value, default=None):
    value = obj(value) if value is not None else None
    return default if value is None else value


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class SyncJob:
    JOB_HANDLERS = {'DATASET': 'dataset_job', 'TRAIN': 'train_job', 'DEPLOY': 'deploy_job', 'SYNC': 'sync_job'}

    # ---- common ------------------------------------------------------------------------------------------------------
    def trainer_api(self):
        return trainer_for(self)

    def last_attempt(self, job):
        return job['attempts'] >= self.MAX_ATTEMPTS

    def fail_job(self, tx, job, code, result=None):
        """Terminal failure with an 8.4.2 code (shown by API-41), without the runner's retry."""
        self.active(tx, job)
        tx.execute("""UPDATE jobs SET status='FAILED',error_code=%s,result=%s,finished_at=%s,lease_until=NULL
            WHERE job_id=%s AND lease_token=%s""", (code, dumps(result or {}), self.service.clock(), job['job_id'], job['lease_token']))

    def worker_audit(self, tx, action, target_type, target_ref, changes, reason=None):
        tx.execute("""INSERT INTO audit_events(audit_event_id,actor_user_id,action,target_type,target_ref,changes,reason,request_id,created_at)
            VALUES(%s,NULL,%s,%s,%s,%s,%s,%s,%s)""", (uid(), action, target_type, str(target_ref)[:100], dumps(changes), reason, uid(), self.service.clock()))

    @staticmethod
    def log_sync(event, code, meta):
        from datetime import datetime, timezone
        line = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': 'WARNING', 'service': 'worker', 'event': event,
                'request_id': None, 'trace_id': None, 'code': code, 'message': event, 'meta': meta}
        print(dumps(line), file=sys.stderr, flush=True)

    def ensure_periodic_sync(self):
        """Hook for the maintenance loop: keep one hourly SYNC job queued (8.2.25)."""
        with self.db.transaction() as tx:
            queue_sync(self.service, tx, when=self.service.clock() + SYNC_EVERY)

    # ---- SYNC ----------------------------------------------------------------------------------------------------
    def sync_job(self, job):
        payload = jsonb(job['payload'], {})
        try:
            if payload.get('scope') == 'TRAINING_RUN':
                again = self.sync_training_run(payload['training_run_id'])
            else:
                self.sync_all()
                again = False
        except (TrainerUnavailable, TrainerError) as exc:
            if not self.last_attempt(job):
                raise
            code = exc.code if isinstance(exc, TrainerError) else 'SERVICE_UNAVAILABLE'
            with self.db.transaction() as tx:
                self.fail_job(tx, job, code)
                self.reschedule(tx, job, payload)
            return
        with self.db.transaction() as tx:
            self.finish(tx, job, {'scope': payload.get('scope', 'ALL')})
            if again or payload.get('scope') != 'TRAINING_RUN':
                self.reschedule(tx, job, payload)

    def reschedule(self, tx, job, payload):
        moment = self.service.clock()
        if payload.get('scope') == 'TRAINING_RUN':
            run = tx.one('SELECT status FROM training_runs WHERE training_run_id=%s', (payload['training_run_id'],))
            if run and run['status'] not in TERMINAL_RUN_STATES:
                queue_sync(self.service, tx, run_id=payload['training_run_id'], when=moment + RUN_POLL)
        else:
            queue_sync(self.service, tx, when=moment + SYNC_EVERY)

    def sync_all(self):
        """Hourly / on admin views: datasets, models, evaluations, serving snapshot, open runs, stuck merges."""
        self.sync_datasets()
        models = self.sync_models()
        self.sync_evaluations([m.get('evaluation_id') for m in models])
        self.sync_serving()
        with self.db.transaction() as tx:
            runs = tx.all("SELECT training_run_id FROM training_runs WHERE status IN ('QUEUED','RUNNING') AND job_id IS NOT NULL")
            for r in runs:
                queue_sync(self.service, tx, run_id=str(r['training_run_id']))
            merges = tx.all("""SELECT DISTINCT f.merge_request_id FROM feedback_candidates f WHERE f.review_status='MERGING'
                AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_type='DATASET' AND j.target_id=f.merge_request_id AND j.status IN ('QUEUED','RUNNING'))""")
        for m in merges:
            self.reconcile_merge(str(m['merge_request_id']))

    def sync_datasets(self):
        api, items, page = self.trainer_api(), [], 1
        while True:
            data = api.list_datasets(page=page, size=100)
            batch = data.get('datasets') or []
            items += batch
            if not batch or len(items) >= int(data.get('total_count') or 0):
                break
            page += 1
        for item in items:
            version = item.get('dataset_version')
            if not isinstance(version, str):
                continue
            detail = api.dataset(version)   # 7.4.4 has the val/test/es versions the list lacks
            self.upsert_dataset({**item, **detail, 'dataset_version': version})

    def upsert_dataset(self, d):
        moment = self.service.clock()
        values = (d.get('parent_version'), d.get('status') or 'ACTIVE', bool(d.get('contains_aihub')), dumps(d.get('statistics') or {}),
                  d.get('script_version') or '', d.get('retrieval_config_version') or '', d.get('validation_set_version') or '',
                  d.get('test_set_version') or '', d.get('eval_slide_version') or '', d.get('test_use_count'),
                  d.get('change_note') or '', d.get('created_at') or moment, moment)
        with self.db.transaction() as tx:
            row = tx.one('SELECT dataset_id FROM datasets WHERE version_name=%s FOR UPDATE', (d['dataset_version'],))
            if row:
                tx.execute("""UPDATE datasets SET parent_version=%s,status=%s,contains_aihub=%s,statistics=%s,script_version=%s,
                    retrieval_config_version=%s,validation_set_version=%s,test_set_version=%s,eval_slide_version=%s,test_use_count=%s,
                    change_note=%s,created_at=%s,synced_at=%s WHERE dataset_id=%s""", (*values, row['dataset_id']))
            else:
                tx.execute("""INSERT INTO datasets(parent_version,status,contains_aihub,statistics,script_version,retrieval_config_version,
                    validation_set_version,test_set_version,eval_slide_version,test_use_count,change_note,created_at,synced_at,dataset_id,version_name)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (*values, uid(), d['dataset_version']))

    def sync_models(self):
        """7.4.17 · 7.4.18 overwrite; models gone from the training server become DELETED; one serving row at most."""
        api = self.trainer_api()
        listed = api.list_models().get('models') or []
        moment, models = self.service.clock(), []
        for m in listed:
            version = m.get('model_version')
            if not isinstance(version, str):
                continue
            detail = api.model(version)
            manifest = detail.get('manifest') or {}
            models.append({**m, **{k: v for k, v in detail.items() if v is not None}, 'manifest': manifest,
                           'evaluation_id': detail.get('evaluation_id')})
        serving = next((m['model_version'] for m in models if m.get('serving')), None)
        with self.db.transaction() as tx:
            tx.lock('model-versions-sync')
            # Clear the old serving flag first: model_versions has a partial UNIQUE on serving.
            tx.execute('UPDATE model_versions SET serving=false WHERE serving=true AND version_name<>%s', (serving or '',))
            for m in models:
                self.upsert_model(tx, m, moment)
            names = [m['model_version'] for m in models]
            marks = ','.join('%s' for _ in names) or "''"
            tx.execute(f"UPDATE model_versions SET status='DELETED',serving=false,synced_at=%s WHERE version_name NOT IN ({marks}) AND status<>'DELETED'",
                       (moment, *names))
        return models

    def upsert_model(self, tx, m, moment):
        manifest = m['manifest']
        version = m['model_version']
        sha = manifest.get('artifact_sha256') or m.get('artifact_sha256') or ''
        if len(sha) != 64:
            sha = '0' * 64   # 7.4.17/7.4.18 may omit it (review D-4); verification is the training server's job
        # artifact_uri is NOT NULL but absent from 7.4.17/7.4.18 (review D-4): use the 7.4.29 package location.
        uri = m.get('artifact_uri') or manifest.get('artifact_uri') or f'/home/t26112/easyq-data/models/{version}/'
        threshold = number(m.get('threshold')) if number(m.get('threshold')) is not None else number(manifest.get('threshold'))
        metrics = {k: m.get(k) for k in ('roc_auc', 'false_answer_rate', 'tpr', 'precision_auc_drop', 'latency_p95_ms') if k in m}
        if m.get('baselines') is not None:
            metrics['baselines'] = m['baselines']
        dataset = tx.one('SELECT validation_set_version,test_set_version FROM datasets WHERE version_name=%s', (m.get('dataset_version') or '',))
        contract = {'format': manifest.get('format') or 'pytorch-fp16',
                    'validation_set_version': dataset['validation_set_version'] if dataset else None,
                    'test_set_version': m.get('test_set_version') or (dataset['test_set_version'] if dataset else None),
                    'threshold_rule': 'max_fpr_0.04', 'evaluation_id': m.get('evaluation_id')}
        run = tx.one('SELECT training_run_id FROM training_runs WHERE model_version=%s ORDER BY created_at DESC LIMIT 1', (version,))
        values = (run['training_run_id'] if run else None, uri, sha, threshold if threshold is not None else 0,
                  m.get('status') if m.get('status') in ('REGISTERED', 'DELETED') else 'REGISTERED', bool(m.get('deployable')),
                  m.get('compared_with'), bool(m.get('serving')), bool(m.get('served_before')), dumps(metrics), dumps(contract),
                  (m.get('memo') or None) and str(m['memo'])[:500], moment)
        row = tx.one('SELECT model_version_id FROM model_versions WHERE version_name=%s FOR UPDATE', (version,))
        if row:
            tx.execute("""UPDATE model_versions SET training_run_id=COALESCE(%s,training_run_id),artifact_uri=%s,artifact_sha256=%s,threshold=%s,
                status=%s,deployable=%s,compared_with=%s,serving=%s,served_before=%s,metrics=%s,evaluation_contract=%s,memo=%s,synced_at=%s
                WHERE model_version_id=%s""", (*values, row['model_version_id']))
        else:
            tx.execute("""INSERT INTO model_versions(training_run_id,artifact_uri,artifact_sha256,threshold,status,deployable,compared_with,
                serving,served_before,metrics,evaluation_contract,memo,synced_at,model_version_id,version_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (*values, uid(), version))

    def sync_evaluations(self, evaluation_ids):
        api = self.trainer_api()
        for eid in dict.fromkeys(e for e in evaluation_ids if isinstance(e, str)):
            try:
                data = api.evaluation(eid)
            except TrainerError as exc:
                if exc.code == 'RESOURCE_NOT_FOUND':
                    continue
                raise
            self.store_evaluation(eid, data)

    def store_evaluation(self, eid, e):
        """One 7.4.13 result -> rows per evaluator/scope/split (8.2.26)."""
        moment = self.service.clock()
        with self.db.transaction() as tx:
            dataset = tx.one('SELECT dataset_id FROM datasets WHERE version_name=%s', (e.get('dataset_version') or '',))
            if not dataset:
                return   # the dataset copy arrives first on the next SYNC
            model = None
            for m in tx.all('SELECT model_version_id,evaluation_contract FROM model_versions'):
                if jsonb(m['evaluation_contract'], {}).get('evaluation_id') == eid:
                    model = m
                    break
            base = {'model_format': 'fp16', 'retrieval_config_version': e.get('retrieval_config_version'),
                    'validation_set_version': e.get('validation_set_version'), 'test_set_version': e.get('test_set_version'),
                    'candidates': 20}
            pick = lambda *keys: {k: e[k] for k in keys if k in e}   # noqa: E731
            rows = [('CUSTOM', 'QUESTION', 'TEST', pick('threshold', 'roc_auc', 'false_answer_rate', 'tpr', 'latency_p95_ms', 'eval_counts',
                                                        'escalation_by_label', 'by_question_source', 'roc_points'), base),
                    ('CUSTOM', 'QUESTION', 'VALIDATION', pick('valid_auc_fp32', 'valid_auc_fp16', 'precision_auc_drop'),
                     {**base, 'model_format': 'fp16/fp32'}),
                    ('CUSTOM', 'PAIR', 'TEST', pick('pair_roc_auc'), base),
                    ('CUSTOM', 'SERVICE', 'TEST', dict(e.get('service_metrics') or {}), base)]
            llm = e.get('llm_only') or {}
            for b in e.get('baselines') or []:
                if b.get('evaluator') in ('SIMILARITY', 'PRETRAINED', 'SONNET_ONLY'):
                    config = {**base, **({'llm_model_id': llm.get('llm_model_id'), 'llm_prompt_version': llm.get('llm_prompt_version')}
                                         if b['evaluator'] == 'SONNET_ONLY' else {})}
                    rows.append((b['evaluator'], 'QUESTION', 'TEST', {k: v for k, v in b.items() if k != 'evaluator'}, config))
            for evaluator, scope, split, metrics, config in rows:
                if not metrics:
                    continue
                model_id = model['model_version_id'] if model and evaluator == 'CUSTOM' else None
                row = tx.one('SELECT evaluation_id FROM evaluation_runs WHERE external_evaluation_id=%s AND evaluator=%s AND scope=%s AND split=%s',
                             (eid, evaluator, scope, split))
                values = (model_id, dataset['dataset_id'], dumps(config), dumps(metrics), e.get('test_use_count'), moment)
                if row:
                    tx.execute("""UPDATE evaluation_runs SET model_version_id=%s,dataset_id=%s,config=%s,metrics=%s,test_use_count=%s,synced_at=%s
                        WHERE evaluation_id=%s""", (*values, row['evaluation_id']))
                else:
                    tx.execute("""INSERT INTO evaluation_runs(model_version_id,dataset_id,config,metrics,test_use_count,synced_at,
                        evaluation_id,external_evaluation_id,evaluator,scope,split) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (*values, uid(), eid, evaluator, scope, split))

    def sync_serving(self):
        """7.4.21 -> ACTIVE deployments.serving_snapshot (review A-8). A change made directly on the training server is
        written to the snapshot and audited (8.2.27). Without an ACTIVE row nothing is stored (worker has no INSERT)."""
        live = self.trainer_api().serving()
        keys = ('mode', 'serving_version', 'previous_version', 'threshold', 'llm_only_threshold', 'llm_model_id',
                'llm_prompt_version', 'deployed_at')
        snapshot = {k: live.get(k) for k in keys if k in live}
        with self.db.transaction() as tx:
            row = tx.one("SELECT deployment_id,serving_snapshot FROM deployments WHERE status='ACTIVE' FOR UPDATE")
            if not row:
                return
            old = jsonb(row['serving_snapshot'], {})
            tx.execute('UPDATE deployments SET serving_snapshot=%s,synced_at=%s WHERE deployment_id=%s',
                       (dumps(snapshot), self.service.clock(), row['deployment_id']))
            if {k: v for k, v in old.items() if k != 'deployed_at'} != {k: v for k, v in snapshot.items() if k != 'deployed_at'}:
                self.worker_audit(tx, 'SERVING_CONFIG_SYNCED', 'DEPLOYMENT', row['deployment_id'], {'before': old, 'after': snapshot})

    def sync_training_run(self, run_id):
        """7.4.8 -> training_runs. Returns True while the run is still open (poll again)."""
        with self.db.transaction() as tx:
            run = tx.one('SELECT * FROM training_runs WHERE training_run_id=%s', (run_id,))
            if not run or run['status'] in TERMINAL_RUN_STATES:
                return False
            if not run['job_id']:
                train = tx.one("SELECT status,error_code FROM jobs WHERE job_type='TRAIN' AND target_id=%s ORDER BY created_at DESC LIMIT 1", (run_id,))
                if train and train['status'] in ('CANCELLED', 'FAILED'):
                    status = 'CANCELLED' if train['status'] == 'CANCELLED' else 'FAILED'
                    tx.execute('UPDATE training_runs SET status=%s,error_code=%s,finished_at=%s,synced_at=%s WHERE training_run_id=%s',
                               (status, train['error_code'], self.service.clock(), self.service.clock(), run_id))
                    return False
                return train is not None and train['status'] in ('QUEUED', 'RUNNING')
            job_id = run['job_id']
        data = self.trainer_api().training_status(job_id)
        status = data.get('status') if data.get('status') in RUN_STATES else run['status']
        error = None
        if status == 'FAILED':
            error = service_code(data.get('error_code')) or (data.get('error_code') if data.get('error_code') in ('TRAINING_FAILED',) else 'TRAINING_FAILED')
        elif status == 'HELD':
            error = 'TRAINING_HELD'
        metrics = dict(data.get('metrics') or {})
        moment = self.service.clock()
        progress = number(data.get('progress'))
        with self.db.transaction() as tx:
            tx.execute("""UPDATE training_runs SET status=%s,stage=%s,hold_reason=%s,progress=%s,learning_curve=%s,metrics=%s,
                model_version=%s,deployable=%s,error_code=%s,finished_at=%s,synced_at=%s WHERE training_run_id=%s""",
                (status, data.get('stage') if data.get('stage') in STAGES else run['stage'],
                 data.get('hold_reason') if data.get('hold_reason') in HOLD_REASONS else None,
                 None if progress is None else max(0.0, min(1.0, round(progress, 3))),
                 dumps(data.get('curve') or jsonb(run['learning_curve'], [])), dumps(metrics),
                 data.get('model_version') if isinstance(data.get('model_version'), str) else None,
                 data.get('deployable') if isinstance(data.get('deployable'), bool) else None, error,
                 moment if status in TERMINAL_RUN_STATES else None, moment, run_id))
        if status == 'SUCCEEDED':
            self.sync_datasets()
            models = self.sync_models()
            self.sync_evaluations([data.get('evaluation_id')] + [m.get('evaluation_id') for m in models])
        return status not in TERMINAL_RUN_STATES

    # ---- TRAIN (API-57 -> 7.4.7) -----------------------------------------------------------------------------------
    def train_job(self, job):
        payload = jsonb(job['payload'], {})
        run_id = payload['training_run_id']
        with self.db.transaction() as tx:
            self.active(tx, job)
            run = tx.one('SELECT status,job_id FROM training_runs WHERE training_run_id=%s', (run_id,))
            if not run or run['status'] != 'QUEUED' or run['job_id']:
                self.finish(tx, job, {'skipped': True})
                return
        try:
            data = self.trainer_api().start_training(payload['dataset_version'], run_id, payload.get('config_override'))
        except TrainerError as exc:
            # 603 / 609 / 613: 202 stays; the job and the run show FAILED with the service code (API-57, P-11).
            self.end_run(job, run_id, exc.code)
            return
        except TrainerUnavailable:
            if not self.last_attempt(job):
                raise   # same request_id: the training server runs it once (7.4.7)
            self.end_run(job, run_id, 'SERVICE_UNAVAILABLE')
            return
        external = data.get('job_id')
        with self.db.transaction() as tx:
            self.active(tx, job)
            tx.execute("""UPDATE training_runs SET job_id=%s,status=%s,synced_at=%s WHERE training_run_id=%s AND job_id IS NULL""",
                       (external if isinstance(external, str) else None,
                        data.get('status') if data.get('status') in ('QUEUED', 'RUNNING') else 'QUEUED', self.service.clock(), run_id))
            self.finish(tx, job, {'training_job_id': external})
            queue_sync(self.service, tx, run_id=run_id, when=self.service.clock() + RUN_POLL)

    def end_run(self, job, run_id, code):
        with self.db.transaction() as tx:
            self.fail_job(tx, job, code)
            tx.execute("UPDATE training_runs SET status='FAILED',error_code=%s,finished_at=%s,synced_at=%s WHERE training_run_id=%s",
                       (code, self.service.clock(), self.service.clock(), run_id))

    # ---- DATASET (API-55 -> 7.4.5) ---------------------------------------------------------------------------------
    def merge_items(self, tx, merge_id):
        """Sanitized pairs + final label for 7.4.5. Candidates that are no longer eligible are deleted (8.2.37)."""
        items, dropped = [], 0
        for c in tx.all("SELECT * FROM feedback_candidates WHERE merge_request_id=%s AND review_status='MERGING' FOR UPDATE", (merge_id,)):
            if candidate_session(self.service, tx, c['candidate_id']) is None:
                self.delete_candidates(tx, 'candidate_id=%s', (c['candidate_id'],))
                dropped += 1
                continue
            pair = jsonb(c['sanitized_pair'], {})
            item = {k: pair.get(k) for k in ('question_text', 'evidence_id', 'evidence_type', 'evidence_text', 'page_no', 'start_ms', 'end_ms', 'course_id')}
            chosen = tx.one('SELECT evidence_id FROM label_reviews WHERE candidate_id=%s AND evidence_id IS NOT NULL LIMIT 1', (c['candidate_id'],))
            if chosen:
                # The reviewers picked the correct evidence among the retrieved candidates (6.3.2-2). retrieved_evidence
                # may lack page_no/start_ms/end_ms (review A-17); missing location fields are sent as null.
                hit = next((e for e in jsonb(c['retrieved_evidence'], []) if str(e.get('evidence_id')) == str(chosen['evidence_id'])), None)
                if hit:
                    item.update(evidence_id=str(hit['evidence_id']), evidence_type=hit.get('evidence_type'), evidence_text=hit.get('evidence_text'),
                                page_no=hit.get('page_no'), start_ms=hit.get('start_ms'), end_ms=hit.get('end_ms'))
            item.update(label=c['final_label'], event_type=c['event_type'])
            items.append(item)
        return items, dropped

    @staticmethod
    def delete_candidates(tx, condition, args):
        """Candidate, its reviews and provenance link (explicit, not only FK CASCADE)."""
        ids = [r['candidate_id'] for r in tx.all('SELECT candidate_id FROM feedback_candidates WHERE ' + condition, args)]
        for cid in ids:
            tx.execute('DELETE FROM label_reviews WHERE candidate_id=%s', (cid,))
            tx.execute('DELETE FROM candidate_provenance WHERE candidate_id=%s', (cid,))
            tx.execute('DELETE FROM feedback_candidates WHERE candidate_id=%s', (cid,))
        return len(ids)

    def dataset_job(self, job):
        payload = jsonb(job['payload'], {})
        merge_id = payload['merge_request_id']
        with self.db.transaction() as tx:
            self.active(tx, job)
            items, dropped = self.merge_items(tx, merge_id)
            if not items:
                self.fail_job(tx, job, 'CANDIDATE_NOT_ELIGIBLE', {'dropped': dropped})
                return
        try:
            result = self.trainer_api().merge_labels(payload['base_dataset_version'], merge_id, items, payload.get('description'))
        except TrainerError as exc:
            # Refused (601 / 613): nothing was merged, so the candidates go back to APPROVED (review A-3 recovery rule).
            with self.db.transaction() as tx:
                self.fail_job(tx, job, exc.code)
                tx.execute("UPDATE feedback_candidates SET review_status='APPROVED',merge_request_id=NULL WHERE merge_request_id=%s AND review_status='MERGING'",
                           (merge_id,))
            return
        except TrainerUnavailable:
            if not self.last_attempt(job):
                raise
            # Unknown whether merged: candidates stay MERGING; the hourly SYNC re-posts the same merge_request_id
            # (7.4.5 is idempotent per request_id) and completes or restores them (reconcile_merge).
            with self.db.transaction() as tx:
                self.fail_job(tx, job, 'SERVICE_UNAVAILABLE')
            return
        stats = self.complete_merge(merge_id, payload, result)
        with self.db.transaction() as tx:
            self.finish(tx, job, {**stats, 'dropped': dropped})

    def complete_merge(self, merge_id, payload, result):
        """6.3.2-4: drop candidate links, reviews and provenance; then old unreviewed AUTO_ESCALATED candidates."""
        cutoff = payload.get('cleanup_cutoff_at')
        with self.db.transaction() as tx:
            self.delete_candidates(tx, 'merge_request_id=%s', (merge_id,))
            if cutoff:
                self.delete_candidates(tx, """event_type='AUTO_ESCALATED' AND review_status='PENDING' AND merge_request_id IS NULL
                    AND review_started_at IS NULL AND created_at<%s
                    AND NOT EXISTS (SELECT 1 FROM label_reviews r WHERE r.candidate_id=feedback_candidates.candidate_id)""", (dt(cutoff),))
        version = result.get('dataset_version')
        if isinstance(version, str):
            try:
                detail = self.trainer_api().dataset(version)
                self.upsert_dataset({**detail, 'dataset_version': version, 'parent_version': detail.get('parent_version') or payload.get('base_dataset_version'),
                                     'change_note': detail.get('change_note') or payload.get('description') or ''})
            except (TrainerError, TrainerUnavailable):
                pass   # the hourly SYNC copies it later
        return {k: result.get(k) for k in ('dataset_version', 'added', 'excluded_overlap', 'masked', 'skipped')}

    def reconcile_merge(self, merge_id):
        """MERGING candidates whose DATASET job ended without an answer (8.2.22, 8.2.37). Also usable by PURGE."""
        with self.db.transaction() as tx:
            job = tx.one("SELECT payload FROM jobs WHERE job_type='DATASET' AND target_id=%s ORDER BY created_at DESC LIMIT 1", (merge_id,))
            if not job:
                return None
            payload = jsonb(job['payload'], {})
            items, _ = self.merge_items(tx, merge_id)
        if not items:
            return None
        try:
            result = self.trainer_api().merge_labels(payload['base_dataset_version'], merge_id, items, payload.get('description'))
        except TrainerError:
            with self.db.transaction() as tx:
                tx.execute("UPDATE feedback_candidates SET review_status='APPROVED',merge_request_id=NULL WHERE merge_request_id=%s AND review_status='MERGING'",
                           (merge_id,))
            return 'RESTORED'
        except TrainerUnavailable:
            return None
        self.complete_merge(merge_id, payload, result)
        return 'MERGED'

    # ---- DEPLOY (API-62 confirmation with 7.4.21) ------------------------------------------------------------------
    def deploy_job(self, job):
        payload = jsonb(job['payload'], {})
        deployment_id = payload['deployment_id']
        with self.db.transaction() as tx:
            self.active(tx, job)
            d = tx.one('SELECT status FROM deployments WHERE deployment_id=%s', (deployment_id,))
            if not d or d['status'] != 'PENDING':
                self.finish(tx, job, {'skipped': True})
                return
        api = self.trainer_api()
        try:
            live = api.serving()
            applied = self.applied(live, payload)
            if not applied and not payload.get('sent'):
                # The backend could not tell whether its call arrived and 7.4.21 shows it is not applied: send once now.
                if payload['operation'] == 'SWITCH':
                    api.switch(payload['target'])
                else:
                    api.rollback(payload['target'], payload.get('reason') or '')
                live = api.serving()
                applied = self.applied(live, payload)
        except TrainerError as exc:
            self.fail_deployment(job, deployment_id, exc.code)
            return
        except TrainerUnavailable:
            if not self.last_attempt(job):
                raise
            self.fail_deployment(job, deployment_id, 'DEPLOYMENT_FAILED')
            return
        if not applied:
            if not self.last_attempt(job):
                raise APIError(409, 'DEPLOYMENT_PENDING')   # retried by the runner with backoff
            self.fail_deployment(job, deployment_id, 'DEPLOYMENT_FAILED')
            return
        snapshot = {k: live.get(k) for k in ('mode', 'serving_version', 'previous_version', 'threshold', 'llm_only_threshold',
                                                'llm_model_id', 'llm_prompt_version', 'deployed_at') if k in live}
        moment = self.service.clock()
        with self.db.transaction() as tx:
            self.active(tx, job)
            tx.lock('deployments')
            # Lower the old ACTIVE row first: deployments has a partial UNIQUE on ACTIVE.
            tx.execute("UPDATE deployments SET status='SUPERSEDED' WHERE status='ACTIVE' AND deployment_id<>%s", (deployment_id,))
            tx.execute("UPDATE deployments SET status='ACTIVE',serving_snapshot=%s,synced_at=%s,error_code=NULL WHERE deployment_id=%s",
                       (dumps(snapshot), moment, deployment_id))
            self.finish(tx, job, {'mode': snapshot.get('mode'), 'serving_version': snapshot.get('serving_version')})
        try:
            self.sync_models()   # serving / served_before come only from the training server (8.2.27)
        except (TrainerError, TrainerUnavailable):
            pass

    @staticmethod
    def applied(live, payload):
        if live.get('mode') != payload['mode']:
            return False
        return payload['mode'] == 'LLM_ONLY' or live.get('serving_version') == payload['target']

    def fail_deployment(self, job, deployment_id, code):
        with self.db.transaction() as tx:
            self.fail_job(tx, job, code)
            tx.execute("UPDATE deployments SET status='FAILED',error_code=%s WHERE deployment_id=%s AND status='PENDING'", (code, deployment_id))


__all__ = ['SyncJob', 'SYNC_ALL_TARGET']
