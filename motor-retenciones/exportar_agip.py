# -*- coding: utf-8 -*-
"""Genera el archivo de importacion de retenciones al aplicativo e-ARCIBA (AGIP).

Layout: "Documento Tecnico Importacion de Operaciones e-ARCIBA - Diseno de Registro
de Importaciones de Retenciones/Percepciones", vigente desde el periodo 01-2022.
23 campos, 226 caracteres por linea, UTF-8, ordenado por fecha de retencion.

Reglas del propio documento:
  - los numeros se alinean a la derecha y se completan con ceros
  - los textos se alinean a la izquierda y se completan con blancos
  - el separador decimal es la coma y va dentro del largo del campo
    (el maximo 9999999999999,99 ocupa exactamente los 16 caracteres)
  - Monto Sujeto a Retencion = Monto del comprobante - Importe IVA - Importe otros
  - Retencion Practicada = Monto Sujeto a Retencion * Alicuota / 100

Uso:
    python exportar_agip.py 2026-07
    python exportar_agip.py 2026-07 --proveedores proveedores_agip.csv
"""
import argparse
import csv
import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import motor_caba
from exportar_sicore import fecha, nro_comprobante

DIR = Path(__file__).resolve().parent
LARGO_LINEA = 226

TIPO_OPERACION_RETENCION = "1"

# 29 = "PADRON DE REGIMENES GENERALES". Es el regimen por el que retiene Fiberhome:
# la alicuota sale del padron por sujeto, que es justamente lo que esta pegado en la
# columna M del Excel. Es ademas uno de los dos codigos (28 y 29) para los que el
# diseno admite alicuota cero, que es lo que pasa con los proveedores exentos.
CODIGO_NORMA = "29"

TIPO_COMPROBANTE = {"FC": "01"}     # 01 = Factura
TIPO_DOC_CUIT = "3"
SITUACION_IVA = {"A": "1",          # letra A -> responsable inscripto
                 "M": "1",
                 "C": "4"}          # letra C -> monotributo / exento


def dos_decimales(valor):
    """Redondeo financiero (medio hacia arriba), no el de Python.

    AGIP revalida que Retencion Practicada = Monto Sujeto * Alicuota / 100. Con el
    redondeo por defecto de Python, 1016,535 da 1016,53 y la validacion falla.
    """
    return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def num(valor, largo):
    """Numero alineado a la derecha, con coma decimal y relleno de ceros."""
    t = f"{dos_decimales(valor):.2f}".replace(".", ",")
    if len(t) > largo:
        raise ValueError(f"{valor} no entra en {largo} caracteres")
    return t.rjust(largo, "0")


def txt(valor, largo):
    """Texto alineado a la izquierda, completado con blancos."""
    return str(valor or "")[:largo].ljust(largo)


def entero(valor, largo):
    return str(valor or "").rjust(largo, "0")[:largo]


def cargar_proveedores(ruta):
    """CUIT -> datos maestros que el Excel no guarda (N de inscripcion en IIBB, etc.)."""
    if not ruta or not Path(ruta).exists():
        return {}
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        return {r["cuit"].strip(): r for r in csv.DictReader(fh, delimiter=";")
                if r.get("cuit")}


def linea(*, fecha_retencion, tipo_comprobante, letra, nro_comp, fecha_comprobante,
          monto_comprobante, nro_certificado, cuit, situacion_ib, nro_inscripcion_ib,
          situacion_iva, razon_social, otros_conceptos, importe_iva, monto_sujeto,
          alicuota, retencion):
    """Arma un registro de 226 caracteres."""
    partes = [
        TIPO_OPERACION_RETENCION,             # 1         1
        entero(CODIGO_NORMA, 3),              # 2-4       3
        fecha_retencion,                      # 5-14     10
        tipo_comprobante,                     # 15-16     2
        txt(letra, 1),                        # 17        1
        entero(nro_comp, 16),                 # 18-33    16
        fecha_comprobante,                    # 34-43    10
        num(monto_comprobante, 16),           # 44-59    16
        txt(nro_certificado, 16),             # 60-75    16
        TIPO_DOC_CUIT,                        # 76        1
        entero(cuit, 11),                     # 77-87    11
        situacion_ib,                         # 88        1
        entero(nro_inscripcion_ib, 11),       # 89-99    11
        situacion_iva,                        # 100       1
        txt(razon_social, 30),                # 101-130  30
        num(otros_conceptos, 16),             # 131-146  16
        num(importe_iva, 16),                 # 147-162  16
        num(monto_sujeto, 16),                # 163-178  16
        num(alicuota, 5),                     # 179-183   5
        num(retencion, 16),                   # 184-199  16
        num(retencion, 16),                   # 200-215  16  igual a la anterior
        " ",                                  # 216       1  aceptacion
        " " * 10,                             # 217-226  10  fecha de aceptacion
    ]
    l = "".join(partes)
    assert len(l) == LARGO_LINEA, f"linea de {len(l)}, deberian ser {LARGO_LINEA}"
    return l


def conectar():
    db = DIR / "retenciones.db"
    if not db.exists():
        raise SystemExit(f"falta {db.name}: corre primero 'python migrar.py'")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def generar(periodo, proveedores=None, iva=0.0, conn=None):
    """Lineas del lote de e-ARCIBA para un periodo, leidas de la base."""
    propia = conn is None
    conn = conn or conectar()
    try:
        filas = conn.execute(
            "SELECT r.id, r.regimen, r.monto, r.alicuota, r.base_imponible, "
            "       r.fecha_retencion, "
            "       r.nro_certificado, c.id AS comprobante_id, c.tipo, c.letra, "
            "       c.punto_venta, c.numero, c.numero_crudo, c.agrupa_varias, "
            "       c.fecha AS fecha_comprobante, c.fecha_cruda, c.neto, "
            "       p.cuit, p.razon_social, p.situacion_ib, p.situacion_iva, "
            "       p.nro_inscripcion_ib "
            "FROM retenciones r "
            "JOIN comprobantes c ON c.id = r.comprobante_id "
            "JOIN proveedores p ON p.cuit = c.cuit "
            "WHERE r.impuesto = 'iibb_caba' AND r.periodo = ? "
            "  AND r.estado = 'practicada' AND r.monto > 0 "
            "ORDER BY r.fecha_retencion, r.id", (periodo,)).fetchall()
    finally:
        if propia:
            conn.close()

    maestros = cargar_proveedores(proveedores)
    lineas, avisos, derivados = [], [], []
    for r in filas:
        m = maestros.get(r["cuit"], {})
        etiqueta = f"comprobante #{r['comprobante_id']} ({r['razon_social']})"
        falta = []
        if not r["fecha_retencion"]:
            falta.append("sin fecha de retencion")
        if not r["fecha_comprobante"]:
            falta.append(f"la fecha de la factura no es una fecha ({r['fecha_cruda']!r})")
        if not (r["punto_venta"] and r["numero"]):
            falta.append(f"no se pudo normalizar el numero ({r['numero_crudo']!r})")

        tipo_comp = TIPO_COMPROBANTE.get(r["tipo"] or "FC")
        if not tipo_comp:
            falta.append(f"tipo de comprobante desconocido: {r['tipo']!r}")

        sit_iva = m.get("situacion_iva") or r["situacion_iva"] or SITUACION_IVA.get(r["letra"])
        if not sit_iva:
            falta.append(f"no se deduce la situacion frente al IVA de la letra {r['letra']!r}")

        sit_ib = m.get("situacion_ib") or r["situacion_ib"]
        if not sit_ib:
            falta.append("falta la situacion IB del proveedor")

        # Convenio Multilateral usa el propio CUIT; los locales, un numero propio.
        nro_ib = m.get("nro_inscripcion_ib") or r["nro_inscripcion_ib"] or ""
        if not nro_ib and sit_ib == "2":
            nro_ib = r["cuit"]
        if not nro_ib:
            falta.append("falta el N de inscripcion en IIBB (contribuyente local)")

        if falta:
            avisos.append((etiqueta, falta))
            continue
        if r["agrupa_varias"]:
            derivados.append(f"{etiqueta}: la factura agrupa varias "
                             f"({r['numero_crudo']}); se declara "
                             f"{r['punto_venta']}-{r['numero']}")

        fr, _ = fecha(r["fecha_retencion"])
        fc, _ = fecha(r["fecha_comprobante"])
        # AGIP revalida que monto sujeto x alicuota = retencion, asi que se usa la
        # base guardada en la retencion y no el neto del comprobante: pueden
        # diferir si alguien la ajusto a mano.
        neto = r["base_imponible"] if r["base_imponible"] is not None else r["neto"]
        importe_iva = neto * iva
        lineas.append(linea(
            fecha_retencion=fr, tipo_comprobante=tipo_comp, letra=r["letra"] or "A",
            nro_comp=f"{r['punto_venta']}{r['numero']}", fecha_comprobante=fc,
            monto_comprobante=neto + importe_iva, nro_certificado=r["nro_certificado"],
            cuit=r["cuit"], situacion_ib=sit_ib, nro_inscripcion_ib=nro_ib,
            situacion_iva=sit_iva, razon_social=r["razon_social"],
            otros_conceptos=0.0, importe_iva=importe_iva, monto_sujeto=neto,
            alicuota=(r["alicuota"] or 0) * 100, retencion=r["monto"]))
    return lineas, avisos, derivados


def verificar(lineas):
    """Revalida sobre el archivo ya armado las dos formulas que controla AGIP."""
    def n(t):
        return Decimal(t.replace(",", "."))

    errores = []
    for i, l in enumerate(lineas, 1):
        if len(l) != LARGO_LINEA:
            errores.append(f"linea {i}: {len(l)} caracteres")
            continue
        monto_comp, otros = n(l[43:59]), n(l[130:146])
        iva, sujeto = n(l[146:162]), n(l[162:178])
        alic, ret, total = n(l[178:183]), n(l[183:199]), n(l[199:215])
        if monto_comp - iva - otros != sujeto:
            errores.append(f"linea {i}: monto sujeto {sujeto} != {monto_comp} - {iva} - {otros}")
        esperado = dos_decimales(sujeto * alic / 100)
        if abs(esperado - ret) > Decimal("0.01"):
            errores.append(f"linea {i}: retencion {ret} != {sujeto} x {alic}/100 = {esperado}")
        if total != ret:
            errores.append(f"linea {i}: monto total retenido {total} != retencion {ret}")
    return errores


def plantilla_proveedores(ruta):
    """Escribe el CSV de datos maestros con lo que se puede deducir y el resto vacio.

    El N de inscripcion en IIBB de los contribuyentes locales no esta en ningun lado
    del Excel y AGIP lo exige. Es un dato estable: se carga una vez por proveedor.
    """
    conn = conectar()
    try:
        filas = [dict(x) for x in conn.execute(
            "SELECT p.cuit, p.razon_social, "
            "       COALESCE(p.situacion_ib, '') AS situacion_ib, "
            "       COALESCE(p.nro_inscripcion_ib, '') AS nro_inscripcion_ib, "
            "       COALESCE(p.situacion_iva, '') AS situacion_iva, "
            "       COUNT(r.id) AS retenciones "
            "FROM proveedores p "
            "JOIN comprobantes c ON c.cuit = p.cuit "
            "JOIN retenciones r ON r.comprobante_id = c.id AND r.impuesto = 'iibb_caba' "
            "GROUP BY p.cuit ORDER BY retenciones DESC")]
    finally:
        conn.close()
    for r in filas:
        # Convenio Multilateral: el numero de inscripcion es el propio CUIT
        if r["situacion_ib"] == "2" and not r["nro_inscripcion_ib"]:
            r["nro_inscripcion_ib"] = r["cuit"]
    campos = ["cuit", "razon_social", "situacion_ib", "nro_inscripcion_ib",
              "situacion_iva", "retenciones"]
    with open(ruta, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campos, delimiter=";")
        w.writeheader()
        w.writerows(filas)

    faltan = [r for r in filas if not r["nro_inscripcion_ib"]]
    print(f"{len(filas)} proveedores -> {Path(ruta).name}")
    print(f"   situacion IB y situacion IVA: completadas desde el padron y la letra del comprobante")
    print(f"   N de inscripcion en IIBB: {len(filas) - len(faltan)} completados "
          f"(Convenio Multilateral), faltan {len(faltan)} por cargar a mano")
    return filas


def main():
    ap = argparse.ArgumentParser(description="Genera el lote e-ARCIBA de un periodo")
    ap.add_argument("periodo", nargs="?", help="AAAA-MM, por ejemplo 2026-07")
    ap.add_argument("--plantilla", action="store_true",
                    help="escribe la plantilla de datos maestros de proveedores y termina")
    ap.add_argument("--salida")
    ap.add_argument("--proveedores", default=str(DIR / "proveedores_agip.csv"),
                    help="datos maestros de proveedores (N de inscripcion en IIBB)")
    ap.add_argument("--iva", type=float, default=0.0,
                    help="alicuota de IVA para declarar el total como monto del "
                         "comprobante. Por defecto 0: se declara el neto, que es el "
                         "criterio de los certificados actuales de Fiberhome.")
    args = ap.parse_args()

    if args.plantilla:
        plantilla_proveedores(args.proveedores)
        return
    if not args.periodo:
        ap.error("falta el periodo (o usa --plantilla)")

    lineas, avisos, derivados = generar(args.periodo, args.proveedores, args.iva)
    salida = Path(args.salida or DIR / f"AGIP_{args.periodo.replace('-', '')}.txt")
    salida.write_text("\r\n".join(lineas) + ("\r\n" if lineas else ""), encoding="utf-8")

    print(f"periodo {args.periodo}: {len(lineas)} lineas -> {salida.name}")
    if lineas:
        print(f"largos: {sorted({len(l) for l in lineas})}")
        errores = verificar(lineas)
        print(f"validaciones de AGIP: {'todas ok' if not errores else str(len(errores)) + ' ERRORES'}")
        for e in errores[:10]:
            print("  ", e)
    if derivados:
        print()
        print(f"FILAS AGRUPADAS ({len(derivados)}):")
        for d in derivados:
            print("  ", d)
    if avisos:
        print()
        print(f"NO EXPORTADAS ({len(avisos)}):")
        for etiqueta, motivos in avisos:
            print(f"   {etiqueta}: {'; '.join(motivos)}")


if __name__ == "__main__":
    main()
