# -*- coding: utf-8 -*-
"""Genera el archivo de texto de importacion a SICORE (diseno de registro F.744).

El layout se tomo del archivo que produce el ERP para otro cliente
(`SICORE (3).txt`): 18 lineas de 198 caracteres, ya aceptado por AFIP. Las
posiciones de abajo estan verificadas caracter por caracter contra ese archivo.

Uso:
    python exportar_sicore.py 2026-07
    python exportar_sicore.py 2026-07 --salida SICORE_202607.txt
"""
import argparse, json, re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

DIR = Path(__file__).resolve().parent

LARGO_LINEA = 198

# --- codigos ---------------------------------------------------------------
COD_COMPROBANTE = "01"     # 01 = Factura / Tique, como dice el certificado de Fiberhome
COD_OPERACION = "1"        # 1 = retencion
COD_CONDICION = "01"
SUSPENDIDO = "0"
TIPO_DOC_CUIT = "80"

# Codigo de impuesto por tributo, para el archivo de SICORE.
#
# Solo Ganancias. Los certificados que emite Fiberhome muestran que sus otras dos
# retenciones salen por SIRE y no por SICORE:
#
#   IVA  -> F. 2005, impuesto 216 "SIRE - IVA", regimen 831
#   SUSS -> F. 2004, impuesto 353 "Retenciones contrib. seg. social", regimen 748
#
# (El archivo del ERP de otro cliente si declara el IVA por SICORE con 767/831.
# Se respeta lo que hace Fiberhome, que es lo que estan emitiendo.)
COD_IMPUESTO = {
    "ganancias": "217",
    "iva": None,           # va por SIRE (F. 2005)
    "suss": None,          # va por SIRE (F. 2004)
}
# Codigo de regimen para los tributos que no lo traen por fila.
#
# 831 = "Empresas de limpieza Edif, Investig y seg. y recolec. resid." (RG 3164),
# 748 = "Reten contrib seg soc prestadores serv limpieza inmuebles" (RG 1556).
# Los dos verificados contra los certificados que emite Fiberhome.
COD_REGIMEN_FIJO = {
    "iva": "831",
    "suss": "748",
}

# Que va en "Importe del comprobante" (pos 29-44).
#
#   "neto"  = el neto de la factura. Es lo que Fiberhome viene declarando por SIAP:
#             el certificado 0000-2026-000174 de Proveedor A imprime $1.150.435,54,
#             que es el neto del Excel y no el total con IVA ($1.392.027,00).
#   "total" = neto x (1 + IVA). Es lo que escribe el ERP para el otro cliente
#             (en su archivo, importe / base = 1,21 exacto).
#
# DECIDIDO: se mantiene "neto", que es el criterio con el que se viene cargando.
# No se cambia el criterio sin que lo confirme el contador. El sistema nuevo tiene
# que guardar la factura completa (neto, IVA, percepciones, total) para que el dato
# este disponible si alguna vez hay que declarar el total.
IMPORTE_COMPROBANTE = "neto"
IVA_POR_DEFECTO = 0.21


def campo(valor, largo, alinear="izq", relleno=" "):
    v = str(valor)
    if len(v) > largo:
        raise ValueError(f"'{v}' no entra en {largo} caracteres")
    return v.ljust(largo, relleno) if alinear == "izq" else v.rjust(largo, relleno)


def importe(valor, largo=14):
    """Importe con dos decimales y redondeo financiero (medio hacia arriba)."""
    v = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return campo(f"{v:.2f}", largo, "der")


def fecha(texto, anio_defecto=None):
    """Devuelve (dd/mm/aaaa, nota).

    La columna de fecha de factura a veces trae un rango en vez de una fecha,
    porque la fila agrupa varias facturas ('26/02 - 28/02/2026',
    '22/04_08/05_13/05.'). En ese caso se toma la mas reciente, que es el criterio
    con el que se emite el certificado agrupado.
    """
    if not texto:
        return None, "sin fecha de factura"
    t = str(texto).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        a, m, d = t.split("-")
        return f"{d}/{m}/{a}", None

    crudas = re.findall(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", t)
    if not crudas:
        return None, f"fecha ilegible ({texto!r})"
    fechas = []
    for d, m, a in crudas:
        if a:
            a = int(a)
            a += 2000 if a < 100 else 0
        elif anio_defecto:
            a = anio_defecto
        else:
            continue
        try:
            fechas.append(date(a, int(m), int(d)))
        except ValueError:
            continue
    if not fechas:
        return None, f"fecha ilegible ({texto!r})"
    elegida = max(fechas)
    nota = None
    if len(fechas) > 1:
        nota = (f"la fila cubre {len(fechas)} fechas ({texto!r}); "
                f"se toma la mas reciente: {elegida:%d/%m/%Y}")
    return f"{elegida:%d/%m/%Y}", nota


def _expandir(base, sufijo):
    """'00005799' + '5800' -> '00005800'. El sufijo reemplaza los ultimos digitos."""
    if len(sufijo) >= len(base):
        return sufijo.zfill(len(base))
    return base[: len(base) - len(sufijo)] + sufijo


def nro_comprobante(texto):
    """Devuelve (punto_de_venta+numero, nota).

    Una fila puede agrupar varias facturas del mismo regimen bajo un unico
    certificado ('00010-00005799/5800', '00001-5888/5893/.../5889'). En ese caso
    se declara la factura mas reciente, que es el criterio de carga actual.
    """
    if not texto:
        return None, "sin numero de factura"
    t = re.sub(r"\s+", "", str(texto))       # '0003-0000 5195/5198' -> sin espacios
    nums = re.findall(r"\d+", t)
    if len(nums) < 2:
        return None, f"no tiene punto de venta y numero ({texto!r})"
    pv, base = nums[0].lstrip("0") or "0", nums[1]
    if int(base) == 0:
        return None, f"el numero de factura es cero ({texto!r})"
    base = str(int(base))                     # '000000004' -> '4'
    if len(pv) > 5 or len(base) > 8:
        return None, f"punto de venta o numero demasiado largos ({texto!r})"

    base = base.zfill(8)
    candidatos = [base] + [_expandir(base, s) for s in nums[2:]]
    if any(len(c) > 8 for c in candidatos):
        return None, f"numero de factura demasiado largo ({texto!r})"
    elegido = max(candidatos, key=int)
    nota = None
    if len(candidatos) > 1:
        nota = (f"la fila agrupa {len(candidatos)} facturas ({texto!r}); "
                f"se declara la mas reciente: {pv.zfill(5)}-{elegido.zfill(8)}")
    return pv.zfill(5) + elegido.zfill(8), nota


def linea(*, cod_comprobante, fecha_comprobante, nro_comp, total_comprobante,
          cod_impuesto, cod_regimen, base_calculo, fecha_retencion, monto_retencion, cuit):
    """Arma un registro de 198 caracteres."""
    partes = [
        campo(cod_comprobante, 2),                  # 1-2
        campo(fecha_comprobante, 10),               # 3-12
        campo(nro_comp, 16),                        # 13-28
        importe(total_comprobante, 16),             # 29-44
        "0",                                        # 45   relleno
        campo(cod_impuesto, 3, "der", "0"),         # 46-48
        campo(cod_regimen, 3),                      # 49-51  alineado a izquierda
        campo(COD_OPERACION, 1),                    # 52
        importe(base_calculo, 14),                  # 53-66
        campo(fecha_retencion, 10),                 # 67-76
        campo(COD_CONDICION, 2),                    # 77-78
        campo(SUSPENDIDO, 1),                       # 79
        importe(monto_retencion, 14),               # 80-93
        campo("", 6),                               # 94-99   porcentaje de exclusion
        campo("", 10),                              # 100-109 fecha de publicacion
        campo(TIPO_DOC_CUIT, 2),                    # 110-111
        campo(cuit, 20),                            # 112-131
    ]
    return "".join(partes).ljust(LARGO_LINEA)


def retenciones_del_periodo(H, periodo):
    """Devuelve (lineas, avisos) para un periodo AAAA-MM."""
    filas, avisos = [], []
    for f in H["ganancias"]:
        if f["periodo"] != periodo or f["anulada"]:
            continue
        if not f["retencion"]:
            continue                                # no se practico retencion
        base = {"fila": f["fila"], "cuit": f["cuit"], "razon_social": f["razon_social"],
                "fecha_comprobante": f["fecha_factura"], "fecha_retencion": f["fecha_retencion"],
                "nro_factura": f["nro_factura"], "neto": f["monto_neto"]}
        filas.append({**base, "tributo": "ganancias", "regimen": str(f["regimen"]),
                      "monto": f["retencion"]})
        if f["ret_iva_966"]:
            filas.append({**base, "tributo": "iva", "regimen": COD_REGIMEN_FIJO["iva"],
                          "monto": f["ret_iva_966"]})
        if f["ret_suss"]:
            filas.append({**base, "tributo": "suss", "regimen": COD_REGIMEN_FIJO["suss"],
                          "monto": f["ret_suss"]})
    return filas, avisos


def generar(periodo, iva=IVA_POR_DEFECTO, criterio=IMPORTE_COMPROBANTE):
    H = json.loads((DIR / "historico.json").read_text(encoding="utf-8"))
    filas, avisos = retenciones_del_periodo(H, periodo)
    lineas, sire, derivados = [], [], []
    for r in filas:
        cod_imp = COD_IMPUESTO[r["tributo"]]
        if r["tributo"] in ("iva", "suss"):
            sire.append(r)                      # se declaran aparte, por SIRE
            continue
        if cod_imp is None or r["regimen"] in (None, "None"):
            avisos.append(f"fila {r['fila']}: falta el codigo de impuesto/regimen para "
                          f"{r['tributo'].upper()} - linea omitida")
            continue
        fr, _ = fecha(r["fecha_retencion"])
        anio = int(fr[-4:]) if fr else None
        fc, nota_fecha = fecha(r["fecha_comprobante"], anio)
        nc, nota_nro = nro_comprobante(r["nro_factura"])
        if not fr:
            avisos.append(f"fila {r['fila']}: sin fecha de retencion - linea omitida")
            continue
        if not fc:
            avisos.append(f"fila {r['fila']}: {nota_fecha} - linea omitida")
            continue
        if not nc:
            avisos.append(f"fila {r['fila']}: {nota_nro} - linea omitida")
            continue
        for n in (nota_nro, nota_fecha):
            if n:
                derivados.append(f"fila {r['fila']}: {n}")
        lineas.append(linea(
            cod_comprobante=COD_COMPROBANTE, fecha_comprobante=fc, nro_comp=nc,
            total_comprobante=r["neto"] * (1 + iva) if criterio == "total" else r["neto"],
            cod_impuesto=cod_imp, cod_regimen=r["regimen"],
            base_calculo=r["neto"], fecha_retencion=fr,
            monto_retencion=r["monto"], cuit=r["cuit"]))
    return lineas, avisos, sire, derivados


def main():
    ap = argparse.ArgumentParser(description="Genera el lote SICORE de un periodo")
    ap.add_argument("periodo", help="AAAA-MM, por ejemplo 2026-07")
    ap.add_argument("--salida", help="archivo de salida (por defecto SICORE_AAAAMM.txt)")
    ap.add_argument("--iva", type=float, default=IVA_POR_DEFECTO,
                    help="alicuota de IVA, solo si --importe total")
    ap.add_argument("--importe", choices=("neto", "total"), default=IMPORTE_COMPROBANTE,
                    help="que declarar en 'importe del comprobante' (por defecto: neto)")
    args = ap.parse_args()

    lineas, avisos, sire, derivados = generar(args.periodo, args.iva, args.importe)
    salida = Path(args.salida or DIR / f"SICORE_{args.periodo.replace('-', '')}.txt")
    salida.write_text("\r\n".join(lineas) + "\r\n", encoding="latin-1")

    print(f"periodo {args.periodo}: {len(lineas)} lineas -> {salida.name}")
    print(f"largos: {sorted({len(l) for l in lineas})}")
    if derivados:
        print()
        print(f"FILAS AGRUPADAS ({len(derivados)}) - revisar que el comprobante elegido sea el correcto:")
        for d in derivados:
            print("  ", d)
        print()
    if sire:
        print(f"{len(sire)} retencion(es) de IVA/SUSS no van en este archivo: se declaran")
        print("por SIRE (F. 2005 y F. 2004). Ver INFORME_VALIDACION.md.")
    if avisos:
        print()
        print(f"AVISOS ({len(avisos)}):")
        for a in avisos:
            print("  ", a)
    print()
    if args.importe == "neto":
        print("'Importe del comprobante' (pos 29-44) declarado con el NETO, que es el")
        print("criterio de los certificados actuales de Fiberhome. El ERP, para el otro")
        print("cliente, declara ahi el total con IVA. Ver INFORME_VALIDACION.md.")
    else:
        print(f"'Importe del comprobante' estimado como neto x {1 + args.iva:.2f}: el Excel no")
        print("guarda el total real, asi que si la factura tiene percepciones queda mal.")


if __name__ == "__main__":
    main()
