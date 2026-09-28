import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_benefits as fb


def promo(description):
    return {"id": 1, "title": "X", "url": "https://banco.santander.cl/beneficios/promociones/x",
            "description": description, "tags": ["cat-descuentos"], "covers": []}


@pytest.mark.parametrize(("description", "expected"), [
    ("<p>20% dcto. sobre el total</p>", "20%"),
    ("<p>- No acumulable.CAE 1,51% Calculado por un monto referencial</p>", ""),  # tasa, no descuento
    ("<p>- 1,51% Calculado por un monto referencial</p>", ""),                    # decimal sin "CAE"
    ("<p>CAE 1,46%. - 20% dcto. sobre el total</p>", "20%"),                       # tasa + descuento real
    ("<p>tasa de interés 0,99% mensual</p>", ""),
    ("<p>Paga en 3 cuotas sin interés</p>", ""),
])
def test_santander_discount_ignores_interest_rates(description, expected):
    assert fb.norm_santander(promo(description))["discount"] == expected
