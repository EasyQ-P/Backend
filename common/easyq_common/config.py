"""Environment configuration. No development credentials are used implicitly."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    origin: str
    hmac_key: str
    encryption_key: str
    internal_key: str
    cookie_secure: bool = True
    storage_root: str = "./var/objects"
    storage_backend: str = "s3"
    s3_bucket: str = ""
    auth_url: str = "http://auth:8000"
    embedding_url: str = ""
    embedding_key: str = ""
    embedding_model: str = ""
    stt_model: str = ""
    stt_api_key: str = ""
    retrieval_version: str = "easyq-v0.3-768-c800-o100"
    # Slide chunk token counter (retrieval chunk_slides, rc-v1): Hugging Face id or local tokenizer.json path.
    chunk_tokenizer: str = "BAAI/bge-reranker-v2-m3"
    # Training server (7.4) through the SSH tunnel; empty disables calls (judge-model keepalive, 6.4.14).
    training_api_base_url: str = ""
    training_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    upload_limit: int = 50 * 1024 * 1024
    expanded_limit: int = 200 * 1024 * 1024
    embedding_max_batch_cost_usd: str = ""
    embedding_price_version: str = ""
    reference_version: str = ""
    reference_retrieval_version: str = ""
    worker_internal_key: str = ""
    service_policy_version: str = "service-v1"
    training_policy_version: str = "training-v1"
    # Claude (LLM_ONLY judgment, answers, summaries). Serving settings from 7.4.21 override model / prompt / τ_LLM;
    # these defaults apply only before the first deployment is synced. Prices are USD per million tokens (8.2.34).
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_prompt_version: str = "llm-only-v1"
    llm_only_threshold: float = 70.0
    llm_input_usd_per_mtok: str = ""
    llm_output_usd_per_mtok: str = ""
    llm_price_version: str = ""

    @classmethod
    def from_env(cls):
        required = ["DATABASE_URL", "PUBLIC_ORIGIN", "CODE_HMAC_KEY", "DELIVERY_ENCRYPTION_KEY", "INTERNAL_SERVICE_KEY"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError("Missing configuration: " + ", ".join(missing))
        if len(os.environ["CODE_HMAC_KEY"]) < 32 or len(os.environ["INTERNAL_SERVICE_KEY"]) < 32:
            raise RuntimeError("Service and HMAC keys must be at least 32 characters")
        from cryptography.fernet import Fernet
        Fernet(os.environ["DELIVERY_ENCRYPTION_KEY"].encode())
        return cls(
            database_url=os.environ["DATABASE_URL"], origin=os.environ["PUBLIC_ORIGIN"].rstrip("/"),
            hmac_key=os.environ["CODE_HMAC_KEY"], encryption_key=os.environ["DELIVERY_ENCRYPTION_KEY"],
            internal_key=os.environ["INTERNAL_SERVICE_KEY"],
            cookie_secure=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
            storage_root=os.environ.get("OBJECT_ROOT", "./var/objects"),
            storage_backend=os.environ.get("STORAGE_BACKEND", "s3"), s3_bucket=os.environ.get("S3_BUCKET", ""),
            auth_url=os.environ.get("AUTH_BASE_URL", "http://auth:8000"),
            embedding_url=os.environ.get("EMBEDDING_URL", ""), embedding_key=os.environ.get("EMBEDDING_API_KEY", ""),
            embedding_model=os.environ.get("EMBEDDING_MODEL", ""),
            stt_model=os.environ.get("STT_MODEL", ""), stt_api_key=os.environ.get("STT_API_KEY", ""),
            smtp_host=os.environ.get("SMTP_HOST", ""), smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", ""), smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("SMTP_FROM", ""), smtp_starttls=os.environ.get("SMTP_STARTTLS", "true").lower() == "true",
            smtp_ssl=os.environ.get("SMTP_SSL_TLS", "false").lower() == "true",
            embedding_max_batch_cost_usd=os.environ.get('EMBEDDING_MAX_BATCH_COST_USD', ''),
            embedding_price_version=os.environ.get('EMBEDDING_PRICE_VERSION', ''),
            reference_version=os.environ.get('EVAL_REFERENCE_VERSION', ''),
            reference_retrieval_version=os.environ.get('EVAL_REFERENCE_RETRIEVAL_VERSION', ''),
            chunk_tokenizer=os.environ.get('CHUNK_TOKENIZER', 'BAAI/bge-reranker-v2-m3'),
            training_api_base_url=os.environ.get('TRAINING_API_BASE_URL', ''),
            training_api_key=os.environ.get('TRAINING_API_KEY', ''),
            worker_internal_key=os.environ.get('WORKER_SERVICE_KEY', ''),
            service_policy_version=os.environ.get('SERVICE_POLICY_VERSION', 'service-v1'),
            training_policy_version=os.environ.get('TRAINING_POLICY_VERSION', 'training-v1'),
            anthropic_api_key=os.environ.get('ANTHROPIC_API_KEY', ''),
            llm_model=os.environ.get('LLM_MODEL', 'claude-sonnet-5-5'),
            llm_prompt_version=os.environ.get('LLM_PROMPT_VERSION', 'llm-only-v1'),
            llm_only_threshold=float(os.environ.get('LLM_ONLY_THRESHOLD', '70')),
            llm_input_usd_per_mtok=os.environ.get('LLM_INPUT_USD_PER_MTOK', ''),
            llm_output_usd_per_mtok=os.environ.get('LLM_OUTPUT_USD_PER_MTOK', ''),
            llm_price_version=os.environ.get('LLM_PRICE_VERSION', ''),
        )
