"""
Análisis de CV con Claude. Mismo esquema/prompt que el nodo n8n
"Analyze document1" + "Parsear respuesta LLM".

Para PDF: se manda el archivo entero (base64) a Claude/OpenAI -- la propia
API lo extrae (mejor calidad que pdfplumber, especialmente en CVs con
tablas/columnas/escaneos), tal cual se hacia en el workflow n8n original.
Para docx/doc/txt: esas APIs no aceptan el binario como documento, se sigue
mandando el texto ya extraido localmente (extract.py).
"""
import base64
import json
import logging
import re

from anthropic import Anthropic
from openai import OpenAI

from app.config import config

log = logging.getLogger("llm")

# Un cliente por cuenta de Anthropic (crearlo es barato pero no gratis, y en
# la ingesta en lote se pasa por acá una vez por CV).
_clients: dict[str, Anthropic] = {}
_openai_client: OpenAI | None = None

# Cuentas que ya devolvieron "credit balance is too low" en este proceso. Sin
# esto, cada CV siguiente vuelve a pagar el viaje de ida y vuelta a una cuenta
# que ya sabemos que está seca: en una corrida de miles de mails son miles de
# llamadas al pedo. Es un set de Python (add/in son atómicos), no hace falta
# lock aunque la ingesta corra con varios hilos.
_sin_credito_ya: set[str] = set()


def _get_client(api_key: str) -> Anthropic:
    cliente = _clients.get(api_key)
    if cliente is None:
        cliente = _clients[api_key] = Anthropic(api_key=api_key)
    return cliente


def _es_falta_de_credito(e: Exception) -> bool:
    """Distingue "esta cuenta se quedó sin plata" de un error pasajero (un
    timeout, un 529). Solo el primero justifica descartar la cuenta para el
    resto del proceso; lo otro puede andar en el CV siguiente."""
    msg = str(e).lower()
    return "credit balance" in msg or "billing" in msg


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


SCHEMA_PROMPT = """Estructura:

{
  "datos_personales": {
    "nombre": null, "apellido": null, "dni": null, "fecha_nacimiento": null,
    "localidad": null, "domicilio": null, "estado_civil": null, "sexo": null,
    "nacionalidad": null, "hijos": null, "incapacidad": null, "telefono": null,
    "email": null, "red_social": null
  },
  "situacion_laboral": {
    "ocupacion": null, "movilidad_propia": null, "licencias": [],
    "disponibilidad_viajar": null, "disponibilidad_cambio_residencia": null
  },
  "experiencia_laboral": [
    {"empresa": null, "puesto": null, "fecha_inicio": null, "fecha_finalizacion": null, "descripcion": null}
  ],
  "formacion_academica": [],
  "idiomas": [],
  "tecnologias": [],
  "redes_sociales": [],
  "referencias": {"comerciales": [], "laborales": [], "personales": []},
  "informe_personal": {"resumen": null}
}


Extrae los datos del CV y devuelve SOLO un JSON válido
El JSON debe estar en una sola línea o con formato compacto.

Devuelve SOLO el JSON válido, sin envolverlo en comillas, sin escapar caracteres, sin código de bloque, sin backticks, sin comillas escapadas, sin saltos de línea explícitos.
El JSON debe ser válido y parseable directamente."""


def _clean_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```$", "", raw).strip()
    return raw


def _es_pdf(cv_adjunto: dict) -> bool:
    return (cv_adjunto or {}).get("mime_type") == "application/pdf"


def _analizar_cv_claude(cv_adjunto: dict, texto_cv: str, api_key: str) -> str:
    client = _get_client(api_key)
    if _es_pdf(cv_adjunto):
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(cv_adjunto["data"]).decode("utf-8"),
                },
            },
            {"type": "text", "text": SCHEMA_PROMPT},
        ]
    else:
        content = f"{SCHEMA_PROMPT}\n\nCV:\n{texto_cv[:12000]}"
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8000,
        temperature=0,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text


def _analizar_cv_openai(cv_adjunto: dict, texto_cv: str) -> str:
    client = _get_openai_client()
    if _es_pdf(cv_adjunto):
        b64 = base64.standard_b64encode(cv_adjunto["data"]).decode("utf-8")
        content = [
            {
                "type": "file",
                "file": {
                    "filename": cv_adjunto.get("filename", "cv.pdf"),
                    "file_data": f"data:application/pdf;base64,{b64}",
                },
            },
            {"type": "text", "text": SCHEMA_PROMPT},
        ]
    else:
        content = f"{SCHEMA_PROMPT}\n\nCV:\n{texto_cv[:12000]}"
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=8000,
        temperature=0,
        messages=[{"role": "user", "content": content}],
    )
    return resp.choices[0].message.content


def analizar_cv(cv_adjunto: dict, texto_cv: str) -> dict:
    """Un solo intento por email: se prueban las cuentas de Anthropic en orden
    (config.anthropic_keys) y, si ninguna responde, un unico fallback a OpenAI.
    Si ese tambien falla (o el JSON no parsea), se devuelve {"error": ...} en
    vez de reintentar -- el llamador (nodes.py) cierra el mensaje en el primer
    fallo, no lo vuelve a poner en cola.

    Varias cuentas de Anthropic: cuando la primera se queda sin creditos se
    sigue con la segunda en vez de degradar todo el lote al modelo de OpenAI.
    Una cuenta que contesta "credit balance is too low" se descarta para lo que
    queda del proceso (_sin_credito_ya), asi el resto de los CVs no vuelve a
    pagar ese viaje.

    Si el adjunto es PDF, se manda el archivo entero (cv_adjunto["data"]);
    para el resto de formatos se manda texto_cv (ya extraido localmente)."""
    raw = None
    ultimo_error: Exception | None = None
    for n, api_key in enumerate(config.anthropic_keys, start=1):
        if api_key in _sin_credito_ya:
            continue
        try:
            raw = _analizar_cv_claude(cv_adjunto, texto_cv, api_key)
            break
        except Exception as e:
            ultimo_error = e
            if _es_falta_de_credito(e):
                _sin_credito_ya.add(api_key)
                log.warning("cuenta Anthropic #%d sin créditos, se descarta por el resto de la corrida", n)
            else:
                log.warning("cuenta Anthropic #%d falló: %s", n, e)

    if raw is None:
        log.warning("ninguna cuenta de Anthropic respondió (%s), fallback a OpenAI", ultimo_error)
        try:
            raw = _analizar_cv_openai(cv_adjunto, texto_cv)
        except Exception as e2:
            log.error("OpenAI también falló analizando CV: %s", e2)
            return {"error": "llm_failed", "detail": str(e2)}

    raw = _clean_json(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("no se pudo parsear respuesta del LLM (primeros 500 chars): %s", raw[:500])
        return {"error": "parse_failed", "raw": raw}
