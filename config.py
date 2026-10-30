import os
import psycopg2

def conectar():
    try:
        url = os.environ.get("DATABASE_URL")
        if url:
            conexion = psycopg2.connect(url, client_encoding="utf8")
        else:
            conexion = psycopg2.connect(
                user="postgres",
                password="xxx",
                host="localhost",
                port="5432",
                database="alquiler",
                client_encoding="utf8"
            )
        return conexion
    except (Exception, psycopg2.Error) as error:
        print ("Error al conectar a postgreSQL: ", error)

def desconectar(conexion):
    if conexion is not None:
        conexion.close()
        print("Conexión a PostgreSQl cerrada")

conexion = conectar()


if conexion is not None:
    print("Conexión a PostgreSQL establecida correctamente")
else:
    print("Error al conectar a PostgreSQL. No se pudo establecer la conexión.")
