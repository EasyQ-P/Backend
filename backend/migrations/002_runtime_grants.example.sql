-- Run only after provisioning easyq_auth/easyq_backend/easyq_worker as LOGIN roles.
-- Passwords are deliberately outside source control. Adapt names before applying.
-- Execute as migration owner, not as a runtime service. No schema DDL rights granted.
GRANT USAGE ON SCHEMA public TO easyq_auth, easyq_backend, easyq_worker;
GRANT SELECT,INSERT,UPDATE,DELETE ON users,auth_sessions,email_verifications,
    password_resets,auth_rate_limits,consents,jobs,ws_events TO easyq_auth;
GRANT SELECT ON lecture_sessions,participants,auth_session_view TO easyq_auth;
GRANT SELECT ON auth_session_view TO easyq_backend;
GRANT SELECT(user_id,display_name) ON users TO easyq_backend;
GRANT SELECT,INSERT,UPDATE ON lecture_sessions,participants,consents,documents,jobs,
    ws_events,auth_rate_limits TO easyq_backend;
-- API-23 drops a failed document's chunks; the audio WebSocket stores PENDING utterance chunks (6.2.5).
GRANT SELECT,INSERT,DELETE ON knowledge_chunks TO easyq_backend;
-- API-14 joined: my_question_count.
GRANT SELECT ON questions TO easyq_backend;
GRANT SELECT,INSERT,UPDATE,DELETE ON documents,knowledge_chunks,eval_reference_chunks,
    jobs,ws_events,auth_rate_limits,feedback_candidates,candidate_provenance,label_reviews TO easyq_worker;
GRANT SELECT,UPDATE ON lecture_sessions,participants,consents,api_usage TO easyq_worker;
-- CLEANUP (TRAINING consent withdrawal) finds a member's candidates through their questions.
GRANT SELECT ON questions TO easyq_worker;
GRANT INSERT ON api_usage,jobs TO easyq_worker;
GRANT SELECT ON app_settings TO easyq_worker;
-- PostgreSQL requires UPDATE privilege on at least one column for SELECT FOR UPDATE.
GRANT UPDATE(updated_at) ON app_settings TO easyq_worker;
GRANT SELECT,UPDATE ON email_verifications,password_resets TO easyq_worker;
GRANT SELECT,DELETE ON users TO easyq_worker;
GRANT USAGE,SELECT ON SEQUENCE ws_events_event_id_seq TO easyq_auth,easyq_backend,easyq_worker;
