"""Ingesta COMPLETA de los CVs que sólo existen en Drive.

Diferencia con scripts/backfill_cv_drive.py: aquel sólo RE-VINCULA archivos de
Drive con CVs que YA estaban en la base (matchea por hash y les escribe el
drive_file_id + la copia local). Todo lo que ese script reporta como
"sin_match" es un CV que nunca pasó por vicki_mail: no tiene candidato, no
tiene texto y no está en Qdrant, así que el chat no lo puede encontrar. Este
script es el que los da de alta.

Corre el MISMO pipeline que un CV que llega por mail (app/nodes.py), en el
mismo orden y con las mismas funciones — no hay una segunda implementación que
se pueda desincronizar:

    Drive → texto (PDF nativo o LibreOffice) → perfil (LLM) → texto_limpio
          → rag_system.candidato (upsert por nombre/email)
          → rag_system.documento_aprobado (upsert por hash)
          → Qdrant colección 'cvs'   ← esto es lo que busca el chat
          → cv_store local (original + PDF + miniatura) + drive_file_id

Idempotente: la lista de hashes ya cargados se trae en UNA consulta y los
archivos repetidos (el mismo CV subido dos veces a Drive) se saltean en
memoria. Se puede cortar y volver a correr.

Uso (dentro del contenedor, que ya tiene LibreOffice y poppler):

    docker compose exec vicki-mail python -m scripts.ingesta_cv_drive --limit 10 --dry-run
    docker compose exec vicki-mail python -m scripts.ingesta_cv_drive --limit 10
    docker compose exec vicki-mail python -m scripts.ingesta_cv_drive

OJO con el costo: cada CV nuevo es UNA llamada al LLM (Claude, con fallback a
OpenAI). Por eso el flujo recomendado es --limit 10 --dry-run primero: baja,
extrae y analiza 10, muestra qué sacó y no escribe nada.
"""
import argparse
import csv
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from googleapiclient.errors import HttpError

from app import cv_store, drive_client
from app.config import config
from app.constants import DRIVE_FOLDER_CV_ARCHIVE
from app.db import (
    construir_texto_limpio,
    ensure_columnas_archivo,
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
)
from app.llm import analizar_cv
from app.qdrant_store import upsert_documento

# Reusa el listado paginado y el índice de hashes del backfill: misma carpeta,
# mismo criterio de "ya está en la base".
from scripts.backfill_cv_drive import indice_por_hash, listar_carpeta

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("ingesta")

REPORTE = os.getenv("INGESTA_REPORTE", "/tmp/ingesta_cv_drive.csv")

# Google Docs nativos: no se bajan crudos, se exportan a .docx.
GOOGLE_DOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Tipos de Drive que no son un archivo (carpetas, sheets, forms…): se saltean.
GOOGLE_APPS_PREFIX = "application/vnd.google-apps"


def _bajar(f: dict) -> tuple[bytes, str]:
    """Bytes + mime real del archivo de Drive. Un Google Doc se exporta a docx
    (no tiene contenido descargable); el resto se baja tal cual."""
    mime = f.get("mimeType") or ""
    if mime == GOOGLE_DOC:
        return drive_client.export_as_docx(f["id"]), DOCX
    return drive_client.download_file(f["id"]), mime


def _texto_y_pdf(data: bytes, mime: str, filename: str) -> tuple[str, dict | None]:
    """Mismo criterio que nodes.extract_text_node: si el formato es
    convertible, primero a PDF (mejor fidelidad y se le puede mandar el
    archivo entero al LLM); si falla, texto local."""
    ext = EXTENSION_POR_MIME_CONVERTIBLE.get(mime)
    if ext:
        pdf = convertir_a_pdf(data, ext)
        if pdf:
            return extraer_texto("application/pdf", pdf), {
                "mime_type": "application/pdf", "data": pdf, "filename": filename,
            }
        log.warning("conversión a PDF falló para %s, sigo con texto local", filename)
    return extraer_texto(mime, data), None


def procesar(f: dict, dry_run: bool) -> tuple[str, str]:
    """Devuelve (estado, detalle). Estados: ok | ya_estaba | sin_texto |
    formato | error_descarga | error_llm | error."""
    mime = f.get("mimeType") or ""
    if mime.startswith(GOOGLE_APPS_PREFIX) and mime != GOOGLE_DOC:
        return "formato", mime

    try:
        data, mime = _bajar(f)
    except HttpError as e:
        return "error_descarga", str(e)

    h = calcular_hash(data)
    f["_hash"] = h
    if h in YA_CARGADOS:
        return "ya_estaba", h[:12]

    filename = f.get("name") or "cv.pdf"
    texto, para_ia = _texto_y_pdf(data, mime, filename)
    if es_imagen_o_escaneo(texto):
        # Foto o escaneo sin texto seleccionable: el LLM no lo puede leer y
        # cargarlo sería meter un candidato vacío en la base. Va al CSV.
        return "sin_texto", filename

    perfil = analizar_cv(para_ia or {"mime_type": mime, "data": data, "filename": filename}, texto)
    if perfil.get("error"):
        return "error_llm", str(perfil.get("detail"))[:200]
    try:
        texto_limpio = construir_texto_limpio(perfil)
    except Exception as e:
        return "error_llm", f"texto_limpio: {e}"

    dp = perfil.get("datos_personales", {}) or {}
    quien = f"{dp.get('nombre','')} {dp.get('apellido','')} <{dp.get('email','')}>".strip()
    if dry_run:
        return "ok", f"[dry-run] {quien} — {len(texto)} chars"

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
        # De dónde salió: sin esto no hay forma de distinguir después un CV
        # histórico de Drive de uno que entró por mail.
        email_id=f"drive:{f['id']}",
        accion=candidato["accion"],
    )
    # Lo que hace que el chat lo encuentre. Si esto falla, el CV queda en la
    # base pero invisible para la búsqueda → se reporta como error, no se traga.
    upsert_documento(
        collection=config.QDRANT_COLLECTION_CVS,
        texto=texto_limpio,
        hash_archivo=h,
        metadata={
            "candidato_id": candidato["id"],
            "nombre": candidato.get("nombre"),
            "apellido": candidato.get("apellido"),
            "email": candidato.get("email"),
            "fuente": "drive",
        },
    )
    # Copia local para la barra de CVs del chat (miniatura + PDF). No se
    # vuelve a subir nada a Drive: el archivo ya está ahí, sólo se guarda su id.
    try:
        res = cv_store.guardar(h, data, mime, pdf_data=(para_ia or {}).get("data"))
        marcar_archivo(h, drive_file_id=f["id"], local=True, pdf=res["pdf"], thumb=res["thumb"])
    except Exception:
        log.exception("no pude guardar %s en el store local (sigue igual en Qdrant)", filename)
        marcar_archivo(h, drive_file_id=f["id"])
    return "ok", quien


YA_CARGADOS: set = set()
_lock = Lock()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=DRIVE_FOLDER_CV_ARCHIVE)
    ap.add_argument("--limit", type=int, default=0, help="cortar después de N archivos")
    ap.add_argument("--dry-run", action="store_true",
                    help="baja, extrae y analiza, pero NO escribe nada")
    ap.add_argument("--workers", type=int, default=4,
                    help="archivos en paralelo (descarga + LLM son espera de red)")
    args = ap.parse_args()

    if not args.dry_run:
        ensure_columnas_archivo()

    YA_CARGADOS.update(indice_por_hash().keys())
    log.info("%d CVs ya cargados en la base (se saltean)", len(YA_CARGADOS))

    archivos = []
    for f in listar_carpeta(args.folder):
        archivos.append(f)
        if args.limit and len(archivos) >= args.limit:
            break
    log.info("%d archivos en la carpeta de Drive", len(archivos))

    contadores: dict[str, int] = {}
    reporte: list[list] = []

    def _uno(f: dict):
        try:
            estado, detalle = procesar(f, args.dry_run)
        except Exception as e:
            log.exception("falló %s", f.get("name"))
            estado, detalle = "error", f"{type(e).__name__}: {e}"
        with _lock:
            contadores[estado] = contadores.get(estado, 0) + 1
            if estado == "ok":
                # el hash recién cargado no se vuelve a procesar si el mismo CV
                # está duplicado más adelante en la carpeta
                if f.get("_hash"):
                    YA_CARGADOS.add(f["_hash"])
                log.info("ok  %s — %s", f.get("name"), detalle)
            elif estado != "ya_estaba":
                reporte.append([f["id"], f.get("name", ""), estado, detalle])
            n = sum(contadores.values())
            if n % 25 == 0:
                log.info("%d/%d… %s", n, len(archivos), contadores)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        list(ex.map(_uno, archivos))

    with open(REPORTE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["drive_file_id", "nombre", "estado", "detalle"])
        w.writerows(reporte)

    log.info("listo: %s", contadores)
    log.info("%d archivos para revisar a mano en %s", len(reporte), REPORTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
