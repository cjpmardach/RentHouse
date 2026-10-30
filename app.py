from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify
from config import conectar, desconectar
import bcrypt
import psycopg2
import smtplib
import random
import string
import os
import re
import time
import threading
from datetime import date
from decimal import Decimal, InvalidOperation
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from psycopg2 import sql

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = "cambia-esto-por-una-clave-secreta-segura"

CARPETA_FOTOS = os.path.join('static', 'uploads')
os.makedirs(CARPETA_FOTOS, exist_ok=True)
EXTENSIONES_FOTO = {'jpg', 'jpeg', 'png', 'webp'}
EXTENSIONES_VIDEO = {'mp4', 'webm', 'mov'}
EXTENSIONES_PERMITIDAS = EXTENSIONES_FOTO | EXTENSIONES_VIDEO
LIMITE_VIDEO_MB = 100
LIMITE_MENSAJE = 1000
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# Correos que entran como administrador (no hace falta tocar la base de datos)
ADMIN_CORREOS = ['saiehandrade@gmail.com', 'cjpmardach@gmail.com']

METODOS_PAGO = ['Efectivo', 'Transferencia', 'Nequi', 'Daviplata', 'Tarjeta', 'Otro']
ESTADOS_PAGO = ['Pagado', 'Pendiente']


def extension_valida(nombre_archivo):
    return '.' in nombre_archivo and nombre_archivo.rsplit('.', 1)[1].lower() in EXTENSIONES_PERMITIDAS


def es_video(nombre_archivo):
    return '.' in nombre_archivo and nombre_archivo.rsplit('.', 1)[1].lower() in EXTENSIONES_VIDEO


def video_demasiado_grande(archivos):
    for archivo in archivos:
        if archivo and archivo.filename and es_video(archivo.filename):
            archivo.seek(0, os.SEEK_END)
            tamano = archivo.tell()
            archivo.seek(0)
            if tamano > LIMITE_VIDEO_MB * 1024 * 1024:
                return archivo.filename
    return None


def tamano_archivo(archivo):
    archivo.seek(0, os.SEEK_END)
    tamano = archivo.tell()
    archivo.seek(0)
    return tamano


def archivos_con_contenido(archivos):
    return [a for a in archivos if a and a.filename and tamano_archivo(a) > 0]


def contar_archivos_validos(archivos):
    return sum(1 for a in archivos_con_contenido(archivos) if extension_valida(a.filename))


def entero_hasta_3_digitos(valor, minimo=0):
    texto = (valor or '').strip()
    if not re.fullmatch(r'[0-9]{1,3}', texto):
        return None
    numero = int(texto)
    return numero if numero >= minimo else None


def formato_cop(valor):
    if valor is None or valor == '':
        return ''
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return valor
    if numero == numero.to_integral_value():
        texto = f'{numero:,.0f}'.replace(',', '.')
    else:
        entero, decimales = f'{numero:,.2f}'.split('.')
        texto = entero.replace(',', '.') + ',' + decimales
    return '$' + texto


app.add_template_filter(formato_cop, 'cop')


OCUPADA_SQL = (
    "EXISTS (SELECT 1 FROM arriendo ar WHERE ar.cod1_vivienda = v.cod_vi "
    "AND (ar.fecha_fin IS NULL OR ar.fecha_fin > CURRENT_DATE))"
)
DISPONIBLE_SQL = "(v.estado = 'disponible' AND NOT " + OCUPADA_SQL + ")"
ESTADO_VISIBLE_SQL = "(CASE WHEN v.estado = 'disponible' AND " + OCUPADA_SQL + " THEN 'alquilada' ELSE v.estado END)"

MENSAJE_3_DIGITOS = ('Huéspedes, habitaciones y baños deben ser números enteros de máximo 3 dígitos '
                     '(de 0 a 999; los huéspedes, de 1 a 999).')


def guardar_archivos(cursor, archivos, cod_vi):
    nombre_carpeta = secure_filename(session['nombre']) or f"usuario_{session['usuario_id']}"
    carpeta_propietario = os.path.join(CARPETA_FOTOS, nombre_carpeta)
    fotos = 0
    videos = 0
    omitidos = []
    for archivo in archivos:
        if not archivo or not archivo.filename:
            continue
        if not extension_valida(archivo.filename):
            omitidos.append(archivo.filename)
            continue
        base, extension = archivo.filename.rsplit('.', 1)
        nombre_seguro = secure_filename(base) or 'archivo'
        nombre_unico = f"{cod_vi}_{random.randint(1000, 9999)}_{nombre_seguro}.{extension.lower()}"
        os.makedirs(carpeta_propietario, exist_ok=True)
        archivo.save(os.path.join(carpeta_propietario, nombre_unico))
        cursor.execute(
            "INSERT INTO foto_vivienda (url, cod1_vivienda) VALUES (%s, %s)",
            (f"{nombre_carpeta}/{nombre_unico}", cod_vi)
        )
        if es_video(archivo.filename):
            videos += 1
        else:
            fotos += 1
    return fotos, videos, omitidos


def borrar_archivos(urls):
    base = os.path.abspath(CARPETA_FOTOS)
    for url in urls:
        ruta = os.path.abspath(os.path.join(CARPETA_FOTOS, url))
        if ruta.startswith(base + os.sep) and os.path.isfile(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass


def fila_a_vivienda(fila):
    return {
        'cod_vi': fila[0],
        'titulo': fila[1],
        'descripcion': fila[2],
        'direccion': fila[3],
        'ciudad': fila[4],
        'estrato': fila[5],
        'precio': fila[6],
        'tipo_vivienda': fila[7],
        'estado': fila[8],
        'cod_dueno': fila[9],
        'huespedes_max': fila[10],
        'habitaciones': fila[11],
        'banos': fila[12],
        'wifi': fila[13],
        'piscina': fila[14],
        'cocina_equipada': fila[15],
        'estacionamiento': fila[16],
        'aire_acondicionado': fila[17],
        'mascotas_permitidas': fila[18]
    }


def url_mis_viviendas(roles):
    if 'arrendador' in roles and 'arrendatario' in roles:
        return url_for('menu_completo') + '#ar-viviendas'
    return url_for('menu_arrendador') + '#viviendas'


def url_historial_arrendador(roles):
    if 'arrendador' in roles and 'arrendatario' in roles:
        return url_for('menu_completo') + '#ar-historial'
    return url_for('menu_arrendador') + '#historial'


def url_historial_arrendatario(roles):
    if 'arrendador' in roles and 'arrendatario' in roles:
        return url_for('menu_completo') + '#at-historial'
    return url_for('menu_arrendatario') + '#historial'


def url_publicar_vivienda(roles):
    if 'arrendador' in roles and 'arrendatario' in roles:
        return url_for('menu_completo') + '#ar-publicar'
    return url_for('menu_arrendador') + '#publicar'


def normalizar_destino(destino, roles):
    if 'arrendador' in roles and 'arrendatario' in roles:
        return {'arrendador': 'completo_ar', 'arrendatario': 'completo_at'}.get(destino, destino)
    return destino


def url_historial(roles, como):
    if como == 'arrendador':
        return url_historial_arrendador(roles)
    return url_historial_arrendatario(roles)


def obtener_columna_banos(cursor):
    cursor.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'vivienda' AND column_name LIKE 'ba%os'"
    )
    return cursor.fetchone()[0]


def tabla_resena(cursor):
    cursor.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'rese%a'"
    )
    return sql.Identifier(cursor.fetchone()[0])


def agregar_portadas(cursor, viviendas):
    if not viviendas:
        return []
    cursor.execute(
        "SELECT cod1_vivienda, url FROM foto_vivienda WHERE cod1_vivienda = ANY(%s) ORDER BY cod_foto",
        ([fila[0] for fila in viviendas],)
    )
    portadas = {}
    for cod_vi, url in cursor.fetchall():
        if cod_vi not in portadas and not es_video(url):
            portadas[cod_vi] = url
    return [tuple(fila) + (portadas.get(fila[0]),) for fila in viviendas]


def crear_notificacion(cursor, cod_usu, mensaje, enlace=None, evitar_repetida=False, rol=None):
    if evitar_repetida and enlace:
        cursor.execute(
            "SELECT 1 FROM notificacion WHERE cod_usu = %s AND enlace = %s AND leida = FALSE LIMIT 1",
            (cod_usu, enlace)
        )
        if cursor.fetchone() is not None:
            return
    cursor.execute(
        "INSERT INTO notificacion (cod_usu, mensaje, enlace, rol) VALUES (%s, %s, %s, %s)",
        (cod_usu, mensaje[:255], enlace, rol)
    )


def obtener_notificaciones(cursor, cod_usu, rol=None):
    cursor.execute(
        "SELECT cod_not, mensaje, fecha, leida, enlace FROM notificacion "
        "WHERE cod_usu = %s AND (%s IS NULL OR rol = %s OR rol IS NULL) "
        "ORDER BY fecha DESC, cod_not DESC LIMIT 50",
        (cod_usu, rol, rol)
    )
    return [
        {'cod_not': f[0], 'mensaje': f[1], 'fecha': f[2], 'leida': f[3], 'enlace': f[4]}
        for f in cursor.fetchall()
    ]


def contar_sin_leer(cursor, cod_usu, rol=None):
    cursor.execute(
        "SELECT COUNT(*) FROM notificacion "
        "WHERE cod_usu = %s AND leida = FALSE AND (%s IS NULL OR rol = %s OR rol IS NULL)",
        (cod_usu, rol, rol)
    )
    return cursor.fetchone()[0]


def estado_alquiler(fecha_inicio, fecha_fin):
    hoy = date.today()
    if fecha_inicio and fecha_inicio > hoy:
        return 'Por iniciar'
    if fecha_fin and fecha_fin <= hoy:
        return 'Finalizado'
    return 'Activo'


def estado_pago_alquiler(pagados, pendientes):
    if pagados + pendientes == 0:
        return 'Sin pagos'
    return 'Pagado' if pagados > 0 and pendientes == 0 else 'Pendiente'


def actualizar_estado_pago_arriendo(cursor, cod_ar):
    cursor.execute(
        "SELECT COUNT(*) FILTER (WHERE estado = 'Pagado'), COUNT(*) FILTER (WHERE estado = 'Pendiente') "
        "FROM pago WHERE cod1_ar = %s",
        (cod_ar,)
    )
    pagados, pendientes = cursor.fetchone()
    nuevo = 'pagado' if pagados > 0 and pendientes == 0 else 'pendiente'
    cursor.execute("UPDATE arriendo SET estado_de_pago = %s WHERE cod_ar = %s", (nuevo, cod_ar))


def armar_alquiler(f, clave_otro):
    estado = estado_alquiler(f[3], f[4])
    return {
        'cod_ar': f[0],
        'titulo': f[1],
        clave_otro: f[2],
        'fecha_inicio': f[3],
        'fecha_fin': f[4],
        'estado_alquiler': estado,
        'estado_pago': estado_pago_alquiler(f[5], f[6]),
        'total_pagado': f[7],
        'ya_resenado': f[8],
        'pagos_registrados': f[5] + f[6],
        'total_resenas': f[9],
        'puede_finalizar': estado == 'Activo',
        'puede_desasociar': estado in ('Activo', 'Por iniciar'),
        'puede_resenar': estado != 'Por iniciar' and not f[8]
    }


def obtener_arriendos_arrendador(cursor, cod_arrendador):
    tabla = tabla_resena(cursor)
    cursor.execute(
        sql.SQL(
            """
            SELECT a.cod_ar, v.titulo, u.nombre, a.fecha_inicio, a.fecha_fin,
                   (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pagado'),
                   (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pendiente'),
                   (SELECT COALESCE(SUM(p.monto), 0) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pagado'),
                   EXISTS (SELECT 1 FROM {} r WHERE r.cod1_ar = a.cod_ar AND r.cod1_usu = %s),
                   (SELECT COUNT(*) FROM {} r2 WHERE r2.cod1_ar = a.cod_ar)
            FROM arriendo a
            JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
            JOIN usuario u ON u.cod_usu = a.cod1_usu
            WHERE v.cod1_usu = %s
            ORDER BY a.cod_ar DESC
            """
        ).format(tabla, tabla),
        (cod_arrendador, cod_arrendador)
    )
    return [armar_alquiler(f, 'arrendatario') for f in cursor.fetchall()]


def obtener_alquileres_arrendatario(cursor, cod_usu):
    tabla = tabla_resena(cursor)
    cursor.execute(
        sql.SQL(
            """
            SELECT a.cod_ar, v.titulo, u.nombre, a.fecha_inicio, a.fecha_fin,
                   (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pagado'),
                   (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pendiente'),
                   (SELECT COALESCE(SUM(p.monto), 0) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pagado'),
                   EXISTS (SELECT 1 FROM {} r WHERE r.cod1_ar = a.cod_ar AND r.cod1_usu = %s),
                   (SELECT COUNT(*) FROM {} r2 WHERE r2.cod1_ar = a.cod_ar)
            FROM arriendo a
            JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
            JOIN usuario u ON u.cod_usu = v.cod1_usu
            WHERE a.cod1_usu = %s
            ORDER BY a.cod_ar DESC
            """
        ).format(tabla, tabla),
        (cod_usu, cod_usu)
    )
    return [armar_alquiler(f, 'arrendador') for f in cursor.fetchall()]


def obtener_resenas_recibidas(cursor, cod_usu, como, limite=50):
    tabla = tabla_resena(cursor)
    if como == 'arrendador':
        filtro = "v.cod1_usu = %s AND r.cod1_usu = a.cod1_usu"
    else:
        filtro = "a.cod1_usu = %s AND r.cod1_usu = v.cod1_usu"
    consulta = (
        "SELECT r.calificacion, r.comentario, r.fecha, u.nombre, v.titulo "
        "FROM {} r "
        "JOIN arriendo a ON a.cod_ar = r.cod1_ar "
        "JOIN vivienda v ON v.cod_vi = a.cod1_vivienda "
        "JOIN usuario u ON u.cod_usu = r.cod1_usu "
        "WHERE " + filtro + " ORDER BY r.fecha DESC, r.cod_re DESC LIMIT %s"
    )
    cursor.execute(sql.SQL(consulta).format(tabla), (cod_usu, limite))
    return [
        {'calificacion': f[0], 'comentario': f[1], 'fecha': f[2], 'autor': f[3], 'titulo': f[4]}
        for f in cursor.fetchall()
    ]


def obtener_resenas_de_arrendatarios(cursor, cod_arrendador):
    cursor.execute(
        """
        SELECT DISTINCT u.cod_usu, u.nombre
        FROM arriendo a
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        JOIN usuario u ON u.cod_usu = a.cod1_usu
        WHERE v.cod1_usu = %s
        """,
        (cod_arrendador,)
    )
    personas = {f[0]: f[1] for f in cursor.fetchall()}

    cursor.execute(
        """
        SELECT DISTINCT u.cod_usu, u.nombre
        FROM solicitud_alquiler s
        JOIN vivienda v ON v.cod_vi = s.cod1_vivienda
        JOIN usuario u ON u.cod_usu = s.cod1_usu
        WHERE v.cod1_usu = %s AND s.estado = 'pendiente'
        """,
        (cod_arrendador,)
    )
    solicitantes = {f[0]: f[1] for f in cursor.fetchall()}
    personas.update(solicitantes)

    if not personas:
        return []
    ordenadas = sorted(personas.items(), key=lambda p: ((p[1] or '').lower(), p[0]))

    tabla = tabla_resena(cursor)
    cursor.execute(
        sql.SQL(
            "SELECT a.cod1_usu, r.calificacion, r.comentario, r.fecha, u.nombre, v.titulo "
            "FROM {} r "
            "JOIN arriendo a ON a.cod_ar = r.cod1_ar "
            "JOIN vivienda v ON v.cod_vi = a.cod1_vivienda "
            "JOIN usuario u ON u.cod_usu = r.cod1_usu "
            "WHERE a.cod1_usu = ANY(%s) AND r.cod1_usu = v.cod1_usu "
            "ORDER BY r.fecha DESC, r.cod_re DESC"
        ).format(tabla),
        ([p[0] for p in ordenadas],)
    )
    por_usuario = {}
    for cod_usu, calificacion, comentario, fecha, autor, titulo in cursor.fetchall():
        por_usuario.setdefault(cod_usu, []).append(
            {'calificacion': calificacion, 'comentario': comentario, 'fecha': fecha, 'autor': autor, 'titulo': titulo}
        )

    resultado = []
    for cod_usu, nombre in ordenadas:
        lista = por_usuario.get(cod_usu, [])
        notas = [r['calificacion'] for r in lista if r['calificacion'] is not None]
        resultado.append({
            'cod_usu': cod_usu,
            'nombre': nombre,
            'solicitud_pendiente': cod_usu in solicitantes,
            'total': len(lista),
            'promedio': round(sum(notas) / len(notas), 1) if notas else None,
            'resenas': lista
        })
    return resultado


def obtener_calificacion_vivienda(cursor, cod_vi, cod_dueno):
    tabla = tabla_resena(cursor)
    cursor.execute(
        sql.SQL(
            "SELECT COUNT(*), AVG(calificacion) FROM {} "
            "WHERE cod1_vivienda = %s AND cod1_ar IS NOT NULL AND cod1_usu <> %s"
        ).format(tabla),
        (cod_vi, cod_dueno)
    )
    total, promedio = cursor.fetchone()
    return total, (round(float(promedio), 1) if promedio is not None else None)


def obtener_pagos_arrendador(cursor, cod_arrendador):
    cursor.execute(
        """
        SELECT p.cod_pag, v.titulo, u.nombre, p.fecha_pago, p.monto, p.metodo_pago, p.estado
        FROM pago p
        JOIN arriendo a ON a.cod_ar = p.cod1_ar
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        JOIN usuario u ON u.cod_usu = a.cod1_usu
        WHERE v.cod1_usu = %s
        ORDER BY p.fecha_pago DESC, p.cod_pag DESC
        """,
        (cod_arrendador,)
    )
    return [
        {'cod_pag': f[0], 'titulo': f[1], 'arrendatario': f[2], 'fecha_pago': f[3],
         'monto': f[4], 'metodo_pago': f[5], 'estado': f[6]}
        for f in cursor.fetchall()
    ]


def obtener_pagos_arrendatario(cursor, cod_usu):
    cursor.execute(
        """
        SELECT p.cod_pag, v.titulo, p.fecha_pago, p.monto, p.metodo_pago, p.estado
        FROM pago p
        JOIN arriendo a ON a.cod_ar = p.cod1_ar
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        WHERE a.cod1_usu = %s
        ORDER BY p.fecha_pago DESC, p.cod_pag DESC
        """,
        (cod_usu,)
    )
    return [
        {'cod_pag': f[0], 'titulo': f[1], 'fecha_pago': f[2],
         'monto': f[3], 'metodo_pago': f[4], 'estado': f[5]}
        for f in cursor.fetchall()
    ]


def obtener_favoritos(cursor, cod_usu):
    cursor.execute(
        """
        SELECT v.cod_vi, v.titulo, v.ciudad, v.precio
        FROM favorito f
        JOIN vivienda v ON v.cod_vi = f.cod1_vivienda
        WHERE f.cod1_usu = %s AND """ + DISPONIBLE_SQL + """
        ORDER BY f.cod_fav DESC
        """,
        (cod_usu,)
    )
    favoritos = agregar_portadas(cursor, cursor.fetchall())
    cursor.execute("SELECT cod1_vivienda FROM favorito WHERE cod1_usu = %s", (cod_usu,))
    ids = {fila[0] for fila in cursor.fetchall()}
    return favoritos, ids


def volver_a(formulario, defecto):
    volver = formulario.get('volver', '')
    ancla = formulario.get('ancla', '')
    if not volver.startswith('/') or volver.startswith('//') or '\\' in volver:
        volver = defecto
    volver = volver.rstrip('?')
    if ancla and ancla.replace('-', '').replace('_', '').isalnum():
        volver += '#' + ancla
    return volver


def obtener_perfil(cursor, cod_usu):
    cursor.execute(
        "SELECT nombre, correo, telefono, ciudad, identificacion, tipo_identificacion, fecha_nacimiento "
        "FROM usuario WHERE cod_usu = %s",
        (cod_usu,)
    )
    f = cursor.fetchone()
    if f is None:
        return {'nombre': '', 'correo': '', 'telefono': '', 'ciudad': '',
                'identificacion': '', 'tipo_identificacion': '', 'fecha_nacimiento': ''}
    nacimiento = f[6].strftime('%d/%m/%Y') if hasattr(f[6], 'strftime') else (f[6] or '')
    return {
        'nombre': f[0] or '',
        'correo': f[1] or '',
        'telefono': f[2] or '',
        'ciudad': f[3] or '',
        'identificacion': f[4] or '',
        'tipo_identificacion': f[5] or '',
        'fecha_nacimiento': nacimiento
    }


def buscar_viviendas(cursor, ciudad, huespedes):
    ciudad = (ciudad or '').strip()
    try:
        huespedes_num = int(huespedes) if huespedes else None
    except (TypeError, ValueError):
        huespedes_num = None
    if huespedes_num is not None and huespedes_num < 1:
        huespedes_num = None

    condiciones = [DISPONIBLE_SQL]
    parametros = []
    if ciudad:
        condiciones.append("LOWER(TRIM(v.ciudad)) = LOWER(TRIM(%s))")
        parametros.append(ciudad)
    if huespedes_num is not None:
        condiciones.append("v.huespedes_max >= %s")
        parametros.append(huespedes_num)

    cursor.execute(
        "SELECT v.cod_vi, v.titulo, v.ciudad, v.precio FROM vivienda v WHERE " + " AND ".join(condiciones) + " ORDER BY v.cod_vi DESC",
        tuple(parametros)
    )
    viviendas = agregar_portadas(cursor, cursor.fetchall())

    cursor.execute(
        "SELECT DISTINCT ON (LOWER(TRIM(v.ciudad))) TRIM(v.ciudad) FROM vivienda v "
        "WHERE " + DISPONIBLE_SQL + " AND v.ciudad IS NOT NULL AND TRIM(v.ciudad) <> '' "
        "ORDER BY LOWER(TRIM(v.ciudad)), TRIM(v.ciudad)"
    )
    ciudades = [f[0] for f in cursor.fetchall()]
    if ciudad and ciudad.lower() not in [c.lower() for c in ciudades]:
        ciudades.append(ciudad)

    return viviendas, ciudad, (str(huespedes_num) if huespedes_num is not None else ''), ciudades


def validar_chat(cursor, usuario_id, roles, cod_vi, cod_otro):
    cursor.execute("SELECT titulo, direccion, cod1_usu FROM vivienda WHERE cod_vi = %s", (cod_vi,))
    fila_vivienda = cursor.fetchone()
    cursor.execute("SELECT nombre FROM usuario WHERE cod_usu = %s", (cod_otro,))
    fila_otro = cursor.fetchone()
    if fila_vivienda is None or fila_otro is None:
        return None

    titulo, direccion, cod_dueno = fila_vivienda
    valido = False
    if cod_dueno == usuario_id and cod_otro != usuario_id:
        cursor.execute(
            """
            SELECT 1 FROM mensaje
            WHERE cod_vi = %s
              AND ((cod_emisor = %s AND cod_receptor = %s) OR (cod_emisor = %s AND cod_receptor = %s))
            LIMIT 1
            """,
            (cod_vi, usuario_id, cod_otro, cod_otro, usuario_id)
        )
        valido = cursor.fetchone() is not None
    elif cod_dueno == cod_otro and cod_dueno != usuario_id and 'arrendatario' in roles:
        valido = True

    return (titulo, direccion, cod_dueno, fila_otro[0]) if valido else None


def armar_mensaje(cod_men, contenido, cod_emisor, nombre_emisor, cod_dueno, usuario_id):
    rol = 'arrendador' if cod_emisor == cod_dueno else 'arrendatario'
    return {
        'cod_men': cod_men,
        'contenido': contenido,
        'cod_emisor': cod_emisor,
        'etiqueta': f"{nombre_emisor}/{rol}",
        'mio': cod_emisor == usuario_id
    }


def guardar_mensaje(cursor, usuario_id, cod_otro, cod_vi, contenido, titulo, cod_dueno=None):
    cursor.execute(
        "INSERT INTO mensaje (contenido, fecha, cod_emisor, cod_receptor, cod_vi) "
        "VALUES (%s, CURRENT_DATE, %s, %s, %s) RETURNING cod_men",
        (contenido, usuario_id, cod_otro, cod_vi)
    )
    cod_men = cursor.fetchone()[0]
    if not esta_en_chat(cod_otro, cod_vi, usuario_id):
        crear_notificacion(
            cursor,
            cod_otro,
            f'Nuevo mensaje de {session["nombre"]} sobre "{titulo}".',
            url_for('ver_mensajes', cod_vi=cod_vi, cod_otro=usuario_id),
            evitar_repetida=True,
            rol='arrendador' if cod_dueno == cod_otro else 'arrendatario'
        )
    return cod_men


def asegurar_tabla_notificacion():
    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS notificacion (
            cod_not SERIAL PRIMARY KEY,
            cod_usu INTEGER NOT NULL REFERENCES usuario(cod_usu) ON DELETE CASCADE,
            mensaje VARCHAR(255) NOT NULL,
            enlace VARCHAR(255),
            fecha TIMESTAMP NOT NULL DEFAULT NOW(),
            leida BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    cursor.execute("ALTER TABLE notificacion ADD COLUMN IF NOT EXISTS rol VARCHAR(20)")
    cursor.execute(
        "UPDATE notificacion SET rol = 'arrendatario' WHERE rol IS NULL AND enlace LIKE '%#at-%'"
    )
    cursor.execute(
        "UPDATE notificacion SET rol = 'arrendador' WHERE rol IS NULL AND enlace LIKE '%#ar-%'"
    )
    cursor.execute(
        """
        UPDATE notificacion n SET rol = r.rol
        FROM rol_usuario r
        WHERE n.rol IS NULL AND n.cod_usu = r.cod_usu
          AND (SELECT COUNT(*) FROM rol_usuario x WHERE x.cod_usu = n.cod_usu) = 1
        """
    )
    conexion.commit()
    cursor.close()
    desconectar(conexion)


PRESENCIA_SEGUNDOS = 6
presencia_chat = {}
bloqueo_presencia = threading.Lock()


def marcar_en_chat(cod_usu, cod_vi, cod_otro):
    ahora = time.monotonic()
    with bloqueo_presencia:
        presencia_chat[(cod_usu, cod_vi, cod_otro)] = ahora
        if len(presencia_chat) > 1000:
            for clave in [k for k, t in presencia_chat.items() if ahora - t > PRESENCIA_SEGUNDOS]:
                del presencia_chat[clave]


def salir_de_chat(cod_usu, cod_vi, cod_otro):
    with bloqueo_presencia:
        presencia_chat.pop((cod_usu, cod_vi, cod_otro), None)


def esta_en_chat(cod_usu, cod_vi, cod_otro):
    with bloqueo_presencia:
        momento = presencia_chat.get((cod_usu, cod_vi, cod_otro))
    return momento is not None and time.monotonic() - momento <= PRESENCIA_SEGUNDOS


def asegurar_tabla_solicitud():
    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS solicitud_alquiler (
            cod_sol SERIAL PRIMARY KEY,
            cod1_usu INTEGER NOT NULL REFERENCES usuario(cod_usu) ON DELETE CASCADE,
            cod1_vivienda INTEGER NOT NULL REFERENCES vivienda(cod_vi) ON DELETE CASCADE,
            estado VARCHAR(20) NOT NULL DEFAULT 'pendiente',
            fecha TIMESTAMP NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS solicitud_pendiente_unica
            ON solicitud_alquiler (cod1_usu, cod1_vivienda) WHERE estado = 'pendiente';
        """
    )
    conexion.commit()
    cursor.close()
    desconectar(conexion)


asegurar_tabla_notificacion()
asegurar_tabla_solicitud()


@app.errorhandler(413)
def archivos_demasiado_grandes(error):
    flash('Los archivos superan el tamaño máximo permitido (500 MB en total). Sube menos archivos.', 'publicar_error')
    return redirect(url_publicar_vivienda(session.get('roles', [])))


@app.route("/")
def inicio():
    return render_template("inicio.html")


@app.route("/PROYECTO_FINAL")
def index():
    return render_template("index.html")


@app.route("/index", methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        usuario = request.form['usuario']
        contraseña = request.form['contraseña']

        conexion = conectar()
        cursor = conexion.cursor()
        cursor.execute(
            "SELECT * FROM usuario WHERE correo = %s",
            (usuario,)
        )
        resultado = cursor.fetchone()

        if resultado is None or not bcrypt.checkpw(contraseña.encode(), resultado[6].encode()):
            cursor.close()
            desconectar(conexion)
            return render_template("index.html", error="Usuario o contraseña incorrectos")

        if resultado[7] == 'inactivo':
            cursor.close()
            desconectar(conexion)
            return render_template("index.html", error="Esta cuenta está inactiva")

        cod_usu = resultado[0]

        cursor.execute(
            "SELECT rol FROM rol_usuario WHERE cod_usu = %s",
            (cod_usu,)
        )
        roles = [fila[0] for fila in cursor.fetchall()]
        cursor.close()
        desconectar(conexion)

        if usuario.strip().lower() in [c.lower() for c in ADMIN_CORREOS] and 'administrador' not in roles:
            roles.append('administrador')

        session['usuario_id'] = cod_usu
        session['nombre'] = resultado[1]
        session['roles'] = roles

        if 'administrador' in roles:
            session['rol_activo'] = 'administrador'
            return redirect(url_for('menu_admin'))

        if 'arrendador' in roles and 'arrendatario' in roles:
            return redirect(url_for('menu_completo'))
        elif 'arrendador' in roles:
            session['rol_activo'] = 'arrendador'
            return redirect(url_for('menu_arrendador'))
        elif 'arrendatario' in roles:
            session['rol_activo'] = 'arrendatario'
            return redirect(url_for('menu_arrendatario'))

        return redirect(url_for('index'))

    return render_template("index.html")


@app.route('/seleccion-rol')
def seleccion_rol():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    return render_template('seleccion_rol.html', nombre=session['nombre'])


@app.route('/seleccion-rol/arrendador')
def elegir_arrendador():
    if 'usuario_id' not in session or 'arrendador' not in session.get('roles', []):
        return redirect(url_for('login'))
    session['rol_activo'] = 'arrendador'
    return redirect(url_for('menu_arrendador'))


@app.route('/seleccion-rol/arrendatario')
def elegir_arrendatario():
    if 'usuario_id' not in session or 'arrendatario' not in session.get('roles', []):
        return redirect(url_for('login'))
    session['rol_activo'] = 'arrendatario'
    return redirect(url_for('menu_arrendatario'))


@app.route('/arrendador/menu')
def menu_arrendador():
    if 'usuario_id' not in session or 'arrendador' not in session.get('roles', []):
        return redirect(url_for('login'))
    if 'arrendatario' in session.get('roles', []):
        return redirect(url_for('menu_completo'))

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        "SELECT v.cod_vi, v.titulo, v.ciudad, v.precio, " + ESTADO_VISIBLE_SQL + " FROM vivienda v WHERE v.cod1_usu = %s ORDER BY v.cod_vi DESC",
        (session['usuario_id'],)
    )
    viviendas = cursor.fetchall()
    viviendas = agregar_portadas(cursor, viviendas)
    notificaciones = obtener_notificaciones(cursor, session['usuario_id'], 'arrendador')
    sin_leer = contar_sin_leer(cursor, session['usuario_id'], 'arrendador')
    arriendos = obtener_arriendos_arrendador(cursor, session['usuario_id'])
    pagos_recibidos = obtener_pagos_arrendador(cursor, session['usuario_id'])
    resenas_recibidas = obtener_resenas_recibidas(cursor, session['usuario_id'], 'arrendador')
    resenas_arrendatarios = obtener_resenas_de_arrendatarios(cursor, session['usuario_id'])
    perfil = obtener_perfil(cursor, session['usuario_id'])
    cursor.close()
    desconectar(conexion)

    return render_template(
        'arrendador/menu.html',
        nombre=session['nombre'],
        viviendas=viviendas,
        notificaciones=notificaciones,
        sin_leer=sin_leer,
        arriendos=arriendos,
        pagos_recibidos=pagos_recibidos,
        resenas_recibidas=resenas_recibidas,
        resenas_arrendatarios=resenas_arrendatarios,
        perfil=perfil,
        metodos_pago=METODOS_PAGO,
        estados_pago=ESTADOS_PAGO
    )


@app.route('/publicar-vivienda', methods=['POST'])
def publicar_vivienda():
    if 'usuario_id' not in session or 'arrendador' not in session.get('roles', []):
        return redirect(url_for('login'))

    volver = url_publicar_vivienda(session.get('roles', []))
    archivos = request.files.getlist('archivos')

    if not archivos_con_contenido(archivos):
        flash('Debes subir al menos una foto o un video para publicar la vivienda.', 'publicar_error')
        return redirect(volver)
    if contar_archivos_validos(archivos) == 0:
        flash('Ninguno de los archivos tiene un formato permitido. Fotos: JPG, PNG o WEBP. Videos: MP4, WEBM o MOV.', 'publicar_error')
        return redirect(volver)

    grande = video_demasiado_grande(archivos)
    if grande:
        flash(f'El video "{grande}" supera el límite de {LIMITE_VIDEO_MB} MB. La vivienda no se publicó.', 'publicar_error')
        return redirect(volver)

    huespedes_max = entero_hasta_3_digitos(request.form.get('huespedes_max'), 1)
    habitaciones = entero_hasta_3_digitos(request.form.get('habitaciones'))
    banos = entero_hasta_3_digitos(request.form.get('banos'))
    if huespedes_max is None or habitaciones is None or banos is None:
        flash(MENSAJE_3_DIGITOS + ' La vivienda no se publicó.', 'publicar_error')
        return redirect(volver)
    estrato = entero_hasta_3_digitos(request.form.get('estrato'), 1)
    if estrato is None or estrato > 6:
        flash('El estrato debe ser un número de 1 a 6. La vivienda no se publicó.', 'publicar_error')
        return redirect(volver)

    titulo = request.form['titulo']
    direccion = request.form['direccion']
    ciudad = request.form['ciudad']
    precio = request.form['precio']
    tipo_vivienda = request.form['tipo_vivienda']
    descripcion = request.form['descripcion']

    wifi = 'wifi' in request.form
    piscina = 'piscina' in request.form
    cocina_equipada = 'cocina_equipada' in request.form
    estacionamiento = 'estacionamiento' in request.form
    aire_acondicionado = 'aire_acondicionado' in request.form
    mascotas_permitidas = 'mascotas_permitidas' in request.form

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        """
        INSERT INTO vivienda
        VALUES (DEFAULT, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING cod_vi
        """,
        (titulo, descripcion, direccion, ciudad, estrato, precio,
         tipo_vivienda, 'disponible', session['usuario_id'],
         huespedes_max, habitaciones, banos,
         wifi, piscina, cocina_equipada, estacionamiento, aire_acondicionado, mascotas_permitidas)
    )
    cod_vi = cursor.fetchone()[0]

    fotos, videos, omitidos = guardar_archivos(cursor, archivos, cod_vi)

    crear_notificacion(
        cursor,
        session['usuario_id'],
        f'Publicaste la vivienda "{titulo}".',
        url_for('ver_vivienda', cod_vi=cod_vi),
        rol='arrendador'
    )

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    mensaje = 'Vivienda publicada correctamente.'
    if fotos or videos:
        mensaje += f' Se subieron {fotos} foto(s) y {videos} video(s).'
    flash(mensaje, 'publicar_ok')
    if omitidos:
        flash('No se subieron por formato no permitido: ' + ', '.join(omitidos) + '.', 'publicar_error')
    return redirect(volver)


@app.route('/arrendatario/menu')
def menu_arrendatario():
    if 'usuario_id' not in session or 'arrendatario' not in session.get('roles', []):
        return redirect(url_for('login'))
    if 'arrendador' in session.get('roles', []):
        return redirect(url_for('menu_completo', **request.args.to_dict()))

    conexion = conectar()
    cursor = conexion.cursor()
    viviendas, ciudad_seleccionada, huespedes_seleccionado, ciudades = buscar_viviendas(
        cursor, request.args.get('ciudad', ''), request.args.get('huespedes', '')
    )
    favoritos, favoritos_ids = obtener_favoritos(cursor, session['usuario_id'])
    notificaciones = obtener_notificaciones(cursor, session['usuario_id'], 'arrendatario')
    sin_leer = contar_sin_leer(cursor, session['usuario_id'], 'arrendatario')
    mis_pagos = obtener_pagos_arrendatario(cursor, session['usuario_id'])
    mis_alquileres = obtener_alquileres_arrendatario(cursor, session['usuario_id'])
    resenas_recibidas = obtener_resenas_recibidas(cursor, session['usuario_id'], 'arrendatario')
    perfil = obtener_perfil(cursor, session['usuario_id'])
    cursor.close()
    desconectar(conexion)

    return render_template(
        'arrendatario/menu.html',
        nombre=session['nombre'],
        viviendas=viviendas,
        favoritos=favoritos,
        favoritos_ids=favoritos_ids,
        notificaciones=notificaciones,
        sin_leer=sin_leer,
        mis_pagos=mis_pagos,
        mis_alquileres=mis_alquileres,
        resenas_recibidas=resenas_recibidas,
        perfil=perfil,
        ciudades=ciudades,
        ciudad_seleccionada=ciudad_seleccionada,
        huespedes_seleccionado=huespedes_seleccionado
    )


def rutas_de_inicio(roles):
    if 'administrador' in roles:
        return url_for('menu_admin'), None, url_for('menu_admin') + '#cuenta'
    if 'arrendador' in roles and 'arrendatario' in roles:
        base = url_for('menu_completo')
        return base, base + '#ar-publicar', base + '#at-perfil'
    if 'arrendador' in roles:
        base = url_for('menu_arrendador')
        return base, base + '#publicar', base + '#perfil'
    base = url_for('menu_arrendatario')
    return base, None, base + '#perfil'


@app.route('/vivienda/<int:cod_vi>')
def ver_vivienda(cod_vi):
    usuario_id = session.get('usuario_id')
    es_publico = usuario_id is None
    roles = session.get('roles', [])
    if es_publico:
        url_inicio, url_publicar, url_perfil = url_for('inicio'), None, None
    else:
        url_inicio, url_publicar, url_perfil = rutas_de_inicio(roles)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM vivienda WHERE cod_vi = %s", (cod_vi,))
    fila = cursor.fetchone()

    estado_visible = None
    es_arrendatario_de_ella = False
    if fila is not None:
        cursor.execute("SELECT " + ESTADO_VISIBLE_SQL + " FROM vivienda v WHERE v.cod_vi = %s", (cod_vi,))
        estado_visible = cursor.fetchone()[0]
        if not es_publico:
            cursor.execute(
                "SELECT 1 FROM arriendo WHERE cod1_usu = %s AND cod1_vivienda = %s LIMIT 1",
                (usuario_id, cod_vi)
            )
            es_arrendatario_de_ella = cursor.fetchone() is not None

    if fila is None or (estado_visible != 'disponible' and fila[9] != usuario_id and not es_arrendatario_de_ella and 'administrador' not in roles):
        cursor.close()
        desconectar(conexion)
        return redirect(url_inicio)

    cursor.execute("SELECT nombre FROM usuario WHERE cod_usu = %s", (fila[9],))
    fila_dueno = cursor.fetchone()

    es_favorito = False
    solicitud_pendiente = False
    if not es_publico:
        cursor.execute(
            "SELECT 1 FROM favorito WHERE cod1_usu = %s AND cod1_vivienda = %s",
            (usuario_id, cod_vi)
        )
        es_favorito = cursor.fetchone() is not None

        cursor.execute(
            "SELECT 1 FROM solicitud_alquiler WHERE cod1_usu = %s AND cod1_vivienda = %s AND estado = 'pendiente'",
            (usuario_id, cod_vi)
        )
        solicitud_pendiente = cursor.fetchone() is not None

    cursor.execute(
        "SELECT url FROM foto_vivienda WHERE cod1_vivienda = %s ORDER BY cod_foto",
        (cod_vi,)
    )
    imagenes = []
    videos = []
    for (url,) in cursor.fetchall():
        if es_video(url):
            videos.append(url)
        else:
            imagenes.append(url)

    total_resenas, calificacion_promedio = obtener_calificacion_vivienda(cursor, cod_vi, fila[9])
    resenas_arrendador = obtener_resenas_recibidas(cursor, fila[9], 'arrendador', 10)

    cursor.close()
    desconectar(conexion)

    vivienda = fila_a_vivienda(fila)
    vivienda['estado'] = estado_visible
    es_dueno = vivienda['cod_dueno'] == usuario_id

    return render_template(
        'ver_vivienda.html',
        nombre=session.get('nombre'),
        es_publico=es_publico,
        vivienda=vivienda,
        nombre_dueno=fila_dueno[0] if fila_dueno else 'Arrendador',
        imagenes=imagenes,
        videos=videos,
        es_dueno=es_dueno,
        es_favorito=es_favorito,
        solicitud_pendiente=solicitud_pendiente,
        puede_chatear='arrendatario' in roles and not es_dueno,
        total_resenas=total_resenas,
        calificacion_promedio=calificacion_promedio,
        resenas_arrendador=resenas_arrendador,
        url_inicio=url_inicio,
        url_publicar=url_publicar,
        url_perfil=url_perfil
    )


def obtener_roles_usuario(cursor, cod_usu):
    cursor.execute("SELECT rol FROM rol_usuario WHERE cod_usu = %s", (cod_usu,))
    return [fila[0] for fila in cursor.fetchall()]


def obtener_conversaciones(cursor, usuario_id):
    cursor.execute(
        """
        SELECT m.contenido, m.cod_emisor, m.cod_receptor, m.cod_vi,
               v.titulo, v.direccion, v.cod1_usu
        FROM mensaje m
        JOIN vivienda v ON v.cod_vi = m.cod_vi
        WHERE m.cod_emisor = %s OR m.cod_receptor = %s
        ORDER BY m.fecha DESC, m.cod_men DESC
        """,
        (usuario_id, usuario_id)
    )
    filas = cursor.fetchall()

    vistos = {}
    for contenido, cod_emisor, cod_receptor, cod_vi, titulo, direccion, cod_dueno in filas:
        otro = cod_receptor if cod_emisor == usuario_id else cod_emisor
        clave = (cod_vi, otro)
        if clave not in vistos:
            vistos[clave] = {
                'cod_vi': cod_vi,
                'cod_otro': otro,
                'titulo': titulo,
                'direccion': direccion,
                'ultimo': contenido,
                'soy_dueno': cod_dueno == usuario_id
            }

    lista = []
    for chat in vistos.values():
        nombre_otro = None
        if chat['soy_dueno']:
            cursor.execute("SELECT nombre FROM usuario WHERE cod_usu = %s", (chat['cod_otro'],))
            fila = cursor.fetchone()
            nombre_otro = fila[0] if fila else "Usuario"
        chat['nombre_otro'] = nombre_otro
        lista.append(chat)

    return lista


def obtener_viviendas_disponibles(cursor, usuario_id, roles, conversaciones):
    if 'arrendatario' not in roles:
        return []

    ya_en_chat = {(c['cod_vi'], c['cod_otro']) for c in conversaciones}

    cursor.execute(
        "SELECT v.cod_vi, v.titulo, v.direccion, v.cod1_usu FROM vivienda v "
        "WHERE " + DISPONIBLE_SQL + " AND v.cod1_usu != %s ORDER BY v.cod_vi DESC",
        (usuario_id,)
    )
    filas = cursor.fetchall()

    lista = []
    for cod_vi, titulo, direccion, cod_dueno in filas:
        if (cod_vi, cod_dueno) in ya_en_chat:
            continue
        lista.append({
            'cod_vi': cod_vi,
            'cod_dueno': cod_dueno,
            'titulo': titulo,
            'direccion': direccion
        })

    return lista


@app.route('/mensajes')
def mensajes():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    conexion = conectar()
    cursor = conexion.cursor()

    roles = session.get('roles', [])
    conversaciones = obtener_conversaciones(cursor, usuario_id)
    viviendas_disponibles = obtener_viviendas_disponibles(cursor, usuario_id, roles, conversaciones)
    sin_leer_ar = contar_sin_leer(cursor, usuario_id, 'arrendador')
    sin_leer_at = contar_sin_leer(cursor, usuario_id, 'arrendatario')

    cursor.close()
    desconectar(conexion)

    return render_template(
        'mensajes.html',
        nombre=session['nombre'],
        roles=roles,
        sin_leer_ar=sin_leer_ar,
        sin_leer_at=sin_leer_at,
        conversaciones=conversaciones,
        viviendas_disponibles=viviendas_disponibles,
        cod_vi_activo=None,
        cod_otro_activo=None,
        mensajes_hilo=[],
        usuario_id=usuario_id
    )

@app.route('/mensajes/<int:cod_vi>/<int:cod_otro>', methods=['GET', 'POST'])
def ver_mensajes(cod_vi, cod_otro):
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    roles = session.get('roles', [])
    conexion = conectar()
    cursor = conexion.cursor()

    chat = validar_chat(cursor, usuario_id, roles, cod_vi, cod_otro)
    if chat is None:
        cursor.close()
        desconectar(conexion)
        return redirect(url_for('mensajes'))

    titulo_activo, direccion_activa, cod_dueno, nombre_otro_activo = chat
    soy_dueno_activo = cod_dueno == usuario_id

    if request.method == 'GET':
        marcar_en_chat(usuario_id, cod_vi, cod_otro)

    if request.method == 'POST':
        contenido = request.form.get('contenido', '').strip()
        if contenido and len(contenido) <= LIMITE_MENSAJE:
            try:
                guardar_mensaje(cursor, usuario_id, cod_otro, cod_vi, contenido, titulo_activo, cod_dueno)
                conexion.commit()
            except psycopg2.DataError:
                conexion.rollback()
        cursor.close()
        desconectar(conexion)
        return redirect(url_for('ver_mensajes', cod_vi=cod_vi, cod_otro=cod_otro))

    cursor.execute(
        """
        SELECT m.cod_men, m.contenido, m.cod_emisor, u.nombre
        FROM mensaje m
        JOIN usuario u ON u.cod_usu = m.cod_emisor
        WHERE m.cod_vi = %s
          AND ((m.cod_emisor = %s AND m.cod_receptor = %s) OR (m.cod_emisor = %s AND m.cod_receptor = %s))
        ORDER BY m.fecha, m.cod_men
        """,
        (cod_vi, usuario_id, cod_otro, cod_otro, usuario_id)
    )
    mensajes_hilo = [
        armar_mensaje(cod_men, contenido, cod_emisor, nombre_emisor, cod_dueno, usuario_id)
        for cod_men, contenido, cod_emisor, nombre_emisor in cursor.fetchall()
    ]
    ultimo_id = max((m['cod_men'] for m in mensajes_hilo), default=0)

    conversaciones = obtener_conversaciones(cursor, usuario_id)
    viviendas_disponibles = obtener_viviendas_disponibles(cursor, usuario_id, roles, conversaciones)
    sin_leer_ar = contar_sin_leer(cursor, usuario_id, 'arrendador')
    sin_leer_at = contar_sin_leer(cursor, usuario_id, 'arrendatario')

    cursor.close()
    desconectar(conexion)

    return render_template(
        'mensajes.html',
        nombre=session['nombre'],
        roles=roles,
        sin_leer_ar=sin_leer_ar,
        sin_leer_at=sin_leer_at,
        conversaciones=conversaciones,
        viviendas_disponibles=viviendas_disponibles,
        cod_vi_activo=cod_vi,
        cod_otro_activo=cod_otro,
        titulo_activo=titulo_activo,
        direccion_activa=direccion_activa,
        nombre_otro_activo=nombre_otro_activo,
        soy_dueno_activo=soy_dueno_activo,
        mensajes_hilo=mensajes_hilo,
        ultimo_id=ultimo_id,
        usuario_id=usuario_id
    )


@app.route('/mensajes/<int:cod_vi>/<int:cod_otro>/nuevos')
def mensajes_nuevos(cod_vi, cod_otro):
    if 'usuario_id' not in session:
        return jsonify({'error': 'sesion'}), 401

    usuario_id = session['usuario_id']
    roles = session.get('roles', [])
    try:
        despues = int(request.args.get('despues', 0))
    except ValueError:
        despues = 0

    conexion = conectar()
    cursor = conexion.cursor()
    chat = validar_chat(cursor, usuario_id, roles, cod_vi, cod_otro)
    if chat is None:
        cursor.close()
        desconectar(conexion)
        return jsonify({'error': 'chat'}), 403

    marcar_en_chat(usuario_id, cod_vi, cod_otro)
    cod_dueno = chat[2]
    cursor.execute(
        """
        SELECT m.cod_men, m.contenido, m.cod_emisor, u.nombre
        FROM mensaje m
        JOIN usuario u ON u.cod_usu = m.cod_emisor
        WHERE m.cod_vi = %s AND m.cod_men > %s
          AND ((m.cod_emisor = %s AND m.cod_receptor = %s) OR (m.cod_emisor = %s AND m.cod_receptor = %s))
        ORDER BY m.cod_men
        """,
        (cod_vi, despues, usuario_id, cod_otro, cod_otro, usuario_id)
    )
    nuevos = [
        armar_mensaje(cod_men, contenido, cod_emisor, nombre_emisor, cod_dueno, usuario_id)
        for cod_men, contenido, cod_emisor, nombre_emisor in cursor.fetchall()
    ]
    cursor.close()
    desconectar(conexion)

    return jsonify({'mensajes': nuevos})


@app.route('/mensajes/<int:cod_vi>/<int:cod_otro>/salir', methods=['POST'])
def salir_chat(cod_vi, cod_otro):
    if 'usuario_id' in session:
        salir_de_chat(session['usuario_id'], cod_vi, cod_otro)
    return ('', 204)


@app.route('/mensajes/<int:cod_vi>/<int:cod_otro>/enviar', methods=['POST'])
def enviar_mensaje(cod_vi, cod_otro):
    if 'usuario_id' not in session:
        return jsonify({'error': 'sesion'}), 401

    usuario_id = session['usuario_id']
    roles = session.get('roles', [])
    contenido = request.form.get('contenido', '').strip()
    if not contenido or len(contenido) > LIMITE_MENSAJE:
        return jsonify({'error': 'contenido'}), 400

    conexion = conectar()
    cursor = conexion.cursor()
    chat = validar_chat(cursor, usuario_id, roles, cod_vi, cod_otro)
    if chat is None:
        cursor.close()
        desconectar(conexion)
        return jsonify({'error': 'chat'}), 403

    try:
        cod_men = guardar_mensaje(cursor, usuario_id, cod_otro, cod_vi, contenido, chat[0], chat[2])
        conexion.commit()
    except psycopg2.DataError:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        return jsonify({'error': 'contenido'}), 400

    cursor.close()
    desconectar(conexion)

    mensaje = armar_mensaje(cod_men, contenido, usuario_id, session['nombre'], chat[2], usuario_id)
    return jsonify({'mensaje': mensaje})


@app.route('/menu-completo')
def menu_completo():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    roles = session.get('roles', [])
    if 'arrendador' not in roles or 'arrendatario' not in roles:
        return redirect(url_for('login'))

    conexion = conectar()
    cursor = conexion.cursor()

    cursor.execute(
        "SELECT v.cod_vi, v.titulo, v.ciudad, v.precio, " + ESTADO_VISIBLE_SQL + " FROM vivienda v WHERE v.cod1_usu = %s ORDER BY v.cod_vi DESC",
        (session['usuario_id'],)
    )
    viviendas_propias = cursor.fetchall()
    viviendas_propias = agregar_portadas(cursor, viviendas_propias)

    viviendas_disponibles, ciudad_seleccionada, huespedes_seleccionado, ciudades = buscar_viviendas(
        cursor, request.args.get('ciudad', ''), request.args.get('huespedes', '')
    )
    favoritos, favoritos_ids = obtener_favoritos(cursor, session['usuario_id'])
    notificaciones_at = obtener_notificaciones(cursor, session['usuario_id'], 'arrendatario')
    notificaciones_ar = obtener_notificaciones(cursor, session['usuario_id'], 'arrendador')
    sin_leer_at = contar_sin_leer(cursor, session['usuario_id'], 'arrendatario')
    sin_leer_ar = contar_sin_leer(cursor, session['usuario_id'], 'arrendador')
    arriendos = obtener_arriendos_arrendador(cursor, session['usuario_id'])
    pagos_recibidos = obtener_pagos_arrendador(cursor, session['usuario_id'])
    mis_pagos = obtener_pagos_arrendatario(cursor, session['usuario_id'])
    mis_alquileres = obtener_alquileres_arrendatario(cursor, session['usuario_id'])
    resenas_como_arrendador = obtener_resenas_recibidas(cursor, session['usuario_id'], 'arrendador')
    resenas_como_arrendatario = obtener_resenas_recibidas(cursor, session['usuario_id'], 'arrendatario')
    resenas_arrendatarios = obtener_resenas_de_arrendatarios(cursor, session['usuario_id'])
    perfil = obtener_perfil(cursor, session['usuario_id'])

    cursor.close()
    desconectar(conexion)

    return render_template(
        'menu_completo.html',
        nombre=session['nombre'],
        viviendas_propias=viviendas_propias,
        viviendas_disponibles=viviendas_disponibles,
        favoritos=favoritos,
        favoritos_ids=favoritos_ids,
        notificaciones_at=notificaciones_at,
        notificaciones_ar=notificaciones_ar,
        sin_leer_at=sin_leer_at,
        sin_leer_ar=sin_leer_ar,
        arriendos=arriendos,
        pagos_recibidos=pagos_recibidos,
        mis_pagos=mis_pagos,
        mis_alquileres=mis_alquileres,
        resenas_como_arrendador=resenas_como_arrendador,
        resenas_como_arrendatario=resenas_como_arrendatario,
        resenas_arrendatarios=resenas_arrendatarios,
        perfil=perfil,
        metodos_pago=METODOS_PAGO,
        estados_pago=ESTADOS_PAGO,
        ciudades=ciudades,
        ciudad_seleccionada=ciudad_seleccionada,
        huespedes_seleccionado=huespedes_seleccionado
    )


@app.route('/vivienda/<int:cod_vi>/solicitar', methods=['POST'])
def solicitar_alquiler(cod_vi):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendatario' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_for('ver_vivienda', cod_vi=cod_vi)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT v.titulo, v.cod1_usu, " + ESTADO_VISIBLE_SQL + " FROM vivienda v WHERE v.cod_vi = %s", (cod_vi,))
    vivienda = cursor.fetchone()

    error = None
    if vivienda is None:
        error = 'Esta vivienda ya no está disponible.'
        volver = rutas_de_inicio(roles)[0]
    elif vivienda[1] == usuario_id:
        error = 'No puedes solicitar el alquiler de tu propia vivienda.'
    else:
        cursor.execute(
            "SELECT 1 FROM arriendo WHERE cod1_usu = %s AND cod1_vivienda = %s AND (fecha_fin IS NULL OR fecha_fin > CURRENT_DATE) LIMIT 1",
            (usuario_id, cod_vi)
        )
        if cursor.fetchone() is not None:
            error = 'Ya tienes un alquiler vigente en esta vivienda.'
        elif vivienda[2] != 'disponible':
            error = 'Esta vivienda ya no está disponible.'
            volver = rutas_de_inicio(roles)[0]

    if error:
        cursor.close()
        desconectar(conexion)
        flash(error, 'solicitud_error')
        return redirect(volver)

    try:
        cursor.execute(
            "INSERT INTO solicitud_alquiler (cod1_usu, cod1_vivienda) VALUES (%s, %s)",
            (usuario_id, cod_vi)
        )
        roles_dueno = obtener_roles_usuario(cursor, vivienda[1])
        correo = obtener_perfil(cursor, usuario_id)['correo']
        crear_notificacion(
            cursor,
            vivienda[1],
            f'{session["nombre"]} ({correo}) solicitó alquilar "{vivienda[0]}". Asóciala desde tu Historial.',
            url_historial_arrendador(roles_dueno),
            rol='arrendador'
        )
        conexion.commit()
    except psycopg2.errors.UniqueViolation:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        flash('Ya enviaste una solicitud para esta vivienda. El arrendador la revisará.', 'solicitud_error')
        return redirect(volver)

    cursor.close()
    desconectar(conexion)

    flash('Solicitud enviada. El arrendador recibió tu solicitud de alquiler.', 'solicitud_ok')
    return redirect(volver)


@app.route('/favorito/<int:cod_vi>', methods=['POST'])
def alternar_favorito(cod_vi):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendatario' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    destino = volver_a(request.form, rutas_de_inicio(roles)[0])

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT v.titulo, v.cod1_usu, " + ESTADO_VISIBLE_SQL + " FROM vivienda v WHERE v.cod_vi = %s", (cod_vi,))
    vivienda = cursor.fetchone()

    if vivienda is None or vivienda[1] == usuario_id:
        cursor.close()
        desconectar(conexion)
        return redirect(destino)

    cursor.execute(
        "SELECT cod_fav FROM favorito WHERE cod1_usu = %s AND cod1_vivienda = %s",
        (usuario_id, cod_vi)
    )
    existente = cursor.fetchone()

    if existente:
        cursor.execute(
            "DELETE FROM favorito WHERE cod1_usu = %s AND cod1_vivienda = %s",
            (usuario_id, cod_vi)
        )
    elif vivienda[2] == 'disponible':
        cursor.execute(
            "INSERT INTO favorito (cod1_usu, cod1_vivienda) VALUES (%s, %s)",
            (usuario_id, cod_vi)
        )

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    return redirect(destino)


@app.route('/notificacion/<int:cod_not>')
def abrir_notificacion(cod_not):
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        "UPDATE notificacion SET leida = TRUE WHERE cod_not = %s AND cod_usu = %s RETURNING enlace",
        (cod_not, session['usuario_id'])
    )
    fila = cursor.fetchone()
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    enlace = fila[0] if fila else None
    if not enlace or not enlace.startswith('/') or enlace.startswith('//') or '\\' in enlace:
        enlace = rutas_de_inicio(session.get('roles', []))[0]
    roles = session.get('roles', [])
    if 'arrendador' in roles and 'arrendatario' in roles:
        enlace = enlace.replace('/arrendador/menu#', '/menu-completo#ar-').replace('/arrendatario/menu#', '/menu-completo#at-')
    return redirect(enlace)


@app.route('/notificaciones/leer', methods=['POST'])
def leer_notificaciones():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    rol = request.form.get('rol', '')
    if rol not in ('arrendador', 'arrendatario'):
        rol = None

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        "UPDATE notificacion SET leida = TRUE "
        "WHERE cod_usu = %s AND leida = FALSE AND (%s IS NULL OR rol = %s OR rol IS NULL)",
        (session['usuario_id'], rol, rol)
    )
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    return redirect(volver_a(request.form, rutas_de_inicio(session.get('roles', []))[0]))


@app.route('/notificaciones/contador')
def contador_notificaciones():
    if 'usuario_id' not in session:
        return jsonify({'sin_leer': 0}), 401

    conexion = conectar()
    cursor = conexion.cursor()
    roles = session.get('roles', [])
    como_arrendador = contar_sin_leer(cursor, session['usuario_id'], 'arrendador') if 'arrendador' in roles else 0
    como_arrendatario = contar_sin_leer(cursor, session['usuario_id'], 'arrendatario') if 'arrendatario' in roles else 0
    cursor.close()
    desconectar(conexion)

    return jsonify({
        'sin_leer': como_arrendador + como_arrendatario,
        'arrendador': como_arrendador,
        'arrendatario': como_arrendatario
    })


@app.route('/arriendo/crear', methods=['POST'])
def crear_arriendo():
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_historial_arrendador(roles)

    try:
        cod_vi = int(request.form['cod_vi'])
        correo = request.form['correo'].strip()
        fecha_inicio = date.fromisoformat(request.form['fecha_inicio'])
        texto_fin = request.form.get('fecha_fin', '').strip()
        fecha_fin = date.fromisoformat(texto_fin) if texto_fin else None
    except (KeyError, ValueError):
        flash('Revisa los datos: elige la vivienda, escribe el correo del arrendatario y la fecha de inicio.', 'pago_error')
        return redirect(volver)

    if not correo or (fecha_fin is not None and fecha_fin < fecha_inicio):
        flash('Escribe el correo del arrendatario. La fecha de fin no puede ser anterior a la de inicio.', 'pago_error')
        return redirect(volver)

    conexion = conectar()
    cursor = conexion.cursor()

    cursor.execute("SELECT titulo FROM vivienda WHERE cod_vi = %s AND cod1_usu = %s", (cod_vi, usuario_id))
    vivienda = cursor.fetchone()

    cursor.execute(
        """
        SELECT u.cod_usu, u.nombre
        FROM usuario u
        JOIN rol_usuario r ON r.cod_usu = u.cod_usu
        WHERE u.correo = %s AND r.rol = 'arrendatario'
        """,
        (correo,)
    )
    arrendatario = cursor.fetchone()

    error = None
    if vivienda is None:
        error = 'Esa vivienda no es tuya.'
    elif arrendatario is None:
        error = 'No existe un arrendatario registrado con ese correo.'
    elif arrendatario[0] == usuario_id:
        error = 'No puedes asociarte a ti mismo como arrendatario.'
    else:
        cursor.execute(
            """
            SELECT cod1_usu FROM arriendo
            WHERE cod1_vivienda = %s
              AND fecha_inicio < COALESCE(%s, DATE 'infinity')
              AND COALESCE(fecha_fin, DATE 'infinity') > %s
            LIMIT 1
            """,
            (cod_vi, fecha_fin, fecha_inicio)
        )
        ocupante = cursor.fetchone()
        if ocupante is not None:
            error = (
                'Ese arrendatario ya está asociado a esa vivienda en esas fechas.'
                if ocupante[0] == arrendatario[0]
                else 'Esa vivienda ya está alquilada a otra persona en esas fechas.'
            )

    if error:
        cursor.close()
        desconectar(conexion)
        flash(error, 'pago_error')
        return redirect(volver)

    cursor.execute(
        "INSERT INTO arriendo (fecha_inicio, fecha_fin, estado_de_pago, cod1_usu, cod1_vivienda) VALUES (%s, %s, %s, %s, %s)",
        (fecha_inicio, fecha_fin, 'pendiente', arrendatario[0], cod_vi)
    )
    cursor.execute(
        "UPDATE solicitud_alquiler SET estado = 'aceptada' WHERE cod1_usu = %s AND cod1_vivienda = %s AND estado = 'pendiente'",
        (arrendatario[0], cod_vi)
    )
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(f'{arrendatario[1]} quedó asociado a "{vivienda[0]}". Ya puedes registrar sus pagos.', 'pago_ok')
    return redirect(volver)


@app.route('/arriendo/<int:cod_ar>/finalizar', methods=['POST'])
def finalizar_arriendo(cod_ar):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_historial_arrendador(roles)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        """
        UPDATE arriendo SET fecha_fin = CURRENT_DATE
        WHERE cod_ar = %s
          AND fecha_inicio <= CURRENT_DATE AND (fecha_fin IS NULL OR fecha_fin > CURRENT_DATE)
          AND cod1_vivienda IN (SELECT cod_vi FROM vivienda WHERE cod1_usu = %s)
        RETURNING cod1_usu, cod1_vivienda
        """,
        (cod_ar, usuario_id)
    )
    fila = cursor.fetchone()

    if fila is None:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        flash('No se pudo finalizar: el alquiler no es tuyo o no está activo.', 'pago_error')
        return redirect(volver)

    cursor.execute("SELECT titulo FROM vivienda WHERE cod_vi = %s", (fila[1],))
    titulo = cursor.fetchone()[0]
    roles_arrendatario = obtener_roles_usuario(cursor, fila[0])
    crear_notificacion(
        cursor,
        fila[0],
        f'Tu alquiler de "{titulo}" fue marcado como finalizado.',
        url_historial_arrendatario(roles_arrendatario),
        rol='arrendatario'
    )

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(f'El alquiler de "{titulo}" quedó finalizado. Ya pueden calificarse mutuamente.', 'pago_ok')
    return redirect(volver)


@app.route('/arriendo/<int:cod_ar>/desasociar', methods=['POST'])
def desasociar_arriendo(cod_ar):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_historial_arrendador(roles)

    conexion = conectar()
    cursor = conexion.cursor()
    tabla = tabla_resena(cursor)
    cursor.execute(
        sql.SQL(
            """
            SELECT a.cod1_usu, u.nombre, v.cod_vi, v.titulo, a.fecha_inicio, a.fecha_fin,
                   (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar),
                   (SELECT COUNT(*) FROM {} r WHERE r.cod1_ar = a.cod_ar)
            FROM arriendo a
            JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
            JOIN usuario u ON u.cod_usu = a.cod1_usu
            WHERE a.cod_ar = %s AND v.cod1_usu = %s
            """
        ).format(tabla),
        (cod_ar, usuario_id)
    )
    alquiler = cursor.fetchone()

    error = None
    if alquiler is None:
        error = 'No puedes desasociar ese alquiler.'
    else:
        cod_arrendatario, nombre_arrendatario, cod_vi, titulo, fecha_inicio, fecha_fin, pagos, resenas = alquiler
        estado = estado_alquiler(fecha_inicio, fecha_fin)
        if estado == 'Finalizado':
            error = 'Ese alquiler ya está finalizado.'

    if error:
        cursor.close()
        desconectar(conexion)
        flash(error, 'pago_error')
        return redirect(volver)

    if pagos == 0 and resenas == 0:
        cursor.execute("DELETE FROM arriendo WHERE cod_ar = %s", (cod_ar,))
        mensaje = f'{nombre_arrendatario} fue desasociado de "{titulo}".'
    else:
        if estado == 'Por iniciar':
            cursor.execute(
                "UPDATE arriendo SET fecha_inicio = CURRENT_DATE, fecha_fin = CURRENT_DATE WHERE cod_ar = %s",
                (cod_ar,)
            )
        else:
            cursor.execute("UPDATE arriendo SET fecha_fin = CURRENT_DATE WHERE cod_ar = %s", (cod_ar,))
        mensaje = f'{nombre_arrendatario} fue desasociado de "{titulo}". El alquiler quedó finalizado y conserva sus pagos y reseñas.'

    cursor.execute("SELECT " + ESTADO_VISIBLE_SQL + " FROM vivienda v WHERE v.cod_vi = %s", (cod_vi,))
    if cursor.fetchone()[0] == 'disponible':
        mensaje += ' La vivienda vuelve a estar disponible.'

    roles_arrendatario = obtener_roles_usuario(cursor, cod_arrendatario)
    crear_notificacion(
        cursor,
        cod_arrendatario,
        f'Tu arrendador te desasoció de "{titulo}".',
        url_historial_arrendatario(roles_arrendatario),
        rol='arrendatario'
    )

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(mensaje, 'pago_ok')
    return redirect(volver)


@app.route('/pago/registrar', methods=['POST'])
def registrar_pago():
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_historial_arrendador(roles)

    try:
        cod_ar = int(request.form['cod_ar'])
        fecha_pago = date.fromisoformat(request.form['fecha_pago'])
        monto = Decimal(request.form['monto'].strip())
        metodo_pago = request.form['metodo_pago']
        estado = request.form['estado']
    except (KeyError, ValueError, InvalidOperation):
        flash('Revisa los datos del pago: arriendo, fecha, monto, método y estado.', 'pago_error')
        return redirect(volver)

    if (not monto.is_finite() or monto <= 0 or monto >= Decimal('1000000000000')
            or metodo_pago not in METODOS_PAGO or estado not in ESTADOS_PAGO):
        flash('Revisa los datos del pago: el monto debe ser mayor a 0 y el método y el estado deben ser de la lista.', 'pago_error')
        return redirect(volver)

    monto = monto.quantize(Decimal('0.01'))

    conexion = conectar()
    cursor = conexion.cursor()

    cursor.execute(
        """
        SELECT a.cod1_usu, v.titulo
        FROM arriendo a
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        WHERE a.cod_ar = %s AND v.cod1_usu = %s
        """,
        (cod_ar, usuario_id)
    )
    arriendo = cursor.fetchone()

    if arriendo is None:
        cursor.close()
        desconectar(conexion)
        flash('No puedes registrar pagos en ese arriendo.', 'pago_error')
        return redirect(volver)

    cursor.execute(
        "INSERT INTO pago (fecha_pago, monto, metodo_pago, estado, cod1_ar) VALUES (%s, %s, %s, %s, %s)",
        (fecha_pago, monto, metodo_pago, estado, cod_ar)
    )
    actualizar_estado_pago_arriendo(cursor, cod_ar)

    roles_arrendatario = obtener_roles_usuario(cursor, arriendo[0])
    if 'arrendador' in roles_arrendatario and 'arrendatario' in roles_arrendatario:
        enlace = url_for('menu_completo') + '#at-historial'
    else:
        enlace = url_for('menu_arrendatario') + '#historial'
    crear_notificacion(
        cursor,
        arriendo[0],
        f'Tu arrendador registró un pago de {formato_cop(monto)} ({estado.lower()}) por "{arriendo[1]}".',
        enlace,
        rol='arrendatario'
    )

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash('Pago registrado correctamente.', 'pago_ok')
    return redirect(volver)


@app.route('/pago/<int:cod_pag>/estado', methods=['POST'])
def cambiar_estado_pago(cod_pag):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_historial_arrendador(roles)

    nuevo_estado = request.form.get('estado', '')
    if nuevo_estado not in ESTADOS_PAGO:
        flash('Estado de pago no válido.', 'pago_error')
        return redirect(volver)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT p.cod1_ar, a.cod1_usu, v.titulo, p.monto
        FROM pago p
        JOIN arriendo a ON a.cod_ar = p.cod1_ar
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        WHERE p.cod_pag = %s AND v.cod1_usu = %s
        """,
        (cod_pag, usuario_id)
    )
    pago = cursor.fetchone()

    if pago is None:
        cursor.close()
        desconectar(conexion)
        flash('No puedes modificar ese pago.', 'pago_error')
        return redirect(volver)

    cursor.execute("UPDATE pago SET estado = %s WHERE cod_pag = %s", (nuevo_estado, cod_pag))
    actualizar_estado_pago_arriendo(cursor, pago[0])

    roles_arrendatario = obtener_roles_usuario(cursor, pago[1])
    crear_notificacion(
        cursor,
        pago[1],
        f'Tu arrendador cambió el pago de {formato_cop(pago[3])} por "{pago[2]}" a {nuevo_estado.lower()}.',
        url_historial_arrendatario(roles_arrendatario),
        rol='arrendatario'
    )

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(f'El pago quedó como {nuevo_estado.lower()}.', 'pago_ok')
    return redirect(volver)


@app.route('/resena/crear', methods=['POST'])
def crear_resena():
    roles = session.get('roles', [])
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    como = request.form.get('como', 'arrendatario')
    volver = url_historial(roles, como)

    try:
        cod_ar = int(request.form['cod_ar'])
        calificacion = int(request.form['calificacion'])
    except (KeyError, ValueError):
        flash('Elige el alquiler y una calificación de 1 a 5.', 'resena_error')
        return redirect(volver)

    comentario = request.form.get('comentario', '').strip()
    if not 1 <= calificacion <= 5:
        flash('La calificación debe estar entre 1 y 5.', 'resena_error')
        return redirect(volver)
    if len(comentario) > 1000:
        flash('El comentario no puede pasar de 1000 caracteres.', 'resena_error')
        return redirect(volver)

    conexion = conectar()
    cursor = conexion.cursor()

    cursor.execute(
        """
        SELECT a.cod1_usu, v.cod1_usu, v.cod_vi, v.titulo, a.fecha_inicio, a.fecha_fin
        FROM arriendo a
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        WHERE a.cod_ar = %s
        """,
        (cod_ar,)
    )
    alquiler = cursor.fetchone()

    error = None
    if alquiler is None or usuario_id not in (alquiler[0], alquiler[1]):
        error = 'No puedes calificar ese alquiler.'
    else:
        cod_arrendatario, cod_dueno, cod_vi, titulo, fecha_inicio, fecha_fin = alquiler
        if usuario_id == cod_dueno:
            como = 'arrendador'
            cod_calificado = cod_arrendatario
        else:
            como = 'arrendatario'
            cod_calificado = cod_dueno
        volver = url_historial(roles, como)

        if estado_alquiler(fecha_inicio, fecha_fin) == 'Por iniciar':
            error = 'Podrás calificar cuando el alquiler haya iniciado.'
        else:
            tabla = tabla_resena(cursor)
            cursor.execute(
                sql.SQL("SELECT 1 FROM {} WHERE cod1_ar = %s AND cod1_usu = %s").format(tabla),
                (cod_ar, usuario_id)
            )
            if cursor.fetchone() is not None:
                error = 'Ya calificaste este alquiler.'

    if error:
        cursor.close()
        desconectar(conexion)
        flash(error, 'resena_error')
        return redirect(volver)

    try:
        cursor.execute(
            sql.SQL(
                "INSERT INTO {} (comentario, calificacion, fecha, cod1_usu, cod1_vivienda, cod1_ar) "
                "VALUES (%s, %s, CURRENT_DATE, %s, %s, %s)"
            ).format(tabla),
            (comentario, calificacion, usuario_id, cod_vi, cod_ar)
        )

        roles_calificado = obtener_roles_usuario(cursor, cod_calificado)
        como_calificado = 'arrendatario' if como == 'arrendador' else 'arrendador'
        crear_notificacion(
            cursor,
            cod_calificado,
            f'{session["nombre"]} te calificó con {calificacion}/5 por "{titulo}".',
            url_historial(roles_calificado, como_calificado),
            rol=como_calificado
        )
        conexion.commit()
    except psycopg2.errors.UniqueViolation:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        flash('Ya calificaste este alquiler.', 'resena_error')
        return redirect(volver)

    cursor.close()
    desconectar(conexion)

    flash('Reseña enviada correctamente.', 'resena_ok')
    return redirect(volver)


@app.route('/vivienda/<int:cod_vi>/editar', methods=['GET', 'POST'])
def editar_vivienda(cod_vi):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_mis_viviendas(roles)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM vivienda WHERE cod_vi = %s AND cod1_usu = %s", (cod_vi, usuario_id))
    fila = cursor.fetchone()

    if fila is None:
        cursor.close()
        desconectar(conexion)
        return redirect(volver)

    vivienda = fila_a_vivienda(fila)

    if request.method == 'POST':
        archivos = request.files.getlist('archivos')
        grande = video_demasiado_grande(archivos)
        if grande:
            cursor.close()
            desconectar(conexion)
            flash(f'El video "{grande}" supera el límite de {LIMITE_VIDEO_MB} MB. No se guardaron los cambios.', 'editar_error')
            return redirect(url_for('editar_vivienda', cod_vi=cod_vi))

        try:
            titulo = request.form['titulo'].strip()
            descripcion = request.form['descripcion'].strip()
            direccion = request.form['direccion'].strip()
            ciudad = request.form['ciudad'].strip()
            estrato = int(float(request.form['estrato']))
            precio = int(float(request.form['precio']))
            tipo_vivienda = request.form['tipo_vivienda']
            estado = request.form['estado']
            huespedes_max = entero_hasta_3_digitos(request.form.get('huespedes_max'), 1)
            habitaciones = entero_hasta_3_digitos(request.form.get('habitaciones'))
            banos = entero_hasta_3_digitos(request.form.get('banos'))
            numeros_validos = None not in (huespedes_max, habitaciones, banos)
            datos_validos = bool(
                titulo and direccion and ciudad
                and 1 <= estrato <= 6 and precio > 0
                and estado in ('disponible', 'no disponible', vivienda['estado'])
            )
        except (KeyError, ValueError):
            numeros_validos = True
            datos_validos = False

        if not numeros_validos:
            cursor.close()
            desconectar(conexion)
            flash(MENSAJE_3_DIGITOS + ' No se guardaron los cambios.', 'editar_error')
            return redirect(url_for('editar_vivienda', cod_vi=cod_vi))

        ids_eliminar = [int(x) for x in request.form.getlist('eliminar') if x.isdigit()]
        if ids_eliminar:
            cursor.execute(
                "SELECT COUNT(*) FROM foto_vivienda WHERE cod1_vivienda = %s AND cod_foto <> ALL(%s)",
                (cod_vi, ids_eliminar)
            )
            quedarian = cursor.fetchone()[0] + contar_archivos_validos(archivos)
            if quedarian == 0:
                cursor.close()
                desconectar(conexion)
                flash('La vivienda debe conservar al menos una foto o un video. Agrega uno nuevo antes de quitar los actuales.', 'editar_error')
                return redirect(url_for('editar_vivienda', cod_vi=cod_vi))

        if not datos_validos:
            cursor.close()
            desconectar(conexion)
            flash('Revisa los datos: título, dirección y ciudad son obligatorios, el estrato va de 1 a 6 y el precio y los huéspedes deben ser mayores a 0.', 'editar_error')
            return redirect(url_for('editar_vivienda', cod_vi=cod_vi))

        columna_banos = obtener_columna_banos(cursor)
        consulta = sql.SQL(
            "UPDATE vivienda SET titulo = %s, descripcion = %s, direccion = %s, ciudad = %s, estrato = %s, "
            "precio = %s, tipo_vivienda = %s, estado = %s, huespedes_max = %s, habitaciones = %s, {} = %s, "
            "wifi = %s, piscina = %s, cocina_equipada = %s, estacionamiento = %s, aire_acondicionado = %s, "
            "mascotas_permitidas = %s WHERE cod_vi = %s AND cod1_usu = %s"
        ).format(sql.Identifier(columna_banos))
        cursor.execute(consulta, (
            titulo, descripcion, direccion, ciudad, estrato,
            precio, tipo_vivienda, estado, huespedes_max, habitaciones, banos,
            'wifi' in request.form, 'piscina' in request.form, 'cocina_equipada' in request.form,
            'estacionamiento' in request.form, 'aire_acondicionado' in request.form,
            'mascotas_permitidas' in request.form, cod_vi, usuario_id
        ))

        urls_borrados = []
        if ids_eliminar:
            cursor.execute(
                "DELETE FROM foto_vivienda WHERE cod_foto = ANY(%s) AND cod1_vivienda = %s RETURNING url",
                (ids_eliminar, cod_vi)
            )
            urls_borrados = [f[0] for f in cursor.fetchall()]

        fotos, videos, omitidos = guardar_archivos(cursor, archivos, cod_vi)
        conexion.commit()
        cursor.close()
        desconectar(conexion)
        borrar_archivos(urls_borrados)

        mensaje = 'Vivienda actualizada correctamente.'
        if urls_borrados:
            mensaje += f' Se eliminaron {len(urls_borrados)} archivo(s).'
        if fotos or videos:
            mensaje += f' Se subieron {fotos} foto(s) y {videos} video(s).'
        flash(mensaje, 'vivienda_ok')
        if omitidos:
            flash('No se subieron por formato no permitido: ' + ', '.join(omitidos) + '.', 'vivienda_error')
        return redirect(volver)

    cursor.execute(
        "SELECT cod_foto, url FROM foto_vivienda WHERE cod1_vivienda = %s ORDER BY cod_foto",
        (cod_vi,)
    )
    archivos_actuales = [
        {'cod_foto': f[0], 'url': f[1], 'es_video': es_video(f[1])}
        for f in cursor.fetchall()
    ]
    cursor.close()
    desconectar(conexion)

    estados = ['disponible', 'no disponible']
    if vivienda['estado'] not in estados:
        estados.append(vivienda['estado'])

    url_inicio, url_publicar, url_perfil = rutas_de_inicio(roles)
    return render_template(
        'editar_vivienda.html',
        nombre=session['nombre'],
        vivienda=vivienda,
        archivos=archivos_actuales,
        estados=estados,
        volver=volver,
        url_inicio=url_inicio,
        url_publicar=url_publicar,
        url_perfil=url_perfil
    )


@app.route('/vivienda/<int:cod_vi>/eliminar', methods=['GET', 'POST'])
def eliminar_vivienda(cod_vi):
    roles = session.get('roles', [])
    if 'usuario_id' not in session or 'arrendador' not in roles:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    volver = url_mis_viviendas(roles)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM vivienda WHERE cod_vi = %s AND cod1_usu = %s", (cod_vi, usuario_id))
    fila = cursor.fetchone()

    if fila is None:
        cursor.close()
        desconectar(conexion)
        return redirect(volver)

    vivienda = fila_a_vivienda(fila)

    if request.method == 'POST':
        cursor.execute("SELECT url FROM foto_vivienda WHERE cod1_vivienda = %s", (cod_vi,))
        urls = [f[0] for f in cursor.fetchall()]
        try:
            cursor.execute("DELETE FROM foto_vivienda WHERE cod1_vivienda = %s", (cod_vi,))
            cursor.execute("DELETE FROM favorito WHERE cod1_vivienda = %s", (cod_vi,))
            cursor.execute("DELETE FROM mensaje WHERE cod_vi = %s", (cod_vi,))
            cursor.execute("DELETE FROM vivienda WHERE cod_vi = %s AND cod1_usu = %s", (cod_vi, usuario_id))
            crear_notificacion(cursor, usuario_id, f'Eliminaste la vivienda "{vivienda["titulo"]}".', rol='arrendador')
            conexion.commit()
        except psycopg2.errors.ForeignKeyViolation:
            conexion.rollback()
            cursor.close()
            desconectar(conexion)
            flash('No se puede eliminar esta vivienda porque tiene registros asociados (arriendos, reseñas, reportes o pagos).', 'vivienda_error')
            return redirect(volver)

        cursor.close()
        desconectar(conexion)
        borrar_archivos(urls)
        flash(f'La vivienda "{vivienda["titulo"]}" fue eliminada.', 'vivienda_ok')
        return redirect(volver)

    cursor.execute("SELECT COUNT(*) FROM foto_vivienda WHERE cod1_vivienda = %s", (cod_vi,))
    total_archivos = cursor.fetchone()[0]
    cursor.close()
    desconectar(conexion)

    url_inicio, url_publicar, url_perfil = rutas_de_inicio(roles)
    return render_template(
        'eliminar_vivienda.html',
        nombre=session['nombre'],
        vivienda=vivienda,
        total_archivos=total_archivos,
        url_inicio=url_inicio,
        url_publicar=url_publicar,
        url_perfil=url_perfil
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def enviar_correo_bienvenida(destinatario, nombre):
    mi_email = "soporterenthaouse@gmail.com"
    mi_password = "vkry cfnb jimw rfqv"

    try:
        email = MIMEMultipart()
        email['From'] = mi_email
        email['To'] = destinatario
        email['Subject'] = "¡Bienvenido a RentHouse!"

        cuerpo = f"""Hola {nombre},

¡Tu cuenta en RentHouse fue creada exitosamente!

Ya puedes iniciar sesión con tu correo y contraseña para empezar a explorar viviendas disponibles.

Gracias por unirte a RentHouse.
"""
        email.attach(MIMEText(cuerpo, 'plain'))

        print("Intentando conectar al servidor SMTP...")
        servidor = smtplib.SMTP('smtp.gmail.com', 587)
        servidor.starttls()
        print("Intentando login con:", mi_email)
        servidor.login(mi_email, mi_password)
        servidor.send_message(email)
        servidor.quit()

        print("Correo enviado correctamente a:", destinatario)
        return True

    except Exception as e:
        print(f"Error al enviar: {e}")
        return False


def generar_contraseña_temporal(longitud=10):
    caracteres = string.ascii_letters + string.digits
    return ''.join(random.choice(caracteres) for _ in range(longitud))


def enviar_correo_recuperacion(destinatario, contraseña_temporal):
    mi_email = "soporterenthaouse@gmail.com"
    mi_password = "vkry cfnb jimw rfqv"

    try:
        email = MIMEMultipart()
        email['From'] = mi_email
        email['To'] = destinatario
        email['Subject'] = "Recuperación de contraseña - RentHouse"

        cuerpo = f"""Hola,

Recibimos una solicitud para recuperar tu contraseña en RentHouse.

Tu contraseña temporal es: {contraseña_temporal}

Por seguridad, te recomendamos iniciar sesión y cambiarla lo antes posible.

Si no solicitaste este cambio, ignora este correo.
"""
        email.attach(MIMEText(cuerpo, 'plain'))

        servidor = smtplib.SMTP('smtp.gmail.com', 587)
        servidor.starttls()
        servidor.login(mi_email, mi_password)
        servidor.send_message(email)
        servidor.quit()

        return True

    except Exception as e:
        print(f"Error al enviar: {e}")
        return False


@app.route("/registro", methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form['nombre']
        fecha_nacimiento = request.form['fecha_nacimiento']
        tipo_identificacion = request.form['tipo_identificacion']
        identificacion = request.form['identificacion']
        ciudad = request.form['ciudad']
        correo = request.form['correo']
        telefono = request.form['telefono']
        roles = request.form.getlist('roles')
        contraseña = request.form['contraseña']
        contraseña = bcrypt.hashpw(contraseña.encode(), bcrypt.gensalt()).decode()

        if not roles:
            return render_template("registro.html", error="Debes seleccionar al menos un rol")

        nacimiento = date.fromisoformat(fecha_nacimiento)
        hoy = date.today()
        edad = hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))
        if edad < 18:
            return render_template("registro.html", error="Debes ser mayor de edad para registrarte")

        conexion = conectar()
        cursor = conexion.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO usuario
                VALUES (DEFAULT, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING cod_usu
                """,
                (nombre, identificacion, ciudad, correo, telefono,
                 contraseña, 'activo', tipo_identificacion, fecha_nacimiento)
            )
            cod_usu = cursor.fetchone()[0]

            for rol in roles:
                cursor.execute(
                    "INSERT INTO rol_usuario (cod_usu, rol) VALUES (%s, %s)",
                    (cod_usu, rol)
                )

            crear_notificacion(cursor, cod_usu, f'¡Bienvenido a RentHouse, {nombre}!', rol=roles[0] if len(roles) == 1 else None)

            conexion.commit()
            cursor.close()
            desconectar(conexion)
        except psycopg2.errors.UniqueViolation:
            conexion.rollback()
            cursor.close()
            desconectar(conexion)
            print("Registro falló: correo duplicado, no se intenta enviar correo")
            return render_template("registro.html", error="El correo ya ha sido usado")

        print("Registro guardado en la base de datos, intentando enviar correo a:", correo)
        enviar_correo_bienvenida(correo, nombre)

        return redirect(url_for('login'))

    return render_template("registro.html")


@app.route("/contraseña", methods=['GET', 'POST'])
def recuperar_contraseña():
    if request.method == 'POST':
        correo = request.form['correo']

        conexion = conectar()
        cursor = conexion.cursor()
        cursor.execute(
            "SELECT * FROM usuario WHERE correo = %s",
            (correo,)
        )
        resultado = cursor.fetchone()

        if resultado is None:
            cursor.close()
            desconectar(conexion)
            return render_template("contraseña.html", error="No existe una cuenta con ese correo")

        contraseña_temporal = generar_contraseña_temporal()
        contraseña_hash = bcrypt.hashpw(contraseña_temporal.encode(), bcrypt.gensalt()).decode()

        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'usuario' AND column_name LIKE 'contrase%'"
        )
        nombre_columna = cursor.fetchone()[0]

        query = sql.SQL("UPDATE usuario SET {} = %s WHERE correo = %s").format(
            sql.Identifier(nombre_columna)
        )
        cursor.execute(query, (contraseña_hash, correo))

        conexion.commit()
        cursor.close()
        desconectar(conexion)

        enviado = enviar_correo_recuperacion(correo, contraseña_temporal)

        if enviado:
            return render_template("contraseña.html", mensaje="Te enviamos una contraseña temporal a tu correo")
        else:
            return render_template("contraseña.html", error="No se pudo enviar el correo, intenta de nuevo")

    return render_template("contraseña.html")

@app.route('/perfil/actualizar', methods=['POST'])
def actualizar_perfil():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    roles = session.get('roles', [])
    usuario_id = session['usuario_id']

    destinos = {
        'arrendador': ('menu_arrendador', 'perfil', 'arrendador' in roles),
        'arrendatario': ('menu_arrendatario', 'perfil', 'arrendatario' in roles),
        'completo_at': ('menu_completo', 'at-perfil', 'arrendador' in roles and 'arrendatario' in roles),
        'completo_ar': ('menu_completo', 'ar-perfil', 'arrendador' in roles and 'arrendatario' in roles),
    }
    destino = normalizar_destino(request.form.get('destino'), roles)
    endpoint, ancla, permitido = destinos.get(destino, (None, None, False))
    volver = url_for(endpoint) + '#' + ancla if permitido else rutas_de_inicio(roles)[2]

    nombre = request.form.get('nombre', '').strip()
    correo = request.form.get('correo', '').strip()
    telefono = request.form.get('telefono', '').strip()
    ciudad = request.form.get('ciudad', '').strip()

    conexion = conectar()
    cursor = conexion.cursor()
    actual = obtener_perfil(cursor, usuario_id)

    error = None
    if not nombre or not correo or not telefono or not ciudad:
        error = 'Nombre, correo, teléfono y ciudad son obligatorios.'
    elif nombre != actual['nombre'] and len(nombre) > 100:
        error = 'El nombre no puede pasar de 100 caracteres.'
    elif correo != actual['correo'] and (len(correo) > 120 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', correo)):
        error = 'Escribe un correo válido, por ejemplo nombre@correo.com.'
    elif telefono != actual['telefono'] and not re.fullmatch(r'\+?[0-9][0-9 ()\-]{5,23}', telefono):
        error = 'Escribe un teléfono válido (solo números, espacios, guiones o paréntesis; puede empezar con +).'
    elif ciudad != actual['ciudad'] and len(ciudad) > 100:
        error = 'La ciudad no puede pasar de 100 caracteres.'
    elif correo != actual['correo']:
        cursor.execute(
            "SELECT 1 FROM usuario WHERE LOWER(correo) = LOWER(%s) AND cod_usu <> %s",
            (correo, usuario_id)
        )
        if cursor.fetchone() is not None:
            error = 'Ese correo ya está registrado en otra cuenta.'

    if error:
        cursor.close()
        desconectar(conexion)
        flash(error, 'perfil_error')
        return redirect(volver)

    try:
        cursor.execute(
            "UPDATE usuario SET nombre = %s, correo = %s, telefono = %s, ciudad = %s WHERE cod_usu = %s",
            (nombre, correo, telefono, ciudad, usuario_id)
        )
        crear_notificacion(cursor, usuario_id, 'Actualizaste los datos de tu perfil.')
        conexion.commit()
    except psycopg2.errors.UniqueViolation:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        flash('Ese correo ya está registrado en otra cuenta.', 'perfil_error')
        return redirect(volver)
    except psycopg2.DataError:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        flash('Alguno de los datos es demasiado largo para guardarse. Acórtalo e intenta de nuevo.', 'perfil_error')
        return redirect(volver)

    cursor.close()
    desconectar(conexion)

    session['nombre'] = nombre
    flash('Datos actualizados correctamente.', 'perfil_ok')
    return redirect(volver)


@app.route('/perfil/habilitar-rol', methods=['POST'])
def habilitar_rol():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    usuario_id = session['usuario_id']
    rol = request.form.get('rol', '')
    password_actual = request.form.get('password_actual', '')

    conexion = conectar()
    cursor = conexion.cursor()
    roles = obtener_roles_usuario(cursor, usuario_id)
    session['roles'] = roles
    volver = rutas_de_inicio(roles)[2]

    error = None
    categoria = 'rol_error'
    if rol not in ('arrendador', 'arrendatario'):
        error = 'El rol elegido no es válido.'
    elif rol in roles:
        error = 'Ya tienes ese rol habilitado.'
        categoria = 'perfil_error'
    else:
        cursor.execute("SELECT * FROM usuario WHERE cod_usu = %s", (usuario_id,))
        resultado = cursor.fetchone()
        if resultado is None or not bcrypt.checkpw(password_actual.encode(), resultado[6].encode()):
            error = 'La contraseña no es correcta.'

    if error:
        cursor.close()
        desconectar(conexion)
        flash(error, categoria)
        return redirect(volver)

    ancla_nueva = '#ar-publicar' if rol == 'arrendador' else '#at-buscar'
    try:
        cursor.execute(
            "INSERT INTO rol_usuario (cod_usu, rol) VALUES (%s, %s)",
            (usuario_id, rol)
        )
        crear_notificacion(
            cursor,
            usuario_id,
            f'Habilitaste tu rol de {rol}. Ya puedes usar ambos paneles.',
            url_for('menu_completo') + ancla_nueva,
            rol=rol
        )
        conexion.commit()
    except psycopg2.errors.UniqueViolation:
        conexion.rollback()
        session['roles'] = obtener_roles_usuario(cursor, usuario_id)
        cursor.close()
        desconectar(conexion)
        flash('Ya tienes ese rol habilitado.', 'perfil_error')
        return redirect(volver)

    session['roles'] = obtener_roles_usuario(cursor, usuario_id)
    cursor.close()
    desconectar(conexion)
    session.pop('rol_activo', None)

    flash(f'Rol de {rol} habilitado. Ahora tienes acceso a ambos paneles con la misma cuenta.', 'perfil_ok')
    return redirect(url_for('menu_completo') + ('#ar-perfil' if rol == 'arrendador' else '#at-perfil'))


@app.route('/cambiar-password', methods=['POST'])
def cambiar_password():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    destinos = {
        'arrendador': ('menu_arrendador', 'perfil'),
        'arrendatario': ('menu_arrendatario', 'perfil'),
        'completo_at': ('menu_completo', 'at-perfil'),
        'completo_ar': ('menu_completo', 'ar-perfil'),
        'admin': ('menu_admin', 'cuenta'),
    }
    roles = session.get('roles', [])
    destino = normalizar_destino(request.form.get('destino'), roles)
    if 'administrador' in roles:
        por_defecto = ('menu_admin', 'cuenta')
    elif 'arrendador' in roles and 'arrendatario' in roles:
        por_defecto = ('menu_completo', 'at-perfil')
    else:
        por_defecto = ('menu_arrendatario', 'perfil')
    endpoint, ancla = destinos.get(destino, por_defecto)
    volver = url_for(endpoint) + '#' + ancla

    password_actual = request.form['password_actual']
    password_nueva = request.form['password_nueva']
    password_confirmar = request.form['password_confirmar']

    if password_nueva != password_confirmar:
        flash('Las contraseñas nuevas no coinciden.', 'error')
        return redirect(volver)

    patron = r'(?=.*[a-z])(?=.*[A-Z])(?=.*[\d!@#$%^&*()_+\-={}[\]|:;,.?]).{8,}'
    if not re.fullmatch(patron, password_nueva):
        flash('La contraseña nueva debe tener mínimo 8 caracteres, con una mayúscula, una minúscula y un número o símbolo.', 'error')
        return redirect(volver)

    if password_nueva == password_actual:
        flash('La contraseña nueva no puede ser igual a la actual.', 'error')
        return redirect(volver)

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        "SELECT * FROM usuario WHERE cod_usu = %s",
        (session['usuario_id'],)
    )
    resultado = cursor.fetchone()

    if resultado is None or not bcrypt.checkpw(password_actual.encode(), resultado[6].encode()):
        cursor.close()
        desconectar(conexion)
        flash('La contraseña actual no es correcta.', 'error')
        return redirect(volver)

    password_hash = bcrypt.hashpw(password_nueva.encode(), bcrypt.gensalt()).decode()

    cursor.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'usuario' AND column_name LIKE 'contrase%'"
    )
    nombre_columna = cursor.fetchone()[0]

    query = sql.SQL("UPDATE usuario SET {} = %s WHERE cod_usu = %s").format(
        sql.Identifier(nombre_columna)
    )
    cursor.execute(query, (password_hash, session['usuario_id']))
    crear_notificacion(cursor, session['usuario_id'], 'Cambiaste tu contraseña.')

    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash('Contraseña actualizada correctamente.', 'ok')
    return redirect(volver)


# ============================================================
# PANEL DE ADMINISTRADOR
# ============================================================

MESES_ABREV = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']


def es_admin():
    return 'usuario_id' in session and 'administrador' in session.get('roles', [])


def asegurar_tablas_admin():
    conexion = conectar()
    cursor = conexion.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_log (
                cod_log SERIAL PRIMARY KEY,
                cod_admin INTEGER REFERENCES usuario(cod_usu) ON DELETE SET NULL,
                accion VARCHAR(60) NOT NULL,
                detalle TEXT,
                fecha TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        conexion.commit()
        # La tabla original solo aceptaba 'arrendador' y 'arrendatario'
        cursor.execute("ALTER TABLE rol_usuario DROP CONSTRAINT IF EXISTS rol_usuario_rol_check")
        cursor.execute(
            "ALTER TABLE rol_usuario ADD CONSTRAINT rol_usuario_rol_check "
            "CHECK (rol IN ('arrendador', 'arrendatario', 'administrador')) NOT VALID"
        )
        conexion.commit()
    except psycopg2.Error as error:
        conexion.rollback()
        print("No se pudo preparar la base de datos para el administrador:", error)
    finally:
        cursor.close()
        desconectar(conexion)


asegurar_tablas_admin()


def registrar_actividad(cursor, accion, detalle=None):
    cursor.execute(
        "INSERT INTO admin_log (cod_admin, accion, detalle) VALUES (%s, %s, %s)",
        (session['usuario_id'], accion, detalle)
    )


def volver_admin(ancla):
    return redirect(url_for('menu_admin') + '#' + ancla)


@app.route('/admin')
def admin_inicio():
    return redirect(url_for('menu_admin'))


@app.route('/admin/menu')
def menu_admin():
    if not es_admin():
        return redirect(url_for('login'))

    conexion = conectar()
    cursor = conexion.cursor()
    tabla = tabla_resena(cursor)

    # Usuarios
    cursor.execute(
        """
        SELECT u.cod_usu, u.nombre, u.correo, u.ciudad, u.estado_cuenta,
               ARRAY_REMOVE(ARRAY_AGG(r.rol ORDER BY r.rol), NULL)
        FROM usuario u
        LEFT JOIN rol_usuario r ON r.cod_usu = u.cod_usu
        GROUP BY u.cod_usu
        ORDER BY u.cod_usu DESC
        """
    )
    usuarios = [
        {
            'cod_usu': f[0], 'nombre': f[1], 'correo': f[2], 'ciudad': f[3],
            'estado': f[4] or 'activo', 'roles': f[5] or [],
            'es_admin': 'administrador' in (f[5] or [])
        }
        for f in cursor.fetchall()
    ]

    # Viviendas
    cursor.execute(
        "SELECT v.cod_vi, v.titulo, v.ciudad, v.precio, u.nombre, " + ESTADO_VISIBLE_SQL + ", v.estado "
        "FROM vivienda v JOIN usuario u ON u.cod_usu = v.cod1_usu ORDER BY v.cod_vi DESC"
    )
    viviendas = [
        {'cod_vi': f[0], 'titulo': f[1], 'ciudad': f[2], 'precio': f[3],
         'dueno': f[4], 'estado': f[5], 'estado_real': f[6]}
        for f in cursor.fetchall()
    ]

    # Alquileres
    cursor.execute(
        """
        SELECT a.cod_ar, v.titulo, ua.nombre, ut.nombre, a.fecha_inicio, a.fecha_fin,
               (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pagado'),
               (SELECT COUNT(*) FROM pago p WHERE p.cod1_ar = a.cod_ar AND p.estado = 'Pendiente')
        FROM arriendo a
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        JOIN usuario ua ON ua.cod_usu = v.cod1_usu
        JOIN usuario ut ON ut.cod_usu = a.cod1_usu
        ORDER BY a.cod_ar DESC
        """
    )
    alquileres = [
        {
            'cod_ar': f[0], 'titulo': f[1], 'arrendador': f[2], 'arrendatario': f[3],
            'fecha_inicio': f[4], 'fecha_fin': f[5],
            'estado_alquiler': estado_alquiler(f[4], f[5]),
            'estado_pago': estado_pago_alquiler(f[6], f[7]).lower(),
            'pagos': f[6] + f[7]
        }
        for f in cursor.fetchall()
    ]

    # Pagos
    cursor.execute(
        """
        SELECT v.titulo, u.nombre, p.fecha_pago, p.monto, p.metodo_pago, p.estado
        FROM pago p
        JOIN arriendo a ON a.cod_ar = p.cod1_ar
        JOIN vivienda v ON v.cod_vi = a.cod1_vivienda
        JOIN usuario u ON u.cod_usu = a.cod1_usu
        WHERE p.fecha_pago IS NOT NULL
        ORDER BY p.fecha_pago DESC, p.cod_pag DESC
        LIMIT 200
        """
    )
    pagos = [
        {'titulo': f[0], 'arrendatario': f[1], 'fecha_pago': f[2],
         'monto': f[3], 'metodo_pago': f[4], 'estado': f[5]}
        for f in cursor.fetchall()
    ]

    # Reseñas
    cursor.execute(
        sql.SQL(
            """
            SELECT r.cod_re, r.fecha, u.nombre, v.titulo, r.calificacion, r.comentario
            FROM {} r
            LEFT JOIN usuario u ON u.cod_usu = r.cod1_usu
            LEFT JOIN vivienda v ON v.cod_vi = r.cod1_vivienda
            ORDER BY r.cod_re DESC
            """
        ).format(tabla)
    )
    resenas = [
        {'cod_re': f[0], 'fecha': f[1], 'autor': f[2] or '—', 'titulo': f[3],
         'calificacion': f[4], 'comentario': f[5]}
        for f in cursor.fetchall()
    ]

    # Actividad del administrador
    cursor.execute(
        """
        SELECT l.fecha, COALESCE(u.nombre, '(cuenta eliminada)'), l.accion, l.detalle
        FROM admin_log l
        LEFT JOIN usuario u ON u.cod_usu = l.cod_admin
        ORDER BY l.fecha DESC, l.cod_log DESC
        LIMIT 50
        """
    )
    actividad = [
        {'fecha': f[0], 'admin': f[1], 'accion': f[2], 'detalle': f[3]}
        for f in cursor.fetchall()
    ]

    # Resumen
    cursor.execute("SELECT COUNT(*) FROM solicitud_alquiler WHERE estado = 'pendiente'")
    solicitudes_pendientes = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COALESCE(SUM(monto) FILTER (WHERE estado = 'Pagado'), 0),
               COUNT(*) FILTER (WHERE estado = 'Pendiente')
        FROM pago
        WHERE fecha_pago IS NOT NULL
          AND date_trunc('month', fecha_pago) = date_trunc('month', CURRENT_DATE)
        """
    )
    pagos_mes, pagos_pendientes_mes = cursor.fetchone()

    cursor.execute(
        """
        SELECT date_trunc('month', fecha_pago)::date, SUM(monto)
        FROM pago
        WHERE estado = 'Pagado' AND fecha_pago IS NOT NULL
          AND fecha_pago >= date_trunc('month', CURRENT_DATE) - INTERVAL '5 months'
        GROUP BY 1
        """
    )
    por_mes = {(f[0].year, f[0].month): f[1] for f in cursor.fetchall()}

    hoy = date.today()
    meses = []
    anio, mes = hoy.year, hoy.month
    for _ in range(6):
        meses.append((anio, mes))
        mes -= 1
        if mes == 0:
            anio, mes = anio - 1, 12
    meses.reverse()
    totales = [por_mes.get(m, 0) for m in meses]
    maximo = max(totales) if totales and max(totales) > 0 else 0
    barras = [
        {
            'etiqueta': f"{MESES_ABREV[m[1] - 1]} {m[0]}",
            'total': t,
            'pct': round(float(t) / float(maximo) * 100) if maximo else 0
        }
        for m, t in zip(meses, totales)
    ]

    cursor.execute(
        sql.SQL("SELECT COUNT(*), AVG(calificacion) FROM {}").format(tabla)
    )
    total_resenas, promedio = cursor.fetchone()

    cursor.close()
    desconectar(conexion)

    resumen = {
        'usuarios_activos': sum(1 for u in usuarios if u['estado'] != 'inactivo'),
        'usuarios_inactivos': sum(1 for u in usuarios if u['estado'] == 'inactivo'),
        'arrendadores': sum(1 for u in usuarios if 'arrendador' in u['roles']),
        'arrendatarios': sum(1 for u in usuarios if 'arrendatario' in u['roles']),
        'ambos': sum(1 for u in usuarios if 'arrendador' in u['roles'] and 'arrendatario' in u['roles']),
        'viviendas': len(viviendas),
        'viviendas_disponibles': sum(1 for v in viviendas if v['estado'] == 'disponible'),
        'viviendas_alquiladas': sum(1 for v in viviendas if v['estado'] == 'alquilada'),
        'viviendas_ocultas': sum(1 for v in viviendas if v['estado'] == 'no disponible'),
        'alquileres_activos': sum(1 for a in alquileres if a['estado_alquiler'] == 'Activo'),
        'solicitudes_pendientes': solicitudes_pendientes,
        'pagos_mes': pagos_mes,
        'pagos_pendientes_mes': pagos_pendientes_mes,
        'resenas': total_resenas,
        'promedio_resenas': round(float(promedio), 1) if promedio is not None else None,
        'barras': barras
    }

    return render_template(
        'admin/menu.html',
        nombre=session['nombre'],
        yo=session['usuario_id'],
        resumen=resumen,
        usuarios=usuarios,
        viviendas=viviendas,
        alquileres=alquileres,
        pagos=pagos,
        resenas=resenas,
        actividad=actividad
    )


@app.route('/admin/usuario/<int:cod_usu>/estado', methods=['POST'])
def admin_cambiar_estado_usuario(cod_usu):
    if not es_admin():
        return redirect(url_for('login'))

    if cod_usu == session['usuario_id']:
        flash('No puedes desactivar tu propia cuenta.', 'admin_error')
        return volver_admin('usuarios')

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute(
        "SELECT u.nombre, u.estado_cuenta, "
        "EXISTS (SELECT 1 FROM rol_usuario r WHERE r.cod_usu = u.cod_usu AND r.rol = 'administrador') "
        "FROM usuario u WHERE u.cod_usu = %s",
        (cod_usu,)
    )
    fila = cursor.fetchone()

    if fila is None:
        cursor.close()
        desconectar(conexion)
        flash('Esa cuenta no existe.', 'admin_error')
        return volver_admin('usuarios')

    nombre, estado, es_otro_admin = fila
    if es_otro_admin:
        cursor.close()
        desconectar(conexion)
        flash('No puedes cambiar el estado de otro administrador.', 'admin_error')
        return volver_admin('usuarios')

    nuevo = 'activo' if estado == 'inactivo' else 'inactivo'
    cursor.execute("UPDATE usuario SET estado_cuenta = %s WHERE cod_usu = %s", (nuevo, cod_usu))
    registrar_actividad(
        cursor,
        'Reactivar usuario' if nuevo == 'activo' else 'Desactivar usuario',
        f'{nombre} (código {cod_usu})'
    )
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(f'La cuenta de {nombre} fue {"reactivada" if nuevo == "activo" else "desactivada"}.', 'admin_ok')
    return volver_admin('usuarios')


@app.route('/admin/vivienda/<int:cod_vi>/estado', methods=['POST'])
def admin_cambiar_estado_vivienda(cod_vi):
    if not es_admin():
        return redirect(url_for('login'))

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT titulo, estado, cod1_usu FROM vivienda WHERE cod_vi = %s", (cod_vi,))
    fila = cursor.fetchone()

    if fila is None:
        cursor.close()
        desconectar(conexion)
        flash('Esa vivienda no existe.', 'admin_error')
        return volver_admin('viviendas')

    titulo, estado, dueno = fila
    oculta_ahora = estado != 'disponible'
    nuevo = 'disponible' if oculta_ahora else 'no disponible'
    cursor.execute("UPDATE vivienda SET estado = %s WHERE cod_vi = %s", (nuevo, cod_vi))
    registrar_actividad(
        cursor,
        'Mostrar vivienda' if nuevo == 'disponible' else 'Ocultar vivienda',
        f'{titulo} (código {cod_vi})'
    )
    crear_notificacion(
        cursor, dueno,
        f'Un administrador {"volvió a publicar" if nuevo == "disponible" else "ocultó"} tu vivienda "{titulo}".',
        rol='arrendador'
    )
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(f'La vivienda "{titulo}" ahora está {"visible" if nuevo == "disponible" else "oculta"}.', 'admin_ok')
    return volver_admin('viviendas')


@app.route('/admin/vivienda/<int:cod_vi>/eliminar', methods=['POST'])
def admin_eliminar_vivienda(cod_vi):
    if not es_admin():
        return redirect(url_for('login'))

    conexion = conectar()
    cursor = conexion.cursor()
    cursor.execute("SELECT titulo, cod1_usu FROM vivienda WHERE cod_vi = %s", (cod_vi,))
    fila = cursor.fetchone()

    if fila is None:
        cursor.close()
        desconectar(conexion)
        flash('Esa vivienda no existe.', 'admin_error')
        return volver_admin('viviendas')

    titulo, dueno = fila
    cursor.execute("SELECT url FROM foto_vivienda WHERE cod1_vivienda = %s", (cod_vi,))
    urls = [f[0] for f in cursor.fetchall()]

    try:
        cursor.execute("DELETE FROM foto_vivienda WHERE cod1_vivienda = %s", (cod_vi,))
        cursor.execute("DELETE FROM favorito WHERE cod1_vivienda = %s", (cod_vi,))
        cursor.execute("DELETE FROM mensaje WHERE cod_vi = %s", (cod_vi,))
        cursor.execute("DELETE FROM vivienda WHERE cod_vi = %s", (cod_vi,))
        registrar_actividad(cursor, 'Eliminar vivienda', f'{titulo} (código {cod_vi})')
        crear_notificacion(
            cursor, dueno,
            f'Un administrador eliminó tu vivienda "{titulo}".',
            rol='arrendador'
        )
        conexion.commit()
    except psycopg2.errors.ForeignKeyViolation:
        conexion.rollback()
        cursor.close()
        desconectar(conexion)
        flash('No se puede eliminar: la vivienda tiene arriendos, reseñas, reportes o pagos asociados. '
              'Mejor ocúltala.', 'admin_error')
        return volver_admin('viviendas')

    cursor.close()
    desconectar(conexion)
    borrar_archivos(urls)

    flash(f'La vivienda "{titulo}" fue eliminada.', 'admin_ok')
    return volver_admin('viviendas')


@app.route('/admin/resena/<int:cod_re>/eliminar', methods=['POST'])
def admin_eliminar_resena(cod_re):
    if not es_admin():
        return redirect(url_for('login'))

    conexion = conectar()
    cursor = conexion.cursor()
    tabla = tabla_resena(cursor)
    cursor.execute(
        sql.SQL(
            "SELECT r.cod1_usu, u.nombre, v.titulo FROM {} r "
            "LEFT JOIN usuario u ON u.cod_usu = r.cod1_usu "
            "LEFT JOIN vivienda v ON v.cod_vi = r.cod1_vivienda "
            "WHERE r.cod_re = %s"
        ).format(tabla),
        (cod_re,)
    )
    fila = cursor.fetchone()

    if fila is None:
        cursor.close()
        desconectar(conexion)
        flash('Esa reseña no existe.', 'admin_error')
        return volver_admin('resenas')

    autor_id, autor, titulo = fila
    cursor.execute(sql.SQL("DELETE FROM {} WHERE cod_re = %s").format(tabla), (cod_re,))
    registrar_actividad(cursor, 'Eliminar reseña', f'Reseña {cod_re} de {autor or "?"} sobre {titulo or "un usuario"}')
    if autor_id is not None:
        crear_notificacion(cursor, autor_id, 'Un administrador eliminó una de tus reseñas.')
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash('La reseña fue eliminada.', 'admin_ok')
    return volver_admin('resenas')


@app.route('/admin/anuncio', methods=['POST'])
def admin_enviar_anuncio():
    if not es_admin():
        return redirect(url_for('login'))

    destino = request.form.get('destino', '')
    mensaje = (request.form.get('mensaje') or '').strip()

    if destino not in ('todos', 'arrendadores', 'arrendatarios'):
        flash('Selecciona a quién enviar el anuncio.', 'admin_error')
        return volver_admin('anuncios')
    if not mensaje or len(mensaje) > 200:
        flash('El mensaje es obligatorio y de máximo 200 caracteres.', 'admin_error')
        return volver_admin('anuncios')

    conexion = conectar()
    cursor = conexion.cursor()

    if destino == 'todos':
        cursor.execute(
            "SELECT u.cod_usu FROM usuario u WHERE COALESCE(u.estado_cuenta, 'activo') <> 'inactivo' "
            "AND NOT EXISTS (SELECT 1 FROM rol_usuario r WHERE r.cod_usu = u.cod_usu AND r.rol = 'administrador')"
        )
        rol_notificacion = None
    else:
        rol = 'arrendador' if destino == 'arrendadores' else 'arrendatario'
        cursor.execute(
            "SELECT DISTINCT u.cod_usu FROM usuario u JOIN rol_usuario r ON r.cod_usu = u.cod_usu "
            "WHERE r.rol = %s AND COALESCE(u.estado_cuenta, 'activo') <> 'inactivo'",
            (rol,)
        )
        rol_notificacion = rol

    destinatarios = [f[0] for f in cursor.fetchall()]
    for cod_usu in destinatarios:
        crear_notificacion(cursor, cod_usu, '📣 Anuncio: ' + mensaje, rol=rol_notificacion)

    registrar_actividad(cursor, 'Enviar anuncio', f'A {destino} ({len(destinatarios)} usuarios): {mensaje}')
    conexion.commit()
    cursor.close()
    desconectar(conexion)

    flash(f'Anuncio enviado a {len(destinatarios)} usuario(s).', 'admin_ok')
    return volver_admin('anuncios')


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
