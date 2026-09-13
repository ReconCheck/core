from conftest import make_table
from reconcheck.align import align
from reconcheck.align.units import convert  # noqa: F401  (sanity import check)
from reconcheck.models import Cell


def test_align_matches_by_normalised_part_no():
    left = make_table(
        ["line", "part", "qty"],
        [
            {"line": "1", "part": "A-0012", "qty": "100"},
            {"line": "2", "part": "B0034", "qty": "200"},
        ],
    )
    right = make_table(
        ["line", "part", "qty"],
        [
            {"line": "1", "part": "A012", "qty": "100"},
            {"line": "3", "part": "B034", "qty": "199"},
        ],
    )
    pairs = align(left, right, match_on=["part"], normalize={"part": "part_no"})
    assert [p.key for p in pairs] == ["a12", "b34"]
    left_qty: Cell = pairs[0].left.by_header("qty")
    assert left_qty.value == 100


def test_align_multi_column_key():
    left = make_table(
        ["site", "part"],
        [{"site": "A", "part": "X-1"}, {"site": "A", "part": "X-2"}],
    )
    right = make_table(
        ["site", "part"],
        [{"site": "A", "part": "X1"}, {"site": "B", "part": "X2"}],
    )
    pairs = align(left, right, match_on=["site", "part"], normalize={"part": "part_no"})
    assert [p.key for p in pairs] == ["A\x1fx1"]


def test_align_no_shared_keys():
    left = make_table(["id"], [{"id": "1"}])
    right = make_table(["id"], [{"id": "2"}])
    assert align(left, right, match_on=["id"]) == []


def test_align_defaults_to_first_common_header():
    left = make_table(["id", "v"], [{"id": "1", "v": "10"}])
    right = make_table(["id", "v"], [{"id": "1", "v": "11"}])
    pairs = align(left, right)
    assert len(pairs) == 1
    assert pairs[0].left.by_header("v").value == 10
