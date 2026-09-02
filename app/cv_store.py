"""Almacen local del archivo del CV (original + PDF normalizado + miniatura).

Por que existe: el CV se archivaba solo en Drive y ni siquiera se guardaba el
id que devuelve la subida, asi que no habia forma de ir de un candidato a su
archivo. Para mostrar el CV en el chat hace falta servirlo rapido y sin pasar
por Google en cada vista (cada apertura y cada miniatura seria una llamada a
Drive: latencia + cuota). Drive queda como archivo/respaldo; el origen de la
vista es este store.

Layout (CV_STORE_DIR, por default /data/cv_store):

    <hash[:2]>/<hash>/original.<ext>
    <hash[:2]>/<hash>/doc.pdf
    <hash[:2]>/<hash>/thumb.jpg

`hash` es el sha256 del archivo original (`extract.calcular_hash`), el mismo
que ya es UNIQUE en rag_system.documento_aprobado -> no hace falta guardar la
ruta en la base, se deriva. El primer nivel de 2 chars evita un directorio con
decenas de miles de entradas.

Escribe vicki_mail (ingesta y backfill); vicki_chat lo monta de solo lectura.
"""
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("cv_store")

CV_STORE_DIR = os.getenv("CV_STORE_DIR", "/data/cv_store")

EXTENSION_POR_MIME = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}

# Ancho de la miniatura en px. La barra del chat la muestra a ~150px de ancho;
# 300 alcanza para pantallas retina sin que el archivo pese.
THUMB_ANCHO = 300


def dir_hash(hash_archivo: str) -> str:
    return os.path.join(CV_STORE_DIR, hash_archivo[:2], hash_archivo)


def ruta_pdf(hash_archivo: str) -> str:
    return os.path.join(dir_hash(hash_archivo), "doc.pdf")


def ruta_thumb(hash_archivo: str) -> str:
    return os.path.join(dir_hash(hash_archivo), "thumb.jpg")


def ruta_original(hash_archivo: str, mime_type: str = "") -> str:
    ext = EXTENSION_POR_MIME.get(mime_type, "")
    if ext:
        return os.path.join(dir_hash(hash_archivo), f"original{ext}")
    # sin mime conocido: buscar el que este
    d = dir_hash(hash_archivo)
    try:
        for n in os.listdir(d):
            if n.startswith("original"):
                return os.path.join(d, n)
    except OSError:
        pass
    return os.path.join(d, "original")


def _generar_thumb(pdf_path: str, destino: str) -> bool:
    """Primera pagina del PDF a JPG con pdftoppm (poppler-utils).

    pdftoppm y no una libreria de Python a proposito: ya esta en la imagen por
    poppler, corre en un proceso aparte (un PDF corrupto no voltea la ingesta)
    y renderiza una sola pagina, asi que es constante en tiempo por CV.
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "t")
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-r", "50", "-f", "1", "-l", "1",
                 "-scale-to-x", str(THUMB_ANCHO), "-scale-to-y", "-1",
                 "-singlefile", pdf_path, base],
                capture_output=True, timeout=30, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("no se pudo generar la miniatura (%s): %s", type(e).__name__, e)
            return False
        salida = f"{base}.jpg"
        if not os.path.exists(salida):
            return False
        shutil.move(salida, destino)
        return True


def guardar(hash_archivo: str, data: bytes, mime_type: str,
            pdf_data: bytes | None = None) -> dict:
    """Guarda original + PDF + miniatura. Idempotente: reescribe si ya estaba.

    pdf_data: el PDF ya convertido por LibreOffice en extract_text_node
    (`cv_para_ia`). Si el original ya es PDF se usa el original y no se
    convierte nada. Si no hay PDF (un .txt, o la conversion fallo) se guarda
    el original igual y se queda sin miniatura: el modal cae al texto.

    Devuelve {"pdf": bool, "thumb": bool} para registrarlo en la base.
    """
    destino = dir_hash(hash_archivo)
    os.makedirs(destino, exist_ok=True)

    ext = EXTENSION_POR_MIME.get(mime_type, "")
    with open(os.path.join(destino, f"original{ext}"), "wb") as f:
        f.write(data)

    pdf = pdf_data if pdf_data else (data if mime_type == "application/pdf" else None)
    tiene_pdf = False
    tiene_thumb = False
    if pdf:
        pdf_path = ruta_pdf(hash_archivo)
        with open(pdf_path, "wb") as f:
            f.write(pdf)
        tiene_pdf = True
        tiene_thumb = _generar_thumb(pdf_path, ruta_thumb(hash_archivo))

    log.info("cv %s guardado en el store (pdf=%s thumb=%s)", hash_archivo[:12], tiene_pdf, tiene_thumb)
    return {"pdf": tiene_pdf, "thumb": tiene_thumb}
