"""
Cliente Gmail API. Reusa la credencial OAuth2 que ya tiene autorizada n8n
para seleccion@everwear.com.ar (mismo client_id/secret, mismo refresh_token
o uno nuevo emitido para esa misma app OAuth -- ver README).

Equivalente a los nodos n8n: "Recibir Mensaje" (getAll por label), "Obtener
Archivos" (get + adjuntos), "Marcar Como Leido", "Agregar/Remove label",
"Delete a message", "Send email".
"""
import base64
import email.utils
import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import config

log = logging.getLogger("gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


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
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _addr(header_value: str) -> str:
    name, address = email.utils.parseaddr(header_value or "")
    return (address or "").lower().strip()


def _name(header_value: str) -> str:
    name, _address = email.utils.parseaddr(header_value or "")
    return name or ""


def _header(headers: list[dict], key: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == key.lower():
            return h.get("value", "")
    return ""


def _walk_parts(payload: dict):
    if not payload:
        return
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def _extract_body_text(payload: dict) -> str:
    for part in _walk_parts(payload):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    # fallback: primer text/html si no hay texto plano
    for part in _walk_parts(payload):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    return ""


def list_labels() -> list[dict]:
    """id + nombre de todos los labels del buzon (para verificar LABEL_QUEUE /
    LABEL_CV_PROCESADO en app/constants.py contra los IDs reales)."""
    svc = _service()
    resp = svc.users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"]} for l in resp.get("labels", [])]


def list_queue(label_id: str, max_results: int = 5) -> list[str]:
    """IDs de mensajes con el label 'cola' (equivalente a nodo 'Recibir Mensaje')."""
    svc = _service()
    resp = svc.users().messages().list(
        userId="me", labelIds=[label_id], maxResults=max_results
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


def get_message(message_id: str, download_attachments: bool = False) -> dict:
    svc = _service()
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = msg.get("payload", {}).get("headers", [])
    from_header = _header(headers, "From")
    reply_to_header = _header(headers, "Reply-To")
    label_ids = msg.get("labelIds", []) or []

    parsed = {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from_address": _addr(from_header),
        "from_name": _name(from_header),
        "reply_to_address": _addr(reply_to_header) if reply_to_header else "",
        "subject": _header(headers, "Subject"),
        "snippet": msg.get("snippet", ""),
        "label_ids": label_ids,
        "is_sent": "SENT" in label_ids,
        "body_text": _extract_body_text(msg.get("payload", {})),
        "attachments": [],
    }
    if download_attachments:
        parsed["attachments"] = _download_attachments(svc, message_id, msg.get("payload", {}))
    return parsed


def _download_attachments(svc, message_id: str, payload: dict) -> list[dict]:
    out = []
    for part in _walk_parts(payload):
        filename = part.get("filename")
        body = part.get("body", {})
        if not filename or not body.get("attachmentId"):
            continue
        att = svc.users().messages().attachments().get(
            userId="me", messageId=message_id, id=body["attachmentId"]
        ).execute()
        data = att.get("data", "")
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        out.append({
            "filename": filename,
            "mime_type": part.get("mimeType", "application/octet-stream"),
            "size": len(raw),
            "data": raw,
        })
    return out


def mark_as_read(message_id: str) -> None:
    _service().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def add_labels(message_id: str, label_ids: list[str]) -> None:
    _service().users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": label_ids}
    ).execute()


def remove_labels(message_id: str, label_ids: list[str]) -> None:
    _service().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": label_ids}
    ).execute()


def delete_message(message_id: str) -> None:
    _service().users().messages().trash(userId="me", id=message_id).execute()


def send_email(
    to_address: str, subject: str, html_body: str, from_address: str | None = None,
    attachments: list[dict] | None = None,
) -> None:
    """attachments: [{"filename": ..., "data": bytes}] (opcional)."""
    msg = MIMEMultipart("mixed")
    msg["To"] = to_address
    msg["From"] = from_address or config.RRHH_EMAIL
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    for att in attachments or []:
        part = MIMEApplication(att["data"], Name=att["filename"])
        part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    _service().users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("mail enviado a %s: %s%s", to_address, subject, f" (+{len(attachments)} adjunto/s)" if attachments else "")
