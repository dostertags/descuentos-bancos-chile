import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_benefits as fb

DESCRIPCION = (
    "<p>V&aacute;lido hasta el 30&nbsp;de septiembre&nbsp;de 2026.|50% Dcto.|Vitacura</p>\n\n<p>|</p>\n\n"
    "<ul>\n\t<li>Pagando con tus Tarjetas&nbsp;de Cr&eacute;dito Visa Scotiabank Signature.</li>\n"
    "\t<li>Tope de descuento: $50.000 por cuenta y cliente titular.</li>\n</ul>\n\n"
    "<p>|<br />\nPromoci&oacute;n v&aacute;lida todos los lunes hasta el 30 de septiembre de 2026.</p>"
    "|focovisa|pet-friendly|rutatematica"
)

CARNAL = {
    "nombre": "Carnal ", "direccion": "Alonso de Córdova 3053, Vitacura", "telefono": "50% Dcto.",
    "web": "https://carnalprime.cl/", "especialidad": "Todos los lunes",
    "imagen": "/resource/images/CARNAL-RF1788194320.jpg", "descripcion": DESCRIPCION,
    "id_sitio": 519, "id_region": 13,
}
NONNA = {
    "nombre": "La Pasta De La Nonna - Concepción", "direccion": "Caupolicán 255, Concepción",
    "telefono": "25% Dcto.", "web": " https://pastadelanonna.cl/", "especialidad": "Lunes a jueves",
    "imagen": "/resource/banner/la nonna.jpg",
    "descripcion": "<p>Válido hasta el 30 de junio de 2026.|25% Dcto.|Concepción|<ul><li>x</li></ul>|"
                   "Promoción válida hasta el 30 de septiembre de 2026.|todas|x</p>",
    "id_sitio": 600, "id_region": 8,
}


def scotia_page(santiago: list, regiones: list, *, drop: str = "") -> str:
    """Imita el <script> real: JSON con escapes \\/ y \\u00F3, un `];` dentro de un string."""
    arrays = {"sitiosSantiago": santiago, "sitiosRegiones": regiones}
    lines = [f"\tconst {name} = {json.dumps(value).replace('/', chr(92) + '/')};"
             for name, value in arrays.items() if name != drop]
    return ("<html><head><script>var otra = ['no];'];</script></head><body><script>\n"
            + "\n".join(lines)
            + "\n\tconst sitiosTotales = sitiosSantiago.concat(sitiosRegiones);\n</script></body></html>")


# --------------------------------------------------------------- extracción

def test_extract_js_array_parses_both_arrays():
    page = scotia_page([CARNAL], [NONNA])
    assert fb.extract_js_array(page, "sitiosSantiago") == [CARNAL]
    assert fb.extract_js_array(page, "sitiosRegiones") == [NONNA]


def test_extract_js_array_ignores_brackets_inside_strings():
    tricky = {**CARNAL, "nombre": "Bar ] [ ];", "descripcion": 'dice "hola" y [nada]'}
    assert fb.extract_js_array(scotia_page([tricky], []), "sitiosSantiago") == [tricky]


def test_extract_js_array_fails_explicitly_when_missing():
    with pytest.raises(ValueError, match="sitiosRegiones"):
        fb.extract_js_array(scotia_page([CARNAL], [NONNA], drop="sitiosRegiones"), "sitiosRegiones")


def test_extract_js_array_fails_on_unclosed_array():
    with pytest.raises(ValueError, match="no está cerrado"):
        fb.extract_js_array("<script>const sitiosSantiago = [{\"a\": 1}, </script>", "sitiosSantiago")


# ------------------------------------------------------------ normalización

@pytest.mark.parametrize(("text", "expected"), [
    ("Todos los lunes", ["Lunes"]),
    ("Todos los Jueves ", ["Jueves"]),
    ("lunes, martes, sábados y domingo", ["Lunes", "Martes", "Sábado", "Domingo"]),
    ("Lunes a jueves", ["Lunes", "Martes", "Miércoles", "Jueves"]),
    ("De lunes a miércoles y viernes ", ["Lunes", "Martes", "Miércoles", "Viernes"]),
    ("martes a domingo", ["Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]),
    ("", []),
])
def test_parse_days(text, expected):
    assert fb.parse_scotia_days(text) == expected


def test_parse_description_sections():
    d = fb.parse_scotia_description(DESCRIPCION)
    assert d["validity"] == "Válido hasta el 30 de septiembre de 2026."
    assert d["discount"] == "50% Dcto." and d["comuna"] == "Vitacura"
    assert d["specifications"].startswith("- Pagando con tus Tarjetas de Crédito")
    assert "\t" not in d["specifications"]
    assert d["legal"].startswith("Promoción válida todos los lunes")
    assert d["cards"] == "focovisa" and d["pet_friendly"] is True and d["special_route"] == "rutatematica"
    assert d["valid_to"] == "2026-09-30"


def test_valid_to_uses_latest_date_when_summary_is_stale():
    assert fb.parse_scotia_description(NONNA["descripcion"])["valid_to"] == "2026-09-30"


def test_norm_scotia_maps_to_unified_schema():
    r = fb.norm_scotia(CARNAL)
    assert r["source"] == "scotia" and r["id"] == "519"
    assert r["title"] == "Carnal" and r["merchant"] == "Carnal"
    assert r["discount"] == "50%"
    assert r["url"] == "https://carnalprime.cl/"
    assert r["images"] == ["https://www.scotiarewards.cl/resource/images/CARNAL-RF1788194320.jpg"]
    assert r["category"] == "Ruta Gourmet"
    assert r["tags"] == ["Lunes", "Metropolitana de Santiago", "Vitacura", "Pet friendly", "Ruta temática"]
    assert r["days"] == "Lunes"
    assert r["location"] == "Alonso de Córdova 3053, Vitacura | Metropolitana de Santiago"
    assert r["valid_to"].startswith("2026-09-30")
    assert r["conditions"].startswith("Promoción válida")


def test_norm_scotia_cleans_urls_and_handles_missing_web():
    r = fb.norm_scotia(NONNA)
    assert r["url"] == "https://pastadelanonna.cl/"          # NBSP inicial eliminado
    assert r["images"] == ["https://www.scotiarewards.cl/resource/banner/la nonna.jpg"]
    assert "Biobío" in r["tags"] and r["discount"] == "25%"
    assert fb.norm_scotia({**CARNAL, "web": ""})["url"] == fb.SCOTIA_URL


def test_norm_scotia_multiple_addresses_and_unknown_region():
    r = fb.norm_scotia({**CARNAL, "direccion": "Local 1 | Local 2", "id_region": 99})
    assert r["location"] == "Local 1 | Local 2 | 99"


# ------------------------------------------------------------- integración

class FakeFetcher:
    def __init__(self, page: str):
        self.page, self.calls = page, []

    def get_html(self, url, timeout=60):
        self.calls.append(url)
        return self.page


def scotia_dataset():
    return next(d for d in fb.DATASETS if d.site == "scotia")


def test_scotia_registered_and_runs():
    assert "scotia" in fb.SITES
    ds = scotia_dataset()
    assert ds.filename == "scotia_ruta_gourmet.json" and ds.url == fb.SCOTIA_URL
    fetcher = FakeFetcher(scotia_page([CARNAL], [NONNA]))
    rows = [ds.normalize(x) for x in ds.fetch(fetcher, ds)]
    assert fetcher.calls == [fb.SCOTIA_URL]
    assert [r["id"] for r in rows] == ["519", "600"]


def test_scotia_fails_explicitly_if_array_disappears():
    ds = scotia_dataset()
    with pytest.raises(ValueError, match="sitiosSantiago"):
        list(ds.fetch(FakeFetcher("<html>rediseño sin arreglos</html>"), ds))


# --------------------------------------------------------------- live smoke

@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to hit the real site")
def test_live_scotia_smoke():
    ds = scotia_dataset()
    rows = [ds.normalize(x) for x in ds.fetch(fb.Fetcher(), ds)]
    assert len(rows) >= 10
    assert len({r["id"] for r in rows}) == len(rows)
    assert all(r["discount"].endswith("%") for r in rows)
