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
    # Cuentas de respaldo de Anthropic: cuando una se queda sin créditos se pasa
    # a la siguiente ANTES de caer al fallback de OpenAI (ver llm.analizar_cv).
    # ANTHROPIC_KEY_2 es el atajo para el caso de dos cuentas; ANTHROPIC_KEYS
    # acepta varias separadas por coma si algún día hay más.
    ANTHROPIC_KEY_2: str = os.getenv("ANTHROPIC_KEY_2", "")
    ANTHROPIC_KEYS: str = os.getenv("ANTHROPIC_KEYS", "")
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

    # ── Qdrant ───────────────────────────────────────────────────────────
    QDRANT_URL: str = os.getenv("QDRANT_URL", "http://n8n_qdrant:6333")
    QDRANT_API_KEY: str = os.getenv("QDRANT_API_KEY", "")
    # 5s (el default de qdrant_client) se queda corto en escrituras en lote
    # — ver qdrant_store._qdrant().
    QDRANT_TIMEOUT: float = float(os.getenv("QDRANT_TIMEOUT", "60"))
    # Datos del POSTULANTE extraídos del CV (texto_limpio: perfil, experiencia,
    # formación). Es lo que busca el chat. Se llamaba 'cvs', pero el nombre
    # confundía: acá no hay CVs, hay datos sacados de los CVs. El nombre 'cvs'
    # queda reservado para una colección del texto crudo del archivo.
    QDRANT_COLLECTION_POSTULANTES: str = os.getenv("QDRANT_COLLECTION_POSTULANTES", "postulantes")
    QDRANT_COLLECTION_DOCS: str = os.getenv("QDRANT_COLLECTION_DOCS", "documentos")

    # ── Polling (cron, horario laboral -- ver TZ mas abajo) ────────────────
    POLL_CRON_DAY_OF_WEEK: str = os.getenv("POLL_CRON_DAY_OF_WEEK", "mon-fri")
    POLL_CRON_HOUR: str = os.getenv("POLL_CRON_HOUR", "8-17")
    POLL_CRON_MINUTE: str = os.getenv("POLL_CRON_MINUTE", "*/2")
    BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "5"))

    # ── Emails / dominios ────────────────────────────────────────────────
    RRHH_EMAIL: str = os.getenv("RRHH_EMAIL", "rrhh@everwear.com.ar")
    RRHH_INTERNAL_CONTACT: str = os.getenv("RRHH_INTERNAL_CONTACT", "recursoshumanos@everwear.com.ar")
    INTERNAL_DOMAIN: str = os.getenv("INTERNAL_DOMAIN", "everwear.com.ar")

    TZ: str = os.getenv("TZ", "America/Argentina/Buenos_Aires")

    @property
    def anthropic_keys(self) -> list[str]:
        """Las cuentas de Anthropic a probar, en orden y sin repetidas.
        Vacía si no hay ninguna configurada (ahí se va derecho a OpenAI)."""
        crudas = [self.ANTHROPIC_KEY, self.ANTHROPIC_KEY_2, *self.ANTHROPIC_KEYS.split(",")]
        vistas: set[str] = set()
        orden: list[str] = []
        for k in (x.strip() for x in crudas):
            if k and k not in vistas:
                vistas.add(k)
                orden.append(k)
        return orden


config = Config()
