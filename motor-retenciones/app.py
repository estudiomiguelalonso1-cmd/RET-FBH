# -*- coding: utf-8 -*-
"""Interfaz web local del sistema de retenciones.

    python app.py          y abrir http://127.0.0.1:5000

Pantalla principal: se sube la factura y queda el panel doble del diseño — la
factura a la izquierda, la propuesta editable a la derecha. Nada se guarda hasta
que alguien confirma.

Todo lo que el lector saca de la factura se puede corregir a mano antes de
confirmar, y cada impuesto se puede desactivar para esa factura en particular.

Corre solo en 127.0.0.1: es una herramienta de escritorio, no un servidor
expuesto. La base y los PDF quedan en esta misma carpeta.
"""
import io
import shutil
import sqlite3
import traceback
from datetime import date as _date
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from flask import (Flask, abort, g, redirect, render_template, request, send_file,
                   send_from_directory, url_for)

import calcular
import certificados
import clientes
import leer_factura
import padron_agip
import procesar

DIR = Path(__file__).resolve().parent
# Las carpetas y la base dependen del cliente con el que se esta trabajando: cada
# ruta de cliente cuelga de /c/<cliente>/ y g.cliente apunta a su carpeta.

app = Flask(__name__, template_folder=str(DIR / "plantillas_web"))
# el padron de AGIP descomprimido pesa unos 135 MB
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024

# Clave estable de cada linea del calculo, para poder apagarla desde la pantalla.
IMPUESTO_CLAVE = {"Ganancias": "ganancias", "IIBB CABA": "iibb_caba",
                  "IVA": "iva", "SUSS": "suss"}
# y el camino inverso, para mostrar lo que sale de la base
NOMBRE_IMPUESTO = {v: k for k, v in IMPUESTO_CLAVE.items()}


def conectar():
    return g.cliente.conectar()


def buscar_pdf(nombre):
    """La factura puede estar en entrada o ya movida a procesadas."""
    nombre = Path(nombre or "").name
    if not nombre:
        return None
    for carpeta in (g.cliente.entrada, g.cliente.procesadas):
        if (carpeta / nombre).exists():
            return carpeta / nombre
    return None


def provisorio(f):
    """Proveedor armado con los datos de la factura, para cotizar antes del alta."""
    cuit = f["emisor"]["cuit"] or ""
    return {"razon_social": f["emisor"]["razon_social"],
            "situacion_ganancias": "I",
            "tipo_persona": "H" if cuit[:2] in ("20", "23", "24", "27") else "J",
            "jurisdiccion": calcular.jurisdiccion_de(f["emisor"]["domicilio"]),
            "retiene_iva_3164": 0, "retiene_suss_1556": 0,
            "retiene_suss_2682": 0, "tipo_obra": None}


def aplicar_ediciones(f, args):
    """Pisa lo que leyo el parser con lo que corrigio la persona en pantalla.

    La lectura de un PDF puede salir mal: si no se pudiera corregir, la factura
    quedaria trabada. Por eso todo lo que se muestra es editable.
    """
    for campo, clave in (("rs", "razon_social"), ("dom", "domicilio"),
                         ("cuit", "cuit"), ("iibb", "ingresos_brutos")):
        v = (args.get(campo) or "").strip()
        if v:
            f["emisor"][clave] = v
    for campo, clave in (("pv", "punto_venta"), ("nro", "numero"),
                         ("fecha_fc", "fecha"), ("letra", "letra")):
        v = (args.get(campo) or "").strip()
        if v:
            f[clave] = v
    for n, item in enumerate(f.get("items") or []):
        v = args.get(f"base_{n}", type=float)
        if v is not None:
            item["subtotal"] = v
    return f


def partidas_de(f, args, habitual):
    """Un item = una base + un regimen. El regimen sale de la pantalla."""
    out = []
    for n, it in enumerate(f.get("items") or []):
        cod = args.get(f"reg_{n}", type=int) or habitual
        out.append({"base": it["subtotal"] or 0.0, "regimen": cod, "item": it, "n": n})
    return out


def desactivados_de(args):
    return {c for c in IMPUESTO_CLAVE.values() if args.get(f"off_{c}") == "1"}


def clave_linea(r):
    """Identifica una linea del calculo: Ganancias puede tener varias, una por regimen."""
    return f"{IMPUESTO_CLAVE.get(r['impuesto'], r['impuesto'])}_{r['regimen']}"


def aplicar_overrides(calculo, args):
    """Pisa la base o la alicuota de una linea con lo que se corrigio en pantalla.

    Lo editado es lo que se guarda y lo que sale impreso en el certificado, asi
    que se deja constancia de que la linea se ajusto a mano.
    """
    for r in calculo["retenciones"]:
        k = clave_linea(r)
        base = args.get(f"base_ret_{k}", type=float)
        alic = args.get(f"alic_ret_{k}", type=float)
        if base is None and alic is None:
            continue
        base_final = r["base"] if base is None else base
        if alic is not None:
            alic_final = alic / 100
        elif isinstance(r["alicuota"], str):
            alic_final = None            # s/escala y no la tocaron: no se recalcula
        else:
            alic_final = r["alicuota"]
        if alic_final is None:
            continue
        nuevo = calcular.redondear(base_final * alic_final)
        if abs(base_final - r["base"]) < 0.005 and abs(alic_final - (r["alicuota"] or 0)) < 1e-9:
            continue
        r["base"], r["alicuota"], r["monto"] = base_final, alic_final, nuevo
        r["editada"] = True
        r["desglose"] = None
        r["nota"] = "base o alícuota ajustadas a mano"
    calculo["total"] = calcular.redondear(sum(x["monto"] for x in calculo["retenciones"]))
    calculo["a_pagar"] = calcular.redondear(calculo["neto"] - calculo["total"])
    return calculo


def controles_del_calculo(calculo, neto_factura):
    """Revisa el calculo antes de dejar confirmar.

    Los mismos controles que hacen AGIP y el propio archivo, pero acá, donde
    todavia se pueden corregir. Devuelve (errores, avisos): con errores no se
    deja confirmar, con avisos si.
    """
    errores, avisos = [], []
    for r in calculo["retenciones"]:
        if not r.get("activo", True):
            continue
        nombre = f"{r['impuesto']} {r['regimen']}"
        base, monto = r["base"] or 0, r["monto"] or 0
        alic = None if isinstance(r["alicuota"], str) else (r["alicuota"] or 0)

        if base < 0:
            errores.append(f"{nombre}: la base es negativa")
        if monto < 0:
            errores.append(f"{nombre}: el importe a retener es negativo")
        if alic is not None and not (0 <= alic <= 0.9999):
            errores.append(f"{nombre}: la alícuota {alic:.2%} está fuera de rango "
                           f"(el campo de AGIP admite hasta 99,99 %)")
        if monto > base + 0.01:
            errores.append(f"{nombre}: se retiene más que la base")
        if alic is not None and base and abs(base * alic - monto) > 0.01 and monto:
            errores.append(f"{nombre}: base por alícuota da "
                           f"${base * alic:,.2f} y el importe dice ${monto:,.2f}")
        if base > (neto_factura or 0) + 0.01:
            avisos.append(f"{nombre}: la base (${base:,.2f}) supera el neto de la "
                          f"factura (${neto_factura or 0:,.2f})")
    return errores, avisos


def enlazar_facturas_previas(calculo):
    """Marca que facturas anteriores del desglose tienen su PDF disponible."""
    for r in calculo["retenciones"]:
        for d in (r.get("desglose") or []):
            archivo = d.get("archivo")
            d["pdf"] = buscar_pdf(archivo).name if archivo and buscar_pdf(archivo) else None
    return calculo


def marcar_desactivados(calculo, apagados):
    """Deja las lineas apagadas a la vista, en cero y con el motivo."""
    total = 0.0
    for r in calculo["retenciones"]:
        clave = IMPUESTO_CLAVE.get(r["impuesto"])
        r["clave"] = clave
        r["activo"] = clave not in apagados
        if not r["activo"]:
            r["monto_original"] = r["monto"]
            r["monto"] = 0.0
            r["nota"] = "desactivada a mano para esta factura"
        total += r["monto"]
    calculo["total"] = calcular.redondear(total)
    calculo["a_pagar"] = calcular.redondear(calculo["neto"] - total)
    return calculo


def filtro_historial(args):
    """WHERE y parametros comunes a la bandeja y a la exportacion."""
    q = (args.get("q") or "").strip()
    desde = (args.get("desde") or "").strip()
    hasta = (args.get("hasta") or "").strip()
    donde, params = [], []
    if q:
        donde.append("(p.razon_social LIKE ? OR p.cuit LIKE ? OR c.numero LIKE ? "
                     "OR c.numero_crudo LIKE ?)")
        params += [f"%{q}%"] * 4
    # Se filtra por la fecha de la retencion, que es la que define el periodo a
    # presentar, y no por la fecha de la factura.
    if desde:
        donde.append("COALESCE(r.fecha_retencion, c.fecha) >= ?")
        params.append(desde)
    if hasta:
        donde.append("COALESCE(r.fecha_retencion, c.fecha) <= ?")
        params.append(hasta)
    sql = (" WHERE " + " AND ".join(donde)) if donde else ""
    return sql, params, {"q": q, "desde": desde, "hasta": hasta}


@app.url_value_preprocessor
def tomar_cliente(endpoint, values):
    if values and "cliente" in values:
        try:
            g.cliente = clientes.obtener(values.pop("cliente"))
        except KeyError:
            abort(404)


@app.url_defaults
def completar_cliente(endpoint, values):
    if "cliente" in values or not getattr(g, "cliente", None):
        return
    if app.url_map.is_endpoint_expecting(endpoint, "cliente"):
        values["cliente"] = g.cliente.slug


@app.context_processor
def cliente_en_plantillas():
    return {"cliente": getattr(g, "cliente", None)}


def plata(v):
    return f"{v:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def impuesto(clave):
    return NOMBRE_IMPUESTO.get(clave, clave)


app.jinja_env.filters["plata"] = plata
app.jinja_env.filters["impuesto"] = impuesto


# ------------------------------------------------------------------ panel
@app.route("/")
def panel():
    filas = [{"c": c, "d": c.datos(), "r": c.resumen(), "falta": c.faltantes(),
              "iibb": "iibb_caba" in c.agente_de()} for c in clientes.listar()]
    activos = [x for x in filas if not x["d"].get("archivado")]
    archivados = [x for x in filas if x["d"].get("archivado")]
    return render_template("panel.html", activos=activos, archivados=archivados)


# ------------------------------------------------------------------ padron AGIP
def estado_padron():
    agentes = []
    for c in clientes.listar():
        if "iibb_caba" not in c.agente_de():
            continue
        conn = c.conectar()
        try:
            f = conn.execute("SELECT MAX(vigencia_desde), COUNT(DISTINCT cuit) "
                             "FROM padron_iibb_caba").fetchone()
        finally:
            conn.close()
        agentes.append({"nombre": c.nombre, "vigencia": f[0], "cuits": f[1]})
    return {"cache": padron_agip.en_cache(), "agentes": agentes,
            "mes": calcular.date.today().isoformat()[:7]}


@app.route("/padron")
def padron():
    return render_template("padron.html", e=estado_padron(),
                           ok=request.args.get("ok"), error=request.args.get("error"))


def resultado_carga(periodo, txt):
    cargas = padron_agip.cargar_en_clientes(txt)
    detalle = ", ".join(f"{c.nombre}: {n} proveedores" for c, _, n in cargas)
    return redirect(url_for("padron", ok=f"Padrón {periodo} cargado. {detalle}".strip()))


@app.route("/padron/descargar", methods=["POST"])
def padron_descargar():
    try:
        rar = padron_agip.descargar()
        txt = padron_agip.extraer(rar)
        return resultado_carga(padron_agip.periodo_de(txt), txt)
    except (Exception, SystemExit) as e:
        return redirect(url_for("padron", error=f"No se pudo bajar de AGIP: {e}"))


@app.route("/padron/importar", methods=["POST"])
def padron_importar():
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        return redirect(url_for("padron", error="Elegí un archivo .rar o .txt."))
    extension = Path(archivo.filename).suffix.lower()
    if extension not in (".rar", ".txt"):
        return redirect(url_for("padron", error="El archivo tiene que ser .rar o .txt."))
    padron_agip.CACHE.mkdir(exist_ok=True)
    subido = padron_agip.CACHE / ("_subido" + extension)
    archivo.save(subido)
    try:
        periodo, txt = padron_agip.importar(subido)
        return resultado_carga(periodo, txt)
    except (Exception, SystemExit) as e:
        return redirect(url_for("padron", error=f"No se pudo importar: {e}"))
    finally:
        subido.unlink(missing_ok=True)


@app.route("/c/<cliente>/archivar", methods=["POST"])
def archivar_cliente():
    g.cliente.archivar(True)
    return redirect(url_for("panel"))


@app.route("/c/<cliente>/desarchivar", methods=["POST"])
def desarchivar_cliente():
    g.cliente.archivar(False)
    return redirect(request.form.get("volver") or url_for("panel"))


@app.route("/nuevo", methods=["POST"])
def nuevo_cliente():
    nombre = (request.form.get("nombre") or "").strip()
    if not nombre or not clientes.slug_de(nombre):
        return redirect(url_for("panel"))
    c = clientes.crear(nombre)
    return redirect(url_for("datos_cliente", cliente=c.slug))


@app.route("/c/<cliente>/datos", methods=["GET", "POST"])
def datos_cliente():
    d = g.cliente.datos()
    if request.method == "POST":
        f = request.form
        dom = {k: f.get(f"dom_{k}", "").strip() for k in ("default", "ganancias", "iibb_caba")}
        d.update({
            "nombre": f.get("nombre", "").strip() or d.get("nombre"),
            "razon_social": f.get("razon_social", "").strip(),
            "cuit": "".join(ch for ch in f.get("cuit", "") if ch.isdigit()),
            "domicilios": dom,
            "agente_de": {k: f.get(f"agente_{k}") == "1" for k in clientes.IMPUESTOS},
        })
        g.cliente.guardar_datos(d)
        return redirect(url_for("bandeja"))
    agente = g.cliente.agente_de()
    return render_template("datos.html", d=d, agente=agente,
                           impuestos=clientes.IMPUESTOS, falta=g.cliente.faltantes())


# ------------------------------------------------------------------ bandeja
@app.route("/c/<cliente>/")
def bandeja():
    conn = conectar()
    try:
        donde, params, filtro = filtro_historial(request.args)
        recientes = conn.execute(
            "SELECT c.id, c.fecha, c.punto_venta, c.numero, c.neto, p.razon_social, "
            "       p.cuit, COUNT(r.id) AS retenciones, "
            "       COALESCE(SUM(r.monto), 0) AS total_retenido "
            "FROM comprobantes c JOIN proveedores p ON p.cuit = c.cuit "
            "LEFT JOIN retenciones r ON r.comprobante_id = c.id "
            + donde + " GROUP BY c.id ORDER BY c.id DESC LIMIT 200", params).fetchall()
        total_filtrado = sum(r["total_retenido"] for r in recientes)

        # Una factura deja de estar pendiente cuando se confirma: ahi se mueve a
        # procesadas. Ademas se marca la que ya este cargada, por las dudas.
        cargados = {r[0] for r in conn.execute(
            "SELECT punto_venta || '-' || numero FROM comprobantes "
            "WHERE punto_venta IS NOT NULL")}
        pendientes = []
        for pdf in (sorted(g.cliente.entrada.glob("*.pdf")) if g.cliente.entrada.exists() else []):
            try:
                d = leer_factura.leer(pdf)
                clave = f"{d['punto_venta']}-{d['numero']}"
                pendientes.append({"nombre": pdf.name,
                                   "proveedor": d["emisor"]["razon_social"],
                                   "comprobante": clave,
                                   "neto": d["importes"]["base_ganancias"],
                                   "ya_cargada": clave in cargados})
            except Exception:
                pendientes.append({"nombre": pdf.name, "proveedor": None,
                                   "comprobante": None, "neto": None,
                                   "ya_cargada": False})

        avisos = []
        if g.cliente.faltantes():
            avisos.append("Faltan datos del cliente para emitir certificados: "
                          + ", ".join(g.cliente.faltantes()) + ".")
        ultimo = conn.execute(
            "SELECT MAX(vigencia_desde) FROM padron_iibb_caba").fetchone()[0]
        hoy = calcular.date.today().isoformat()
        if "iibb_caba" not in g.cliente.agente_de():
            pass
        elif not ultimo:
            avisos.append("No hay ningún padrón de AGIP cargado. "
                          "Cargalo desde el menú Padrón AGIP.")
        elif calcular.meses_de_atraso(ultimo, hoy[:7]) >= 1:
            avisos.append(f"El padrón de AGIP más nuevo es de {ultimo[:7]}. "
                          f"Actualizalo desde el menú Padrón AGIP.")
        return render_template("bandeja.html", recientes=recientes, filtro=filtro,
                               total_filtrado=total_filtrado,
                               pendientes=pendientes, avisos=avisos)
    finally:
        conn.close()


@app.route("/c/<cliente>/subir", methods=["POST"])
def subir():
    archivo = request.files.get("factura")
    if not archivo or not archivo.filename.lower().endswith(".pdf"):
        return redirect(url_for("bandeja"))
    g.cliente.entrada.mkdir(exist_ok=True)
    nombre = Path(archivo.filename).name
    archivo.save(g.cliente.entrada / nombre)
    return redirect(url_for("revisar", nombre=nombre))


@app.route("/c/<cliente>/descartar", methods=["POST"])
def descartar():
    """Saca una factura de la bandeja sin procesarla.

    Solo borra el PDF de la carpeta de entrada. Si ya estaba confirmada, el
    comprobante y sus retenciones no se tocan: para eso esta anular.
    """
    pdf = g.cliente.entrada / Path(request.form.get("nombre", "")).name
    if pdf.exists() and pdf.parent == g.cliente.entrada:
        pdf.unlink()
    return redirect(url_for("bandeja"))


@app.route("/c/<cliente>/factura/<path:nombre>")
def factura_pdf(nombre):
    pdf = buscar_pdf(nombre)
    if pdf is None:
        abort(404)
    return send_from_directory(pdf.parent, pdf.name)


@app.route("/c/<cliente>/certificado/<path:nombre>")
def certificado_pdf(nombre):
    return send_from_directory(g.cliente.certificados, nombre)


@app.route("/c/<cliente>/exportar")
def exportar():
    """Baja el historial filtrado a un Excel, una fila por retencion."""
    conn = conectar()
    try:
        donde, params, filtro = filtro_historial(request.args)
        filas = conn.execute(
            "SELECT COALESCE(r.fecha_retencion, c.fecha) AS fecha_ret, r.periodo, "
            "       p.razon_social, p.cuit, c.letra, c.punto_venta, c.numero, "
            "       c.fecha AS fecha_factura, c.neto, c.importe_iva, c.total, "
            "       r.impuesto, r.regimen, r.base_imponible, r.alicuota, r.monto, "
            "       r.estado, r.nro_certificado, r.motivo "
            "FROM retenciones r JOIN comprobantes c ON c.id = r.comprobante_id "
            "JOIN proveedores p ON p.cuit = c.cuit "
            + donde + " ORDER BY fecha_ret, p.razon_social, r.impuesto", params).fetchall()
    finally:
        conn.close()

    columnas = [
        ("Fecha retención", "fecha_ret", 15), ("Período", "periodo", 10),
        ("Proveedor", "razon_social", 34), ("CUIT", "cuit", 14),
        ("Letra", "letra", 7), ("Pto. venta", "punto_venta", 11),
        ("Número", "numero", 12), ("Fecha factura", "fecha_factura", 14),
        ("Neto", "neto", 15), ("IVA", "importe_iva", 14), ("Total", "total", 15),
        ("Impuesto", "impuesto", 12), ("Régimen", "regimen", 10),
        ("Base imponible", "base_imponible", 16), ("Alícuota", "alicuota", 10),
        ("Retenido", "monto", 15), ("Estado", "estado", 14),
        ("Certificado", "nro_certificado", 16), ("Observación", "motivo", 46),
    ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Retenciones"
    ws.append([c[0] for c in columnas])
    for celda in ws[1]:
        celda.font = Font(bold=True)
        celda.alignment = Alignment(vertical="center")
    for f in filas:
        ws.append([f[c[1]] for c in columnas])
    for i, (_, clave, ancho) in enumerate(columnas, 1):
        ws.column_dimensions[get_column_letter(i)].width = ancho
        if clave in ("neto", "importe_iva", "total", "base_imponible", "monto"):
            for celda in ws[get_column_letter(i)][1:]:
                celda.number_format = "#,##0.00"
        if clave == "alicuota":
            for celda in ws[get_column_letter(i)][1:]:
                celda.number_format = "0.00%"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    sufijo = "_".join(x for x in (filtro["desde"], filtro["hasta"]) if x) or _date.today().isoformat()
    return send_file(buf, as_attachment=True,
                     download_name=f"retenciones_{sufijo}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


# ------------------------------------------------------------------ revisar
@app.route("/c/<cliente>/revisar/<path:nombre>")
def revisar(nombre):
    pdf = buscar_pdf(nombre)
    if pdf is None:
        abort(404)
    try:
        f = aplicar_ediciones(leer_factura.leer(pdf), request.args)
    except Exception:
        return render_template("error.html", nombre=nombre,
                               detalle=traceback.format_exc())
    avisos = leer_factura.controles(f)

    conn = conectar()
    try:
        cuit = f["emisor"]["cuit"]
        proveedor = conn.execute(
            "SELECT * FROM proveedores WHERE cuit = ?", (cuit,)).fetchone()
        habitual = procesar.regimen_habitual(conn, cuit)
        regimen = request.args.get("regimen", type=int) or habitual
        fecha_pago = request.args.get("fecha_pago") or calcular.date.today().isoformat()
        neto = request.args.get("neto", type=float) or f["importes"]["base_ganancias"]
        servicio_caba = request.args.get("servicio_caba") == "1"
        apagados = desactivados_de(request.args)

        partidas = partidas_de(f, request.args, regimen)
        calculo = None
        if neto and (partidas or regimen):
            calculo = calcular.calcular(
                conn, cuit, neto, regimen, fecha_pago,
                proveedor_provisorio=provisorio(f), servicio_en_caba=servicio_caba,
                neto_gravado=f["importes"]["neto_gravado"], partidas=partidas or None,
                letra=f["letra"], sujeta_a_retencion=f["sujeta_a_retencion"],
                iva_facturado=f["importes"]["iva"], agente_de=g.cliente.agente_de())
            calculo = aplicar_overrides(calculo, request.args)
            calculo = enlazar_facturas_previas(marcar_desactivados(calculo, apagados))
            errores, avisos_calculo = controles_del_calculo(calculo, neto)
        else:
            errores, avisos_calculo = [], []
        regimenes = conn.execute(
            "SELECT cod_regimen, concepto FROM regimenes_ganancias "
            "WHERE situacion = 'I' AND tipo_persona = '' ORDER BY cod_regimen").fetchall()
        ya_cargada = conn.execute(
            "SELECT id FROM comprobantes WHERE cuit = ? AND punto_venta = ? AND numero = ?",
            (cuit, f["punto_venta"], f["numero"])).fetchone()
        return render_template(
            "revisar.html", nombre=nombre, f=f, avisos=avisos, proveedor=proveedor,
            regimen=regimen, regimenes=regimenes, fecha_pago=fecha_pago, neto=neto,
            calculo=calculo, ya_cargada=ya_cargada, servicio_caba=servicio_caba,
            partidas=partidas, apagados=apagados, errores=errores,
            avisos_calculo=avisos_calculo,
            jurisdiccion=calcular.jurisdiccion_de(f["emisor"]["domicilio"]))
    finally:
        conn.close()


@app.route("/c/<cliente>/confirmar", methods=["POST"])
def confirmar():
    nombre = request.form["nombre"]
    pdf = buscar_pdf(nombre)
    if pdf is None:
        abort(404)
    f = aplicar_ediciones(leer_factura.leer(pdf), request.form)
    regimen = int(request.form["regimen"]) if request.form.get("regimen") else None
    fecha_pago = request.form["fecha_pago"]
    neto = float(request.form["neto"])
    servicio_caba = request.form.get("servicio_caba") == "1"
    apagados = desactivados_de(request.form)
    partidas = partidas_de(f, request.form, regimen)

    if g.cliente.faltantes():
        return render_template(
            "error.html", nombre=nombre,
            detalle="No se guardó nada: faltan datos del cliente ("
                    + ", ".join(g.cliente.faltantes()) + "). Cargalos en Datos del cliente.")
    conn = conectar()
    try:
        conn.execute("BEGIN")
        cambios, _ = procesar.sincronizar_proveedor(conn, f, True)
        calculo = calcular.calcular(
            conn, f["emisor"]["cuit"], neto, regimen, fecha_pago,
            proveedor_provisorio=provisorio(f), servicio_en_caba=servicio_caba,
            neto_gravado=f["importes"]["neto_gravado"], partidas=partidas or None,
            letra=f["letra"], sujeta_a_retencion=f["sujeta_a_retencion"],
            iva_facturado=f["importes"]["iva"], agente_de=g.cliente.agente_de())
        calculo = marcar_desactivados(aplicar_overrides(calculo, request.form), apagados)
        errores, _ = controles_del_calculo(calculo, neto)
        if errores:
            conn.rollback()
            return render_template(
                "error.html", nombre=nombre,
                detalle="No se guardó nada:\n\n  - " + "\n  - ".join(errores))
        cid, emitidos = procesar.guardar(conn, f, calculo, fecha_pago, servicio_caba)
        for _, nro in emitidos:
            certificados.emitir(conn, certificados.retenciones(conn, certificado=nro),
                                destino=g.cliente.certificados, agente=g.cliente.datos())
        conn.commit()
    except Exception:
        conn.rollback()
        return render_template("error.html", nombre=nombre,
                               detalle=traceback.format_exc())
    finally:
        conn.close()

    # con esto la factura deja de figurar como pendiente
    g.cliente.procesadas.mkdir(exist_ok=True)
    if pdf.parent == g.cliente.entrada:
        shutil.move(str(pdf), str(g.cliente.procesadas / pdf.name))
    return redirect(url_for("comprobante", cid=cid))


# ------------------------------------------------------------- comprobante
@app.route("/c/<cliente>/comprobante/<int:cid>")
def comprobante(cid):
    conn = conectar()
    try:
        c = conn.execute(
            "SELECT c.*, p.razon_social, p.domicilio, p.jurisdiccion, "
            "       p.nro_inscripcion_ib FROM comprobantes c "
            "JOIN proveedores p ON p.cuit = c.cuit WHERE c.id = ?", (cid,)).fetchone()
        if c is None:
            abort(404)
        retenciones = conn.execute(
            "SELECT * FROM retenciones WHERE comprobante_id = ? ORDER BY impuesto",
            (cid,)).fetchall()
        archivos = []
        if g.cliente.certificados.exists():
            for r in retenciones:
                if r["nro_certificado"]:
                    marca = r["nro_certificado"].replace("/", "-")
                    archivos += [p.name for p in sorted(g.cliente.certificados.glob(f"*_{marca}_*.pdf"))]
        pdf = buscar_pdf(c["origen"] or "")
        return render_template("comprobante.html", c=c, retenciones=retenciones,
                               archivos=sorted(set(archivos)),
                               pdf=pdf.name if pdf else None)
    finally:
        conn.close()


def es_el_ultimo_de_la_serie(conn, r):
    """True si no hay ningun certificado posterior de ese impuesto y año."""
    nro = (r["nro_certificado"] or "").strip()
    if "-" not in nro:
        return True
    anio, sec = nro.split("-", 1)
    if not sec.isdigit():
        return False
    posterior = conn.execute(
        "SELECT 1 FROM retenciones WHERE impuesto = ? AND id <> ? "
        "AND nro_certificado LIKE ? AND "
        "CAST(substr(nro_certificado, length(?) + 2) AS INTEGER) > ? LIMIT 1",
        (r["impuesto"], r["id"], f"{anio}-%", anio, int(sec))).fetchone()
    return posterior is None


def borrar_certificados(nro):
    """Borra los PDF emitidos con ese numero de certificado."""
    if not nro or not g.cliente.certificados.exists():
        return
    for pdf in g.cliente.certificados.glob(f"*_{nro.replace('/', '-')}_*.pdf"):
        pdf.unlink(missing_ok=True)


@app.route("/c/<cliente>/anular/<int:rid>", methods=["POST"])
def anular(rid):
    """Anula una retencion ya practicada.

    Si es el ultimo certificado de su serie se borra del todo, y asi el numero
    vuelve a estar disponible para la proxima. Si hay alguno posterior no se puede
    borrar sin dejar un hueco en la numeracion: queda anulada, conservando su
    numero.

    En los dos casos la retencion deja de consumir el minimo no imponible del mes.
    """
    motivo = (request.form.get("motivo") or "").strip()
    conn = conectar()
    try:
        r = conn.execute("SELECT * FROM retenciones WHERE id = ?", (rid,)).fetchone()
        if r is None:
            abort(404)
        cid = r["comprobante_id"]
        if es_el_ultimo_de_la_serie(conn, r):
            borrar_certificados(r["nro_certificado"])
            conn.execute("DELETE FROM retenciones WHERE id = ?", (rid,))
        else:
            conn.execute(
                "UPDATE retenciones SET estado = 'anulada', monto = 0, motivo = ? "
                "WHERE id = ?",
                (f"anulada el {calcular.date.today().isoformat()}"
                 + (f": {motivo}" if motivo else "")
                 + f" (conserva el numero {r['nro_certificado']} porque hay "
                   f"certificados posteriores)", rid))

        # si al comprobante no le queda ninguna retencion, se va con la factura
        quedan = conn.execute(
            "SELECT COUNT(*) FROM retenciones WHERE comprobante_id = ?", (cid,)).fetchone()[0]
        if not quedan:
            origen = conn.execute(
                "SELECT origen FROM comprobantes WHERE id = ?", (cid,)).fetchone()[0]
            conn.execute("DELETE FROM comprobantes WHERE id = ?", (cid,))
            conn.commit()
            pdf = buscar_pdf(origen or "")
            if pdf is not None and pdf.parent == g.cliente.procesadas:
                g.cliente.entrada.mkdir(exist_ok=True)
                shutil.move(str(pdf), str(g.cliente.entrada / pdf.name))
            return redirect(url_for("bandeja"))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("comprobante", cid=cid))


@app.route("/c/<cliente>/reactivar/<int:rid>", methods=["POST"])
def reactivar(rid):
    """Vuelve atras una anulacion hecha por error."""
    conn = conectar()
    try:
        r = conn.execute("SELECT * FROM retenciones WHERE id = ?", (rid,)).fetchone()
        if r is None:
            abort(404)
        estado = "practicada" if (r["monto"] or 0) > 0 else "no_practicada"
        conn.execute("UPDATE retenciones SET estado = ?, motivo = NULL WHERE id = ?",
                     (estado, rid))
        conn.commit()
        cid = r["comprobante_id"]
    finally:
        conn.close()
    return redirect(url_for("comprobante", cid=cid))


if __name__ == "__main__":
    print("Sistema de retenciones -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
