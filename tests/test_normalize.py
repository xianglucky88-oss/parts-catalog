from decimal import Decimal
from parts_catalog.normalize import normalize_part, to_catalog_part


def test_aliases_and_model_key_are_normalized():
    part = normalize_part({"supplier": "S", "part_number": "ab-120 / 4", "brand": "Acme Inc.", "description": "  Bearing  20 mm ", "package_quantity": 1, "price": "18.50", "stock_status": "In Stock"})
    assert part.part_number == "AB1204"
    assert part.brand == "ACME"
    assert part.price == Decimal("18.50")
    assert to_catalog_part(part).canonical_number == "AB1204"
