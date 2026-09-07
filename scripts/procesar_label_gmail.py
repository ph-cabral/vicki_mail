"""Procesa el backlog de CVs que quedó etiquetado en Gmail, SIN escribirle a nadie.

Es la versión "a mano" del flujo del mail (app/nodes.py) para los mensajes que
ya estaban etiquetados antes de trabajarlos: mismo pipeline y mismas funciones,
pero **no se manda un solo mail** — ni respuesta al candidato, ni reenvío a
RRHH, ni acuse. El módulo `app.email_templates` ni siquiera se importa: acá no
hay forma de que salga un mail.

    label origen  →  adjunto CV  →  texto (PDF nativo o LibreOffice)
                  →  perfil (LLM)  →  texto_limpio
                  →  rag_system.candidato
                  →  rag_system.documento_aprobado
                  →  Qdrant 'postulantes'      ← esto es lo que busca el chat
                  →  cv_store local (original + PDF + miniatura)
                  →  se le pone el label destino y se le saca el de origen

Tampoco sube nada a Drive: el archivo queda sólo en el store local (que es de
donde lo lee la barra de CVs del chat) y en el mail original.

## Qué hace con cada mensaje

- **CV nuevo** → se carga entero y el mail pasa al label destino.
- **CV que ya estaba en la base** → no se vuelve a llamar al LLM. Se
  **corrobora que esté completo** (candidato, texto_limpio, vectores en Qdrant,
  original + PDF + miniatura en el store) y se completa sólo lo que falte;
  después pasa al label destino igual. Los flags de la base no se creen de
  palabra: se mira el disco.
- **Adjunto que es foto/escaneo** (texto extraído por debajo de
  MIN_CHARS_TEXTO_VALIDO), **mensaje sin CV adjunto** y **falla del LLM** →
  no se tocan: quedan con el label de origen para revisar a mano, y salen en
  el CSV del reporte.

## Uso

    docker compose exec vicki-mail python -m scripts.procesar_label_gmail --dry-run --limit 10
    docker compose exec vicki-mail python -m scripts.procesar_label_gmail --limit 10
    docker compose exec vicki-mail python -m scripts.procesar_label_gmail

--dry-run baja, extrae y analiza, pero no escribe en la base ni mueve labels.
OJO con el costo: cada CV **nuevo** es una llamada al LLM (Claude, con fallback
a OpenAI); los que ya estaban no gastan nada (a lo sumo embeddings, si les
faltaba el índice de Qdrant). Por eso conviene la corrida chica primero.

Los labels se pasan por nombre, tal como se los copia de Gmail
(`label:seleccion-y-reclutamiento-cv-no-procesado`); el ID interno se resuelve
solo. Se puede cortar y volver a correr: lo ya procesado sale del label de
origen, y el hash de archivo es UNIQUE en la base.
"""
import argparse
import csv
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from app import cv_store, gmail_client
from app.config import config
from app.db import (
    construir_texto_limpio,
    ensure_columnas_archivo,
    get_pool,
    marcar_archivo,
    upsert_candidato,
    upsert_documento_cv,
)
from app.extract import (
    EXTENSION_POR_MIME_CONVERTIBLE,
    calcular_hash,
    convertir_a_pdf,
    es_imagen_o_escaneo,
    extraer_texto,
    filtrar_adjunto_cv,
)
from app.llm import analizar_cv
from app.qdrant_store import hashes_indexados, upsert_documento

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("label")

LABEL_ORIGEN = os.getenv("LABEL_BACKLOG_ORIGEN", "seleccion-y-reclutamiento-cv-no-procesado")
LABEL_DESTINO = os.getenv("LABEL_BACKLOG_DESTINO", "cv-procesados")
REPORTE = os.getenv("LABEL_BACKLOG_REPORTE", "/tmp/procesar_label_gmail.csv")

# googleapiclient no es thread-safe (el objeto service comparte una conexión
# httplib2), así que TODA llamada a Gmail pasa por este lock. Lo caro —
# LibreOffice, el LLM, los embeddings — queda afuera, que es donde el
# paralelismo realmente sirve.
_gmail_lock = threading.Lock()
_lock = threading.Lock()

# hash → de qué mail salió. El mismo CV entra varias veces (la persona lo manda
# dos veces, o reenvía el hilo): sin esto, cada copia paga de nuevo el LLM y los
# embeddings para escribir exactamente lo mismo. Los repetidos se mueven al
# label destino igual, pero sin trabajo. Si dos copias caen en el mismo instante
# en dos workers distintos, la segunda se puede colar y pagarse dos veces —
# corta la enorme mayoría, no pretende ser un lock.
_hechos: dict[str, str] = {}


def _g(fn, *args, **kwargs):
    with _gmail_lock:
        return fn(*args, **kwargs)


# ── índice de lo que ya está en la base (UNA consulta) ──────────────────────

_SQL_INDICE = """
SELECT d.hash_archivo,
       d.candidato_id,
       (COALESCE(TRIM(d.texto_limpio), '') <> '') AS con_texto,
       d.archivo_local, d.archivo_pdf, d.archivo_thumb,
       c.nombre, c.apellido, c.email
  FROM rag_system.documento_aprobado d
  LEFT JOIN rag_system.candidato c ON c.id = d.candidato_id
 WHERE d.tipo = 'CV'
"""

_COLS = ["hash_archivo", "candidato_id", "con_texto", "archivo_local",
         "archivo_pdf", "archivo_thumb", "nombre", "apellido", "email"]


def indice_docs() -> dict[str, dict]:
    """hash_archivo → estado del CV en la base, en una sola consulta.

    Se trae el índice entero una vez y no una consulta por mail: son ~800 filas
    y el backlog puede ser de cientos de mensajes. No se trae `texto_limpio`
    (son varios KB por fila y casi nunca hace falta), sólo si está: el texto se
    busca puntualmente para los pocos que haya que reindexar."""
    with get_pool().connection() as conn:
        filas = conn.execute(_SQL_INDICE).fetchall()
    return {f[0]: dict(zip(_COLS, f)) for f in filas}


def texto_limpio_de(hash_archivo: str) -> str:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT texto_limpio FROM rag_system.documento_aprobado WHERE hash_archivo = %s",
            (hash_archivo,),
        ).fetchone()
    return (row[0] if row else "") or ""


# ── helpers de procesamiento (mismo criterio que nodes.extract_text_node) ───

def _texto_y_pdf(data: bytes, mime: str, filename: str) -> tuple[str, bytes | None]:
    """Si el formato es convertible, primero a PDF (mejor fidelidad y se le
    puede mandar el archivo entero al LLM); si falla, texto local."""
    ext = EXTENSION_POR_MIME_CONVERTIBLE.get(mime)
    if ext:
        pdf = convertir_a_pdf(data, ext)
        if pdf:
            return _sin_nul(extraer_texto("application/pdf", pdf)), pdf
        log.warning("conversión a PDF falló para %s, sigo con texto local", filename)
    return _sin_nul(extraer_texto(mime, data)), (data if mime == "application/pdf" else None)


def _sin_nul(texto: str) -> str:
    """Saca los bytes NUL (0x00) del texto.

    Postgres no los acepta en campos `text` ni en `jsonb` (`PostgreSQL text
    fields cannot contain NUL (0x00) bytes`) y hay PDFs mal generados de los que
    pdfplumber los saca. Sin esto, el CV se pierde en el INSERT después de haber
    pagado la llamada al LLM."""
    return texto.replace("\x00", "") if texto else texto


def _guardar_archivo(h: str, data: bytes, mime: str, pdf: bytes | None) -> None:
    """Copia local (original + PDF + miniatura) y marcado en la base. A Drive
    no se sube nada."""
    try:
        res = cv_store.guardar(h, data, mime, pdf_data=pdf)
        marcar_archivo(h, local=True, pdf=res["pdf"], thumb=res["thumb"])
    except Exception:
        log.exception("no se pudo guardar el CV en el store local (hash=%s)", h[:12])


def _falta_archivo(h: str, doc: dict, mime: str) -> bool:
    """El archivo se da por presente sólo si está EN EL DISCO. Los flags de la
    base pueden haber quedado en true de una corrida vieja cuyo volumen ya no
    está montado — es justo lo que hay que detectar acá.

    El PDF sólo se exige si el formato lo permite: un .txt nunca lo va a tener,
    y pedírselo dejaría el mismo mail "incompleto" en cada corrida."""
    if not os.path.exists(cv_store.ruta_original(h, mime)):
        return True
    espera_pdf = mime == "application/pdf" or mime in EXTENSION_POR_MIME_CONVERTIBLE
    if espera_pdf and not os.path.exists(cv_store.ruta_pdf(h)):
        return True
    return not doc.get("archivo_local")


def _indexar(h: str, texto: str, doc: dict) -> None:
    upsert_documento(
        collection=config.QDRANT_COLLECTION_POSTULANTES,
        texto=texto,
        hash_archivo=h,
        metadata={
            "candidato_id": doc.get("candidato_id"),
            "nombre": doc.get("nombre"),
            "apellido": doc.get("apellido"),
            "email": doc.get("email"),
            "fuente": "email",
        },
    )


# ── un mensaje ──────────────────────────────────────────────────────────────

def _ok(h: str, filename: str, estado: str, detalle: str) -> tuple[str, str]:
    """Marca el hash como ya resuelto en esta corrida y devuelve el resultado."""
    with _lock:
        _hechos[h] = filename
    return estado, detalle


def procesar(message_id: str, args, docs: dict, indexados: set) -> tuple[str, str]:
    """(estado, detalle). Estados: nuevo | completado | ya_estaba | duplicado |
    sin_cv | imagen | error_llm."""
    msg = _g(gmail_client.get_message, message_id, download_attachments=True)
    quien = msg.get("from_address") or "?"

    cv = filtrar_adjunto_cv(msg.get("attachments") or [])
    if cv is None:
        return "sin_cv", f"{quien} — {msg.get('subject', '')[:60]}"

    data, mime, filename = cv["data"], cv["mime_type"], cv.get("filename") or "cv.pdf"
    h = calcular_hash(data)
    with _lock:
        ya = _hechos.get(h)
    if ya is not None:
        # el mismo archivo ya se resolvió en esta corrida (otro mail lo traía):
        # no se vuelve a extraer, ni a analizar, ni a indexar
        return "duplicado", f"{filename} — mismo CV que «{ya}»"
    doc = docs.get(h)

    # ── ya estaba en la base: no se paga el LLM, sólo se completa lo que falte
    if doc and doc.get("candidato_id") and doc.get("con_texto"):
        faltantes = []
        if h not in indexados:
            faltantes.append("qdrant")
        if _falta_archivo(h, doc, mime):
            faltantes.append("archivo")
        if not faltantes:
            return _ok(h, filename, "ya_estaba", f"{filename} — {h[:12]}")
        if args.dry_run:
            return _ok(h, filename, "completado", f"[dry-run] falta {'+'.join(faltantes)} — {filename}")
        if "archivo" in faltantes:
            _, pdf = _texto_y_pdf(data, mime, filename)
            _guardar_archivo(h, data, mime, pdf)
        if "qdrant" in faltantes:
            texto = texto_limpio_de(h)
            if texto:
                _indexar(h, texto, doc)
            else:
                faltantes.append("sin texto_limpio en la base")
        return _ok(h, filename, "completado", f"{filename} — se completó {'+'.join(faltantes)}")

    # ── CV nuevo (o fila incompleta: sin candidato o sin texto) → pipeline entero
    texto, pdf = _texto_y_pdf(data, mime, filename)
    if es_imagen_o_escaneo(texto):
        # foto o escaneo sin texto seleccionable: el LLM no lo puede leer y
        # cargarlo sería meter un candidato vacío en la base
        return "imagen", f"{quien} — {filename}"

    perfil = analizar_cv(
        {"mime_type": "application/pdf", "data": pdf, "filename": filename} if pdf
        else {"mime_type": mime, "data": data, "filename": filename},
        texto,
    )
    if perfil.get("error"):
        return "error_llm", str(perfil.get("detail"))[:200]
    # el perfil va a una columna jsonb, que tampoco admite \u0000: el LLM
    # devuelve fragmentos del texto del CV y puede arrastrarlos
    perfil = json.loads(json.dumps(perfil).replace("\\u0000", ""))
    try:
        texto_limpio = construir_texto_limpio(perfil)
    except Exception as e:
        return "error_llm", f"texto_limpio: {e}"

    dp = perfil.get("datos_personales", {}) or {}
    persona = f"{dp.get('nombre', '')} {dp.get('apellido', '')} <{dp.get('email', '')}>".strip()
    if args.dry_run:
        return _ok(h, filename, "nuevo", f"[dry-run] {persona} — {len(texto)} chars")

    candidato = upsert_candidato(perfil)
    upsert_documento_cv(
        hash_archivo=h,
        nombre_archivo=filename,
        texto_limpio=texto_limpio,
        perfil=perfil,
        candidato_id=candidato["id"],
        mime_type=mime,
        tamanio_bytes=len(data),
        texto_raw=texto,
        email_id=message_id,
        accion=candidato["accion"],
    )
    _indexar(h, texto_limpio, {
        "candidato_id": candidato["id"], "nombre": candidato.get("nombre"),
        "apellido": candidato.get("apellido"), "email": candidato.get("email"),
    })
    _guardar_archivo(h, data, mime, pdf)
    return _ok(h, filename, "nuevo", persona)


def mover(message_id: str, destino: str, origen: str) -> None:
    """Le pone el label de procesado y le saca el de origen en UNA sola llamada
    (modify acepta las dos listas juntas). El mail no se toca de ninguna otra
    forma: no se borra, no se archiva, no se marca leído."""
    with _gmail_lock:
        gmail_client._service().users().messages().modify(
            userId="me", id=message_id,
            body={"addLabelIds": [destino], "removeLabelIds": [origen]},
        ).execute()


# ── main ────────────────────────────────────────────────────────────────────

MUEVEN = {"nuevo", "completado", "ya_estaba", "duplicado"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Procesa el backlog de CVs de un label de Gmail, sin mandar mails.")
    ap.add_argument("--origen", default=LABEL_ORIGEN, help="label a procesar (nombre o ID)")
    ap.add_argument("--destino", default=LABEL_DESTINO, help="label de los procesados (nombre o ID)")
    ap.add_argument("--limit", type=int, default=0, help="cortar después de N mensajes")
    ap.add_argument("--dry-run", action="store_true", help="analiza pero no escribe ni mueve labels")
    ap.add_argument("--workers", type=int, default=4, help="mensajes en paralelo (el LLM es espera de red)")
    ap.add_argument("--reporte", default=REPORTE, help="CSV con lo que quedó sin procesar")
    args = ap.parse_args()

    origen = gmail_client.label_id_por_nombre(args.origen)
    destino = gmail_client.label_id_por_nombre(args.destino)
    if not origen:
        log.error("no existe el label de origen '%s' en %s", args.origen, config.GMAIL_USER)
        return 1
    if not destino:
        log.error("no existe el label destino '%s' en %s (crealo en Gmail primero)", args.destino, config.GMAIL_USER)
        return 1
    log.info("origen %s (%s) → destino %s (%s)", args.origen, origen, args.destino, destino)

    if not args.dry_run:
        ensure_columnas_archivo()

    mensajes = []
    for mid in gmail_client.iter_messages_por_label(origen):
        mensajes.append(mid)
        if args.limit and len(mensajes) >= args.limit:
            break
    log.info("%d mensajes con el label de origen", len(mensajes))
    if not mensajes:
        return 0

    docs = indice_docs()
    indexados = hashes_indexados(config.QDRANT_COLLECTION_POSTULANTES, list(docs))
    log.info("%d CVs en la base, %d con vectores en Qdrant", len(docs), len(indexados))

    contadores: dict[str, int] = {}
    reporte: list[list] = []

    def _uno(mid: str):
        try:
            estado, detalle = procesar(mid, args, docs, indexados)
        except Exception as e:
            log.exception("falló el mensaje %s", mid)
            estado, detalle = "error", f"{type(e).__name__}: {e}"
        if estado in MUEVEN and not args.dry_run:
            try:
                mover(mid, destino, origen)
            except Exception:
                log.exception("procesado pero no se pudo mover el label del mensaje %s", mid)
                estado = "error_label"
        with _lock:
            contadores[estado] = contadores.get(estado, 0) + 1
            if estado in MUEVEN:
                log.info("%-10s %s", estado, detalle)
            else:
                reporte.append([mid, f"https://mail.google.com/mail/u/0/#all/{mid}", estado, detalle])
            n = sum(contadores.values())
            if n % 25 == 0:
                log.info("%d/%d… %s", n, len(mensajes), contadores)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        list(ex.map(_uno, mensajes))

    with open(args.reporte, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["message_id", "link", "estado", "detalle"])
        w.writerows(reporte)

    log.info("listo: %s", contadores)
    log.info("%d mensajes quedaron en '%s' para revisar a mano → %s",
             len(reporte), args.origen, args.reporte)
    return 0


if __name__ == "__main__":
    sys.exit(main())
