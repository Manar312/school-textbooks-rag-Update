from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    timezone: str = "Africa/Cairo"

    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/schoolrag"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: str = "./storage"
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 60 * 24
    cors_origins: str = "*"

    # ---- provider keys (a role can override with its own *_API_KEY) ----
    groq_api_key: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # ---- every role picks its own provider + model: swap any of them from .env ----
    # Defaults are FREE: Groq free tier for the LLM/VLM, local BGE models for embeddings/reranking, Tesseract OCR.
    # LLM: answers      (groq | gemini | anthropic | openai -- "openai" also fits any OpenAI-compatible server via *_BASE_URL)
    llm_provider: str = "groq"
    llm_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"   # official Arabic/French/English support
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    # Condense + subject router (one cheap call per question). Unset provider => reuse the LLM provider/model.
    condense_provider: str | None = "groq"
    condense_model: str = "llama-3.1-8b-instant"
    condense_api_key: str | None = None
    condense_base_url: str | None = None
    # VLM: reads page images (groq | gemini | anthropic | openai)
    vlm_provider: str = "groq"
    vlm_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    vlm_api_key: str | None = None
    vlm_base_url: str | None = None
    # Embeddings (bge_m3 = local dense+sparse | openai = any OpenAI-compatible embeddings API, dense only)
    embed_provider: str = "bge_m3"
    embed_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    embed_api_key: str | None = None
    embed_base_url: str | None = None
    # Reranker (bge | none)
    rerank_provider: str = "bge"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_min_score: float = 0.05
    # Vector store (qdrant)
    vectorstore_provider: str = "qdrant"
    qdrant_url: str = "http://localhost:6333"
    collection_prefix: str = "textbooks"
    # OCR fallback (tesseract | none)
    ocr_provider: str = "tesseract"
    ocr_languages: str = "ara+eng+fra"

    # ---- extraction ----
    extraction_mode: str = "auto"  # auto | vlm | native
    vlm_concurrency: int = 1       # free tiers allow ~30 requests/minute: keep this low
    render_long_edge: int = 1800
    render_jpeg_quality: int = 85  # JPEG keeps each page image far below provider upload limits (e.g. Groq 4MB)
    vlm_max_tokens: int = 4000
    max_upload_mb: int = 300

    # ---- chunking / retrieval ----
    chunk_target_chars: int = 1500
    chunk_max_chars: int = 2400
    top_k_candidates: int = 30
    top_k_final: int = 5
    max_context_chars: int = 9000   # keeps one question well inside free-tier tokens-per-minute budgets
    history_messages: int = 6
    answer_max_tokens: int = 1200

    # ---- multi-RAG router ----
    router_enabled: bool = True
    router_max_subjects: int = 3
    router_min_confidence: float = 0.5
    rerank_per_subject: int = 15    # candidates per routed subject that go into the cross-subject rerank

    # ---- background jobs (Celery) ----
    task_lease_ttl_seconds: int = 300      # a running task renews its lock every ttl/3; a crashed one frees it in <= 5 min
    task_done_ttl_seconds: int = 86400     # remember finished jobs for a day so a duplicate message is ignored
    stuck_book_minutes: int = 30           # Beat re-queues a `processing` book untouched for this long
    stuck_pending_minutes: int = 180       # ...and a `pending` one (it may just be waiting in the queue) after this long
    orphan_file_hours: int = 24            # Beat deletes PDFs without a database row after this long

    # ---- limits ----
    default_daily_requests: int = 60
    default_daily_tokens: int = 150_000
    rate_limit_per_minute: int = 12
    global_questions_per_minute: int = 15  # protects the shared free-tier quota across ALL students (0 = off)
    login_attempts_per_5min: int = 10
    max_question_chars: int = 2000

    @model_validator(mode="after")
    def _production_safety(self):
        """In prod, refuse to start with a guessable secret or wide-open CORS: fail loudly, not silently insecure."""
        if self.app_env == "prod":
            if self.jwt_secret in ("change-me", "change-me-to-a-long-random-string") or len(self.jwt_secret) < 32:
                raise ValueError("APP_ENV=prod needs JWT_SECRET of at least 32 random characters")
            if self.cors_origins.strip() == "*":
                raise ValueError("APP_ENV=prod needs CORS_ORIGINS set to your real site, not *")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
