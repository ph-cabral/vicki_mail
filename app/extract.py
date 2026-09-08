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
    MIME_POR_EXTENSION,
    MIMES_IMAGEN,
    MIN_CHARS_TEXTO_VALIDO,
    PALABRAS_POSTULACION,
    PALABRAS_PROHIBIDAS_IMAGEN,
    PALABRAS_PROHIBIDAS_NOMBRE,
    TAMANIO_MIN_IMAGEN_CV,
)

log = logging.getLogger("extract")


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower()


def nombre_valido(filename: str) -> bool:
    lower = _normalizar(filename or "")
    return not any(p in lower for p in PALABRAS_PROHIBIDAS_NOMBRE)


def mime_normalizado(filename: str, mime_type: str) -> str:
    """Mime real del adjunto, resuelto por la extensión del nombre.

    El mimeType que devuelve Gmail es el que puso el cliente que mandó el mail
    y no es confiable: hay clientes (y celulares) que mandan el PDF como
    `application/octet-stream`, `application/x-pdf` o hasta `text/plain`. Si el
    nombre termina en una extensión conocida, esa manda; si no, se deja el mime
    tal como vino."""
    ext = os.path.splitext(filename or "")[1].lower()
    return MIME_POR_EXTENSION.get(ext, mime_type or "")


def es_imagen(mime_type: str) -> bool:
    return mime_type in MIMES_IMAGEN


def _imagen_es_cv(a: dict, mime: str) -> bool:
    """Descarta la firma del mail y los logos incrustados, que llegan como
    adjuntos igual que la foto del CV. Dos filtros baratos antes de gastar una
    llamada al LLM: el nombre típico y el peso (una firma pesa unos pocos KB,
    la foto de una hoja pesa cientos)."""
    lower = _normalizar(a.get("filename", ""))
    if any(p in lower for p in PALABRAS_PROHIBIDAS_IMAGEN):
        return False
    return len(a.get("data") or b"") >= TAMANIO_MIN_IMAGEN_CV


def filtrar_adjunto_cv(attachments: list[dict]) -> dict | None:
    """Primer adjunto que sea un CV válido por extensión + nombre. None si no hay.

    Devuelve una COPIA con `mime_type` ya normalizado, así todo lo que sigue
    (extracción, conversión a PDF, store, base) trabaja con el mime real y no
    con el que vino en el mail."""
    for a in attachments:
        mime = mime_normalizado(a.get("filename", ""), a.get("mime_type", ""))
        if not nombre_valido(a.get("filename", "")):
            continue
        if mime in EXTENSIONES_PERMITIDAS:
            return {**a, "mime_type": mime}
        if es_imagen(mime) and _imagen_es_cv(a, mime):
            return {**a, "mime_type": mime}
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


def imagen_a_pdf(data: bytes) -> bytes | None:
    """Foto del CV -> PDF de una página, para que siga el mismo camino que un
    PDF nativo: se le manda entero a Claude/OpenAI (que lo leen con visión) y
    pdftoppm le saca la miniatura.

    img2pdf embebe el JPEG/PNG tal cual, sin recomprimir y sin reencodear: no
    degrada la imagen, que es justo de lo que depende que el modelo la lea.
    Devuelve None si el archivo está corrupto o el formato no se puede embeber
    (ej. un PNG con canal alfa) -- el llamador descarta el adjunto."""
    try:
        import img2pdf
    except ImportError:
        log.warning("img2pdf no está instalado: no se pueden procesar CVs en imagen")
        return None
    try:
        return img2pdf.convert(data)
    except Exception as e:
        log.info("img2pdf directo falló (%s), reintento aplanando con Pillow", type(e).__name__)
    # img2pdf rechaza lo que no puede embeber sin tocar (PNG con canal alfa,
    # paletas raras, gif animado). Pillow lo aplana a RGB y recién ahí se
    # embebe. Pillow ya viene con img2pdf, no es una dependencia nueva.
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            fondo = Image.new("RGB", im.size, (255, 255, 255))
            im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
            if im.mode == "RGBA":
                fondo.paste(im, mask=im.split()[3])
            else:
                fondo.paste(im)
            buf = io.BytesIO()
            fondo.save(buf, format="JPEG", quality=92)
        return img2pdf.convert(buf.getvalue())
    except Exception as e:
        log.warning("no se pudo pasar la imagen a PDF: %s: %s", type(e).__name__, e)
        return None


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
