# -*- coding: utf-8 -*-
"""Motor de Retencion de IVA (RG 3164) y de Seguridad Social (RG 1556).

Los dos regimenes alcanzan a las empresas de limpieza de inmuebles, investigacion
y/o seguridad y recoleccion de residuos domiciliarios. En el historico 2026 hay un
solo proveedor alcanzado (MALDONADO CARLOS HUMBERTO), con 7 casos.

    retencion IVA  = neto * 10,5 %      RG 3164, SICORE impuesto 767 regimen 831
    retencion SUSS = neto *  6,0 %      RG 1556, SIRE F. 2004 regimen 748

Las dos, sobre el neto. Para el SUSS lo dice el art. 8 de la RG 1556: la base es
el importe de cada pago "excepto el monto correspondiente al debito fiscal del
impuesto al valor agregado" cuando el beneficiario es responsable inscripto.

Las filas 36 y 86 calcularon el 6 % sobre la base imponible de Ganancias (neto
menos el minimo no imponible) en lugar del neto: estan mal por $4.030,20 cada una.
Desde la fila 89 en adelante el criterio es el correcto.
"""
import json
from pathlib import Path

DIR = Path(__file__).resolve().parent

ALICUOTA_IVA = 0.105     # RG 3164
ALICUOTA_SUSS = 0.06     # RG 1556


def retencion_iva(neto):
    return neto * ALICUOTA_IVA


def retencion_suss(neto):
    return neto * ALICUOTA_SUSS


def main():
    H = json.loads((DIR / "historico.json").read_text(encoding="utf-8"))
    casos = [f for f in H["ganancias"] if f["res_1556"]]

    print(f"filas alcanzadas por RES 1556 (marcadas en azul): {len(casos)}")
    print()
    hdr = (f"{'fila':>5} {'per':8} {'neto':>14} | {'IVA motor':>13} {'IVA plan.':>13} {'':>3} | "
           f"{'SUSS motor':>13} {'SUSS plan.':>13}")
    print(hdr); print("-" * len(hdr))
    ok_iva = ok_suss = 0
    for f in casos:
        n = f["monto_neto"]
        ci, cs = retencion_iva(n), retencion_suss(n)
        ri, rs = f["ret_iva_966"] or 0, f["ret_suss"] or 0
        bi = abs(ci - ri) <= 0.01
        bs = abs(cs - rs) <= 0.01
        ok_iva += bi; ok_suss += bs
        print(f"{f['fila']:>5} {f['periodo']:8} {n:>14,.2f} | {ci:>13,.2f} {ri:>13,.2f} "
              f"{'ok' if bi else 'DIF':>3} | {cs:>13,.2f} {rs:>13,.2f} {'ok' if bs else 'DIF'}")
    print()
    print(f"IVA  (10,5 % del neto): {ok_iva}/{len(casos)} exactas")
    print(f"SUSS ( 6,0 % del neto): {ok_suss}/{len(casos)} exactas")

    dif = [f for f in casos if abs(retencion_suss(f["monto_neto"]) - (f["ret_suss"] or 0)) > 0.01]
    if dif:
        print()
        print("SUSS - filas que usaron otra base (la correcta es el neto, art. 8 RG 1556):")
        for f in dif:
            base_gan = f["base_imponible"]
            print(f"   fila {f['fila']:>4} {f['periodo']}  planilla {f['ret_suss']:,.2f} = 6 % de "
                  f"{(f['ret_suss'] / ALICUOTA_SUSS):,.2f}  (base imponible de Ganancias: {base_gan:,.2f})"
                  f"  -> 6 % del neto seria {retencion_suss(f['monto_neto']):,.2f}")


if __name__ == "__main__":
    main()
