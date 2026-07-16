"""
Nodos del grafo. Cada funcion recibe el EmailState y devuelve un dict con
las claves que actualiza (estilo LangGraph). El estado ya llega con el
mensaje + adjuntos descargados (ver main.py: gmail_client.get_message con
download_attachments=True).
"""
import hashlib
import logging

from app import drive_client, gmail_client
from app.config import config
from app.constants import (
    DRIVE_FOLDER_CV_ARCHIVE,
    DRIVE_FOLDER_FIREFLIES_DONE,
    DRIVE_FOLDER_FIREFLIES_SRC,
    DRIVE_FOLDER_READAI_DONE,
    DRIVE_FOLDER_READAI_SRC,
    DRIVE_TEMPLATE_CV_FILE_ID,
    DRIVE_TEMPLATE_CV_FILENAME,
    LABEL_CV_PROCESADO,
    LABEL_QUEUE,
    SENDER_FIREFLIES,
    SENDER_MEDICINA_LABORAL,
    SENDER_READAI,
    SENDER_TRANSKRIPTOR,
)
from app.db import construir_texto_limpio, insert_documento_meeting, upsert_candidato, upsert_documento_cv
from app.email_templates import (
    foto_no_procesada,
    postulacion_recibida,
    recordatorio_uso_interno,
    solo_recepcion_cv,
    ya_registrado,
)
from app.extract import calcular_hash, es_imagen_o_escaneo, extraer_texto, filtrar_adjunto_cv
from app.graph_state import EmailState
from app.llm import analizar_cv
from app.qdrant_store import upsert_documento

log = logging.getLogger("nodes")


def _cerrar(state: EmailState, aplicar_label_procesado: bool = True) -> None:
    """Deja el mensaje fuera de la 'cola' (remueve LABEL_QUEUE + INBOX),
    opcionalmente marca 'cv procesado', y lo marca como leido. Equivalente a
    los nodos 'Remove label from message' + 'Agregar Etiqueta cv procesado' +
    'Marcar Como Leido' del workflow n8n."""
    message_id = state.get("message_id")
    if not message_id:
        return
    try:
        if aplicar_label_procesado:
            gmail_client.add_labels(message_id, [LABEL_CV_PROCESADO])
        gmail_client.remove_labels(message_id, [LABEL_QUEUE, "INBOX"])
        gmail_client.mark_as_read(message_id)
    except Exception:
        log.exception("no se pudo finalizar/etiquetar el mensaje %s", message_id)


def _reenviar_a_rrhh(state: EmailState) -> None:
    """Caso 'ya le respondimos antes en este hilo y volvio a escribir': ya
    no es para nuestro flujo automatico, se reenvia tal cual a RRHH interno
    (asunto fijo "nos escribieron a seleccion") y se elimina el original
    del buzon de seleccion (evita que siga dando vueltas / se re-procese)."""
    message_id = state.get("message_id")
    if not message_id:
        return
    remitente = state.get("from_name") or state.get("from_address") or "desconocido"
    cuerpo_original = (state.get("body_text") or "").strip() or "(sin contenido)"
    html = (
        f"<p>Mensaje reenviado desde {config.GMAIL_USER}.</p>"
        f"<p><b>De:</b> {remitente} &lt;{state.get('from_address', '')}&gt;<br>"
        f"<b>Asunto original:</b> {state.get('subject', '')}</p>"
        f"<hr>"
        f"<p>{cuerpo_original.replace(chr(10), '<br>')}</p>"
    )
    attachments = [
        {"filename": a["filename"], "data": a["data"]}
        for a in (state.get("attachments") or [])
    ] or None
    try:
        gmail_client.send_email(
            config.RRHH_INTERNAL_CONTACT, "nos escribieron a seleccion", html,
            attachments=attachments,
        )
    except Exception:
        log.exception("no se pudo reenviar a RRHH el mensaje %s", message_id)
        return
    try:
        gmail_client.delete_message(message_id)
    except Exception:
        log.exception("no se pudo eliminar el mensaje %s tras reenviarlo a RRHH", message_id)


def _nombre_destinatario(state: EmailState) -> str:
    candidato = state.get("candidato") or {}
    nombre = " ".join(x for x in [candidato.get("nombre"), candidato.get("apellido")] if x).strip()
    return nombre or state.get("from_name") or ""


# -- router ------------------------------------------------------------------

def router_email(state: EmailState) -> dict:
    from_addr = (state.get("from_address") or "").lower()
    reply_to = (state.get("reply_to_address") or "").lower()
    label_ids = state.get("label_ids") or []

    # anti-loop: mensajes propios (SENT) o de/hacia el propio buzon/RRHH se
    # ignoran ANTES de evaluar es_interno, para no re-responder una respuesta
    # nuestra que haya vuelto a entrar al hilo (bucle infinito).
    if "SENT" in label_ids:
        return {"route": "ignorar"}

    propias_addr = {config.GMAIL_USER.lower(), config.RRHH_EMAIL.lower()}
    if from_addr in propias_addr:
        return {"route": "ignorar"}

    candidatos_addr = [a for a in [reply_to, from_addr] if a]
    es_interno = (
        any(a.endswith(f"@{config.INTERNAL_DOMAIN}") for a in candidatos_addr)
        and not (propias_addr & set(candidatos_addr))
    )
    if es_interno:
        return {"route": "interno"}

    if from_addr == SENDER_READAI:
        return {"route": "readai"}
    if from_addr == SENDER_FIREFLIES:
        return {"route": "fireflies"}
    if from_addr in (SENDER_TRANSKRIPTOR, SENDER_MEDICINA_LABORAL):
        log.info("remitente %s fuera del alcance implementado por ahora, se ignora", from_addr)
        return {"route": "ignorar"}

    return {"route": "candidato"}


# -- rama candidato: adjuntos -> texto -> LLM -> match -> persistencia ------

def check_attachments(state: EmailState) -> dict:
    cv = filtrar_adjunto_cv(state.get("attachments", []) or [])
    if cv is None:
        return {"route": "sin_cv"}
    return {"route": "con_cv", "cv_adjunto": cv}


def extract_text_node(state: EmailState) -> dict:
    cv = state["cv_adjunto"]
    data = cv["data"]
    hash_archivo = calcular_hash(data)
    texto = extraer_texto(cv["mime_type"], data)
    if es_imagen_o_escaneo(texto):
        return {"route": "imagen", "hash_archivo": hash_archivo}
    return {"route": "texto_ok", "texto_cv": texto, "hash_archivo": hash_archivo}


def analyze_cv_node(state: EmailState) -> dict:
    perfil = analizar_cv(state["texto_cv"])
    if perfil.get("error"):
        return {"route": "error_llm", "perfil": perfil}
    texto_limpio = construir_texto_limpio(perfil)
    return {"route": "ok", "perfil": perfil, "texto_limpio": texto_limpio}


def match_candidato_node(state: EmailState) -> dict:
    candidato = upsert_candidato(state["perfil"])
    route = "nuevo" if candidato["accion"] == "inserted" else "existente"
    return {"candidato": candidato, "route": route}


def persist_cv_node(state: EmailState) -> dict:
    """Escribe/actualiza el CV en Postgres (rag_system.documento_aprobado) y
    en Qdrant (coleccion 'cvs'), tanto si el candidato es nuevo como si ya
    estaba registrado -- segun lo pedido: siempre se reemplazan los datos.
    Tambien archiva el CV original en Drive (equivalente a los nodos
    'Upload file'/'Upload file1' del workflow n8n)."""
    cv = state["cv_adjunto"]
    candidato = state["candidato"]
    try:
        upsert_documento_cv(
            hash_archivo=state["hash_archivo"],
            nombre_archivo=cv["filename"],
            texto_limpio=state["texto_limpio"],
            perfil=state["perfil"],
            candidato_id=candidato["id"],
            mime_type=cv["mime_type"],
            tamanio_bytes=cv["size"],
            texto_raw=state["texto_cv"],
            email_id=state.get("message_id", ""),
            accion=candidato["accion"],
        )
        upsert_documento(
            collection=config.QDRANT_COLLECTION_CVS,
            texto=state["texto_limpio"],
            hash_archivo=state["hash_archivo"],
            metadata={
                "candidato_id": candidato["id"],
                "nombre": candidato.get("nombre"),
                "apellido": candidato.get("apellido"),
                "email": candidato.get("email"),
                "fuente": "email",
            },
        )
    except Exception:
        log.exception("error persistiendo CV (candidato_id=%s)", candidato.get("id"))

    try:
        drive_client.upload_file(cv["data"], cv["filename"], DRIVE_FOLDER_CV_ARCHIVE, cv["mime_type"])
    except Exception:
        log.exception("no se pudo archivar el CV original en Drive (candidato_id=%s)", candidato.get("id"))

    return {}


# -- respuestas + cierre ------------------------------------------------------

def reply_nuevo_node(state: EmailState) -> dict:
    subject, html = postulacion_recibida(_nombre_destinatario(state))
    gmail_client.send_email(state["from_address"], subject, html)
    _cerrar(state)
    return {"accion_final": "cv_nuevo"}


def reply_existente_node(state: EmailState) -> dict:
    subject, html = ya_registrado(_nombre_destinatario(state))
    gmail_client.send_email(state["from_address"], subject, html)
    _cerrar(state)
    return {"accion_final": "cv_existente"}


def reply_imagen_node(state: EmailState) -> dict:
    """Foto/escaneo, no texto real: se responde pidiendo el formato correcto
    y se adjunta la plantilla base de CV (equivalente al nodo 'Download
    file' del workflow n8n, que bajaba 'CV para postulantes.docx')."""
    subject, html = foto_no_procesada(state.get("from_name") or "")
    attachments = []
    try:
        data = drive_client.download_file(DRIVE_TEMPLATE_CV_FILE_ID)
        attachments.append({"filename": DRIVE_TEMPLATE_CV_FILENAME, "data": data})
    except Exception:
        log.exception("no se pudo descargar la plantilla base de CV desde Drive, se manda sin adjunto")
    gmail_client.send_email(state["from_address"], subject, html, attachments=attachments)
    _cerrar(state)
    return {"accion_final": "imagen_rechazada"}


def reply_sin_cv_node(state: EmailState) -> dict:
    if gmail_client.thread_has_sent_message(state.get("thread_id", ""), state.get("message_id", "")):
        # ya le contestamos "esto es solo para CVs" antes en este hilo y
        # volvio a escribir sin adjuntar CV: no es otro intento fallido, es
        # una respuesta humana real (ej. propuesta comercial, pregunta) ->
        # se reenvia a RRHH y se elimina, sin repetir la plantilla.
        _reenviar_a_rrhh(state)
        return {"accion_final": "reenviado_rrhh"}
    subject, html = solo_recepcion_cv(state.get("from_name") or "")
    gmail_client.send_email(state["from_address"], subject, html)
    _cerrar(state)
    return {"accion_final": "sin_cv"}


def delete_and_notice_node(state: EmailState) -> dict:
    """Remitente interno (@everwear.com.ar, no rrhh): se responde con el
    recordatorio y se BORRA el mensaje (irreversible, gmail delete real --
    mismo comportamiento que el nodo 'Delete a message' de n8n)."""
    if gmail_client.thread_has_sent_message(state.get("thread_id", ""), state.get("message_id", "")):
        # ya se mando el recordatorio antes en este hilo (esto es lo que
        # generaba el loop de "Recordatorio!!!") -> no volver a
        # responder/borrar acá, se reenvia a RRHH y se elimina.
        _reenviar_a_rrhh(state)
        return {"accion_final": "reenviado_rrhh"}
    subject, html = recordatorio_uso_interno()
    gmail_client.send_email(state["from_address"], subject, html)
    gmail_client.delete_message(state["message_id"])
    return {"accion_final": "interno_eliminado"}


def ignore_node(state: EmailState) -> dict:
    """Respuestas propias (label SENT) o remitentes fuera de alcance: se
    archiva sin responder."""
    _cerrar(state, aplicar_label_procesado=False)
    return {"accion_final": "ignorado"}


def error_node(state: EmailState) -> dict:
    """El LLM no devolvio un JSON parseable. Se deja el mensaje SIN marcar
    como procesado (sigue en la cola) para poder revisarlo/reintentar."""
    log.error("fallo de analisis LLM en mensaje %s: %s", state.get("message_id"), state.get("perfil"))
    return {"accion_final": "error_llm"}


# -- rama notas de reunion (Fireflies / Read AI) -----------------------------

def meeting_notes_node(state: EmailState) -> dict:
    origen = "Read AI" if state.get("route") == "readai" else "Fireflies"
    src_folder = DRIVE_FOLDER_READAI_SRC if origen == "Read AI" else DRIVE_FOLDER_FIREFLIES_SRC
    dest_folder = DRIVE_FOLDER_READAI_DONE if origen == "Read AI" else DRIVE_FOLDER_FIREFLIES_DONE

    try:
        archivos = drive_client.list_folder(src_folder)
    except Exception:
        log.exception("no se pudo listar carpeta Drive de %s", origen)
        archivos = []

    for f in archivos:
        try:
            data = drive_client.export_as_docx(f["id"])
            texto = extraer_texto(drive_client.DOCX_MIME, data)
            hash_logico = hashlib.sha256(texto.encode("utf-8")).hexdigest()
            insert_documento_meeting(
                hash_archivo=hash_logico,
                nombre_archivo=f.get("name", "sin_nombre.docx"),
                texto_limpio=texto,
                mime_type=drive_client.DOCX_MIME,
                tamanio_bytes=len(data),
                texto_raw=texto,
                origen=origen,
            )
            upsert_documento(
                collection=config.QDRANT_COLLECTION_DOCS,
                texto=texto,
                hash_archivo=hash_logico,
                metadata={"origen": origen, "nombre_archivo": f.get("name")},
            )
            drive_client.move_file(f["id"], dest_folder)
        except Exception:
            log.exception("error procesando archivo %s de %s", f.get("id"), origen)

    # el mail de notificacion (de read.ai / fireflies) se archiva igual que
    # el resto -- no se pudo confirmar en el JSON si el original lo borraba
    # en vez de archivarlo, ver README.
    _cerrar(state, aplicar_label_procesado=False)
    return {"accion_final": f"meeting_notes_{origen.lower().replace(' ', '')}"}
