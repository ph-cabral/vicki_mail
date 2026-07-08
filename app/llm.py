"""
Análisis de CV con Claude. Mismo esquema/prompt que el nodo n8n
"Analyze document1" + "Parsear respuesta LLM", adaptado para recibir texto ya
extraído (en vez de mandar el binario) — funciona igual para pdf/doc/docx/txt.
"""
import json
import logging
import re

from anthropic import Anthropic

from app.config import config

log = logging.getLogger("llm")

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_KEY)
    return _client


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


def analizar_cv(texto_cv: str) -> dict:
    client = _get_client()
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8000,
        temperature=0,
        messages=[{
            "role": "user",
            "content": f"{SCHEMA_PROMPT}\n\nCV:\n{texto_cv[:12000]}",
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("no se pudo parsear respuesta de Claude (primeros 500 chars): %s", raw[:500])
        return {"error": "parse_failed", "raw": raw}
