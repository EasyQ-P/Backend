-- EasyQ service DB / design v1.4 working copy (v1.2 + review fixes with minimal frontend/AI impact), section 8.2 (PostgreSQL 16 + pgvector).
-- Connect to the empty easyq database as a migration owner, then run this file once.
-- Provision the database and runtime roles separately. UUID values are issued by the application.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    user_id UUID NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(50) NOT NULL,
    role VARCHAR(10) NOT NULL DEFAULT 'MEMBER',
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING_VERIFICATION',
    email_verified_at TIMESTAMPTZ,
    suspension_reason VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id)
);

CREATE TABLE auth_sessions (
    auth_session_id UUID NOT NULL,
    user_id UUID,
    participant_id UUID,
    token_hash CHAR(64) NOT NULL,
    csrf_token_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    renewed_at TIMESTAMPTZ,
    PRIMARY KEY (auth_session_id)
);

CREATE TABLE email_verifications (
    verification_id UUID NOT NULL,
    user_id UUID NOT NULL,
    code_hash CHAR(64) NOT NULL,
    key_id VARCHAR(50) NOT NULL,
    delivery_ciphertext BYTEA,
    expires_at TIMESTAMPTZ NOT NULL,
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    sent_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (verification_id)
);

CREATE TABLE password_resets (
    reset_id UUID NOT NULL,
    user_id UUID NOT NULL,
    token_hash CHAR(64) NOT NULL,
    key_id VARCHAR(50) NOT NULL,
    delivery_ciphertext BYTEA,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (reset_id)
);

CREATE TABLE auth_rate_limits (
    bucket_key CHAR(64) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    last_request_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bucket_key, window_start)
);

CREATE TABLE lecture_sessions (
    session_id UUID NOT NULL,
    host_user_id UUID,
    host_display_name VARCHAR(50) NOT NULL,
    title VARCHAR(100) NOT NULL,
    description VARCHAR(500),
    scheduled_at TIMESTAMPTZ NOT NULL,
    join_code CHAR(5) NOT NULL,
    previous_join_code CHAR(5),
    terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    removed_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(12) NOT NULL DEFAULT 'DRAFT',
    stt_status VARCHAR(16) NOT NULL DEFAULT 'STOPPED',
    end_reason VARCHAR(16),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    raw_data_expires_at TIMESTAMPTZ,
    host_deleted_at TIMESTAMPTZ,
    is_eval_lecture BOOLEAN NOT NULL DEFAULT false,
    eval_detection JSONB NOT NULL DEFAULT '{}'::jsonb,
    course_id VARCHAR(100) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    statistics JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id)
);

CREATE TABLE participants (
    participant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    user_id UUID,
    kind VARCHAR(10) NOT NULL,
    nickname VARCHAR(20),
    status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE',
    archived BOOLEAN NOT NULL DEFAULT true,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (participant_id)
);

CREATE TABLE consents (
    consent_id UUID NOT NULL,
    session_id UUID,
    user_id UUID,
    participant_id UUID,
    purpose VARCHAR(16) NOT NULL,
    policy_version VARCHAR(30) NOT NULL,
    granted BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consent_id)
);

CREATE TABLE documents (
    document_id UUID NOT NULL,
    session_id UUID NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    object_key TEXT NOT NULL,
    media_type VARCHAR(100) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'PENDING',
    stage VARCHAR(12),
    page_count INTEGER,
    chunk_count INTEGER,
    terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_code VARCHAR(40),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id)
);

CREATE TABLE knowledge_chunks (
    chunk_id UUID NOT NULL,
    session_id UUID NOT NULL,
    document_id UUID,
    source_type VARCHAR(12) NOT NULL,
    source_key VARCHAR(100) NOT NULL,
    page_no INTEGER,
    start_ms BIGINT,
    end_ms BIGINT,
    content TEXT NOT NULL,
    embedding VECTOR(768),
    search_vector TSVECTOR NOT NULL,  -- rc-v1 tokenize_keywords tokens (simple); positions give BM25 term frequency (6.1.5)
    retrieval_config_version VARCHAR(50) NOT NULL,
    index_status VARCHAR(10) NOT NULL DEFAULT 'PENDING',
    captured_at TIMESTAMPTZ NOT NULL,
    indexed_at TIMESTAMPTZ,
    PRIMARY KEY (chunk_id)
);

CREATE TABLE eval_reference_chunks (
    eval_slide_version VARCHAR(20) NOT NULL,
    chunk_key VARCHAR(100) NOT NULL,
    course_id VARCHAR(100) NOT NULL,
    page_no INTEGER,
    ngram_hashes BIGINT[] NOT NULL,
    normalized_text TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (eval_slide_version, chunk_key)
);

CREATE TABLE questions (
    question_id UUID NOT NULL,
    session_id UUID NOT NULL,
    participant_id UUID,
    document_id UUID,
    page_no INTEGER,
    content VARCHAR(200),
    status VARCHAR(16) NOT NULL DEFAULT 'QUEUED',
    escalation_reason VARCHAR(30),
    decision_score NUMERIC(6,5),
    vote_count INTEGER NOT NULL DEFAULT 0,
    model_version_id UUID,
    decision_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key UUID NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_token_at TIMESTAMPTZ,
    finalized_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    content_purged_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (question_id)
);

CREATE TABLE answers (
    answer_id UUID NOT NULL,
    question_id UUID NOT NULL,
    content TEXT,
    provider_model VARCHAR(100) NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'ACTIVE',
    retraction_reason VARCHAR(32),
    retraction_detail VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at TIMESTAMPTZ,
    session_id UUID NOT NULL,
    PRIMARY KEY (answer_id)
);

CREATE TABLE answer_citations (
    answer_id UUID NOT NULL,
    citation_no SMALLINT NOT NULL,
    chunk_id UUID,
    source_type VARCHAR(12) NOT NULL,
    location_label VARCHAR(50) NOT NULL,
    page_no INTEGER,
    start_ms BIGINT,
    end_ms BIGINT,
    excerpt TEXT NOT NULL,
    session_id UUID NOT NULL,
    PRIMARY KEY (answer_id, citation_no)
);

CREATE TABLE votes (
    question_id UUID NOT NULL,
    participant_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id UUID NOT NULL,
    PRIMARY KEY (question_id, participant_id)
);

CREATE TABLE question_notes (
    question_id UUID NOT NULL,
    session_id UUID NOT NULL,
    user_id UUID NOT NULL,
    question_text VARCHAR(200) NOT NULL,
    content VARCHAR(1000) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (question_id, user_id)
);

CREATE TABLE answer_feedback (
    answer_id UUID NOT NULL,
    participant_id UUID NOT NULL,
    rating VARCHAR(16) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id UUID NOT NULL,
    PRIMARY KEY (answer_id, participant_id)
);

CREATE TABLE summary_notes (
    summary_id UUID NOT NULL,
    session_id UUID NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'QUEUED',
    content JSONB,
    source_refs JSONB NOT NULL,
    provider_model VARCHAR(100),
    generated_at TIMESTAMPTZ,
    PRIMARY KEY (summary_id)
);

CREATE TABLE feedback_candidates (
    candidate_id UUID NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    default_label VARCHAR(24),
    reason_code VARCHAR(32),
    sanitized_pair JSONB NOT NULL,
    retrieved_evidence JSONB NOT NULL,
    judgment_snapshot JSONB NOT NULL,
    review_status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    final_label VARCHAR(24),
    merge_request_id UUID,
    review_started_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_id)
);

CREATE TABLE label_reviews (
    candidate_id UUID NOT NULL,
    reviewer_user_id UUID NOT NULL,
    label VARCHAR(24) NOT NULL,
    evidence_id UUID,
    comment VARCHAR(1000),
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_id, reviewer_user_id)
);

CREATE TABLE candidate_provenance (
    candidate_id UUID NOT NULL,
    question_id UUID NOT NULL,
    answer_id UUID,
    actor_user_id UUID,
    actor_participant_id UUID,
    source_revision INTEGER NOT NULL,
    consent_snapshot JSONB NOT NULL,
    event_key CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id UUID NOT NULL,
    PRIMARY KEY (candidate_id)
);

CREATE TABLE datasets (
    dataset_id UUID NOT NULL,
    version_name VARCHAR(20) NOT NULL,
    parent_version VARCHAR(20),
    status VARCHAR(20) NOT NULL,
    contains_aihub BOOLEAN NOT NULL,
    statistics JSONB NOT NULL,
    script_version VARCHAR(100) NOT NULL,
    retrieval_config_version VARCHAR(50) NOT NULL,
    validation_set_version VARCHAR(20) NOT NULL,
    test_set_version VARCHAR(20) NOT NULL,
    eval_slide_version VARCHAR(20) NOT NULL,
    test_use_count INTEGER DEFAULT 0,
    change_note TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dataset_id)
);

CREATE TABLE training_runs (
    training_run_id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    requested_by UUID,
    job_id VARCHAR(30),
    status VARCHAR(16) NOT NULL DEFAULT 'QUEUED',
    stage VARCHAR(16),
    hold_reason VARCHAR(40),
    progress NUMERIC(4,3),
    config JSONB NOT NULL,
    learning_curve JSONB NOT NULL,
    metrics JSONB NOT NULL,
    model_version VARCHAR(20),
    deployable BOOLEAN,
    error_code VARCHAR(40),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (training_run_id)
);

CREATE TABLE model_versions (
    model_version_id UUID NOT NULL,
    training_run_id UUID,
    version_name VARCHAR(20) NOT NULL,
    artifact_uri TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL,
    threshold NUMERIC(8,5) NOT NULL,
    status VARCHAR(16) NOT NULL,
    deployable BOOLEAN NOT NULL,
    compared_with VARCHAR(20),
    serving BOOLEAN NOT NULL DEFAULT false,
    served_before BOOLEAN NOT NULL DEFAULT false,
    metrics JSONB NOT NULL,
    evaluation_contract JSONB NOT NULL,
    memo VARCHAR(500),
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (model_version_id)
);

CREATE TABLE evaluation_runs (
    evaluation_id UUID NOT NULL,
    external_evaluation_id VARCHAR(30) NOT NULL,
    model_version_id UUID,
    dataset_id UUID NOT NULL,
    evaluator VARCHAR(16) NOT NULL,
    scope VARCHAR(10) NOT NULL,
    split VARCHAR(10) NOT NULL,
    config JSONB NOT NULL,
    metrics JSONB NOT NULL,
    test_use_count INTEGER,
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (evaluation_id)
);

CREATE TABLE deployments (
    deployment_id UUID NOT NULL,
    model_version_id UUID,
    mode VARCHAR(12) NOT NULL,
    operation VARCHAR(12) NOT NULL,
    requested_by UUID NOT NULL,
    previous_deployment_id UUID,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    reason VARCHAR(500) NOT NULL,
    serving_snapshot JSONB NOT NULL,
    error_code VARCHAR(40),
    synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (deployment_id)
);

CREATE TABLE jobs (
    job_id UUID NOT NULL,
    session_id UUID,
    requested_by UUID,
    participant_id UUID,
    job_type VARCHAR(24) NOT NULL,
    target_id UUID NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'QUEUED',
    idempotency_key VARCHAR(200) NOT NULL,
    client_request_key CHAR(64),
    request_hash CHAR(64),
    payload JSONB NOT NULL,
    attempts SMALLINT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL,
    lease_until TIMESTAMPTZ,
    lease_token UUID,
    error_code VARCHAR(40),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (job_id)
);

CREATE TABLE ws_events (
    event_id BIGINT GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    session_id UUID,
    question_id UUID,
    event_type VARCHAR(40) NOT NULL,
    generation_id UUID,
    sequence INTEGER,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id)
);

CREATE TABLE audit_events (
    audit_event_id UUID NOT NULL,
    actor_user_id UUID,
    action VARCHAR(50) NOT NULL,
    target_type VARCHAR(30) NOT NULL,
    target_ref VARCHAR(100) NOT NULL,
    changes JSONB NOT NULL,
    reason VARCHAR(500),
    request_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (audit_event_id)
);

CREATE TABLE api_usage (
    usage_id UUID NOT NULL,
    session_id UUID,
    question_id UUID,
    provider VARCHAR(30) NOT NULL,
    model_name VARCHAR(100) NOT NULL,
    operation VARCHAR(20) NOT NULL,
    input_tokens BIGINT,
    output_tokens BIGINT,
    audio_ms BIGINT,
    estimated_cost NUMERIC(18,8),
    currency CHAR(3) NOT NULL DEFAULT 'USD',
    price_version VARCHAR(50),
    latency_ms INTEGER NOT NULL,
    outcome VARCHAR(20) NOT NULL,
    request_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (usage_id)
);

CREATE TABLE notices (
    notice_id UUID NOT NULL,
    author_user_id UUID,
    title VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (notice_id)
);

CREATE TABLE inquiries (
    inquiry_id UUID NOT NULL,
    user_id UUID NOT NULL,
    title VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'WAITING',
    reply TEXT,
    replied_by UUID,
    replied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (inquiry_id)
);

CREATE TABLE app_settings (
    setting_key VARCHAR(40) NOT NULL,
    value JSONB NOT NULL,
    updated_by UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    hard_cap_enabled BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (setting_key)
);

-- Unique keys and query indexes from section 8.2.35.
-- Created before foreign keys: composite FKs need these UNIQUE keys to exist first.
CREATE UNIQUE INDEX uq_users_email ON users (email);
CREATE UNIQUE INDEX uq_auth_sessions_token_hash ON auth_sessions (token_hash);
CREATE UNIQUE INDEX uq_auth_sessions_participant_id_active ON auth_sessions (participant_id) WHERE participant_id IS NOT NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX uq_email_verifications_user_id ON email_verifications (user_id) WHERE consumed_at IS NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX uq_password_resets_user_id ON password_resets (user_id) WHERE consumed_at IS NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX uq_lecture_sessions_join_code ON lecture_sessions (join_code);
CREATE UNIQUE INDEX uq_lecture_sessions_previous_join_code ON lecture_sessions (previous_join_code) WHERE previous_join_code IS NOT NULL;
CREATE UNIQUE INDEX uq_participants_session_id_participant_id ON participants (session_id, participant_id);
CREATE UNIQUE INDEX uq_participants_session_id_user_id ON participants (session_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX uq_documents_session_id_document_id ON documents (session_id, document_id);
CREATE UNIQUE INDEX uq_documents_object_key ON documents (object_key);
CREATE UNIQUE INDEX uq_knowledge_chunks_session_id_chunk_id ON knowledge_chunks (session_id, chunk_id);
CREATE UNIQUE INDEX uq_knowledge_chunks_session_id_source_key ON knowledge_chunks (session_id, source_key);
CREATE UNIQUE INDEX uq_questions_session_id_question_id ON questions (session_id, question_id);
CREATE UNIQUE INDEX uq_questions_participant_id_idempotency_key ON questions (participant_id, idempotency_key);
CREATE UNIQUE INDEX uq_summary_notes_session_id ON summary_notes (session_id);
CREATE UNIQUE INDEX uq_answers_question_id ON answers (question_id);
CREATE UNIQUE INDEX uq_answers_session_id_answer_id ON answers (session_id, answer_id);
-- H4: one merge request (7.4.5) carries many candidates, so this is a lookup index, not UNIQUE.
CREATE INDEX ix_feedback_candidates_merge_request_id ON feedback_candidates (merge_request_id) WHERE merge_request_id IS NOT NULL;
CREATE UNIQUE INDEX uq_candidate_provenance_event_key ON candidate_provenance (event_key);
CREATE UNIQUE INDEX uq_datasets_version_name ON datasets (version_name);
CREATE UNIQUE INDEX uq_model_versions_version_name ON model_versions (version_name);
CREATE UNIQUE INDEX uq_model_versions_serving ON model_versions (serving) WHERE serving;
CREATE UNIQUE INDEX uq_ws_events_generation_id_sequence_event_type ON ws_events (generation_id, sequence, event_type) WHERE generation_id IS NOT NULL;
CREATE UNIQUE INDEX uq_deployments_status ON deployments (status) WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX uq_jobs_idempotency_key ON jobs (idempotency_key);
-- M2: client Idempotency-Key = HMAC(subject, job_type, key); same key with a different request_hash is 409 IDEMPOTENCY_CONFLICT.
CREATE UNIQUE INDEX uq_jobs_client_request_key ON jobs (client_request_key) WHERE client_request_key IS NOT NULL;
CREATE UNIQUE INDEX uq_evaluation_runs_external_evaluation_id_evaluator_scope_split ON evaluation_runs (external_evaluation_id, evaluator, scope, split);
CREATE INDEX ix_lecture_sessions_host_user_id_status_scheduled_at ON lecture_sessions (host_user_id, status, scheduled_at);
CREATE INDEX ix_lecture_sessions_raw_data_expires_at ON lecture_sessions (raw_data_expires_at);
CREATE INDEX ix_lecture_sessions_status_started_at ON lecture_sessions (status, started_at);
CREATE INDEX ix_participants_session_id_status ON participants (session_id, status);
CREATE INDEX ix_participants_user_id_archived ON participants (user_id, archived);
CREATE INDEX ix_knowledge_chunks_session_id_source_type_index_status ON knowledge_chunks (session_id, source_type, index_status);
CREATE INDEX ix_questions_session_id_status_vote_count_created_at ON questions (session_id, status, vote_count DESC, created_at);
CREATE INDEX ix_questions_participant_id_created_at ON questions (participant_id, created_at);
CREATE INDEX ix_question_notes_user_id ON question_notes (user_id);
CREATE INDEX ix_question_notes_session_id_user_id ON question_notes (session_id, user_id);
CREATE INDEX ix_jobs_status_available_at ON jobs (status, available_at);
CREATE INDEX ix_api_usage_created_at_operation ON api_usage (created_at, operation);
CREATE INDEX ix_api_usage_session_id_created_at ON api_usage (session_id, created_at);
CREATE INDEX ix_notices_deleted_at_created_at ON notices (deleted_at, created_at DESC);
CREATE INDEX ix_inquiries_user_id_created_at ON inquiries (user_id, created_at DESC);
CREATE INDEX ix_inquiries_status_created_at ON inquiries (status, created_at);
CREATE INDEX ix_knowledge_chunks_search_vector ON knowledge_chunks USING gin (search_vector);
CREATE INDEX ix_knowledge_chunks_embedding ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;
CREATE INDEX ix_eval_reference_chunks_ngram_hashes ON eval_reference_chunks USING gin (ngram_hashes);
CREATE INDEX ix_ws_events_session_event ON ws_events (session_id, event_id);
CREATE INDEX ix_ws_events_expires ON ws_events (expires_at);
CREATE INDEX ix_auth_rate_limits_expires ON auth_rate_limits (expires_at);
CREATE INDEX ix_candidate_provenance_question ON candidate_provenance (question_id);
CREATE INDEX ix_label_reviews_evidence ON label_reviews (evidence_id);
-- L9: FK cascade / SET NULL paths and frequent lookups (latest consent, session revocation, job by target).
CREATE INDEX ix_ws_events_session_id_question_id ON ws_events (session_id, question_id) WHERE question_id IS NOT NULL;
CREATE INDEX ix_answer_citations_session_id_chunk_id ON answer_citations (session_id, chunk_id) WHERE chunk_id IS NOT NULL;
CREATE INDEX ix_votes_session_id_participant_id ON votes (session_id, participant_id);
CREATE INDEX ix_answer_feedback_session_id_participant_id ON answer_feedback (session_id, participant_id);
CREATE INDEX ix_questions_session_id_document_id ON questions (session_id, document_id) WHERE document_id IS NOT NULL;
CREATE INDEX ix_knowledge_chunks_session_id_document_id ON knowledge_chunks (session_id, document_id) WHERE document_id IS NOT NULL;
CREATE INDEX ix_candidate_provenance_session_id_answer_id ON candidate_provenance (session_id, answer_id) WHERE answer_id IS NOT NULL;
CREATE INDEX ix_consents_user_id_purpose_recorded_at ON consents (user_id, purpose, recorded_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX ix_consents_session_id_participant_id_purpose_recorded_at ON consents (session_id, participant_id, purpose, recorded_at DESC) WHERE participant_id IS NOT NULL;
CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX ix_auth_sessions_participant_id ON auth_sessions (participant_id) WHERE participant_id IS NOT NULL;
CREATE INDEX ix_jobs_job_type_target_id ON jobs (job_type, target_id);

-- Referential integrity. Composite references keep lecture data in its own session.
ALTER TABLE auth_sessions ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE auth_sessions ADD FOREIGN KEY (participant_id) REFERENCES participants (participant_id) ON DELETE CASCADE;
ALTER TABLE email_verifications ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE password_resets ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE lecture_sessions ADD FOREIGN KEY (host_user_id) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE participants ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE participants ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE consents ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE consents ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
-- Guest consent rows always carry session_id, so the composite FK keeps them in the same lecture.
ALTER TABLE consents ADD FOREIGN KEY (session_id, participant_id) REFERENCES participants (session_id, participant_id) ON DELETE CASCADE;
ALTER TABLE documents ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE knowledge_chunks ADD FOREIGN KEY (session_id, document_id) REFERENCES documents (session_id, document_id);
ALTER TABLE knowledge_chunks ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE questions ADD FOREIGN KEY (session_id, participant_id) REFERENCES participants (session_id, participant_id) ON DELETE SET NULL (participant_id);
ALTER TABLE questions ADD FOREIGN KEY (session_id, document_id) REFERENCES documents (session_id, document_id) ON DELETE SET NULL (document_id);
ALTER TABLE questions ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE questions ADD FOREIGN KEY (model_version_id) REFERENCES model_versions (model_version_id) ON DELETE SET NULL;
ALTER TABLE answers ADD FOREIGN KEY (session_id, question_id) REFERENCES questions (session_id, question_id) ON DELETE CASCADE;
ALTER TABLE answer_citations ADD FOREIGN KEY (session_id, answer_id) REFERENCES answers (session_id, answer_id) ON DELETE CASCADE;
ALTER TABLE answer_citations ADD FOREIGN KEY (session_id, chunk_id) REFERENCES knowledge_chunks (session_id, chunk_id) ON DELETE SET NULL (chunk_id);
ALTER TABLE votes ADD FOREIGN KEY (session_id, question_id) REFERENCES questions (session_id, question_id) ON DELETE CASCADE;
ALTER TABLE votes ADD FOREIGN KEY (session_id, participant_id) REFERENCES participants (session_id, participant_id) ON DELETE CASCADE;
-- Notes are lecture rows too (8.2.1): the composite FK keeps them in the question's lecture.
ALTER TABLE question_notes ADD FOREIGN KEY (session_id, question_id) REFERENCES questions (session_id, question_id) ON DELETE CASCADE;
ALTER TABLE question_notes ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE answer_feedback ADD FOREIGN KEY (session_id, answer_id) REFERENCES answers (session_id, answer_id) ON DELETE CASCADE;
ALTER TABLE answer_feedback ADD FOREIGN KEY (session_id, participant_id) REFERENCES participants (session_id, participant_id) ON DELETE CASCADE;
ALTER TABLE summary_notes ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE label_reviews ADD FOREIGN KEY (candidate_id) REFERENCES feedback_candidates (candidate_id) ON DELETE CASCADE;
ALTER TABLE label_reviews ADD FOREIGN KEY (reviewer_user_id) REFERENCES users (user_id);
-- label_reviews.evidence_id has no FK (8.2.21): it is validated against the candidate's retrieved_evidence and must survive raw-data purge.
ALTER TABLE candidate_provenance ADD FOREIGN KEY (candidate_id) REFERENCES feedback_candidates (candidate_id) ON DELETE CASCADE;
-- H5: deleting a question must not silently drop the only link to an unmerged candidate.
-- Delete jobs remove candidates (8.2.37 order) before questions; MERGING candidates wait for merge confirmation.
ALTER TABLE candidate_provenance ADD FOREIGN KEY (session_id, question_id) REFERENCES questions (session_id, question_id) ON DELETE RESTRICT;
ALTER TABLE candidate_provenance ADD FOREIGN KEY (session_id, answer_id) REFERENCES answers (session_id, answer_id);
ALTER TABLE candidate_provenance ADD FOREIGN KEY (actor_user_id) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE candidate_provenance ADD FOREIGN KEY (session_id, actor_participant_id) REFERENCES participants (session_id, participant_id) ON DELETE SET NULL (actor_participant_id);
ALTER TABLE training_runs ADD FOREIGN KEY (dataset_id) REFERENCES datasets (dataset_id);
ALTER TABLE training_runs ADD FOREIGN KEY (requested_by) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE model_versions ADD FOREIGN KEY (training_run_id) REFERENCES training_runs (training_run_id) ON DELETE SET NULL;
ALTER TABLE evaluation_runs ADD FOREIGN KEY (model_version_id) REFERENCES model_versions (model_version_id) ON DELETE SET NULL;
ALTER TABLE evaluation_runs ADD FOREIGN KEY (dataset_id) REFERENCES datasets (dataset_id);
ALTER TABLE deployments ADD FOREIGN KEY (model_version_id) REFERENCES model_versions (model_version_id);
ALTER TABLE deployments ADD FOREIGN KEY (requested_by) REFERENCES users (user_id);
ALTER TABLE deployments ADD FOREIGN KEY (previous_deployment_id) REFERENCES deployments (deployment_id);
ALTER TABLE jobs ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE SET NULL;
ALTER TABLE jobs ADD FOREIGN KEY (requested_by) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE jobs ADD FOREIGN KEY (participant_id) REFERENCES participants (participant_id) ON DELETE SET NULL;
ALTER TABLE ws_events ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE CASCADE;
ALTER TABLE ws_events ADD FOREIGN KEY (session_id, question_id) REFERENCES questions (session_id, question_id) ON DELETE CASCADE;
ALTER TABLE audit_events ADD FOREIGN KEY (actor_user_id) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE api_usage ADD FOREIGN KEY (session_id) REFERENCES lecture_sessions (session_id) ON DELETE SET NULL;
ALTER TABLE api_usage ADD FOREIGN KEY (question_id) REFERENCES questions (question_id) ON DELETE SET NULL;
ALTER TABLE notices ADD FOREIGN KEY (author_user_id) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE inquiries ADD FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE inquiries ADD FOREIGN KEY (replied_by) REFERENCES users (user_id) ON DELETE SET NULL;
ALTER TABLE app_settings ADD FOREIGN KEY (updated_by) REFERENCES users (user_id) ON DELETE SET NULL;

-- State and shape constraints.
ALTER TABLE users ADD CONSTRAINT ck_users_role CHECK (role IN ('MEMBER', 'ADMIN'));
ALTER TABLE users ADD CONSTRAINT ck_users_status CHECK (status IN ('PENDING_VERIFICATION', 'ACTIVE', 'SUSPENDED', 'DELETING'));
ALTER TABLE lecture_sessions ADD CONSTRAINT ck_lecture_sessions_status CHECK (status IN ('DRAFT', 'LIVE', 'ENDED', 'PURGED', 'DELETING'));
ALTER TABLE lecture_sessions ADD CONSTRAINT ck_lecture_sessions_stt_status CHECK (stt_status IN ('STOPPED', 'CONNECTING', 'RUNNING', 'PAUSED', 'DEGRADED'));
ALTER TABLE lecture_sessions ADD CONSTRAINT ck_lecture_sessions_end_reason CHECK (end_reason IN ('HOST', 'AUTO_TIMEOUT', 'ADMIN'));
ALTER TABLE participants ADD CONSTRAINT ck_participants_kind CHECK (kind IN ('MEMBER', 'GUEST'));
ALTER TABLE participants ADD CONSTRAINT ck_participants_status CHECK (status IN ('ACTIVE', 'REVOKED'));
ALTER TABLE consents ADD CONSTRAINT ck_consents_purpose CHECK (purpose IN ('SERVICE', 'TRAINING', 'STT'));
ALTER TABLE documents ADD CONSTRAINT ck_documents_status CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED', 'DELETING'));
ALTER TABLE documents ADD CONSTRAINT ck_documents_stage CHECK (stage IN ('UPLOADED', 'EXTRACTING', 'INDEXING', 'TERMS'));
ALTER TABLE knowledge_chunks ADD CONSTRAINT ck_knowledge_chunks_source_type CHECK (source_type IN ('DOCUMENT', 'TRANSCRIPT'));
ALTER TABLE knowledge_chunks ADD CONSTRAINT ck_knowledge_chunks_index_status CHECK (index_status IN ('PENDING', 'READY', 'FAILED'));
ALTER TABLE questions ADD CONSTRAINT ck_questions_status CHECK (status IN ('QUEUED', 'PROCESSING', 'AI_ANSWERED', 'ESCALATED', 'COMPLETED'));
ALTER TABLE questions ADD CONSTRAINT ck_questions_escalation_reason CHECK (escalation_reason IN ('INSUFFICIENT_EVIDENCE', 'LOW_CONFIDENCE', 'SECOND_STAGE_INSUFFICIENT', 'PROVIDER_FAILURE', 'BUDGET_EXCEEDED', 'RETRACTED', 'FINALIZE_TIMEOUT'));
ALTER TABLE answers ADD CONSTRAINT ck_answers_status CHECK (status IN ('ACTIVE', 'RETRACTED'));
ALTER TABLE answers ADD CONSTRAINT ck_answers_retraction_reason CHECK (retraction_reason IN ('WRONG_OR_INSUFFICIENT', 'EXPLAIN_AGAIN'));
ALTER TABLE answer_citations ADD CONSTRAINT ck_answer_citations_source_type CHECK (source_type IN ('DOCUMENT', 'TRANSCRIPT'));
ALTER TABLE answer_feedback ADD CONSTRAINT ck_answer_feedback_rating CHECK (rating IN ('HELPFUL', 'UNHELPFUL'));
ALTER TABLE summary_notes ADD CONSTRAINT ck_summary_notes_status CHECK (status IN ('QUEUED', 'RUNNING', 'READY', 'FAILED', 'SKIPPED'));
ALTER TABLE feedback_candidates ADD CONSTRAINT ck_feedback_candidates_event_type CHECK (event_type IN ('AUTO_ESCALATED', 'RETRACTED', 'AUDIENCE_HELPFUL', 'AUDIENCE_UNHELPFUL', 'SECOND_STAGE_INSUFFICIENT'));
ALTER TABLE feedback_candidates ADD CONSTRAINT ck_feedback_candidates_review_status CHECK (review_status IN ('PENDING', 'DISAGREEMENT', 'APPROVED', 'REJECTED', 'MERGING'));
ALTER TABLE feedback_candidates ADD CONSTRAINT ck_feedback_candidates_default_label CHECK (default_label IN ('ANSWERABLE', 'INSUFFICIENT_EVIDENCE', 'OPINION'));
ALTER TABLE feedback_candidates ADD CONSTRAINT ck_feedback_candidates_final_label CHECK (final_label IN ('ANSWERABLE', 'INSUFFICIENT_EVIDENCE', 'OPINION'));
ALTER TABLE feedback_candidates ADD CONSTRAINT ck_feedback_candidates_reason_code CHECK (reason_code IN ('WRONG_OR_INSUFFICIENT', 'EXPLAIN_AGAIN'));
ALTER TABLE label_reviews ADD CONSTRAINT ck_label_reviews_label CHECK (label IN ('ANSWERABLE', 'INSUFFICIENT_EVIDENCE', 'OPINION', 'REJECT'));
ALTER TABLE training_runs ADD CONSTRAINT ck_training_runs_status CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'HELD', 'CANCELLED'));
ALTER TABLE training_runs ADD CONSTRAINT ck_training_runs_stage CHECK (stage IN ('train', 'export', 'eval', 'register'));
ALTER TABLE training_runs ADD CONSTRAINT ck_training_runs_hold_reason CHECK (hold_reason IN ('precision_drop', 'below_target'));
ALTER TABLE model_versions ADD CONSTRAINT ck_model_versions_status CHECK (status IN ('REGISTERED', 'DELETED'));
ALTER TABLE evaluation_runs ADD CONSTRAINT ck_evaluation_runs_scope CHECK (scope IN ('QUESTION', 'PAIR', 'SERVICE'));
ALTER TABLE evaluation_runs ADD CONSTRAINT ck_evaluation_runs_split CHECK (split IN ('VALIDATION', 'TEST'));
ALTER TABLE deployments ADD CONSTRAINT ck_deployments_mode CHECK (mode IN ('CUSTOM', 'LLM_ONLY'));
ALTER TABLE deployments ADD CONSTRAINT ck_deployments_operation CHECK (operation IN ('SWITCH', 'ROLLBACK'));
ALTER TABLE deployments ADD CONSTRAINT ck_deployments_status CHECK (status IN ('PENDING', 'ACTIVE', 'SUPERSEDED', 'FAILED'));
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_status CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'HELD'));
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_job_type CHECK (job_type IN ('INGEST', 'TRANSCRIPT_INDEX', 'QA', 'FINALIZE', 'SUMMARY', 'AUTO_END', 'PURGE', 'CLEANUP', 'DATASET', 'TRAIN', 'DEPLOY', 'SYNC', 'EMAIL_VERIFICATION', 'PASSWORD_RESET', 'AUTH_REVOKE'));
ALTER TABLE api_usage ADD CONSTRAINT ck_api_usage_operation CHECK (operation IN ('STT', 'EMBEDDING', 'JUDGE_LLM', 'ANSWER', 'SUMMARY', 'EMAIL'));
ALTER TABLE api_usage ADD CONSTRAINT ck_api_usage_outcome CHECK (outcome IN ('RESERVED', 'SUCCESS', 'TIMEOUT', 'ERROR', 'SKIPPED_BUDGET'));
ALTER TABLE inquiries ADD CONSTRAINT ck_inquiries_status CHECK (status IN ('WAITING', 'ANSWERED'));
ALTER TABLE users ADD CHECK (email = lower(email) AND length(email) > 0);
ALTER TABLE users ADD CHECK ((status = 'PENDING_VERIFICATION') = (email_verified_at IS NULL) OR status IN ('SUSPENDED','DELETING'));
ALTER TABLE users ADD CHECK ((status = 'SUSPENDED') = (suspension_reason IS NOT NULL));
ALTER TABLE auth_sessions ADD CHECK ((user_id IS NULL) <> (participant_id IS NULL));
ALTER TABLE email_verifications ADD CHECK (attempt_count BETWEEN 0 AND 5);
ALTER TABLE auth_rate_limits ADD CHECK (request_count >= 0);
ALTER TABLE participants ADD CHECK ((kind = 'MEMBER') = (user_id IS NOT NULL));
ALTER TABLE consents ADD CHECK ((user_id IS NULL) <> (participant_id IS NULL));
ALTER TABLE consents ADD CHECK (session_id IS NOT NULL OR (user_id IS NOT NULL AND purpose <> 'STT'));
ALTER TABLE consents ADD CHECK (purpose <> 'STT' OR (user_id IS NOT NULL AND session_id IS NOT NULL));
ALTER TABLE consents ADD CHECK (participant_id IS NULL OR session_id IS NOT NULL);
ALTER TABLE feedback_candidates ADD CHECK (event_type <> 'RETRACTED' OR reason_code IS NOT NULL);
ALTER TABLE lecture_sessions ADD CHECK (version >= 1);
ALTER TABLE lecture_sessions ADD CHECK (join_code ~ '^[A-HJ-NP-Z2-9]{5}$');
ALTER TABLE documents ADD CHECK (size_bytes >= 0 AND attempt_count >= 0 AND (page_count IS NULL OR page_count >= 0) AND (chunk_count IS NULL OR chunk_count >= 0));
ALTER TABLE knowledge_chunks ADD CHECK ((source_type = 'DOCUMENT' AND document_id IS NOT NULL AND page_no > 0 AND start_ms IS NULL AND end_ms IS NULL) OR (source_type = 'TRANSCRIPT' AND document_id IS NULL AND page_no IS NULL AND start_ms >= 0 AND end_ms > start_ms));
ALTER TABLE knowledge_chunks ADD CHECK (index_status <> 'READY' OR (embedding IS NOT NULL AND indexed_at IS NOT NULL));
-- H3: document deletion sets document_id NULL while page_no stays for statistics; APIs hide a page without its document.
ALTER TABLE questions ADD CHECK (vote_count >= 0 AND version >= 1 AND (decision_score IS NULL OR decision_score BETWEEN 0 AND 1) AND (page_no IS NULL OR page_no > 0));
ALTER TABLE answers ADD CHECK ((status = 'RETRACTED') = (retracted_at IS NOT NULL));
ALTER TABLE answer_citations ADD CHECK (citation_no >= 1);
ALTER TABLE answer_feedback ADD CHECK (version >= 1);
ALTER TABLE training_runs ADD CHECK (progress IS NULL OR progress BETWEEN 0 AND 1);
ALTER TABLE model_versions ADD CHECK (threshold BETWEEN 0 AND 1);
ALTER TABLE deployments ADD CHECK ((mode = 'CUSTOM' AND model_version_id IS NOT NULL) OR (mode = 'LLM_ONLY' AND model_version_id IS NULL));
ALTER TABLE jobs ADD CHECK (attempts >= 0 AND (client_request_key IS NULL) = (request_hash IS NULL));
ALTER TABLE ws_events ADD CHECK (sequence IS NULL OR sequence >= 0);
ALTER TABLE ws_events ADD CHECK (question_id IS NULL OR session_id IS NOT NULL);
ALTER TABLE ws_events ADD CHECK (session_id IS NOT NULL OR event_type IN ('session.revoked', 'auth.csrf_rotated'));
ALTER TABLE api_usage ADD CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (audio_ms IS NULL OR audio_ms >= 0) AND (estimated_cost IS NULL OR estimated_cost >= 0) AND latency_ms >= 0);
ALTER TABLE notices ADD CHECK (version >= 1 AND char_length(content) <= 2000);
ALTER TABLE inquiries ADD CHECK (char_length(content) <= 2000 AND (reply IS NULL OR char_length(reply) <= 2000));

-- Auth can read base tables; backend should only read this limited session projection.
CREATE VIEW auth_session_view AS SELECT a.auth_session_id, a.token_hash, a.expires_at, a.revoked_at, a.user_id, a.participant_id, u.status AS user_status, u.role AS user_role, (u.email_verified_at IS NOT NULL) AS email_verified, p.status AS guest_status, p.session_id AS guest_session_id, a.csrf_token_hash, a.created_at AS session_created_at FROM auth_sessions a LEFT JOIN users u ON u.user_id = a.user_id LEFT JOIN participants p ON p.participant_id = a.participant_id;
COMMIT;
