-- easyq_schema_v1.3.sql already defines this view; this migration upgrades databases created from v1.2.
-- CREATE OR REPLACE VIEW can only append columns, so the v1.3 additions come last.
BEGIN;
CREATE OR REPLACE VIEW auth_session_view AS
SELECT a.auth_session_id, a.token_hash, a.expires_at, a.revoked_at,
       a.user_id, a.participant_id, u.status AS user_status, u.role AS user_role,
       (u.email_verified_at IS NOT NULL) AS email_verified,
       p.status AS guest_status, p.session_id AS guest_session_id,
       a.csrf_token_hash, a.created_at AS session_created_at
FROM auth_sessions a
LEFT JOIN users u ON u.user_id = a.user_id
LEFT JOIN participants p ON p.participant_id = a.participant_id;
COMMIT;
