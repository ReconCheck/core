/* ReconCheck web UI — batch upload, auto pairing, clickable evidence. */
"use strict";

const $ = (sel) => document.querySelector(sel);

const KIND_TOKENS = {
  po: ["po", "purchase", "order", "采购", "订单", "订购"],
  invoice: ["inv", "invoice", "发票", "iv", "ir"],
  delivery: ["dn", "delivery", "送货", "收货", "asn", "发货"],
};
const KIND_LABEL = { po: "订单", invoice: "发票", delivery: "送货", unknown: "未识别" };

function kindOf(name) {
  const base = name.replace(/\.[^.]+$/, "").toLowerCase();
  for (const [kind, tokens] of Object.entries(KIND_TOKENS)) {
    if (tokens.some((t) => base.includes(t))) return kind;
  }
  return "unknown";
}

function baseKey(name) {
  const stem = name.replace(/\.[^.]+$/, "");
  for (const tokens of Object.values(KIND_TOKENS)) {
    for (const t of tokens) stem = stem.replace(new RegExp(t, "ig"), "");
  }
  const key = stem.toLowerCase().replace(/[^a-z0-9]+/g, "");
  return key;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

/* ---------------- state ---------------- */
const state = { files: [], job: null, pollTimer: null };

/* ---------------- elements ---------------- */
const dz = $("#dropzone");
const fileInput = $("#fileinput");
const fileList = $("#fileList");
const fileCount = $("#fileCount");
const filesSection = $("#filesSection");
const runBtn = $("#runBtn");
const clearBtn = $("#clearBtn");
const progressSection = $("#progressSection");
const progressFill = $("#progressFill");
const progressText = $("#progressText");
const resultsSection = $("#resultsSection");
const summaryBox = $("#summary");
const pairsBox = $("#pairs");
const unpairedBox = $("#unpaired");

/* ---------------- health ---------------- */
fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    $("#health").classList.add("ok");
    $("#healthText").textContent = `引擎 v${h.version} · 服务正常`;
  })
  .catch(() => {
    $("#health").classList.add("bad");
    $("#healthText").textContent = "API 不可用";
  });

/* ---------------- files ---------------- */
function renderFiles() {
  fileCount.textContent = `(${state.files.length})`;
  fileList.innerHTML = "";
  for (const f of state.files) {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="badge ${f.kind}">${KIND_LABEL[f.kind]}</span>` +
      `<span class="file-name">${esc(f.name)}</span>` +
      `<span class="badge group" title="分组键">组 ${esc(f.base)}</span>` +
      `<span class="file-size">${fmtSize(f.size)}</span>`;
    fileList.appendChild(li);
  }
  filesSection.classList.remove("hidden");
  runBtn.disabled = state.files.length < 2;
}

function addFiles(files) {
  for (const f of files) {
    if (!/\.(csv|tsv|txt|xlsx|xlsm)$/i.test(f.name)) continue;
    state.files.push({ file: f, name: f.name, kind: kindOf(f.name), base: baseKey(f.name) });
  }
  renderFiles();
}

function clearFiles() {
  state.files = [];
  fileInput.value = "";
  renderFiles();
  resultsSection.classList.add("hidden");
  progressSection.classList.add("hidden");
}

dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("drag");
  addFiles(e.dataTransfer.files);
});
$("#browse").addEventListener("click", (e) => { e.preventDefault(); fileInput.click(); });
fileInput.addEventListener("change", () => addFiles(fileInput.files));
clearBtn.addEventListener("click", clearFiles);

/* ---------------- run & poll ---------------- */
runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  clearBtn.disabled = true;
  resultsSection.classList.add("hidden");
  progressSection.classList.remove("hidden");
  progressFill.style.width = "0%";
  progressText.textContent = "上传文件…";

  const fd = new FormData();
  for (const f of state.files) fd.append("files", f.file, f.name);
  fd.append("config", "{}");

  try {
    const resp = await fetch("/api/jobs", { method: "POST", body: fd });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || ("HTTP " + resp.status));
    state.job = body;
    startPolling();
  } catch (err) {
    fail(`提交失败：${err.message}`);
  }
});

function startPolling() {
  progressText.textContent = "排队中…";
  state.pollTimer = setInterval(async () => {
    try {
      const resp = await fetch(`/api/jobs/${state.job.id}`);
      const job = await resp.json();
      state.job = job;
      renderProgress(job);
      if (job.status === "done") {
        clearInterval(state.pollTimer);
        await showResults(job);
      } else if (job.status === "failed") {
        clearInterval(state.pollTimer);
        fail("任务失败：" + (job.error || "未知错误"));
      }
    } catch (err) {
      clearInterval(state.pollTimer);
      fail("轮询失败：" + err.message);
    }
  }, 1000);
}

function renderProgress(job) {
  const p = job.progress || { done: 0, total: 0 };
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  progressFill.style.width = pct + "%";
  progressText.textContent =
    `${p.done}/${p.total} 对单据比对中…` + (p.total ? `（${pct}%）` : "");
}

function fail(msg) {
  clearInterval(state.pollTimer);
  progressSection.classList.add("hidden");
  resultsSection.classList.remove("hidden");
  summaryBox.innerHTML = `<div class="unpaired">${esc(msg)}</div>`;
  runBtn.disabled = false;
  clearBtn.disabled = false;
}

/* ---------------- results ---------------- */
async function showResults(job) {
  progressSection.classList.add("hidden");
  resultsSection.classList.remove("hidden");
  runBtn.disabled = false;
  clearBtn.disabled = false;

  const totals = { total: 0, high: 0, medium: 0, low: 0 };
  const loaded = [];
  for (const pair of job.pairs) {
    if (pair.status === "done" && pair.report_id) {
      const r = await fetch(`/api/reports/${pair.report_id}`).then((x) => x.json());
      loaded.push({ pair, report: r });
      totals.total += r.summary.total;
      totals.high += r.summary.high;
      totals.medium += r.summary.medium;
      totals.low += r.summary.low;
    }
  }

  summaryBox.innerHTML =
    `<div class="sum-card total"><div class="num">${totals.total}</div><div class="lbl">差异总计</div></div>` +
    `<div class="sum-card high"><div class="num">${totals.high}</div><div class="lbl">高</div></div>` +
    `<div class="sum-card medium"><div class="num">${totals.medium}</div><div class="lbl">中</div></div>` +
    `<div class="sum-card low"><div class="num">${totals.low}</div><div class="lbl">低</div></div>` +
    `<div class="sum-card total"><div class="num">${loaded.length}</div><div class="lbl">比对对数</div></div>`;

  pairsBox.innerHTML = "";
  for (const { pair, report } of loaded) {
    pairsBox.appendChild(makePair(pair, report));
  }

  if (job.unpaired && job.unpaired.length) {
    unpairedBox.classList.remove("hidden");
    unpairedBox.innerHTML =
      `<b>未配对文件</b>（文件名缺少相同的业务编号）<br>` +
      job.unpaired.map((n) => `<span>${esc(n)}</span>`).join("");
  } else {
    unpairedBox.classList.add("hidden");
  }
}

function makePair(pair, report) {
  const sec = document.createElement("div");
  sec.className = "pair closed";

  const badges =
    (pair.high ? `<span class="pill f">${pair.high} 高</span>` : "") +
    (pair.medium ? `<span class="pill m">${pair.medium} 中</span>` : "") +
    (pair.low ? `<span class="pill l">${pair.low} 低</span>` : "") +
    (pair.status === "done" && !pair.findings
      ? `<span class="pill ok">无差异</span>`
      : "");

  const head = document.createElement("div");
  head.className = "pair-head";
  head.innerHTML =
    `<span class="pair-title">${esc(pair.left)} <span style="color:var(--muted)">↔</span> ${esc(pair.right)}</span>` +
    `<span class="pair-meta">${badges}<span class="pair-arrow">▸</span></span>`;
  head.addEventListener("click", () => {
    sec.classList.toggle("open");
    if (sec.classList.contains("open") && !sec.dataset.rendered) {
      renderPairBody(report, sec);
      sec.dataset.rendered = "1";
    }
  });

  const body = document.createElement("div");
  body.className = "pair-body";
  sec.appendChild(head);
  sec.appendChild(body);
  return sec;
}

function renderPairBody(report, sec) {
  const body = sec.querySelector(".pair-body");
  const findings = report.findings || [];
  if (!findings.length) {
    body.innerHTML = `<p style="color:var(--ok);font-weight:600">✓ 未发现超过容差范围的差异。</p>`;
    return;
  }

  let rows = "";
  for (const f of findings) {
    let ev = "";
    for (const e of f.evidence) {
      const loc = e.loc;
      ev +=
        `<div>${e.side === "left" ? "左" : "右"}：第 ${loc.row} 行 / 第 ${loc.col} 列 ` +
        `（${esc(loc.header)}）＝ ${esc(e.excerpt)}</div>`;
    }
    rows +=
      `<tr class="fd" data-fid="${esc(f.id)}">` +
      `<td><span class="pill ${f.severity[0]}">${esc(f.severity)}</span></td>` +
      `<td>${esc(f.field)}</td>` +
      `<td>${esc(f.left_value)} → ${esc(f.right_value)}</td>` +
      `<td>${esc(f.claim)}<br><small style="color:var(--muted)">${ev}</small></td>` +
      `</tr>`;
  }

  body.innerHTML =
    `<table class="find-table"><thead><tr>` +
    `<th>级别</th><th>字段</th><th>左 → 右</th><th>说明（点击行高亮原文）</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>` +
    `<div class="doc-grid" id="docs-${esc(report.engine.version)}"></div>`;

  const grid = body.querySelector(".doc-grid");
  for (const doc of report.documents) {
    grid.appendChild(renderDoc(doc));
  }

  body.querySelectorAll("tr.fd").forEach((tr) => {
    tr.addEventListener("click", () => {
      const f = findings.find((x) => x.id === tr.dataset.fid);
      highlight(f, body);
    });
  });
}

function renderDoc(doc) {
  const card = document.createElement("div");
  card.className = "doc-card";
  const table = doc.tables && doc.tables[0];
  if (!table) {
    card.innerHTML = `<div class="doc-head">${esc(doc.path)}（空）</div>`;
    return card;
  }
  const headHtml =
    `<tr>${table.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
  let bodyHtml = "";
  for (const row of table.rows || []) {
    const tds = row
      .map((c) =>
        `<td data-r="${c.loc.row}" data-c="${c.loc.col}" title="${esc(c.loc.path)} 第${c.loc.row}行 第${c.loc.col}列">${esc(c.text)}</td>`
      )
      .join("");
    bodyHtml += `<tr>${tds}</tr>`;
  }
  card.innerHTML =
    `<div class="doc-head">${esc(doc.path)}</div>` +
    `<table class="doc-table"><thead>${headHtml}</thead><tbody>${bodyHtml}</tbody></table>`;
  return card;
}

function highlight(finding, body) {
  body.querySelectorAll("td.hl").forEach((td) => td.classList.remove("hl"));
  for (const e of finding.evidence) {
    const grid = body.querySelector(".doc-grid");
    const cards = grid.querySelectorAll(".doc-card");
    const idx = e.side === "left" ? 0 : 1;
    const cell = cards[idx] && cards[idx].querySelector(`td[data-r="${e.loc.row}"][data-c="${e.loc.col}"]`);
    if (cell) {
      cell.classList.add("hl");
      cell.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}