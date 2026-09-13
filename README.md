# ReconCheck

![CI](https://github.com/ReconCheck/core/actions/workflows/ci.yml/badge.svg) · Python 3.10+ (Linux / macOS / Windows) · Apache-2.0

**Cross-document verification engine.** Point it at messy invoices, purchase orders and delivery notes. It tells you what doesn't match, by how much, and where in the original file.

把一叠格式混乱的单据拖进去，告诉你哪几处对不上、差多少钱、原文在哪一行。

> **Status: early but runnable.** A closed loop works today on tabular files
> (CSV / TSV / XLSX) and PDF text layers: point it at a purchase order and an
> invoice (or a whole PO + delivery note + invoice triple), it parses both,
> aligns the rows, runs the rules, and emits a JSON report whose every finding
> cites the exact position in the original file. OCR for scanned documents and
> real entity resolution are next — watch or star to follow along.

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,web,pdf]"   # Windows; pdf extra = PDF text-layer parsing
.venv/bin/python -m pip install -e ".[dev,web,pdf]"       # macOS / Linux

# CLI
.venv/Scripts/reconcheck compare examples/po.csv examples/invoice.csv \
  --rules examples/rules --match-on 料号 --normalize 料号:part_no

# Three-way: purchase order + delivery note + invoice
.venv/Scripts/reconcheck compare3 examples/po.csv examples/dn.csv examples/invoice.csv \
  --match-on 料号

# Web UI + REST API (http://127.0.0.1:8765)
.venv/Scripts/reconcheck-api
```

Every finding in the report carries `evidence` with a `cell://` href pointing
back to the exact cell in the original file — that is the whole point.

## Web API

The FastAPI service (`reconcheck-api`, port 8765) is the integration point for
enterprise systems and the upload frontend:

| Endpoint | Purpose |
|---|---|
| `GET  /api/health` | liveness + engine version |
| `POST /api/compare` | synchronous: compare two files and/or `doc_ids`, returns the report JSON |
| `POST /api/compare3` | synchronous three-way verification (PO / delivery note / invoice): 3 files or 3 `doc_ids` |
| `POST /api/jobs` | async batch: upload files + reference document ids, auto-pair by business key |
| `GET  /api/jobs/{id}` | job status / progress / per-pair summary |
| `GET  /api/reports/{id}` | stored comparison report |
| `GET  /api/documents` · `POST /api/documents` | document library: list / register uploaded files |
| `GET  /api/documents/{id}` | document meta + parsed table preview |
| `GET  /api/documents/{id}/content` · `DELETE` | raw download / delete |
| `GET  /api/datasources` · `POST` · `PUT` · `DELETE` | enterprise data source (custom web API) configuration |
| `POST /api/datasources/{id}/probe` | connectivity test |
| `POST /api/datasources/{id}/list` | list pickable records/documents from the source |
| `POST /api/datasources/{id}/fetch` | fetch a document (file stream) or records (JSON→table) into the library |

Interactive docs at `http://127.0.0.1:8765/docs`. Set `RECONCHECK_API_KEY` to
require an `X-API-Key` header on every `/api/*` call. **Without it the API is
unauthenticated** — the server prints a warning on startup, and anyone who can
reach the port can read and write data; always set the key outside a trusted
network. Example for enterprise callers:

```bash
# compare two uploaded files
curl -X POST http://127.0.0.1:8765/api/compare \
  -H "X-API-Key: $RECONCHECK_API_KEY" \
  -F "files=@po.csv" -F "files=@invoice.csv"

# configure a custom enterprise web API (records JSON) and fetch it
curl -X POST http://127.0.0.1:8765/api/datasources \
  -H "Content-Type: application/json" \
  -d '{"name":"ERP-PO","type":"records","url":"https://erp/api/orders","records_path":"data","auth":"bearer","token":"xxx"}'
curl -X POST http://127.0.0.1:8765/api/datasources/<id>/fetch

# batch-compare documents fetched from the enterprise system
curl -X POST http://127.0.0.1:8765/api/jobs \
  -d "doc_ids=<doc_a>,<doc_b>"
```

Data source types: `file` (the endpoint returns the document byte stream, URL
may contain `{id}`) and `records` (JSON array + `records_path`, converted into
a table). Auth: `none` / `bearer` / custom `header`. Secrets are stored in the
local `RECONCHECK_DATA` directory and never echoed by the API.

Operational knobs:

| variable | default | effect |
|---|---|---|
| `RECONCHECK_DATA` | `./data` | data directory (jobs, reports, documents, data sources) |
| `RECONCHECK_API_KEY` | *(unset)* | require `X-API-Key` on every `/api/*` call |
| `RECONCHECK_TTL_DAYS` | `30` | retention for uploaded files and generated reports; a background janitor prunes older entries every hour (active jobs are never touched) |
| — (fixed) | `64 MB` | per-upload size limit (413) |
| — (fixed) | `50 MB` | per-fetch response cap, enforced while streaming from a data source |

Text parsing decodes UTF-8 / GB18030 / UTF-16 / Latin-1 and refuses bytes that
do not look like readable text (binary junk gets a clear error instead of a
garbage table). XLSX is loaded in read-only streaming mode to keep memory flat
on large workbooks.

## The problem

Every month, someone in your company does this by hand:

Take the purchase orders, the delivery notes and the invoices. Match them, line by line. Find the ones that disagree. Work out whether each difference is a real error or just a rounding artefact. Write it up. Hand it to finance.

The files are never clean. They are scans. Skewed tables. Multi-column PDFs. Excel exports with merged cells. Photographs of paper.

It is slow, it is tedious, and it gets expensive the moment something slips through.

Your ERP does not help here. An ERP records what happened. It does not check whether what was recorded is consistent with everything else.

## What ReconCheck does

Four steps, in this order:

1. **Parse.** Recover the real table structure from scans, borderless tables and multi-column PDFs.
2. **Align.** Recognise that `华加` and `深圳市华加生物科技有限公司` are the same entity, that `A12` and `0012` are the same part, and normalise units across documents.
3. **Judge.** Separate genuine discrepancies from legitimate ones — FX rounding, decimal conventions, unit conversion.
4. **Cite.** Every finding points back to exact coordinates in the source file. Click it, see the original.

The output is not a risk score. It is a list of specific, checkable claims about your documents.

## Feature overview (this build)

What ships *today* (status details in the [docs](https://github.com/ReconCheck/docs)):

- **Parse** — CSV / TSV / TXT (sniffed delimiters, ragged rows), XLSX / XLSM (read-only streaming, multi-sheet, optional sheet filter) and **PDF text layer** (`pip install pdfplumber`; ruling-line tables first, layout fallback for borderless print-outs, a clear error for scanned files that need OCR). Encodings: UTF-8, GB18030, UTF-16, Latin-1 — with a plausibility gate so binary junk fails with a clear error instead of parsing as a garbage table.
- **Align** — exact key matching on `match_on` with normalisation (part numbers, entity suffixes, whitespace); cell-level units (`"5000 g"` → value `5000`, unit `"g"`) survive into the judgement stage.
- **Judge** — YAML differential rules (relative + absolute tolerance; exceptions: unit conversion `kg↔g`, rounding, **`dates_within: {days}` date tolerance**, **case-insensitive text equality**; severity; `evidence.require: both_sides`) **plus a built-in auto rule as the baseline** that compares every shared numeric column and never double-reports a column an explicit rule covers.
- **Cite** — every finding carries coordinates on both sides and a `cell://` href into the original file (PDF findings cite the page).
- **Three-way verification** — `reconcheck compare3 PO DN INV` / `POST /api/compare3` verifies purchase order, delivery note and invoice together: every pairwise report plus a conflicts section that names the outlier side per key and field.
- **CLI** — `reconcheck compare ...` / `compare3 ...` with clean error handling.
- **REST API** — synchronous compare, async batch jobs (auto-pairing, per-pair progress, failed-pair isolation), document library, stored reports, and user-configured enterprise data sources (probe / list / fetch).
- **Frontend** — dependency-free static page: drag-and-drop batch upload, comparison results with clickable evidence highlighting the source cell, document library, data-source configuration form.
- **Ops & security** — 64 MB upload / 50 MB streaming fetch caps, document-id allowlist (path-traversal guard), optional `X-API-Key` auth with a startup warning, atomic persistence, queued-job replay after restart, TTL janitor for uploads/reports (`RECONCHECK_TTL_DAYS`).

Full capability matrix, end-user guide and cross-engine design: [ReconCheck/docs](https://github.com/ReconCheck/docs).

## Design principles

- **Read-only.** It never writes to your data and never touches your business systems.
- **Auditable.** Every judgement carries its provenance. A finding that cannot be traced is not asserted.
- **Deployable anywhere.** Designed to run on-premise, with no document leaving your network.

## Roadmap

- [x] Tabular parsing, row alignment, YAML differential rules (tolerance + exceptions), evidence-chain JSON, CLI
- [x] PDF text-layer parsing (pdfplumber; scan-only files raise a clear "OCR not wired" error)
- [x] Exception catalogue: unit conversion, rounding, date tolerance (`dates_within`), case-insensitive text equality
- [x] REST API (`/api/compare`, `/api/compare3`, async `/api/jobs`, reports) + batch-upload web UI with clickable evidence
- [x] Enterprise data sources: configure custom web APIs, probe, fetch (file stream or records JSON) into the document library
- [x] Three-way match: purchase order / delivery note / invoice (compare3)
- [ ] LLM participation (opt-in): alignment disambiguation + finding explanations (interface reserved in `reconcheck/llm`)
- [ ] OCR for scanned PDFs/images; borderless multi-column layout recovery
- [ ] Cross-document alignment — entity resolution, fuzzy matching, unit normalisation
- [ ] Differential rule engine — richer exception catalogue (v1 spec freeze next)
- [ ] Evidence-chain output format — stable v1

Order is not a promise. It is the order in which the pieces are useful.

## Contributing

Not open for contributions yet — the interfaces are still moving. If you have a
corpus of document pairs that break existing tools, open an issue and describe it.
Real-world documents are the most useful thing you can give this project.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

---

## 中文说明

**ReconCheck** 是一个单据交叉核对引擎。它把订单、送货单、发票这几份文件放在一起逐条比对，输出「哪几处对不上、差多少钱、原文在哪一行」。

它**只读不写**，不接入业务系统、不改动任何数据；**每条判定都能点回原文出处**，可追溯、可复核。

它解决的是 ERP 不解决的问题：ERP 负责记账，不负责检查账记的这几份文件之间是否自洽。

> 项目处于中期开发阶段，表格类文件（CSV/TSV/XLSX）与 PDF 文本层的解析、三方核对（compare3）已可用；扫描件 OCR 与实体对齐是下一步。欢迎 Watch / Star 关注进展。

使用方式与功能清单见 [docs 仓库](https://github.com/ReconCheck/docs)：中文《用户使用指南》《功能能力清单》+ 英文 capability list。

### 为什么先做开源

核对的难点不在模型，在**版面还原、实体对齐、差异判定规则**这三件事上，而它们都极度依赖真实世界的单据样本。开源核心引擎，让更多人把样本和规则带进来，比闭门造车快得多。

企业版规则库（按行业、按客户定制的差异判定规则）不开源，放在 [rules](https://github.com/ReconCheck/rules)。