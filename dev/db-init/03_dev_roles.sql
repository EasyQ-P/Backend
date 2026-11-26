-- LOCAL DEVELOPMENT ONLY (compose.dev.yaml). Never run against a shared or production database.
-- Runtime roles with fixed development passwords, so the dev stack uses the same least-privilege
-- split as production (auth / backend / worker). Grants follow in 04 (migrations/002).
CREATE ROLE easyq_auth LOGIN PASSWORD 'easyq_auth_dev';
CREATE ROLE easyq_backend LOGIN PASSWORD 'easyq_backend_dev';
CREATE ROLE easyq_worker LOGIN PASSWORD 'easyq_worker_dev';
