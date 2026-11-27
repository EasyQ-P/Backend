-- EasyQ runtime DB roles / design v1.4 working copy, section 8.2.1 (least privilege per execution unit).
-- Run after easyq_schema_v1.4.sql as the migration owner. Create the LOGIN roles first (passwords stay outside source control):
--   CREATE ROLE easyq_auth LOGIN; CREATE ROLE easyq_backend LOGIN; CREATE ROLE easyq_worker LOGIN;
-- Rules:
--   * Only easyq_auth reads password_hash, email_verifications.code_hash and password_resets.token_hash.
--   * easyq_backend reads sessions only through auth_session_view.
--   * easyq_worker reads the mail delivery envelope (not the verifier hashes) and removes unverified or withdrawn accounts.
--   * Foreign key actions (CASCADE / SET NULL) run as the table owner, so no extra grants are needed for them.
BEGIN;
GRANT USAGE ON SCHEMA public TO easyq_auth, easyq_backend, easyq_worker;
GRANT USAGE, SELECT ON SEQUENCE ws_events_event_id_seq TO easyq_auth, easyq_backend, easyq_worker;

-- auth: sign-up, e-mail verification, login, password, CSRF, sessions, rate limits, INT-01~03.
GRANT SELECT, INSERT, UPDATE, DELETE ON users, auth_sessions, email_verifications, password_resets, auth_rate_limits TO easyq_auth;
GRANT SELECT, INSERT ON consents TO easyq_auth;
GRANT SELECT, INSERT ON jobs TO easyq_auth;
GRANT INSERT ON ws_events TO easyq_auth;
GRANT SELECT ON lecture_sessions, participants, auth_session_view TO easyq_auth;

-- backend: lecture, participation, questions, admin REST and WebSocket.
GRANT SELECT ON auth_session_view TO easyq_backend;
GRANT SELECT (user_id, email, display_name, role, status, email_verified_at, suspension_reason, created_at, updated_at) ON users TO easyq_backend;
GRANT UPDATE (status, suspension_reason, updated_at) ON users TO easyq_backend;
GRANT SELECT, INSERT, UPDATE ON auth_rate_limits TO easyq_backend;  -- JOIN_CODE buckets only (code-level rule, keys are hashed)
GRANT SELECT, INSERT, UPDATE ON lecture_sessions, participants, documents, questions, deployments, app_settings, jobs TO easyq_backend;
GRANT SELECT, INSERT ON consents, knowledge_chunks, audit_events, api_usage, training_runs, ws_events TO easyq_backend;
GRANT SELECT ON answers, answer_citations, summary_notes, datasets, model_versions, evaluation_runs TO easyq_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON votes, question_notes, answer_feedback, notices, inquiries TO easyq_backend;
GRANT SELECT, INSERT, UPDATE ON feedback_candidates, label_reviews TO easyq_backend;
GRANT UPDATE (status, retraction_reason, retraction_detail, retracted_at) ON answers TO easyq_backend;
-- H7: API-53 re-checks consent and evaluation-lecture status through the provenance link; it never returns these IDs.
GRANT SELECT, INSERT ON candidate_provenance TO easyq_backend;

-- worker: ingest, transcript index, QA, finalize, summary, mail, sync, purge, cleanup.
GRANT SELECT (user_id, email, display_name, role, status, email_verified_at, created_at) ON users TO easyq_worker;
GRANT UPDATE (status, updated_at) ON users TO easyq_worker;
GRANT DELETE ON users TO easyq_worker;
GRANT SELECT (verification_id, user_id, key_id, delivery_ciphertext, expires_at, sent_at, consumed_at, revoked_at, created_at) ON email_verifications TO easyq_worker;
GRANT SELECT (reset_id, user_id, key_id, delivery_ciphertext, expires_at, consumed_at, revoked_at, created_at) ON password_resets TO easyq_worker;
GRANT UPDATE (delivery_ciphertext, sent_at) ON email_verifications TO easyq_worker;
GRANT UPDATE (delivery_ciphertext) ON password_resets TO easyq_worker;
GRANT DELETE ON email_verifications, password_resets, auth_rate_limits TO easyq_worker;
GRANT SELECT ON auth_rate_limits TO easyq_worker;
GRANT SELECT, UPDATE, DELETE ON lecture_sessions, participants, documents, questions TO easyq_worker;
GRANT SELECT, DELETE ON consents, votes, question_notes, answer_feedback, label_reviews, inquiries TO easyq_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_chunks, eval_reference_chunks, answers, answer_citations, summary_notes,
    feedback_candidates, candidate_provenance, datasets, training_runs, model_versions, evaluation_runs, jobs, ws_events TO easyq_worker;
GRANT SELECT, UPDATE ON deployments TO easyq_worker;
GRANT SELECT, INSERT, UPDATE ON api_usage TO easyq_worker;
GRANT INSERT ON audit_events TO easyq_worker;
GRANT SELECT ON app_settings TO easyq_worker;
GRANT UPDATE (updated_at) ON app_settings TO easyq_worker;  -- SELECT ... FOR UPDATE needs a column UPDATE privilege
COMMIT;
