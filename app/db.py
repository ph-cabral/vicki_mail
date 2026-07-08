"""
Capa Postgres — schema rag_system (YA EXISTE, según confirmó el usuario).
Match/upsert de candidato adaptado 1:1 del nodo n8n "UPSERT candidato"
(dni > email > teléfono normalizado > nombre normalizado, en ese orden de
prioridad). Upsert de documento_aprobado para CVs e INSERT simple para notas
de reunión (Fireflies/Read AI), igual que los nodos "UPSERT documento_aprobado"
/ "INSERT documento_aprobado1".
"""
import json
import logging
from functools import lru_cache

from psycopg_pool import ConnectionPool

from app.config import config

log = logging.getLogger("db")

SYSTEM_USER_ID = 1  # aprobado_por / creado_por — mismo valor fijo que usaba el workflow n8n


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    return ConnectionPool(config.DATABASE_URL, min_size=1, max_size=5, open=True)


# ── candidato ────────────────────────────────────────────────────────────────

_UPSERT_CANDIDATO_SQL = """
WITH input_data AS (
  SELECT
    NULLIF(TRIM(%(nombre)s), '') AS p_nombre,
    NULLIF(TRIM(%(apellido)s), '') AS p_apellido,
    NULLIF(TRIM(LOWER(%(email)s)), '') AS p_email,
    NULLIF(REGEXP_REPLACE(
      REGEXP_REPLACE(COALESCE(%(telefono)s, ''), '\\D', '', 'g'),
      '^54?9?', ''
    ), '') AS p_tel_norm,
    NULLIF(TRIM(%(telefono)s), '') AS p_telefono,
    NULLIF(REGEXP_REPLACE(COALESCE(%(dni)s, ''), '\\D', '', 'g'), '') AS p_dni,
    (
      SELECT array_to_string(
        ARRAY(
          SELECT palabra FROM unnest(
            string_to_array(
              LOWER(rag_system.immutable_unaccent(
                TRIM(COALESCE(%(nombre)s, '') || ' ' || COALESCE(%(apellido)s, ''))
              )),
              ' '
            )
          ) AS palabra
          WHERE palabra <> ''
          ORDER BY palabra
        ), ' '
      )
    ) AS p_nombre_norm,
    %(perfil)s::jsonb AS p_perfil
),
matched AS (
  SELECT c.id
  FROM rag_system.candidato c, input_data i
  WHERE
    (i.p_dni IS NOT NULL AND c.dni = i.p_dni)
    OR (i.p_email IS NOT NULL AND LOWER(c.email) = i.p_email)
    OR (i.p_tel_norm IS NOT NULL AND c.telefono_normalizado = i.p_tel_norm)
    OR (i.p_nombre_norm <> '' AND c.nombre_normalizado = i.p_nombre_norm)
  ORDER BY
    CASE
      WHEN c.dni = (SELECT p_dni FROM input_data) THEN 1
      WHEN LOWER(c.email) = (SELECT p_email FROM input_data) THEN 2
      WHEN c.telefono_normalizado = (SELECT p_tel_norm FROM input_data) THEN 3
      ELSE 4
    END
  LIMIT 1
),
updated AS (
  UPDATE rag_system.candidato c
  SET
    email                = COALESCE(c.email, i.p_email),
    telefono             = COALESCE(NULLIF(c.telefono, ''), i.p_telefono),
    telefono_normalizado = COALESCE(c.telefono_normalizado, i.p_tel_norm),
    dni                  = COALESCE(c.dni, i.p_dni),
    nombre               = COALESCE(NULLIF(c.nombre, ''), i.p_nombre),
    apellido             = COALESCE(NULLIF(c.apellido, ''), i.p_apellido),
    nombre_normalizado   = COALESCE(c.nombre_normalizado, i.p_nombre_norm),
    perfil               = c.perfil || i.p_perfil,
    updated_at           = NOW()
  FROM input_data i
  WHERE c.id = (SELECT id FROM matched)
  RETURNING c.id, c.nombre, c.apellido, c.email, c.telefono, c.perfil, 'updated'::text AS accion
),
inserted AS (
  INSERT INTO rag_system.candidato (
    nombre, apellido, email, telefono, telefono_normalizado,
    dni, nombre_normalizado, perfil, fuente_original, hash_candidato
  )
  SELECT
    i.p_nombre, i.p_apellido, i.p_email, i.p_telefono, i.p_tel_norm,
    i.p_dni, i.p_nombre_norm, i.p_perfil, 'cv_ingesta',
    rag_system.calcular_hash_candidato(i.p_nombre, i.p_apellido, i.p_email, i.p_telefono)
  FROM input_data i
  WHERE NOT EXISTS (SELECT 1 FROM matched)
  RETURNING id, nombre, apellido, email, telefono, perfil, 'inserted'::text AS accion
)
SELECT * FROM updated
UNION ALL
SELECT * FROM inserted;
"""


def upsert_candidato(perfil: dict) -> dict:
    """perfil = JSON estructurado que devuelve Claude (analizar_cv).
    Retorna {id, nombre, apellido, email, telefono, perfil, accion} —
    accion = 'inserted' (candidato nuevo) o 'updated' (ya existía)."""
    dp = perfil.get("datos_personales", {}) or {}
    telefono = dp.get("telefono")
    if isinstance(telefono, list):
        telefono = ", ".join(str(t) for t in telefono if t)
    params = {
        "nombre": dp.get("nombre") or "",
        "apellido": dp.get("apellido") or "",
        "email": dp.get("email") or "",
        "telefono": telefono or "",
        "dni": dp.get("dni") or "",
        "perfil": json.dumps(perfil),
    }
    with get_pool().connection() as conn:
        row = conn.execute(_UPSERT_CANDIDATO_SQL, params).fetchone()
        conn.commit()
    if not row:
        raise RuntimeError("upsert_candidato no devolvió fila (revisar función calcular_hash_candidato / immutable_unaccent en rag_system)")
    cols = ["id", "nombre", "apellido", "email", "telefono", "perfil", "accion"]
    return dict(zip(cols, row))


# ── documento_aprobado (CV) ──────────────────────────────────────────────────

_UPSERT_DOC_CV_SQL = """
WITH upserted AS (
  INSERT INTO rag_system.documento_aprobado (
    tipo, dominio, hash_archivo, nombre_archivo, texto_limpio, datos_estructurados,
    candidato_id, metadata, embeddings_generados, indexado_en_qdrant,
    aprobado_por, aprobado_at, mime_type, tamanio_bytes, texto_raw, fuente, creado_por
  )
  VALUES (
    'CV', 'CANDIDATOS',
    %(hash_archivo)s, %(nombre_archivo)s, %(texto_limpio)s, %(datos_estructurados)s::jsonb,
    %(candidato_id)s,
    jsonb_build_object(
      'procesamiento_automatico', true,
      'workflow', 'vicki_mail',
      'email_id', %(email_id)s,
      'accion_candidato', %(accion)s,
      'fecha_procesamiento', NOW()::text
    ),
    false, false, %(system_user)s, NOW(),
    %(mime_type)s, %(tamanio_bytes)s, %(texto_raw)s, 'EMAIL', %(system_user)s
  )
  ON CONFLICT (hash_archivo) DO UPDATE SET
    candidato_id        = EXCLUDED.candidato_id,
    texto_limpio         = EXCLUDED.texto_limpio,
    datos_estructurados  = EXCLUDED.datos_estructurados,
    metadata             = rag_system.documento_aprobado.metadata || EXCLUDED.metadata,
    aprobado_at          = NOW()
  RETURNING id, hash_archivo, (xmax = 0) AS fue_insertado
)
SELECT * FROM upserted;
"""


def upsert_documento_cv(
    hash_archivo: str, nombre_archivo: str, texto_limpio: str, perfil: dict,
    candidato_id: int, mime_type: str, tamanio_bytes: int, texto_raw: str,
    email_id: str, accion: str,
) -> dict:
    params = {
        "hash_archivo": hash_archivo,
        "nombre_archivo": nombre_archivo,
        "texto_limpio": texto_limpio,
        "datos_estructurados": json.dumps(perfil),
        "candidato_id": candidato_id,
        "email_id": email_id,
        "accion": accion,
        "system_user": SYSTEM_USER_ID,
        "mime_type": mime_type,
        "tamanio_bytes": tamanio_bytes,
        "texto_raw": texto_raw,
    }
    with get_pool().connection() as conn:
        row = conn.execute(_UPSERT_DOC_CV_SQL, params).fetchone()
        conn.commit()
    cols = ["id", "hash_archivo", "fue_insertado"]
    return dict(zip(cols, row))


# ── documento_aprobado (notas de reunión: Fireflies / Read AI) ──────────────

_INSERT_DOC_MEETING_SQL = """
INSERT INTO rag_system.documento_aprobado (
  tipo, dominio, hash_archivo, nombre_archivo, texto_limpio, datos_estructurados,
  candidato_id, metadata, embeddings_generados, indexado_en_qdrant,
  aprobado_por, aprobado_at, mime_type, tamanio_bytes, texto_raw, fuente, creado_por
)
VALUES (
  'OTRO', 'CONOCIMIENTO',
  %(hash_archivo)s, %(nombre_archivo)s, %(texto_limpio)s, '{}'::jsonb,
  NULL,
  jsonb_build_object(
    'origen', %(origen)s,
    'plataforma', 'google_drive',
    'google_file', true,
    'procesamiento_automatico', true,
    'workflow', 'vicki_mail',
    'fecha_procesamiento', NOW()::text
  ),
  false, false, %(system_user)s, NOW(),
  %(mime_type)s, %(tamanio_bytes)s, %(texto_raw)s, 'EMAIL', %(system_user)s
)
ON CONFLICT (hash_archivo) DO NOTHING
RETURNING id;
"""


def insert_documento_meeting(
    hash_archivo: str, nombre_archivo: str, texto_limpio: str,
    mime_type: str, tamanio_bytes: int, texto_raw: str, origen: str,
) -> int | None:
    params = {
        "hash_archivo": hash_archivo,
        "nombre_archivo": nombre_archivo,
        "texto_limpio": texto_limpio,
        "origen": origen,
        "system_user": SYSTEM_USER_ID,
        "mime_type": mime_type,
        "tamanio_bytes": tamanio_bytes,
        "texto_raw": texto_raw,
    }
    with get_pool().connection() as conn:
        row = conn.execute(_INSERT_DOC_MEETING_SQL, params).fetchone()
        conn.commit()
    return row[0] if row else None


# ── texto_limpio (formateo legible del perfil para embeddings/lectura) ──────

def construir_texto_limpio(perfil: dict) -> str:
    """Puerto de 1:1 del nodo 'Generar texto_limpio'. Tolera tanto el esquema
    nuevo (nombre/apellido separados) como el viejo (nombre_apellido)."""
    dp = perfil.get("datos_personales", {}) or {}
    nombre_completo = dp.get("nombre_apellido") or " ".join(
        x for x in [dp.get("nombre"), dp.get("apellido")] if x
    ) or "N/A"

    telefono = dp.get("telefono")
    telefono_str = ", ".join(str(t) for t in telefono) if isinstance(telefono, list) else (telefono or "No especificado")

    lineas = [
        f"CANDIDATO: {nombre_completo}",
        f"EDAD: {dp.get('edad', 'N/A')}",
        f"DNI: {dp.get('dni', 'N/A')}",
        f"LOCALIDAD: {dp.get('localidad', 'N/A')}",
        f"DOMICILIO: {dp.get('domicilio', 'N/A')}",
        f"ESTADO CIVIL: {dp.get('estado_civil', 'N/A')}",
        f"TELÉFONO: {telefono_str}",
        f"EMAIL: {dp.get('email', 'No especificado')}",
        "",
    ]

    if perfil.get("perfil_profesional"):
        lineas += [f"PERFIL PROFESIONAL:\n{perfil['perfil_profesional']}", ""]

    lineas.append("EXPERIENCIA LABORAL:")
    for exp in perfil.get("experiencia_laboral", []) or []:
        lineas += [
            f"- Empresa: {exp.get('empresa', 'N/A')}",
            f"  Puesto: {exp.get('puesto', 'N/A')}",
            f"  Período: {exp.get('fecha_inicio', 'N/A')} - {exp.get('fecha_finalizacion', 'N/A')}",
            f"  Descripción: {exp.get('descripcion', 'N/A')}",
            f"  Industria: {exp.get('industria', 'N/A')}",
            "",
        ]

    lineas.append("FORMACIÓN ACADÉMICA:")
    for form in perfil.get("formacion_academica", []) or []:
        lineas.append(
            f"- {form.get('titulo', 'N/A')} en {form.get('institucion', 'N/A')} "
            f"({form.get('nivel', 'N/A')}) - {form.get('estado', 'N/A')}"
        )

    competencias = perfil.get("competencias", {}) or {}
    lineas += [
        "",
        f"COMPETENCIAS TÉCNICAS:\n{', '.join(competencias.get('tecnicas', [])) or 'No especificadas'}",
        "",
        f"COMPETENCIAS BLANDAS:\n{', '.join(competencias.get('blandas', [])) or 'No especificadas'}",
        "",
    ]
    if competencias.get("herramientas"):
        lineas += [f"HERRAMIENTAS:\n{', '.join(competencias['herramientas'])}", ""]

    tecnologias = perfil.get("tecnologias", []) or []
    if tecnologias:
        lineas.append("TECNOLOGÍAS:")
        for t in tecnologias:
            nombre_t = t.get("nombre") if isinstance(t, dict) else t
            lineas.append(f"- {nombre_t or 'N/A'}")

    sl = perfil.get("situacion_laboral", {}) or {}
    lineas += ["", "SITUACIÓN LABORAL:"]
    if sl.get("ocupacion"):
        lineas.append(f"Ocupación: {sl['ocupacion']}")
    if sl.get("disponibilidad_horaria"):
        lineas.append(f"Disponibilidad: {sl['disponibilidad_horaria']}")
    lineas.append(f"Licencias: {', '.join(sl.get('licencias', [])) or 'No especificadas'}")

    return "\n".join(lineas).strip()
