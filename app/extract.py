"""
Filtrado de adjuntos, extracción de texto (pdf/doc/docx/txt) y detección de
"CV que en realidad es una imagen" (foto/escaneo sin texto seleccionable).

Equivalente a los nodos n8n: "Filtrar Por Extenciones Permitidas2",
"detector extencion2", "Extraer texto DOCX" (mammoth), "Detecta binario,
calcula hash2", "Detector de CV1".
"""
import hashlib
import logging
import subprocess
import tempfile
import unicodedata

import pdfplumber
from docx import Document as DocxDocument

from app.constants import (
    EXTENSIONES_PERMITIDAS,
    MIN_CHARS_TEXTO_VALIDO,
    PALABRAS_POSTULACION,
    PALABRAS_PROHIBIDAS_NOMBRE,
)

log = logging.getLogger("extract")


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower()


def nombre_valido(filename: str) -> bool:
    lower = _normalizar(filename or "")
    return not any(p in lower for p in PALABRAS_PROHIBIDAS_NOMBRE)


def filtrar_adjunto_cv(attachments: list[dict]) -> dict | None:
    """Primer adjunto que sea un CV válido por extensión + nombre. None si no hay."""
    for a in attachments:
        if a.get("mime_type") in EXTENSIONES_PERMITIDAS and nombre_valido(a.get("filename", "")):
            return a
    return None


def detectar_postulacion(asunto: str, cuerpo: str, nombres_adjuntos: str) -> bool:
    texto = _normalizar(f"{asunto or ''} {cuerpo or ''} {nombres_adjuntos or ''}")
    return any(_normalizar(p) in texto for p in PALABRAS_POSTULACION)


def calcular_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extraer_pdf(data: bytes) -> str:
    partes = []
    with pdfplumber.open(tempfile_from_bytes(data)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            partes.append(t)
    return "\n".join(partes).strip()


def tempfile_from_bytes(data: bytes):
    import io
    return io.BytesIO(data)


def _extraer_docx(data: bytes) -> str:
    doc = DocxDocument(tempfile_from_bytes(data))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def _extraer_doc(data: bytes) -> str:
    """.doc viejo (binario) — no hay librería pura Python confiable; se usa antiword."""
    with tempfile.NamedTemporaryFile(suffix=".doc") as f:
        f.write(data)
        f.flush()
        try:
            out = subprocess.run(
                ["antiword", f.name], capture_output=True, timeout=30, check=True
            )
            return out.stdout.decode("utf-8", "replace").strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.warning("no se pudo extraer .doc con antiword: %s", e)
            return ""


def _extraer_txt(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def extraer_texto(mime_type: str, data: bytes) -> str:
    if mime_type == "application/pdf":
        return _extraer_pdf(data)
    if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extraer_docx(data)
    if mime_type == "application/msword":
        return _extraer_doc(data)
    if mime_type == "text/plain":
        return _extraer_txt(data)
    return ""


def es_imagen_o_escaneo(texto: str) -> bool:
    """True si el texto extraído es demasiado corto (foto/escaneo sin texto real)."""
    return len((texto or "").strip()) < MIN_CHARS_TEXTO_VALIDO
