"""
Chunking + embeddings + upsert a Qdrant. Reemplaza los nodos n8n
"Recursive Character Text Splitter" + "Embeddings OpenAI" + "Qdrant Vector
Store" (mismas dos colecciones: "cvs" para CVs, "documentos" para notas de
reunión Fireflies/Read AI — mismo modelo de embeddings que usa vicki_chat,
text-embedding-3-small, para que la búsqueda de vicki_chat siga funcionando
sobre estos mismos vectores).

IDs de punto determinísticos (hash_archivo + índice de chunk): si el mismo
CV se reenvía, se actualiza en vez de duplicarse.
"""
import logging
import uuid

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.config import config

log = logging.getLogger("qdrant_store")

_openai_client: OpenAI | None = None
_qdrant_client: QdrantClient | None = None

VECTOR_SIZE = 1536  # text-embedding-3-small


def _openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


def _qdrant() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY or None)
    return _qdrant_client


def chunk_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[str]:
    """Split por bloques con solape, cortando en salto de línea/espacio
    cercano — equivalente aproximado al RecursiveCharacterTextSplitter."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            corte = text.rfind("\n", start, end)
            if corte <= start:
                corte = text.rfind(" ", start, end)
            if corte > start:
                end = corte
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        next_start = end - chunk_overlap
        start = next_start if next_start > start else end
    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    resp = _openai().embeddings.create(model=config.EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def _point_id(hash_archivo: str, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{hash_archivo}:{chunk_index}"))


def ensure_collection(collection: str) -> None:
    try:
        client = _qdrant()
        existentes = [c.name for c in client.get_collections().collections]
        if collection not in existentes:
            client.create_collection(
                collection_name=collection,
                vectors_config=qmodels.VectorParams(size=VECTOR_SIZE, distance=qmodels.Distance.COSINE),
            )
            log.info("colección Qdrant '%s' creada", collection)
    except Exception:
        log.exception("no se pudo verificar/crear la colección '%s' (¿ya existe con otra config?)", collection)


def upsert_documento(collection: str, texto: str, hash_archivo: str, metadata: dict) -> int:
    chunks = chunk_text(texto)
    if not chunks:
        log.warning("texto vacío, nada para indexar en Qdrant (hash=%s)", hash_archivo[:12])
        return 0
    ensure_collection(collection)
    vectors = embed(chunks)
    points = [
        qmodels.PointStruct(
            id=_point_id(hash_archivo, i),
            vector=vector,
            payload={
                "content": chunk,
                "metadata": {**metadata, "hash_archivo": hash_archivo, "chunk_index": i},
            },
        )
        for i, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    _qdrant().upsert(collection_name=collection, points=points)
    log.info("qdrant upsert: %d chunks en '%s' (hash=%s)", len(points), collection, hash_archivo[:12])
    return len(points)
