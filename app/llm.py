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

_client: Anthropic | None = None
_openai_client: OpenAI | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_KEY)
    return _client


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


def _analizar_cv_claude(cv_adjunto: dict, texto_cv: str) -> str:
    client = _get_client()
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
    """Un solo intento por email: Claude, y si falla, un unico fallback a
    OpenAI. Si ese tambien falla (o el JSON no parsea), se devuelve
    {"error": ...} en vez de reintentar -- el llamador (nodes.py) cierra el
    mensaje en el primer fallo, no lo vuelve a poner en cola.

    Si el adjunto es PDF, se manda el archivo entero (cv_adjunto["data"]);
    para el resto de formatos se manda texto_cv (ya extraido localmente)."""
    try:
        raw = _analizar_cv_claude(cv_adjunto, texto_cv)
    except Exception as e:
        log.warning("Claude falló (¿sin créditos/tokens?), fallback a OpenAI: %s", e)
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
