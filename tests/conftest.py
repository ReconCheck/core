"""Shared test helpers: build small in-memory tables."""

from reconcheck.models import Cell, Row, SourceLoc, Table


def make_cell(text: str, header: str, row: int, col: int, path: str = "test.csv") -> Cell:
    cell = Cell(text=text, loc=SourceLoc(path=path, row=row, col=col, header=header))
    cell.coerce()
    return cell


def make_table(headers: list[str], rows: list[dict[str, str]], path: str = "test.csv") -> Table:
    """``rows`` is a list of ``{header: text}`` dicts, one per data row."""
    table_rows: list[Row] = []
    for i, data in enumerate(rows, start=2):
        cells = [
            make_cell(str(data.get(header, "")), header, i, j + 1, path)
            for j, header in enumerate(headers)
        ]
        table_rows.append(Row(cells))
    return Table(headers=headers, rows=table_rows, loc=SourceLoc(path=path, row=2, col=1))
