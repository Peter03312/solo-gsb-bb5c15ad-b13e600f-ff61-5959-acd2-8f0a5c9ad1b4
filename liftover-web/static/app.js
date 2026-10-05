"use strict";

const PREVIEW_LIMIT = 1000;

const STATUS_TEXT = {
  mapped: "完全映射",
  partial: "部分映射",
  ambiguous: "歧义（多位置）",
  unmapped: "无映射",
};

const form = document.getElementById("lift-form");
const bedInput = document.getElementById("bed");
const chainInput = document.getElementById("chain");
const bedHint = document.getElementById("bed-hint");
const chainHint = document.getElementById("chain-hint");
const submitBtn = document.getElementById("submit-btn");
const errorBox = document.getElementById("error-box");
const errorList = document.getElementById("error-list");
const resultBox = document.getElementById("result-box");
const statsEl = document.getElementById("stats");
const tbody = document.getElementById("result-tbody");
const previewNote = document.getElementById("preview-note");
const downloadBtn = document.getElementById("download-btn");
const downloadAdjBtn = document.getElementById("download-adj-btn");
const adjVersionEl = document.getElementById("adj-version");
const filterBtns = document.querySelectorAll(".filter");

let currentToken = null;
let currentName = null;
let currentRows = [];
let activeFilter = "all";
// 人工裁决状态：版本号 + 各歧义行选择（source_line -> 候选序号；null=未裁决）
let currentVersion = 1;
let currentDecisions = {};
let adjBusy = false;

bedInput.addEventListener("change", () => {
  bedHint.textContent = bedInput.files[0]
    ? `${bedInput.files[0].name}（${formatSize(bedInput.files[0].size)}）`
    : "未选择文件";
});
chainInput.addEventListener("change", () => {
  chainHint.textContent = chainInput.files[0]
    ? `${chainInput.files[0].name}（${formatSize(chainInput.files[0].size)}）`
    : "未选择文件";
});

filterBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    activeFilter = btn.dataset.f;
    filterBtns.forEach((b) => b.classList.toggle("active", b === btn));
    renderRows();
  });
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  errorBox.classList.add("hidden");
  resultBox.classList.add("hidden");
  submitBtn.disabled = true;
  submitBtn.textContent = "转换中…";

  const fd = new FormData();
  fd.append("bed", bedInput.files[0]);
  fd.append("chain", chainInput.files[0]);
  fd.append("side", form.querySelector('input[name="side"]:checked').value);

  try {
    const resp = await fetch("/api/liftover", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) {
      showErrors(data.errors || [`请求失败（HTTP ${resp.status}）`]);
      return;
    }
    currentToken = data.download_token;
    currentName = data.download_name;
    currentRows = data.preview.rows;
    // 新结果尚无裁决：版本 1，所有歧义行未裁决
    currentVersion = data.adjudication_version || 1;
    currentDecisions = {};
    renderStats(data.preview);
    renderRows();
    resultBox.classList.remove("hidden");
    resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showErrors([`网络或服务错误：${err && err.message ? err.message : err}`]);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "校验并转换";
  }
});

downloadBtn.addEventListener("click", () => {
  if (!currentToken) return;
  // 原始结果：裁决不会改变该 TSV
  const a = document.createElement("a");
  a.href = `/api/download/${encodeURIComponent(currentToken)}`;
  a.download = currentName || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
});

downloadAdjBtn.addEventListener("click", () => {
  if (!currentToken) return;
  // 裁决版：已选行仅保留被选候选，未选歧义行标明“未裁决”
  const a = document.createElement("a");
  a.href = `/api/download/adjudicated/${encodeURIComponent(currentToken)}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
});

function applyAdjudicationState(data) {
  currentVersion = data.version;
  currentDecisions = {};
  for (const d of data.decisions || []) {
    currentDecisions[d.source_line] = d.selected_candidate;
  }
  if (Array.isArray(data.preview && data.preview.rows)) {
    currentRows = data.preview.rows;
  }
  adjVersionEl.textContent = currentVersion;
  renderRows();
}

async function refreshAdjudication() {
  const resp = await fetch(`/api/adjudication/${encodeURIComponent(currentToken)}`);
  if (resp.status === 404) {
    showErrors(["结果不存在或已过期，请重新提交转换"]);
    resultBox.classList.add("hidden");
    return false;
  }
  if (!resp.ok) {
    showErrors([`读取裁决状态失败（HTTP ${resp.status}）`]);
    return false;
  }
  applyAdjudicationState(await resp.json());
  return true;
}

async function submitAdjudication(lineNo, candidateIndex) {
  if (adjBusy || !currentToken) return;
  adjBusy = true;
  renderRows();
  try {
    const resp = await fetch(
      `/api/adjudication/${encodeURIComponent(currentToken)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_line: lineNo,
          candidate_index: candidateIndex,
          version: currentVersion,
        }),
      },
    );
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      applyAdjudicationState(data);
      return;
    }
    if (resp.status === 409) {
      // 并发修改抢先成功：同步当前选择后提示，行内选择不丢失可见性
      await refreshAdjudication();
      showErrors([
        (data.errors && data.errors[0])
          || `裁决版本冲突（当前版本已更新为 ${currentVersion}），已同步最新选择，请重试`,
      ]);
      return;
    }
    if (resp.status === 404) {
      showErrors(["结果不存在或已过期，请重新提交转换"]);
      resultBox.classList.add("hidden");
      return;
    }
    showErrors(data.errors || [`裁决提交失败（HTTP ${resp.status}）`]);
  } catch (err) {
    showErrors([`网络或服务错误：${err && err.message ? err.message : err}`]);
  } finally {
    adjBusy = false;
    renderRows();
  }
}

function showErrors(errors) {
  errorList.innerHTML = "";
  for (const msg of errors) {
    const li = document.createElement("li");
    li.textContent = msg;
    errorList.appendChild(li);
  }
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderStats(preview) {
  const c = preview.counts;
  statsEl.innerHTML = "";
  const items = [
    ["总数", preview.total, ""],
    ["完全映射", c.mapped, "mapped"],
    ["部分映射", c.partial, "partial"],
    ["歧义", c.ambiguous, "ambiguous"],
    ["无映射", c.unmapped, "unmapped"],
  ];
  for (const [label, n, cls] of items) {
    const div = document.createElement("div");
    div.className = `stat ${cls}`;
    div.innerHTML = `<b>${n}</b>${label}`;
    statsEl.appendChild(div);
  }
}

function buildAdjudicationCell(row) {
  const td = document.createElement("td");
  td.className = "adj-cell";
  if (row.status !== "ambiguous") {
    td.textContent = "—";
    return td;
  }

  const selected = Object.prototype.hasOwnProperty.call(currentDecisions, row.line_no)
    ? currentDecisions[row.line_no]
    : null;
  const radioName = `adj-${row.line_no}`;

  row.candidates.forEach((cand, ci) => {
    const ci1 = ci + 1;
    const label = document.createElement("label");
    label.className = "adj-option";
    if (selected === ci1) label.classList.add("selected");
    if (selected != null && selected !== ci1) label.classList.add("dimmed");

    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = radioName;
    radio.value = String(ci1);
    radio.checked = selected === ci1;
    radio.disabled = adjBusy;
    radio.addEventListener("change", () => submitAdjudication(row.line_no, ci1));

    const txt = document.createElement("span");
    txt.textContent = `候选 ${ci1}（chain ${cand.chain_id}）`;
    label.append(radio, txt);
    td.appendChild(label);
  });

  const actions = document.createElement("div");
  actions.className = "adj-actions";

  const undo = document.createElement("button");
  undo.type = "button";
  undo.className = "adj-undo";
  undo.textContent = selected == null ? "未裁决（撤销不可用）" : "撤销选择（恢复未裁决）";
  undo.disabled = adjBusy || selected == null;
  undo.addEventListener("click", () => submitAdjudication(row.line_no, null));

  const state = document.createElement("span");
  state.className = "adj-state";
  state.textContent = selected == null
    ? "状态：未裁决（全部候选保留）"
    : `状态：已选定候选 ${selected}`;

  actions.append(undo, state);
  td.appendChild(actions);
  return td;
}

function renderRows() {
  tbody.innerHTML = "";
  adjVersionEl.textContent = currentVersion;
  const rows = currentRows.filter((r) => activeFilter === "all" || r.status === activeFilter);
  const shown = rows.slice(0, PREVIEW_LIMIT);
  previewNote.textContent =
    currentRows.length > PREVIEW_LIMIT
      ? `预览仅显示前 ${PREVIEW_LIMIT} 行（当前筛选 ${rows.length} 行 / 共 ${currentRows.length} 行）；下载的 TSV 包含全部结果。`
      : `共 ${currentRows.length} 行源区间；多片段、歧义候选在“候选 / 片段”列内分行展开。歧义行可在右侧“人工裁决”列选择候选，下载裁决版时仅保留被选候选。`;

  for (const row of shown) {
    const tr = document.createElement("tr");
    if (row.status === "ambiguous"
        && Object.prototype.hasOwnProperty.call(currentDecisions, row.line_no)) {
      tr.classList.add("adj-resolved");
    }

    const tdLine = document.createElement("td");
    tdLine.className = "mono";
    tdLine.textContent = row.line_no;

    const tdSrc = document.createElement("td");
    tdSrc.className = "mono";
    tdSrc.textContent = `${row.src_chrom}:${row.src_start}-${row.src_end}`;

    const tdStatus = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${row.status}`;
    badge.textContent = STATUS_TEXT[row.status] || row.status;
    tdStatus.appendChild(badge);

    const selected = Object.prototype.hasOwnProperty.call(currentDecisions, row.line_no)
      ? currentDecisions[row.line_no]
      : null;

    const tdDest = document.createElement("td");
    if (row.candidates.length === 0) {
      tdDest.textContent = "—";
    } else {
      row.candidates.forEach((cand, ci) => {
        const ci1 = ci + 1;
        // 已裁决行：页面预览只突出被选候选，其余折叠置灰
        if (row.status === "ambiguous" && selected != null && ci1 !== selected) {
          const skipped = document.createElement("div");
          skipped.className = "cand cand-skipped";
          skipped.textContent = `候选 ${ci1}（chain ${cand.chain_id}）：未选定，裁决版中不输出`;
          tdDest.appendChild(skipped);
          return;
        }
        const div = document.createElement("div");
        div.className = "cand";
        if (row.candidates.length > 1) {
          const head = document.createElement("div");
          head.className = "cand-head";
          head.textContent =
            `候选 ${ci1}/${row.candidates.length} · chain ${cand.chain_id}`
            + (row.status === "ambiguous" && selected === ci1 ? " · 已选定" : "");
          div.appendChild(head);
        }
        cand.fragments.forEach((frag, fi) => {
          const fl = document.createElement("div");
          fl.className = "frag-line";
          const prefix = cand.fragments.length > 1
            ? `片段${fi + 1}(${frag.src_start}-${frag.src_end}) → `
            : "";
          fl.textContent =
            `${prefix}${cand.dest_chrom}:${frag.dest_start}-${frag.dest_end} ` +
            `(${cand.dest_strand})`;
          div.appendChild(fl);
        });
        tdDest.appendChild(div);
      });
    }

    const tdReason = document.createElement("td");
    tdReason.className = "reason";
    tdReason.textContent = row.reason || "";

    tr.append(tdLine, tdSrc, tdStatus, tdDest, tdReason, buildAdjudicationCell(row));
    tbody.appendChild(tr);
  }
}

function formatSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}
