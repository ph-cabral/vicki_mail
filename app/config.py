import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Postgres ──────────────────────────────────────────────────────────
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # ── Gmail / Drive OAuth2 (credencial reusada de n8n) ─────────────────
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REFRESH_TOKEN: str = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    GMAIL_USER: str = os.getenv("GMAIL_USER", "seleccion@everwear.com.ar")

    # ── LLM ──────────────────────────────────────────────────────────────
    ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_KEY", "")
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

    # ── Qdrant ───────────────────────────────────────────────────────────
    QDRANT_URL: str = os.getenv("QDRANT_URL", "http://n8n_qdrant:6333")
    QDRANT_API_KEY: str = os.getenv("QDRANT_API_KEY", "")
    QDRANT_COLLECTION_CVS: str = os.getenv("QDRANT_COLLECTION_CVS", "cvs")
    QDRANT_COLLECTION_DOCS: str = os.getenv("QDRANT_COLLECTION_DOCS", "documentos")

    # ── Polling ──────────────────────────────────────────────────────────
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "5"))

    # ── Emails / dominios ────────────────────────────────────────────────
    RRHH_EMAIL: str = os.getenv("RRHH_EMAIL", "rrhh@everwear.com.ar")
    RRHH_INTERNAL_CONTACT: str = os.getenv("RRHH_INTERNAL_CONTACT", "recursoshumanos@everwear.com.ar")
    INTERNAL_DOMAIN: str = os.getenv("INTERNAL_DOMAIN", "everwear.com.ar")

    TZ: str = os.getenv("TZ", "America/Argentina/Buenos_Aires")


config = Config()
