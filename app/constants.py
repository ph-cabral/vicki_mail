"""
IDs y reglas extraidos del workflow n8n "Ingesta y respuesta Email" (mismo buzon
seleccion@everwear.com.ar). Los IDs de labels/carpetas son especificos de esa
cuenta de Google -- si se reusa la misma cuenta siguen siendo validos, pero
VERIFICAR antes de ir a prod (Gmail > Configuracion > Etiquetas; Drive > URL
de la carpeta).
"""
import os

# Label que el workflow usa como "cola" de entrada: se lee de ahi y se remueve
# (+ INBOX) al terminar de procesar, dejando la bandeja limpia.
LABEL_QUEUE = os.getenv("LABEL_QUEUE", "Label_4652258528252762123")

# Label "cv procesado": se aplica en las ramas de exito (CV nuevo, ya
# registrado, recordatorio a interno).
LABEL_CV_PROCESADO = os.getenv("LABEL_CV_PROCESADO", "Label_3877397017358731180")

# Label alternativo visto en una rama distinta del workflow original (nodo
# "Agregar Etiqueta cv procesado8") -- no se pudo determinar con certeza a
# que rama corresponde exactamente. Se deja disponible por si se necesita.
LABEL_ALT_PROCESADO = os.getenv("LABEL_ALT_PROCESADO", "Label_642290056197378156")

# Carpetas Drive donde Read AI / Fireflies dejan los resumenes de reunion.
DRIVE_FOLDER_READAI_SRC = os.getenv("DRIVE_FOLDER_READAI_SRC", "15lKi0d6gi6qCBbDkGCyLzspmZMJuySck")
DRIVE_FOLDER_FIREFLIES_SRC = os.getenv("DRIVE_FOLDER_FIREFLIES_SRC", "1L2Vo7KbNRWBQPvDjbxPlV3L95kZy0kn2")

# Carpetas destino tras procesar (archivado).
DRIVE_FOLDER_FIREFLIES_DONE = os.getenv("DRIVE_FOLDER_FIREFLIES_DONE", "1_nG9cC2Yo9PnVqb7t6ZvAR1GDHtjz52P")
DRIVE_FOLDER_READAI_DONE = os.getenv("DRIVE_FOLDER_READAI_DONE", "1HzCZ6CFXQ3G2ACzi6z5qCwhX1yCy6hov")

# Carpeta donde se archiva el CV original recibido (equivalente a los nodos
# "Upload file"/"Upload file1" del workflow n8n, carpeta llamada "ceve").
DRIVE_FOLDER_CV_ARCHIVE = os.getenv("DRIVE_FOLDER_CV_ARCHIVE", "1pySf6-w6CvHS3nCrUE0yV3R8MdVlpaOf")

# Plantilla base de CV que se adjunta cuando rechazamos una foto/escaneo
# (nodo "Download file" del workflow n8n).
DRIVE_TEMPLATE_CV_FILE_ID = os.getenv("DRIVE_TEMPLATE_CV_FILE_ID", "1A0VP4rfWW6hXhWmyAve0v47H_6JgoubN")
DRIVE_TEMPLATE_CV_FILENAME = os.getenv("DRIVE_TEMPLATE_CV_FILENAME", "CV para postulantes.docx")

# Remitentes de notificacion de cada integracion de transcripcion.
SENDER_READAI = "support@read.ai"
SENDER_FIREFLIES = "fred@fireflies.ai"

# Vistos en el workflow original pero fuera del alcance descrito (no
# implementados aca -- se loguean y se ignoran si aparecen):
SENDER_TRANSKRIPTOR = "no-reply@transkriptor.com"
SENDER_MEDICINA_LABORAL = "amiclaboral@gmail.com"

# Adjuntos validos como CV.
EXTENSIONES_PERMITIDAS = [
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
]

# Si el nombre del archivo contiene alguna de estas palabras, no se considera CV
# aunque tenga una extension valida (certificados, diplomas, etc.).
PALABRAS_PROHIBIDAS_NOMBRE = [
    "certificado", "diploma", "curso", "presentacion", "portfolio", "constancia",
]

# Palabras que indican intencion de postulacion en asunto/cuerpo del mail
# (para distinguir "sin CV pero es postulacion" de "no tiene nada que ver").
PALABRAS_POSTULACION = [
    "puesto", "postulante", "cv", "curriculum", "curriculum", "interes", "interes",
    "postulacion", "postulacion", "aplico", "candidato", "candidata", "solicito",
    "postulo", "vacante", "adjunto", "sumarme", "equipo de trabajo", "oportunidad",
    "trabajo", "operario", "administrador",
]

# Umbral de caracteres: si el texto extraido de un adjunto "de texto" (pdf/doc/
# docx) queda por debajo de esto, se asume que es una imagen/escaneo sin texto
# real (ej. foto del CV convertida a PDF). Ajustar segun casos reales.
MIN_CHARS_TEXTO_VALIDO = 40
