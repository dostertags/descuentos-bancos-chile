import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_benefits as fb

BASE = "https://itaubeneficios.cl"

CARD = """
<a alt="{title}" class="page-beneficios-list-default__grid__item beneficio__item" href="{base}/{path}/" title="{title}">
 <div class="beneficio__item__content">
  <div class="beneficio__item__background" style="background-image: url({base}/wp-content/uploads/bg-{slug}.jpg); background-repeat: no-repeat;"></div>
  <div class="beneficio__item__logo" style="background-color: #ffffff;"><img src="{base}/wp-content/uploads/logo-{slug}.jpg"/></div>
  <div class="beneficio__item__info-location">
   <h2 class="beneficio__item__info-location__title">{title}</h2>
   <p class="beneficio__item__info-location__address">Vitacura, RM</p>
   <p class="beneficio__item__info-location__details">Presencial</p>
  </div>
  <div class="beneficio__item__info-discount-only"><p class="beneficio__item__info-discount-only__info">40%</p></div>
 </div>
 <div class="beneficio__item__category"><p class="beneficio__item__category__name">{category}</p></div>
</a>"""

DETAIL = """<html><body class="single single-post postid-60328">
<div class="beneficio__sidebar">
 <div class="beneficio__sidebar__caption"><p>40% de dcto.</p></div>
 <div class="beneficio__sidebar__date"><span>Válido desde <em>01-09-2026</em></span><span>hasta el <em>30-09-2026</em></span></div>
</div>
<div class="beneficio__information__texto">
 <div class="beneficio__information__texto__block-1"><h2>Beneficio</h2>
  <p><strong>40% de descuento todos los viernes y sábados</strong></p>
  <p><strong>Condiciones:</strong><br/>Válido exclusivamente para consumo en el local.</p></div>
 <div class="beneficio__information__texto__block-2"><h2>Descripción</h2><p>Sushi Nikkei</p></div>
 <div class="beneficio__information__texto__list-services"><h2>Servicios</h2><ul></ul></div>
 <div class="beneficio__information__texto__block-2"><h2>Restricciones</h2><p>No acumulable. <strong>Excluye propinas.</strong></p></div>
</div>
<div class="beneficio__information__galeria"><img src="{base}/wp-content/uploads/foto_1.jpg"/></div>
</body></html>""".replace("{base}", BASE)


def card(title, category, path, slug="x"):
    return CARD.format(base=BASE, title=title, category=category, path=path, slug=slug)


def page(*cards):
    return f"<html><body><div class='page-beneficios-list-default__grid'>{''.join(cards)}</div></body></html>"


# ------------------------------------------------------------------ unit tests

def test_parse_listing_card():
    [c] = fb.parse_itau_listing(page(card("Doga Nikkei", "Restaurantes", "restaurantes/doga-nikkei-2", "doga")))
    assert c == {
        "url": f"{BASE}/restaurantes/doga-nikkei-2/",
        "title": "Doga Nikkei",
        "address": "Vitacura, RM",
        "channel": "Presencial",
        "discount": "40%",
        "category": "Restaurantes",
        "background_image": f"{BASE}/wp-content/uploads/bg-doga.jpg",
        "logo": f"{BASE}/wp-content/uploads/logo-doga.jpg",
    }


def test_parse_detail_sections_dates_and_id():
    d = fb.parse_itau_detail(DETAIL)
    assert d["post_id"] == "60328"
    assert d["caption"] == "40% de dcto."
    assert d["benefit"].startswith("40% de descuento todos los viernes y sábados")
    assert "Condiciones:" in d["benefit"]
    assert d["description"] == "Sushi Nikkei"
    assert d["restrictions"] == "No acumulable. Excluye propinas."
    assert (d["valid_from"], d["valid_to"]) == ("2026-09-01", "2026-09-30")  # dd-mm-yyyy, no mm-dd
    assert d["gallery"] == [f"{BASE}/wp-content/uploads/foto_1.jpg"]


@pytest.mark.parametrize(("category", "path", "expected"), [
    ("Restaurantes", "restaurantes/a", True),
    ("Martes Gourmet", "martes-gourmet/a", True),
    ("Miércoles Gourmet", "miercoles-gourmet/a", True),
    ("De compras", "de-compras/barbacoa-store", False),   # "bar" sólo como palabra completa
    ("Salud y belleza", "belleza-y-salud/a", False),
    ("", "restaurantes/sin-categoria", True),             # respaldo: segmento de la URL
])
def test_restaurant_filter(category, path, expected):
    c = {"category": category, "url": f"{BASE}/{path}/"}
    assert fb.is_itau_restaurant(c) is expected


def test_norm_itau_maps_to_unified_schema():
    [c] = fb.parse_itau_listing(page(card("Doga Nikkei", "Restaurantes", "restaurantes/doga-nikkei-2", "doga")))
    r = fb.norm_itau({**c, "detail": fb.parse_itau_detail(DETAIL)})
    assert r["source"] == "itau" and r["id"] == "60328"
    assert r["url"] == f"{BASE}/restaurantes/doga-nikkei-2/"
    assert r["category"] == "Restaurantes" and r["tags"] == ["Presencial"]
    assert r["description"].endswith("Sushi Nikkei")
    assert r["conditions"] == "No acumulable. Excluye propinas."
    assert r["discount"] == "40%" and r["location"] == "Vitacura, RM"
    assert r["valid_to"].startswith("2026-09-30")
    assert len(r["images"]) == 3


def test_norm_itau_without_detail_uses_card_only():
    [c] = fb.parse_itau_listing(page(card("Papa Johns", "Restaurantes", "restaurantes/papa-johns")))
    r = fb.norm_itau({**c, "detail": {}})
    assert r["id"] == f"{BASE}/restaurantes/papa-johns/"
    assert r["description"] == "40% Presencial"


# ----------------------------------------------------------- integration test

class FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.calls = pages, []

    def get_html(self, url, timeout=60):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"HTTP 500 en {url}")
        return self.pages[url]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)


def itau_dataset():
    return next(d for d in fb.DATASETS if d.site == "itau")


def test_fetch_merges_listings_filters_and_survives_detail_error(monkeypatch):
    monkeypatch.setattr(fb, "ITAU_LISTING_PAGES", ["/beneficios/", "/restaurantes/"])
    fetcher = FakeFetcher({
        f"{BASE}/beneficios/": page(card("Doga", "Restaurantes", "restaurantes/doga"),
                                     card("Casaideas", "De compras", "de-compras/casaideas")),
        f"{BASE}/restaurantes/": page(card("Doga", "Restaurantes", "restaurantes/doga"),       # repetido
                                       card("Sicily", "Miércoles Gourmet", "miercoles-gourmet/sicily"),
                                       card("Cabify", "Viajes", "viajes/cabify")),              # destacado ajeno
        f"{BASE}/restaurantes/doga/": DETAIL,
        # sin detalle para Sicily -> error registrado, se conserva la tarjeta
    })
    rows = [fb.norm_itau(r) for r in fb.fetch_itau(fetcher, itau_dataset())]

    assert [r["title"] for r in rows] == ["Doga", "Sicily"]
    assert rows[0]["id"] == "60328" and rows[0]["conditions"]
    assert rows[1]["id"] == f"{BASE}/miercoles-gourmet/sicily/" and rows[1]["conditions"] == ""
    assert f"{BASE}/de-compras/casaideas/" not in fetcher.calls  # no se piden detalles de no-restaurantes


def test_itau_registered_in_cli():
    assert "itau" in fb.SITES
    assert itau_dataset().filename == "bancoitau_restaurantes.json"


# --------------------------------------------------------------- live smoke

@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to hit the real site")
def test_live_itau_listing_smoke():
    html_page = fb.Fetcher().get_html(f"{BASE}/restaurantes/")
    cards = fb.parse_itau_listing(html_page)
    assert any(fb.is_itau_restaurant(c) for c in cards)
