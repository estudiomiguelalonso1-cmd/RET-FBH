# -*- coding: utf-8 -*-
"""Genera los certificados de retencion en PDF.

Reproduce los dos layouts que Fiberhome emite hoy: el del SI.CO.RE. para Ganancias
y la constancia de AGIP para Ingresos Brutos. Los logos se extrajeron de los
certificados originales, asi que el resultado es visualmente el mismo.

    python certificados.py --certificado 2026-174
    python certificados.py --periodo 2026-08
    python certificados.py --periodo 2026-08 --impuesto iibb_caba

Los datos del agente de retencion salen de agente.json. El domicilio del proveedor
sale de la base; si falta, el certificado se genera igual pero se avisa.
"""
import argparse
import json
import sqlite3
from pathlib import Path

import pymupdf
from jinja2 import Template

DIR = Path(__file__).resolve().parent
PLANTILLAS = DIR / "plantillas"
SALIDA = DIR / "certificados"
DB = DIR / "retenciones.db"

# que plantilla y que titulo usa cada impuesto
IMPUESTOS = {
    "ganancias": {"plantilla": "cert_ganancias.html", "etiqueta": "Gcias",
                  "titulo": "Impto. a las Ganancias"},
    "iibb_caba": {"plantilla": "cert_iibb.html", "etiqueta": "IIBB",
                  "titulo": "Ingresos Brutos"},
}

TIPO_COMPROBANTE = {"FC": "01 - Factura / Tique Factura / Tique"}


def plata(v):
    """1150435.54 -> '1.150.435,54'"""
    return f"{v:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fecha_ar(iso):
    if not iso or len(iso) != 10:
        return iso or ""
    a, m, d = iso.split("-")
    return f"{d}/{m}/{a}"


def nro_certificado_sicore(nro):
    """'2026-174' -> '0000-2026-000174', que es como lo imprime el SIAP."""
    if not nro or "-" not in nro:
        return nro or ""
    anio, sec = nro.split("-", 1)
    sec = sec.strip()
    return f"0000-{anio}-{sec.zfill(6)}" if sec.isdigit() else nro


def cuit_con_guiones(c):
    return f"{c[:2]}-{c[2:10]}-{c[10:]}" if c and len(c) == 11 else c


def conectar():
    if not DB.exists():
        raise SystemExit(f"falta {DB.name}: corre primero 'python migrar.py'")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def cargar_agente():
    ruta = DIR / "agente.json"
    if not ruta.exists():
        raise SystemExit("falta agente.json con los datos del agente de retencion")
    return json.loads(ruta.read_text(encoding="utf-8"))


def contexto(r, agente, impuesto):
    """Arma las variables de la plantilla a partir de una fila de retenciones_completas."""
    conf = agente["domicilios"].get(impuesto, agente["domicilios"]["default"])
    numero = (f"{r['punto_venta']}-{r['numero']}" if r["punto_venta"] and r["numero"]
              else (r["numero_crudo"] or ""))
    ctx = {
        "nro_certificado": (nro_certificado_sicore(r["nro_certificado"])
                            if impuesto == "ganancias" else (r["nro_certificado"] or "")),
        "fecha": fecha_ar(r["fecha_retencion"]),
        "agente": {"razon_social": agente["razon_social"],
                   "cuit": cuit_con_guiones(agente["cuit"]),
                   "domicilio": conf},
        "sujeto": {"razon_social": r["razon_social"],
                   "cuit": (cuit_con_guiones(r["cuit"]) if impuesto == "ganancias"
                            else r["cuit"]),
                   "domicilio": r["domicilio"] or ""},
        "comprobante_tipo": TIPO_COMPROBANTE.get(r["tipo"], r["tipo"]),
        "comprobante_numero": numero,
        "monto_comprobante": plata(r["neto"]),
        "monto_retencion": plata(r["monto"]),
    }
    if impuesto == "ganancias":
        ctx["impuesto"] = IMPUESTOS["ganancias"]["titulo"]
        ctx["regimen"] = r["concepto_regimen"] or f"Régimen {r['regimen']}"
        ctx["imposibilidad"] = "NO"
    else:
        ctx["alicuota"] = f"{(r['alicuota'] or 0) * 100:.2f}".replace(".", ",") + "%"
    return ctx


def generar_pdf(html, destino):
    """Renderiza el HTML a un PDF A4. Las imagenes se resuelven contra plantillas/."""
    hoja = pymupdf.paper_rect("a4")
    marco = hoja + (57, 57, -57, -57)          # margenes de 2 cm
    story = pymupdf.Story(html=html, archive=pymupdf.Archive(PLANTILLAS))
    writer = pymupdf.DocumentWriter(str(destino))
    mas = True
    while mas:
        dispositivo = writer.begin_page(hoja)
        mas, _ = story.place(marco)
        story.draw(dispositivo)
        writer.end_page()
    writer.close()


def retenciones(conn, periodo=None, certificado=None, impuesto=None):
    sql = ("SELECT rc.*, rg.concepto AS concepto_regimen, p.domicilio "
           "FROM retenciones_completas rc "
           "JOIN proveedores p ON p.cuit = rc.cuit "
           "LEFT JOIN regimenes_ganancias rg "
           "       ON rg.cod_regimen = CAST(rc.regimen AS INTEGER) "
           "      AND rg.situacion = 'I' AND rg.tipo_persona = '' "
           "      AND rc.impuesto = 'ganancias' "
           "WHERE rc.estado = 'practicada' AND rc.monto > 0 "
           "  AND rc.nro_certificado IS NOT NULL "
           "  AND rc.impuesto IN ('ganancias', 'iibb_caba')")
    args = []
    if periodo:
        sql += " AND rc.periodo = ?"
        args.append(periodo)
    if certificado:
        sql += " AND rc.nro_certificado = ?"
        args.append(certificado)
    if impuesto:
        sql += " AND rc.impuesto = ?"
        args.append(impuesto)
    return conn.execute(sql + " GROUP BY rc.id ORDER BY rc.fecha_retencion, rc.id", args).fetchall()


def emitir(conn, filas, destino=None, agente=None):
    """Genera los PDF de esas retenciones. Devuelve (rutas, proveedores_sin_domicilio)."""
    agente = agente or cargar_agente()
    destino = Path(destino or SALIDA)
    destino.mkdir(parents=True, exist_ok=True)
    rutas, sin_domicilio = [], set()
    for r in filas:
        conf = IMPUESTOS[r["impuesto"]]
        html = Template((PLANTILLAS / conf["plantilla"]).read_text(encoding="utf-8"))
        nombre = (f"Ret_{conf['etiqueta']}_{(r['nro_certificado'] or '').replace('/', '-')}"
                  f"_{r['cuit']}.pdf")
        generar_pdf(html.render(**contexto(r, agente, r["impuesto"])), destino / nombre)
        rutas.append(destino / nombre)
        if not r["domicilio"]:
            sin_domicilio.add((r["cuit"], r["razon_social"]))
    return rutas, sin_domicilio


def main():
    ap = argparse.ArgumentParser(description="Genera los certificados de retencion en PDF")
    ap.add_argument("--periodo", help="AAAA-MM")
    ap.add_argument("--certificado", help="un certificado puntual, por su numero")
    ap.add_argument("--impuesto", choices=("ganancias", "iibb_caba"))
    ap.add_argument("--salida", default=str(SALIDA))
    args = ap.parse_args()
    if not (args.periodo or args.certificado):
        ap.error("indica --periodo o --certificado")

    conn = conectar()
    try:
        filas = retenciones(conn, args.periodo, args.certificado, args.impuesto)
        rutas, sin_domicilio = emitir(conn, filas, args.salida)
        print(f"{len(rutas)} certificados -> {Path(args.salida).name}/")
        if sin_domicilio:
            print(f"\n{len(sin_domicilio)} proveedores sin domicilio cargado "
                  f"(el certificado sale con el campo vacio):")
            for cuit, rs in sorted(sin_domicilio, key=lambda x: x[1] or ""):
                print(f"   {cuit}  {rs}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
