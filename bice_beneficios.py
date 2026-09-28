"""
Descarga todos los beneficios/descuentos de Banco BICE.

Endpoint (Modyo Content API, el mismo que usa el sitio):
    https://banco.bice.cl/api/content/spaces/beneficios-bice/types/beneficios/entries
    params: per_page (max 100), page

El sitio está detrás de un WAF que responde 403 a clientes "no navegador"
(requests, urllib). Por eso se usa curl_cffi, que imita el TLS de Chrome.
    pip install curl_cffi

Uso:
    python bice_beneficios.py                # guarda en ./data/
    python bice_beneficios.py --out otra/carpeta
Genera:
    bice_beneficios_YYYY-MM-DD.json   (respuesta cruda, todas las páginas)
    bice_beneficios_YYYY-MM-DD.csv    (tabla plana, texto limpio)
    bice_beneficios_latest.csv        (copia del último run)
"""
import argparse
import csv
import html
import json
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path

from curl_cffi import requests

API_URL = "https://banco.bice.cl/api/content/spaces/beneficios-bice/types/beneficios/entries"
PUBLIC_URL = "https://banco.bice.cl/personas/beneficios/{slug}"
PER_PAGE = 100


def fetch_all(retries=3):
    session = requests.Session(impersonate="chrome")
    session.headers.update({
        "Accept": "application/json",
        "Referer": "https://banco.bice.cl/personas/beneficios",
    })
    entries, page, total_pages = [], 1, 1
    while page <= total_pages:
        for attempt in range(1, retries + 1):
            r = session.get(API_URL, params={"per_page": PER_PAGE, "page": page}, timeout=60)
            if r.status_code == 200:
                break
            print(f"página {page}: HTTP {r.status_code}, reintento {attempt}/{retries}", file=sys.stderr)
            time.sleep(5 * attempt)
        else:
            raise RuntimeError(f"No se pudo descargar la página {page} (HTTP {r.status_code})")
        data = r.json()
        total_pages = data["meta"]["total_pages"]
        entries.extend(data["entries"])
        print(f"página {page}/{total_pages}: {len(data['entries'])} beneficios")
        page += 1
    return entries


def clean_html(value):
    if not value:
        return ""
    text = re.sub(r"<\s*(br|/p|/li)\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n", text.replace("\xa0", " ")).strip()


def join(value):
    return " | ".join(value) if isinstance(value, list) else (value or "")


def flatten(entry):
    m, f = entry["meta"], entry["fields"]
    return {
        "uuid": m["uuid"],
        "nombre": m["name"],
        "marca": f.get("Marca", ""),
        "categoria": m.get("category_name") or "",
        "subcategorias": join(f.get("Sub-Categorias")),
        "descuento": f.get("Promo-index", ""),
        "promo_texto": f.get("Texto-promo-small", ""),
        "resumen": clean_html(f.get("Bajada-sitio-publico") or f.get("Bajada")),
        "descripcion": clean_html(f.get("Descripcion_beneficio")),
        "como_usar": clean_html(f.get("Como-usar-beneficio")),
        "requisitos": clean_html(f.get("Para_hacer_valido_beneficio")),
        "condiciones": clean_html(f.get("Condiciones_beneficio")),
        "donde": f.get("Donde", ""),
        "direccion": clean_html(f.get("Direccion")),
        "ciudades": clean_html(f.get("Ciudades")),
        "regiones": join(f.get("Region")),
        "tarjetas": join(f.get("Tarjetas")),
        "tipo_tarjeta": join(f.get("Tipo Tarjeta")),
        "segmento": join(f.get("Segmento")),
        "fecha_desde": f.get("Fecha-desde", ""),
        "fecha_hasta": f.get("Fecha-hasta", ""),
        "sitio_web": f.get("Sitio_web", ""),
        "logo": (f.get("Logo") or {}).get("url", ""),
        "imagen": (f.get("Imagen-show") or {}).get("url", ""),
        "url_bice": PUBLIC_URL.format(slug=m["slug"]),
        "actualizado": m.get("updated_at", ""),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=Path(__file__).parent / "data", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    entries = fetch_all()
    stamp = date.today().isoformat()

    json_path = args.out / f"bice_beneficios_{stamp}.json"
    json_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = [flatten(e) for e in entries]
    csv_path = args.out / f"bice_beneficios_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:  # utf-8-sig: Excel lee bien los acentos
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    shutil.copyfile(csv_path, args.out / "bice_beneficios_latest.csv")

    print(f"{len(rows)} beneficios -> {csv_path}")


if __name__ == "__main__":
    main()
