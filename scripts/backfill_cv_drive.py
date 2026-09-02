"""Backfill del archivo de los CVs historicos que ya estan en Drive.

Contexto: hasta ahora el CV se subia a la carpeta de archivo de Drive y el id
que devolvia la subida se descartaba, asi que no hay ningun vinculo entre
rag_system.documento_aprobado y el archivo. Este script lo reconstruye.

Como matchea: baja cada archivo de la carpeta, le calcula el MISMO sha256 que
usa la ingesta (extract.calcular_hash) y busca ese hash en documento_aprobado
(columna UNIQUE). Es el unico criterio confiable — el nombre del archivo se
repite ("CV.pdf" hay a montones) y no sirve para decidir de quien es.

Que hace con lo que matchea: lo guarda en el store local (original + PDF +
miniatura, ver app/cv_store.py) y le escribe drive_file_id.
Lo que no matchea se lista en un CSV para revisarlo a mano — no se inventa
ninguna asociacion.

Uso (dentro del contenedor, que ya tiene LibreOffice y poppler):

    docker compose exec vicki-mail python -m scripts.backfill_cv_drive
    docker compose exec vicki-mail python -m scripts.backfill_cv_drive --dry-run
    docker compose exec vicki-mail python -m scripts.backfill_cv_drive --limit 50

Es idempotente: se puede cortar y volver a correr. Por default saltea los
documentos que ya tienen archivo local (--rehacer los vuelve a escribir).
"""
import argparse
import csv
import logging
import os
import sys

from googleapiclient.errors import HttpError

from app import cv_store, drive_client
from app.constants import DRIVE_FOLDER_CV_ARCHIVE
from app.db import ensure_columnas_archivo, get_pool, marcar_archivo
from app.extract import EXTENSION_POR_MIME_CONVERTIBLE, calcular_hash, convertir_a_pdf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill")

REPORTE = os.getenv("BACKFILL_REPORTE", "/tmp/backfill_cv_sin_match.csv")


def listar_carpeta(folder_id: str):
    """Igual que drive_client.list_folder pero paginado: la carpeta de archivo
    tiene miles de CVs y sin pageToken Drive devuelve solo los primeros 100."""
    svc = drive_client._service()
    token = None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,size)",
            pageSize=1000,
            pageToken=token,
        ).execute()
        for f in resp.get("files", []):
            yield f
        token = resp.get("nextPageToken")
        if not token:
            break


def indice_por_hash() -> dict:
    """Todos los CVs de la base indexados por hash, en UNA sola consulta.

    A proposito no se consulta por archivo: son miles de archivos en Drive y
    serian miles de idas a Postgres. El hash es un sha256 (64 chars), asi que
    incluso con decenas de miles de CVs el mapa entra holgado en memoria y el
    match pasa a ser un lookup de diccionario."""
    sql = """
    SELECT hash_archivo, id, candidato_id, mime_type, archivo_local
      FROM rag_system.documento_aprobado
     WHERE tipo = 'CV' AND hash_archivo IS NOT NULL
    """
    with get_pool().connection() as conn:
        filas = conn.execute(sql).fetchall()
    return {
        f[0]: {"id": f[1], "candidato_id": f[2], "mime_type": f[3], "archivo_local": f[4]}
        for f in filas
    }


def procesar(archivo: dict, doc: dict, dry_run: bool) -> str:
    data = archivo["_data"]
    mime = archivo.get("mimeType") or doc["mime_type"] or ""
    if dry_run:
        return "match"
    pdf = None
    ext = EXTENSION_POR_MIME_CONVERTIBLE.get(mime)
    if ext:
        pdf = convertir_a_pdf(data, ext)
    res = cv_store.guardar(archivo["_hash"], data, mime, pdf_data=pdf)
    marcar_archivo(
        archivo["_hash"], drive_file_id=archivo["id"],
        local=True, pdf=res["pdf"], thumb=res["thumb"],
    )
    return "guardado"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=DRIVE_FOLDER_CV_ARCHIVE)
    ap.add_argument("--limit", type=int, default=0, help="cortar despues de N archivos")
    ap.add_argument("--dry-run", action="store_true", help="solo reporta, no escribe")
    ap.add_argument("--rehacer", action="store_true", help="reprocesar los que ya tienen archivo local")
    args = ap.parse_args()

    if not args.dry_run:
        ensure_columnas_archivo()

    docs = indice_por_hash()
    log.info("%d CVs en la base para matchear", len(docs))

    total = matcheados = guardados = saltados = 0
    sin_match = []

    for f in listar_carpeta(args.folder):
        total += 1
        if args.limit and total > args.limit:
            total -= 1
            break
        try:
            data = drive_client.download_file(f["id"])
        except HttpError as e:
            log.warning("no pude bajar %s (%s): %s", f.get("name"), f["id"], e)
            sin_match.append([f["id"], f.get("name", ""), f.get("size", ""), "error_descarga"])
            continue

        h = calcular_hash(data)
        f["_data"], f["_hash"] = data, h
        doc = docs.get(h)
        if not doc:
            sin_match.append([f["id"], f.get("name", ""), f.get("size", ""), "sin_match"])
            continue

        matcheados += 1
        if doc["archivo_local"] and not args.rehacer:
            saltados += 1
        else:
            estado = procesar(f, doc, args.dry_run)
            if estado == "guardado":
                guardados += 1
        if total % 50 == 0:
            log.info("%d archivos… %d con match, %d guardados", total, matcheados, guardados)

    with open(REPORTE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["drive_file_id", "nombre", "tamanio", "motivo"])
        w.writerows(sin_match)

    log.info(
        "listo: %d archivos en Drive, %d con match en la base, %d guardados, "
        "%d ya estaban, %d sin match (ver %s)",
        total, matcheados, guardados, saltados, len(sin_match), REPORTE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
