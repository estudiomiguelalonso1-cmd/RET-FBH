# -*- coding: utf-8 -*-
"""Lee una factura electronica de AFIP en PDF y devuelve sus datos.

Las facturas electronicas traen el texto embebido, con un layout estandar de
etiqueta y valor. No hace falta OCR ni un modelo con vision: alcanza con ubicar
cada etiqueta y tomar lo que tiene a la derecha, que es deterministico y gratis.

    python leer_factura.py factura.pdf
    python leer_factura.py factura.pdf --json

Devuelve, entre otras cosas, el domicilio del proveedor y su numero de Ingresos
Brutos, que son los dos datos maestros que el Excel no guarda.
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

import pymupdf

# Etiquetas que aparecen dos veces: una en el bloque del emisor y otra en el del
# receptor. El bloque del receptor arranca en esta etiqueta.
MARCA_RECEPTOR = "apellido y nombre / razon social:"


def normalizar(t):
    """Minusculas y sin acentos, para comparar etiquetas sin sorpresas."""
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def numero(t):
    """'1.045.850,41' -> 1045850.41"""
    if t is None:
        return None
    t = re.sub(r"[^\d,.\-]", "", str(t))
    if not t:
        return None
    return float(t.replace(".", "").replace(",", "."))


def fecha_iso(t):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(t or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def lineas_de(pdf, pagina=0):
    """[(y, x0, x1, texto)] de una pagina, ordenado como se lee."""
    doc = pymupdf.open(pdf)
    out = []
    for bloque in doc[pagina].get_text("dict")["blocks"]:
        for linea in bloque.get("lines", []):
            txt = "".join(s["text"] for s in linea["spans"]).strip()
            if txt:
                x0, y0, x1, y1 = linea["bbox"]
                out.append((round(y0, 1), round(x0, 1), round(x1, 1), txt))
    doc.close()
    return sorted(out)


def y_receptor(lineas):
    for y, x0, x1, t in lineas:
        if normalizar(t) == MARCA_RECEPTOR:
            return y
    return float("inf")


def valor(lineas, etiqueta, *, desde_y=None, hasta_y=None, tolerancia=6):
    """Texto a la derecha de una etiqueta, en la misma banda horizontal."""
    objetivo = normalizar(etiqueta)
    for y, x0, x1, t in lineas:
        if normalizar(t) != objetivo:
            continue
        if desde_y is not None and y < desde_y:
            continue
        if hasta_y is not None and y >= hasta_y:
            continue
        candidatos = [(xx0, tt) for yy, xx0, xx1, tt in lineas
                      if abs(yy - y) <= tolerancia and xx0 > x1 - 1]
        if candidatos:
            return min(candidatos)[1]
    return None


def valor_multilinea(lineas, etiqueta, *, desde_y=None, hasta_y=None, maximo=3):
    """Como valor(), pero engancha las lineas de continuacion de abajo.

    El domicilio suele venir partido en dos renglones alineados a la izquierda
    con el primero.
    """
    objetivo = normalizar(etiqueta)
    for y, x0, x1, t in lineas:
        if normalizar(t) != objetivo:
            continue
        if desde_y is not None and y < desde_y:
            continue
        if hasta_y is not None and y >= hasta_y:
            continue
        candidatos = [(xx0, yy, tt) for yy, xx0, xx1, tt in lineas
                      if abs(yy - y) <= 6 and xx0 > x1 - 1]
        if not candidatos:
            return None
        cx, cy, texto = min(candidatos)
        partes = [texto]
        for yy, xx0, xx1, tt in lineas:
            if cy < yy <= cy + 14 * maximo and abs(xx0 - cx) < 3 and normalizar(tt) != objetivo:
                if tt.endswith(":") or len(partes) > maximo:
                    break
                partes.append(tt)
                cy = yy
        return " ".join(partes)
    return None


# Columnas de la tabla de items, por el rango de x de cada encabezado. Son fijas
# en la factura electronica de AFIP.
COLUMNAS = [
    ("codigo", 10, 52), ("descripcion", 53, 235), ("cantidad", 236, 285),
    ("unidad", 286, 330), ("precio_unitario", 331, 386), ("bonificacion", 387, 420),
    ("subtotal", 430, 481), ("alicuota_iva", 482, 515), ("subtotal_con_iva", 520, 585),
]
FIN_DE_ITEMS = ("importe neto gravado", "importe otros tributos", "cae n", "importe total")


def columna_de(x0, x1):
    centro = (x0 + x1) / 2
    for nombre, a, b in COLUMNAS:
        if a <= centro <= b:
            return nombre
    return None


def items(lineas):
    """Devuelve los renglones de la factura, uno por producto o servicio.

    Una factura puede traer items de distinto regimen de retencion, asi que hay
    que leerlos por separado y no quedarse solo con el total. La descripcion
    puede venir partida en varios renglones debajo del primero.
    """
    cabecera = next((y for y, x0, x1, t in lineas
                     if normalizar(t).startswith("producto / servicio")), None)
    if cabecera is None:
        return []
    fin = next((y for y, x0, x1, t in lineas
                if y > cabecera and normalizar(t).startswith(FIN_DE_ITEMS)), float("inf"))

    # agrupo por renglon: un item nuevo empieza donde hay un valor en "subtotal"
    filas = {}
    for y, x0, x1, t in lineas:
        if not (cabecera + 6 < y < fin):
            continue
        col = columna_de(x0, x1)
        if col is None:
            continue
        filas.setdefault(round(y / 4), {}).setdefault(col, []).append((y, t))

    out, actual = [], None
    for _, celdas in sorted(filas.items()):
        if "subtotal" in celdas:
            actual = {nombre: None for nombre, _, _ in COLUMNAS}
            for col, vals in celdas.items():
                actual[col] = " ".join(t for _, t in sorted(vals))
            for campo in ("cantidad", "precio_unitario", "bonificacion",
                          "subtotal", "subtotal_con_iva"):
                actual[campo] = numero(actual[campo])
            out.append(actual)
        elif actual is not None and "descripcion" in celdas:
            extra = " ".join(t for _, t in sorted(celdas["descripcion"]))
            actual["descripcion"] = f"{actual['descripcion'] or ''} {extra}".strip()
    return out


def leer(pdf):
    lineas = lineas_de(pdf)
    yr = y_receptor(lineas)
    texto = "\n".join(t for _, _, _, t in lineas)

    tipo = next((t for _, _, _, t in lineas
                 if normalizar(t) in ("factura", "nota de credito", "nota de debito")), None)
    letra = next((t for y, x0, x1, t in lineas if len(t) == 1 and t.isalpha() and y < 60), None)
    cod = re.search(r"COD\.\s*(\d+)", texto)

    emisor = {
        "razon_social": valor(lineas, "Razón Social:", hasta_y=yr),
        "nombre_fantasia": next((t for y, x0, x1, t in lineas if y < 80 and x0 < 120), None),
        "cuit": valor(lineas, "CUIT:", hasta_y=yr),
        "domicilio": valor_multilinea(lineas, "Domicilio Comercial:", hasta_y=yr),
        "ingresos_brutos": valor(lineas, "Ingresos Brutos:"),
        "condicion_iva": valor(lineas, "Condición frente al IVA:", hasta_y=yr),
        "inicio_actividades": fecha_iso(valor(lineas, "Fecha de Inicio de Actividades:")),
    }
    receptor = {
        "cuit": valor(lineas, "CUIT:", desde_y=yr - 6),
        "razon_social": valor(lineas, "Apellido y Nombre / Razón Social:"),
        "domicilio": valor_multilinea(lineas, "Domicilio Comercial:", desde_y=yr),
        "condicion_iva": valor(lineas, "Condición frente al IVA:", desde_y=yr),
    }

    def total(etiqueta):
        return numero(valor(lineas, etiqueta))

    importes = {
        "neto_gravado": total("Importe Neto Gravado: $"),
        "neto_no_gravado": total("Importe Neto No Gravado: $"),
        "exento": total("Importe Op. Exentas: $") or total("Importe Exento: $"),
        "iva_27": total("IVA 27%: $"),
        "iva_21": total("IVA 21%: $"),
        "iva_105": total("IVA 10.5%: $"),
        "iva_5": total("IVA 5%: $"),
        "iva_25": total("IVA 2.5%: $"),
        "iva_0": total("IVA 0%: $"),
        "otros_tributos": total("Importe Otros Tributos: $"),
        "total": total("Importe Total: $"),
    }
    importes["iva"] = sum(v or 0 for k, v in importes.items() if k.startswith("iva_"))
    # Base de Ganancias y de SUSS: el pago sin IVA, e incluye lo no gravado y lo
    # exento. La de IVA (RG 3164), en cambio, es solo el precio neto gravado.
    importes["base_ganancias"] = ((importes["neto_gravado"] or 0)
                                  + (importes["neto_no_gravado"] or 0)
                                  + (importes["exento"] or 0))

    cae = re.search(r"\b(\d{14})\b", texto)
    return {
        "archivo": Path(pdf).name,
        "tipo": tipo, "letra": letra, "codigo": cod.group(1) if cod else None,
        "punto_venta": valor(lineas, "Punto de Venta:"),
        "numero": valor(lineas, "Comp. Nro:"),
        "fecha": fecha_iso(valor(lineas, "Fecha de Emisión:")),
        "periodo_desde": fecha_iso(valor(lineas, "Período Facturado Desde:")),
        "periodo_hasta": fecha_iso(valor(lineas, "Hasta:")),
        "emisor": emisor, "receptor": receptor, "importes": importes,
        "items": items(lineas),
        "cae": cae.group(1) if cae else None,
    }


def controles(d):
    """Chequeos que tienen que dar bien si la lectura fue correcta."""
    avisos = []
    i = d["importes"]
    if i["neto_gravado"] is not None and i["total"] is not None:
        esperado = (i["base_ganancias"] or 0) + i["iva"] + (i["otros_tributos"] or 0)
        if abs(esperado - i["total"]) > 0.01:
            avisos.append(f"neto + IVA + otros = {esperado:,.2f} y el total dice "
                          f"{i['total']:,.2f}")
    for campo, ruta in [("CUIT del emisor", ("emisor", "cuit")),
                        ("CUIT del receptor", ("receptor", "cuit"))]:
        v = d[ruta[0]][ruta[1]]
        if not v or not re.fullmatch(r"\d{11}", str(v)):
            avisos.append(f"{campo} ilegible: {v!r}")
    if not d["punto_venta"] or not d["numero"]:
        avisos.append("no se pudo leer el punto de venta o el numero")
    if not d["items"]:
        avisos.append("no se pudieron leer los items de la factura")
    else:
        suma = sum(x["subtotal"] or 0 for x in d["items"])
        if i["neto_gravado"] and abs(suma - i["base_ganancias"]) > 0.01:
            avisos.append(f"los items suman {suma:,.2f} y el neto declarado es "
                          f"{i['base_ganancias']:,.2f}")
    return avisos


def imprimir(d):
    e, r, i = d["emisor"], d["receptor"], d["importes"]
    print(f"{d['tipo']} {d['letra']} (cod. {d['codigo']})  "
          f"{d['punto_venta']}-{d['numero']}   {d['fecha']}")
    print()
    print("EMISOR")
    print(f"   razon social      {e['razon_social']}")
    print(f"   nombre fantasia   {e['nombre_fantasia']}")
    print(f"   CUIT              {e['cuit']}")
    print(f"   domicilio         {e['domicilio']}")
    print(f"   ingresos brutos   {e['ingresos_brutos']}")
    print(f"   condicion IVA     {e['condicion_iva']}")
    print("RECEPTOR")
    print(f"   {r['razon_social']}   CUIT {r['cuit']}")
    print("IMPORTES")
    print(f"   neto gravado      {i['neto_gravado']:>14,.2f}" if i["neto_gravado"] else "")
    print(f"   IVA               {i['iva']:>14,.2f}")
    print(f"   otros tributos    {(i['otros_tributos'] or 0):>14,.2f}")
    print(f"   TOTAL             {i['total']:>14,.2f}" if i["total"] else "")
    if d["items"]:
        print("ITEMS")
        for x in d["items"]:
            print(f"   {(x['codigo'] or ''):>8}  {(x['descripcion'] or '')[:52]:52} "
                  f"{(x['subtotal'] or 0):>14,.2f}  IVA {x['alicuota_iva'] or '-'}")
    av = controles(d)
    print()
    print("  controles: ok" if not av else "  AVISOS:")
    for a in av:
        print(f"    {a}")


def main():
    ap = argparse.ArgumentParser(description="Lee una factura electronica de AFIP")
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for pdf in args.pdf:
        d = leer(pdf)
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        else:
            print("=" * 74)
            imprimir(d)


if __name__ == "__main__":
    main()
