/* ===========================================================
   审查 Agent 控制台 —— 四页交互
   =========================================================== */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const APP_CONFIG = window.REVIEW_AGENT_CONFIG || {};
const API_BASE = String(APP_CONFIG.apiBase || "").replace(/\/+$/, "");

const DIM = {
  fact: ["事实", "dim-fact"],
  safety: ["安全", "dim-safety"],
  compliance: ["合规", "dim-compliance"],
  style: ["风格", "dim-style"],
};
const SEV = {
  blocker: ["BLOCKER", "sev-blocker"],
  major: ["MAJOR", "sev-major"],
  minor: ["MINOR", "sev-minor"],
  nit: ["NIT", "sev-minor"],
};
const VERDICT = {
  fail: ["FAIL", "verdict-fail", "dot-red"],
  warn: ["WARN", "verdict-warn", "dot-yellow"],
  // 不用 PASS：这个引擎有已知漏报，"通过"是它给不起的结论
  pass: ["未发现", "verdict-pass", "dot-green"],
};

const state = {
  targets: [],
  selected: null,
  reports: [],
  currentReport: null,
  currentLog: null,
  rules: [],
  ruleFilter: "all",
  agents: [],
  agent: "auto",
  llm: null,
  llmHistory: [],
  providers: [],
  providerChanged: false,
  integrity: null,
  job: null,
  poll: null,
  tab: "review",
};

/* ---------------- 基础工具 ---------------- */
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function api(path, opts) {
  return fetch(API_BASE + path, opts).then(async (res) => {
    try {
      return await res.json();
    } catch (e) {
      return { ok: false, error: `响应解析失败（HTTP ${res.status}）` };
    }
  });
}

let toastTimer = null;
function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3600);
}

function fmtBytes(size) {
  const n = Number(size) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function fmtClock(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function relTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff >= 0 && diff < 60) return "刚刚";
  if (diff >= 0 && diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  const p = (n) => String(n).padStart(2, "0");
  const clock = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return clock;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${clock}`;
  return `${d.getMonth() + 1}-${p(d.getDate())} ${clock}`;
}

function basename(path) {
  return String(path || "").split("/").pop();
}

/* ---------------- Tab 切换 ---------------- */
function switchTab(name) {
  state.tab = name;
  $$(".nav-tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === name));
  $$(".page").forEach((page) => page.classList.toggle("active", page.id === `page-${name}`));
  if (location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
  if (name === "logs") loadLogs(state.currentLog);
  if (name === "ledger") loadIntegrity();
  if (name === "rules") loadRules();
}

function switchLog(name) {
  $$(".log-subtab").forEach((btn) => btn.classList.toggle("active", btn.dataset.log === name));
  $$(".log-content").forEach((panel) =>
    panel.classList.toggle("active", panel.id === `log-${name}`)
  );
}

/* ---------------- 待审对象 / 上传 ---------------- */
async function loadTargets(preferRel) {
  const data = await api("/api/targets");
  state.targets = data.items || [];
  const select = $("#targetSelect");
  select.innerHTML = "";
  if (!state.targets.length) {
    select.innerHTML = '<option value="">暂无文件</option>';
    selectTarget(null);
    return;
  }
  for (const item of state.targets) {
    const option = document.createElement("option");
    option.value = item.rel;
    option.textContent = `${item.uploaded ? "[上传] " : ""}${item.name} · ${fmtBytes(item.size)}`;
    select.appendChild(option);
  }
  const rel = preferRel || (state.selected && state.selected.rel) || state.targets[0].rel;
  selectTarget(rel);
}

function selectTarget(rel) {
  const item = state.targets.find((x) => x.rel === rel) || null;
  state.selected = item;
  const select = $("#targetSelect");
  if (select && item) select.value = item.rel;
  renderFileChip();
  updateRunState();
}

function renderFileChip() {
  const chip = $("#fileChip");
  if (!state.selected) {
    chip.classList.add("hidden");
    return;
  }
  chip.classList.remove("hidden");
  $("#fileName").textContent = state.selected.name;
  $("#fileMeta").textContent = `${fmtBytes(state.selected.size)} · ${state.selected.mtime || ""}`;
}

/* 二进制类（PDF / Word / 图片 / 压缩包）的体积上限，与后端一致 */
const BINARY_EXT_RE = /\.(pdf|docx|docm|doc|rtf|png|jpe?g|gif|bmp|webp|tiff?|heic|zip)$/i;
const TEXT_MAX_BYTES = 2 * 1024 * 1024;
const BINARY_MAX_BYTES = 20 * 1024 * 1024;

async function uploadFile(file) {
  if (!file) return;
  const limit = BINARY_EXT_RE.test(file.name) ? BINARY_MAX_BYTES : TEXT_MAX_BYTES;
  if (file.size === 0) { toast("文件为空", true); return; }
  if (file.size > limit) {
    toast(`文件超过 ${Math.round(limit / 1024 / 1024)} MB 限制`, true);
    return;
  }
  const box = $("#uploadBox");
  box.classList.add("drag");
  try {
    const res = await fetch(
      `${API_BASE}/api/upload?name=${encodeURIComponent(file.name)}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }
    );
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!data || !data.ok) {
      toast((data && data.error) || `上传失败（HTTP ${res.status}）`, true);
      return;
    }
    const first = data.file;
    if (first) await loadTargets(first.rel);
    const count = (data.extracted || []).length;
    if (!first) {
      toast(`已接收 ${file.name}，但未产生可审对象（${data.kind === "image" ? "图片需视觉模型或人工复核" : "未提取到文本"}）`, true);
    } else if (count > 1) {
      toast(`已上传 ${first.name}，共解出 ${count} 个待审对象`);
    } else {
      toast(`已上传：${first.name}`);
    }
    const warns = data.warnings || [];
    if (warns.length) {
      setTimeout(() => toast(`摄取提示：${warns[0]}`, true), 600);
    }
  } catch (e) {
    toast("上传失败：" + e.message, true);
  } finally {
    box.classList.remove("drag");
    $("#fileInput").value = "";
  }
}

function initUpload() {
  const input = $("#fileInput");
  const box = $("#uploadBox");
  box.addEventListener("click", () => input.click());
  input.addEventListener("change", () => uploadFile(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    box.addEventListener(ev, () => box.classList.remove("drag"))
  );
  box.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
  $("#fileRemove").addEventListener("click", (e) => {
    e.stopPropagation();
    selectTarget(null);
  });
}

/* ---------------- 历史报告 ---------------- */
async function loadReports() {
  const data = await api("/api/reports");
  state.reports = data.items || [];
  $("#historyTitle").textContent = `历史报告（${state.reports.length}）`;
  renderHistory();
  populateLogSelect();
  if (!state.currentReport && state.reports.length) {
    await selectReport(state.reports[0].id, false);
  }
}

function statsPills(item) {
  const stats = item.stats || {};
  const pills = [];
  ["blocker", "major", "minor", "nit"].forEach((key) => {
    if (stats[key]) pills.push(`<span class="stat-pill">${stats[key]} ${key}</span>`);
  });
  if (!pills.length) pills.push('<span class="stat-pill">0 issues</span>');
  return pills.join("");
}

/* 拖拽排序 / 删除的运行时状态 */
let dragId = null;
let suppressRowClick = false;
let pendingDeleteId = null;

function renderHistory() {
  const list = $("#historyList");
  if (!state.reports.length) {
    list.innerHTML = '<div class="history-empty">暂无历史报告</div>';
    return;
  }
  list.innerHTML = state.reports.map((item) => {
    const verdict = VERDICT[item.verdict] || ["—", "verdict-warn", "dot-gray"];
    const active = state.currentReport && state.currentReport.review_id === item.id;
    return `<div class="history-row${active ? " active" : ""}" data-id="${esc(item.id)}" draggable="true" title="按住手柄上下拖动可调整顺序">
      <span class="drag-handle" aria-hidden="true">⋮⋮</span>
      <div class="history-main">
        <div class="history-id"><span class="status-dot ${verdict[2]}"></span>${esc(item.id)}</div>
        <div class="history-file">${esc(item.target_name || basename(item.target_path))} · ${relTime(item.timestamp)}</div>
        <div class="history-stats">${statsPills(item)}</div>
      </div>
      <button type="button" class="history-del" data-del="${esc(item.id)}" title="删除该报告" aria-label="删除 ${esc(item.id)}">🗑</button>
    </div>`;
  }).join("");
  bindHistoryEvents();
}

function clearDropMarks() {
  $$("#historyList .history-row").forEach((r) => r.classList.remove("drop-before", "drop-after"));
}

function bindHistoryEvents() {
  const list = $("#historyList");
  list.querySelectorAll(".history-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest(".history-del")) return;
      if (suppressRowClick) return;
      selectReport(row.dataset.id, false);
    });
    row.addEventListener("dragstart", (e) => {
      dragId = row.dataset.id;
      row.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", dragId); } catch (err) { /* 浏览器可能限制 */ }
      }
    });
    row.addEventListener("dragend", () => {
      dragId = null;
      row.classList.remove("dragging");
      clearDropMarks();
      suppressRowClick = true;
      setTimeout(() => { suppressRowClick = false; }, 0);
    });
    row.addEventListener("dragover", (e) => {
      if (!dragId || row.dataset.id === dragId) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      const rect = row.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      clearDropMarks();
      row.classList.add(after ? "drop-after" : "drop-before");
    });
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const from = dragId;
      const to = row.dataset.id;
      const after = row.classList.contains("drop-after");
      clearDropMarks();
      if (!from || !to || from === to) return;
      moveReportInList(from, to, after);
    });
  });
  list.querySelectorAll(".history-del").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      askDeleteReport(btn.dataset.del);
    });
    btn.addEventListener("dragstart", (e) => e.stopPropagation());
  });
}

/** 把 from 移动到 to 的位置（after=true 表示放到 to 之后） */
function moveReportInList(from, to, after) {
  const ids = state.reports.map((r) => r.id);
  const fromIdx = ids.indexOf(from);
  if (fromIdx < 0) return;
  ids.splice(fromIdx, 1);
  let toIdx = ids.indexOf(to);
  if (toIdx < 0) toIdx = ids.length - 1;
  if (after) toIdx += 1;
  ids.splice(toIdx, 0, from);
  const byId = new Map(state.reports.map((r) => [r.id, r]));
  state.reports = ids.map((id) => byId.get(id)).filter(Boolean);
  renderHistory();
  persistReportOrder();
}

async function persistReportOrder() {
  const order = state.reports.map((r) => r.id);
  const data = await api("/api/reports/order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order }),
  });
  if (!data.ok) {
    toast(data.error || "保存排序失败", true);
    return;
  }
  toast("排序已保存");
}

/* ---------------- 删除历史报告（二次确认） ---------------- */
function askDeleteReport(id) {
  const item = state.reports.find((r) => r.id === id);
  pendingDeleteId = id;
  $("#confirmSub").textContent = item
    ? `${id} · ${item.target_name || basename(item.target_path)} · ${item.issues} 条异议 · ${item.review_mode || "-"}`
    : id;
  $("#confirmOk").disabled = false;
  $("#confirmModal").classList.remove("hidden");
}

function closeConfirm() {
  pendingDeleteId = null;
  $("#confirmModal").classList.add("hidden");
}

async function confirmDeleteReport() {
  const id = pendingDeleteId;
  if (!id) return;
  const btn = $("#confirmOk");
  btn.disabled = true;
  // 服务端同样要求 confirm=id，UI 的二次确认不是唯一防线
  const data = await api("/api/report?id=" + encodeURIComponent(id), {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: id }),
  });
  btn.disabled = false;
  if (!data.ok) {
    if (data.need_confirm) {
      closeConfirm();
      toast(data.error || "需要二次确认", true);
      return;
    }
    toast(data.error || "删除失败", true);
    return;
  }
  closeConfirm();
  if (state.currentReport && state.currentReport.review_id === id) {
    state.currentReport = null;
    state.currentLog = null;
  }
  await loadReports();
  await loadIntegrity();
  toast(`已删除 ${id}（文件在 outputs/.trash/ 可找回）`);
}

async function selectReport(id, switchToLogs) {
  const data = await api("/api/report?id=" + encodeURIComponent(id));
  if (!data.ok) { toast(data.error || "读取报告失败", true); return; }
  state.currentReport = data.report;
  state.currentLog = id;
  renderReport(data.report);
  renderHistory();
  if ($("#logSelect")) $("#logSelect").value = id;
  if (state.targets.some((t) => t.rel === data.report.target_path)) {
    selectTarget(data.report.target_path);
  }
  if (!state.job || state.job.status !== "running") {
    state.job = null;
    renderProgress([], "");
  }
  if (switchToLogs) switchTab("logs");
  else if (state.tab === "logs") loadLogs(id);
}

/* ---------------- 原文标注 + 问题汇总 ---------------- */
const DIM_FULL = {
  fact: "事实", logic: "逻辑", safety: "安全", compliance: "合规", style: "风格",
};
const SEV_RANK = { blocker: 0, major: 1, minor: 2, nit: 3 };

/**
 * 用报告里的 Agent 元数据补齐维度标签。
 *
 * 维度不再由前端写死：安全审 Agent 的「凭据/注入/配置/依赖」在旧前端里
 * 会直接显示成英文 id，图例也会缺色——这类"加一个 Agent 就得改前端"的耦合
 * 正是要拆掉的，所以标签从 report.agent.dimensions 读。
 */
function applyAgentMeta(report) {
  const meta = (report && report.agent && report.agent.dimensions) || [];
  meta.forEach((d) => {
    if (!d || !d.id) return;
    DIM_FULL[d.id] = d.label || d.id;
    if (!DIM[d.id]) DIM[d.id] = [d.label || d.id, `dim-${d.id}`];
  });
}

/* 原文标注锚点：issue_id → 该问题在原文中的全部 <mark>（同一 quote 可能出现多次） */
let annoAnchors = new Map();
/* 每个问题当前跳转到第几处命中（重复点击可循环到下一处） */
let annoCursor = {};
const NAV_HEIGHT = 56;   // sticky 顶部导航高度，跳转时需避让

/**
 * 统一编号：按维度分组，组内按 严重度 → 置信度 → 编号 排序。
 * 返回的 index 供「原文角标」与「问题汇总表」共用，
 * 保证角标上的「逻辑3」在汇总表里也能找到同一条。
 */
function buildIssueIndex(issues) {
  const groups = {};
  issues.forEach((it) => {
    (groups[it.dimension] = groups[it.dimension] || []).push(it);
  });
  const preferred = ["logic", "fact", "safety", "compliance", "style"];
  const dims = preferred.filter((d) => groups[d])
    .concat(Object.keys(groups).filter((d) => !preferred.includes(d)));
  const bySev = (a, b) =>
    (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9)
    || (b.confidence || 0) - (a.confidence || 0)
    || String(a.issue_id || "").localeCompare(String(b.issue_id || ""));
  const index = new Map();
  const ordered = {};
  dims.forEach((d) => {
    const list = groups[d].slice().sort(bySev);
    ordered[d] = list;
    list.forEach((it, i) => {
      index.set(it.issue_id, {
        dim: d, seq: i + 1, label: `${DIM_FULL[d] || d}${i + 1}`,
      });
    });
  });
  return { index, dims, ordered };
}

function renderAnnotatedText(report) {
  const block = $("#annotatedBlock");
  const text = report.target_text || "";
  const issues = report.issues || [];
  // 切换报告时，上一份留下的类型选择器不能残留
  closeIssueChooser();
  if (!text) {
    block.classList.add("hidden");
    return;
  }
  block.classList.remove("hidden");
  const { index } = buildIssueIndex(issues);

  // 定位所有问题片段（同一 quote 可能出现多次，逐个标注）
  const marks = [];
  issues.forEach((it) => {
    const q = it.quote || "";
    if (!q) return;
    let from = 0, idx;
    while ((idx = text.indexOf(q, from)) >= 0) {
      marks.push({ start: idx, end: idx + q.length, issue: it });
      from = idx + q.length;
      if (marks.length > 500) break;
    }
  });
  // 排序：起点靠前的优先，同起点取更长的（更长的更有代表性）
  marks.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  // 合并重叠区间：**不能丢弃**——同一段原文常被多个维度同时引用
  // （例如同一句既是「事实不符」又是「逻辑冲突」），丢掉就等于漏报。
  const merged = [];
  for (const m of marks) {
    const last = merged[merged.length - 1];
    if (last && m.start < last.end) {
      last.end = Math.max(last.end, m.end);
      last.items.push(m.issue);
    } else {
      merged.push({ start: m.start, end: m.end, items: [m.issue] });
    }
  }

  let html = "", pos = 0, segNo = 0;
  const anchorMap = new Map();
  for (const seg of merged) {
    html += esc(text.slice(pos, seg.start));
    const it = seg.items[0];
    const tip = seg.items.map((x) => {
      const d = DIM_FULL[x.dimension] || x.dimension;
      const n = index.get(x.issue_id);
      return `${n ? n.label : ""} ${x.issue_id} ${d}｜${x.rule_id}｜${x.severity}｜置信 ${x.confidence ?? "-"}`
        + `${x.open_discovery ? "｜规则清单外发现" : ""}\n${x.evidence || ""}`;
    }).join("\n——\n");
    const more = seg.items.length > 1 ? `（此处共 ${seg.items.length} 个问题）` : "";
    // 角标：显示「这是第几个XX问题」，与下方汇总表一一对应
    // 多类型命中时，每个角标都能单独点击 → 只跳这一条（单类型跳转）
    const badges = seg.items.map((x) => {
      const n = index.get(x.issue_id);
      if (!n) return "";
      return `<sup class="anno-badge pickable dim-${esc(x.dimension)}`
        + `${x.open_discovery ? " is-open" : ""}"`
        + ` data-issue="${esc(x.issue_id)}"`
        + ` title="只看「${esc(n.label)}」这一条">${esc(n.label)}</sup>`;
    }).join("");
    // 锚点：记录该问题在这处原文中的位置，供汇总表点击跳转
    segNo += 1;
    const markId = `anno-${segNo}`;
    const ids = [];
    seg.items.forEach((x) => {
      if (!x.issue_id) return;
      ids.push(x.issue_id);
      if (!anchorMap.has(x.issue_id)) anchorMap.set(x.issue_id, []);
      anchorMap.get(x.issue_id).push(markId);
    });
    html += `<mark class="anno dim-${esc(it.dimension)} sev-${esc(it.severity)}`
      + `${it.open_discovery ? " is-open" : ""}" id="${markId}"`
      + ` data-issues="${esc(ids.join(","))}"`
      + ` title="${esc(tip + more)}`
      + (seg.items.length > 1
        ? `\n（点击段落可选出要看的类型，或直接点角标）`
        : `\n（点击定位到下方问题汇总）`) + `">`
      + `${esc(text.slice(seg.start, seg.end))}${badges}</mark>`;
    pos = seg.end;
  }
  html += esc(text.slice(pos));
  $("#annotatedText").innerHTML = html;
  annoAnchors = anchorMap;
  annoCursor = {};

  // 反向定位：点原文里的高亮 → 回跳并高亮汇总表对应行
  const box = $("#annotatedText");
  box.classList.remove("is-focusing");
  setAnnoHint("");
  if (!box.dataset.backBound) {
    box.dataset.backBound = "1";
    box.addEventListener("click", (e) => {
      // ① 点角标：只针对这一个问题（单类型跳转）
      const badge = e.target.closest("sup.anno-badge[data-issue]");
      if (badge) {
        e.stopPropagation();
        const id = badge.dataset.issue;
        const mark = badge.closest("mark.anno");
        closeIssueChooser();
        markActiveBadge(mark, id);
        const ok = focusSummaryRows([id], true);
        if (!ok) { toast("问题汇总中没有对应条目", true); return; }
        setAnnoHint(`已只看「${badge.textContent.trim()}」这一条`);
        return;
      }
      const mark = e.target.closest("mark.anno");
      if (!mark) return;
      const ids = (mark.dataset.issues || "").split(",").filter(Boolean);
      if (!ids.length) return;
      // ② 一句话命中多类问题：让用户选要跳哪一类，而不是一股脑全高亮
      if (ids.length > 1) {
        showIssueChooser(mark, ids);
        return;
      }
      // ③ 单类型：直接回跳
      markActiveBadge(mark, ids[0]);
      const ok = focusSummaryRows(ids, true);
      if (!ok) toast("问题汇总中没有对应条目", true);
      else setAnnoHint("已定位到汇总表中的对应条目");
    });
  }

  const counts = {};
  issues.forEach((it) => { counts[it.dimension] = (counts[it.dimension] || 0) + 1; });
  $("#annoLegend").innerHTML =
    Object.keys(counts).map((d) =>
      `<span class="legend-chip"><i class="legend-dot dim-${esc(d)}"></i>`
      + `${esc(DIM_FULL[d] || d)} ${counts[d]}</span>`).join("")
    + `<span class="legend-note">共 ${issues.length} 条问题，命中 ${merged.length} 处位置`
    + `（同段原文被多维命中会合并，悬停看全部）· 颜色=类型 · 下划线=严重度 · 角标=该类型第几条`
    + ` · 点汇总表可跳到原文；点原文可回跳汇总表`
    + `（同句多类型时点角标只跳该条，点整段会弹出类型选择）</span>`;
}

/* ---------------- 汇总表 ↔ 原文 双向定位 ---------------- */

let annoFocusTimer = null;
let sumFocusTimer = null;

/** 把元素带进视口（避让 56px sticky 导航） */
function revealInViewport(el) {
  if (!el) return;
  const r = el.getBoundingClientRect();
  const topSafe = NAV_HEIGHT + 12;
  const needsScroll = r.top < topSafe || r.top > window.innerHeight * 0.55;
  if (!needsScroll) return;
  const top = window.pageYOffset + r.top - topSafe;
  window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

/**
 * 计算「让该标注按规则落位」所需的容器 scrollTop。
 * 规则：片段比可视区还高 → 顶部对齐；否则垂直居中。
 * 用 rect 差值而非 offsetTop —— offsetParent 是哪个元素并不确定
 * （.anno-text 没有 position，offsetTop 会以 body 为参照，直接算会偏几千像素）。
 */
function desiredScrollTop(mark, box) {
  const boxRect = box.getBoundingClientRect();
  const r = mark.getBoundingClientRect();
  const relTop = r.top - boxRect.top;
  return r.height > box.clientHeight * 0.7
    ? box.scrollTop + relTop - 14
    : box.scrollTop + relTop - (box.clientHeight - r.height) / 2;
}

/** 确保标注本身没被 sticky 导航或视口边缘遮住（闭环校正页面滚动） */
function ensureMarkInViewport(mark) {
  const r = mark.getBoundingClientRect();
  const topSafe = NAV_HEIGHT + 16;
  const bottomSafe = window.innerHeight - 16;
  if (r.top < topSafe) {
    window.scrollBy({ top: r.top - topSafe, behavior: "auto" });
  } else if (r.bottom > bottomSafe) {
    const delta = Math.min(r.bottom - bottomSafe, r.top - topSafe);
    window.scrollBy({ top: delta, behavior: "auto" });
  }
}

/** 让某处标注在原文框内精确落位：先平滑滚动，再轮询闭环补差 */
function centerMarkInBox(mark, box) {
  const go = () => Math.max(0, desiredScrollTop(mark, box));
  box.scrollTo({ top: go(), behavior: "smooth" });
  // 平滑滚动是异步的：轮询到落点稳定为止，避免"动画还没停就写死位置"，
  // 也避免"完全不校验、落点靠猜"。最多 4 次后强制对齐。
  let tries = 0;
  const tick = () => {
    tries += 1;
    ensureMarkInViewport(mark);
    const gap = go() - box.scrollTop;
    if (Math.abs(gap) <= 2) return;          // 已到位
    if (tries >= 4) { box.scrollTop = go(); return; }  // 兜底：直接写死
    setTimeout(tick, 140);
  };
  setTimeout(tick, 260);
}

/* ---------------- 单类型跳转：同句多类型时的选择器 ---------------- */

let issueChooserEl = null;

function closeIssueChooser() {
  if (issueChooserEl) { issueChooserEl.remove(); issueChooserEl = null; }
  document.removeEventListener("mousedown", onChooserOutside, true);
  document.removeEventListener("keydown", onChooserKey, true);
}
function onChooserOutside(e) {
  if (issueChooserEl && !issueChooserEl.contains(e.target)) closeIssueChooser();
}
function onChooserKey(e) { if (e.key === "Escape") closeIssueChooser(); }

/** 高亮某个角标，表示"当前看的是这一条"；其余角标恢复常态 */
function markActiveBadge(mark, issueId) {
  const box = $("#annotatedText");
  if (!box) return;
  box.querySelectorAll("sup.anno-badge.is-picked")
    .forEach((b) => b.classList.remove("is-picked"));
  if (!mark || !issueId) return;
  const b = mark.querySelector(`sup.anno-badge[data-issue="${issueId}"]`);
  if (b) b.classList.add("is-picked");
}

/** 一句话命中多类问题时，弹出类型选择器（而不是一次性全高亮） */
function showIssueChooser(mark, ids) {
  closeIssueChooser();
  const items = ids.map((id) => {
    const b = mark.querySelector(`sup.anno-badge[data-issue="${id}"]`);
    const dim = b ? (String(b.className).match(/dim-([a-z]+)/) || [])[1] || "" : "";
    return {
      id,
      label: b ? b.textContent.trim() : id,
      dim,
      dimText: DIM_FULL[dim] || dim,
    };
  });

  const panel = document.createElement("div");
  panel.className = "anno-chooser";
  panel.innerHTML = `<div class="anno-chooser-title">该句命中 ${items.length} 类问题，选择一条</div>`
    + items.map((x) => `<button type="button" class="anno-chooser-item" data-issue="${esc(x.id)}">`
      + `<i class="legend-dot dim-${esc(x.dim)}"></i>`
      + `<span class="chooser-label">${esc(x.label)}</span>`
      + `<span class="chooser-dim">${esc(x.dimText)}</span>`
      + "</button>").join("");
  document.body.appendChild(panel);

  // 定位：贴近该句；超出视口就翻转或夹取，避免弹到屏幕外
  const r = mark.getBoundingClientRect();
  const pr = panel.getBoundingClientRect();   // 统一用 rect，不用 offsetWidth/Height
  const pw = pr.width, ph = pr.height;
  const left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - pw - 8));
  let top = r.bottom + 6;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;

  issueChooserEl = panel;
  panel.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-issue]");
    if (!btn) return;
    const id = btn.dataset.issue;
    const label = btn.querySelector(".chooser-label").textContent;
    closeIssueChooser();
    markActiveBadge(mark, id);
    if (!focusSummaryRows([id], true)) toast("问题汇总中没有对应条目", true);
    else setAnnoHint(`已只看「${label}」这一条`);
  });
  // 延迟绑定，避免"打开选择器的这次点击"立刻把选择器关掉
  setTimeout(() => {
    document.addEventListener("mousedown", onChooserOutside, true);
    document.addEventListener("keydown", onChooserKey, true);
  }, 0);
  const first = panel.querySelector("button");
  if (first) first.focus();
}

/**
 * 聚焦某一处标注：调暗其余高亮，只让目标句子保持满亮度并闪烁。
 * 这是「一堆高亮里找不到目标」的解法——不是加强目标，而是压暗背景。
 */
function setAnnoFocus(mark, issueId) {
  const box = $("#annotatedText");
  if (!box) return;
  box.querySelectorAll("mark.is-flash, mark.is-target")
    .forEach((el) => el.classList.remove("is-flash", "is-target"));
  clearTimeout(annoFocusTimer);
  if (!mark) { box.classList.remove("is-focusing"); return; }
  box.classList.add("is-focusing");
  mark.classList.add("is-target");
  void mark.offsetWidth;   // 强制重排：连点同一处也能重播动画
  mark.classList.add("is-flash");
  // 同句多类型时，只点亮被定位的那一个角标
  markActiveBadge(mark, issueId);
  annoFocusTimer = setTimeout(() => {
    mark.classList.remove("is-flash", "is-target");
    box.classList.remove("is-focusing");
    box.querySelectorAll("sup.anno-badge.is-picked")
      .forEach((b) => b.classList.remove("is-picked"));
  }, 2600);
}

/** 滚动到原文中的某处标注并聚焦；重复点击同一行时循环到下一处命中 */
function jumpToIssue(issueId) {
  const marks = annoAnchors.get(issueId);
  if (!marks || !marks.length) {
    toast("该问题在原文中没有可定位的引用片段", true);
    return;
  }
  const cur = annoCursor[issueId] || 0;
  const markId = marks[cur % marks.length];
  annoCursor[issueId] = cur + 1;

  const mark = document.getElementById(markId);
  if (!mark) return;
  const box = $("#annotatedText");

  revealInViewport($("#annotatedBlock"));
  centerMarkInBox(mark, box);
  setAnnoFocus(mark, issueId);

  // 多处命中时告诉用户「还有下一处」，否则用户以为跳错了
  if (marks.length > 1) {
    const next = (cur % marks.length) + 1;
    setAnnoHint(`「${issueId}」共 ${marks.length} 处命中，当前第 ${next} 处 —— 再点一次看下一处`);
  } else {
    setAnnoHint(`已定位到「${issueId}」在原文中的位置`);
  }
}

/** 原文标注区的提示行（跳转反馈：命中处数 / 当前位置） */
function setAnnoHint(text) {
  const el = $("#annoHint");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("on", Boolean(text));
  clearTimeout(setAnnoHint._timer);
  if (text) {
    setAnnoHint._timer = setTimeout(() => {
      el.classList.remove("on");
      el.textContent = "";
    }, 4200);
  }
}

/** 高亮汇总表对应行（点原文 → 回跳汇总表用）；rows 在汇总表内滚动可见 */
function focusSummaryRows(issueIds, dimOthers) {
  const host = $("#issueSummary");
  if (!host) return false;
  host.querySelectorAll("tr.sum-row.is-active, tr.sum-row.is-row-flash")
    .forEach((r) => r.classList.remove("is-active", "is-row-flash"));
  clearTimeout(sumFocusTimer);
  host.classList.remove("is-focusing");

  const rows = issueIds.map((id) => host.querySelector(`tr.sum-row[data-issue-id="${id}"]`))
    .filter(Boolean);
  if (!rows.length) return false;

  rows.forEach((r) => { r.classList.add("is-active"); r.classList.add("is-row-flash"); });
  if (dimOthers) host.classList.add("is-focusing");
  revealInViewport(rows[0]);

  sumFocusTimer = setTimeout(() => {
    rows.forEach((r) => r.classList.remove("is-row-flash"));
    host.classList.remove("is-focusing");
  }, 2600);
  return true;
}

function renderIssueSummary(report) {
  const block = $("#summaryBlock");
  const issues = report.issues || [];
  if (!issues.length) { block.classList.add("hidden"); return; }
  block.classList.remove("hidden");
  const { index, dims, ordered } = buildIssueIndex(issues);
  // 旧报告没有原文快照 → 无处可定位，明说原因而不是点了没反应
  const noSnapshot = !(report.target_text || "").trim();
  if (noSnapshot) {
    block.classList.add("has-no-anno");
  } else {
    block.classList.remove("has-no-anno");
  }

  $("#issueSummary").innerHTML = dims.map((d) => {
    const list = ordered[d];
    return `<div class="sum-group">
      <div class="sum-group-head">
        <span class="sum-dot dim-${esc(d)}"></span>
        <span class="sum-dim">${esc(DIM_FULL[d] || d)}</span>
        <span class="sum-count">${list.length} 条</span>
      </div>
      <table class="sum-table">
        <thead><tr><th>编号</th><th>严重度</th><th>规则</th><th>位置</th><th>问题摘要</th><th>${noSnapshot ? "定位" : "定位 ↓"}</th></tr></thead>
        <tbody>${list.map((it) => {
          const n = index.get(it.issue_id);
          const hits = (annoAnchors.get(it.issue_id) || []).length;
          const loc = hits > 0
            ? `<span class="sum-loc" title="点击跳转到原文中的标注位置${hits > 1 ? `（共 ${hits} 处，再次点击可继续跳）` : ""}">定位 →</span>`
            : `<span class="sum-loc none" title="该问题在原文中没有可定位的引用片段">—</span>`;
          return `<tr class="sum-row${hits ? "" : " no-loc"}" data-issue-id="${esc(it.issue_id || "")}"
            title="${hits ? "点击定位到原文中的对应标注" : "该问题在原文中没有可定位的引用片段"}">
            <td><span class="seq-badge dim-${esc(d)}">${esc(n ? n.label : "")}</span></td>
            <td><span class="sev-tag sev-${esc(it.severity)}">${esc(it.severity)}</span></td>
            <td class="mono">${esc(it.rule_id)}${it.open_discovery ? ' <span class="open-tag">规则外</span>' : ""}</td>
            <td class="mono-dim">${esc(it.location || "")}</td>
            <td>${esc(String(it.evidence || it.quote || "").slice(0, 110))}</td>
            <td class="sum-loc-cell">${loc}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`;
  }).join("");

  // 事件委托：一次绑定对全部汇总行生效（表格内容会被重新渲染，不能逐行绑定）
  const host = $("#issueSummary");
  // 报告级状态必须挂在 DOM 上：委托只绑一次，闭包会固化成第一份报告的值
  host.dataset.noSnapshot = noSnapshot ? "1" : "0";
  if (!host.dataset.jumpBound) {
    host.dataset.jumpBound = "1";
    host.addEventListener("click", (e) => {
      const row = e.target.closest("tr.sum-row");
      if (!row) return;
      const id = row.dataset.issueId;
      if (!id) return;
      if (host.dataset.noSnapshot === "1") {
        toast("该报告生成时未保存原文快照，无法定位；请重新审查一次", true);
        return;
      }
      if (!(annoAnchors.get(id) || []).length) {
        toast("该问题在原文中没有可定位的引用片段", true);
        return;
      }
      // 行选中态高亮，与原文闪烁同步；清掉反向操作留下的整表暗化
      host.classList.remove("is-focusing");
      host.querySelectorAll("tr.sum-row.is-active").forEach((r) => r.classList.remove("is-active"));
      row.classList.add("is-active");
      jumpToIssue(id);
    });
  }
}

function renderReport(report) {
  $("#resultEmpty").classList.add("hidden");
  $("#resultView").classList.remove("hidden");
  const stats = report.stats || {};
  const verdict = VERDICT[report.verdict] || ["—", "verdict-warn", "dot-gray"];
  const blocker = stats.blocker || 0;
  const major = stats.major || 0;

  let title;
  if (report.verdict === "fail") title = `FAIL — 存在 ${blocker} 条阻断性问题`;
  else if (report.verdict === "warn") title = `WARN — 存在 ${major} 条严重问题`;
  // 「未发现」≠「通过」：这个引擎有已知漏报，pass 不能当放行依据
  else title = "未发现 — 不等于通过";

  // 文本覆盖：不显示覆盖率，用户会默认「全文都审过了」——长文截断时这是最危险的错觉
  const cov = report.coverage || {};
  const charsTotal = Number(cov.chars_total || 0);
  const charsReviewed = Number(cov.chars_reviewed != null ? cov.chars_reviewed : charsTotal);
  const covPct = cov.text_coverage_pct != null
    ? Number(cov.text_coverage_pct)
    : (charsTotal ? Math.round((charsReviewed / charsTotal) * 100) : 100);
  const incomplete = cov.complete === false
    || (cov.input_truncated_dimensions || []).length > 0
    || (cov.output_truncated_dimensions || []).length > 0;
  let coverLine = "";
  if (charsTotal) {
    coverLine = incomplete
      ? `<div class="verdict-cover warn">⚠ 仅审查了 ${esc(String(covPct))}% 的原文`
        + `（${esc(charsReviewed.toLocaleString())} / ${esc(charsTotal.toLocaleString())} 字符）`
        + `，其余 ${esc((charsTotal - charsReviewed).toLocaleString())} 字符未进入审查</div>`
      : `<div class="verdict-cover">✓ 全文覆盖：${esc(charsTotal.toLocaleString())} 字符</div>`;
  }

  applyAgentMeta(report);
  const agentName = (report.agent && report.agent.name) || "";
  $("#resultCaption").textContent =
    `审查结果 · ${report.review_id} · ${report.review_mode || "rule-only"}`
    + (agentName ? ` · ${agentName}` : "") + ` · `
    + `事实断言 ${(report.coverage && report.coverage.fact_assertions_matched) || 0}/`
    + `${(report.coverage && report.coverage.fact_assertions_total) || 0}`;
  $("#verdictBanner").innerHTML = `<div class="verdict-banner ${verdict[1]}">
    <span class="status-dot ${verdict[2]}" style="width:12px;height:12px;"></span>
    <div>
      <div class="verdict-text">${esc(title)}</div>
      <div class="verdict-sub">${esc(basename(report.target_path))} · ${relTime(report.timestamp)} · ${report.verdict === "pass" ? "未发现不等于通过，需人工复核" : "需整改后使用"}</div>
      ${coverLine}
    </div>
  </div>`;

  const excluded = report.excluded || [];
  const exEl = $("#excludedBlock");
  if (excluded.length) {
    exEl.classList.remove("hidden");
    exEl.innerHTML = '<div class="limitations-title">已排除（模型自否，不计入异议）</div><ul>'
      + excluded.map((x) =>
        `<li>${esc(x.rule_id || "")} — ${esc(String(x.quote || "").slice(0, 60))}`
        + `<span class="excluded-reason">（${esc(x.reason || "模型自否")}）</span></li>`).join("")
      + "</ul>";
  } else {
    exEl.classList.add("hidden");
    exEl.innerHTML = "";
  }

  const limitations = report.limitations || [];
  const limitEl = $("#limitations");
  if (limitations.length) {
    limitEl.classList.remove("hidden");
    limitEl.innerHTML = '<div class="limitations-title">本次审查边界</div><ul>'
      + limitations.map((item) => `<li>${esc(item)}</li>`).join("")
      + "</ul>";
  } else {
    limitEl.classList.add("hidden");
    limitEl.innerHTML = "";
  }

  const issues = report.issues || [];
  renderAnnotatedText(report);
  renderIssueSummary(report);
  if (!issues.length) {
    $("#issueList").innerHTML = '<div class="issue"><div class="issue-meta">未发现异议。</div></div>';
    return;
  }
  $("#issueList").innerHTML = issues.map((issue) => {
    const severity = SEV[issue.severity] || SEV.minor;
    const confidence = issue.confidence != null ? Number(issue.confidence) : null;
    const confText = confidence == null ? "" : `置信度 ${confidence.toFixed(2)}`;
    const confirm = confidence != null && confidence < 0.7 ? "（待人工确认）" : "";
    return `<div class="issue">
      <div class="issue-header">
        <span class="issue-severity ${severity[1]}">${severity[0]} · ${esc(issue.rule_id)}</span>
        <span class="issue-meta">${confText}${confirm}</span>
      </div>
      <div class="issue-quote">"${esc(issue.quote)}"</div>
      <div class="issue-evidence">${esc(issue.evidence)}</div>
      ${issue.suggestion ? `<div class="issue-suggestion">建议：${esc(issue.suggestion)}</div>` : ""}
    </div>`;
  }).join("");
}

/* ---------------- 完整性 / Ledger ---------------- */
async function loadIntegrity() {
  const data = await api("/api/integrity");
  state.integrity = data;
  const bar = $("#integrityBar");
  if (!data || data.ok === undefined) {
    bar.className = "integrity-bar";
    bar.textContent = "完整性状态未知";
    return;
  }
  if (!data.entries || !data.entries.length) {
    bar.className = "integrity-bar";
    bar.textContent = "暂无审计记录，运行一次审查后生成。";
  } else if (data.ok) {
    const stale = data.stale_count || 0;
    if (stale > 0) {
      // 规则集升级 ≠ 篡改：黄色提示，不报红
      bar.className = "integrity-bar warn";
      bar.innerHTML =
        `✓ 审计链完整 · ${data.entries.length} 条 prev_hash 逐行衔接无误<br>` +
        `⚠ ${stale} 份历史报告基于旧版判定依据（当前规则 ${esc(data.rules_version || "")}）——` +
        `规则升级后这些结论可能不再适用，请按需重审`;
    } else {
      bar.className = "integrity-bar";
      bar.innerHTML =
        `✓ 审计链完整 · ${data.entries.length} 条 prev_hash 逐行衔接无误<br>` +
        `✓ ${data.reports.length} 份报告全部自洽 · 0 份篡改嫌疑`;
    }
  } else {
    bar.className = "integrity-bar bad";
    const broken = data.broken_at ? `（断点 seq ${data.broken_at}）` : "";
    const head = data.chain_ok ? "报告内容校验异常" : "审计链断裂";
    bar.innerHTML = `✗ ${head}${esc(broken)}<br>${esc(data.detail || "请检查 ledger 与 outputs")}`;
  }
  renderLedger(data);
}

function renderLedger(data) {
  const chain = $("#chainVisual");
  const verify = $("#verifyResult");
  const meta = $("#ledgerMeta");
  if (!data || !data.entries || !data.entries.length) {
    $("#ledgerIntro").textContent = "当前链状态：暂无记录";
    chain.innerHTML = '<div class="chain-empty">还没有 ledger 记录，运行一次审查后生成。</div>';
    verify.className = "verify-result verify-ok";
    verify.textContent = "等待首次审查。";
    meta.textContent = "";
    return;
  }
  $("#ledgerIntro").textContent = data.chain_ok
    ? "当前链状态（正常）："
    : "当前链状态（异常）：";

  const nodes = [];
  data.entries.forEach((entry, index) => {
    const broken = entry.ok === false;
    nodes.push(`<div class="chain-node${broken ? " broken" : ""}">
      <div class="chain-node-id">${esc(entry.review_id || "R-???")}${broken ? " ⚠️" : ""}</div>
      <div class="chain-node-hash">${esc(entry.report_hash_short || "")}</div>
    </div>`);
    if (index < data.entries.length - 1) {
      const next = data.entries[index + 1];
      nodes.push(`<div class="chain-arrow${next.ok === false ? " broken" : ""}"></div>`);
    }
  });
  chain.innerHTML = nodes.join("");

  if (data.ok) {
    const stale = data.stale_count || 0;
    if (stale > 0) {
      verify.className = "verify-result verify-ok";
      verify.textContent =
        `✓ 校验通过：${data.reports.length} 份报告的 prev_hash 逐行衔接无误，未检测到篡改。` +
        `（其中 ${stale} 份基于旧版规则集/知识库/Agent，升级后结论可能需重审。）`;
    } else {
      verify.className = "verify-result verify-ok";
      verify.textContent = `✓ 校验通过：${data.reports.length} 份报告的 prev_hash 逐行衔接无误，未检测到篡改。`;
    }
  } else {
    verify.className = "verify-result verify-fail";
    const broken = data.broken_at ? `链在 seq ${data.broken_at} 处异常。` : "";
    verify.textContent = `✗ 校验失败：${broken}${data.detail || "存在报告内容被修改或链断裂。"}`;
  }
  const last = data.entries[data.entries.length - 1];
  meta.textContent =
    `链记录 ${data.entries.length} 条 · 报告 ${data.reports.length} 份 · 最后一条 ${last.ts ? relTime(last.ts) : "—"}`;
}

/* ---------------- 规则库 ---------------- */
async function loadRules() {
  const data = await api("/api/rules");
  if (!data.ok) { toast(data.error || "读取规则失败", true); return; }
  state.rules = data.items || [];
  updateRulesTitle();
  renderRules();
}

function updateRulesTitle() {
  const enabled = state.rules.filter((r) => r.enabled).length;
  $("#rulesTitle").textContent = `规则库（${enabled} 条生效中 / 共 ${state.rules.length} 条）`;
}

function renderRules() {
  const body = $("#rulesBody");
  const items = state.rules.filter(
    (r) => state.ruleFilter === "all" || r.dimension === state.ruleFilter
  );
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:32px;">没有匹配的规则</td></tr>';
    return;
  }
  body.innerHTML = items.map((rule) => {
    const dim = DIM[rule.dimension] || [rule.dimension, "dim-style"];
    const sev = SEV[rule.severity] || SEV.minor;
    return `<tr>
      <td class="rule-id">${esc(rule.rule_id)}</td>
      <td><span class="dim-tag ${dim[1]}">${esc(dim[0])}</span></td>
      <td>${esc(rule.desc)}</td>
      <td><span class="issue-severity ${sev[1]}">${esc(rule.severity)}</span></td>
      <td><div class="switch${rule.enabled ? " on" : ""}" role="switch"
        aria-checked="${rule.enabled}" data-rule="${esc(rule.rule_id)}"></div></td>
    </tr>`;
  }).join("");
}

async function toggleRule(ruleId, enabled) {
  const result = await api("/api/rules/toggle", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rule_id: ruleId, enabled }),
  });
  if (!result.ok) { toast(result.error || "保存失败", true); return null; }
  const rule = state.rules.find((r) => r.rule_id === ruleId);
  if (rule) rule.enabled = result.enabled;
  updateRulesTitle();
  return result;
}

/* ---------------- 审计日志 ---------------- */
function populateLogSelect() {
  const select = $("#logSelect");
  if (!select) return;
  select.innerHTML = state.reports.map((item) =>
    `<option value="${esc(item.id)}">${esc(item.id)} · ${esc(item.target_name || basename(item.target_path))}</option>`
  ).join("");
  if (state.currentLog) select.value = state.currentLog;
}

/** 渲染单步明细：普通键值对 + 字典型值（如"各维结果"）展开成子表 */
function renderStepDetail(step) {
  const d = step.detail;
  if (!d || !Object.keys(d).length) return "";
  const rows = Object.entries(d).map(([k, v]) => {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const inner = Object.entries(v).map(([k2, v2]) =>
        `<div class="log-kv"><span>${esc(k2)}</span><b>${esc(String(v2))}</b></div>`).join("");
      return `<div class="log-field"><div class="log-k">${esc(k)}</div>`
        + `<div class="log-v nested">${inner || "—"}</div></div>`;
    }
    const text = Array.isArray(v) ? (v.join("、") || "—") : String(v);
    return `<div class="log-field"><div class="log-k">${esc(k)}</div>`
      + `<div class="log-v">${esc(text)}</div></div>`;
  }).join("");
  return `<div class="log-entry-body detail">${rows}</div>`;
}

async function loadLogs(id) {
  const rid = id || state.currentLog || (state.currentReport && state.currentReport.review_id)
    || (state.reports[0] && state.reports[0].id);
  const panels = {
    task: $("#log-task"), decision: $("#log-decision"),
    tool: $("#log-tool"), cost: $("#log-cost"),
  };
  if (!rid) {
    $("#logsTitle").textContent = "审计日志 · 暂无记录";
    Object.values(panels).forEach((p) => { p.innerHTML = '<div class="log-empty">暂无日志</div>'; });
    return;
  }
  state.currentLog = rid;
  const data = await api("/api/logs?id=" + encodeURIComponent(rid));
  if (!data.ok) {
    $("#logsTitle").textContent = "审计日志 · 读取失败";
    Object.values(panels).forEach((p) => { p.innerHTML = `<div class="log-empty">${esc(data.error || "读取失败")}</div>`; });
    return;
  }
  $("#logsTitle").textContent = `审计日志 · ${data.id}`;
  if ($("#logSelect")) $("#logSelect").value = data.id;

  const task = data.task || {};
  const steps = task.steps || [];
  const hasDetail = steps.some((s) => s.detail && Object.keys(s.detail).length);
  const staleHint = steps.length && !hasDetail
    ? `<div class="log-stale">该次运行未记录步骤明细（旧版本日志）。`
      + `本次共 ${(data.decisions || []).length} 条决策、${(data.tools || []).length} 次工具调用，`
      + `可在「决策 / 工具调用」页查看；重新审查一次即可看到每步内容。</div>`
    : "";
  panels.task.innerHTML = steps.length
    ? staleHint + steps.map((step) => {
        const detail = renderStepDetail(step);
        let body = detail;
        if (step.step === "S6") {
          const summary = {
            final_status: task.final_status || "success",
            abort_reason: task.abort_reason || null,
          };
          body += `<div class="log-entry-body">${esc(JSON.stringify(summary, null, 2))}</div>`;
        }
        const statusCls = step.status === "success" ? "ok"
          : (step.status === "skipped" ? "skip" : "bad");
        return `<div class="log-entry">
          <div class="log-entry-header">
            <span class="log-entry-title">
              <span class="log-step-chip">${esc(step.step)}</span>${esc(step.name)}
              <span class="log-status ${statusCls}">${esc(step.status || "success")}</span>
            </span>
            <span class="log-entry-time">${fmtClock(step.ts)} · ${step.latency_ms || 0}ms</span>
          </div>${body}
        </div>`;
      }).join("")
    : '<div class="log-empty">暂无任务日志</div>';

  const decisions = data.decisions || [];
  panels.decision.innerHTML = decisions.length
    ? decisions.map((d, i) => {
        const body = [
          `rule_id: ${d.rule_id || "—"}`,
          `rationale: ${d.rationale || ""}`,
          `alternatives: ${JSON.stringify(d.alternatives || [])}`,
          `why_not: ${d.why_not_alternative || ""}`,
        ].join("\n");
        return `<div class="log-entry">
          <div class="log-entry-header">
            <span class="log-entry-title">决策 ${i + 1}：${esc(d.decision)}</span>
            <span class="log-entry-time">${fmtClock(d.ts)}</span>
          </div>
          <div class="log-entry-body">${esc(body)}</div>
        </div>`;
      }).join("")
    : '<div class="log-empty">暂无决策日志</div>';

  const tools = data.tools || [];
  panels.tool.innerHTML = tools.length
    ? tools.map((t) => {
        const body = `input: ${t.input || ""}\noutput_summary: ${t.output_summary || ""}`;
        return `<div class="log-entry">
          <div class="log-entry-header">
            <span class="log-entry-title">${esc(t.tool)} · ${esc(t.status)}</span>
            <span class="log-entry-time">${t.latency_ms || 0}ms · retry ${t.retry || 0}</span>
          </div>
          <div class="log-entry-body">${esc(body)}</div>
        </div>`;
      }).join("")
    : '<div class="log-empty">暂无工具日志</div>';

  const cost = data.cost || {};
  panels.cost.innerHTML = Object.keys(cost).length
    ? `<div class="log-entry">
        <div class="log-entry-header">
          <span class="log-entry-title">成本汇总</span>
          <span class="log-entry-time">总耗时 ${((cost.total_latency_ms || 0) / 1000).toFixed(1)}s</span>
        </div>
        <div class="log-entry-body">${esc(JSON.stringify(cost, null, 2))}</div>
      </div>`
    : '<div class="log-empty">暂无成本日志</div>';
}

/* ---------------- 审查任务 / 进度 ---------------- */
const STEP_DEFS = [
  ["S1", "接收"], ["S2", "规则匹配"], ["S3", "分维审查"],
  ["S4", "证据锚定"], ["S5", "交叉复核"], ["S6", "出报告"],
];

function renderProgress(steps, fileName) {
  const card = $("#progressCard");
  if (!state.job) card.classList.add("hidden");
  else card.classList.remove("hidden");
  const map = {};
  (steps || []).forEach((s) => { map[s.step] = s; });
  const running = state.job && state.job.status === "running";
  let activeAssigned = false;
  $("#steps").innerHTML = STEP_DEFS.map(([id, label], index) => {
    const done = !!map[id];
    let cls = "step";
    if (done) cls += " done";
    else if (running && !activeAssigned) { cls += " active"; activeAssigned = true; }
    const circle = done ? "✓" : String(index + 1);
    return `<div class="${cls}"><div class="step-circle">${circle}</div><div class="step-label">${id} ${label}</div></div>`;
  }).join("");
  if (state.job && state.job.status === "error") {
    $("#progressTitle").textContent = `审查失败：${state.job.error || "未知错误"}`;
  } else if (running) {
    $("#progressTitle").textContent = `正在审查 ${fileName || ""}…`;
  } else if (state.job && state.job.report_id) {
    $("#progressTitle").textContent = `审查完成 · ${state.job.report_id}`;
  } else {
    $("#progressTitle").textContent = "等待开始审查…";
  }
}

function updateRunState() {
  const btn = $("#runBtn");
  const example = $("#exampleBtn");
  const running = !!(state.job && state.job.status === "running");
  const hasFile = !!state.selected;
  const llmOk = !!(state.llm && state.llm.available);
  btn.disabled = !hasFile || !llmOk || running;
  if (running) btn.textContent = "审查中…";
  else if (!llmOk) btn.textContent = "运行审查（需配置 LLM）";
  else btn.textContent = "运行审查";
  btn.title = !llmOk ? "请先配置 LLM" : !hasFile ? "请先选择或上传文件" : "";
  if (example) example.disabled = running;
}

async function startReview(llmOverride) {
  if (!state.selected) { toast("请先选择或上传文件", true); return; }
  if (state.job && state.job.status === "running") return;
  const payload = { file: state.selected.rel };
  if (state.agent && state.agent !== "auto") payload.agent = state.agent;
  if (llmOverride === true || llmOverride === false) payload.llm = llmOverride;
  const data = await api("/api/review/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!data.ok) { toast(data.error || "启动审查失败", true); return; }
  state.job = { id: data.job_id, status: "running", report_id: null };
  $("#resultEmpty").classList.add("hidden");
  renderProgress([], state.selected.name);
  updateRunState();
  pollJob();
}

async function pollJob() {
  if (!state.job) return;
  const jobId = state.job.id;
  const data = await api("/api/review/status?job=" + encodeURIComponent(jobId));
  if (!data.ok) {
    toast(data.error || "查询进度失败", true);
    state.job = null;
    updateRunState();
    return;
  }
  state.job = data.job;
  renderProgress(data.steps || [], state.selected ? state.selected.name : "");
  if (data.job.status === "running") {
    state.poll = setTimeout(pollJob, 350);
    return;
  }
  if (data.job.status === "done" && data.report) {
    state.currentReport = data.report;
    state.currentLog = data.report.review_id;
    renderReport(data.report);
    await loadReports();
    await loadIntegrity();
    renderHistory();
    populateLogSelect();
    if (state.tab === "logs") loadLogs(state.currentLog);
    toast(`审查完成：${data.report.review_id} · ${String(data.report.verdict).toUpperCase()}`);
  } else {
    toast(`审查失败：${data.job.error || "未知错误"}`, true);
  }
  state.job = data.job;
  updateRunState();
}

async function runExample() {
  const sample = state.targets.find((t) => t.rel === "targets/answer_001.txt") || state.targets[0];
  if (!sample) { toast("没有可用的示例文件", true); return; }
  selectTarget(sample.rel);
  const llmOk = !!(state.llm && state.llm.available);
  if (!llmOk) toast("LLM 未配置，使用纯规则模式试跑");
  await startReview(llmOk ? undefined : false);
}

/* ---------------- LLM 状态与配置 ---------------- */
const FALLBACK_PROVIDER = {
  id: "custom", name: "自定义（OpenAI 兼容）", base_url: "", model: "",
  models: [], needs_key: null, local: false,
  hint: "任意兼容 /chat/completions 的端点；本机或局域网地址可留空 Key。",
};

function normalizeUrl(url) {
  return String(url || "").trim().replace(/\/+$/, "");
}

function hostOf(url) {
  try {
    return new URL(normalizeUrl(url)).hostname.toLowerCase();
  } catch (e) {
    return "";
  }
}

function isLocalUrl(url) {
  const host = hostOf(url);
  if (!host) return false;
  if (["127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"].includes(host)) {
    return true;
  }
  if (host.endsWith(".local")) return true;
  return /^10\./.test(host) || /^192\.168\./.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host);
}

function providerById(id) {
  return state.providers.find((p) => p.id === id)
    || (id === "custom" ? FALLBACK_PROVIDER : null);
}

async function loadProviders() {
  try {
    const data = await api("/api/llm/providers");
    state.providers = data && data.ok && Array.isArray(data.items) ? data.items : [];
  } catch (e) {
    state.providers = [];
  }
  const select = $("#llmProvider");
  select.innerHTML = state.providers.length
    ? state.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")
    : '<option value="custom">自定义（OpenAI 兼容）</option>';
}

function renderProviderHint(provider) {
  const hint = $("#providerHint");
  if (!provider) { hint.textContent = ""; hint.className = "provider-hint"; return; }
  const prefix = provider.local ? "本地模型： " : "";
  hint.textContent = prefix + (provider.hint || "");
  hint.className = "provider-hint" + (provider.local ? " local" : "");
}

function fillModelOptions(provider) {
  const list = $("#llmModelList");
  list.innerHTML = (provider && provider.models ? provider.models : [])
    .map((m) => `<option value="${esc(m)}"></option>`).join("");
}

function syncProviderFromUrl() {
  const base = normalizeUrl($("#llmBaseUrl").value);
  const found = state.providers.find((p) => p.base_url && normalizeUrl(p.base_url) === base);
  const provider = found || providerById("custom");
  $("#llmProvider").value = provider.id;
  renderProviderHint(provider);
  fillModelOptions(provider);
}

function applyProvider(id) {
  const provider = providerById(id);
  if (!provider) return;
  state.providerChanged = true;
  if (provider.id !== "custom") {
    $("#llmBaseUrl").value = provider.base_url;
    $("#llmModel").value = provider.model;
  }
  fillModelOptions(provider);
  renderProviderHint(provider);
  const oldBase = state.llm && state.llm.base_url;
  if (hostOf(oldBase) !== hostOf($("#llmBaseUrl").value)) {
    $("#llmKey").value = "";
    $("#llmKey").disabled = false;
    $("#llmClearKey").checked = false;
    $("#llmKey").placeholder = provider.needs_key
      ? "请填写该服务商的 API Key"
      : "本地模型通常无需 Key，可留空";
  }
}

async function loadLlmStatus() {
  try {
    const data = await api("/api/llm/status");
    state.llm = data && data.ok !== false ? data : null;
  } catch (e) {
    state.llm = null;
  }
  const ok = !!(state.llm && state.llm.available);
  $("#llmDot").className = "status-dot " + (ok ? "dot-green" : "dot-gray");
  $("#llmText").textContent = ok ? `LLM · ${state.llm.model}` : "LLM 未配置";
  $("#llmBanner").classList.toggle("hidden", ok);
  updateRunState();
}

function refreshKeyPlaceholder() {
  const input = $("#llmKey");
  if (!input) return;
  input.placeholder = state.llm && state.llm.api_key_set
    ? `已保存 ${state.llm.api_key}，留空不修改`
    : "未保存 Key，请填写（本地端点可留空）";
}

function renderLlmStatusLine() {
  const line = $("#llmStatusLine");
  if (!state.llm) {
    line.textContent = "配置状态未知";
    line.className = "llm-status bad";
    return;
  }
  const sources = {
    ui: "前端保存", env: "环境变量", config: "config/llm.yaml",
    default: "默认值", "config-error": "config 解析失败",
  };
  const key = state.llm.api_key_set ? `Key ${state.llm.api_key}` : "未设置 Key";
  line.textContent =
    `${state.llm.available ? "可用" : "不可用"} · ${state.llm.model} · 来源：${sources[state.llm.source] || state.llm.source || "未知"} · ${key} · ${state.llm.base_url}`;
  line.className = "llm-status " + (state.llm.available ? "ok" : "bad");
}

function fillLlmForm() {
  const s = state.llm || {};
  $("#llmBaseUrl").value = s.base_url || "https://api.deepseek.com/v1";
  $("#llmModel").value = s.model || "deepseek-chat";
  $("#llmKey").value = "";
  $("#llmKey").disabled = false;
  $("#llmClearKey").checked = false;
  $("#llmEnabled").checked = s.enabled !== false;
  $("#llmTimeout").value = s.timeout || 40;
  $("#llmMaxTokens").value = s.max_tokens || 1200;
  syncProviderFromUrl();
  state.providerChanged = false;
  refreshKeyPlaceholder();
  renderLlmStatusLine();
  renderLlmHistory();
}

/* ---------------- LLM 配置记忆（最近使用） ---------------- */
async function loadLlmHistory() {
  try {
    const data = await api("/api/llm/history");
    state.llmHistory = (data && data.items) || [];
  } catch (e) {
    state.llmHistory = [];
  }
  renderLlmHistory();
}

function renderLlmHistory() {
  const wrap = $("#llmHistoryWrap");
  const list = $("#llmHistoryList");
  if (!wrap || !list) return;
  const items = state.llmHistory || [];
  if (!items.length) {
    wrap.classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  const cur = state.llm || {};
  list.innerHTML = items.map((h, i) => {
    const isCur = h.base_url === cur.base_url && h.model === cur.model;
    const keyTag = h.api_key_set ? " · 有 Key" : "";
    const when = String(h.last_used_at || "").replace("T", " ").slice(5, 16);
    return `<button type="button" class="llm-history-item${isCur ? " current" : ""}" data-i="${i}">
      <span class="hm">${esc(h.model)}</span>
      <span class="hu">${esc(h.base_url)}${keyTag}</span>
      <span class="ht">${esc(when)}</span>
    </button>`;
  }).join("");
  list.querySelectorAll(".llm-history-item").forEach((btn) => {
    btn.addEventListener("click", () => applyHistoryItem(btn.dataset.i));
  });
}

function applyHistoryItem(idx) {
  const h = (state.llmHistory || [])[Number(idx)];
  if (!h) return;
  $("#llmBaseUrl").value = h.base_url;
  $("#llmModel").value = h.model;
  syncProviderFromUrl();
  state.providerChanged = true;
  // 换了端点就不能沿用旧 Key
  const oldBase = state.llm && state.llm.base_url;
  if (hostOf(oldBase) !== hostOf(h.base_url)) {
    $("#llmKey").value = "";
    $("#llmKey").disabled = false;
    $("#llmClearKey").checked = false;
  }
  refreshKeyPlaceholder();
  renderLlmStatusLine();
  toast(`已填入 ${h.model} · ${h.base_url}，点「保存」后生效`);
}

async function openLlmModal() {
  $("#llmModal").classList.remove("hidden");
  document.body.classList.add("modal-open");
  await loadLlmStatus();
  await loadLlmHistory();
  fillLlmForm();
  $("#llmBaseUrl").focus();
}

function closeLlmModal() {
  $("#llmModal").classList.add("hidden");
  document.body.classList.remove("modal-open");
}

function collectLlmDraft() {
  const draft = {
    base_url: $("#llmBaseUrl").value.trim(),
    model: $("#llmModel").value.trim(),
    enabled: $("#llmEnabled").checked,
  };
  const timeout = parseInt($("#llmTimeout").value, 10);
  if (Number.isFinite(timeout)) draft.timeout = timeout;
  const maxTokens = parseInt($("#llmMaxTokens").value, 10);
  if (Number.isFinite(maxTokens)) draft.max_tokens = maxTokens;
  const key = $("#llmKey").value.trim();
  if (key) draft.api_key = key;
  if ($("#llmClearKey").checked) draft.clear_key = true;
  return draft;
}

async function saveLlm(ev) {
  if (ev) ev.preventDefault();
  const btn = $("#llmSave");
  const draft = collectLlmDraft();
  const provider = providerById($("#llmProvider").value);
  const oldBase = state.llm && state.llm.base_url;
  const hostChanged = hostOf(oldBase) !== hostOf(draft.base_url);
  const oldKeySet = !!(state.llm && state.llm.api_key_set);

  if (hostChanged && oldKeySet && !draft.api_key && !draft.clear_key) {
    if ((provider && provider.local) || isLocalUrl(draft.base_url)) {
      draft.clear_key = true;
    } else {
      toast("切换服务商后请重新填写 API Key，或勾选「清除已保存的 Key」", true);
      return;
    }
  }
  if (!draft.api_key && !draft.clear_key && !oldKeySet
      && provider && provider.needs_key) {
    toast("该服务商需要 API Key", true);
    return;
  }

  btn.disabled = true;
  try {
    const result = await api("/api/llm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    });
    if (!result.ok) { toast(result.error || "保存失败", true); return; }
    await loadLlmStatus();
    await loadLlmHistory();
    fillLlmForm();
    state.providerChanged = false;
    toast(result.saved ? "LLM 配置已保存" : "没有改动需要保存");
  } catch (e) {
    toast("保存失败：" + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function testLlm() {
  const btn = $("#llmTest");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "测试中…";
  $("#llmStatusLine").textContent = "正在请求模型…";
  $("#llmStatusLine").className = "llm-status";
  try {
    const result = await api("/api/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectLlmDraft()),
    });
    if (!result.ok) {
      $("#llmStatusLine").textContent = "测试失败：" + (result.error || "未知错误");
      $("#llmStatusLine").className = "llm-status bad";
      return;
    }
    const reply = String(result.reply || "").replace(/\s+/g, " ").slice(0, 40);
    const to = result.timeout ? ` · 超时上限 ${Math.round(result.timeout)}s` : "";
    $("#llmStatusLine").textContent =
      `连接成功 · ${result.model} · 延迟 ${result.latency_ms}ms${to} · 回复「${reply || "（空）"}」`;
    $("#llmStatusLine").className = "llm-status ok";
    await loadLlmHistory();
  } catch (e) {
    $("#llmStatusLine").textContent = "测试失败：" + e.message;
    $("#llmStatusLine").className = "llm-status bad";
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function resetLlm() {
  if (!confirm("清除前端保存的 LLM 配置，恢复 config/llm.yaml / 环境变量？")) return;
  try {
    const result = await api("/api/llm/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!result.ok) { toast(result.error || "重置失败", true); return; }
    await loadLlmStatus();
    fillLlmForm();
    toast("已恢复默认配置");
  } catch (e) {
    toast("重置失败：" + e.message, true);
  }
}

/* ---------------- 初始化 ---------------- */
async function init() {
  $$(".nav-tab").forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );
  $$(".log-subtab").forEach((btn) =>
    btn.addEventListener("click", () => switchLog(btn.dataset.log))
  );
  $$("#ruleFilters .filter-chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      state.ruleFilter = chip.dataset.dim;
      $$("#ruleFilters .filter-chip").forEach((c) => c.classList.toggle("active", c === chip));
      renderRules();
    })
  );
  $("#rulesBody").addEventListener("click", async (e) => {
    const sw = e.target.closest(".switch");
    if (!sw || sw.classList.contains("busy")) return;
    const enabled = !sw.classList.contains("on");
    sw.classList.add("busy");
    const result = await toggleRule(sw.dataset.rule, enabled);
    sw.classList.remove("busy");
    if (!result) return;
    sw.classList.toggle("on", result.enabled);
    sw.setAttribute("aria-checked", String(result.enabled));
    toast(`${result.rule_id} 已${result.enabled ? "启用" : "停用"}`);
  });

  initUpload();
  $("#runBtn").addEventListener("click", () => startReview());
  $("#exampleBtn").addEventListener("click", runExample);
  $("#logSelect").addEventListener("change", (e) => {
    state.currentLog = e.target.value;
    loadLogs(e.target.value);
  });

  $("#llmPill").addEventListener("click", openLlmModal);
  $("#llmBannerBtn").addEventListener("click", openLlmModal);
  $("#llmClose").addEventListener("click", closeLlmModal);
  $("#llmModal").querySelectorAll("[data-close]").forEach((el) =>
    el.addEventListener("click", closeLlmModal)
  );
  $("#llmForm").addEventListener("submit", saveLlm);
  $("#llmTest").addEventListener("click", testLlm);
  $("#llmReset").addEventListener("click", resetLlm);
  $("#llmProvider").addEventListener("change", (e) => applyProvider(e.target.value));  $("#llmBaseUrl").addEventListener("input", syncProviderFromUrl);
  $("#llmKey").addEventListener("input", () => {
    if ($("#llmKey").value) $("#llmClearKey").checked = false;
  });
  $("#llmClearKey").addEventListener("change", () => {
    const clear = $("#llmClearKey").checked;
    if (clear) $("#llmKey").value = "";
    $("#llmKey").disabled = clear;
  });
  $("#confirmClose").addEventListener("click", closeConfirm);
  $("#confirmCancel").addEventListener("click", closeConfirm);
  $("#confirmOk").addEventListener("click", confirmDeleteReport);
  $("#confirmModal").querySelectorAll("[data-close]").forEach((el) =>
    el.addEventListener("click", closeConfirm)
  );
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#llmModal").classList.contains("hidden")) closeLlmModal();
    if (!$("#confirmModal").classList.contains("hidden")) closeConfirm();
  });
  $("#themeBtn").addEventListener("click", () => toast("深色模式开发中"));

  await loadProviders();
  await loadLlmStatus();
  await loadTargets();
  await loadAgents();
  await loadRules();
  await loadIntegrity();
  await loadReports();
  renderProgress([], "");

  const initialTab = (location.hash || "").replace("#", "");
  if (["review", "rules", "logs", "ledger"].includes(initialTab)) switchTab(initialTab);
  if (new URLSearchParams(location.search).get("llm") === "1") openLlmModal();
}

/* ---------------- 审查 Agent ---------------- */
async function loadAgents() {
  const data = await api("/api/agents");
  const items = (data && data.items) || [];
  state.agents = items;
  const sel = $("#agentSelect");
  const opts = [`<option value="auto">自动路由（推荐）</option>`].concat(
    items.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} v${esc(a.version)}</option>`)
  );
  sel.innerHTML = opts.join("");
  const sync = () => {
    state.agent = sel.value;
    const a = items.find((x) => x.id === sel.value);
    const hint = $("#agentHint");
    if (!a) {
      hint.textContent = "按文件扩展名自动选择 Agent（当前为文档审查）";
      return;
    }
    const dims = (a.dimensions || []).map((d) => d.label).join("、");
    hint.innerHTML = `${esc(a.description || "")}<br>维度：${esc(dims)}`
      + `<br>规则 ${esc(String(a.rules || 0))} 条 · 检查器 ${esc(String((a.checks || []).length))} 项`
      + (a.auto ? "" : " · 仅手动选择（不参与自动路由）");
  };
  sel.addEventListener("change", sync);
  sync();
}

document.addEventListener("DOMContentLoaded", init);
