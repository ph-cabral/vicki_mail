from typing import Optional, TypedDict


class EmailState(TypedDict, total=False):
    # datos crudos del mensaje (ya vienen resueltos por gmail_client.get_message)
    message_id: str
    thread_id: str
    from_address: str
    from_name: str
    reply_to_address: str
    subject: str
    snippet: str
    body_text: str
    label_ids: list[str]
    is_sent: bool
    attachments: list[dict]  # [{filename, mime_type, size, data: bytes}]

    # ruteo — cada nodo de decisión escribe acá por qué rama sigue
    route: Optional[str]

    # rama CV
    cv_adjunto: Optional[dict]
    texto_cv: Optional[str]
    hash_archivo: Optional[str]
    perfil: Optional[dict]
    texto_limpio: Optional[str]
    candidato: Optional[dict]

    # telemetría / debug
    accion_final: Optional[str]
