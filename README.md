# vicki_mail

Port a Python/LangGraph del workflow n8n **"Ingesta y respuesta Email"**
(buzón `seleccion@everwear.com.ar`). Mismo servidor que `vicki_chat`,
`hikvsion_get_events`, `ever` (red Docker `ai-net`), pero servicio propio:
este flujo es un *poller* (revisa el correo cada N segundos), no un
request/response como `vicki_chat`.

## Qué hace

1. Cada `POLL_INTERVAL_SECONDS` (default 60), busca mensajes que sigan en el
   inbox (`gmail_client.list_inbox`, `q=in:inbox -label:sent -label:chats`).
   Ya no depende del label "cola" (`LABEL_QUEUE`) que ponía un filtro de
   Gmail configurado aparte en la cuenta (patrón heredado de n8n) — ese
   filtro no se podía confirmar que etiquetara también las respuestas dentro
   de un hilo ya existente, así que se sacó esa dependencia: cualquier
   mensaje que siga en INBOX se toma como no procesado, porque cada rama del
   grafo lo saca de INBOX (o lo borra) al terminar.
2. Por cada mensaje, corre el grafo (`app/graph.py`):
   - **Remitente interno** (`@everwear.com.ar`, no `rrhh@`) → responde
     recordatorio y **borra** el mensaje (irreversible).
   - **Es una respuesta propia** (label `SENT`) → se ignora/archiva.
   - **Read AI / Fireflies** → busca el resumen en la carpeta Drive fija de
     cada integración, lo extrae, lo guarda en Postgres + Qdrant
     (colección `documentos`) y mueve el archivo a la carpeta "procesado".
   - **Resto** → se trata como postulación:
     - sin adjunto válido → responde "esto es solo para CVs".
     - adjunto es imagen/escaneo (texto extraído casi vacío) → responde
       pidiendo Word/PDF real.
     - CV válido → extrae texto → Claude estructura los datos → matchea
       contra `rag_system.candidato` (por DNI > email > teléfono > nombre
       normalizado) → upsert candidato + `documento_aprobado` → upsert en
       Qdrant (colección `cvs`) → responde "postulación recibida" (nuevo) o
       "ya registrado, no mejora tus probabilidades" (ya existía).
3. Al terminar, saca el mensaje de la cola (remueve label + INBOX) y lo
   marca como leído.

## Setup

### 1. Credenciales Google (Gmail + Drive)

No se puede extraer el refresh_token de n8n directamente (vive cifrado en
su base interna). Se reusa el **mismo Client ID/Secret OAuth** que n8n ya
tiene autorizado (Google Cloud Console → APIs & Services → Credentials) y
se emite un token nuevo para este servicio:

```bash
pip install google-auth-oauthlib
GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/get_refresh_token.py
```

Se abre el navegador, loguearse con `seleccion@everwear.com.ar`, aceptar. El
script imprime `GOOGLE_REFRESH_TOKEN=...` → pegarlo en `.env`.

### 2. `.env`

Copiar `.env.example` → `.env` y completar: `DATABASE_URL` (mismo
`n8n_sql`), `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`, `ANTHROPIC_KEY`,
`OPENAI_API_KEY` (mismo modelo de embeddings que usa `vicki_chat`, para que
la colección `cvs` sea compatible con su buscador).

### 3. Levantar

```bash
docker compose up -d --build
```

Contenedor `vicki-mail`, puerto host `8089`. `/health` para chequear,
`/process_now` (POST) para forzar un ciclo sin esperar al scheduler.

## Pendientes / a confirmar (no bloquean, pero revisar antes de prod)

- **IDs de labels y carpetas Drive** (`app/constants.py`): extraídos del
  JSON del workflow n8n original. Si es la misma cuenta de Google siguen
  siendo válidos, pero conviene confirmarlos (Gmail → Configuración →
  Etiquetas; Drive → abrir la carpeta → ID en la URL).
- **`LABEL_ALT_PROCESADO`**: apareció en el JSON en una rama que no se pudo
  identificar con certeza. No se usa por ahora.
- **Plantilla "solo se usa para CVs"** (`solo_recepcion_cv` en
  `app/email_templates.py`): el workflow n8n no tenía un nodo con este
  texto exacto — se escribió nueva, mismo tono/firma que el resto. Revisar
  el copy.
- **`Send email3`** ("Acabo de actualizar tus datos en la base") existe en
  el JSON original pero no se pudo determinar en qué rama se disparaba
  distinto de `Send email4`. Por ahora el flujo usa `Send email4`
  ("ya registrado, no mejora tus probabilidades") siempre que el candidato
  ya existía — `actualizado_en_base()` queda escrita pero sin usar.
- **Email de notificación de Fireflies/Read AI**: no quedó claro en el JSON
  si el mensaje original se borraba o se archivaba tras procesar el
  resumen de Drive. Por ahora se archiva (se saca de la cola, no se
  marca "cv procesado", no se borra).
- **`.doc` viejo** (Word 97-2003): se extrae con `antiword` (instalado en
  el Dockerfile). Si llega un `.doc` con formato raro puede fallar —
  revisar logs.
- **Umbral "imagen/escaneo"** (`MIN_CHARS_TEXTO_VALIDO` en
  `app/constants.py`, default 40 caracteres): heurística simple. Si da
  falsos positivos/negativos, ajustar.
- **Remitentes vistos en el JSON pero fuera de alcance** (no
  implementados, se loguean y se ignoran si llegan):
  `no-reply@transkriptor.com`, `amiclaboral@gmail.com`.
- Nada de esto se probó contra el buzón real (sin credenciales ni acceso de
  red desde este entorno) — antes de producción, correr `/process_now`
  contra un mensaje de prueba y revisar los logs.
- **`LABEL_CV_PROCESADO`**: usá `GET /labels` para confirmar que el ID
  corresponde al label correcto en el buzón real (no es legible solo mirando
  el JSON de n8n). `LABEL_QUEUE` ya no hace falta confirmarlo — quedó sin uso
  en el descubrimiento de mensajes (ver arriba), solo se sigue removiendo por
  las dudas en `nodes.py:_cerrar` si un mensaje todavía lo tuviera puesto de
  cuando existía el filtro viejo.
- **Filtro de Gmail viejo (label "cola")**: si sigue activo en la cuenta, no
  rompe nada (el mensaje simplemente además tiene ese label, que `_cerrar` lo
  saca igual), pero conviene desactivarlo o dejarlo — ya no es necesario para
  que `vicki_mail` funcione.
- **Archivo original del CV**: se sube a Drive (carpeta `DRIVE_FOLDER_CV_ARCHIVE`,
  "ceve" en el workflow original) después de persistir en Postgres/Qdrant —
  si falla la subida, solo se loguea, no bloquea la respuesta al candidato.
- **Plantilla de CV al rechazar una foto**: se adjunta `DRIVE_TEMPLATE_CV_FILENAME`
  (bajada por ID de Drive) al mail de "no procesamos fotos" — si falla la
  descarga, el mail sale igual pero sin adjunto (se loguea).

## Mapeo con el workflow n8n original

| n8n | vicki_mail |
|---|---|
| Gmail Trigger + Schedule Trigger(s) | `app/main.py` — `procesar_cola()` cada `POLL_INTERVAL_SECONDS` |
| Recibir Mensaje / Obtener Archivos | `app/gmail_client.py` |
| Switch1/2/6 (routing por remitente) | `nodes.router_email` |
| Filtrar Por Extenciones Permitidas2 / detector extencion2 | `app/extract.py` |
| Extraer texto DOCX (mammoth) / PDF / doc | `app/extract.py::extraer_texto` |
| Detecta binario, calcula hash2 | `app/extract.py::calcular_hash` |
| Analyze document1 (Claude) + Parsear respuesta LLM | `app/llm.py::analizar_cv` |
| Generar texto_limpio | `app/db.py::construir_texto_limpio` |
| UPSERT candidato | `app/db.py::upsert_candidato` |
| UPSERT/INSERT documento_aprobado | `app/db.py::upsert_documento_cv` / `insert_documento_meeting` |
| Qdrant Vector Store + Embeddings OpenAI + Text Splitter | `app/qdrant_store.py` |
| Read Ai / Fireflies (list + export + move) | `app/drive_client.py`, `nodes.meeting_notes_node` |
| Send email / email1 / email2 / email3 / email4 / email5 | `app/email_templates.py` |
| Agregar/Remove label, Marcar Como Leido, Delete a message | `nodes._cerrar`, `gmail_client.py` |
| Upload file / Upload file1 (archivar CV en Drive) | `drive_client.upload_file`, `nodes.persist_cv_node` |
| Download file (plantilla base de CV) | `drive_client.download_file`, `nodes.reply_imagen_node` |
# vicki_mail
