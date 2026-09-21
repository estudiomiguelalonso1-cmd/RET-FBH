# -*- coding: utf-8 -*-
"""Circuito completo: lee la factura, calcula las retenciones y emite los certificados.

    python procesar.py factura.pdf                  ve la propuesta, no toca nada
    python procesar.py factura.pdf --regimen 94     fija el regimen de Ganancias
    python procesar.py factura.pdf --confirmar      guarda y emite los certificados

Sin --confirmar no escribe nada: muestra lo que haria. La idea es que alguien
mire la propuesta antes de que se emita un certificado.
"""
import argparse
import sqlite3
from pathlib import Path

import calcular
import certificados
import clientes
import leer_factura

DIR = Path(__file__).resolve().parent

# El regimen de Ganancias no se puede deducir del texto de la factura con
# confianza, asi que se propone el que mas uso ese proveedor y se puede pisar.
REGIMEN_POR_DEFECTO = None


def normalizar_iibb(valor, cuit):
    """'CM:20252500872' -> '20252500872'. Deja solo digitos."""
    if not valor:
        return None
    solo = "".join(c for c in str(valor) if c.isdigit())
    return solo or None


def regimen_habitual(conn, cuit):
    fila = conn.execute(
        "SELECT r.regimen, COUNT(*) n FROM retenciones r "
        "JOIN comprobantes c ON c.id = r.comprobante_id "
        "WHERE c.cuit = ? AND r.impuesto = 'ganancias' AND r.regimen IS NOT NULL "
        "GROUP BY r.regimen ORDER BY n DESC LIMIT 1", (cuit,)).fetchone()
    return int(fila["regimen"]) if fila else None


def sincronizar_proveedor(conn, f, escribir):
    """Da de alta al proveedor si es nuevo y completa lo que falte con la factura."""
    e = f["emisor"]
    cuit = e["cuit"]
    p = conn.execute("SELECT * FROM proveedores WHERE cuit = ?", (cuit,)).fetchone()
    cambios = []
    iibb = normalizar_iibb(e["ingresos_brutos"], cuit)
    sit_iva = "1" if "inscripto" in (e["condicion_iva"] or "").lower() else None
    jur = calcular.jurisdiccion_de(e["domicilio"])

    if p is None:
        cambios.append("alta del proveedor")
        if escribir:
            conn.execute(
                "INSERT INTO proveedores (cuit, razon_social, tipo_persona, "
                "situacion_iva, nro_inscripcion_ib, domicilio, jurisdiccion) "
                "VALUES (?,?,?,?,?,?,?)",
                (cuit, e["razon_social"] or cuit,
                 "H" if cuit[:2] in ("20", "23", "24", "27") else "J",
                 sit_iva, iibb, e["domicilio"], jur))
        return cambios, True

    for columna, nuevo, etiqueta in [("domicilio", e["domicilio"], "domicilio"),
                                     ("nro_inscripcion_ib", iibb, "N de inscripcion en IIBB"),
                                     ("situacion_iva", sit_iva, "situacion frente al IVA"),
                                     ("jurisdiccion", jur, "jurisdiccion")]:
        if nuevo and not p[columna]:
            cambios.append(f"completa el {etiqueta} desde la factura: {nuevo}")
            if escribir:
                conn.execute(f"UPDATE proveedores SET {columna} = ? WHERE cuit = ?",
                             (nuevo, cuit))
    return cambios, False


def siguiente_certificado(conn, impuesto, anio):
    """Proximo numero de la serie, respetando el formato de cada impuesto."""
    filas = conn.execute(
        "SELECT nro_certificado FROM retenciones WHERE impuesto = ? "
        "AND nro_certificado LIKE ?", (impuesto, f"{anio}-%")).fetchall()
    maximo = 0
    ancho = 3 if impuesto == "ganancias" else 7
    for (n,) in filas:
        sec = (n or "").split("-", 1)[-1].strip()
        if sec.isdigit():
            maximo = max(maximo, int(sec))
            ancho = max(ancho, len(sec))
    return f"{anio}-{str(maximo + 1).zfill(ancho)}"


def guardar(conn, f, calculo, fecha_pago, servicio_en_caba=False):
    """Crea el comprobante y sus retenciones, y devuelve los certificados asignados."""
    e = f["emisor"]
    i = f["importes"]
    cur = conn.execute(
        "INSERT INTO comprobantes (cuit, tipo, letra, punto_venta, numero, "
        "numero_crudo, fecha, neto, importe_iva, otros_conceptos, total, "
        "servicio_en_caba, origen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (e["cuit"], "FC", f["letra"], f["punto_venta"], f["numero"],
         f"{f['punto_venta']}-{f['numero']}", f["fecha"], i["neto_gravado"],
         i["iva"], i["otros_tributos"], i["total"],
         1 if servicio_en_caba else 0, f["archivo"]))
    cid = cur.lastrowid

    clave = {"Ganancias": "ganancias", "IIBB CABA": "iibb_caba",
             "IVA": "iva", "SUSS": "suss"}
    emitidos = []
    for r in calculo["retenciones"]:
        imp = clave[r["impuesto"]]
        nro = None
        if r["monto"] > 0 and r.get("activo") is not False and imp in ("ganancias", "iibb_caba"):
            nro = siguiente_certificado(conn, imp, fecha_pago[:4])
        # una linea apagada a mano se guarda igual, en cero y con el motivo: es
        # una decision que conviene que quede registrada
        apagada = r.get("activo") is False
        conn.execute(
            "INSERT INTO retenciones (comprobante_id, impuesto, regimen, periodo, "
            "base_imponible, alicuota, monto, fecha_retencion, nro_certificado, "
            "estado, motivo, origen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, imp, r["regimen"], fecha_pago[:7], r["base"],
             None if isinstance(r["alicuota"], str) else r["alicuota"],
             r["monto"], fecha_pago, nro,
             "practicada" if r["monto"] > 0 else "no_practicada",
             (f"desactivada a mano (habria sido ${r.get('monto_original', 0):,.2f})"
              if apagada else r.get("nota")),
             f["archivo"]))
        if nro:
            emitidos.append((imp, nro))
    return cid, emitidos


def main():
    ap = argparse.ArgumentParser(
        description="Lee una factura, calcula las retenciones y emite los certificados")
    ap.add_argument("pdf")
    ap.add_argument("--regimen", type=int, help="codigo de regimen de Ganancias")
    ap.add_argument("--fecha-pago", help="fecha de la retencion (ISO). Por defecto, hoy")
    ap.add_argument("--confirmar", action="store_true",
                    help="guarda en la base y genera los PDF de los certificados")
    clientes.agregar_argumento(ap)
    args = ap.parse_args()
    cliente = clientes.del_argumento(args)
    if args.confirmar and cliente.faltantes():
        raise SystemExit(f"faltan datos de {cliente.nombre}: {', '.join(cliente.faltantes())}")

    f = leer_factura.leer(args.pdf)
    avisos_lectura = leer_factura.controles(f)
    e = f["emisor"]
    print(f"FACTURA   {f['tipo']} {f['letra']}  {f['punto_venta']}-{f['numero']}   {f['fecha']}")
    print(f"          {e['razon_social']}   CUIT {e['cuit']}")
    print(f"          neto {f['importes']['neto_gravado']:,.2f}   "
          f"total {f['importes']['total']:,.2f}")
    for a in avisos_lectura:
        print(f"          AVISO DE LECTURA: {a}")
    print()

    conn = cliente.conectar()
    try:
        conn.execute("BEGIN")
        cambios, es_nuevo = sincronizar_proveedor(conn, f, args.confirmar)
        for c in cambios:
            print(f"PROVEEDOR {c}")
        if cambios:
            print()

        regimen = args.regimen or regimen_habitual(conn, e["cuit"])
        if regimen is None:
            print("No se puede proponer un regimen de Ganancias: el proveedor no tiene "
                  "historial.\nVolve a correrlo con --regimen.")
            conn.rollback()
            return
        if not args.regimen:
            print(f"REGIMEN   {regimen} (el que mas uso este proveedor). "
                  f"Se cambia con --regimen.")
            print()

        fecha_pago = args.fecha_pago or calcular.date.today().isoformat()
        partidas = [{"base": x["subtotal"] or 0.0, "regimen": regimen}
                    for x in (f.get("items") or [])]
        calculo = calcular.calcular(conn, e["cuit"], f["importes"]["base_ganancias"],
                                    regimen, fecha_pago,
                                    neto_gravado=f["importes"]["neto_gravado"],
                                    partidas=partidas or None, letra=f["letra"],
                                    sujeta_a_retencion=f["sujeta_a_retencion"],
                                    iva_facturado=f["importes"]["iva"],
                                    agente_de=cliente.agente_de())
        calcular.imprimir(calculo)

        if not args.confirmar:
            print()
            print("Nada se guardo. Si la propuesta esta bien, volve a correrlo "
                  "con --confirmar.")
            conn.rollback()
            return

        cid, emitidos = guardar(conn, f, calculo, fecha_pago)
        conn.commit()
        print()
        print(f"Guardado. Comprobante #{cid}.")
        for _, nro in emitidos:
            filas = certificados.retenciones(conn, certificado=nro)
            rutas, sin_dom = certificados.emitir(conn, filas, cliente.certificados,
                                                 agente=cliente.datos())
            for ruta in rutas:
                print(f"   certificado {nro}  ->  {ruta.name}")
            for cuit, rs in sin_dom:
                print(f"   AVISO: {rs} no tiene domicilio: el certificado sale sin ese dato")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
