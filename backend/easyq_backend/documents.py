"""Teaching material (API-21 · 22 · 23): upload validation, storage, INGEST jobs, retry."""
from io import BytesIO
from pathlib import PurePath
import zipfile


from easyq_common.contracts import (APIError, Result, dumps, fields,
                                    obj, uid, uuid_value)
from easyq_common.contracts import PDF, PPTX


def validate_document(filename, media, data, settings):
    if not isinstance(filename, str) or not filename or len(filename) > 255 or '/' in filename or '\\' in filename:
        raise APIError(422, 'VALIDATION_ERROR')
    if len(data) > settings.upload_limit:
        raise APIError(413, 'FILE_TOO_LARGE')
    ext = PurePath(filename).suffix.lower()
    if ext == '.pdf' and media == PDF and data.startswith(b'%PDF-'):
        return
    if ext != '.pptx' or media != PPTX or not zipfile.is_zipfile(BytesIO(data)):
        raise APIError(415, 'FILE_TYPE_UNSUPPORTED')
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(infos) > 10000 or sum(i.file_size for i in infos) > settings.expanded_limit:
                raise APIError(413, 'FILE_TOO_LARGE')
            if len(names) != len(set(names)) or any('..' in i.filename.split('/') or i.filename.startswith('/') or i.flag_bits & 1 for i in infos):
                raise APIError(415, 'FILE_TYPE_UNSUPPORTED')
            if '[Content_Types].xml' not in names or 'ppt/presentation.xml' not in names or any(n.endswith('vbaProject.bin') for n in names):
                raise APIError(415, 'FILE_TYPE_UNSUPPORTED')
            # Read bounded entries now to check CRC and actual decompression, not just headers.
            for item in infos:
                with archive.open(item) as stream:
                    count = 0
                    while part := stream.read(65536):
                        count += len(part)
                        if count > settings.expanded_limit:
                            raise APIError(413, 'FILE_TOO_LARGE')
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise APIError(415, 'FILE_TYPE_UNSUPPORTED') from None


class DocumentsMixin:
    # ---- API-23 교안 처리 재시도 ------------------------------------------------------
    PARSE_FAILED = 'DOCUMENT_PARSE_FAILED'

    def document_retryable(self, document):
        return document['status'] == 'FAILED' and document['error_code'] != self.PARSE_FAILED

    def retry_document(self, tx, req, p, sid, did):
        import hashlib
        s = self.lecture(tx, sid, p, True)
        fields(req.body, [])
        key = uuid_value(req.headers.get('idempotency-key'))
        job_key = 'ingest-retry:' + str(p['user_id']) + ':' + did + ':' + key
        fingerprint = hashlib.sha256(('retry:' + sid + ':' + did).encode()).hexdigest()
        tx.lock(job_key)
        prior = tx.one('SELECT * FROM jobs WHERE idempotency_key=%s', (job_key,))
        if prior:
            if obj(prior['payload']).get('fingerprint') != fingerprint:
                raise APIError(409, 'IDEMPOTENCY_CONFLICT')
            return Result(202, {'resource_id': prior['target_id'], 'job_id': prior['job_id'], 'status': prior['status']}, {'Location': '/api/v1/jobs/' + str(prior['job_id'])})
        document = tx.one('SELECT * FROM documents WHERE session_id=%s AND document_id=%s FOR UPDATE', (sid, did))
        if not document or document['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if s['status'] not in ('DRAFT', 'LIVE'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if not self.document_retryable(document):
            raise APIError(409, 'DOCUMENT_NOT_RETRYABLE')
        # Same S3 original, from the first stage: drop earlier chunks/vectors and any job still waiting (e.g. budget HELD).
        tx.execute("UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE job_type='INGEST' AND target_id=%s AND status IN ('QUEUED','HELD')",
                   (self.clock(), did))
        tx.execute("DELETE FROM knowledge_chunks WHERE session_id=%s AND document_id=%s", (sid, did))
        tx.execute("""UPDATE documents SET status='PENDING',stage='UPLOADED',error_code=NULL,page_count=NULL,chunk_count=NULL,
            terms=%s WHERE document_id=%s""", (dumps([]), did))
        jid = self.job(tx, 'INGEST', did, job_key, {'fingerprint': fingerprint, 'retry': True}, user=str(p['user_id']), sid=sid)
        self.event(tx, 'document.updated', {'document_id': did, 'status': 'PENDING', 'stage': 'UPLOADED'}, sid)
        return Result(202, {'resource_id': did, 'job_id': jid, 'status': 'QUEUED'}, {'Location': '/api/v1/jobs/' + jid})

    def upload(self, tx, req, p, sid):
        s = self.lecture(tx, sid, p, True)
        if s['status'] not in ('DRAFT', 'LIVE'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if not req.file:
            raise APIError(422, 'VALIDATION_ERROR')
        filename, media, content = req.file
        validate_document(filename, media, content, self.settings)
        key = uuid_value(req.headers.get('idempotency-key'))
        job_key = 'ingest:' + str(p['user_id']) + ':' + sid + ':' + key
        import hashlib
        fingerprint = hashlib.sha256(content + filename.encode() + media.encode()).hexdigest()
        tx.lock(job_key)
        prior = tx.one('SELECT * FROM jobs WHERE idempotency_key=%s', (job_key,))
        if prior:
            if obj(prior['payload'])['fingerprint'] != fingerprint:
                raise APIError(409, 'IDEMPOTENCY_CONFLICT')
            return Result(202, {'resource_id': prior['target_id'], 'job_id': prior['job_id'], 'status': prior['status']}, {'Location': '/api/v1/jobs/' + str(prior['job_id'])})
        did = uid()
        object_key = sid + '/' + did
        req._staged_object = object_key
        self.storage.put(object_key, content, media)
        # Object keys are generated; the user filename never becomes a filesystem path.
        tx.execute("""INSERT INTO documents(document_id,session_id,file_name,object_key,media_type,size_bytes,stage)
            VALUES(%s,%s,%s,%s,%s,%s,'UPLOADED')""", (did, sid, filename, object_key, media, len(content)))
        jid = self.job(tx, 'INGEST', did, job_key, {'fingerprint': fingerprint}, user=str(p['user_id']), sid=sid)
        self.event(tx, 'document.updated', {'document_id': did, 'status': 'PENDING', 'stage': 'UPLOADED'}, sid)
        return Result(202, {'resource_id': did, 'job_id': jid, 'status': 'QUEUED'}, {'Location': '/api/v1/jobs/' + jid})
