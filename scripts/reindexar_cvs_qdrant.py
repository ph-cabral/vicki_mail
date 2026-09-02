"""Reindexa a Qdrant los CVs que YA están en Postgres.

Para qué: el chat de Vicki no encontraba a nadie porque busca en Qdrant, y la
colección 'cvs' estaba vacía (o se perdió). Pero el texto ya procesado
(`texto_limpio`) y el candidato asociado están en
`rag_system.documento_aprobado` — 813 CVs. O sea: no hace falta volver a bajar
nada de Drive ni volver a pasar los CVs por el LLM. Sólo hay que volver a
calcular los embeddings y escribirlos.

Costo: embeddings (text-embedding-3-small), centavos. Cero llamadas al LLM.

Idempotente: los ids de punto son determinísticos (`hash_archivo` + índice de
chunk, ver qdrant_store._point_id), así que correrlo dos veces sobrescribe, no
duplica.

Eficiencia (son miles de chunks):
- Los CVs se leen de Postgres en lotes con keyset pagination por id — nada de
  traer las 813 filas con su texto entero de una.
- Los embeddings se piden por lote de varios CVs juntos (una llamada a OpenAI
  por lote, no una por CV) y se escriben con un solo upsert por lote.

Uso (dentro del contenedor):

    docker compose exec vicki-mail python -m scripts.reindexar_cvs_qdrant --dry-run
    docker compose exec vicki-mail python -m scripts.reindexar_cvs_qdrant
    docker compose exec vicki-mail python -m scripts.reindexar_cvs_qdrant --solo-faltantes
"""
import argparse
import logging
import sys
import time

from qdrant_client.http import models as qmodels

from app.config import config
from app.db import get_pool
from app.qdrant_store import _point_id, _qdrant, chunk_text, embed, ensure_collection

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reindex")

# Trae el CV con el candidato ya resuelto: la metadata que guarda la ingesta
# (nombre/apellido/email) sale de rag_system.candidato, no del documento.
# Keyset pagination por d.id: sin OFFSET, que en tablas grandes vuelve a
# escanear todo lo ya leído en cada página.
SQL_LOTE = """
SELECT d.id, d.hash_archivo, d.texto_limpio, d.candidato_id,
       c.nombre, c.apellido, c.email
  FROM rag_system.documento_aprobado d
  LEFT JOIN rag_system.candidato c ON c.id = d.candidato_id
 WHERE d.tipo = 'CV'
   AND d.hash_archivo IS NOT NULL
   AND COALESCE(TRIM(d.texto_limpio), '') <> ''
   AND d.id > %(desde)s
 ORDER BY d.id
 LIMIT %(lote)s
"""


def leer_cvs(lote: int):
    """Generador de filas de CV, de a `lote` por consulta."""
    desde = 0
    while True:
        with get_pool().connection() as conn:
            filas = conn.execute(SQL_LOTE, {"desde": desde, "lote": lote}).fetchall()
        if not filas:
            return
        for f in filas:
            yield f
        desde = filas[-1][0]


def hashes_en_qdrant(coleccion: str) -> set:
    """hash_archivo ya presentes en la colección, recorriendo el payload con
    scroll. Sólo se usa con --solo-faltantes: en una reindexación completa no
    hace falta y es una vuelta de más."""
    vistos = set()
    offset = None
    client = _qdrant()
    while True:
        puntos, offset = client.scroll(
            collection_name=coleccion, limit=1000, offset=offset,
            with_payload=["metadata"], with_vectors=False,
        )
        for p in puntos:
            h = ((p.payload or {}).get("metadata") or {}).get("hash_archivo")
            if h:
                vistos.add(h)
        if offset is None:
            return vistos


def _upsert(puntos: list, coleccion: str, tam: int, intentos: int = 4) -> None:
    """Escribe los puntos en tandas chicas, con reintentos y backoff.

    Un vector de 1536 floats pesa ~30 KB en JSON: mandar 250 de una es un
    request de varios MB que a Qdrant le puede tomar más que el timeout del
    cliente (pasó: `httpx.WriteTimeout`). Se parte en tandas y cada tanda se
    reintenta — un timeout puntual no puede tirar abajo una reindexación de
    miles de chunks a la mitad.

    wait=False: no se espera a que Qdrant termine de indexar cada tanda, sólo a
    que la acepte. La búsqueda las ve unos segundos después; a cambio la
    escritura no se frena esperando el índice.
    """
    for i in range(0, len(puntos), tam):
        tanda = puntos[i:i + tam]
        for intento in range(1, intentos + 1):
            try:
                _qdrant().upsert(collection_name=coleccion, points=tanda, wait=False)
                break
            except Exception as e:
                if intento == intentos:
                    raise
                espera = 2 ** intento
                log.warning("upsert falló (%s), reintento %d en %ds",
                            type(e).__name__, intento, espera)
                time.sleep(espera)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coleccion", default=config.QDRANT_COLLECTION_CVS)
    ap.add_argument("--lote", type=int, default=25,
                    help="CVs por lote de embeddings")
    ap.add_argument("--upsert-batch", type=int, default=64,
                    help="puntos por request a Qdrant (bajalo si da timeout)")
    ap.add_argument("--limit", type=int, default=0, help="cortar después de N CVs")
    ap.add_argument("--dry-run", action="store_true",
                    help="cuenta CVs y chunks, no llama a OpenAI ni escribe")
    ap.add_argument("--solo-faltantes", action="store_true",
                    help="saltear los hash que ya están en la colección")
    args = ap.parse_args()

    client = _qdrant()
    if not args.dry_run:
        ensure_collection(args.coleccion)

    try:
        antes = client.count(args.coleccion, exact=True).count
    except Exception:
        log.warning("no pude contar los puntos actuales (Qdrant lento); sigo igual")
        antes = 0
    log.info("colección %r: %d puntos antes", args.coleccion, antes)

    saltear = set()
    if args.solo_faltantes and antes:
        saltear = hashes_en_qdrant(args.coleccion)
        log.info("%d CVs ya indexados (se saltean)", len(saltear))

    total = chunks_total = saltados = sin_texto = 0
    buffer: list[tuple] = []

    def _flush():
        """Un embed y un upsert por lote."""
        nonlocal chunks_total
        if not buffer:
            return
        textos = [c for _, _, chs in buffer for c in chs]
        if args.dry_run:
            chunks_total += len(textos)
            buffer.clear()
            return
        vectores = embed(textos)
        puntos, i = [], 0
        for hash_archivo, meta, chs in buffer:
            for idx, chunk in enumerate(chs):
                puntos.append(qmodels.PointStruct(
                    id=_point_id(hash_archivo, idx),
                    vector=vectores[i],
                    payload={"content": chunk,
                             "metadata": {**meta, "hash_archivo": hash_archivo,
                                          "chunk_index": idx}},
                ))
                i += 1
        _upsert(puntos, args.coleccion, args.upsert_batch)
        chunks_total += len(puntos)
        buffer.clear()

    for fila in leer_cvs(args.lote * 4):
        _id, hash_archivo, texto, candidato_id, nombre, apellido, email = fila
        if args.limit and total >= args.limit:
            break
        if hash_archivo in saltear:
            saltados += 1
            continue
        chs = chunk_text(texto)
        if not chs:
            sin_texto += 1
            continue
        total += 1
        buffer.append((
            hash_archivo,
            {"candidato_id": candidato_id, "nombre": nombre, "apellido": apellido,
             "email": email, "fuente": "reindex"},
            chs,
        ))
        if len(buffer) >= args.lote:
            _flush()
            log.info("%d CVs… %d chunks", total, chunks_total)
    _flush()

    despues = antes  # en dry-run no se escribió nada: el conteo no cambia
    if not args.dry_run:
        try:
            despues = client.count(args.coleccion, exact=True).count
        except Exception:
            log.exception("no pude contar los puntos después de indexar")
            despues = -1
    log.info(
        "listo%s: %d CVs indexados (%d chunks), %d salteados, %d sin texto. "
        "Puntos en %r: %d → %d",
        " [dry-run]" if args.dry_run else "", total, chunks_total, saltados,
        sin_texto, args.coleccion, antes, despues,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
