# ReconCheck design

Status: **minimal closed loop on tabular files**. See the roadmap in README.md for
what comes next.

## Pipeline

The engine is four stages, in this order:

1. **Parse** — turn an input file into `Document -> Table -> Row -> Cell`.
   Every cell carries a `SourceLoc` (path, sheet/page, 1-based row, 1-based
   column, header), so any later claim can be pointed back to the original file.
2. **Align** — match rows across two tables on one or more key columns
   (`match_on`). Keys are normalised first: part numbers (`A-012` → `a12`),
   legal-suffix stripping for entity names, whitespace. Rows that do not match
   are left alone for now (three-way matching is a later phase).
3. **Judge** — for each aligned row pair, rule engine compares columns. A
   difference is a finding only if it exceeds tolerance *and* no exception
   applies (rounding, unit conversion).
4. **Cite** — every finding carries `Evidence` for both sides with exact
   coordinates and a `cell://` href: `cell://<path>#<sheet>#<row>:<col>`.

## Data model

See `src/reconcheck/models.py`. Notes:

- `Cell.value` is a `Decimal` when the text can be coerced (a unit suffix is
  kept in `Cell.unit`, e.g. `"5000 g"` → value `5000`, unit `"g"`).
- `Finding.claim` is a plain sentence; the machine-readable details live in the
  finding fields. The report JSON is the contract.

## Rules

Rules are YAML, loaded from `--rules <file-or-dir>`. Format (subset of the
spec in the private ReconCheck/rules repo):

```yaml
id: po-invoice-qty-mismatch
applies_to: [purchase_order, invoice]   # informational for now
severity: high                          # high | medium | low
match_on: [料号]                         # rows must carry these headers on both sides
compare: 数量                            # single column name, or a list
tolerance:
  relative: 0        # |l - r| <= abs + rel * max(|l|, |r|, 1)
  absolute: 0
exceptions:
  - when:
      unit_conversion_between: [kg, g]
  - when:
      rounding: { decimals: 2 }
evidence:
  require: both_sides  # skip a finding that cannot cite both source cells
```

Implemented exception kinds: `unit_conversion_between`, `rounding`.
With no `--rules` the engine uses a built-in auto rule: any common column that
has numbers on both sides, tolerance `relative 0.001 / absolute 0.01`.

## Current limitations (next phases)

- No PDF or image parsing yet (scans, borderless tables, multi-column) — OCR
  pipeline is the next milestone.
- XLSX merged cells: value read from the top-left cell only.
- Entity resolution is suffix/whitespace stripping, not full linking
  (`华加` vs `深圳市华加生物科技有限公司` still needs a real resolution pass).
- Row matching is exact key equality; fuzzy and three-way matching are future.
- Batch pairing keys on filenames only (kind tokens such as `po`/`inv` are
  stripped; the remaining business key must be shared by the files of one
  group). Manual pairing UI is future work.
- Job and report state lives in the server process (jobs.json + reports dir
  under `RECONCHECK_DATA`); no distributed queue yet.

## Web layer

`src/reconcheck/web/app.py` is a FastAPI app (`reconcheck-api`, port 8765):

- `/api/compare` — synchronous two-file comparison (multipart), same report
  contract as the CLI.
- `/api/jobs` — upload any number of files; the server groups them by business
  key (see below) and compares every pair in a background worker; poll
  `/api/jobs/{id}` for progress.
- `/api/reports/{id}` — stored report JSON, deep-linkable.
- Optional `RECONCHECK_API_KEY` (env) turns on `X-API-Key` enforcement for
  every `/api/*` route; the static frontend stays open.
- The frontend is a dependency-free static app (no CDN, no build step) served
  from `web/static/`: drag-drop upload, auto pairing, severity summary, and
  click-to-highlight evidence cells in the rendered source tables.

### Pairing heuristic

`guess_kind` marks a file as `po` / `invoice` / `delivery` / `unknown` by kind
tokens in the filename (`po`, `order`, `采购` …). `base_key` strips those
tokens plus separators and lowercases, e.g. `PO-240913-001` and
`INV-240913-001` both key to `240913001` and are compared as a pair. Files
whose key appears alone are reported as unpaired.

## Dev setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
pytest
ruff check src tests
reconcheck compare examples/po.csv examples/invoice.csv \
  --rules examples/rules --match-on 料号 --normalize 料号:part_no
```