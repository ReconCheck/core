"""Unit canonicalisation and conversion (mass / length; count units are opaque).

``"5 kg"`` and ``"5000 g"`` are the same quantity but different unit strings —
the judge stage needs to know that before deciding a difference is real.
"""

from __future__ import annotations

from decimal import Decimal

UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "kg": ("kg", "千克", "公斤", "kgs"),
    "g": ("g", "克", "gm", "gram", "grams"),
    "t": ("t", "ton", "tons", "吨"),
    "m": ("m", "米", "metre", "metres", "meter", "meters"),
    "cm": ("cm", "厘米"),
    "mm": ("mm", "毫米"),
    "pcs": ("pcs", "pc", "pce", "pces", "pcs.", "ea", "个", "件", "只"),
    "box": ("box", "boxes", "箱", "盒"),
    "set": ("set", "sets", "套"),
}

_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _aliases in UNIT_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_CANON[_alias] = _canon

# Conversion factors expressed in grams / millimetres. Units may only convert
# within their family: mass (kg/g/t) or length (m/cm/mm). Count units (pcs,
# box, set) are opaque without a pack size and only convert to themselves.
_FACTORS: dict[str, Decimal] = {
    "kg": Decimal("1000"),
    "g": Decimal("1"),
    "t": Decimal("1000000"),
    "m": Decimal("100"),
    "cm": Decimal("1"),
    "mm": Decimal("0.1"),
}

_FAMILIES: dict[str, frozenset[str]] = {
    "mass": frozenset({"kg", "g", "t"}),
    "length": frozenset({"m", "cm", "mm"}),
}


def _family(unit: str) -> str | None:
    for family, members in _FAMILIES.items():
        if unit in members:
            return family
    return None


def canonical(unit: str | None) -> str | None:
    """Map an alias to its canonical unit, or None when unknown."""
    if not unit:
        return None
    return _ALIAS_TO_CANON.get(unit.strip().lower())


def convertible_pair(a: str | None, b: str | None) -> bool:
    """True when the two unit strings are the same or share a numeric family."""
    ca, cb = canonical(a), canonical(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    fa, fb = _family(ca), _family(cb)
    return bool(fa and fb and fa == fb)


def convert(value: Decimal, from_unit: str | None, to_unit: str | None) -> Decimal | None:
    """Convert ``value`` between canonical units; None when not possible."""
    if not from_unit or not to_unit:
        return None
    cf, ct = canonical(from_unit), canonical(to_unit)
    if not cf or not ct:
        return None
    if cf == ct:
        return value
    fa, fb = _family(cf), _family(ct)
    if not fa or not fb or fa != fb:
        return None
    if cf in _FACTORS and ct in _FACTORS:
        return value * _FACTORS[cf] / _FACTORS[ct]
    return None
