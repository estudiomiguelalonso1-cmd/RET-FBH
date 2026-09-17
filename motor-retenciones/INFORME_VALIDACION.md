# Motor de retenciones — validación contra el histórico 2026

Fecha: 2026-09-17 · Fiberhome Argentina S.A.

## Qué se hizo

Se escribieron como código las reglas de cálculo de los cuatro impuestos que Fiberhome
retiene hoy, y se corrieron sobre las retenciones ya practicadas entre enero y agosto
de 2026 para ver si reproducen exactamente lo que se hizo a mano.

```
python extraer_historico.py    # lee los dos Excel -> historico.json
python validar.py              # corre el motor y compara contra lo practicado
```

## Resultado

| Impuesto | Filas | Anuladas | Sin practicar | Exactas | Difieren |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ganancias (RG 830) | 150 | 27 | 1 | **121** | 1 |
| IIBB CABA (AGIP) | 131 | 16 | 1 | **114** | 0 |
| IVA (Reg. 966) | 7 | — | — | **7** | 0 |
| SUSS (RES 1556) | 7 | — | — | **5** | 2 |

En IIBB, además, la alícuota usada coincide con el padrón AGIP pegado en la propia
fila en **114 de 114** casos.

## Reglas confirmadas

**Ganancias.** Base imponible = neto − saldo no consumido del mínimo no imponible del
régimen. El saldo se reinicia cada mes por proveedor y régimen y se consume pago a
pago (de ahí las bases imponibles negativas). Si la retención calculada queda por
debajo del mínimo de retención del régimen ($240 en casi todos), no se retiene.

**Las filas anuladas no consumen el mínimo.** Fue el hallazgo que más diferencias
explicó: 13 de las 21 iniciales desaparecieron al dejar de computarlas.

**IIBB CABA.** Sin mínimo ni acumulado: retención = neto × alícuota del padrón AGIP
vigente para ese CUIT ese mes.

**IVA — RG 3164.** 10,5 % del neto. Exacto en 7 de 7.

**SUSS — RG 1556.** 6 % del neto. Exacto en 5 de 7. La base correcta es el neto: el
art. 8 de la RG 1556 dice que es el importe de cada pago "excepto el monto
correspondiente al débito fiscal del impuesto al valor agregado" cuando el
beneficiario es responsable inscripto. Las filas 36 y 86 usaron la base imponible de
Ganancias: **están mal por $4.030,20 cada una**.

### Los dos regímenes, identificados

El proveedor alcanzado (MALDONADO CARLOS HUMBERTO) no es de la construcción sino de
**limpieza de inmuebles**. La combinación 10,5 % de IVA + 6 % de SUSS es la firma de
ese rubro:

| Tributo | Norma | Alícuota | Dónde se declara | Impuesto / régimen |
| --- | --- | ---: | --- | --- |
| IVA | RG 3164 | 10,5 % | SICORE | **767 / 831** |
| SUSS | RG 1556 | 6 % | **SIRE, F. 2004** | 353 / **748** |

El régimen 831 es "locación de obras y prestaciones de servicios realizados por
empresas de limpieza de edificios, investigación y/o seguridad y recolección de
residuos domiciliarios". **Es exactamente el código que usa el ERP en su archivo**
(`767` + `831`, 10,5 %), lo que además confirma que el corte de campos es el correcto.
El encabezado del Excel dice "Reg. 966", que no se corresponde con este régimen.

**El SUSS no va por SICORE.** Desde el 01/03/2015, la RG 3726/15 sacó las retenciones
de seguridad social del SICORE y las pasó al SIRE (Sistema Integral de Retenciones
Electrónicas), formulario F. 2004. Por eso el archivo del ERP no trae ninguna línea de
SUSS. Hace falta un segundo exportador, con otro formato.

**Régimen 119 (profesiones liberales): escala progresiva.** La tabla de AFIP devuelve
alícuota 0 porque este régimen no tiene alícuota fija — el simulador lo muestra como
"s/escala". La escala del Anexo VIII de la RG 830 se reconstruyó a partir de las
retenciones reales y se contrastó contra el simulador oficial:

| Base imponible desde | Retiene | Más el % sobre el excedente |
| ---: | ---: | ---: |
| 0 | 0,00 | 5 % |
| 71.000 | 3.550,00 | 9 % |
| 142.000 | 9.940,00 | 12 % |
| 213.000 | 18.460,00 | 15 % |
| 284.000 | 29.110,00 | 19 % |
| 426.000 | 56.090,00 | 23 % |
| 568.000 | 88.750,00 | 27 % |
| 852.000 | 165.430,00 | 31 % |

Los topes son múltiplos 1, 2, 3, 4, 6, 8 y 12 de una unidad que para 2026 es
**$71.000**. A diferencia de la escala del art. 94 de la ley, **no tiene tramo del
35 %**: el último arranca en $852.000 y va al 31 % sin tope. Verificado con el
simulador de AFIP en $28.339.928,75 → $8.686.688 y en $2.000.000 → $521.310.

## Las 3 diferencias

| Impuesto | Fila | Proveedor | Planilla | Motor | Diferencia |
| --- | ---: | --- | ---: | ---: | --- |
| Ganancias | 195 | BAENA GABRIEL | 13.865,80 | 12.656,60 | **1.209,20 de más** |
| SUSS | 36 | MALDONADO CARLOS H. | 30.622,92 | 34.653,12 | 4.030,20 de menos |
| SUSS | 86 | MALDONADO CARLOS H. | 30.622,92 | 34.653,12 | 4.030,20 de menos |

**Fila 195 — BAENA GABRIEL.** Se restó $6.710 de mínimo no imponible en lugar de
$67.170. Parece un error de tipeo; se retuvo $1.209,20 de más.

**Filas 36 y 86 — SUSS.** Calcularon el 6 % sobre la base imponible de Ganancias
($510.382,07) en lugar del neto ($577.552,07). Desde la fila 89 en adelante el
criterio es uniformemente el neto. **A confirmar cuál es el correcto.**

**Sin practicar (no se cuentan como diferencia).** La factura de Cabify
00002-00003649, neto $47.885,56, quedó en cero en Ganancias y en IIBB a la vez.
Habrían correspondido $957,71 y $95,77.

## Hallazgos sobre los archivos actuales

**El color del Excel codifica reglas de negocio.** La leyenda está en las filas
213-215 de la hoja de Ganancias:

- **Rojo (letra)** — "No corresponde / Anulada". 27 filas en Ganancias, 16 en CABA.
  Es información que no existe en ninguna columna: si se migra el Excel sin leer los
  colores, se pierde.
- **Verde (columna Monto Neto)** — "FC USD - TC del día". 11 filas. No queda
  registrado el importe original en dólares ni el tipo de cambio aplicado.
- **Azul (fila completa)** — "Retención IVA y SUSS *RES 1556". 7 filas.
- **Amarillo (columna Razón Social)** — 10 filas en Ganancias, 4 en CABA, sin leyenda
  y sin significado definido. Se conserva como marca, no se usa para calcular.

**`SICORE (3).txt` es de otro cliente del ERP, no de Fiberhome.** Sus 18 líneas no
cruzan con ninguna fila del Excel (9 de los 11 CUIT retenidos no existen en la
planilla). Fiberhome no tiene contratado ese módulo y hoy carga las retenciones a
mano en el aplicativo. Como el archivo fue generado por el ERP y aceptado por AFIP,
**sirve como especificación del formato**: basta replicarlo carácter por carácter.

**El archivo SICORE usa el neto como base de cálculo.** En el campo de base de
cálculo va el neto de la factura, y la retención ya viene con el mínimo no imponible
descontado.

**Faltan 5 certificados en la serie de Ganancias.** Los números 2026-146 a 2026-150
no aparecen en ninguna fila de la hoja. La serie es continua en todo el resto del año.

**El endpoint de AFIP no se puede llamar desde el servidor.** `conceptosalcliente`
(la tabla de regímenes) responde bien sin navegador, pero `CalcularRetencion` está
detrás de un WAF que bloquea todo POST con cuerpo JSON que no venga de una sesión de
navegador real. Delegar el cálculo en AFIP no es viable; la tabla cacheada sí, y el
motor propio ya reproduce el 99 % de los casos.

## Exportador SICORE

`exportar_sicore.py` genera el archivo de importación (diseño de registro F.744,
198 caracteres por línea) a partir del motor:

```
python exportar_sicore.py 2026-07
```

El layout se verificó carácter por carácter contra el archivo del ERP:

| Pos. | Campo | Valor |
| ---: | --- | --- |
| 1-2 | Código de comprobante | `01` — Factura / Tique, como dice el certificado de Fiberhome (el ERP usa `06`) |
| 3-12 | Fecha del comprobante | dd/mm/aaaa |
| 13-28 | Número de comprobante | punto de venta (5) + número (8) |
| 29-44 | Importe del comprobante | ver nota abajo |
| 45 | Relleno | `0` |
| 46-48 | Código de impuesto | `217` Ganancias · `767` IVA |
| 49-51 | Código de régimen | alineado a la izquierda: `94 `, `31 `, `119` |
| 52 | Código de operación | `1` |
| 53-66 | Base de cálculo | neto de la factura |
| 67-76 | Fecha de retención | dd/mm/aaaa |
| 77-78 / 79 | Condición / suspendido | `01` / `0` |
| 80-93 | Importe de la retención | |
| 110-111 / 112-131 | Tipo de documento / CUIT | `80` / CUIT del retenido |

Que los códigos de impuesto caigan en `217` (Ganancias) y `767` (IVA) —los códigos
reales de AFIP— es lo que confirma dónde cortan los campos, porque la otra lectura
posible daba códigos de régimen inexistentes.

**Qué va en "Importe del comprobante".** El registro tiene dos campos de importe
distintos: "importe del comprobante" (pos 29-44) y "base de cálculo" (pos 53-66). La
base de cálculo es el neto en los dos casos y no está en discusión — de ahí sale el
importe retenido.

El otro campo difiere. El certificado 0000-2026-000174 de MALDONADO imprime
$1.150.435,54, que es el **neto** (la factura 00004-00000500 con IVA da $1.392.027,00).
El ERP, en cambio, declara ahí el total: en su archivo, importe ÷ base = 1,21 exacto.

**Decidido: se mantiene el neto**, que es el criterio con el que Fiberhome viene
cargando por SIAP. No cambia ningún importe retenido, sólo qué monto de factura se
declara y qué se imprime en el certificado. Se cambia con `--importe total` si el
contador define lo contrario.

**Requisito derivado para el sistema nuevo:** hay que guardar la factura completa
—neto, IVA discriminado, percepciones y total— y no sólo el neto, que es lo único que
guarda el Excel de hoy. Sin el total no se puede declarar el total aunque se quiera, y
calcularlo como neto × 1,21 sólo funciona si la factura no tiene percepciones.

### Filas que agrupan varias facturas

12 filas del Excel juntan varias facturas bajo un solo neto, y 6 tienen en la columna
de fecha un rango en vez de una fecha, por el mismo motivo. El caso extremo es la
fila 106 (CABANELLAS, $28.499.928,75) con **diez** facturas:
`00001-5888/5893/5894/5895/5896/5897/5898/5899/5720/5889`.

Así se emiten hoy desde el SIAP: **un solo certificado por grupo**, siempre que las
facturas sean del mismo código de régimen, declarando la **factura más reciente**. El
exportador reproduce ese criterio: expande las abreviaturas (`00005799/5800` son las
facturas 5799 y 5800, no 5799 y 5800 sueltas), toma el número mayor y, cuando la fecha
es un rango, la fecha más reciente.

Cada vez que tiene que elegir lo deja asentado, para que alguien lo pueda revisar
antes de presentar:

```
fila 106: la fila agrupa 10 facturas ('00001-5888/...5889'); se declara la mas reciente: 00001-00005899
fila  93: la fila cubre 2 fechas ('12/3 - 26/3/2026'); se toma la mas reciente: 26/03/2026
```

### Resultado sobre el histórico

| Período | Líneas | Agrupadas | A SIRE (SUSS) | Omitidas |
| --- | ---: | ---: | ---: | ---: |
| 2026-01 | 12 | 0 | 0 | 1 |
| 2026-02 | 16 | 2 | 1 | 0 |
| 2026-03 | 16 | 5 | 0 | 0 |
| 2026-04 | 26 | 6 | 2 | 0 |
| 2026-05 | 15 | 3 | 1 | 0 |
| 2026-06 | 15 | 6 | 1 | 0 |
| 2026-07 | 14 | 1 | 1 | 0 |
| 2026-08 | 13 | 1 | 1 | 0 |
| **Total** | **127** | **24** | **7** | **1** |

**Conciliación contra la planilla.** La suma de las retenciones de los ocho archivos
da $12.616.351,97 de Ganancias y $586.056,73 de IVA, contra $12.651.271,99 y
$586.056,72 del Excel. Toda la diferencia es la fila 20; el resto son centavos de
redondeo a dos decimales (máximo 2 centavos por período), porque el Excel arrastra
fracciones de centavo y el archivo declara dos decimales.

**La única fila que no se puede exportar** es la 20: ALONSO MIGUEL ANGEL, enero 2026,
neto $1.746.000, retención $34.920, certificado 2026-09 — **la celda del número de
factura está vacía**. El certificado se emitió, pero el comprobante que lo originó no
quedó registrado.

## Exportador AGIP (e-ARCIBA)

`exportar_agip.py` genera el archivo de importación de retenciones de IIBB CABA:

```
python exportar_agip.py --plantilla     # datos maestros de proveedores, una sola vez
python exportar_agip.py 2026-07
```

Layout tomado del **Documento Técnico de Importación de Operaciones e-ARCIBA**
(diseño vigente desde el período 01-2022): 23 campos, **226 caracteres** por línea,
UTF-8, ordenado por fecha de retención. Números a la derecha rellenados con ceros,
textos a la izquierda con blancos, y **separador decimal coma incluido dentro del
largo del campo** — el máximo `9999999999999,99` ocupa exactamente los 16 caracteres,
y `99,99` los 5 de la alícuota.

**Código de Norma 29 — "Padrón de Regímenes Generales".** Es el régimen por el que
retiene Fiberhome: la alícuota sale del padrón por sujeto, que es justamente lo que
está pegado en la columna M del Excel. Confirma además el encaje que el 29 sea uno de
los dos códigos (28 y 29) para los que el diseño admite alícuota cero — que es lo que
pasa con los proveedores exentos, como ALONSO MIGUEL ANGEL.

**Situación IB del retenido, deducida del padrón.** El campo `Tipo-Contr_Insc` del
padrón AGIP vale `D` o `C`, y según el diseño oficial del padrón por sujeto `D` es
"Directo C.A.B.A.". Mapea a la situación que pide e-ARCIBA: `D` → 1 (Local),
`C` → 2 (Convenio Multilateral).

**El exportador revalida su propia salida.** Sobre el archivo ya armado vuelve a
comprobar las dos fórmulas que controla AGIP:

- Monto Sujeto a Retención = Monto del comprobante − Importe IVA − Importe otros
- Retención Practicada = Monto Sujeto × Alícuota ÷ 100

Esto obligó a usar **redondeo financiero** (medio hacia arriba) en los dos
exportadores: el redondeo por defecto de Python convierte $1.016,535 en $1.016,53 y
AGIP espera $1.016,54, con lo que la línea sería rechazada.

### El dato que falta: el N° de inscripción en IIBB

Es el único campo obligatorio que no está en ninguna parte del Excel. Para los
contribuyentes de Convenio Multilateral es el propio CUIT y el exportador lo completa
solo, pero para los locales es un número de 8 dígitos más 2 verificadores que hay que
cargar una vez por proveedor.

`python exportar_agip.py --plantilla` escribe `proveedores_agip.csv` con los 38
proveedores ordenados por cantidad de retenciones, con la situación IB y la situación
frente al IVA ya completadas, 4 números de inscripción resueltos (los de Convenio
Multilateral) y **34 por cargar a mano**. Es un trabajo de una sola vez: son datos
estables del proveedor.

Con ese CSV completo, el exportador produce **las 113 retenciones practicadas de 2026,
sin una sola fila bloqueada**, y las validaciones de AGIP pasan en todas:

| Período | 01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Líneas | 15 | 12 | 18 | 23 | 11 | 14 | 13 | 7 | **113** |

## Pendiente

- Completar los 34 N° de inscripción en IIBB de `proveedores_agip.csv`.
- ~~Exportador de SIRE (F. 2004) para las retenciones de SUSS~~ — **en pausa hasta
  nuevo aviso** (decisión del 17/09/2026). El motor ya calcula el SUSS correctamente;
  lo único que falta es la presentación, que se sigue haciendo a mano. Son 7
  retenciones al año, de un solo proveedor.
- Corregir la fila 195 (BAENA) y definir qué pasó con los certificados 146-150.
