/* ReconCheck web UI — batch upload, enterprise data sources, document library. */
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
  return stem.toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
  return body;
}

/* ============================ tabs ============================ */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    $("#view-compare").classList.toggle("hidden", btn.dataset.view !== "compare");
    $("#view-ext").classList.toggle("hidden", btn.dataset.view !== "ext");
    if (btn.dataset.view === "ext") refreshExt();
  });
});

/* ============================ state ============================ */
const state = { files: [], job: null, pollTimer: null };

/* ============================ health ============================ */
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

/* ===================== view: compare ===================== */
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

function renderFiles() {
  fileCount.textContent = `(${state.files.length})`;
  fileList.innerHTML = "";
  for (const f of state.files) {
    const li = document.createElement("li");
    const badge = f.doc
      ? `<span class="badge group">文档</span>`
      : `<span class="badge ${f.kind}">${KIND_LABEL[f.kind]}</span>`;
    li.innerHTML =
      badge +
      `<span class="file-name">${esc(f.name)}</span>` +
      `<span class="badge group" title="分组键">组 ${esc(f.base)}</span>` +
      `<span class="file-size">${f.doc ? "文档库" : fmtSize(f.size)}</span>`;
    li.querySelector(".file-name").addEventListener("click", () => removeFromQueue(f));
    li.querySelector(".file-name").style.cursor = "pointer";
    li.querySelector(".file-name").title = "点击移出队列";
    fileList.appendChild(li);
  }
  filesSection.classList.remove("hidden");
  runBtn.disabled = state.files.length < 2;
}

function removeFromQueue(f) {
  state.files = state.files.filter((x) => x !== f);
  renderFiles();
}

function addFiles(files) {
  for (const f of files) {
    if (!/\.(csv|tsv|txt|xlsx|xlsm)$/i.test(f.name)) continue;
    state.files.push({ file: f, name: f.name, kind: kindOf(f.name), base: baseKey(f.name) });
  }
  renderFiles();
}

function addDocToQueue(doc) {
  state.files.push({ doc: true, id: doc.id, name: doc.name, kind: kindOf(doc.name), base: baseKey(doc.name), size: doc.size });
  renderFiles();
  showView("compare");
}

function showView(name) {
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.view === name;
    b.classList.toggle("active", on);
    const target = on ? (name === "compare" ? "#view-compare" : "#view-ext") : null;
  });
  $("#view-compare").classList.toggle("hidden", name !== "compare");
  $("#view-ext").classList.toggle("hidden", name !== "ext");
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

runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  clearBtn.disabled = true;
  resultsSection.classList.add("hidden");
  progressSection.classList.remove("hidden");
  progressFill.style.width = "0%";
  progressText.textContent = "上传文件…";

  const fd = new FormData();
  for (const f of state.files) {
    if (f.doc) continue;
    fd.append("files", f.file, f.name);
  }
  const docIds = state.files.filter((f) => f.doc).map((f) => f.id);
  fd.append("doc_ids", docIds.join(","));
  fd.append("config", "{}");

  try {
    const body = await api("/api/jobs", { method: "POST", body: fd });
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
      const job = await api(`/api/jobs/${state.job.id}`);
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

/* -------- results -------- */
async function showResults(job) {
  progressSection.classList.add("hidden");
  resultsSection.classList.remove("hidden");
  runBtn.disabled = false;
  clearBtn.disabled = false;

  const totals = { total: 0, high: 0, medium: 0, low: 0 };
  const loaded = [];
  for (const pair of job.pairs) {
    if (pair.status === "done" && pair.report_id) {
      const r = await api(`/api/reports/${pair.report_id}`);
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
      `<b>未配对文件</b>（文件名缺少相同的业务编号或组内少于两份）<br>` +
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
      : "") +
    (pair.error ? `<span class="pill f">失败</span>` : "");

  const head = document.createElement("div");
  head.className = "pair-head";
  head.innerHTML =
    `<span class="pair-title">${esc(pair.left)} <span style="color:var(--muted)">↔</span> ${esc(pair.right)}</span>` +
    `<span class="pair-meta">${badges}<span class="pair-arrow">▸</span></span>`;
  head.addEventListener("click", () => {
    sec.classList.toggle("open");
    if (sec.classList.contains("open") && !sec.dataset.rendered) {
      if (pair.error) {
        sec.querySelector(".pair-body").innerHTML =
          `<p style="color:var(--high)">比对失败：${esc(pair.error)}</p>`;
      } else {
        renderPairBody(report, sec);
      }
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
    `<div class="doc-grid"></div>`;

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
  const headHtml = `<tr>${table.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
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
    const cards = body.querySelectorAll(".doc-card");
    const idx = e.side === "left" ? 0 : 1;
    const cell = cards[idx] && cards[idx].querySelector(`td[data-r="${e.loc.row}"][data-c="${e.loc.col}"]`);
    if (cell) {
      cell.classList.add("hl");
      cell.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}

/* ================ view: data sources & document library ================ */

/* -------- data source list -------- */
let editingDsId = null;

async function refreshExt() {
  await Promise.all([refreshDsList(), refreshDocList()]);
}

async function refreshDsList() {
  const ul = $("#dsList");
  try {
    const { datasources } = await api("/api/datasources");
    if (!datasources.length) {
      ul.innerHTML = `<li class="empty">尚未配置数据源</li>`;
      return;
    }
    ul.innerHTML = "";
    for (const ds of datasources) {
      const li = document.createElement("li");
      li.innerHTML =
        `<div class="ds-info">` +
        `<span class="ds-name">${esc(ds.name)}</span>` +
        `<span class="badge ${ds.type}">${ds.type === "file" ? "文件流" : "记录 JSON"}</span>` +
        `<span class="badge unknown">${ds.auth}</span>` +
        `<div class="ds-url">${esc(ds.url)}</div>` +
        `</div>` +
        `<div class="ds-actions">` +
        `<button class="ghost small" data-act="probe">测试</button>` +
        `<button class="ghost small" data-act="list">拉取</button>` +
        `<button class="ghost small" data-act="edit">编辑</button>` +
        `<button class="ghost small danger" data-act="del">删除</button>` +
        `</div>` +
        `<span class="ds-msg hidden"></span>`;
      li.querySelector('[data-act="probe"]').addEventListener("click", () => probeDs(ds, li));
      li.querySelector('[data-act="list"]').addEventListener("click", () => listDs(ds));
      li.querySelector('[data-act="edit"]').addEventListener("click", () => openDsForm(ds));
      li.querySelector('[data-act="del"]').addEventListener("click", () => deleteDs(ds, li));
      ul.appendChild(li);
    }
  } catch (err) {
    ul.innerHTML = `<li class="empty">加载失败：${esc(err.message)}</li>`;
  }
}

async function probeDs(ds, li) {
  const msg = li.querySelector(".ds-msg");
  msg.classList.remove("hidden");
  msg.textContent = "测试中…";
  try {
    const r = await api(`/api/datasources/${ds.id}/probe`, { method: "POST" });
    msg.textContent = r.ok
      ? `✓ HTTP ${r.status} · ${r.bytes} 字节`
      : `✗ ${r.error}`;
    msg.className = "ds-msg";
  } catch (err) {
    msg.textContent = `✗ ${err.message}`;
  }
}

async function listDs(ds) {
  const modal = $("#pickerModal");
  modal.classList.remove("hidden");
  $("#pickerTitle").textContent = `从「${ds.name}」拉取`;
  const sel = $("#pickerSelect");
  sel.innerHTML = `<option value="">直接拉取（整份/整个列表）</option>`;
  try {
    const { items } = await api(`/api/datasources/${ds.id}/list`, { method: "POST" });
    for (const item of items.slice(0, 500)) {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.name;
      sel.appendChild(opt);
    }
  } catch (err) {
    /* listing optional; direct fetch still allowed */
  }
  $("#pickerMsg").textContent = "";
  const ok = $("#pickerOk");
  ok.onclick = async () => {
    ok.disabled = true;
    $("#pickerMsg").textContent = "拉取中…";
    try {
      await api(`/api/datasources/${ds.id}/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ record_id: sel.value || null }),
      });
      modal.classList.add("hidden");
      await refreshDocList();
    } catch (err) {
      $("#pickerMsg").textContent = `✗ ${err.message}`;
    } finally {
      ok.disabled = false;
    }
  };
}

$("#pickerCancel").addEventListener("click", () => {
  $("#pickerModal").classList.add("hidden");
});

async function deleteDs(ds, li) {
  if (!confirm(`删除数据源「${ds.name}」？`)) return;
  try {
    await api(`/api/datasources/${ds.id}`, { method: "DELETE" });
    refreshDsList();
  } catch (err) {
    li.querySelector(".ds-msg").textContent = `✗ ${err.message}`;
  }
}

/* -------- data source form -------- */
function openDsForm(ds = null) {
  editingDsId = ds ? ds.id : null;
  $("#dsForm").classList.remove("hidden");
  $("#dsId").value = ds ? ds.id : "";
  $("#dsName").value = ds ? ds.name : "";
  $("#dsType").value = ds ? ds.type : "file";
  $("#dsUrl").value = ds ? ds.url : "";
  $("#dsMethod").value = ds ? ds.method : "GET";
  $("#dsAuth").value = ds ? ds.auth : "none";
  $("#dsToken").value = "";
  $("#dsHeaderName").value = ds ? (ds.header_name || "") : "";
  $("#dsHeaders").value = ds && ds.headers ? JSON.stringify(ds.headers, null, 0) : "";
  $("#dsRecordsPath").value = ds ? (ds.records_path || "") : "";
  $("#dsIdField").value = ds ? (ds.id_field || "") : "";
  $("#dsNameField").value = ds ? (ds.name_field || "") : "";
  $("#dsListUrl").value = ds ? (ds.list_url || "") : "";
  $("#dsFormMsg").textContent = "";
  syncDsForm();
}

function syncDsForm() {
  const type = $("#dsType").value;
  document.querySelectorAll(".records-only").forEach((el) => el.classList.toggle("hidden", type !== "records"));
  document.querySelectorAll(".file-only").forEach((el) => el.classList.toggle("hidden", type !== "file"));
  $("#authExtra").classList.toggle("hidden", $("#dsAuth").value === "none");
}

$("#dsType").addEventListener("change", syncDsForm);
$("#dsAuth").addEventListener("change", syncDsForm);

$("#newDsBtn").addEventListener("click", () => openDsForm());
$("#dsCancelBtn").addEventListener("click", () => {
  $("#dsForm").classList.add("hidden");
});

$("#dsSaveBtn").addEventListener("click", async () => {
  const payload = {
    name: $("#dsName").value.trim(),
    type: $("#dsType").value,
    url: $("#dsUrl").value.trim(),
    method: $("#dsMethod").value,
    auth: $("#dsAuth").value,
    token: $("#dsToken").value,
    header_name: $("#dsHeaderName").value.trim(),
    records_path: $("#dsRecordsPath").value.trim(),
    id_field: $("#dsIdField").value.trim(),
    name_field: $("#dsNameField").value.trim(),
    list_url: $("#dsListUrl").value.trim(),
  };
  try {
    payload.headers = JSON.parse($("#dsHeaders").value || "{}");
  } catch {
    $("#dsFormMsg").textContent = "额外请求头不是合法 JSON";
    return;
  }
  if (!payload.name || !payload.url) {
    $("#dsFormMsg").textContent = "名称和 URL 必填";
    return;
  }
  try {
    if (editingDsId) {
      await api(`/api/datasources/${editingDsId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/datasources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    $("#dsForm").classList.add("hidden");
    refreshDsList();
  } catch (err) {
    $("#dsFormMsg").textContent = `✗ ${err.message}`;
  }
});

/* -------- document library -------- */
async function refreshDocList() {
  const ul = $("#docList");
  try {
    const { documents } = await api("/api/documents");
    $("#docCount").textContent = `(${documents.length})`;
    if (!documents.length) {
      ul.innerHTML = `<li class="empty">还没有文档——从数据源拉取，或上传文件入库</li>`;
      return;
    }
    ul.innerHTML = "";
    for (const doc of documents) {
      const li = document.createElement("li");
      const srcBadge =
        doc.source === "records"
          ? `<span class="badge records">记录</span>`
          : doc.source === "datasource"
            ? `<span class="badge delivery">数据源</span>`
            : `<span class="badge unknown">上传</span>`;
      li.innerHTML =
        `<div class="ds-info">` +
        `<span class="ds-name">${esc(doc.name)}</span> ${srcBadge}` +
        `<div class="ds-url">${fmtSize(doc.size)} · ${doc.id}</div>` +
        `<div class="doc-preview hidden"></div>` +
        `</div>` +
        `<div class="ds-actions">` +
        `<button class="ghost small" data-act="prev">预览</button>` +
        `<button class="ghost small primary" data-act="use">加入比对</button>` +
        `<button class="ghost small" data-act="dl">下载</button>` +
        `<button class="ghost small danger" data-act="del">删除</button>` +
        `</div>`;
      li.querySelector('[data-act="prev"]').addEventListener("click", () => togglePreview(doc, li));
      li.querySelector('[data-act="use"]').addEventListener("click", () => addDocToQueue(doc));
      li.querySelector('[data-act="dl"]').addEventListener("click", () => {
        window.location.href = `/api/documents/${doc.id}/content`;
      });
      li.querySelector('[data-act="del"]').addEventListener("click", async () => {
        if (!confirm(`删除文档「${doc.name}」？`)) return;
        await api(`/api/documents/${doc.id}`, { method: "DELETE" });
        refreshDocList();
      });
      void info;
      ul.appendChild(li);
    }
  } catch (err) {
    ul.innerHTML = `<li class="empty">加载失败：${esc(err.message)}</li>`;
  }
}

async function togglePreview(doc, li) {
  const box = li.querySelector(".doc-preview");
  if (!box.classList.contains("hidden")) {
    box.classList.add("hidden");
    return;
  }
  try {
    const d = await api(`/api/documents/${doc.id}`);
    if (d.preview && d.preview.headers) {
      const p = d.preview;
      const head = `<tr>${p.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
      const rows = p.rows
        .map((r) => `<tr>${r.map((c) => `<td>${esc(c.text)}</td>`).join("")}</tr>`)
        .join("");
      box.innerHTML =
        `<table class="doc-table"><thead>${head}</thead><tbody>${rows}</tbody></table>` +
        (p.total_rows > p.rows.length ? `<div class="ds-url">…共 ${p.total_rows} 行（预览前 ${p.rows.length} 行）</div>` : "");
    } else {
      box.innerHTML = `<div class="ds-url">${esc((d.preview && d.preview.error) || "无表格预览")}</div>`;
    }
    box.classList.remove("hidden");
  } catch (err) {
    box.innerHTML = `<div class="ds-url">✗ ${esc(err.message)}</div>`;
    box.classList.remove("hidden");
  }
}

$("#docUploadBtn").addEventListener("click", () => $("#docUploadInput").click());
$("#docUploadInput").addEventListener("change", async () => {
  const fd = new FormData();
  for (const f of $("#docUploadInput").files) fd.append("files", f, f.name);
  try {
    await api("/api/documents", { method: "POST", body: fd });
    $("#docUploadInput").value = "";
    refreshDocList();
  } catch (err) {
    alert("上传失败：" + err.message);
  }
});