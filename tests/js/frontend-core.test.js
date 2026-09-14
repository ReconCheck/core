/* Unit tests for the frontend's pure helpers (frontend-core.js).
   Run with:  node --test tests/js/   (CI does this automatically) */
"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "reconcheck", "web", "static", "frontend-core.js"),
  "utf8",
);
(0, eval)(src); // indirect eval: defines globalThis.RC exactly like the browser <script>
const RC = globalThis.RC;

test("kindOf: recognises both chains, tolerant of noise", () => {
  assert.equal(RC.kindOf("PO-240913-001.csv"), "po");
  assert.equal(RC.kindOf("采购订单123.xlsx"), "po");
  assert.equal(RC.kindOf("SO-240913-002.csv"), "so");
  assert.equal(RC.kindOf("SIV-240913-002.pdf"), "invoice");
  assert.equal(RC.kindOf("出库单.PDF"), "outbound");
  assert.equal(RC.kindOf("DN-240913-001.csv"), "delivery");
  assert.equal(RC.kindOf("random-notes.txt"), "unknown");
});

test("baseKey: strips kind tokens, groups one business number", () => {
  // siv/iv collision: "siv" must be stripped before "iv" (longest-first)
  assert.equal(RC.baseKey("SIV-240913-002.csv"), "240913002");
  assert.equal(RC.baseKey("INV-240913-002.csv"), "240913002");
  assert.equal(RC.baseKey("SO-240913-002.csv"), "240913002");
  assert.equal(RC.baseKey("OUT-240913-002.csv"), "240913002");
  assert.notEqual(RC.baseKey("PO-240913-001.csv"), RC.baseKey("PO-240913-002.csv"));
});

test("esc: neutralises HTML metacharacters", () => {
  assert.equal(RC.esc(`<img src=x onerror="alert(1)">`), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  assert.equal(RC.esc(null), "");
});

test("friendly: maps the common API errors to Chinese", () => {
  assert.match(RC.friendly("no comparable pairs found"), /没有可配对/);
  assert.match(RC.friendly("fetch failed: boom"), /拉取失败/);
  assert.match(RC.friendly("blocked target: loopback"), /拦截/);
  assert.equal(RC.friendly("totally unknown"), "totally unknown"); // passes through
});

test("fmtSize: humanises byte counts", () => {
  assert.equal(RC.fmtSize(500), "500 B");
  assert.equal(RC.fmtSize(2048), "2.0 KB");
  assert.equal(RC.fmtSize(5 * 1024 * 1024), "5.0 MB");
});
