"""
Descarga los beneficios/descuentos de BICE, BCI, Banco de Chile, Santander,
Banco Security, Itaú y Scotiabank (restaurantes) y los guarda en un esquema unificado.

    python fetch_benefits.py --out ./output --sites all
    python fetch_benefits.py --sites bci,santander

Ver README.md para detalles y cómo actualizar los endpoints.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import random
import re
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests
from dateutil import parser as dateparser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# BICE y Santander responden 403 al cliente `requests` (bloqueo por huella TLS).
# curl_cffi imita el TLS de Chrome y expone la misma API que `requests`.
IMPERSONATE = "chrome"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "es-CL,es;q=0.9",
}
# Clave pública que la web de BCI envía desde el navegador de cualquier visitante
# (definida en el JS del widget de https://www.bci.cl/beneficios/beneficios-bci).
BCI_SUBSCRIPTION_KEY = "fa981752762743668413b68821a43840"

# Banco Security (Drupal Views AJAX). Los filtros expuestos y `page` se leen del
# query string: si van sólo en el cuerpo del POST, Drupal los ignora.
SECURITY_BASE_URL = "https://personas.bancosecurity.cl"
SECURITY_CATEGORY_ID = 121          # "Restaurantes" dentro de /beneficios/gourmet
SECURITY_MAX_PAGES = 50             # tope de seguridad (hoy son ~3 páginas de 12)
SECURITY_TIMEOUT = 60

# Itaú (itaubeneficios.cl, WordPress). La REST API está deshabilitada (todo /wp-json/ y
# ?rest_route= devuelve la portada) y no hay sitemap, feed ni admin-ajax público, así que
# se usa un FALLBACK HTML: tarjetas de las páginas de listado + página de detalle de cada una.
ITAU_BASE_URL = "https://itaubeneficios.cl"
ITAU_LISTING_PAGES = [                      # ninguna tiene paginación; se unen y deduplican
    "/beneficios/beneficios-y-descuentos/",  # listado general (~178, le faltan algunos)
    "/restaurantes/",
    "/ruta-gourmet/",                        # redirige a /ruta-gourmet-black/
]
# Se compara (sin tildes, palabra completa) contra la categoría de la tarjeta y el primer
# segmento de su URL, p. ej. "Martes Gourmet" / "/martes-gourmet/...".
ITAU_RESTAURANT_KEYWORDS = [
    "restaurante", "restaurantes", "gourmet", "comida", "pizza", "pizzeria", "cafe",
    "cafeteria", "bar", "sabor", "sabores", "sushi", "martes gourmet",
]
ITAU_FETCH_DETAILS = True                   # False = sólo datos de la tarjeta (más rápido)

# Scotiabank "Ruta Gourmet". No hay API: los datos vienen embebidos en el HTML de la página
# como arreglos JS (`const sitiosSantiago = [...]`, `const sitiosRegiones = [...]`).
SCOTIA_BASE_URL = "https://www.scotiarewards.cl"
SCOTIA_URL = f"{SCOTIA_BASE_URL}/scclubfront/categoria/platosycomida/rutagourmet"
SCOTIA_ARRAYS = ("sitiosSantiago", "sitiosRegiones")

# Numeración oficial de regiones de Chile (la que usa `id_region` de Scotiabank: 13 = RM, 8 = Biobío).
CHILE_REGIONS = {
    1: "Tarapacá", 2: "Antofagasta", 3: "Atacama", 4: "Coquimbo", 5: "Valparaíso",
    6: "O'Higgins", 7: "Maule", 8: "Biobío", 9: "La Araucanía", 10: "Los Lagos", 11: "Aysén",
    12: "Magallanes", 13: "Metropolitana de Santiago", 14: "Los Ríos", 15: "Arica y Parinacota",
    16: "Ñuble",
}

log = logging.getLogger("fetch_benefits")


# --------------------------------------------------------------------------- HTTP

class RetryableHTTPError(Exception):
    pass


class Fetcher:
    def __init__(self):
        self.session = requests.Session(impersonate=IMPERSONATE)
        self.session.headers.update(HEADERS)

    def get_json(self, url: str, params: dict, headers: dict | None = None, timeout: int = 60):
        return self._request("GET", url, params=params, headers=headers, timeout=timeout).json()

    def post_json(self, url: str, data: dict, headers: dict | None = None, timeout: int = 60):
        return self._request("POST", url, data=data, headers=headers, timeout=timeout).json()

    def get_html(self, url: str, timeout: int = 60) -> str:
        return self._request("GET", url, headers={"Accept": "text/html,application/xhtml+xml"},
                             timeout=timeout).text

    @retry(retry=retry_if_exception_type((RetryableHTTPError, requests.RequestsError)),
           stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=60),
           reraise=True)
    def _request(self, method: str, url: str, **kwargs):
        r = self.session.request(method, url, **kwargs)
        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 120))
            raise RetryableHTTPError(f"HTTP {r.status_code} en {r.url}")
        r.raise_for_status()
        return r


def paginate(fetcher: Fetcher, url: str, *, page_param: str, size_param: str, size: int,
             items: Callable[[dict], list], extra_params: dict | None = None,
             headers: dict | None = None) -> Iterator[dict]:
    """Pide page=1,2,3... hasta que la lista venga vacía o más corta que `size`."""
    page = 1
    while True:
        data = fetcher.get_json(url, {**(extra_params or {}), size_param: size, page_param: page}, headers)
        batch = items(data) or []
        log.info("  %s página %d: %d registros", url, page, len(batch))
        yield from batch
        if len(batch) < size:
            return
        page += 1
        time.sleep(random.uniform(1, 2))


# --------------------------------------------------------------------- helpers

def clean_html(value) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\b[^>]*>", "\n", value, flags=re.IGNORECASE)  # <br data-x="1"/> incl.
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").replace("\r", "")
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def iso_date(value) -> str | None:
    if not value:
        return None
    try:
        return dateparser.parse(value).isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def urls(*assets) -> list[str]:
    out = []
    for a in assets:
        u = a.get("url") if isinstance(a, dict) else a
        if u and isinstance(u, str) and u not in out:
            out.append(u)
    return out


def record(source, id, title, description="", url="", category="", tags=None, images=None,
           conditions="", merchant="", discount="", valid_from=None, valid_to=None,
           location="", days="") -> dict:
    """Esquema unificado (los 9 primeros campos son los del spec; el resto son extras útiles)."""
    return {
        "source": source, "id": str(id), "title": (title or "").strip(),
        "description": description, "url": url, "category": category or "",
        "tags": [t for t in (tags or []) if t], "images": images or [],
        "conditions": conditions, "merchant": (merchant or "").strip(),
        "discount": discount or "", "valid_from": iso_date(valid_from), "valid_to": iso_date(valid_to),
        "location": location or "", "days": days or "",
    }


# ---------------------------------------------------------------- normalizers

def norm_bice(e):
    m, f = e["meta"], e["fields"]
    return record(
        "bice", m["uuid"], f.get("Titulo-sitio-publico") or m["name"],
        clean_html(f.get("Descripcion_beneficio") or f.get("Bajada-sitio-publico") or f.get("Bajada")),
        f"https://banco.bice.cl/personas/beneficios/{m['slug']}",
        m.get("category_name"), f.get("Sub-Categorias"),
        urls(f.get("Imagen-show"), f.get("Logo"), *(f.get("Galeria") or [])),
        clean_html(f.get("Condiciones_beneficio")), f.get("Marca"), f.get("Promo-index"),
        f.get("Fecha-desde"), f.get("Fecha-hasta"))


def norm_bci(o):
    pct = ((o.get("beneficio") or {}).get("discount") or {}).get("porcentajeDescuento")
    cats = [c.get("titulo") for c in o.get("categorias") or []]
    return record(
        "bci", o["id"], o.get("titulo"),
        "\n".join(x for x in (clean_html(o.get("subtitulo")), clean_html(o.get("descripcion"))) if x),
        o.get("link") or f"https://www.bci.cl/beneficios/beneficios-bci/detalle/{o['slug']}",
        cats[0] if cats else "", [t.get("nombre") for t in o.get("tags") or []] + cats[1:],
        urls(*(o.get("imagenes") or {}).values()), clean_html(o.get("legal")),
        (o.get("comercio") or {}).get("nombre"), f"{pct}%" if pct else "",
        o.get("fechaInicio"), o.get("fechaTermino") if o.get("tieneFechaTermino") else None)


def norm_bancochile_beneficio(e):
    m, f = e["meta"], e["fields"]
    return record(
        "bancochile", m["uuid"], f.get("Titulo") or m["name"], clean_html(f.get("Descripcion")),
        f.get("Url Beneficio Externa") or f.get("Url")
        or f"https://sitiospublicos.bancochile.cl/personas/beneficios/detalle/{m['slug']}",
        m.get("category"), m.get("tags"), urls(f.get("Portada"), f.get("Logo")),
        clean_html(f.get("Condiciones Comerciales")), m["name"], f.get("Tipo Beneficio"),
        m.get("published_at"), m.get("unpublish_at"))


def norm_bancochile_seguro(e):
    m, f = e["meta"], e["fields"]
    return record(
        "bancochile", m["uuid"], f.get("Titulo_Tarjeta") or m["name"],
        clean_html(f.get("Texto_Tarjeta") or f.get("Cobertura_y_Beneficios")),
        f.get("Url Externa") or "", m.get("category"), m.get("tags"),
        urls(f.get("Imagen")), clean_html(f.get("Condiciones_Legales")), m["name"],
        f.get("Seguro_Viajes_Porcentaje_Descuento") or f.get("Descuento") or "")


def norm_santander(p):
    # La API trae `category` y `discount` casi siempre en null: la categoría viene
    # codificada en los tags ("cat-descuentos") y el % sólo aparece en el texto.
    tags = p.get("tags") or []
    cat_tags = [t[4:].replace("-", " ") for t in tags if t.startswith("cat-")]
    description = clean_html(p.get("excerpt")) or clean_html(p.get("description"))
    # Se descartan tasas ("CAE 1,51%", "tasa de interés 0,99%") y decimales, que no son descuentos.
    without_rates = re.sub(r"(CAE|tasa|inter[eé]s)[^%]{0,30}%", " ", description, flags=re.IGNORECASE)
    pct = re.search(r"(?<![\d.,])(\d{1,3})\s?%", without_rates)
    return record(
        "santander", p["id"], p.get("title"), description,
        p.get("url"), p.get("category") or ", ".join(cat_tags), tags, urls(*(p.get("covers") or [])),
        clean_html(p.get("conditions")), p.get("title"),
        p.get("discount") or (f"{pct.group(1)}%" if pct else ""),
        p.get("start_date"), p.get("end_date"))


def norm_security(c):
    pct = c["discount_percent"]
    return record(
        "security", c["node_id"] or c["detail_url"], c["title"], c["description"], c["detail_url"],
        c["category_label"], ["Restaurantes", *[d.strip() for d in re.split(r",| y ", c["days"]) if d.strip()]],
        urls(c["main_image_url"], c["logo_url"]), "", c["title"],
        f"{pct:g}%" if pct is not None else c["discount_text"],
        location=c["location"], days=c["days"])


# ------------------------------------------------------------- Banco Security

def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _icon_text(card, icon_class: str) -> str:
    icon = card.select_one(f"i.{icon_class}")
    return _text(icon.parent).rstrip(". ") if icon else ""


def parse_security_cards(html_fragment: str, base_url: str = SECURITY_BASE_URL) -> list[dict]:
    """Extrae las tarjetas de beneficio del HTML que devuelve Drupal Views AJAX."""
    soup = BeautifulSoup(html_fragment, "html.parser")
    cards = soup.select("article[data-history-node-id]") or soup.select("a.card-container")
    out = []
    for card in cards:
        link = card if card.name == "a" else card.select_one("a.card-container")
        data = card.select_one(".data-container")
        data_divs = data.find_all("div", recursive=False) if data else []
        discount_text = _text(card.select_one(".beneficio-label")) or (_text(data_divs[-1]) if data_divs else "")
        pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", discount_text)
        imgs = [urljoin(base_url, i["src"]) for i in card.select("img[src]")]
        out.append({
            "node_id": card.get("data-history-node-id", ""),
            "detail_url": urljoin(base_url, link["href"]) if link and link.get("href") else "",
            "title": _text(data_divs[0]) if data_divs else "",
            "description": _text(card.select_one(".description")),
            "discount_text": discount_text,
            "discount_percent": float(pct.group(1).replace(",", ".")) if pct else None,
            "location": _icon_text(card, "la-map-marker"),
            "days": _icon_text(card, "la-calendar-day"),
            "category_label": _text(card.select_one(".coh-ce-c1e71a37")),
            "main_image_url": imgs[0] if imgs else "",
            "logo_url": imgs[1] if len(imgs) > 1 else "",
        })
    return out


def ajax_html(commands: list) -> str:
    """Junta el HTML de los comandos `insert` (replaceWith/append/...) de Drupal AJAX."""
    return "".join(c["data"] for c in commands
                   if c.get("command") == "insert" and isinstance(c.get("data"), str))


def fetch_security(fetcher, ds) -> Iterator[dict]:
    category = f"field_categorias_beneficio_target_id[{SECURITY_CATEGORY_ID}]"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{SECURITY_BASE_URL}/beneficios/gourmet",
    }
    seen: set[str] = set()
    for page in range(SECURITY_MAX_PAGES):
        filters = {category: SECURITY_CATEGORY_ID, "combine": "",
                   "sort_bef_combine": "field_vigencia_beneficio_value_DESC", "page": page}
        form = {"view_name": "beneficios", "view_display_id": "block_2", "view_args": "",
                "view_path": "/node/1026", "view_dom_id": "beneficios-scraper", "pager_element": 0,
                **filters}
        url = f"{ds.url}?{urlencode({'_wrapper_format': 'drupal_ajax', **filters})}"
        html_fragment = ajax_html(fetcher.post_json(url, form, headers, timeout=SECURITY_TIMEOUT))
        cards = parse_security_cards(html_fragment)
        new = [c for c in cards if (c["node_id"] or c["detail_url"]) not in seen]
        seen.update(c["node_id"] or c["detail_url"] for c in new)
        log.info("  %s página %d: %d registros (%d nuevos)", ds.url, page, len(cards), len(new))
        yield from new
        has_next = BeautifulSoup(html_fragment, "html.parser").select_one(
            "[data-drupal-views-infinite-scroll-pager] a[rel=next], ul.js-pager__items a[rel=next]")
        if not new or not has_next:
            return
        time.sleep(random.uniform(1, 2))
    log.warning("Security: se alcanzó SECURITY_MAX_PAGES=%d; puede haber más páginas", SECURITY_MAX_PAGES)


# ---------------------------------------------------------- Itaú (FALLBACK HTML)
# itaubeneficios.cl no expone JSON público: se parsea HTML. Si el tema de WordPress
# cambia sus clases CSS (`beneficio__item`, `beneficio__sidebar`, ...), ajustar aquí.

def _fold(text: str) -> str:
    """minúsculas y sin tildes, para comparar palabras clave."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


ITAU_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(_fold(k)) for k in ITAU_RESTAURANT_KEYWORDS) + r")\b")


def is_itau_restaurant(card: dict) -> bool:
    path_category = urlparse(card["url"]).path.strip("/").split("/")[0].replace("-", " ")
    return bool(ITAU_KEYWORD_RE.search(_fold(f"{card['category']} {path_category}")))


def _bg_image(el) -> str:
    m = re.search(r"url\(['\"]?([^'\")]+)", (el.get("style") or "") if el else "")
    return m.group(1) if m else ""


def parse_itau_listing(html_page: str, base_url: str = ITAU_BASE_URL) -> list[dict]:
    """Tarjetas `a.beneficio__item` de una página de listado."""
    soup = BeautifulSoup(html_page, "html.parser")
    cards = []
    for a in soup.select("a.beneficio__item[href]"):
        logo = a.select_one(".beneficio__item__logo img[src]")
        discount = (a.select_one(".beneficio__item__info-discount-only__info")
                    or a.select_one("[class*=info-discount] [class*=__discount]")
                    or a.select_one("[class*=info-discount-text__info]"))
        cards.append({
            "url": urljoin(base_url, a["href"]),
            "title": _text(a.select_one(".beneficio__item__info-location__title")) or a.get("title", ""),
            "address": _text(a.select_one(".beneficio__item__info-location__address")),
            "channel": _text(a.select_one(".beneficio__item__info-location__details")),  # Presencial/Online
            "discount": _text(discount),
            "category": _text(a.select_one(".beneficio__item__category__name")),
            "background_image": _bg_image(a.select_one(".beneficio__item__background")),
            "logo": urljoin(base_url, logo["src"]) if logo else "",
        })
    return cards


def _dmy_to_iso(value: str) -> str | None:
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", value or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def parse_itau_detail(html_page: str) -> dict:
    """Secciones de la página de detalle: "Beneficio", "Descripción", "Restricciones"..."""
    soup = BeautifulSoup(html_page, "html.parser")
    sections = {}
    for block in soup.select(".beneficio__information__texto > div"):
        heading = block.find(["h2", "h3"])
        if heading:
            name = _fold(_text(heading))
            heading.extract()
            sections[name] = clean_html(block.decode_contents())
    body_classes = soup.body.get("class", []) if soup.body else []
    post_id = next((c[len("postid-"):] for c in body_classes if c.startswith("postid-")), "")
    dates = [_text(em) for em in soup.select(".beneficio__sidebar__date em")]
    gallery = soup.select(".beneficio__information__galeria img[src]")
    return {
        "post_id": post_id,
        "caption": _text(soup.select_one(".beneficio__sidebar__caption")),
        "benefit": sections.get("beneficio", ""),
        "description": sections.get("descripcion", ""),
        "restrictions": sections.get("restricciones", ""),
        "valid_from": _dmy_to_iso(dates[0]) if dates else None,
        "valid_to": _dmy_to_iso(dates[1]) if len(dates) > 1 else None,
        "gallery": [img["src"] for img in gallery],
    }


def fetch_itau(fetcher, ds) -> Iterator[dict]:
    cards: dict[str, dict] = {}
    for i, path in enumerate(ITAU_LISTING_PAGES):
        if i:
            time.sleep(random.uniform(1, 2))
        listing = parse_itau_listing(fetcher.get_html(urljoin(ITAU_BASE_URL, path)))
        for card in listing:
            cards.setdefault(card["url"].rstrip("/"), card)
        log.info("  %s%s: %d tarjetas (%d únicas acumuladas)", ITAU_BASE_URL, path, len(listing), len(cards))

    restaurants = [c for c in cards.values() if is_itau_restaurant(c)]
    log.info("  Itaú: %d de %d beneficios son de restaurantes", len(restaurants), len(cards))
    for card in restaurants:
        detail = {}
        if ITAU_FETCH_DETAILS:
            time.sleep(random.uniform(1, 2))
            try:
                detail = parse_itau_detail(fetcher.get_html(card["url"]))
            except Exception as exc:  # noqa: BLE001  se conserva la tarjeta aunque falle el detalle
                log.error("Itaú: no se pudo leer el detalle %s: %r", card["url"], exc)
        yield {**card, "detail": detail}


def norm_itau(c):
    d = c.get("detail") or {}
    description = "\n".join(x for x in (d.get("benefit"), d.get("description")) if x)
    return record(
        "itau", d.get("post_id") or c["url"], c["title"],
        description or " ".join(x for x in (c["discount"], c["channel"]) if x), c["url"],
        c["category"], [c["channel"]] if c["channel"] else [],
        urls(c["background_image"], c["logo"], *d.get("gallery", [])),
        d.get("restrictions", ""), c["title"], c["discount"] or d.get("caption", ""),
        d.get("valid_from"), d.get("valid_to"), location=c["address"])


# ------------------------------------------------------ Scotiabank Ruta Gourmet

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
WEEKDAY_LABELS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS_ES = {m: i for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"], start=1)}


def extract_js_array(page: str, name: str) -> list:
    """Devuelve el arreglo de `const <name> = [...]` del HTML.

    Recorre el texto balanceando corchetes (ignorando los que están dentro de strings), así
    que no depende de que el arreglo termine en `];` ni de saltos de línea. Falla de forma
    explícita (ValueError) si el arreglo no está o no es JSON válido.
    """
    m = re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*\[", page)
    if not m:
        raise ValueError(f"No se encontró el arreglo JS `{name}` en la página")
    start = m.end() - 1
    depth, in_string, escaped = 0, "", False
    for i in range(start, len(page)):
        ch = page[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = ""
        elif ch in "\"'":
            in_string = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(page[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"El arreglo JS `{name}` no es JSON válido: {exc}") from exc
    raise ValueError(f"El arreglo JS `{name}` no está cerrado")


def parse_scotia_days(text: str) -> list[str]:
    """"Todos los lunes" / "Lunes a jueves" / "lunes, martes, sábados y domingo" -> días."""
    folded, day = _fold(text), "(" + "|".join(WEEKDAYS) + ")s?"
    days = {WEEKDAYS.index(m.group(1)) for m in re.finditer(rf"\b{day}\b", folded)}
    for m in re.finditer(rf"\b{day}\s+a\s+{day}\b", folded):  # rangos: "lunes a jueves"
        a, b = WEEKDAYS.index(m.group(1)), WEEKDAYS.index(m.group(2))
        days.update(range(a, b + 1) if a <= b else [*range(a, 7), *range(b + 1)])
    return [WEEKDAY_LABELS[d] for d in sorted(days)]


def parse_scotia_description(value: str) -> dict:
    """`descripcion` trae secciones separadas por "|":
    0 vigencia · 1 descuento · 2 comuna · 3 especificaciones · 4 legal · 5 tarjetas
    (p. ej. "focovisa", "todas") · 6 "pet-friendly"/"x" · 7 ruta especial ("rutatematica")."""
    parts = ["\n".join(line.strip() for line in clean_html(p).splitlines()) for p in (value or "").split("|")]
    part = lambda i: parts[i] if len(parts) > i else ""
    # Fechas "30 de septiembre de 2026" en la vigencia y en el texto legal. Se usa la más tardía:
    # hay fichas cuya línea de vigencia quedó desactualizada respecto del legal (que es el vinculante).
    dates = []
    for m in re.finditer(r"(\d{1,2})\s+de\s+([a-z]+)\s+(?:del?\s+)?(\d{4})", _fold(f"{part(0)} {part(4)}")):
        if month := MONTHS_ES.get(m.group(2)):
            dates.append(f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}")
    valid_to = max(dates) if dates else None
    return {
        "validity": part(0), "valid_to": valid_to, "discount": part(1), "comuna": part(2),
        "specifications": part(3), "legal": part(4), "cards": part(5),
        "pet_friendly": _fold(part(6)) == "pet-friendly", "special_route": part(7),
    }


def _scotia_url(value: str) -> str:
    value = (value or "").replace("\xa0", " ").strip()
    return urljoin(SCOTIA_BASE_URL + "/", value) if value else ""


def fetch_scotia(fetcher, ds) -> Iterator[dict]:
    page = fetcher.get_html(ds.url)
    for name in SCOTIA_ARRAYS:
        items = extract_js_array(page, name)
        log.info("  %s `%s`: %d restaurantes", ds.url, name, len(items))
        for item in items:
            yield {**item, "_array": name}


def norm_scotia(s):
    d = parse_scotia_description(s.get("descripcion", ""))
    days = parse_scotia_days(s.get("especialidad", ""))
    pct = re.search(r"(\d{1,3})\s*%", s.get("telefono") or "")
    region = CHILE_REGIONS.get(s.get("id_region"), str(s.get("id_region") or ""))
    addresses = [a.strip() for a in (s.get("direccion") or "").split("|") if a.strip()]
    tags = [*days, region, d["comuna"],
            "Pet friendly" if d["pet_friendly"] else "", "Ruta temática" if d["special_route"] else ""]
    return record(
        "scotia", s["id_sitio"], (s.get("nombre") or "").strip(), d["specifications"],
        _scotia_url(s.get("web")) or SCOTIA_URL, "Ruta Gourmet", tags, urls(_scotia_url(s.get("imagen"))),
        d["legal"], s.get("nombre"), f"{pct.group(1)}%" if pct else (s.get("telefono") or "").strip(),
        None, d["valid_to"], location=" | ".join(addresses + [region]),
        days=", ".join(days) or (s.get("especialidad") or "").strip())


# ------------------------------------------------------------------- datasets

MODYO_CHILE = "https://sitiospublicos.bancochile.cl/api/content/spaces/personas/types/{type}/entries"


@dataclass
class Dataset:
    site: str          # valor de --sites
    filename: str
    url: str
    normalize: Callable[[dict], dict]
    fetch: Callable[[Fetcher, Dataset], Iterator[dict]]


def modyo(f, ds):
    return paginate(f, ds.url, page_param="page", size_param="per_page", size=100,
                    items=lambda d: d.get("entries"))


DATASETS = [
    Dataset("bice", "bice_beneficios.json",
            "https://banco.bice.cl/api/content/spaces/beneficios-bice/types/beneficios/entries",
            norm_bice, modyo),
    Dataset("bci", "bci_beneficios.json",
            "https://api.bciplus.cl/bff-loyalty-beneficios/v1/offers", norm_bci,
            lambda f, ds: paginate(f, ds.url, page_param="pagina", size_param="itemsPorPagina", size=100,
                                   items=lambda d: d.get("ofertas"),
                                   headers={"Ocp-Apim-Subscription-Key": BCI_SUBSCRIPTION_KEY,
                                            "Origin": "https://www.bci.cl", "Referer": "https://www.bci.cl/"})),
    Dataset("bancochile", "bancochile_personas_beneficios.json",
            MODYO_CHILE.format(type="beneficios"), norm_bancochile_beneficio, modyo),
    Dataset("bancochile", "bancochile_seguros.json",
            MODYO_CHILE.format(type="seguro"), norm_bancochile_seguro, modyo),
    Dataset("santander", "santander_promociones.json",
            "https://banco.santander.cl/beneficios/promociones.json", norm_santander,
            lambda f, ds: paginate(f, ds.url, page_param="page", size_param="per_page", size=500,
                                   items=lambda d: d.get("promociones"))),
    Dataset("security", "security_gourmet_restaurantes.json",
            f"{SECURITY_BASE_URL}/views/ajax", norm_security, fetch_security),
    Dataset("itau", "bancoitau_restaurantes.json",
            ", ".join(urljoin(ITAU_BASE_URL, p) for p in ITAU_LISTING_PAGES), norm_itau, fetch_itau),
    Dataset("scotia", "scotia_ruta_gourmet.json", SCOTIA_URL, norm_scotia, fetch_scotia),
]
SITES = sorted({d.site for d in DATASETS})


# ------------------------------------------------------------------------ main

def setup_logging(out: Path):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    err = logging.FileHandler(out / "errors.log", encoding="utf-8")
    err.setLevel(logging.ERROR)
    err.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(err)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("./output"))
    ap.add_argument("--sites", default="all",
                    help=f"'all' o lista separada por comas de: {', '.join(SITES)}")
    ap.add_argument("--raw", action="store_true", help="guardar también la respuesta cruda en <out>/raw/")
    args = ap.parse_args()

    wanted = set(SITES) if args.sites == "all" else {s.strip() for s in args.sites.split(",")}
    if unknown := wanted - set(SITES):
        ap.error(f"sitios desconocidos: {', '.join(sorted(unknown))}")
    args.out.mkdir(parents=True, exist_ok=True)
    setup_logging(args.out)

    started, t0 = datetime.now(timezone.utc), time.monotonic()
    fetcher = Fetcher()
    manifest = {"run_started": started.isoformat(), "datasets": []}

    for ds in (d for d in DATASETS if d.site in wanted):
        log.info("%s -> %s", ds.site, ds.filename)
        entry = {"site": ds.site, "file": ds.filename, "source_url": ds.url, "count": 0, "error": None}
        try:
            raw = list(ds.fetch(fetcher, ds))
            rows = []
            for item in raw:
                try:
                    rows.append(ds.normalize(item))
                except Exception as exc:  # noqa: BLE001  un registro raro no debe botar el dataset completo
                    log.error("%s: no se pudo normalizar un registro: %r", ds.filename, exc)
            (args.out / ds.filename).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.raw:
                (args.out / "raw").mkdir(exist_ok=True)
                (args.out / "raw" / ds.filename).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            entry["count"] = len(rows)
        except Exception as exc:  # noqa: BLE001  un sitio caído no debe detener a los demás
            entry["error"] = f"{type(exc).__name__}: {exc}"
            log.error("%s (%s) falló: %s", ds.filename, ds.url, entry["error"])
        manifest["datasets"].append(entry)

    elapsed = time.monotonic() - t0
    manifest.update(run_finished=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(elapsed, 1),
                    total_records=sum(d["count"] for d in manifest["datasets"]))
    (args.out / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nResumen")
    print("-" * 60)
    for d in manifest["datasets"]:
        status = f"ERROR ({d['error']})" if d["error"] else f"{d['count']:>5} registros"
        print(f"{d['site']:<11} {d['file']:<38} {status}")
    print("-" * 60)
    print(f"Total: {manifest['total_records']} registros en {elapsed:.1f}s -> {args.out.resolve()}")
    return 1 if any(d["error"] for d in manifest["datasets"]) else 0


if __name__ == "__main__":
    sys.exit(main())
