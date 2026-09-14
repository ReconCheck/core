/* ReconCheck web UI — pure helpers (no DOM, no network).
   Loaded before app.js; unit-tested with `node --test tests/js` in CI. */
"use strict";

const RC = (() => {
  const KIND_TOKENS = {
    po: ["po", "purchase", "order", "采购", "订单", "订购"],
    so: ["so", "sales", "sale", "销售", "销单"],
    outbound: ["outbound", "out", "shipment", "ship", "出货", "出库", "发运"],
    invoice: ["invoice", "inv", "发票", "siv", "销项", "iv", "ir"],
    delivery: ["delivery", "dn", "送货", "收货", "asn", "发货"],
  };
  const KIND_LABEL = {
    po: "采购订单", so: "销售订单", outbound: "出库单",
    invoice: "发票", delivery: "送货单", unknown: "未识别",
  };

  function kindOf(name) {
    const base = String(name || "").replace(/\.[^.]+$/, "").toLowerCase();
    for (const [kind, tokens] of Object.entries(KIND_TOKENS)) {
      if (tokens.some((t) => base.includes(t))) return kind;
    }
    return "unknown";
  }

  function baseKey(name) {
    let stem = String(name || "").replace(/\.[^.]+$/, "");
    const tokens = Object.values(KIND_TOKENS)
      .flat()
      .sort((a, b) => b.length - a.length); // "siv" must strip before "iv"
    for (const t of tokens) stem = stem.replace(new RegExp(t, "ig"), "");
    return stem.toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  const FRIENDLY_MAP = [
    [/no comparable pairs found/i, "没有可配对的单据：文件名需要包含相同的业务编号，且每组至少两份"],
    [/at least two distinct documents/i, "至少需要两份不同的单据才能比对"],
    [/exactly two documents are required/i, "比对需要正好两份单据"],
    [/exactly three documents are required/i, "三方核对需要正好三份单据"],
    [/cannot compare a document with itself/i, "不能拿同一份单据和自己比对"],
    [/empty upload/i, "上传的是空文件"],
    [/file too large/i, "文件超过 64MB 上限"],
    [/data source returned no records/i, "数据源没有返回任何记录，请检查 records_path 与 record_id 设置"],
    [/document \S+ not found|document not found/i, "找不到该文档（可能已被删除），请重新选择"],
    [/no files or documents provided/i, "没有收到任何文件或文档"],
    [/ocr|scanned/i, "该 PDF 没有文本层（扫描件）：安装 pdf-ocr 扩展并设置 RECONCHECK_OCR=1，或改用带文本层的文件"],
    [/fetch failed/i, "从数据源拉取失败（网络或接口错误）"],
    [/blocked target/i, "数据源目标被拦截（仅允许公网 http/https，禁止内网/回环地址）"],
    [/valid JSON/i, "数据源返回的不是合法 JSON"],
    [/failed to fetch/i, "网络请求失败（服务不可达或跨域）"],
  ];

  function friendly(msg) {
    for (const [re, text] of FRIENDLY_MAP) if (re.test(msg)) return text;
    return msg;
  }

  return { KIND_TOKENS, KIND_LABEL, kindOf, baseKey, esc, fmtSize, friendly };
})();

if (typeof globalThis !== "undefined") {
  globalThis.RC = RC; // browser: used by app.js; node: used by --test
}
