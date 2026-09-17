# -*- coding: utf-8 -*-
"""Motor de calculo de Retencion de Ganancias (RG 830) + validacion contra el historico.

Regla implementada:
  saldo del minimo no imponible = MONTO_NO_SUJETO del regimen, se reinicia cada mes
  y se consume pago a pago para el mismo beneficiario.

    base    = monto_neto - saldo_no_consumido
    bruta   = max(0, base) * alicuota
    retenc. = 0 si bruta < MONTO_MINIMO ($240), si no bruta
    saldo'  = max(0, saldo - monto_neto)

Se prueban dos criterios de acumulacion para ver cual reproduce la planilla:
  A) el minimo se acumula por (CUIT, periodo, regimen)
  B) el minimo se acumula por (CUIT, periodo)
"""
import json
from collections import defaultdict
from pathlib import Path

from escala_anexo_viii import retencion_por_escala

DIR = Path(__file__).resolve().parent
HIST = json.loads((DIR / "historico.json").read_text(encoding="utf-8"))
AFIP = json.loads(
    (DIR.parent / "diseno-sistema-retenciones" / "regimenes_ganancias_afip.json")
    .read_text(encoding="utf-8")
)

# Fiberhome retiene siempre a inscriptos (situacion I) en la planilla 2026.
TABLA = {
    int(r["COD_REGIMEN"]): r
    for r in AFIP["regimenes"]
    if r["SITUACION"] == "I"
}


def parametros(regimen):
    r = TABLA.get(int(regimen))
    if r is None:
        return None
    return {
        "alicuota": r["PORCENT_A_RETENER"] / 100,
        "no_sujeto": r["MONTO_NO_SUJETO"],
        "minimo": r["MONTO_MINIMO"],
        "concepto": r["CONCEPTO"],
    }


def calcular(monto_neto, saldo_no_sujeto, p):
    base = monto_neto - saldo_no_sujeto
    if p["alicuota"] == 0:
        # alicuota 0 para inscriptos = "s/escala": Anexo VIII de la RG 830
        bruta = retencion_por_escala(max(0.0, base))
    else:
        bruta = max(0.0, base) * p["alicuota"]
    retencion = 0.0 if bruta < p["minimo"] else bruta
    saldo_nuevo = max(0.0, saldo_no_sujeto - monto_neto)
    return base, retencion, saldo_nuevo


def correr(criterio):
    saldos = {}
    resultados = []
    for f in HIST["ganancias"]:
        if f["anulada"]:
            # "No corresponde / Anulada": no se retiene y NO consume el minimo.
            resultados.append((f, None, None, "anulada"))
            continue
        if f["regimen"] is None:
            resultados.append((f, None, None, "sin regimen"))
            continue
        try:
            reg = int(f["regimen"])
        except (TypeError, ValueError):
            resultados.append((f, None, None, f"regimen no numerico: {f['regimen']!r}"))
            continue
        p = parametros(reg)
        if p is None:
            resultados.append((f, None, None, f"regimen {reg} fuera de tabla AFIP"))
            continue
        clave = (f["cuit"], f["periodo"], reg) if criterio == "A" else (f["cuit"], f["periodo"])
        saldo = saldos.get(clave, p["no_sujeto"])
        base, ret, saldo_nuevo = calcular(f["monto_neto"], saldo, p)
        saldos[clave] = saldo_nuevo
        resultados.append((f, base, ret, None))
    return resultados


def comparar(resultados, tol=0.01):
    """-> (ok, ko, na, fallos, no_practicadas)

    Las filas que la planilla dejo en cero no se cuentan como diferencia: se
    apartan como "no practicadas" (factura no pagada o anulada sin marcar).
    """
    ok = ko = na = 0
    fallos, no_practicadas = [], []
    for f, base, ret, err in resultados:
        if err:
            na += 1
            continue
        b_real, r_real = f["base_imponible"], f["retencion"]
        b_ok = b_real is not None and abs(base - b_real) <= tol
        r_ok = r_real is not None and abs(ret - r_real) <= tol
        if b_ok and r_ok:
            ok += 1
        elif not r_real and ret > 0:
            no_practicadas.append((f, base, ret))
        else:
            ko += 1
            fallos.append((f, base, ret, b_ok, r_ok))
    return ok, ko, na, fallos, no_practicadas


def main():
    for criterio in ("A", "B"):
        ok, ko, na, fallos, _ = comparar(correr(criterio))
        etiqueta = "por (CUIT, periodo, regimen)" if criterio == "A" else "por (CUIT, periodo)"
        print(f"criterio {criterio} {etiqueta:32}  coinciden {ok:3}   difieren {ko:3}   sin calcular {na}")

    print()
    ok, ko, na, fallos, _ = comparar(correr("A"))
    print(f"=== detalle de las {len(fallos)} diferencias (criterio A) ===")
    hdr = f"{'fila':>5} {'per':7} {'reg':>4} {'proveedor':28} {'neto':>14} " \
          f"{'base calc':>14} {'base Excel':>14} {'ret calc':>12} {'ret Excel':>12}"
    print(hdr)
    print("-" * len(hdr))
    for f, base, ret, b_ok, r_ok in fallos:
        print(f"{f['fila']:>5} {f['periodo']:7} {str(f['regimen']):>4} "
              f"{(f['razon_social'] or '')[:28]:28} {f['monto_neto']:>14,.2f} "
              f"{base:>14,.2f} {(f['base_imponible'] if f['base_imponible'] is not None else float('nan')):>14,.2f} "
              f"{ret:>12,.2f} {(f['retencion'] if f['retencion'] is not None else float('nan')):>12,.2f}"
              f"  {'' if b_ok else 'BASE'} {'' if r_ok else 'RET'}")


if __name__ == "__main__":
    main()
