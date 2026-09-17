# -*- coding: utf-8 -*-
"""Corre el motor completo (4 impuestos) contra el historico 2026 y resume.

    python validar.py
"""
import csv, json
from pathlib import Path

import motor_caba, motor_ganancias, motor_iva_suss

DIR = Path(__file__).resolve().parent


def caba(H):
    """-> (exactas, diferencias, no_practicadas, anuladas)"""
    exactas, dif, no_prac = 0, [], []
    anuladas = sum(1 for f in H["caba"] if f["anulada"])
    for f in H["caba"]:
        if f["anulada"]:
            continue
        esp = f["monto_neto"] * (f["alicuota"] or 0)
        real = f["retencion"] or 0
        pad = motor_caba.parse_padron(f["padron_crudo"])
        alic_ok = pad is None or abs(pad["alic_retencion"] - (f["alicuota"] or 0)) <= 1e-9
        if abs(esp - real) <= 0.01 and alic_ok:
            exactas += 1
        elif not real and esp > 0:
            no_prac.append((f, esp))
        else:
            dif.append((f, esp))
    return exactas, dif, no_prac, anuladas


def iva_suss(H):
    exactas_iva = exactas_suss = 0
    dif = []
    casos = [f for f in H["ganancias"] if f["res_1556"]]
    for f in casos:
        n = f["monto_neto"]
        if abs(motor_iva_suss.retencion_iva(n) - (f["ret_iva_966"] or 0)) <= 0.01:
            exactas_iva += 1
        else:
            dif.append(("IVA RG 3164", f, motor_iva_suss.retencion_iva(n), f["ret_iva_966"] or 0))
        if abs(motor_iva_suss.retencion_suss(n) - (f["ret_suss"] or 0)) <= 0.01:
            exactas_suss += 1
        else:
            dif.append(("SUSS RG 1556", f, motor_iva_suss.retencion_suss(n), f["ret_suss"] or 0))
    return len(casos), exactas_iva, exactas_suss, dif


def main():
    H = json.loads((DIR / "historico.json").read_text(encoding="utf-8"))
    ok, ko, na, fallos, no_prac_g = motor_ganancias.comparar(motor_ganancias.correr("A"))
    ok_c, dif_c, no_prac_c, anul_c = caba(H)
    n_rs, ok_iva, ok_suss, dif_rs = iva_suss(H)

    print("=" * 80)
    print("MOTOR DE RETENCIONES - VALIDACION CONTRA EL HISTORICO 2026")
    print("=" * 80)
    print()
    print(f"{'impuesto':24} {'filas':>7} {'anuladas':>9} {'sin practicar':>14} {'exactas':>9} {'difieren':>9}")
    print("-" * 80)
    print(f"{'Ganancias (RG 830)':24} {len(H['ganancias']):>7} {na:>9} {len(no_prac_g):>14} {ok:>9} {ko:>9}")
    print(f"{'IIBB CABA (AGIP)':24} {len(H['caba']):>7} {anul_c:>9} {len(no_prac_c):>14} {ok_c:>9} {len(dif_c):>9}")
    print(f"{'IVA (RG 3164)':24} {n_rs:>7} {'-':>9} {'-':>14} {ok_iva:>9} {n_rs - ok_iva:>9}")
    print(f"{'SUSS (RG 1556)':24} {n_rs:>7} {'-':>9} {'-':>14} {ok_suss:>9} {n_rs - ok_suss:>9}")
    print()

    print("DIFERENCIAS")
    filas_csv = []
    for f, base, ret, _b, _r in fallos:
        d = (f["retencion"] or 0) - ret
        print(f"  Ganancias    fila {f['fila']:>4}  {(f['razon_social'] or '')[:26]:26} "
              f"planilla {(f['retencion'] or 0):>12,.2f}  motor {ret:>12,.2f}  "
              f"{'de mas' if d > 0 else 'de menos'} {abs(d):,.2f}")
        filas_csv.append(["Ganancias", f["fila"], f["periodo"], f["razon_social"], f["cuit"],
                          f["monto_neto"], round(ret, 2), f["retencion"], round(d, 2)])
    for f, esp in dif_c:
        d = (f["retencion"] or 0) - esp
        print(f"  IIBB CABA    fila {f['fila']:>4}  {(f['razon_social'] or '')[:26]:26} "
              f"planilla {(f['retencion'] or 0):>12,.2f}  motor {esp:>12,.2f}  "
              f"{'de mas' if d > 0 else 'de menos'} {abs(d):,.2f}")
        filas_csv.append(["IIBB CABA", f["fila"], f["periodo"], f["razon_social"], f["cuit"],
                          f["monto_neto"], round(esp, 2), f["retencion"], round(d, 2)])
    for nombre, f, calc, real in dif_rs:
        d = real - calc
        print(f"  {nombre:12} fila {f['fila']:>4}  {(f['razon_social'] or '')[:26]:26} "
              f"planilla {real:>12,.2f}  motor {calc:>12,.2f}  "
              f"{'de mas' if d > 0 else 'de menos'} {abs(d):,.2f}")
        filas_csv.append([nombre, f["fila"], f["periodo"], f["razon_social"], f["cuit"],
                          f["monto_neto"], round(calc, 2), round(real, 2), round(d, 2)])

    print()
    print(f"SIN PRACTICAR (planilla en cero, no se cuentan): "
          f"{len(no_prac_g)} en Ganancias, {len(no_prac_c)} en IIBB")
    for f, base, ret in no_prac_g:
        print(f"  Ganancias    fila {f['fila']:>4}  {(f['razon_social'] or '')[:26]:26} "
              f"habria correspondido {ret:>12,.2f}")
    for f, esp in no_prac_c:
        print(f"  IIBB CABA    fila {f['fila']:>4}  {(f['razon_social'] or '')[:26]:26} "
              f"habria correspondido {esp:>12,.2f}")

    salida = DIR / "diferencias.csv"
    with salida.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["impuesto", "fila", "periodo", "proveedor", "cuit", "neto",
                    "retencion_motor", "retencion_planilla", "diferencia"])
        w.writerows(filas_csv)
    print()
    print(f"-> detalle en {salida.name}")


if __name__ == "__main__":
    main()
