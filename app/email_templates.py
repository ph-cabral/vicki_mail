"""
Plantillas de respuesta. Texto y firma copiados literal de los nodos
emailSend del workflow n8n original, salvo donde se indica "NUEVA" (caso
descrito por el usuario que no tenía un nodo equivalente en el JSON).
"""

FIRMA_HTML = """
<br>
<div dir="ltr" class="gmail_signature" data-smartmail="gmail_signature">
<div dir="ltr">
<table style="color:rgb(34,34,34);border:none;border-collapse:collapse">
<tbody>
<tr style="height:88.5pt">
<td style="vertical-align:top;padding:5pt;overflow:hidden">
<span style="border:none;display:inline-block;overflow:hidden;width:126px;height:126px">
<font face="arial narrow, sans-serif">
<img src="https://lh7-us.googleusercontent.com/7JbyZYHomWWDU9lnpaD4PNJkNlIam74JaEIIVxaF23IFlQXROfMhSeOvMMIqOPYQCfKLv_hhK0CI6r-7-jhsbAk-oT6BzP1T_6MbG8DpWeWgVDSTXOpjR1iOcCCKow7Ttuh29FSOiDHww_iY-hKU71s" width="96" height="96" style="margin-left:0px;margin-top:0px">
</font>
</span>
</td>
<td style="vertical-align:top;padding:5pt;overflow:hidden">
<p dir="ltr" style="line-height:1.56;margin-top:0pt;margin-bottom:0pt">
<font face="arial narrow, sans-serif">
<span style="background-color:transparent;color:rgb(67,67,67);font-size:12pt;font-weight:700">Lic. Carolina Catramboni</span>
<br>
</font>
</p>
<p dir="ltr" style="line-height:1.56;margin-top:0pt;margin-bottom:0pt">
<span style="background-color:transparent;font-size:11pt">
<font color="#999999" face="arial narrow, sans-serif"><b><i>Responsable de RR.HH</i></b></font>
</span>
</p>
<p dir="ltr" style="line-height:1.56;margin-top:0pt;margin-bottom:0pt">
<span style="background-color:transparent;color:rgb(67,67,67);font-size:11pt">
<font face="arial narrow, sans-serif">03564 - 15231379</font>
</span>
</p>
<p dir="ltr" style="line-height:1.56;margin-top:0pt;margin-bottom:0pt">
<span style="background-color:transparent">
<font color="#434343" face="arial narrow, sans-serif">
<span style="font-size:14.6667px">
<a href="https://www.linkedin.com/company/ever-wear">https://www.linkedin.com/company/ever-wear</a>
</span>
</font>
</span>
</p>
<p dir="ltr" style="line-height:1.56;margin-top:0pt;margin-bottom:0pt">
<a href="https://www.everwear.com.ar/" style="color:rgb(17,85,204)">
<span style="font-size:11pt;color:rgb(67,67,67);background-color:transparent;vertical-align:baseline">
<font face="arial narrow, sans-serif">www.everwear.com.ar</font>
</span>
</a>
</p>
</td>
</tr>
</tbody>
</table>
</div>
</div>
"""


def _wrap(cuerpo: str) -> str:
    return cuerpo + FIRMA_HTML


# Send email / Send email1 en n8n — candidato nuevo.
def postulacion_recibida(nombre: str) -> tuple[str, str]:
    subject = "¡Postulación recibida!"
    html = _wrap(f"""<p>Estimado/a {nombre},</p>

<p>Muchas gracias por enviarnos tu currículum y por tu interés en formar parte de nuestro equipo. Agradecemos el tiempo que te has tomado para contactarnos.</p>

<p>Hemos recibido tu postulación y queremos informarte que tu perfil será cuidadosamente revisado. En caso de que tus habilidades y experiencia se ajusten a nuestras necesidades actuales, serás contactado para futuras etapas del proceso de selección. Si en este momento no surge una vacante acorde, tu currículum será incluido en nuestra base de datos para futuras oportunidades que puedan surgir.</p>

<p>Te deseamos mucho éxito en tu búsqueda laboral.</p>

<p>Además, te invitamos a seguir nuestra página en el perfil de LinkedIn <a href="https://www.linkedin.com/company/ever-wear">https://www.linkedin.com/company/ever-wear</a> donde estaremos publicando las vacantes que surjan e información novedosa sobre nuestra empresa.</p>

<p>Saludos.</p>""")
    return subject, html


# Send email2 en n8n — adjunto es imagen/escaneo, no se pudo procesar.
def foto_no_procesada(nombre: str) -> tuple[str, str]:
    subject = "Detalle a tener en cuenta!"
    html = _wrap(f"""<p>Hola {nombre},</p>
<p>Gracias por tu interés en trabajar con nosotros.</p>
<p>
  Recibimos tu currículum pero nos llegó como una foto (imagen, foto del
  celular, o captura de pantalla).
  <strong>No podemos procesar currículums que vienen como fotos.</strong>
</p>
<p>
  Para poder guardar tus datos, necesitamos que nos mandes el archivo en formato
  Word (doc o docx) o PDF. Lo importante es que no sea una foto, sino un archivo
  donde se pueda seleccionar el texto.
</p>
<p>
  <strong>Ejemplo de lo que NO sirve:</strong><br />- Foto del CV sacada con el
  celular<br />- Captura de pantalla<br />- PDF que es una foto escaneada
</p>
<p>
  <strong>Ejemplo de lo que SÍ sirve:</strong><br />- Archivo Word<br />- PDF
  creado en la computadora (no escaneado)
</p>
<p>
  Por favor, respondé este mail adjuntando tu CV en el formato correcto así
  podemos tenerte en cuenta para futuras búsquedas.
</p>
<p>Saludos cordiales,</p>""")
    return subject, html


# Send email4 en n8n — candidato ya registrado (no mejora probabilidades).
def ya_registrado(nombre: str) -> tuple[str, str]:
    subject = "CV ya agregado!"
    html = _wrap(f"""<p>Hola {nombre},</p>

<p>Confirmamos la recepción de tu CV y hemos actualizado tu ficha en nuestra base de datos.</p>

<p>Para que tu postulación sea más efectiva, te sugerimos enviarnos tu perfil <b> únicamente cuando cuentes con actualizaciones relevantes </b> en tu trayectoria. Debido a que nuestro sistema centraliza tu información desde el primer envío, las aplicaciones múltiples con los mismos datos no incrementan las posibilidades de selección.</p>

<p>Tu perfil ya está activo y bajo la mirada de nuestro equipo de Selección. ¡Te recomendamos seguirnos en <a href="https://www.linkedin.com/company/ever-wear">LinkedIn</a> para ver las vacantes en tiempo real!</p>""")
    return subject, html


# Send email5 en n8n — remitente interno (@everwear.com.ar), mail eliminado.
def recordatorio_uso_interno() -> tuple[str, str]:
    subject = "Recordatorio!!!"
    html = _wrap("""<p>Buenas tardes, te recuerdo que este correo solo sera utilizado para postulaciones.</p>

<p>Por favor, para gestiones personales/internas enviar solicitud a recursoshumanos@everwear.com.ar</p>""")
    return subject, html


# NUEVA — no había nodo equivalente en el JSON para este caso (remitente
# externo, sin adjunto de CV). Mismo tono/firma que el resto.
def solo_recepcion_cv(nombre: str) -> tuple[str, str]:
    subject = "Este casillero es solo para currículums"
    html = _wrap(f"""<p>Hola{f' {nombre}' if nombre else ''},</p>

<p>Gracias por escribirnos. Este casillero de correo se utiliza exclusivamente para la recepción de currículums.</p>

<p>No encontramos un CV adjunto en tu mensaje. Si querés postularte, respondé este mail adjuntando tu currículum en formato Word (doc/docx) o PDF (no como foto o captura de pantalla).</p>

<p>Saludos cordiales,</p>""")
    return subject, html


# Send email3 en n8n — existe en el workflow original pero no quedó claro en
# qué rama se dispara (no la usamos por defecto; queda disponible por si se
# necesita distinguir "actualicé tus datos" de "ya registrado, no mejora
# probabilidades").
def actualizado_en_base(nombre: str) -> tuple[str, str]:
    subject = "¡Postulación recibida!"
    html = _wrap(f"""<p>Estimado/a {nombre},</p>

<p>Acabo de actualizar tus datos en la base.</p>

<p>Recorda seguir nuestra página en el perfil de LinkedIn <a href="https://www.linkedin.com/company/ever-wear">https://www.linkedin.com/company/ever-wear</a> para estar al tanto de las vacantes que surjan e información novedosa sobre nuestra empresa.</p>

<p>Saludos!!</p>""")
    return subject, html
