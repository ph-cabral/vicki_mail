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
import re
import unicodedata
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import config

log = logging.getLogger("gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Labels que Gmail crea solo: su ID ES el nombre, no hay que resolverlos.
LABELS_SISTEMA = {
    "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "UNREAD", "STARRED", "IMPORTANT",
    "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


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


# El directorio de labels del buzon, cacheado en memoria: se pide una vez y se
# refresca solo cuando algo no matchea o cuando creamos un label. Sin esto
# habria una llamada extra a la API por cada mensaje procesado.
_labels_cacheados: list[dict] | None = None
# Labels configurados que no existen en el buzon: se loguean UNA vez y despues
# se ignoran en silencio (y sin volver a pedir el directorio).
_labels_invalidos: set[str] = set()


def _labels(refrescar: bool = False) -> list[dict]:
    global _labels_cacheados
    if _labels_cacheados is None or refrescar:
        _labels_cacheados = list_labels()
    return _labels_cacheados


def crear_label(nombre: str) -> str | None:
    """Crea un label visible con ese nombre y devuelve su ID. None si Gmail lo
    rechaza (nombre invalido, ya existe con otra forma, sin permisos)."""
    try:
        creado = _service().users().labels().create(
            userId="me",
            body={"name": nombre, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
    except Exception:
        log.exception("no se pudo crear el label %r", nombre)
        return None
    _labels(refrescar=True)
    log.info("label %r creado (id=%s)", nombre, creado.get("id"))
    return creado.get("id")


def _buscar_label(valor: str, labels: list[dict]) -> str | None:
    for l in labels:
        if l["id"] == valor:
            return l["id"]
    buscado = _normalizar_label(valor.removeprefix("label:"))
    for l in labels:
        if _normalizar_label(l["name"]) == buscado:
            return l["id"]
    return None


def resolver_label(valor: str, crear: bool = False) -> str | None:
    """ID real de un label a partir de lo que haya configurado: un ID
    (Label_1234...), el nombre visible, o la forma 'label:...' de la busqueda.
    None si en el buzon no existe nada que matchee.

    Existe porque un ID viejo/borrado hace que Gmail rechace la llamada entera
    con 400 'labelId not found'. Resolviendo primero, un label que ya no esta
    se descarta y el resto de la operacion sigue: nunca puede impedir que el
    mensaje salga de INBOX (ver nodes._cerrar)."""
    if not valor or valor in _labels_invalidos:
        return None
    if valor in LABELS_SISTEMA:
        return valor
    encontrado = _buscar_label(valor, _labels())
    if encontrado is None:
        # el label puede haberse creado despues de cachear el directorio
        encontrado = _buscar_label(valor, _labels(refrescar=True))
    if encontrado:
        return encontrado
    if crear and not valor.startswith("Label_"):
        # vino un nombre, no un ID: se puede crear tal cual
        creado = crear_label(valor)
        if creado:
            return creado
    _labels_invalidos.add(valor)
    log.error(
        "el label %r no existe en el buzon (%s): se ignora. Corregir "
        "LABEL_* en .env con el ID/nombre real -- GET /labels los lista.",
        valor, config.GMAIL_USER,
    )
    return None


def _resolver_varios(valores: list[str], crear: bool = False) -> list[str]:
    """IDs reales de una lista de labels, sin repetidos y salteando los que no
    existen."""
    ids: list[str] = []
    for valor in valores or []:
        real = resolver_label(valor, crear=crear)
        if real and real not in ids:
            ids.append(real)
    return ids


def list_queue(label_id: str, max_results: int = 5) -> list[str]:
    """IDs de mensajes con el label 'cola' (equivalente a nodo 'Recibir Mensaje').
    Ya no se usa para descubrir mensajes nuevos (ver list_inbox) -- se deja
    disponible como fallback/debug si hiciera falta volver al esquema viejo
    basado en un filtro de Gmail externo."""
    svc = _service()
    resp = svc.users().messages().list(
        userId="me", labelIds=[label_id], maxResults=max_results
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


def _normalizar_label(nombre: str) -> str:
    """'Seleccion y Reclutamiento/CV no procesado' -> 'seleccion-y-reclutamiento-cv-no-procesado'.

    Es la misma forma en que Gmail escribe el label en la barra de busqueda
    (`label:...`): minusculas, sin acentos, y espacios y barras de anidado como
    guiones. Sirve para aceptar el label tal como se lo copia de la UI, sin
    tener que averiguar su ID interno (Label_1234...)."""
    texto = unicodedata.normalize("NFD", nombre or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"-+", "-", re.sub(r"[\s/_]+", "-", texto)).strip("-")


def label_id_por_nombre(nombre: str) -> str | None:
    """ID interno del label a partir de su nombre visible (o de la forma
    'label:...' de la busqueda). None si no hay ningun label que matchee.

    A diferencia de antes, un valor con forma de ID (Label_...) tambien se
    verifica contra el buzon en vez de devolverse tal cual: un ID que ya no
    existe devuelve None aca y no una llamada rechazada mas adelante."""
    return resolver_label(nombre)


def iter_messages_por_label(label_id: str, page_size: int = 500):
    """IDs de TODOS los mensajes con un label, paginando (la API devuelve como
    maximo 500 por pagina). Es un generador: no arma la lista entera en memoria
    y permite cortar antes (--limit).

    Filtra del lado del servidor por labelIds y no por query de texto: Gmail
    resuelve el label por indice, no hay que traer y descartar mensajes."""
    svc = _service()
    page_token = None
    while True:
        resp = svc.users().messages().list(
            userId="me", labelIds=[label_id],
            maxResults=min(page_size, 500), pageToken=page_token,
        ).execute()
        for m in resp.get("messages", []) or []:
            yield m["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def list_inbox(max_results: int = 5) -> list[str]:
    """IDs de mensajes en la bandeja de entrada, mas viejo primero.

    Reemplaza a list_queue() como mecanismo de descubrimiento: en vez de
    depender de que un filtro de Gmail configurado aparte en la cuenta le
    ponga el label 'cola' a lo que llega (y de que ese filtro tambien
    etiquete las respuestas dentro de un hilo ya existente, algo que no se
    podia confirmar sin acceso a la cuenta real), esto lee el inbox
    directo -- cualquier mensaje que siga en INBOX es, por definicion, algo
    que todavia no se proceso (cada rama del grafo saca el mensaje de INBOX
    al terminar, ver nodes.py:_cerrar / delete_message / _reenviar_a_rrhh).

    Se excluyen los mensajes que enviamos nosotros mismos (label SENT) --
    ademas del chequeo anti-loop que ya hace router_email, evita gastar una
    llamada de API en algo que se va a ignorar de todas formas."""
    svc = _service()
    resp = svc.users().messages().list(
        userId="me", q="in:inbox -label:sent -label:chats", maxResults=max_results
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    return list(reversed(ids))  # la API devuelve mas nuevo primero; procesamos FIFO


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


def mark_important(message_id: str) -> None:
    _service().users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": ["IMPORTANT"]}
    ).execute()


def thread_has_sent_message(thread_id: str, exclude_message_id: str = "") -> bool:
    """True si el hilo ya tiene un mensaje nuestro (label SENT) distinto del
    actual -- sirve para detectar que ya respondimos antes en este hilo y
    evitar repetir la respuesta automatica cada vez que el remitente
    contesta (esa repeticion fue el origen del loop de 'Recordatorio!!!')."""
    if not thread_id:
        return False
    svc = _service()
    thread = svc.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
    for m in thread.get("messages", []) or []:
        if m.get("id") == exclude_message_id:
            continue
        if "SENT" in (m.get("labelIds") or []):
            return True
    return False


def add_labels(message_id: str, label_ids: list[str], crear: bool = False) -> None:
    """Aplica labels resolviendolos antes: los que no existen se descartan (con
    `crear=True`, un valor que sea un nombre se crea). Si no queda ninguno no se
    llama a la API."""
    ids = _resolver_varios(label_ids, crear=crear)
    if not ids:
        return
    _service().users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": ids}
    ).execute()


def remove_labels(message_id: str, label_ids: list[str]) -> None:
    ids = _resolver_varios(label_ids)
    if not ids:
        return
    _service().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ids}
    ).execute()


def archivar(message_id: str, quitar_labels: list[str] | None = None) -> None:
    """Saca el mensaje de INBOX y lo marca leido en UNA sola llamada (y de paso
    le quita los labels extra que se pidan, ej. la 'cola').

    Es el paso que decide si el mensaje se vuelve a procesar: mientras siga en
    INBOX, list_inbox() lo devuelve en cada poll y el grafo le responde de
    nuevo al remitente. Por eso va solo, sin depender de ningun otro label."""
    ids = _resolver_varios(["INBOX", "UNREAD", *(quitar_labels or [])])
    if not ids:
        return
    _service().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ids}
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
