"""
Nodos del grafo. Cada funcion recibe el EmailState y devuelve un dict con
las claves que actualiza (estilo LangGraph). El estado ya llega con el
mensaje + adjuntos descargados (ver main.py: gmail_client.get_message con
download_attachments=True).
"""
import hashlib
import logging

from app import cv_store, drive_client, gmail_client
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
    LABEL_REVISAR,
    LABEL_REVISAR_INTERNO,
    LABEL_REVISAR_NO_PROCESADO,
    LABEL_REVISAR_SIN_CV,
    SENDER_FIREFLIES,
    SENDER_MEDICINA_LABORAL,
    SENDER_READAI,
    SENDER_TRANSKRIPTOR,
)
from app.db import (
    construir_texto_limpio,
    insert_documento_meeting,
    marcar_archivo,
    upsert_candidato,
    upsert_documento_cv,
)
from app.email_templates import (
    foto_no_procesada,
    postulacion_recibida,
    recordatorio_uso_interno,
    solo_recepcion_cv,
    ya_registrado,
)
from app.extract import (
    EXTENSION_POR_MIME_CONVERTIBLE,
    calcular_hash,
    convertir_a_pdf,
    es_imagen,
    es_imagen_o_escaneo,
    extraer_texto,
    filtrar_adjunto_cv,
    imagen_a_pdf,
)
from app.graph_state import EmailState
from app.llm import analizar_cv, perfil_sin_persona
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
    # El etiquetado va en su PROPIO try y antes del archivado, pero no lo
    # condiciona: cuando los dos colgaban del mismo try, un LABEL_CV_PROCESADO
    # que ya no existia en el buzon (400 'labelId not found') cortaba la
    # funcion antes de sacar el mensaje de INBOX -> el poll de cada 2 minutos
    # lo volvia a tomar y le reenviaba la respuesta al postulante una y otra
    # vez. Etiquetar es cosmetico; archivar es lo que cierra el ciclo.
    if aplicar_label_procesado:
        try:
            gmail_client.add_labels(message_id, [LABEL_CV_PROCESADO], crear=True)
        except Exception:
            log.exception("no se pudo etiquetar como procesado el mensaje %s (se archiva igual)", message_id)
    try:
        gmail_client.archivar(message_id, quitar_labels=[LABEL_QUEUE])
    except Exception:
        log.exception("no se pudo archivar el mensaje %s (queda en INBOX)", message_id)


def _derivar_a_revision(state: EmailState, motivo: str) -> None:
    """Lo que el flujo automatico no resuelve (hilo ya respondido, mensaje sin
    CV, o la IA fallo su unico intento) se deja en el propio buzon de
    seleccion@ ETIQUETADO para revision manual.

    Reemplaza al reenvio por mail a `config.RRHH_INTERNAL_CONTACT`
    (2026-09-21): cada caso derivado generaba un mail a la casilla de RRHH y
    el volumen terminaba siendo ruido. Ahora no sale ningun mail: queda todo
    junto y buscable en el buzon de seleccion, que es donde ya estaba.

    Se aplican DOS etiquetas: la padre `LABEL_REVISAR` (la vista con todo) y
    la del `motivo` (sub-etiqueta). La padre se aplica explicita en vez de
    confiar en el anidado implicito de Gmail al crear "Padre/Hijo": asi la
    vista "todo junto" existe siempre, sin depender de como la UI interprete
    la barra.

    El original NUNCA se borra. Antes, los casos sin adjunto se mandaban a la
    papelera porque el reenvio ya llevaba el texto completo del mensaje; sin
    ese reenvio el mensaje ES el registro, asi que borrarlo seria perder el
    caso.

    Etiquetar y archivar van en `try` separados, mismo criterio que `_cerrar`:
    si Gmail rechaza una etiqueta, el mensaje tiene que salir de INBOX igual o
    el poll lo vuelve a tomar en la proxima corrida."""
    message_id = state.get("message_id")
    if not message_id:
        return
    try:
        gmail_client.add_labels(message_id, [LABEL_REVISAR, motivo], crear=True)
    except Exception:
        log.exception("no se pudo etiquetar para revision el mensaje %s (se archiva igual)", message_id)
    try:
        gmail_client.archivar(message_id, quitar_labels=[LABEL_QUEUE])
    except Exception:
        log.exception(
            "no se pudo archivar el mensaje %s tras derivarlo a revision (queda en INBOX)",
            message_id,
        )


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
    """cv_adjunto (el archivo ORIGINAL) nunca se toca -- se archiva/persiste
    tal cual mas adelante. Si es doc/docx, se intenta primero convertirlo a
    PDF (contingencia 1, mejor fidelidad) para mandarselo entero a la IA via
    cv_para_ia; si la conversion falla, contingencia 2: se sigue con el
    texto extraido localmente (mammoth/antiword), como antes."""
    cv = state["cv_adjunto"]
    mime_type = cv["mime_type"]
    data = cv["data"]
    hash_archivo = calcular_hash(data)

    if es_imagen(mime_type):
        # foto del CV: no hay texto que extraer. Se pasa a PDF (img2pdf, sin
        # recomprimir) y se le manda entera al modelo, que la lee con vision.
        # Si no se pudo armar el PDF, se trata como antes: se le pide al
        # candidato que lo reenvie en texto.
        pdf_bytes = imagen_a_pdf(data)
        if not pdf_bytes:
            return {"route": "imagen", "hash_archivo": hash_archivo}
        return {
            "route": "texto_ok",
            "texto_cv": "",
            "hash_archivo": hash_archivo,
            "cv_para_ia": {"mime_type": "application/pdf", "data": pdf_bytes,
                           "filename": cv.get("filename", "cv.pdf")},
        }

    extension = EXTENSION_POR_MIME_CONVERTIBLE.get(mime_type)
    if extension:
        pdf_bytes = convertir_a_pdf(data, extension)
        if pdf_bytes:
            texto = extraer_texto("application/pdf", pdf_bytes)
            if es_imagen_o_escaneo(texto):
                return {"route": "imagen", "hash_archivo": hash_archivo}
            return {
                "route": "texto_ok",
                "texto_cv": texto,
                "hash_archivo": hash_archivo,
                "cv_para_ia": {"mime_type": "application/pdf", "data": pdf_bytes, "filename": cv.get("filename", "cv.pdf")},
            }
        log.warning("conversion a PDF fallo para %s, contingencia: texto local", cv.get("filename"))

    texto = extraer_texto(mime_type, data)
    if es_imagen_o_escaneo(texto):
        return {"route": "imagen", "hash_archivo": hash_archivo}
    return {"route": "texto_ok", "texto_cv": texto, "hash_archivo": hash_archivo}


def analyze_cv_node(state: EmailState) -> dict:
    perfil = analizar_cv(state.get("cv_para_ia") or state["cv_adjunto"], state["texto_cv"])
    if perfil.get("error"):
        return {"route": "error_llm", "perfil": perfil}
    if perfil_sin_persona(perfil):
        # el modelo no identifico a nadie en el archivo (una foto que result
        # ser la firma del mail, una hoja ilegible). Cargarlo dejaria un
        # candidato sin nombre en la base y en la shortlist: se trata igual
        # que una imagen ilegible y se le pide el CV en texto.
        log.info("el LLM no saco ningun dato del adjunto, se trata como imagen ilegible")
        return {"route": "imagen"}
    try:
        texto_limpio = construir_texto_limpio(perfil)
    except Exception as e:
        # el JSON del LLM parseo bien pero algun campo vino con una forma
        # inesperada (ej. formacion_academica con strings sueltos en vez de
        # objetos) -- no dejar que esto crashee el grafo entero y deje el
        # mensaje reintentando en loop infinito (eso paso: mismo mensaje
        # fallando cada 2 min, gastando llamadas a OpenAI sin nunca
        # resolverse). Se trata igual que un fallo de analisis: se reenvia
        # a RRHH para carga manual y se cierra.
        log.error("construir_texto_limpio fallo con perfil bien formado (%s): %s", type(e).__name__, e)
        return {"route": "error_llm", "perfil": {"error": "texto_limpio_failed", "detail": str(e), "perfil_crudo": perfil}}
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
            collection=config.QDRANT_COLLECTION_POSTULANTES,
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

    # Copia local del archivo: original + PDF normalizado + miniatura de la
    # primera pagina. Es lo que despues sirve la barra de CVs del chat, sin
    # pegarle a Drive en cada miniatura. Va DESPUES del upsert porque marca
    # columnas de la fila recien escrita.
    try:
        para_ia = state.get("cv_para_ia") or {}
        res = cv_store.guardar(
            state["hash_archivo"], cv["data"], cv["mime_type"],
            pdf_data=para_ia.get("data") if para_ia.get("mime_type") == "application/pdf" else None,
        )
        marcar_archivo(state["hash_archivo"], local=True, pdf=res["pdf"], thumb=res["thumb"])
    except Exception:
        log.exception("no se pudo guardar el CV en el store local (candidato_id=%s)", candidato.get("id"))

    try:
        subido = drive_client.upload_file(cv["data"], cv["filename"], DRIVE_FOLDER_CV_ARCHIVE, cv["mime_type"])
        # el id de Drive antes se descartaba: sin el no habia forma de volver
        # al archivo desde la base.
        marcar_archivo(state["hash_archivo"], drive_file_id=subido.get("id"))
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
    """Sin CV adjunto. Revertido 2026-09-16 al comportamiento del workflow n8n
    original (el cambio de 2026-07-20 derivaba todo sin avisarle al remitente:
    la mayoria de la gente que escribe sin adjuntar responde despues
    corrigiendo, y esas respuestas se perdian en la pila de derivaciones
    genericas sin ningun contexto).

    - Primera vez en el hilo (`thread_has_sent_message` da False): se le pide
      el formato correcto (`solo_recepcion_cv`) y se cierra SIN derivar
      todavia -- la mayoria corrige solo con esto.
    - Ya se le habia pedido en este mismo hilo y volvio a escribir sin CV: se
      etiqueta para revision manual bajo `LABEL_REVISAR_SIN_CV`, que es lo que
      distingue el reintento fallido del resto. El mensaje queda en el buzon
      (antes se borraba, porque el reenvio a RRHH se llevaba el texto)."""
    if gmail_client.thread_has_sent_message(state.get("thread_id", ""), state.get("message_id", "")):
        _derivar_a_revision(state, LABEL_REVISAR_SIN_CV)
        return {"accion_final": "revision_sin_cv"}
    subject, html = solo_recepcion_cv(state.get("from_name") or "")
    gmail_client.send_email(state["from_address"], subject, html)
    _cerrar(state)
    return {"accion_final": "sin_cv_avisado"}


def delete_and_notice_node(state: EmailState) -> dict:
    """Remitente interno (@everwear.com.ar, no rrhh): se responde con el
    recordatorio y se BORRA el mensaje (irreversible, gmail delete real --
    mismo comportamiento que el nodo 'Delete a message' de n8n)."""
    if gmail_client.thread_has_sent_message(state.get("thread_id", ""), state.get("message_id", "")):
        # ya se mando el recordatorio antes en este hilo (esto es lo que
        # generaba el loop de "Recordatorio!!!") -> no volver a
        # responder/borrar aca: se etiqueta para revision y se archiva.
        _derivar_a_revision(state, LABEL_REVISAR_INTERNO)
        return {"accion_final": "revision_interno"}
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
    """Un solo intento por email: si la IA no devolvio un JSON parseable
    (ni Claude ni el fallback a OpenAI), no se reintenta en el proximo poll --
    el CV queda etiquetado bajo `LABEL_REVISAR_NO_PROCESADO` para carga
    manual. El adjunto original sigue en el mensaje, en el buzon: no hace
    falta reenviarlo a ningun lado para que RRHH lo pueda bajar."""
    log.error("fallo de analisis LLM en mensaje %s: %s", state.get("message_id"), state.get("perfil"))
    _derivar_a_revision(state, LABEL_REVISAR_NO_PROCESADO)
    return {"accion_final": "revision_no_procesado"}


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
