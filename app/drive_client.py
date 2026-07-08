"""
Cliente Google Drive API. Reusa la misma credencial OAuth2 que gmail_client.

Equivalente a los nodos n8n: "Read Ai"/"Fireflies" (list por carpeta),
"HTTP Request" export a docx, "mover a Fireflies"/"Mover a Read AI" (move).
"""
import logging
from functools import lru_cache

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

from app.config import config

log = logging.getLogger("drive")

SCOPES = ["https://www.googleapis.com/auth/drive"]

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@lru_cache(maxsize=1)
def _service():
    creds = Credentials(
        token=None,
        refresh_token=config.GOOGLE_REFRESH_TOKEN,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(folder_id: str) -> list[dict]:
    svc = _service()
    resp = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType)",
    ).execute()
    return resp.get("files", [])


def export_as_docx(file_id: str) -> bytes:
    """Exporta un Google Doc como .docx (equivalente al HTTP Request /export del n8n)."""
    svc = _service()
    request = svc.files().export_media(fileId=file_id, mimeType=DOCX_MIME)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def move_file(file_id: str, destination_folder_id: str) -> None:
    svc = _service()
    file = svc.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file.get("parents", []))
    svc.files().update(
        fileId=file_id,
        addParents=destination_folder_id,
        removeParents=previous_parents,
        fields="id,parents",
    ).execute()
    log.info("archivo %s movido a carpeta %s", file_id, destination_folder_id)
