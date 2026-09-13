from decimal import Decimal

from reconcheck.align.units import canonical, convert, convertible_pair


def test_canonical_aliases():
    assert canonical("KG") == "kg"
    assert canonical("千克") == "kg"
    assert canonical("pcs.") == "pcs"
    assert canonical("箱子") is None


def test_convert_mass():
    assert convert(Decimal("5000"), "g", "kg") == Decimal("5")
    assert convert(Decimal("2"), "t", "kg") == Decimal("2000")


def test_convert_identical_units():
    assert convert(Decimal("3"), "kg", "kg") == Decimal("3")


def test_convert_unknown_returns_none():
    assert convert(Decimal("3"), "m", "kg") is None
    assert convert(Decimal("3"), None, "kg") is None


def test_convertible_pair():
    assert convertible_pair("kg", "g")
    assert convertible_pair("千克", "公斤")
    assert convertible_pair("pcs", "pcs")
    assert not convertible_pair("pcs", "box")  # count units are opaque without a pack size
    assert not convertible_pair("kg", "pcs")
