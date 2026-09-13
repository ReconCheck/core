from reconcheck.align.keys import normalize_entity, normalize_key, normalize_part_no


def test_part_no_leading_zeros():
    assert normalize_part_no("0012") == normalize_part_no("12")


def test_part_no_separators_and_case():
    assert normalize_part_no("A-012") == "a12"
    assert normalize_part_no("A 012") == "a12"
    assert normalize_part_no("A.012") == "a12"


def test_entity_suffix_english():
    assert normalize_entity("ACME Inc.") == "acme"
    assert normalize_entity("Acme Corporation") == "acme"


def test_entity_suffix_chinese():
    assert normalize_entity("深圳市华加生物科技有限公司") == "深圳市华加生物科技"


def test_entity_keeps_short_names():
    assert normalize_entity("华加") == "华加"


def test_normalize_key_dispatch():
    assert normalize_key("A-012", "part_no") == "a12"
    assert normalize_key("深圳市华加生物科技有限公司", "entity") == "深圳市华加生物科技"
    assert normalize_key("  raw  ", "raw") == "raw"
