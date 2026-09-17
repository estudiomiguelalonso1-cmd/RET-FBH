# -*- coding: utf-8 -*-
"""Escala progresiva del Anexo VIII de la RG 830.

Los regimenes cuya PORCENT_A_RETENER es 0 para sujetos inscriptos (119 -
profesiones liberales y oficios, entre otros) no llevan alicuota fija: el
simulador de AFIP los muestra como "s/escala" y calcula del lado del servidor.

La escala tiene la misma forma que la del art. 94 de la Ley de Ganancias:
ocho tramos con topes que son multiplos 1, 2, 3, 4, 6, 8 y 12 de una unidad
que se actualiza periodicamente. Para 2026 la unidad es $71.000.

A diferencia de la escala del art. 94, esta NO tiene tramo del 35%: el ultimo
tramo arranca en 12 unidades ($852.000) y se aplica al 31% sin tope.

Deducida de las retenciones practicadas por Fiberhome y contrastada contra el
simulador oficial de AFIP (ver validar_escala()).
"""

UNIDAD_2026 = 71_000
MULTIPLOS = (1, 2, 3, 4, 6, 8, 12)
TASAS = (0.05, 0.09, 0.12, 0.15, 0.19, 0.23, 0.27, 0.31)


def tramos(unidad=UNIDAD_2026):
    """[(desde, acumulado_hasta_desde, tasa_marginal), ...]"""
    topes = [m * unidad for m in MULTIPLOS]
    filas, acum, desde = [], 0.0, 0.0
    for tope, tasa in zip(topes, TASAS):
        filas.append((desde, acum, tasa))
        acum += (tope - desde) * tasa
        desde = tope
    filas.append((desde, acum, TASAS[-1]))
    return filas


def retencion_por_escala(base, unidad=UNIDAD_2026):
    if base <= 0:
        return 0.0
    for desde, acum, tasa in reversed(tramos(unidad)):
        if base > desde:
            return acum + (base - desde) * tasa
    return 0.0


def validar_escala():
    casos = [           # base imponible -> retencion practicada por Fiberhome
        (92_000, 5_440.00),
        (139_400, 9_706.00),
        (313_650, 34_743.50),
        (514_000, 76_330.00),
        (28_339_928.75, 8_686_687.91),   # tambien confirmado por el simulador AFIP
        (2_000_000, 521_310.00),         # solo simulador AFIP, no hay caso real
    ]
    print(f"{'base':>16} {'esperado':>14} {'escala':>14}  ok")
    for base, esperado in casos:
        calc = retencion_por_escala(base)
        print(f"{base:>16,.2f} {esperado:>14,.2f} {calc:>14,.2f}  "
              f"{'si' if abs(calc - esperado) < 0.01 else 'NO'}")
    print()
    print(f"tramos con unidad ${UNIDAD_2026:,}:")
    for desde, acum, tasa in tramos():
        print(f"  desde {desde:>12,.0f}   fijo {acum:>12,.2f}   + {tasa:.0%}")


if __name__ == "__main__":
    validar_escala()
