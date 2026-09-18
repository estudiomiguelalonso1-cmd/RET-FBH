# -*- coding: utf-8 -*-
"""Interfaz web local del sistema de retenciones.

    python app.py          y abrir http://127.0.0.1:5000

Pantalla principal: se sube la factura y queda el panel doble del diseño — la
factura a la izquierda, la propuesta editable a la derecha. Nada se guarda hasta
que alguien confirma.

Corre solo en 127.0.0.1: es una herramienta de escritorio, no un servidor
expuesto. La base y los PDF quedan en esta misma carpeta.
"""
import sqlite3
import traceback
from pathlib import Path

from flask import (Flask, abort, redirect, render_template, request,
                   send_from_directory, url_for)

import calcular
import certificados
import leer_factura
import procesar

DIR = Path(__file__).resolve().parent
ENTRADA = DIR / "entrada"          # facturas subidas
SALIDA = DIR / "certificados"

app = Flask(__name__, template_folder=str(DIR / "plantillas_web"))
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def conectar():
    conn = sqlite3.connect(DIR / "retenciones.db")
    conn.row_factory = sqlite3.Row
    return conn


def provisorio(f):
    """Proveedor armado con los datos de la factura, para poder cotizar antes del alta."""
    cuit = f["emisor"]["cuit"] or ""
    return {"razon_social": f["emisor"]["razon_social"],
            "situacion_ganancias": "I",
            "tipo_persona": "H" if cuit[:2] in ("20", "23", "24", "27") else "J",
            "retiene_iva_3164": 0, "retiene_suss_1556": 0}


def plata(v):
    return f"{v:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


app.jinja_env.filters["plata"] = plata


@app.route("/")
def bandeja():
    conn = conectar()
    try:
        recientes = conn.execute(
            "SELECT c.id, c.fecha, c.punto_venta, c.numero, c.neto, p.razon_social, "
            "       COUNT(r.id) AS retenciones, "
            "       COALESCE(SUM(r.monto), 0) AS total_retenido "
            "FROM comprobantes c JOIN proveedores p ON p.cuit = c.cuit "
            "LEFT JOIN retenciones r ON r.comprobante_id = c.id "
            "GROUP BY c.id ORDER BY c.id DESC LIMIT 15").fetchall()
        pendientes = sorted(ENTRADA.glob("*.pdf")) if ENTRADA.exists() else []
        avisos = []
        ultimo = conn.execute(
            "SELECT MAX(vigencia_desde) FROM padron_iibb_caba").fetchone()[0]
        hoy = calcular.date.today().isoformat()
        # AGIP publica con un mes de atraso: eso es lo normal y no se avisa.
        if not ultimo:
            avisos.append("No hay ningún padrón de AGIP cargado. "
                          "Cargalo con: python padron_agip.py --descargar")
        elif calcular.meses_de_atraso(ultimo, hoy[:7]) >= 2:
            avisos.append(f"El padrón de AGIP más nuevo es de {ultimo[:7]}. "
                          f"Actualizalo con: python padron_agip.py --descargar")
        return render_template("bandeja.html", recientes=recientes,
                               pendientes=pendientes, avisos=avisos)
    finally:
        conn.close()


@app.route("/subir", methods=["POST"])
def subir():
    archivo = request.files.get("factura")
    if not archivo or not archivo.filename.lower().endswith(".pdf"):
        return redirect(url_for("bandeja"))
    ENTRADA.mkdir(exist_ok=True)
    nombre = Path(archivo.filename).name
    archivo.save(ENTRADA / nombre)
    return redirect(url_for("revisar", nombre=nombre))


@app.route("/factura/<path:nombre>")
def factura_pdf(nombre):
    return send_from_directory(ENTRADA, nombre)


@app.route("/certificado/<path:nombre>")
def certificado_pdf(nombre):
    return send_from_directory(SALIDA, nombre)


@app.route("/revisar/<path:nombre>")
def revisar(nombre):
    pdf = ENTRADA / Path(nombre).name
    if not pdf.exists():
        abort(404)
    try:
        f = leer_factura.leer(pdf)
    except Exception:
        return render_template("error.html", nombre=nombre,
                               detalle=traceback.format_exc())
    avisos = leer_factura.controles(f)

    conn = conectar()
    try:
        cuit = f["emisor"]["cuit"]
        proveedor = conn.execute(
            "SELECT * FROM proveedores WHERE cuit = ?", (cuit,)).fetchone()
        regimen = request.args.get("regimen", type=int) or procesar.regimen_habitual(conn, cuit)
        fecha_pago = request.args.get("fecha_pago") or calcular.date.today().isoformat()
        neto = request.args.get("neto", type=float) or f["importes"]["neto_gravado"]

        calculo = None
        if regimen and neto:
            calculo = calcular.calcular(conn, cuit, neto, regimen, fecha_pago,
                                        proveedor_provisorio=provisorio(f))
        regimenes = conn.execute(
            "SELECT cod_regimen, concepto FROM regimenes_ganancias "
            "WHERE situacion = 'I' AND tipo_persona = '' ORDER BY cod_regimen").fetchall()
        ya_cargada = conn.execute(
            "SELECT id FROM comprobantes WHERE cuit = ? AND punto_venta = ? AND numero = ?",
            (cuit, f["punto_venta"], f["numero"])).fetchone()
        return render_template(
            "revisar.html", nombre=nombre, f=f, avisos=avisos, proveedor=proveedor,
            regimen=regimen, regimenes=regimenes, fecha_pago=fecha_pago, neto=neto,
            calculo=calculo, ya_cargada=ya_cargada)
    finally:
        conn.close()


@app.route("/confirmar", methods=["POST"])
def confirmar():
    nombre = request.form["nombre"]
    pdf = ENTRADA / Path(nombre).name
    f = leer_factura.leer(pdf)
    regimen = int(request.form["regimen"])
    fecha_pago = request.form["fecha_pago"]
    neto = float(request.form["neto"])
    f["importes"]["neto_gravado"] = neto

    conn = conectar()
    try:
        conn.execute("BEGIN")
        cambios, _ = procesar.sincronizar_proveedor(conn, f, True)
        calculo = calcular.calcular(conn, f["emisor"]["cuit"], neto, regimen, fecha_pago,
                                    proveedor_provisorio=provisorio(f))
        cid, emitidos = procesar.guardar(conn, f, calculo, fecha_pago)
        rutas = []
        for _, nro in emitidos:
            filas = certificados.retenciones(conn, certificado=nro)
            generados, _sin = certificados.emitir(conn, filas)
            rutas += [r.name for r in generados]
        conn.commit()
    except Exception:
        conn.rollback()
        return render_template("error.html", nombre=nombre,
                               detalle=traceback.format_exc())
    finally:
        conn.close()
    return render_template("listo.html", cid=cid, calculo=calculo, rutas=rutas,
                           cambios=cambios, nombre=nombre)


if __name__ == "__main__":
    ENTRADA.mkdir(exist_ok=True)
    print("Sistema de retenciones -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
