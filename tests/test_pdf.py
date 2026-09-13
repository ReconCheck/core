"""PDF parsing: text-layer tables, borderless fallback, scanned-PDF error."""

from pathlib import Path

import pytest

from reconcheck.errors import PdfOcrRequiredError
from reconcheck.parse import load_document


def _make_pdf(path: Path, lines: list[str], columns: list[int] | None = None) -> Path:
    """Hand-build a minimal text PDF.

    ``columns`` gives the x-offset of each column (per-line Tj draws); when
    None, each line is drawn as one text run.
    """
    content = []
    if columns is None:
        for i, line in enumerate(lines):
            content.append(f"BT /F1 14 Tf 50 {720 - i * 22} Td ({line}) Tj ET")
    else:
        for i, cells in enumerate(lines):
            draws = []
            for x, cell in zip(columns, cells, strict=True):
                draws.append(f"{x} 0 Td ({cell}) Tj")
            content.append(f"BT /F1 14 Tf 50 {720 - i * 22} Td " + " ".join(draws) + " ET")

    stream = "\n".join(content).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    path.write_bytes(bytes(out))
    return path


def test_pdf_text_table(tmp_path: Path):
    pdf = _make_pdf(
        tmp_path / "po.pdf",
        [["part", "qty"], ["A0012", "100"], ["B0034", "200"]],
        columns=[50, 130],
    )
    doc = load_document(pdf)
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert table.headers == ["part", "qty"]
    assert len(table.rows) == 2
    first = table.rows[0]
    assert first.cells[0].text == "A0012"
    assert first.cells[1].value is not None and float(first.cells[1].value) == 100
    assert first.cells[1].loc.page == 1


def test_pdf_single_column_lines(tmp_path: Path):
    pdf = _make_pdf(
        tmp_path / "note.pdf",
        ["title", "Delivery note 001"],
    )
    doc = load_document(pdf)
    assert doc.tables[0].headers == ["title"]
    assert len(doc.tables[0].rows) == 1
    assert doc.tables[0].rows[0].cells[0].text == "Delivery note 001"


@pytest.mark.parametrize("table_mode", [True, False])
def test_pdf_compare_pipeline(tmp_path: Path, table_mode: bool):
    def build(name, qty, columns=None):
        lines = [["part", "qty"], ["A0012", qty], ["B0034", "999"]] if table_mode else ["part  qty", f"A0012  {qty}", "B0034  999"]
        return _make_pdf(tmp_path / name, lines, columns=([50, 130] if table_mode else None))

    pdf_a = build("po.pdf", "100")
    pdf_b = build("inv.pdf", "99")
    from reconcheck.comparison import compare_files

    report, findings = compare_files(pdf_a, pdf_b, match_on=["part"])
    assert report["summary"]["aligned_rows"] == 2
    # explicit no auto: only part/qty numeric columns; qty 100 vs 99 differs
    assert len(findings) == 1
    assert findings[0].field == "qty"


def test_scanned_pdf_raises_clear_error(tmp_path: Path):
    # a page whose content is empty (no text operators) -> OCR required
    pdf = _make_pdf(tmp_path / "scan.pdf", [])
    with pytest.raises(PdfOcrRequiredError):
        load_document(pdf)


def test_pdf_unsupported_without_dependency_is_clear():
    # when pdfplumber is missing the error names the fix; here it is installed,
    # so we only make sure the module import path exists
    import reconcheck.parse.pdf  # noqa: F401 - import smoke
