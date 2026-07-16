"""
Filtrado de adjuntos, extracción de texto (pdf/doc/docx/txt) y detección de
"CV que en realidad es una imagen" (foto/escaneo sin texto seleccionable).

Equivalente a los nodos n8n: "Filtrar Por Extenciones Permitidas2",
"detector extencion2", "Extraer texto DOCX" (mammoth), "Detecta binario,
calcula hash2", "Detector de CV1".
"""
import hashlib
import logging
import os
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


EXTENSION_POR_MIME_CONVERTIBLE = {
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def convertir_a_pdf(data: bytes, extension: str, timeout: int = 60) -> bytes | None:
    """Convierte doc/docx a PDF via LibreOffice headless -- preserva tablas,
    columnas, headers/footers (mejor fidelidad que python-docx/antiword, que
    solo sacan parrafos de texto plano). Se usa como paso previo para poder
    mandarle el archivo entero a Claude/OpenAI igual que un PDF.

    Devuelve None si la conversion falla (archivo corrupto, timeout,
    LibreOffice no instalado, etc.) -- el llamador debe caer como
    contingencia a la extraccion de texto local (mammoth/antiword)."""
    with tempfile.TemporaryDirectory() as tmp:
        origen = os.path.join(tmp, f"in{extension}")
        with open(origen, "wb") as f:
            f.write(data)
        perfil = os.path.join(tmp, "perfil_lo")
        try:
            subprocess.run(
                [
                    "soffice", "--headless", "--norestore",
                    f"-env:UserInstallation=file://{perfil}",
                    "--convert-to", "pdf", "--outdir", tmp, origen,
                ],
                capture_output=True, timeout=timeout, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("no se pudo convertir a PDF con LibreOffice: %s", e)
            return None
        salida = os.path.join(tmp, "in.pdf")
        if not os.path.exists(salida):
            log.warning("LibreOffice no genero el PDF esperado en %s", salida)
            return None
        with open(salida, "rb") as f:
            return f.read()
