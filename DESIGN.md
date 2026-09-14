# ReconCheck design

Status: **minimal closed loop on tabular files**. See the roadmap in README.md for
what comes next.

## Pipeline

The engine is four stages, in this order:

1. **Parse** — turn an input file into `Document -> Table -> Row -> Cell`.
   Every cell carries a `SourceLoc` (path, sheet/page, 1-based row, 1-based
   column, header), so any later claim can be pointed back to the original file.
   Tabular: CSV / TSV / TXT and XLSX / XLSM. PDF (text layer, via the optional
   `pdf` extra): ruling-line tables first, then a layout fallback that clusters
   words into rows/columns; a PDF without extractable text raises
   `PdfOcrRequiredError` instead of parsing garbage (an optional Tesseract OCR
   backend activates with `pdf-ocr` + `RECONCHECK_OCR=1`; otherwise the refusal
   stands).
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

Implemented exception kinds: `unit_conversion_between`, `rounding`,
`dates_within: {days}` (both sides parse as dates, |Δdays| ≤ N exempt — a
real date difference is still reported), `text_equal_ignore_case: true`
(case-insensitive equality exempts; a real text difference is reported).
With no `--rules` the engine uses a built-in auto rule: any common column that
has numbers on both sides, tolerance `relative 0.001 / absolute 0.01`.

## Three-way verification

`compare_three(docs)` / `reconcheck compare3 PO DN INV` /
`POST /api/compare3` runs the normal two-way pipeline on every unordered pair
(three reports) **and** a `three_way` section: for each shared key and field it
compares the three documents (numbers use the tightest tolerance an applying
rule declares, text compares case-insensitively), reports `consistent` plus
`outlier_indices` — the sides that disagree with the majority, e.g. an invoice
that does not match the PO and delivery note.

## Current limitations (next phases)

- Scanned-PDF OCR is optional (Tesseract via the `pdf-ocr` extra +
  `RECONCHECK_OCR=1`, per-line text output); image files remain unsupported, and
  borderless multi-column layouts rely on the word-clustering fallback and can
  be imperfect.
- PDF table detection follows ruling lines first; merged cells inside PDF
  tables collapse to their left value. XLSX merged cells: value read from the
  top-left cell only.
- Entity resolution is suffix/whitespace stripping, not full linking
  (`华加` vs `深圳市华加生物科技有限公司` still needs a real resolution pass).
- Row matching between two documents is exact key equality; fuzzy matching is
  future. Three-way verification aligns each pair with the same exact-key
  alignment and adds a majority/outlier pass per field.
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

## Enterprise data sources (custom web APIs)

`web/datasources.py` models a user-configured *data source* — an HTTP wrapper
an operator (or the frontend form) points at part of an enterprise system:

- **type=file** — the endpoint returns the document byte stream; the URL may
  contain a `{id}` placeholder filled from the picker; `list_url` optionally
  serves the pickable `{id, name}` entries.
- **type=records** — the endpoint returns JSON; `records_path` selects the
  array (e.g. `data.items`), `id_field`/`name_field` drive the picker, and
  `parse.document_from_records` converts records into a `Table` (union of
  keys as headers, one record per row).

Auth: `none` / `bearer` / custom `header` (name in `header_name`). Tokens are
persisted next to the other data (plaintext in the local data dir — this is an
operator-configured credential store, not a vault) and never echoed by the
API (`has_token` instead). Fetched documents land in the *document library*
(`web/store.py`, `data/documents/<id>/`), previewable as parsed tables and
referenceable in `/api/compare` and `/api/jobs` via `doc_ids`. Fetching is a
server-side request (no browser CORS, no SSRF guard in v0 — the operator
configures the endpoint deliberately).

## Robustness & security (implemented)

- **Input limits** — uploads capped at 64 MB (413); data-source responses capped
  at 50 MB *while streaming* (mid-download abort, not check-after-buffer).
- **Path traversal** — document and report ids are validated against a 12-hex
  allowlist before touching the filesystem (delete / download / report reads);
  `Content-Disposition` filenames are sanitized.
- **Duplicate documents** — a job drops entries with the same `doc_id` or
  byte-identical uploads (sha256), so the `.csv-2` rename trick can never
  produce a self-comparison.
- **Failure isolation** — one bad pair is marked `failed` with its error while
  the rest of the batch finishes; `job.error` surfaces in the job view.
- **Decode plausibility gate** — text candidates (utf-8-sig / gb18030 / utf-16 /
  latin-1) are accepted only when the control-char ratio is below 10%, so
  random binary raises `TextDecodeError` instead of parsing as garbage.
- **Persistence & janitor** — jobs.json is written atomically; queued jobs are
  replayed after a restart; a background janitor (hourly + at startup) prunes
  uploaded files and reports older than `RECONCHECK_TTL_DAYS` (default 30,
  active jobs never touched).
- **Auth** — optional `RECONCHECK_API_KEY` → `X-API-Key` on every `/api/*`
  call; a loud startup warning when unset.

## LLM participation (design, not yet implemented)

The engine stays deterministic: rules are the primary judge. An opt-in
`LLMEnhancer` (contract reserved in `reconcheck/llm`, `NullEnhancer` is the
default) may assist at two seams:

1. **Alignment disambiguation** — rows whose exact keys do not collide go to
   the model as candidate pairs (`disambiguate`); the engine keeps acceptance
   control (threshold, limits). Design target: `华加` ↔ `深圳市华加生物科技有限公司`.
2. **Finding narration** — `explain` attaches a natural-language sentence to a
   finding; once a backend lands, model-touched findings will carry a marker
   field (the report contract has none today).

Guardrails: OpenAI-compatible endpoints only (`base_url` + `api_key` + model,
operator-configured in the same style as data sources); opt-in per job;
documents only travel to the configured endpoint; timeout, token budget and
rollback to the deterministic result on any failure; no model call at all when
disabled. A later milestone wires a concrete OpenAI-compatible client behind
this contract.

## Dev setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,pdf]"   # Windows (pdf extra for PDF tests)
pytest
ruff check src tests
reconcheck compare examples/po.csv examples/invoice.csv \
  --rules examples/rules --match-on 料号 --normalize 料号:part_no
```