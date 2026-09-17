# -*- coding: utf-8 -*-
"""Motor de Retencion de Ingresos Brutos CABA (AGIP) + validacion contra el historico.

A diferencia de Ganancias, IIBB CABA no tiene minimo no imponible ni acumulado
mensual: la alicuota sale del padron de regimenes generales de AGIP vigente para
ese CUIT ese mes, y se aplica directo sobre el neto.

    retencion = neto * alicuota_del_padron

La planilla actual guarda en la columna M el renglon crudo del padron, lo que
permite validar la alicuota usada contra su propia fuente.
"""
import json
from collections import Counter
from pathlib import Path

DIR = Path(__file__).resolve().parent
HIST = json.loads((DIR / "historico.json").read_text(encoding="utf-8"))

# posiciones dentro del renglon del padron, separado por ";"
# Diseno oficial "Padron de Regimenes Generales - por sujeto" (AGIP, 2016):
# fecha_publicacion;vigencia_desde;vigencia_hasta;cuit;tipo_contr;marca_alta;
# marca_alicuota;alic_percepcion;alic_retencion;grupo_perc;grupo_ret;razon_social
PUBLICACION, DESDE, HASTA, CUIT, TIPO_CONTR = 0, 1, 2, 3, 4
MARCA_ALTA, MARCA_ALICUOTA = 5, 6
ALIC_PERCEPCION, ALIC_RETENCION = 7, 8

# Tipo-Contr_Insc del padron -> "Situacion IB del Retenido" que pide e-ARCIBA
SITUACION_IB = {"D": "1",    # Directo C.A.B.A. (local)
                "C": "2"}    # Convenio Multilateral


def parse_padron(linea):
    if not linea:
        return None
    p = linea.split(";")
    if len(p) < 9:
        return None
    try:
        return {
            "publicacion": p[PUBLICACION], "desde": p[DESDE], "hasta": p[HASTA],
            "cuit": p[CUIT], "tipo_contr": p[TIPO_CONTR],
            "situacion_ib": SITUACION_IB.get(p[TIPO_CONTR]),
            "alic_percepcion": float(p[ALIC_PERCEPCION].replace(",", ".")) / 100,
            "alic_retencion": float(p[ALIC_RETENCION].replace(",", ".")) / 100,
            "razon_social": p[11] if len(p) > 11 else None,
        }
    except ValueError:
        return None


def main():
    tot = Counter()
    fallos_alicuota, fallos_importe, sin_padron, cuit_distinto = [], [], [], []

    for f in HIST["caba"]:
        if f["anulada"]:
            tot["anuladas"] += 1
            continue
        tot["evaluadas"] += 1

        # 1) el importe debe ser neto * alicuota
        esperado = f["monto_neto"] * (f["alicuota"] or 0)
        real = f["retencion"] or 0
        if abs(esperado - real) > 0.01:
            fallos_importe.append((f, esperado, real))

        # 2) la alicuota debe coincidir con el padron pegado en la misma fila
        pad = parse_padron(f["padron_crudo"])
        if pad is None:
            sin_padron.append(f)
            continue
        if pad["cuit"] != f["cuit"]:
            cuit_distinto.append((f, pad))
            continue
        tot["con_padron"] += 1
        if abs(pad["alic_retencion"] - (f["alicuota"] or 0)) > 1e-9:
            fallos_alicuota.append((f, pad))

    print(f"filas CABA          : {len(HIST['caba'])}")
    print(f"  anuladas (rojo)   : {tot['anuladas']}")
    print(f"  evaluadas         : {tot['evaluadas']}")
    print(f"  con padron propio : {tot['con_padron']}")
    print()
    print(f"importe = neto x alicuota  -> difieren {len(fallos_importe)}")
    for f, esp, real in fallos_importe:
        print(f"   fila {f['fila']:>4} {f['periodo']} {(f['razon_social'] or '')[:30]:30} "
              f"neto {f['monto_neto']:>14,.2f} alic {f['alicuota']} "
              f"esperado {esp:>12,.2f}  planilla {real:>12,.2f}")
    print()
    print(f"alicuota vs padron AGIP    -> difieren {len(fallos_alicuota)}")
    for f, pad in fallos_alicuota:
        print(f"   fila {f['fila']:>4} {f['periodo']} {(f['razon_social'] or '')[:30]:30} "
              f"usada {f['alicuota']!s:>7}  padron {pad['alic_retencion']!s:>7}  "
              f"vig {pad['desde']}-{pad['hasta']}")
    print()
    print(f"sin renglon de padron en la fila: {len(sin_padron)}"
          f"   (el padron pegado es de otro CUIT: {len(cuit_distinto)})")
    if cuit_distinto:
        for f, pad in cuit_distinto[:8]:
            print(f"   fila {f['fila']:>4} cuit fila {f['cuit']}  cuit padron {pad['cuit']}")


if __name__ == "__main__":
    main()
