import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_benefits as fb

CARD = """
<article data-history-node-id="{nid}">
 <a class="coh-container card-container item-card" href="https://personas.bancosecurity.cl/beneficios/gourmet/{slug}">
  <div class="coh-container coh-ce-cc542e3e">
   <span class="coh-inline-element coh-ce-c1e71a37">Gourmet</span>
   <div><img src="/sites/bancopersonas/files/main-{slug}.png.webp?itok=a"></div>
   <span class="coh-inline-element coh-ce-b2fcfbe4"><div><img src="/sites/bancopersonas/files/logo-{slug}.png.webp"></div></span>
   <span class="coh-inline-element beneficio-label"><div>{discount}</div></span>
  </div>
  <div class="coh-container data-container coh-ce-87ca5438">
   <div>{title}</div>
   <div class="coh-container description"><div>Los viernes, exclusivo en tienda física con tu Tarjeta One.</div></div>
   <div class="coh-container"><i class="coh-inline-element las la-map-marker"></i> Vitacura.</div>
   <div class="coh-container"><i class="coh-inline-element las la-calendar-day"></i> {days}</div>
   <div>{discount}</div>
  </div>
 </a>
</article>"""

PAGER = ('<ul class="js-pager__items pager" data-drupal-views-infinite-scroll-pager="">'
         '<li class="pager__item"><a class="button" href="?page={next}" rel="next">Cargar más</a></li></ul>')


def ajax_response(cards: str, next_page: int | None) -> list:
    html = f'<div class="views-infinite-scroll-content-wrapper">{cards}</div>'
    if next_page is not None:
        html += PAGER.format(next=next_page)
    return [
        {"command": "settings", "settings": {}, "merge": True},
        {"command": "insert", "method": "replaceWith", "selector": ".js-view-dom-id-x", "data": html},
        {"command": "insert", "method": "prepend", "selector": ".js-view-dom-id-x", "data": ""},
    ]


def card(nid, slug, title, discount="40% de descuento", days="Viernes"):
    return CARD.format(nid=nid, slug=slug, title=title, discount=discount, days=days)


# ------------------------------------------------------------------ unit tests

def test_parse_card_fields():
    [c] = fb.parse_security_cards(card(5636, "la-dicha-0", "La Dicha"))
    assert c == {
        "node_id": "5636",
        "detail_url": "https://personas.bancosecurity.cl/beneficios/gourmet/la-dicha-0",
        "title": "La Dicha",
        "description": "Los viernes, exclusivo en tienda física con tu Tarjeta One.",
        "discount_text": "40% de descuento",
        "discount_percent": 40.0,
        "location": "Vitacura",
        "days": "Viernes",
        "category_label": "Gourmet",
        "main_image_url": "https://personas.bancosecurity.cl/sites/bancopersonas/files/main-la-dicha-0.png.webp?itok=a",
        "logo_url": "https://personas.bancosecurity.cl/sites/bancopersonas/files/logo-la-dicha-0.png.webp",
    }


def test_parse_discount_without_percent_and_missing_icons():
    html = card(1, "x", "Sin porcentaje", discount="2x1 en cócteles").replace("la-map-marker", "otro-icono")
    [c] = fb.parse_security_cards(html)
    assert c["discount_text"] == "2x1 en cócteles"
    assert c["discount_percent"] is None
    assert c["location"] == ""


def test_ajax_html_joins_insert_commands_only():
    cmds = ajax_response(card(1, "a", "A"), next_page=1)
    assert "data-history-node-id" in fb.ajax_html(cmds)
    assert fb.ajax_html([{"command": "settings", "data": "<p>no</p>"}]) == ""


def test_norm_security_maps_to_unified_schema():
    [c] = fb.parse_security_cards(card(5636, "la-dicha-0", "La Dicha", days="Jueves y Viernes"))
    r = fb.norm_security(c)
    assert r["source"] == "security" and r["id"] == "5636"
    assert r["discount"] == "40%"
    assert r["category"] == "Gourmet"
    assert r["tags"] == ["Restaurantes", "Jueves", "Viernes"]
    assert r["location"] == "Vitacura" and r["days"] == "Jueves y Viernes"
    assert len(r["images"]) == 2


# ----------------------------------------------------------- integration test

class FakeFetcher:
    """Devuelve respuestas AJAX fijas según el `page` del query string."""

    def __init__(self, pages: dict[int, list]):
        self.pages, self.calls = pages, []

    def post_json(self, url, data, headers=None, timeout=60):
        query = parse_qs(urlparse(url).query)
        self.calls.append((query, data, headers))
        return self.pages[int(query["page"][0])]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)


def security_dataset():
    return next(d for d in fb.DATASETS if d.site == "security")


def test_fetch_paginates_until_no_pager_and_dedupes():
    fetcher = FakeFetcher({
        0: ajax_response(card(1, "a", "A") + card(2, "b", "B"), next_page=1),
        1: ajax_response(card(2, "b", "B") + card(3, "c", "C"), next_page=None),  # 2 repetido
    })
    rows = list(fb.fetch_security(fetcher, security_dataset()))

    assert [r["node_id"] for r in rows] == ["1", "2", "3"]
    assert len(fetcher.calls) == 2
    query, form, headers = fetcher.calls[1]
    # Drupal ignora filtro y página si sólo van en el cuerpo: deben ir en el query string.
    assert query["page"] == ["1"]
    assert query["field_categorias_beneficio_target_id[121]"] == ["121"]
    assert query["_wrapper_format"] == ["drupal_ajax"]
    assert form["view_name"] == "beneficios" and form["view_display_id"] == "block_2"
    assert headers["X-Requested-With"] == "XMLHttpRequest"


def test_fetch_stops_when_page_has_no_new_cards():
    fetcher = FakeFetcher({
        0: ajax_response(card(1, "a", "A"), next_page=1),
        1: ajax_response(card(1, "a", "A"), next_page=2),  # el sitio repite la página
    })
    assert [r["node_id"] for r in fb.fetch_security(fetcher, security_dataset())] == ["1"]
    assert len(fetcher.calls) == 2


def test_security_registered_in_cli():
    assert "security" in fb.SITES


# --------------------------------------------------------------- live smoke

@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to hit the real site")
def test_live_security_smoke():
    rows = [fb.norm_security(c) for c in fb.fetch_security(fb.Fetcher(), security_dataset())]
    assert len(rows) >= 5
    assert all(r["url"].startswith(fb.SECURITY_BASE_URL) and r["title"] for r in rows)
    assert len({r["id"] for r in rows}) == len(rows)
