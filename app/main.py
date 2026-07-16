import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app import gmail_client
from app.config import config
from app.constants import LABEL_CV_PROCESADO, LABEL_QUEUE
from app.graph import build_graph

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

graph = None
scheduler: AsyncIOScheduler | None = None


def procesar_cola() -> None:
    """Job del scheduler: equivalente al 'Schedule Trigger' + 'Recibir
    Mensaje' de n8n. Toma hasta BATCH_SIZE mensajes de la cola (LABEL_QUEUE)
    y corre el grafo para cada uno, uno por vez."""
    try:
        ids = gmail_client.list_queue(LABEL_QUEUE, max_results=config.BATCH_SIZE)
    except Exception:
        log.exception("no se pudo listar la cola de Gmail")
        return

    for message_id in ids:
        try:
            msg = gmail_client.get_message(message_id, download_attachments=True)
            nombres_adjuntos = " ".join(a.get("filename", "") for a in msg.get("attachments", []))
            initial_state = {
                "message_id": msg["id"],
                "thread_id": msg.get("thread_id"),
                "from_address": msg.get("from_address", ""),
                "from_name": msg.get("from_name", ""),
                "reply_to_address": msg.get("reply_to_address", ""),
                "subject": msg.get("subject", ""),
                "snippet": msg.get("snippet", ""),
                "body_text": msg.get("body_text", ""),
                "label_ids": msg.get("label_ids", []),
                "is_sent": msg.get("is_sent", False),
                "attachments": msg.get("attachments", []),
            }
            log.info("procesando mensaje %s de %s (adjuntos: %s)", message_id, msg.get("from_address"), nombres_adjuntos or "-")
            result = graph.invoke(initial_state)
            log.info("mensaje %s -> %s", message_id, result.get("accion_final"))
        except Exception:
            log.exception("error procesando mensaje %s (queda en la cola para reintentar)", message_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global graph, scheduler
    graph = build_graph().compile()
    log.info("grafo compilado")

    scheduler = AsyncIOScheduler(timezone=config.TZ)
    scheduler.add_job(
        procesar_cola, "cron",
        day_of_week=config.POLL_CRON_DAY_OF_WEEK,
        hour=config.POLL_CRON_HOUR,
        minute=config.POLL_CRON_MINUTE,
        id="procesar_cola",
    )
    scheduler.start()
    log.info(
        "scheduler iniciado (cron: dia=%s hora=%s minuto=%s, tz=%s)",
        config.POLL_CRON_DAY_OF_WEEK, config.POLL_CRON_HOUR, config.POLL_CRON_MINUTE, config.TZ,
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="vicki_mail", description="Ingesta de CVs por email + notas de reunion", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "vicki-mail"}


@app.get("/labels")
def labels():
    """Lista todos los labels del buzon con su ID real -- para verificar que
    LABEL_QUEUE / LABEL_CV_PROCESADO en .env apuntan a lo correcto."""
    return {
        "labels": gmail_client.list_labels(),
        "configurados": {
            "LABEL_QUEUE": LABEL_QUEUE,
            "LABEL_CV_PROCESADO": LABEL_CV_PROCESADO,
        },
    }


@app.post("/process_now")
def process_now():
    """Dispara el procesamiento de la cola manualmente (para pruebas)."""
    procesar_cola()
    return {"ok": True}
