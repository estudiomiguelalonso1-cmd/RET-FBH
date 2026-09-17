# Sistema de retenciones — Fiberhome Argentina S.A.

Fiberhome actúa como agente de retención de cuatro impuestos cada vez que le paga una
factura a un proveedor. Hoy el circuito es manual: alguien decide a mano el régimen y
la alícuota, calcula la base restando lo ya retenido en el mes, carga todo en dos
planillas Excel paralelas y después tipea las retenciones una por una en el SIAP y en
el e-ARCIBA.

Este repositorio automatiza el cálculo y la generación de los archivos de presentación.

| Impuesto | Norma | Se presenta en |
| --- | --- | --- |
| Ganancias | RG 830 | SICORE (SIAP) |
| Ingresos Brutos CABA | Padrón de Regímenes Generales | e-ARCIBA (AGIP) |
| IVA | RG 3164 | SICORE (SIAP) |
| Seguridad Social | RG 1556 | SIRE, F. 2004 — *pendiente* |

## Estado

El **motor de cálculo** está terminado y validado: reproduce al centavo 235 de las 238
retenciones practicadas en 2026. Las tres que no coinciden son errores de la planilla,
detallados en [`INFORME_VALIDACION.md`](motor-retenciones/INFORME_VALIDACION.md).

Los **exportadores** de SICORE y e-ARCIBA generan los archivos de presentación con el
layout oficial y se revalidan contra sus propias reglas.

Todavía **no** hay carga de facturas, interfaz, ni generación de certificados PDF.

## Cómo se usa

Necesita Python 3.12 con `openpyxl`.

```bash
cd motor-retenciones

python extraer_historico.py       # lee los dos Excel -> historico.json
python validar.py                 # corre el motor y compara contra lo practicado
python migrar.py                  # carga el histórico en retenciones.db y concilia

python exportar_sicore.py 2026-08            # archivo de importación a SICORE
python exportar_agip.py --plantilla          # datos maestros de proveedores
python exportar_agip.py 2026-08              # archivo de importación a e-ARCIBA
```

## Qué hay en cada archivo

| Archivo | Para qué |
| --- | --- |
| `extraer_historico.py` | Lee los dos Excel, **incluidos los colores de celda**, que codifican si una fila está anulada, si la factura es en dólares y si lleva IVA y SUSS |
| `motor_ganancias.py` | RG 830: mínimo no imponible que se consume mes a mes por proveedor y régimen |
| `escala_anexo_viii.py` | Escala progresiva del Anexo VIII, para los regímenes sin alícuota fija |
| `motor_caba.py` | IIBB CABA y lectura del padrón de AGIP |
| `motor_iva_suss.py` | IVA (10,5 %) y SUSS (6 %) |
| `validar.py` | Corre los cuatro motores contra el histórico y reporta diferencias |
| `exportar_sicore.py` | Archivo de 198 caracteres para el SIAP |
| `exportar_agip.py` | Archivo de 226 caracteres para el e-ARCIBA |
| `esquema.sql` | Modelo de datos (SQLite, portable a PostgreSQL) |
| `migrar.py` | Carga el histórico 2026 en la base y concilia contra el Excel |

## Datos

**Este repositorio no contiene datos reales.** Las planillas, los certificados, la base
de datos y los archivos de presentación están en `.gitignore` porque llevan CUIT,
razones sociales e importes de Fiberhome y de sus proveedores.

Para correrlo hace falta poner en la raíz los dos Excel:

- `Retenciones_Ganancias_Practicadas_2026.xlsx`
- `Retenciones_CABA_Agente_Recaudacion_2026.xlsx`

## Fuentes normativas

Las tablas de AFIP y AGIP se tratan como fuente externa cacheada, nunca como
dependencia en tiempo real. El detalle de cada una, y los límites que tienen, está en
[`INFORME_VALIDACION.md`](motor-retenciones/INFORME_VALIDACION.md) y en el
[documento de diseño](diseno-sistema-retenciones/DISENO_SISTEMA.md).
