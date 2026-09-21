# -*- coding: utf-8 -*-
"""Clientes del estudio: cada uno es un agente de retención distinto.

Cada cliente vive en su propia carpeta, con su propia base de datos:

    clientes/
      fiberhome/
        cliente.json       razón social, CUIT y domicilios del agente
        retenciones.db     sus proveedores, comprobantes y retenciones
        entrada/           facturas subidas, sin procesar
        procesadas/        facturas ya confirmadas
        certificados/      los PDF emitidos

Una base por cliente, y no una sola con una columna "cliente": así es imposible
por construcción que una consulta mezcle datos de dos clientes. Lo único que se
comparte es lo público — el padrón de AGIP (cacheado aparte) y las tablas de
AFIP, que se copian a cada base al crearla.

    python clientes.py --listar
    python clientes.py --crear "Nombre del cliente"
"""
import argparse
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from escala_anexo_viii import UNIDAD_2026, tramos

DIR = Path(__file__).resolve().parent
RAIZ = DIR / "clientes"
TABLA_AFIP = DIR.parent / "diseno-sistema-retenciones" / "regimenes_ganancias_afip.json"

# De que impuestos es agente de retencion el cliente. Ganancias alcanza a
# practicamente todos; IVA y SUSS solo se disparan con proveedores marcados
# (limpieza, seguridad, construccion). IIBB CABA en cambio requiere estar
# inscripto como agente de recaudacion de AGIP: por defecto, no.
IMPUESTOS = {"ganancias": "Ganancias", "iibb_caba": "IIBB CABA",
             "iva": "IVA", "suss": "SUSS"}
AGENTE_POR_DEFECTO = {"ganancias": True, "iibb_caba": False, "iva": True, "suss": True}

DATOS_VACIOS = {
    "razon_social": "",
    "cuit": "",
    "domicilios": {"default": "", "ganancias": "", "iibb_caba": ""},
    "agente_de": dict(AGENTE_POR_DEFECTO),
}


def slug_de(nombre):
    """'Estudio Pérez S.A.' -> 'estudio-perez-s-a'"""
    t = unicodedata.normalize("NFKD", nombre)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


@dataclass(frozen=True)
class Cliente:
    slug: str

    @property
    def carpeta(self):
        return RAIZ / self.slug

    @property
    def db(self):
        return self.carpeta / "retenciones.db"

    @property
    def entrada(self):
        return self.carpeta / "entrada"

    @property
    def procesadas(self):
        return self.carpeta / "procesadas"

    @property
    def certificados(self):
        return self.carpeta / "certificados"

    @property
    def config(self):
        return self.carpeta / "cliente.json"

    def datos(self):
        if not self.config.exists():
            return {"nombre": self.slug, **DATOS_VACIOS}
        return json.loads(self.config.read_text(encoding="utf-8"))

    def guardar_datos(self, datos):
        self.config.write_text(json.dumps(datos, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    @property
    def nombre(self):
        return self.datos().get("nombre") or self.slug

    def agente_de(self):
        """Impuestos que este cliente retiene, como set de claves."""
        conf = {**AGENTE_POR_DEFECTO, **(self.datos().get("agente_de") or {})}
        return {k for k, v in conf.items() if v}

    def faltantes(self):
        """Lo que falta cargar para poder emitir certificados."""
        d = self.datos()
        out = []
        if not d.get("razon_social"):
            out.append("razón social")
        if not re.fullmatch(r"\d{11}", d.get("cuit") or ""):
            out.append("CUIT")
        if not (d.get("domicilios") or {}).get("default"):
            out.append("domicilio")
        return out

    def conectar(self):
        if not self.db.exists():
            raise FileNotFoundError(f"el cliente {self.slug} no tiene base creada")
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def resumen(self):
        """Cifras para el panel principal."""
        if not self.db.exists():
            return {"comprobantes": 0, "retenciones": 0, "ultimo": None, "pendientes": 0}
        conn = self.conectar()
        try:
            c = conn.execute("SELECT COUNT(*), MAX(fecha) FROM comprobantes").fetchone()
            r = conn.execute("SELECT COUNT(*) FROM retenciones "
                             "WHERE estado = 'practicada'").fetchone()[0]
        finally:
            conn.close()
        pend = len(list(self.entrada.glob("*.pdf"))) if self.entrada.exists() else 0
        return {"comprobantes": c[0], "retenciones": r, "ultimo": c[1], "pendientes": pend}


def inicializar_base(conn):
    """Crea el esquema y carga las tablas normativas de AFIP.

    Es lo mismo para todos los clientes: el régimen de Ganancias y la escala del
    Anexo VIII son públicos. El padrón de AGIP no se copia entero: se incorpora
    CUIT por CUIT a medida que aparecen proveedores.
    """
    conn.executescript((DIR / "esquema.sql").read_text(encoding="utf-8"))
    afip = json.loads(TABLA_AFIP.read_text(encoding="utf-8"))
    conn.executemany(
        "INSERT INTO regimenes_ganancias (id_alicuota, cod_regimen, situacion, "
        "tipo_persona, concepto, anexo, alicuota, monto_no_sujeto, monto_minimo, "
        "sincronizado) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r["ID_ALICUOTA"], r["COD_REGIMEN"], r["SITUACION"], r["TIPO_PERSONA"] or "",
          r["CONCEPTO"], r["ANEXO"], r["PORCENT_A_RETENER"] / 100,
          r["MONTO_NO_SUJETO"], r["MONTO_MINIMO"], afip["fecha_descarga"])
         for r in afip["regimenes"]])
    conn.executemany(
        "INSERT INTO escala_ganancias (vigencia_desde, desde, monto_fijo, alicuota) "
        "VALUES (?,?,?,?)",
        [("2026-01-01", d, f, t) for d, f, t in tramos(UNIDAD_2026)])
    conn.commit()


def agregar_argumento(ap):
    """--cliente para las herramientas de linea de comandos."""
    ap.add_argument("--cliente", required=True,
                    help="slug del cliente (ver: python clientes.py --listar)")


def del_argumento(args):
    try:
        return obtener(args.cliente)
    except KeyError as e:
        raise SystemExit(str(e))


def listar():
    if not RAIZ.exists():
        return []
    return sorted((Cliente(p.name) for p in RAIZ.iterdir()
                   if p.is_dir() and (p / "cliente.json").exists()),
                  key=lambda c: c.nombre.lower())


def obtener(slug):
    c = Cliente(slug_de(slug))
    if not c.config.exists():
        raise KeyError(f"no existe el cliente {slug!r}")
    return c


def crear(nombre, datos=None):
    """Da de alta un cliente: carpeta, base vacía con las tablas de AFIP y ficha."""
    c = Cliente(slug_de(nombre))
    for carpeta in (c.carpeta, c.entrada, c.procesadas, c.certificados):
        carpeta.mkdir(parents=True, exist_ok=True)
    if not c.config.exists():
        c.guardar_datos({"nombre": nombre, **DATOS_VACIOS, **(datos or {})})
    if not c.db.exists():
        conn = sqlite3.connect(c.db)
        try:
            inicializar_base(conn)
        finally:
            conn.close()
    return c


def main():
    ap = argparse.ArgumentParser(description="Clientes del estudio")
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--crear", metavar="NOMBRE")
    args = ap.parse_args()
    if args.crear:
        c = crear(args.crear)
        print(f"cliente creado: {c.nombre}  ->  {c.carpeta}")
        return
    for c in listar():
        r = c.resumen()
        falta = c.faltantes()
        print(f"   {c.nombre:22} {r['comprobantes']:>5} comprobantes  "
              f"{r['retenciones']:>5} retenciones"
              + (f"   FALTA: {', '.join(falta)}" if falta else ""))


if __name__ == "__main__":
    main()
