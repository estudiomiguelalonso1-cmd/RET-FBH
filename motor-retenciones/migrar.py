# -*- coding: utf-8 -*-
"""Migra el historico 2026 de los dos Excel a la base de datos.

    python migrar.py            crea retenciones.db desde cero y concilia

Lo que hace, en orden:

  1. crea la base con esquema.sql
  2. carga las tablas normativas (regimenes de AFIP, escala del Anexo VIII, padron
     de AGIP reconstruido desde los renglones pegados en el Excel de CABA)
  3. da de alta los proveedores, cruzando las dos planillas y proveedores_agip.csv
  4. crea un comprobante por factura, uniendo la fila de Ganancias con la de IIBB
     cuando son la misma factura
  5. cuelga de cada comprobante sus retenciones (hasta cuatro: ganancias, iibb,
     iva, suss)
  6. concilia: los totales de la base tienen que dar iguales a los del Excel

La migracion no corrige nada. Los errores detectados al validar el motor (fila 195
de Proveedor B, filas 36 y 86 de SUSS) entran tal cual estan, con su observacion: la base
tiene que reflejar lo que se presento, no lo que deberia haberse presentado.
"""
import csv
import json
import sqlite3
from pathlib import Path

import motor_caba
from escala_anexo_viii import UNIDAD_2026, tramos
from exportar_agip import SITUACION_IVA
from exportar_sicore import nro_comprobante

DIR = Path(__file__).resolve().parent
BASE = DIR.parent
DB = DIR / "retenciones.db"

IMPUESTO_IVA_REGIMEN = "831"      # RG 3164
IMPUESTO_SUSS_REGIMEN = "748"     # RG 1556


def crear(conn):
    conn.executescript((DIR / "esquema.sql").read_text(encoding="utf-8"))


# --------------------------------------------------------------- normativas
def cargar_normativas(conn, H):
    afip = json.loads((BASE / "diseno-sistema-retenciones" /
                       "regimenes_ganancias_afip.json").read_text(encoding="utf-8"))
    conn.executemany(
        "INSERT INTO regimenes_ganancias (id_alicuota, cod_regimen, situacion, "
        "tipo_persona, concepto, anexo, alicuota, monto_no_sujeto, monto_minimo, "
        "sincronizado) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r["ID_ALICUOTA"], r["COD_REGIMEN"], r["SITUACION"], r["TIPO_PERSONA"] or "",
          r["CONCEPTO"],
          r["ANEXO"], r["PORCENT_A_RETENER"] / 100, r["MONTO_NO_SUJETO"],
          r["MONTO_MINIMO"], afip["fecha_descarga"]) for r in afip["regimenes"]])

    conn.executemany(
        "INSERT INTO escala_ganancias (vigencia_desde, desde, monto_fijo, alicuota) "
        "VALUES (?,?,?,?)",
        [("2026-01-01", desde, fijo, tasa) for desde, fijo, tasa in tramos(UNIDAD_2026)])

    # El padron se reconstruye desde los renglones que el Excel de CABA pega en la
    # columna M: es la unica copia del padron de AGIP que hay en la carpeta.
    vistos = {}
    for f in H["caba"]:
        p = motor_caba.parse_padron(f["padron_crudo"])
        if not p:
            continue
        def iso(d):      # 'DDMMAAAA' -> ISO
            return f"{d[4:8]}-{d[2:4]}-{d[0:2]}"
        vistos[(p["cuit"], iso(p["desde"]))] = (
            p["cuit"], iso(p["desde"]), iso(p["hasta"]), iso(p["publicacion"]),
            p["tipo_contr"], p["alic_percepcion"], p["alic_retencion"],
            p["razon_social"])
    conn.executemany(
        "INSERT INTO padron_iibb_caba (cuit, vigencia_desde, vigencia_hasta, "
        "publicacion, tipo_contr, alic_percepcion, alic_retencion, razon_social) "
        "VALUES (?,?,?,?,?,?,?,?)", list(vistos.values()))
    return len(afip["regimenes"]), len(vistos)


# -------------------------------------------------------------- proveedores
def cargar_proveedores(conn, H):
    prov = {}
    for f in H["ganancias"] + H["caba"]:
        if not f["cuit"]:
            continue
        r = prov.setdefault(f["cuit"], {"razon_social": None, "situacion_iva": None,
                                        "situacion_ib": None, "nro_inscripcion_ib": None,
                                        "iva_3164": 0, "suss_1556": 0})
        # quien tuvo retencion de IVA o SUSS alguna vez, esta alcanzado por el regimen
        if f.get("ret_iva_966"):
            r["iva_3164"] = 1
        if f.get("ret_suss"):
            r["suss_1556"] = 1
        # se queda con el nombre mas largo, que suele ser el mas completo
        if f["razon_social"] and (not r["razon_social"]
                                  or len(f["razon_social"]) > len(r["razon_social"])):
            r["razon_social"] = f["razon_social"]
        if f.get("fc_tipo") and not r["situacion_iva"]:
            r["situacion_iva"] = SITUACION_IVA.get(f["fc_tipo"])
        p = motor_caba.parse_padron(f.get("padron_crudo"))
        if p and p["situacion_ib"]:
            r["situacion_ib"] = p["situacion_ib"]
            if p["situacion_ib"] == "2":
                r["nro_inscripcion_ib"] = f["cuit"]

    csv_prov = DIR / "proveedores_agip.csv"
    if csv_prov.exists():
        with csv_prov.open(encoding="utf-8-sig", newline="") as fh:
            for fila in csv.DictReader(fh, delimiter=";"):
                r = prov.get(fila["cuit"].strip())
                if not r:
                    continue
                for k in ("situacion_ib", "situacion_iva", "nro_inscripcion_ib"):
                    if fila.get(k):
                        r[k] = fila[k].strip()

    conn.executemany(
        "INSERT INTO proveedores (cuit, razon_social, tipo_persona, situacion_iva, "
        "situacion_ib, nro_inscripcion_ib, retiene_iva_3164, retiene_suss_1556) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(cuit, r["razon_social"] or cuit,
          "H" if cuit[:2] in ("20", "23", "24", "27") else "J",
          r["situacion_iva"], r["situacion_ib"], r["nro_inscripcion_ib"],
          r["iva_3164"], r["suss_1556"])
         for cuit, r in prov.items()])
    return len(prov)


# ------------------------------------------------- comprobantes y retenciones
def clave(f):
    """Clave para reconocer que dos filas hablan de la misma factura."""
    nc, _ = nro_comprobante(f["nro_factura"])
    return (f["cuit"], nc) if nc else (f["cuit"], f["periodo"], round(f["monto_neto"], 2))


def estado_y_motivo(f, impuesto):
    if f["anulada"]:
        return "anulada", "marcada en rojo: no corresponde / anulada"
    if not f["retencion"]:
        return "no_practicada", "la planilla la dejo en cero"
    return "practicada", None


def cargar_operaciones(conn, H):
    comprobantes, sin_par = {}, 0

    def alta_comprobante(f, origen):
        k = clave(f)
        if k in comprobantes:
            return comprobantes[k]
        nc, nota = nro_comprobante(f["nro_factura"])
        cur = conn.execute(
            "INSERT INTO comprobantes (cuit, tipo, letra, punto_venta, numero, "
            "numero_crudo, agrupa_varias, fecha, fecha_cruda, pzf, neto, moneda, "
            "origen, observacion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["cuit"], f.get("comp") or "FC", f.get("fc_tipo"),
             nc[:5] if nc else None, nc[5:] if nc else None, f["nro_factura"],
             1 if (nota and "agrupa" in nota) else 0,
             f["fecha_factura"] if (f["fecha_factura"] or "")[:4].isdigit() else None,
             f["fecha_factura"], f.get("pzf"), f["monto_neto"],
             "USD" if f.get("fc_usd") else "ARS", origen, nota))
        comprobantes[k] = cur.lastrowid
        return cur.lastrowid

    def alta_retencion(cid, f, impuesto, regimen, base, alicuota, monto, origen):
        estado, motivo = estado_y_motivo(f, impuesto)
        conn.execute(
            "INSERT INTO retenciones (comprobante_id, impuesto, regimen, periodo, "
            "base_imponible, alicuota, monto, fecha_retencion, nro_certificado, "
            "estado, motivo, origen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, impuesto, regimen, f["periodo"], base, alicuota, monto or 0,
             f["fecha_retencion"], f["nro_certificado"], estado, motivo, origen))

    for f in H["ganancias"]:
        origen = f"Excel Ganancias fila {f['fila']}"
        cid = alta_comprobante(f, origen)
        alta_retencion(cid, f, "ganancias", str(f["regimen"]) if f["regimen"] else None,
                       f["base_imponible"], f["alicuota"], f["retencion"], origen)
        if f["ret_iva_966"]:
            alta_retencion(cid, f, "iva", IMPUESTO_IVA_REGIMEN,
                           f["monto_neto"], 0.105, f["ret_iva_966"], origen)
        if f["ret_suss"]:
            alta_retencion(cid, f, "suss", IMPUESTO_SUSS_REGIMEN,
                           f["monto_neto"], 0.06, f["ret_suss"], origen)

    for f in H["caba"]:
        origen = f"Excel CABA fila {f['fila']}"
        k = clave(f)
        if k not in comprobantes:
            sin_par += 1
        cid = alta_comprobante(f, origen)
        alta_retencion(cid, f, "iibb_caba", "29", f["monto_neto"], f["alicuota"],
                       f["retencion"], origen)
    return len(comprobantes), sin_par


# ---------------------------------------------------------------- conciliacion
def conciliar(conn, H):
    ok = True
    print()
    print(f"{'impuesto':12} {'filas Excel':>12} {'filas base':>11} "
          f"{'total Excel':>16} {'total base':>16}  ok")
    print("-" * 78)
    esperado = {
        "ganancias": ([f for f in H["ganancias"]],
                      sum(f["retencion"] or 0 for f in H["ganancias"])),
        "iibb_caba": ([f for f in H["caba"]],
                      sum(f["retencion"] or 0 for f in H["caba"])),
        "iva": ([f for f in H["ganancias"] if f["ret_iva_966"]],
                sum(f["ret_iva_966"] or 0 for f in H["ganancias"])),
        "suss": ([f for f in H["ganancias"] if f["ret_suss"]],
                 sum(f["ret_suss"] or 0 for f in H["ganancias"])),
    }
    for imp, (filas, total) in esperado.items():
        n, suma = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(monto), 0) FROM retenciones WHERE impuesto = ?",
            (imp,)).fetchone()
        bien = n == len(filas) and abs(suma - total) < 0.01
        ok &= bien
        print(f"{imp:12} {len(filas):>12} {n:>11} {total:>16,.2f} {suma:>16,.2f}  "
              f"{'si' if bien else 'NO'}")
    return ok


def main():
    if DB.exists():
        DB.unlink()
    H = json.loads((DIR / "historico.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    try:
        crear(conn)
        n_reg, n_pad = cargar_normativas(conn, H)
        n_prov = cargar_proveedores(conn, H)
        n_comp, sin_par = cargar_operaciones(conn, H)
        conn.commit()

        print(f"base creada: {DB.name}")
        print(f"  regimenes de ganancias (AFIP)  {n_reg:>5}")
        print(f"  tramos de la escala Anexo VIII {len(tramos()):>5}")
        print(f"  renglones de padron AGIP       {n_pad:>5}")
        print(f"  proveedores                    {n_prov:>5}")
        print(f"  comprobantes                   {n_comp:>5}")
        n_ret = conn.execute("SELECT COUNT(*) FROM retenciones").fetchone()[0]
        print(f"  retenciones                    {n_ret:>5}")
        print(f"  filas de IIBB sin par en Ganancias: {sin_par}")

        if conciliar(conn, H):
            print()
            print("conciliacion: los cuatro impuestos coinciden con el Excel")
        else:
            print()
            print("CONCILIACION CON DIFERENCIAS - revisar antes de usar la base")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
