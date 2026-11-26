"""INGEST (6.2.3): extract, chunk, embed, keyword index, term extraction, evaluation-lecture check."""
import time

from easyq_common.contracts import APIError, dumps, obj, uid

from .budget import reserve, settle
from .retrieval import chunks, check_vectors, extract_pages, keywords, overlap_ratio
from .terms import extract_terms, merge_terms


class IngestJob:
    def ingest(self, job):
        self.stage(job, 'EXTRACTING')
        with self.db.transaction() as tx:
            self.active(tx, job)
            document = tx.one('SELECT * FROM documents WHERE document_id=%s', (job['target_id'],))
        if not document or document['status'] == 'DELETING':
            raise APIError(409, 'RESOURCE_NOT_FOUND')
        pages = extract_pages(document['file_name'], self.service.storage.get(document['object_key']))
        pieces = chunks(pages)
        if not pieces:
            raise APIError(422, 'DOCUMENT_NO_TEXT')
        self.stage(job, 'INDEXING')
        if self.tokenizer is None:
            from kiwipiepy import Kiwi
            self.tokenizer = Kiwi()
        vectors = []
        for start in range(0, len(pieces), 32):
            self.heartbeat(job)
            batch = pieces[start:start + 32]
            usage_id = reserve(self.service, job)
            began = time.monotonic()
            try:
                vector_batch = check_vectors(self.embedder.embed([p['content'] for p in batch]), len(batch))
            except Exception:
                settle(self.service, usage_id, False, int((time.monotonic() - began) * 1000))
                raise
            settle(self.service, usage_id, True, int((time.monotonic() - began) * 1000))
            vectors.extend(vector_batch)
        # 6.2.3 / API-25: term extraction is the last stage (UPLOADED → EXTRACTING → INDEXING → TERMS → READY).
        self.stage(job, 'TERMS')
        document_terms = extract_terms(pages, self.tokenizer)
        with self.db.transaction() as tx:
            self.active(tx, job)
            lecture = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (job['session_id'],))
            current = tx.one('SELECT * FROM documents WHERE document_id=%s FOR UPDATE', (job['target_id'],))
            if not lecture or lecture['status'] not in ('DRAFT', 'LIVE') or not current or current['status'] == 'DELETING':
                raise APIError(409, 'SESSION_STATE_CONFLICT')
            # Upsert stable source keys: lease reclaim or retry cannot duplicate chunks.
            for piece, vector in zip(pieces, vectors):
                tx.execute("""INSERT INTO knowledge_chunks(chunk_id,session_id,document_id,source_type,source_key,page_no,
                    content,embedding,search_vector,retrieval_config_version,index_status,captured_at,indexed_at)
                    VALUES(%s,%s,%s,'DOCUMENT',%s,%s,%s,%s::vector,to_tsvector('simple',%s),%s,'READY',%s,%s)
                    ON CONFLICT(session_id,source_key) DO UPDATE SET content=excluded.content,embedding=excluded.embedding,
                    search_vector=excluded.search_vector,retrieval_config_version=excluded.retrieval_config_version,
                    index_status='READY',indexed_at=excluded.indexed_at""",
                    (uid(), job['session_id'], job['target_id'], f'{job["target_id"]}:{piece["page_no"]}:{piece["ordinal"]}',
                     piece['page_no'], piece['content'], dumps(vector), keywords(piece['content'], self.tokenizer),
                     self.settings.retrieval_version, self.service.clock(), self.service.clock()))
            tx.execute("UPDATE documents SET status='READY',stage=NULL,page_count=%s,chunk_count=%s,error_code=NULL,terms=%s WHERE document_id=%s",
                (len(pages), len(pieces), dumps(document_terms), job['target_id']))
            # Add the document's terms to the lecture dictionary once, up to 100, never re-adding host-removed terms.
            # The host edits only in DRAFT; documents added while LIVE still fill free slots for the next STT connection.
            merged, added = merge_terms(obj(lecture['terms']) or [], obj(lecture['removed_terms']) or [], document_terms)
            if added:
                tx.execute('UPDATE lecture_sessions SET terms=%s,version=version+1 WHERE session_id=%s', (dumps(merged), job['session_id']))
                self.service.event(tx, 'session.updated', {'session_id': str(job['session_id']), 'status': lecture['status'],
                    'version': lecture['version'] + 1}, str(job['session_id']))
            # Until the AI team's evaluation reference sync exists, never claim the evaluation check succeeded.
            references = tx.all('SELECT normalized_text,eval_slide_version FROM eval_reference_chunks WHERE eval_slide_version=%s', (self.settings.reference_version,)) if self.settings.reference_version and self.settings.reference_retrieval_version == self.settings.retrieval_version else []
            if references:
                all_chunks = tx.all("SELECT content FROM knowledge_chunks WHERE session_id=%s AND source_type='DOCUMENT' AND index_status='READY'", (job['session_id'],))
                matched = sum(any(overlap_ratio(p['content'], r['normalized_text']) >= .8 for r in references) for p in all_chunks)
                detected = bool(all_chunks) and matched / len(all_chunks) >= .3
                # 8.2.6 eval_detection keys: check_status PENDING / DONE, eval_slide_version.
                detection = {'check_status': 'DONE', 'matched_chunks': matched, 'total_chunks': len(all_chunks),
                    'match_ratio': matched / len(all_chunks) if all_chunks else 0, 'eval_slide_version': references[0]['eval_slide_version'],
                    'retrieval_config_version': self.settings.retrieval_version, 'checked_at': self.service.clock(), 'auto_detected': detected}
                # Preserve a manual evaluation mark; automated checks cannot clear it.
                before = obj(lecture['eval_detection'])
                detection['manual_marked'] = bool(before.get('manual_marked'))
                tx.execute('UPDATE lecture_sessions SET eval_detection=%s,is_eval_lecture=%s WHERE session_id=%s',
                    (dumps(detection), detected or detection['manual_marked'], job['session_id']))
            else:
                detection = obj(lecture['eval_detection'])
                detection['check_status'] = 'PENDING'
                tx.execute('UPDATE lecture_sessions SET eval_detection=%s WHERE session_id=%s', (dumps(detection), job['session_id']))
            self.service.event(tx, 'document.updated', {'document_id': str(job['target_id']), 'status': 'READY', 'page_count': len(pages), 'chunk_count': len(pieces)}, str(job['session_id']))
            self.finish(tx, job, {'page_count': len(pages), 'chunk_count': len(pieces), 'term_count': len(document_terms), 'terms_added': len(added)})
