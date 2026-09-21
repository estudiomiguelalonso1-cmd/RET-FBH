# -*- coding: utf-8 -*-
"""Descarga y carga el Padron de Regimenes Generales de AGIP.

Es la fuente de la alicuota de IIBB CABA. Se publica una vez por mes.

    python padron_agip.py --listar                 que periodos hay publicados
    python padron_agip.py --descargar              el ultimo publicado
    python padron_agip.py --descargar 2026-08
    python padron_agip.py --cargar ARDJU008082026.rar
    python padron_agip.py --buscar 30715410253     un CUIT en el padron ya bajado

Notas de por que esta hecho asi:

  * La pagina de AGIP es una SPA: el HTML que devuelve el servidor no trae los
    enlaces. Los datos salen de POST /api/pages/byPath, que devuelve el contenido
    como HTML escapado dentro de un JSON.
  * El nombre del archivo es deterministico (ARDJU008MMAAAA.rar) pero la carpeta
    cambia casi todos los meses, asi que hay que leer la pagina igual.
  * El archivo es un RAR4 de unos 20 MB que descomprime a un TXT de unos 130 MB.
    Se usa el tar.exe de Windows, que trae libarchive y lee RAR sin instalar nada.
  * Se carga en la base de cada cliente que sea agente de IIBB CABA.
  * Por defecto solo se cargan a la base los CUIT que ya son proveedores: son
    decenas contra el millon y medio de renglones que trae el padron. El TXT queda
    cacheado para poder buscar cualquier otro CUIT cuando aparezca.
"""
import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

import clientes

DIR = Path(__file__).resolve().parent
CACHE = DIR / "padrones"

API = "https://www.agip.gob.ar/api/pages/byPath"
# El padron vigente se publica en la pagina de agentes de recaudacion. La pagina
# "Historico" lista los meses anteriores y no incluye el vigente: de ahi se bajaba
# antes, y por eso parecia que AGIP publicaba con un mes de atraso.
PAGINAS = ("/agentes/agentes-de-recaudacion-e-informacion",
           "/agentes/agentes-de-recaudacion/ib-agentes-recaudacion/padrones/"
           "Padr\u00f3n-de-Reg\u00edmenes-Generales")
TAR = Path(r"C:\Windows\System32\tar.exe")

MESES = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
         7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
         12: "diciembre"}


def publicados():
    """{'2026-09': url, ...} leyendo las paginas de AGIP (vigente e historico)."""
    out = {}
    for pagina in PAGINAS:
        req = urllib.request.Request(
            API, data=json.dumps({"path": pagina}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        # los enlaces pueden venir en cualquier campo del JSON: se busca en todo
        texto = json.dumps(json.loads(
            urllib.request.urlopen(req, timeout=60).read().decode("utf-8")),
            ensure_ascii=False)
        for url, mm, aaaa in re.findall(
                r'(https?://[^"\'\s<>\\]*?ARDJU\d{3}(\d{2})(\d{4})\.rar)', texto):
            out.setdefault(f"{aaaa}-{mm}", url.replace(" ", "%20"))
    return dict(sorted(out.items(), reverse=True))


def descargar(periodo=None):
    disponibles = publicados()
    if not disponibles:
        raise SystemExit("la pagina de AGIP no devolvio ningun padron")
    periodo = periodo or next(iter(disponibles))
    if periodo not in disponibles:
        raise SystemExit(f"no hay padron publicado para {periodo}. "
                         f"Hay: {', '.join(list(disponibles)[:6])}...")
    CACHE.mkdir(exist_ok=True)
    destino = CACHE / Path(disponibles[periodo]).name
    if not destino.exists():
        print(f"bajando {periodo} de {disponibles[periodo]}")
        with urllib.request.urlopen(
                urllib.request.Request(disponibles[periodo],
                                       headers={"User-Agent": "Mozilla/5.0"}),
                timeout=300) as r, destino.open("wb") as fh:
            shutil.copyfileobj(r, fh)
    print(f"archivo: {destino.name}  ({destino.stat().st_size / 1e6:.1f} MB)")
    return destino


def extraer(archivo):
    """Descomprime el RAR y devuelve la ruta del TXT."""
    archivo = Path(archivo)
    if archivo.suffix.lower() == ".txt":
        return archivo
    txt = archivo.with_suffix(".TXT")
    if txt.exists():
        return txt
    if not TAR.exists():
        raise SystemExit("no encuentro tar.exe para descomprimir el .rar")
    subprocess.run([str(TAR), "-xf", archivo.name], cwd=archivo.parent, check=True)
    candidatos = list(archivo.parent.glob(archivo.stem + ".*"))
    txt = next((c for c in candidatos if c.suffix.lower() == ".txt"), None)
    if txt is None:
        raise SystemExit(f"el archivo no contenia un TXT: {[c.name for c in candidatos]}")
    return txt


def renglones(txt):
    with open(txt, encoding="latin-1") as fh:
        for linea in fh:
            p = linea.rstrip("\n").split(";")
            if len(p) < 12:
                continue
            yield p


def iso(d):
    return f"{d[4:8]}-{d[2:4]}-{d[0:2]}" if len(d) == 8 else d


def cargar(txt, conn, todos=False):
    """Inserta en padron_iibb_caba. Por defecto, solo los CUIT que son proveedores."""
    conocidos = None
    if not todos:
        conocidos = {c for (c,) in conn.execute("SELECT cuit FROM proveedores")}
    filas, leidos = [], 0
    for p in renglones(txt):
        leidos += 1
        if conocidos is not None and p[3] not in conocidos:
            continue
        filas.append((p[3], iso(p[1]), iso(p[2]), iso(p[0]), p[4],
                      float(p[7].replace(",", ".")) / 100,
                      float(p[8].replace(",", ".")) / 100, p[11].strip(),
                      ";".join(p).strip()))
    conn.executemany(
        "INSERT OR REPLACE INTO padron_iibb_caba (cuit, vigencia_desde, vigencia_hasta, "
        "publicacion, tipo_contr, alic_percepcion, alic_retencion, razon_social, "
        "renglon) VALUES (?,?,?,?,?,?,?,?,?)", filas)
    conn.commit()
    return leidos, len(filas)


def buscar(txt, cuit):
    for p in renglones(txt):
        if p[3] == cuit:
            return p
    return None


def main():
    ap = argparse.ArgumentParser(description="Padron de Regimenes Generales de AGIP")
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--descargar", nargs="?", const="", metavar="AAAA-MM")
    ap.add_argument("--cargar", metavar="ARCHIVO")
    ap.add_argument("--buscar", metavar="CUIT")
    ap.add_argument("--todos", action="store_true",
                    help="carga el padron completo y no solo los proveedores conocidos")
    args = ap.parse_args()

    if args.listar:
        for periodo, url in publicados().items():
            print(f"   {periodo}   {url}")
        return

    archivo = None
    if args.descargar is not None:
        archivo = descargar(args.descargar or None)
    elif args.cargar:
        archivo = Path(args.cargar)
    if archivo is None:
        ap.error("indica --descargar, --cargar, --listar o --buscar")

    txt = extraer(archivo)
    print(f"padron: {txt.name}  ({txt.stat().st_size / 1e6:.0f} MB)")

    if args.buscar:
        p = buscar(txt, args.buscar)
        if not p:
            print(f"el CUIT {args.buscar} no esta en este padron")
        else:
            print(f"   {p[11].strip()}")
            print(f"   tipo {p[4]} ({'local' if p[4] == 'D' else 'convenio multilateral'})"
                  f"   vigencia {iso(p[1])} a {iso(p[2])}")
            print(f"   alicuota retencion {p[8]} %   percepcion {p[7]} %")
        return

    agentes = [c for c in clientes.listar() if "iibb_caba" in c.agente_de()]
    if not agentes:
        print("ningun cliente es agente de IIBB CABA: no se carga en ninguna base")
    for cliente in agentes:
        conn = cliente.conectar()
        try:
            leidos, cargados = cargar(txt, conn, args.todos)
            print(f"{cliente.nombre}: {leidos:,} renglones leidos, {cargados:,} cargados"
                  f"{'' if args.todos else ' (solo proveedores conocidos)'}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
