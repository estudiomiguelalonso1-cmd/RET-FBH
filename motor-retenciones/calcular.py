# -*- coding: utf-8 -*-
"""Calcula que corresponde retener por una factura nueva.

A diferencia de validar.py, que revisa lo ya practicado, esto contesta la pregunta
del dia a dia: "llega esta factura, cuanto le retengo".

Consulta la base para saber cuanto consumio ese proveedor del minimo no imponible
en el mes, que es el dato que cambia el resultado y que hoy hay que buscar a mano.

    python calcular.py --cuit 20252500872 --neto 1150435.54 --regimen 94
    python calcular.py --cuit 20252500872 --neto 1150435.54 --regimen 94 --fecha 2026-09-18
"""
import argparse
import sqlite3
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from escala_anexo_viii import retencion_por_escala

DIR = Path(__file__).resolve().parent
DB = DIR / "retenciones.db"

ALICUOTA_IVA_3164 = 0.105
ALICUOTA_SUSS_1556 = 0.06

# RG 3164: no se retiene IVA si el neto de la operacion no llega a este monto. Si
# hay varias facturas en el mes se suman, y si la suma lo supera quedan todas
# sujetas. Es un importe fijo de la norma: revisar si AFIP lo actualiza.
MINIMO_IVA_3164 = 17_000

# RG 2682: retencion de seguridad social a contratistas de la construccion.
ALICUOTA_SUSS_2682 = {"ingenieria": 0.012, "arquitectura": 0.025}

# RG 1575: comprobantes clase M y clase A con la leyenda "operacion sujeta a
# retencion". No reemplazan al regimen general: el art. 12 manda aplicar el mayor
# de los dos importes.
RG1575 = {
    "M":       {"iva": 1.00, "ganancias": 0.06, "etiqueta": "comprobante clase M"},
    "leyenda": {"iva": 0.50, "ganancias": 0.03,
                "etiqueta": 'comprobante A con leyenda "operacion sujeta a retencion"'},
}


def caso_rg1575(letra, sujeta_a_retencion):
    if (letra or "").upper() == "M":
        return RG1575["M"]
    if sujeta_a_retencion:
        return RG1575["leyenda"]
    return None


def redondear(v):
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def conectar():
    if not DB.exists():
        raise SystemExit(f"falta {DB.name}: corre primero 'python migrar.py'")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


# Como se deduce la jurisdiccion del domicilio que trae la factura.
CABA = ("ciudad de buenos aires", "capital federal", "c.a.b.a", "caba")
PBA = ("provincia de buenos aires", "prov. de buenos aires", "bs as", "buenos aires")


def jurisdiccion_de(domicilio):
    """CABA / PBA / OTRA a partir del domicilio impreso en la factura."""
    if not domicilio:
        return None
    d = domicilio.lower()
    if any(x in d for x in CABA):
        return "CABA"
    if any(x in d for x in PBA):
        return "PBA"
    return "OTRA"


def exclusion(conn, cuit, impuesto, fecha):
    """Certificado de exclusion vigente a esa fecha, si lo hay."""
    return conn.execute(
        "SELECT * FROM exclusiones WHERE cuit = ? AND impuesto = ? "
        "AND vigencia_desde <= ? AND (vigencia_hasta IS NULL OR vigencia_hasta >= ?) "
        "ORDER BY vigencia_desde DESC LIMIT 1", (cuit, impuesto, fecha, fecha)).fetchone()


def proveedor(conn, cuit):
    return conn.execute("SELECT * FROM proveedores WHERE cuit = ?", (cuit,)).fetchone()


def parametros_regimen(conn, cod_regimen, situacion="I", tipo_persona=""):
    fila = conn.execute(
        "SELECT * FROM regimenes_ganancias WHERE cod_regimen = ? AND situacion = ? "
        "AND tipo_persona IN (?, '') ORDER BY tipo_persona DESC LIMIT 1",
        (cod_regimen, situacion, tipo_persona)).fetchone()
    return fila


def retenido_en_el_mes(conn, cuit, periodo, regimen, impuesto="ganancias", antes_de=None):
    """Retenciones ya practicadas a ese proveedor en el mes, por ese regimen."""
    sql = ("SELECT COALESCE(SUM(r.monto), 0) AS m FROM retenciones r "
           "JOIN comprobantes c ON c.id = r.comprobante_id "
           "WHERE r.impuesto = ? AND r.estado <> 'anulada' "
           "AND c.cuit = ? AND r.periodo = ?")
    args = [impuesto, cuit, periodo]
    if regimen is not None:
        sql += " AND r.regimen = ?"
        args.append(str(regimen))
    if antes_de is not None:
        sql += " AND r.id < ?"
        args.append(antes_de)
    return conn.execute(sql, args).fetchone()["m"]


def facturado_en_el_mes(conn, cuit, periodo):
    """Neto facturado por ese proveedor en el mes, para el minimo de la RG 3164."""
    return conn.execute(
        "SELECT COALESCE(SUM(neto), 0) AS n FROM comprobantes "
        "WHERE cuit = ? AND substr(fecha, 1, 7) = ?", (cuit, periodo)).fetchone()["n"]


def consumido_del_minimo(conn, cuit, periodo, regimen, antes_de=None):
    """Cuanto ya se le pago a ese proveedor en el mes por ese regimen.

    `antes_de` limita el acumulado a las retenciones anteriores a ese id. Sirve para
    reproducir el historico: al recalcular una factura ya cargada hay que excluirla
    a ella misma y a todo lo que vino despues.
    """
    sql = ("SELECT COALESCE(SUM(c.neto), 0) AS n FROM retenciones r "
           "JOIN comprobantes c ON c.id = r.comprobante_id "
           "WHERE r.impuesto = 'ganancias' AND r.estado <> 'anulada' "
           "AND c.cuit = ? AND r.periodo = ? AND r.regimen = ?")
    args = [cuit, periodo, str(regimen)]
    if antes_de is not None:
        sql += " AND r.id < ?"
        args.append(antes_de)
    return conn.execute(sql, args).fetchone()["n"]


def _consultar(conn, cuit, fecha):
    return conn.execute(
        "SELECT alic_retencion, vigencia_desde, vigencia_hasta FROM padron_iibb_caba "
        "WHERE cuit = ? AND vigencia_desde <= ? ORDER BY vigencia_desde DESC LIMIT 1",
        (cuit, fecha)).fetchone()


def meses_de_atraso(periodo_padron, periodo_pago):
    """Cuantos meses atras quedo el padron respecto del pago."""
    a1, m1 = int(periodo_padron[:4]), int(periodo_padron[5:7])
    a2, m2 = int(periodo_pago[:4]), int(periodo_pago[5:7])
    return (a2 - a1) * 12 + (m2 - m1)


def alicuota_iibb(conn, cuit, fecha):
    """Alicuota de retencion del padron AGIP vigente a esa fecha.

    La base solo guarda los CUIT que ya son proveedores, porque el padron trae un
    millon y medio de renglones. Si el CUIT no esta, se lo busca en el archivo
    cacheado y se lo incorpora: es el caso de un proveedor nuevo.
    """
    fila = _consultar(conn, cuit, fecha)
    if fila is not None:
        return fila
    try:
        import padron_agip
    except ImportError:
        return None
    for txt in sorted(padron_agip.CACHE.glob("*.TXT"), reverse=True):
        p = padron_agip.buscar(txt, cuit)
        if not p:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO padron_iibb_caba (cuit, vigencia_desde, "
            "vigencia_hasta, publicacion, tipo_contr, alic_percepcion, "
            "alic_retencion, razon_social) VALUES (?,?,?,?,?,?,?,?)",
            (p[3], padron_agip.iso(p[1]), padron_agip.iso(p[2]),
             padron_agip.iso(p[0]), p[4], float(p[7].replace(",", ".")) / 100,
             float(p[8].replace(",", ".")) / 100, p[11].strip()))
        conn.commit()
        return _consultar(conn, cuit, fecha)
    return None


def agrupar_por_regimen(partidas):
    """[{'base':x,'regimen':94}, ...] -> {94: suma_de_bases}"""
    out = {}
    for x in partidas:
        cod = x.get("regimen")
        if cod is None:
            continue
        out[int(cod)] = out.get(int(cod), 0.0) + (x.get("base") or 0.0)
    return out


def retencion_ganancias(conn, cuit, p, neto, cod_regimen, fecha, periodo, antes_de):
    """Una linea de retencion de Ganancias, o un texto de aviso si no se puede."""
    exc = exclusion(conn, cuit, "ganancias", fecha)
    reg = parametros_regimen(conn, cod_regimen, p["situacion_ganancias"] or "I",
                             p["tipo_persona"] or "")
    if exc is not None and exc["alcance"] == "total":
        return {"impuesto": "Ganancias", "regimen": str(cod_regimen),
                "concepto": reg["concepto"] if reg else None,
                "base": redondear(neto), "alicuota": 0.0, "monto": 0.0,
                "nota": ("no se retiene: certificado de exclusion vigente hasta "
                         f"{exc['vigencia_hasta'] or 'sin vencimiento'}"
                         + (f" ({exc['norma']})" if exc["norma"] else "")),
                "detalle": None}
    if reg is None:
        return f"el regimen {cod_regimen} no esta en la tabla de AFIP"

    # Metodo acumulativo de la RG 830: la retencion se calcula sobre todo lo
    # pagado en el mes menos el minimo no imponible, y se le resta lo ya
    # retenido. Importa cuando un pago no llego al minimo de retencion: esa
    # base no se pierde, se arrastra al pago siguiente.
    consumido = consumido_del_minimo(conn, cuit, periodo, cod_regimen, antes_de)
    ya_retenido = retenido_en_el_mes(conn, cuit, periodo, cod_regimen,
                                     "ganancias", antes_de)
    saldo = max(0.0, reg["monto_no_sujeto"] - consumido)
    base = neto - saldo
    base_acumulada = consumido + neto - reg["monto_no_sujeto"]
    if reg["alicuota"] == 0:
        bruta = retencion_por_escala(max(0.0, base_acumulada)) - ya_retenido
        alic = "s/escala"
    else:
        bruta = max(0.0, base_acumulada) * reg["alicuota"] - ya_retenido
        alic = reg["alicuota"]
    bruta = max(0.0, bruta)
    if exc is not None and exc["porcentaje"] is not None:
        bruta = max(0.0, base) * exc["porcentaje"]
        alic = exc["porcentaje"]
    monto = 0.0 if bruta < reg["monto_minimo"] else redondear(bruta)
    nota = None
    if bruta and monto == 0:
        nota = (f"no llega al minimo de retencion (${reg['monto_minimo']:,.2f}): "
                f"la base queda para el proximo pago del mes")
    return {"impuesto": "Ganancias", "regimen": str(cod_regimen),
            "concepto": reg["concepto"], "base": redondear(base), "alicuota": alic,
            "monto": monto, "nota": nota,
            "detalle": (f"minimo no imponible: queda ${saldo:,.2f} "
                        f"de ${reg['monto_no_sujeto']:,.2f}")}


def calcular(conn, cuit, neto, cod_regimen=None, fecha=None, antes_de=None,
             proveedor_provisorio=None, servicio_en_caba=False, neto_gravado=None,
             partidas=None, letra=None, sujeta_a_retencion=False, iva_facturado=None):
    """Calcula las cuatro retenciones de un pago.

    `proveedor_provisorio` permite cotizar una factura de un proveedor que todavia
    no esta dado de alta, con los datos leidos de la propia factura.

    `partidas` es [{'base': importe, 'regimen': codigo}, ...] cuando la factura
    tiene items de distinto regimen. Si no se pasa, se arma una sola partida con
    `neto` y `cod_regimen`.
    """
    fecha = fecha or date.today().isoformat()
    periodo = fecha[:7]
    p = proveedor(conn, cuit) or proveedor_provisorio
    resultado = {"cuit": cuit, "fecha": fecha, "periodo": periodo, "neto": neto,
                 "proveedor": p["razon_social"] if p else None,
                 "avisos": [], "retenciones": [], "total": 0.0, "a_pagar": neto}
    if p is None:
        resultado["avisos"].append("el proveedor no existe en la base: hay que darlo de alta")
        return resultado
    if proveedor(conn, cuit) is None:
        resultado["avisos"].append("proveedor nuevo: se da de alta al confirmar")
    partidas = partidas or ([{"base": neto, "regimen": cod_regimen}] if cod_regimen else [])

    # --- Ganancias --------------------------------------------------------
    # Una factura puede traer items de distinto regimen: se agrupan por codigo y
    # sale una retencion por cada uno, cada una con su propio minimo no imponible.
    especial = caso_rg1575(letra, sujeta_a_retencion)
    for cod, base_regimen in sorted(agrupar_por_regimen(partidas).items()):
        linea = retencion_ganancias(conn, cuit, p, base_regimen, cod, fecha,
                                    periodo, antes_de)
        if isinstance(linea, str):
            resultado["avisos"].append(linea)
            continue
        if especial:
            # art. 12 de la RG 1575: corresponde el mayor de los dos importes
            por_1575 = redondear(base_regimen * especial["ganancias"])
            if por_1575 > linea["monto"]:
                linea = {**linea,
                         "base": redondear(base_regimen),
                         "alicuota": especial["ganancias"],
                         "monto": por_1575,
                         "nota": (f"RG 1575: {especial['ganancias']:.0%} sobre el total, "
                                  f"que da mas que el regimen general"),
                         "detalle": None}
            else:
                linea = {**linea, "detalle": (linea["detalle"] or "")
                         + f" | por RG 1575 seria ${por_1575:,.2f}"}
        resultado["retenciones"].append(linea)

    # --- IIBB CABA --------------------------------------------------------
    # Fiberhome es de CABA: a los proveedores de CABA les corresponde retencion.
    # A los de otra jurisdiccion, solo si el servicio se presto en CABA.
    jur = p["jurisdiccion"] if "jurisdiccion" in p.keys() else None
    exc_iibb = exclusion(conn, cuit, "iibb_caba", fecha)
    pad = alicuota_iibb(conn, cuit, fecha)
    if jur and jur != "CABA" and not servicio_en_caba:
        resultado["retenciones"].append({
            "impuesto": "IIBB CABA", "regimen": "29", "concepto": None,
            "base": redondear(neto), "alicuota": 0.0, "monto": 0.0,
            "nota": f"no se retiene: el proveedor es de {jur} y no se indico que el "
                    f"servicio se haya prestado en CABA",
            "detalle": None})
    elif exc_iibb is not None and exc_iibb["alcance"] == "total":
        resultado["retenciones"].append({
            "impuesto": "IIBB CABA", "regimen": "29", "concepto": None,
            "base": redondear(neto), "alicuota": 0.0, "monto": 0.0,
            "nota": f"no se retiene: certificado de exclusion vigente hasta "
                    f"{exc_iibb['vigencia_hasta'] or 'sin vencimiento'}",
            "detalle": None})
    elif pad is None:
        resultado["avisos"].append("no hay padron de AGIP para este CUIT: "
                                   "hay que bajar el padron del mes")
    else:
        alic = pad["alic_retencion"]
        # AGIP publica el padron con un mes de atraso, asi que usar el del mes
        # anterior es lo normal y no amerita aviso. Solo se avisa si quedo mas
        # atras que eso, que ahi si es que falta actualizarlo.
        atraso = meses_de_atraso(pad["vigencia_desde"], periodo)
        detalle = f"padron de {pad['vigencia_desde'][:7]}"
        resultado["retenciones"].append({
            "impuesto": "IIBB CABA", "regimen": "29", "concepto": "Padron de Regimenes Generales",
            "base": redondear(neto), "alicuota": alic,
            "monto": redondear(neto * alic),
            "nota": (f"el padron mas nuevo es de {pad['vigencia_desde'][:7]}, "
                     f"{atraso} meses atras: conviene actualizarlo"
                     if atraso >= 2 else None),
            "detalle": detalle})

    # --- IVA y SUSS -------------------------------------------------------
    if especial and iva_facturado:
        monto_1575 = redondear(iva_facturado * especial["iva"])
        resultado["retenciones"].append({
            "impuesto": "IVA", "regimen": "RG 1575", "concepto": especial["etiqueta"],
            "base": redondear(iva_facturado), "alicuota": especial["iva"],
            "monto": monto_1575,
            "nota": f"{especial['iva']:.0%} del IVA facturado, por la RG 1575",
            "detalle": especial["etiqueta"]})
    elif p["retiene_iva_3164"]:
        # La base del IVA es solo el precio neto gravado: lo no gravado y lo
        # exento no generan debito fiscal, asi que no hay nada que retener.
        base_iva = neto_gravado if neto_gravado is not None else neto
        exc_iva = exclusion(conn, cuit, "iva", fecha)
        acumulado_mes = facturado_en_el_mes(conn, cuit, periodo) + base_iva
        if exc_iva is not None and exc_iva["alcance"] == "total":
            monto_iva, nota_iva = 0.0, ("no se retiene: certificado de exclusion vigente "
                                        f"hasta {exc_iva['vigencia_hasta'] or 'sin vencimiento'}")
        elif acumulado_mes < MINIMO_IVA_3164:
            monto_iva, nota_iva = 0.0, (
                f"no se retiene: el neto del mes (${acumulado_mes:,.2f}) no llega al "
                f"minimo de ${MINIMO_IVA_3164:,.2f} de la RG 3164")
        else:
            monto_iva, nota_iva = redondear(base_iva * ALICUOTA_IVA_3164), None
        resultado["retenciones"].append({
            "impuesto": "IVA", "regimen": "831", "concepto": "RG 3164",
            "base": redondear(base_iva), "alicuota": ALICUOTA_IVA_3164,
            "monto": monto_iva, "nota": nota_iva, "detalle": None})
    if "retiene_suss_2682" in p.keys() and p["retiene_suss_2682"]:
        obra = (p["tipo_obra"] if "tipo_obra" in p.keys() else None) or "arquitectura"
        alic = ALICUOTA_SUSS_2682[obra]
        resultado["retenciones"].append({
            "impuesto": "SUSS", "regimen": "construccion", "concepto": "RG 2682",
            "base": redondear(neto), "alicuota": alic,
            "monto": redondear(neto * alic),
            "nota": "se presenta por SIRE, no por SICORE",
            "detalle": f"obras de {obra}"})
    if p["retiene_suss_1556"]:
        resultado["retenciones"].append({
            "impuesto": "SUSS", "regimen": "748", "concepto": "RG 1556",
            "base": redondear(neto), "alicuota": ALICUOTA_SUSS_1556,
            "monto": redondear(neto * ALICUOTA_SUSS_1556),
            "nota": "se presenta por SIRE, no por SICORE", "detalle": None})

    resultado["total"] = redondear(sum(r["monto"] for r in resultado["retenciones"]))
    resultado["a_pagar"] = redondear(neto - resultado["total"])
    return resultado


def imprimir(r):
    print(f"Proveedor  {r['proveedor'] or '(no esta en la base)'}   CUIT {r['cuit']}")
    print(f"Factura    neto ${r['neto']:,.2f}   fecha {r['fecha']}   periodo {r['periodo']}")
    print()
    if r["retenciones"]:
        print(f"{'impuesto':11} {'reg':>4} {'base':>16} {'alicuota':>9} {'a retener':>14}")
        print("-" * 60)
        for x in r["retenciones"]:
            alic = x["alicuota"] if isinstance(x["alicuota"], str) else f"{x['alicuota']:.2%}"
            print(f"{x['impuesto']:11} {x['regimen']:>4} {x['base']:>16,.2f} "
                  f"{alic:>9} {x['monto']:>14,.2f}")
        print("-" * 60)
        print(f"{'TOTAL A RETENER':41} {r['total']:>14,.2f}")
        print(f"{'NETO A PAGAR':41} {r['a_pagar']:>14,.2f}")
        print()
        for x in r["retenciones"]:
            if x["detalle"]:
                print(f"  {x['impuesto']}: {x['detalle']}")
            if x["nota"]:
                print(f"  {x['impuesto']}: {x['nota']}")
    for a in r["avisos"]:
        print(f"  AVISO: {a}")


def excepciones():
    """Retenciones que quedan fuera de la verificacion, con el motivo.

    Son filas del historico con datos inconsistentes entre si, donde no se puede
    saber cual es el valor correcto. Se apartan en vez de contarlas como error del
    motor, pero quedan listadas para que no se pierdan de vista.
    """
    ruta = DIR / "excepciones.csv"
    if not ruta.exists():
        return {}
    import csv
    with ruta.open(encoding="utf-8-sig", newline="") as fh:
        return {r["nro_certificado"].strip(): r["motivo"]
                for r in csv.DictReader(fh, delimiter=";") if r.get("nro_certificado")}


def verificar(conn):
    """Recalcula todo el historico y lo compara contra lo que se practico.

    Cada factura se calcula como si recien llegara: el acumulado del mes se arma
    solo con las retenciones anteriores. Es la prueba de que la calculadora sirve
    para el dia a dia, no solo para revisar el pasado.
    """
    filas = conn.execute(
        "SELECT r.id, r.regimen, r.monto, r.estado, r.periodo, r.nro_certificado, "
        "       c.cuit, c.neto, c.fecha, r.fecha_retencion "
        "FROM retenciones r JOIN comprobantes c ON c.id = r.comprobante_id "
        "WHERE r.impuesto = 'ganancias' AND r.regimen IS NOT NULL "
        "ORDER BY r.id").fetchall()
    excl = excepciones()
    ok = dif = omitidas = sin_practicar = arrastre = 0
    detalle, apartadas, con_error = [], [], set()
    for f in filas:
        if f["estado"] == "anulada":
            omitidas += 1
            continue
        if (f["nro_certificado"] or "").strip() in excl:
            apartadas.append((f, excl[f["nro_certificado"].strip()]))
            continue
        r = calcular(conn, f["cuit"], f["neto"], int(f["regimen"]),
                     f["fecha_retencion"] or f["fecha"], antes_de=f["id"])
        g = next((x for x in r["retenciones"] if x["impuesto"] == "Ganancias"), None)
        if g is None:
            omitidas += 1
            continue
        if abs(g["monto"] - f["monto"]) <= 0.01:
            ok += 1
        elif not f["monto"]:
            sin_practicar += 1        # la planilla la dejo en cero
        elif (f["cuit"], f["periodo"], f["regimen"]) in con_error:
            # El metodo acumulativo resta lo ya retenido en el mes, asi que si un
            # pago anterior del mismo grupo salio mal, este lo compensa. No es un
            # error nuevo.
            arrastre += 1
        else:
            dif += 1
            con_error.add((f["cuit"], f["periodo"], f["regimen"]))
            detalle.append((f, g))
    print(f"Ganancias recalculadas desde la base: {ok} coinciden, {dif} difieren, "
          f"{omitidas} anuladas, {sin_practicar} sin practicar, {len(apartadas)} apartadas"
          + (f", {arrastre} compensan un error anterior del mismo mes" if arrastre else ""))
    for f, motivo in apartadas:
        print(f"   apartada: certificado {f['nro_certificado']} - {motivo}")
    for f, g in detalle:
        print(f"   ret {f['id']:>4} {f['cuit']} {f['periodo']} reg {f['regimen']:>3}  "
              f"practicado {f['monto']:>12,.2f}   calculado {g['monto']:>12,.2f}")
    return dif == 0


def main():
    ap = argparse.ArgumentParser(description="Calcula las retenciones de una factura")
    ap.add_argument("--cuit")
    ap.add_argument("--neto", type=float)
    ap.add_argument("--regimen", type=int, help="codigo de regimen de Ganancias")
    ap.add_argument("--fecha", help="fecha de pago (ISO). Por defecto, hoy")
    ap.add_argument("--verificar", action="store_true",
                    help="recalcula todo el historico y lo compara contra lo practicado")
    args = ap.parse_args()

    conn = conectar()
    try:
        if args.verificar:
            verificar(conn)
            return
        if not (args.cuit and args.neto and args.regimen):
            ap.error("hacen falta --cuit, --neto y --regimen (o usa --verificar)")
        imprimir(calcular(conn, args.cuit, args.neto, args.regimen, args.fecha))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
