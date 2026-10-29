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
  logic: ["逻辑", "dim-logic"],
  safety: ["安全", "dim-safety"],
  compliance: ["合规", "dim-compliance"],
  style: ["风格", "dim-style"],
  format: ["格式", "dim-format"],
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
  candidates: [],
  pendingCount: 0,
  agents: [],
  agent: "auto",
  scenarios: [],
  scenario: "",
  formatSpecs: [],
  formatSpec: "",
  llm: null,
  llmHistory: [],
  providers: [],
  providerChanged: false,
  integrity: null,
  job: null,
  poll: null,
  pollFails: 0,
  pollBusy: false,
  tab: "review",
  purposeFiles: [],
};

/* ---------------- 基础工具 ---------------- */
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function api(path, opts) {
  // 所有写请求都带自定义头：跨站页面无法在不触发预检的情况下伪造它
  const options = Object.assign({}, opts);
  options.headers = Object.assign(
    { "X-Review-Console": "1" }, (opts && opts.headers) || {});
  return fetch(API_BASE + path, options).then(async (res) => {
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
    if (item.reviewable === false) {
      // 机读标注（*.labels.json）是**标准答案**，不是待审原文：审它会把它写的
      // 「某处有错」逐条当问题报出来。后端口径一致，这里只是提前说清楚。
      option.textContent = `［标注·不可审］${item.name} · ${fmtBytes(item.size)}`;
      option.disabled = true;
      option.title = "机读标注（ground truth），不是待审原文；请选对应的原文文件";
    } else {
      option.textContent = `${item.uploaded ? "[上传] " : ""}${item.name} · ${fmtBytes(item.size)}`;
    }
    select.appendChild(option);
  }
  // 默认选中：优先「仍然可审的当前选择」→ 第一个可审对象。
  // 绝不能直接取 items[0]：那会因字母序把 adversarial_*.labels.json 顶成默认值，
  // 用户以为在审刚上传的简历，实际审的是标注文件。
  const usable = state.targets.filter((t) => t.reviewable !== false);
  const keep = state.selected && usable.some((t) => t.rel === state.selected.rel)
    ? state.selected.rel
    : null;
  const rel = preferRel && usable.some((t) => t.rel === preferRel)
    ? preferRel
    : (keep || (usable[0] ? usable[0].rel : null));
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
      { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Review-Console": "1" }, body: file }
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
    // 选完就收起：文件已落到「待审对象」里，留着一大块虚线框只会挤掉下面的设置
    setUploadOpen(false);
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

/**
 * 上传区展开/收起。
 *
 * 为什么默认收起：左栏高度被视口限死（见 style.css 的三栏独立滚动），
 * 常驻一块 180px 的虚线框会把「审查目的」和「运行审查」挤出可视区。
 * 但**拖拽不能因此变麻烦**——所以窗口里拖入文件时自动展开。
 */
function setUploadOpen(open) {
  const box = $("#uploadBox");
  const btn = $("#uploadToggle");
  const sub = $("#uploadToggleSub");
  if (!box || !btn) return;
  box.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", String(open));
  if (sub) {
    sub.textContent = open
      ? "拖入文件，或点击下方虚线框选择"
      : "点击展开上传区，也可直接把文件拖进来";
  }
}

function initUpload() {
  const input = $("#fileInput");
  const box = $("#uploadBox");
  const toggle = $("#uploadToggle");
  if (toggle) {
    toggle.addEventListener("click", () => setUploadOpen(box.classList.contains("hidden")));
  }
  // 拖拽进入窗口就展开：用户不必先点按钮才知道这里能拖
  window.addEventListener("dragenter", (e) => {
    const types = (e.dataTransfer && e.dataTransfer.types) || [];
    if (Array.from(types).includes("Files")) setUploadOpen(true);
  });
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
    setUploadOpen(true);      // 移除后直接露出选择区，省一次点击
  });
}

/* ---------------- 历史报告 ---------------- */
/* 上次成功拉取列表的时间：自动刷新用它做节流（服务端每次要读全部报告 JSON，不能轮询） */
let reportsFetchedAt = 0;

async function loadReports() {
  const data = await api("/api/reports");
  state.reports = data.items || [];
  reportsFetchedAt = Date.now();
  updateHistoryHead();
  renderHistory();
  populateLogSelect();
  if (!state.currentReport && state.reports.length) {
    await selectReport(state.reports[0].id, false);
  }
}

/**
 * 重新拉一次历史列表。
 *
 * 列表不是实时的：在命令行（`python review.py`）或另一个窗口跑完审查，
 * 本页不会自己知道。三个触发点：标题右侧「⟳ 刷新」、窗口重新获得焦点、
 * 页面从后台切回。自动触发 8s 节流——服务端每次都要读全部报告 JSON，不能轮询。
 */
async function refreshReports(opts) {
  const force = !!(opts && opts.force);
  // 收藏请求正在飞：重渲染会把那个按钮换掉，disabled 态与响应回调一起丢
  if ($("#historyList .history-fav[disabled]")) return;
  if (!force && Date.now() - reportsFetchedAt < 8000) return;
  const before = new Set(state.reports.map((r) => r.id));
  const btn = $("#historyRefresh");
  if (btn) btn.classList.add("busy");
  try {
    await loadReports();
  } finally {
    if (btn) btn.classList.remove("busy");
  }
  const added = state.reports.filter((r) => !before.has(r.id)).length;
  if (added > 0) toast(`历史报告已更新（+${added} 条）`);
  else if (force) toast("已是最新");
  // 当前正在看的报告被别人删掉了：别让人对着已删除的内容继续判断
  if (state.currentReport
      && !state.reports.some((r) => r.id === state.currentReport.review_id)) {
    toast("当前报告已不在列表中（可能已在别处删除）", true);
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

/* 删除二次确认的待删 ID */
let pendingDeleteId = null;

/* 收藏视图：只看 ★ 的报告。纯前端筛选，服务端只负责记"哪些被收藏"。 */
let favoritesOnly = false;

function favCount() {
  return state.reports.filter((r) => r.favorite).length;
}

/** 同步标题与 ⭐ 按钮的文案 / 按下态（列表变动后都要调）。 */
function updateHistoryHead() {
  const n = favCount();
  const title = $("#historyTitle");
  if (title) {
    title.textContent = favoritesOnly
      ? `收藏（${n}）`
      : `历史报告（${state.reports.length}）`;
  }
  const btn = $("#historyFavFilter");
  if (!btn) return;
  // 标题已经写了"收藏（N）"，按钮就不再重复一遍数量，而是给"回到全部"的出路
  btn.textContent = favoritesOnly
    ? `← 全部（${state.reports.length}）`
    : `★ 收藏（${n}）`;
  btn.classList.toggle("on", favoritesOnly);
  btn.setAttribute("aria-pressed", favoritesOnly ? "true" : "false");
  btn.title = favoritesOnly
    ? "当前只显示收藏，点一下回到全部报告"
    : "只看收藏过的报告；再点一次回到全部";
}

function renderHistory() {
  const list = $("#historyList");
  const shown = favoritesOnly
    ? state.reports.filter((r) => r.favorite)
    : state.reports;
  if (!shown.length) {
    // 收藏视图为空时不能只说"点行右侧的 ☆"——这个视图里一条行都没有，没星可点
    list.innerHTML = favoritesOnly
      ? '<div class="history-empty">还没有收藏的报告<br><span class="history-empty-sub">点「← 全部」回到列表，再点行右侧的 ☆ 收藏</span></div>'
      : '<div class="history-empty">暂无历史报告</div>';
    return;
  }
  list.innerHTML = shown.map((item) => {
    const verdict = VERDICT[item.verdict] || ["—", "verdict-warn", "dot-gray"];
    const active = state.currentReport && state.currentReport.review_id === item.id;
    const fav = !!item.favorite;
    return `<div class="history-row${active ? " active" : ""}" data-id="${esc(item.id)}">
      <div class="history-main">
        <div class="history-id"><span class="status-dot ${verdict[2]}"></span>${esc(item.id)}</div>
        <div class="history-file">${esc(item.target_name || basename(item.target_path))} · ${relTime(item.timestamp)}</div>
        <div class="history-stats">${statsPills(item)}</div>
      </div>
      <button type="button" class="history-fav${fav ? " on" : ""}" data-fav="${esc(item.id)}"
              aria-pressed="${fav ? "true" : "false"}"
              title="${fav ? "取消收藏" : "收藏这份报告（之后可在「★ 收藏」里快速找到）"}"
              aria-label="${fav ? "取消收藏" : "收藏"} ${esc(item.id)}">${fav ? "★" : "☆"}</button>
      <button type="button" class="history-del" data-del="${esc(item.id)}" title="删除该报告" aria-label="删除 ${esc(item.id)}">🗑</button>
    </div>`;
  }).join("");
  bindHistoryEvents();
}

function bindHistoryEvents() {
  const list = $("#historyList");
  list.querySelectorAll(".history-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      // 星标和删除是行内按钮：不先排除会把"收藏"变成"打开报告"
      if (e.target.closest(".history-del") || e.target.closest(".history-fav")) return;
      selectReport(row.dataset.id, false);
    });
  });
  list.querySelectorAll(".history-fav").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFavorite(btn);
    });
  });
  list.querySelectorAll(".history-del").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      askDeleteReport(btn.dataset.del);
    });
  });
}

/**
 * 收藏 / 取消收藏一份报告。
 *
 * 状态以服务端响应为准（不在本地先改再回滚）：收藏写盘失败时如果已经点亮星星，
 * 用户会以为收藏成功了，下次打开却找不到——这类"看起来成功"的失败最难排查。
 */
async function toggleFavorite(btn) {
  const id = btn.dataset.fav;
  const item = state.reports.find((r) => r.id === id);
  if (!item || btn.disabled) return;
  const want = !item.favorite;
  btn.disabled = true;
  let data;
  try {
    data = await api("/api/reports/favorite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, favorite: want }),
    });
  } catch (e) {
    data = { ok: false, error: `收藏失败：${e && e.message ? e.message : e}` };
  } finally {
    btn.disabled = false;
  }
  if (!data || !data.ok) {
    toast((data && data.error) || "收藏失败", true);
    return;
  }
  item.favorite = want;
  // 只重渲染列表（标题计数要跟着变）；滚动位置手动保留，否则翻到中间一点就跳回顶部
  const keep = $("#historyList").scrollTop;
  renderHistory();
  $("#historyList").scrollTop = keep;
  updateHistoryHead();
  toast(want ? `已收藏 ${id}` : `已取消收藏 ${id}`);
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
  fact: "事实", logic: "逻辑", safety: "安全", compliance: "合规",
  style: "风格", format: "格式",
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
  const preferred = ["logic", "fact", "safety", "compliance", "style", "format"];
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

/**
 * 找最近一个「真的能纵向滚动」的祖先容器。
 *
 * 为什么不能直接用 window：宽屏（≥1101px）下 `#page-review .grid-review > .card`
 * 是 `max-height: calc(100vh - nav)` + `overflow-y: auto` 的独立滚动容器，
 * 整页 `scrollHeight === innerHeight`——`window.scrollTo()` 一点都滚不动。
 * 早期只滚 window，于是「点原文标注 → 跳到问题汇总」在宽屏下永远没反应
 * （反方向「汇总 → 原文」滚的是 #annotatedText 内部，所以看起来是好的）。
 *
 * skipEl：跳过某个祖先。原文标注自己就在 #annotatedText 这个滚动框里，
 * 那一段的位置由 centerMarkInBox 精确控制，不能被这里再滚一次。
 */
function scrollHost(el, skipEl) {
  let p = el && el.parentElement;
  while (p && p !== document.body && p !== document.documentElement) {
    if (p !== skipEl) {
      const st = getComputedStyle(p);
      // overflow-x:auto 会让另一轴的 visible 计算成 auto，所以必须用 scrollHeight 验证
      if (/(auto|scroll|overlay)/.test(st.overflowY)
          && p.scrollHeight > p.clientHeight + 1) {
        return p;
      }
    }
    p = p.parentElement;
  }
  return null;
}

/**
 * 把某个滚动容器滚到目标位置，并**校验是否真的到位**。
 *
 * 平滑滚动是异步的，页面上任何一次重排/重渲染都可能把它打断——
 * 表现就是"点了跳转，页面只挪了一点，或者一点没动"。
 * 所以滚完 200ms 再量一次，差超过 2px 就直接写死（auto），
 * 宁可没有动画，也不能停在半路。
 */
function scrollHostTo(host, top, opts) {
  if (!host) return;
  const immediate = !!(opts && opts.immediate);
  const target = Math.max(0, Math.round(top));
  host.scrollTo({ top: target, behavior: immediate ? "auto" : "smooth" });
  if (immediate) return;
  let tries = 0;
  const tick = () => {
    tries += 1;
    if (Math.abs(host.scrollTop - target) <= 2) return;      // 已到位
    host.scrollTo({ top: target, behavior: "auto" });         // 补差
    if (tries < 3) setTimeout(tick, 160);
  };
  setTimeout(tick, 200);
}

/** 滚动容器的可视区上下边界（视口坐标系，已避让 sticky 导航） */
function viewportBounds(host) {
  if (host === window) return { top: NAV_HEIGHT, bottom: window.innerHeight };
  const hr = host.getBoundingClientRect();
  return { top: hr.top, bottom: hr.bottom };
}

/**
 * 计算「把 rect 摆到可视区正中间」所需的 scrollTop。
 *
 * 落点基准是**可视区中线**，不是容器顶边。早期用「顶边 + 12px」，跳转后目标行
 * 紧贴模块上沿，长句的第一行还会被卡片标题/表头压掉——用户得自己往下挪眼睛。
 * 目标比可视区还高（长段落、整块）时无法居中，退化为顶部对齐并留 14px 呼吸位。
 */
function centerScrollTop(rect, host) {
  const b = viewportBounds(host);
  const viewH = Math.max(0, b.bottom - b.top);
  const base = host === window ? window.pageYOffset : host.scrollTop;
  if (rect.height >= viewH * 0.7) return base + (rect.top - b.top) - 14;
  return base + ((rect.top + rect.height / 2) - (b.top + viewH / 2));
}

/** 把元素带进它所在滚动容器的可视区**正中间**（自动判断是 window 还是卡片内部） */
function revealInViewport(el, opts) {
  if (!el) return;
  const skipEl = (opts && opts.skip) || null;
  const force = !!(opts && opts.force);
  const host = scrollHost(el, skipEl) || window;
  const b = viewportBounds(host);
  const r = el.getBoundingClientRect();
  const viewH = Math.max(0, b.bottom - b.top);
  const mid = (b.top + b.bottom) / 2;
  // 已经落在中线附近的「舒适带」里就不动，避免每次点击都轻微抖一下
  const tol = Math.max(24, viewH * 0.15);
  if (!force && Math.abs((r.top + r.bottom) / 2 - mid) <= tol) return;
  scrollHostTo(host, centerScrollTop(r, host));
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

/** 确保标注落在页面级滚动容器的**正中间**（闭环校正页面级滚动）
 *
 * 只处理 mark 所在的页面级滚动容器（宽屏下是 #page-review 的 .card）；
 * mark 在 #annotatedText 内部的位置由 centerMarkInBox 负责，这里跳过它，
 * 否则两套滚动会互相打架。
 *
 * 判定用「中心偏离中线」而不是「是否被裁切」：句子刚好卡在容器上沿时
 * 在技术上"可见"，但读者根本不知道该看哪儿——用户要的是居中，不是可见。
 */
function ensureMarkInViewport(mark) {
  const box = $("#annotatedText");
  const host = scrollHost(mark, box) || window;
  const b = viewportBounds(host);
  const r = mark.getBoundingClientRect();
  const viewH = Math.max(0, b.bottom - b.top);
  // 中心差在可视区 12% 以内就算已居中，避免轮询期间反复微调抖动
  const mid = (b.top + b.bottom) / 2;
  const tol = Math.max(16, viewH * 0.12);
  if (Math.abs((r.top + r.bottom) / 2 - mid) <= tol) return;
  scrollHostTo(host, centerScrollTop(r, host), { immediate: true });
}

/**
 * 收尾：把「标注中心 vs 屏幕中线」的残余差补到原文框自己的滚动上。
 *
 * 为什么需要这一步：原文框（#annotatedText）在卡片里通常**不居中**——
 * 上方有标题/图例，下方还可能挂别的区块，框的中线比屏幕中线高 50px 以上。
 * `desiredScrollTop` 只能把句子居中在**框内**，落到屏幕上仍然偏上。
 * 外层容器又常常根本滚不动（宽屏整页 scrollHeight === innerHeight），
 * 于是残余差只能由框内再多滚一点来吃掉。
 *
 * 两条护栏，避免"为了居中把句子推到框外"：
 *   1. 屏幕中线必须真的落在框内（框很矮又贴着卡片顶部时，硬推只会推到边缘）；
 *   2. 补差后的 scrollTop 必须仍在 [0, 可滚最大值] 之间（到头的就认了）。
 */
function settleInBoxToViewportCenter(mark, box) {
  const host = scrollHost(mark, box) || window;
  const b = viewportBounds(host);
  const want = (b.top + b.bottom) / 2;
  const boxRect = box.getBoundingClientRect();
  const aim = want - boxRect.top;                       // 屏幕中线在框内的位置
  if (aim < 24 || aim > box.clientHeight - 24) return;  // 中线不在框里，别硬推
  const r = mark.getBoundingClientRect();
  const off = (r.top + r.bottom) / 2 - want;
  if (Math.abs(off) < 12) return;
  const max = box.scrollHeight - box.clientHeight;
  const next = box.scrollTop + off;
  if (next < 0 || next > max) return;
  box.scrollTop = next;
}

/** 让某处标注在原文框内精确落位：先平滑滚动，再轮询闭环补差 */
function centerMarkInBox(mark, box) {
  const go = () => Math.max(0, desiredScrollTop(mark, box));
  box.scrollTo({ top: go(), behavior: "smooth" });
  // 平滑滚动是异步的：轮询到落点稳定为止，避免"动画还没停就写死位置"，
  // 也避免"完全不校验、落点靠猜"。最多 4 次后强制对齐。
  // 落位稳定后才做「对准屏幕中线」的收尾——提前做会被下一轮 go() 拉回去，两者打架。
  let tries = 0;
  const finish = () => {
    ensureMarkInViewport(mark);
    settleInBoxToViewportCenter(mark, box);
  };
  const tick = () => {
    tries += 1;
    // 每轮都先校正页面级滚动：窄屏下原文框自身不可滚，全靠这一步把句子带进视区
    ensureMarkInViewport(mark);
    const gap = go() - box.scrollTop;
    if (Math.abs(gap) > 2) {
      if (tries < 4) { setTimeout(tick, 140); return; }
      box.scrollTop = go();                              // 兜底：直接写死
    }
    finish();
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

  // 先粗定位页面级容器（把标注带进视区中央），再在原文框内精确居中；
  // 两步都要，因为窄屏下原文框自身不滚、只有页面能滚。
  revealInViewport(mark, { skip: box });
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

/* 汇总表的「问题摘要」**默认全文展开**，不折叠、不给按钮。
   判定依据是一句话的价值密度：一条问题的结论往往在最后半句
   （"…未说明修改的范围与验证方式，结论强度与证据强度不匹配"），
   折叠或截断都会让人只看到前半段就下判断。
   代价是表格更高——这是刻意的取舍：可读性优先于紧凑。 */

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
        <thead><tr><th>编号</th><th>严重度</th><th>规则</th><th>位置</th><th>问题摘要</th><th>${noSnapshot ? "定位" : "定位 ↓"}</th><th>标注</th></tr></thead>
        <tbody>${list.map((it) => {
          const n = index.get(it.issue_id);
          const hits = (annoAnchors.get(it.issue_id) || []).length;
          const loc = hits > 0
            ? `<span class="sum-loc" title="点击跳转到原文中的标注位置${hits > 1 ? `（共 ${hits} 处，再次点击可继续跳）` : ""}">定位 →</span>`
            : `<span class="sum-loc none" title="该问题在原文中没有可定位的引用片段">—</span>`;
          // 摘要**全文直出**：不截断、不折行、不留按钮。
          // 旧版 slice(0,110) 砍掉后半句且无省略号，结论往往正好在最后半句；
          // 折 3 行 + 「展开全文」也还是要多点一次才看得全——这里直接给全。
          const brief = String(it.evidence || it.quote || "");
          const briefHtml = `<td class="sum-brief-cell"><div class="sum-brief">${esc(brief)}</div></td>`;
          return `<tr class="sum-row${hits ? "" : " no-loc"}" data-issue-id="${esc(it.issue_id || "")}"
            title="${hits ? "点击定位到原文中的对应标注" : "该问题在原文中没有可定位的引用片段"}">
            <td><span class="seq-badge dim-${esc(d)}">${esc(n ? n.label : "")}</span></td>
            <td><span class="sev-tag sev-${esc(it.severity)}">${esc(it.severity)}</span></td>
            <td class="mono">${esc(it.rule_id)}${it.open_discovery ? ' <span class="open-tag">规则外</span>' : ""}${String(it.rule_id || "").startsWith("AUTO-") ? ' <span class="auto-tag" title="自学习闭环晋升的规则，只匹配措辞，需人工确认">自动</span>' : ""}</td>
            <td class="mono-dim">${esc(it.location || "")}</td>
            ${briefHtml}
            <td class="sum-loc-cell">${loc}</td>
            <td class="sum-anno-cell"><button class="btn-anno" data-anno-id="${esc(it.issue_id || "")}"
              title="这条不是问题？标为误报，作为规则太松的证据">误报</button></td>
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
      // 先判标注按钮：它在行内，不先判会被当成"点击定位"
      const annoBtn = e.target.closest("button[data-anno-id]");
      if (annoBtn) {
        e.stopPropagation();
        markFalsePositive(annoBtn);
        return;
      }
      // 摘要默认全文直出，没有"展开"这一步，所以这里只剩「整行点击 = 定位到原文」
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

/* ---------------- 人工标注（自学习闭环的可信信号） ---------------- */

/**
 * 把一条异议标为误报。
 *
 * 这条链路只**写标注**，不碰规则库：标注是"这条规则可能太松"的证据，
 * 是否收紧、怎么收紧，由门禁 + 人决定（见 docs/自学习闭环.md）。
 * 控制台刻意不提供任何"直接改规则"的入口。
 */
async function markFalsePositive(btn) {
  const report = state.currentReport;
  const issueId = btn.dataset.annoId;
  if (!report || !issueId) return;
  btn.disabled = true;
  const data = await api("/api/annotations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "false_positive", by: "console",
      report_id: report.review_id, issue_id: issueId,
    }),
  });
  if (!data.ok) {
    btn.disabled = false;
    toast(data.error || "标注失败", true);
    return;
  }
  btn.textContent = "已标注";
  btn.classList.add("done");
  toast("已记录误报：它将作为「规则可能太松」的证据进入学习闭环");
}

/** 取原文标注区内的当前选区（去掉角标文字，保证与原文逐字一致）。 */
function selectedAnnoQuote() {
  const sel = window.getSelection();
  const box = $("#annotatedText");
  if (!sel || sel.isCollapsed || !box) return "";
  if (!sel.anchorNode || !box.contains(sel.anchorNode)) return "";
  // 角标（逻辑1/事实3）也在选区文本里，但原文里没有这些字，必须剔除
  return String(sel.toString())
    .replace(/(逻辑|事实|安全|合规|风格|凭据|注入|配置|依赖)\d+/g, "")
    .trim();
}

async function markFalseNegative() {
  const report = state.currentReport;
  if (!report) return;
  const quote = selectedAnnoQuote();
  if (!quote) {
    toast("请先在原文中选中一句有问题的文字，再点「标注漏检」", true);
    return;
  }
  if (report.target_text && !report.target_text.includes(quote)) {
    toast("选中的片段与原文不一致（可能跨了段落），请重新选择", true);
    return;
  }
  const data = await api("/api/annotations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "false_negative", by: "console", target_path: report.target_path,
      quote, dimension: $("#annoDim").value,
    }),
  });
  if (!data.ok) { toast(data.error || "标注失败", true); return; }
  window.getSelection().removeAllRanges();
  toast(`已记录漏检（${$("#annoDim").value}）：它将成为新规则的种子`);
}

/* ---------------- 审查目的与目标证据 ---------------- *
 *
 * 输入侧：一句话目的（可选，存 localStorage）。
 * 输出侧：意图 → 目标（找到/未找到）→ 逐字证据，点证据能在原文里定位。
 * 关键点：**「未找到」也要显示**。只列找到的，会让人以为没列出来的地方都干净。
 */
const REL_CN = {
  risk: ["风险", "rel-risk"],
  support: ["支撑", "rel-support"],
  insufficient: ["证据不足", "rel-insufficient"],
  related: ["相关", "rel-related"],
};
const SRC_CN = { rule: "规则命中", dimension: "维度相关", goal: "目标探针", term: "关键词" };

/* 对照矩阵的结论词：与后端 tools/match_matrix.py 的 STATUS_CN 保持一致。
   「未提及」不是「不具备」，措辞上必须让人一眼看出这是"没写"，不是"不会"。 */
const MX_CN = {
  met: "✅ 满足",
  unmet: "❌ 不满足",
  absent: "— 未提及",
  partial: "△ 部分未提及",
  unclear: "? 无法判定",
  mentioned: "· 双方均提及",
};

function currentPurpose() {
  const el = $("#purposeInput");
  const v = el ? el.value.trim() : "";
  return v ? v.slice(0, 300) : "";
}

/** 审查目的面板开合（与上传同一套交互） */
function setPurposeOpen(open) {
  const panel = $("#purposePanel");
  const btn = $("#purposeToggle");
  if (!panel || !btn) return;
  panel.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", String(open));
  if (open) {
    const el = $("#purposeInput");
    if (el) setTimeout(() => el.focus(), 0);
  }
}

/**
 * 收起状态也要能看出"设没设过目的"。
 *
 * 目的不是装饰：填了它，报告会多出一整块目标与证据；忘掉自己设过，
 * 下一份文件的报告会莫名其妙多几段。所以副标题直接显示已填内容 + 「已设置」角标。
 * 附件目的同理：只显示"已附加 N 个目的文件"。
 */
function refreshPurposeEntry() {
  const el = $("#purposeInput");
  if (!el) return;
  const v = el.value.trim();
  const files = state.purposeFiles.length;
  const chars = state.purposeFiles.reduce((n, f) => n + (Number(f.chars) || 0), 0);
  const sub = $("#purposeToggleSub");
  const btn = $("#purposeToggle");
  const cnt = $("#purposeCount");
  if (sub) {
    if (v && files) {
      sub.textContent = `已填写 + ${files} 个目的文件`;
    } else if (files) {
      // 只传文件不写字也要看得出"目的已设置"，否则用户会以为空目的
      sub.textContent = `已附加 ${files} 个目的文件（${chars} 字）`;
    } else {
      sub.textContent = v
        ? "已填写：" + (v.length > 26 ? v.slice(0, 26) + "…" : v)
        : "点击填写：这份文本想干什么";
    }
  }
  if (btn) btn.classList.toggle("has-value", !!v || files > 0);
  if (cnt) cnt.textContent = `${el.value.length} / 300`;
}

function initPurpose() {
  const el = $("#purposeInput");
  if (!el) return;
  try {
    const saved = localStorage.getItem("ra_purpose") || "";
    if (saved) el.value = saved;
  } catch (e) { /* 隐私模式下 localStorage 不可用，忽略 */ }

  const toggle = $("#purposeToggle");
  if (toggle) {
    toggle.addEventListener("click", () =>
      setPurposeOpen($("#purposePanel").classList.contains("hidden")));
  }
  el.addEventListener("input", () => {
    try { localStorage.setItem("ra_purpose", el.value.slice(0, 300)); } catch (e) {}
    refreshPurposeEntry();
  });
  $$("#purposeChips .purpose-chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      el.value = chip.dataset.purpose || chip.textContent;
      try { localStorage.setItem("ra_purpose", el.value); } catch (e) {}
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
      refreshPurposeEntry();
    })
  );
  const clear = $("#purposeClear");
  if (clear) {
    clear.addEventListener("click", () => {
      el.value = "";
      try { localStorage.removeItem("ra_purpose"); } catch (e) {}
      refreshPurposeEntry();
      el.focus();
    });
  }
  refreshPurposeEntry();
  initPurposeFiles();
}

/* ---------------- 审查目的：上传目的文件 ----------------
 * 与「待审对象上传」是两件事：那里传的是**被审的文件**，这里传的是
 * **说明材料**（审查要求 / 检查清单 / 客户邮件…），摄取成文本后并入目的。
 * 后端暂存在 state/purpose_uploads/，不进 targets/，不会污染待审列表。
 *
 * 交互与待审上传对齐：整块虚线区可点选、可拖放、可一次多选。
 * 多选时**串行上传**：暂存区是「读列表 → 判上限 → 落盘」的流程，
 * 并发上传会让多个请求同时看到同一个「未满」状态，冲过 5 个的上限。
 */
const PURPOSE_FILE_MAX = 5;

let purposeBusy = false;          // 队列 worker 是否在跑
const purposePending = [];        // 待上传队列（多选 / 连续拖放都可能一次来一批）

function setPurposeStatus(text, isErr) {
  const el = $("#purposeFileStatus");
  if (!el) return;
  if (!text) {
    el.classList.add("hidden");
    el.textContent = "";
    el.classList.remove("err");
    return;
  }
  el.classList.remove("hidden");
  el.classList.toggle("err", !!isErr);
  el.textContent = text;
}

function hasFilePayload(e) {
  const dt = e.dataTransfer;
  return !!(dt && Array.from(dt.types || []).includes("Files"));
}

function renderPurposeFiles() {
  const box = $("#purposeFileList");
  if (!box) return;
  if (!state.purposeFiles.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = state.purposeFiles.map((f) => `
    <div class="purpose-file-chip" data-id="${esc(f.id)}">
      <span class="purpose-file-ico" aria-hidden="true">${f.kind === "image" ? "🖼" : f.kind === "pdf" ? "📕" : f.kind === "docx" ? "📘" : f.kind === "zip" ? "📦" : "📄"}</span>
      <span class="purpose-file-name" title="${esc(f.preview || "")}">${esc(f.name)}</span>
      <span class="purpose-file-meta">${f.chars} 字${f.truncated ? " · 已截断" : ""}</span>
      <span class="purpose-file-remove" data-remove="${esc(f.id)}" title="移除该目的文件">✕</span>
    </div>`).join("");
}

async function loadPurposeFiles() {
  const data = await api("/api/purpose/files");
  state.purposeFiles = (data && data.items) || [];
  renderPurposeFiles();
  refreshPurposeEntry();
}

/** 上传单个目的文件。返回 {ok,name,chars,truncated,warnings} 或 {ok:false,error} —— 不 toast，由队列汇总。 */
async function uploadOnePurposeFile(file) {
  const res = await fetch(
    `${API_BASE}/api/purpose/upload?name=${encodeURIComponent(file.name)}`,
    { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Review-Console": "1" }, body: file }
  );
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!data || !data.ok) {
    return { ok: false, name: file.name,
             error: (data && data.error) || `上传失败（HTTP ${res.status}）` };
  }
  // 服务端每次返回完整列表：以它为准，前端不自己 push（避免顺序/去重不一致）
  state.purposeFiles = data.items && data.items.length ? data.items : state.purposeFiles;
  renderPurposeFiles();
  refreshPurposeEntry();
  return { ok: true, name: data.name, chars: data.chars || 0,
           truncated: !!data.truncated, warnings: data.warnings || [] };
}

/** 批量入口：拖放 / 多选都走这里，串行消费队列。 */
async function addPurposeFiles(fileList) {
  const files = Array.from(fileList || []).filter((f) => f && f.name);
  if (!files.length) return;
  purposePending.push(...files);
  if (purposeBusy) return;                 // 已有 worker：新文件排队，不重复起 worker

  purposeBusy = true;
  const drop = $("#purposeFileBtn");
  const input = $("#purposeFileInput");
  if (drop) drop.disabled = true;

  let okCount = 0, okChars = 0, done = 0;
  const fails = [], warnings = [];

  try {
    while (purposePending.length) {
      // 上限在**每个文件上传前**复判：批量选择时不能只在入口判一次
      if (state.purposeFiles.length >= PURPOSE_FILE_MAX) {
        const left = purposePending.splice(0, purposePending.length);
        fails.push(`目的文件最多 ${PURPOSE_FILE_MAX} 个，已忽略 ${left.length} 个`);
        break;
      }
      const file = purposePending.shift();
      done += 1;
      setPurposeStatus(`正在上传 ${done}/${done + purposePending.length}：${file.name}`);
      try {
        const r = await uploadOnePurposeFile(file);
        if (r.ok) {
          okCount += 1;
          okChars += r.chars || 0;
          warnings.push(...(r.warnings || []));
        } else {
          fails.push(`${r.name}：${r.error}`);
        }
      } catch (e) {
        fails.push(`${file.name}：${e.message || e}`);
      }
    }
  } finally {
    purposeBusy = false;
    purposePending.length = 0;
    if (drop) drop.disabled = false;
    if (input) input.value = "";           // 允许再次选同一个文件
    setPurposeStatus("");
  }

  if (okCount) {
    toast(`已附加 ${okCount} 个目的文件（共 ${okChars} 字）`);
  }
  if (fails.length) {
    toast(fails[0] + (fails.length > 1 ? `（另有 ${fails.length - 1} 个失败）` : ""), true);
  }
  const warn = Array.from(new Set(warnings))[0];
  if (warn) setTimeout(() => toast(`摄取提示：${warn}`), 600);
}

async function removePurposeFile(id) {
  const data = await api(`/api/purpose/files?id=${encodeURIComponent(id)}`,
                         { method: "DELETE" });
  if (!data || !data.ok) {
    toast((data && data.error) || "移除失败", true);
    return;
  }
  state.purposeFiles = data.items || [];
  renderPurposeFiles();
  refreshPurposeEntry();
}

function initPurposeFiles() {
  const drop = $("#purposeFileBtn");      // 整块虚线区本身就是一个 button
  const input = $("#purposeFileInput");
  const panel = $("#purposePanel");

  if (drop && input) {
    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", () => addPurposeFiles(input.files));
    // 拖放到虚线区：必须 stopPropagation，否则冒泡到面板的 drop 会二次上传
    ["dragenter", "dragover"].forEach((ev) =>
      drop.addEventListener(ev, (e) => {
        if (!hasFilePayload(e)) return;
        e.preventDefault();
        e.stopPropagation();
        drop.classList.add("drag");
      })
    );
    ["dragleave", "dragend", "drop"].forEach((ev) =>
      drop.addEventListener(ev, () => drop.classList.remove("drag"))
    );
    drop.addEventListener("drop", (e) => {
      if (!hasFilePayload(e)) return;
      e.preventDefault();
      e.stopPropagation();
      addPurposeFiles(e.dataTransfer.files);
    });
  }

  // 移除按钮用事件委托绑在常驻容器上：innerHTML 重渲染后逐行绑定会失效
  const list = $("#purposeFileList");
  if (list) {
    list.addEventListener("click", (e) => {
      const remove = e.target.closest("[data-remove]");
      if (!remove) return;
      e.stopPropagation();
      removePurposeFile(remove.dataset.remove);
    });
  }

  // 面板任意位置也能接拖放（拖到 textarea 上不必精确对准虚线区），与待审上传互不干扰
  if (panel) {
    ["dragenter", "dragover"].forEach((ev) =>
      panel.addEventListener(ev, (e) => {
        if (!hasFilePayload(e)) return;
        e.preventDefault();
        e.stopPropagation();
        panel.classList.add("drag");
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      panel.addEventListener(ev, () => panel.classList.remove("drag"))
    );
    panel.addEventListener("drop", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      e.preventDefault();
      e.stopPropagation();
      addPurposeFiles(e.dataTransfer.files);
    });
  }
  loadPurposeFiles();
}

/* ===========================================================
   报告视图：简洁版裁定界面
   —— 模块顺序刻意与「裁定结果样本/审查进度与裁定-简洁版.html」一致：
   状态条 → 审查目的 → 裁定 → 对照矩阵 → 证据 / 标注 / 汇总 / 清单 → 技术参数。
   每块自己渲染自己，renderReport() 只是按顺序叫一遍，缺数据就整块不出现。
   =========================================================== */

/** 目标卡片的中文标题与「结论词」：found / 未 found 两种说法 */
const GOAL_CARD = {
  criteria_coverage: ["覆盖核查", "逐项覆盖", "有缺项"],
  evidence_of_competence: ["证据核验", "证据充分", "证据不足"],
  gap_and_risk: ["差距与风险", "已写明", "未写全"],
};

/**
 * 把 purpose.text 拆成「手写部分」+「每个目的文件的正文」。
 *
 * 后端把上传文件拼成 `【目的文件：名字】正文`，直接原样渲染的话，
 * 用户会在「审查目的」里看到几千字的岗位说明书贴在同一段里。
 * 这里拆开：手写目的留在正文位，文件正文进折叠区——目的本身一眼能读完。
 */
function splitPurpose(raw) {
  const text = String(raw || "");
  const parts = text.split(/【目的文件：([^】]{1,160})】/);
  const typed = (parts[0] || "").trim();
  const files = [];
  for (let i = 1; i < parts.length; i += 2) {
    files.push({ name: (parts[i] || "").trim(), body: (parts[i + 1] || "").trim() });
  }
  return { typed, files };
}

/** 折叠「目的文件原文」：按钮状态与内容一起翻，避免出现"按钮说展开、内容却收着" */
function toggleDetail(btn) {
  const body = document.getElementById(btn.dataset.target || "");
  if (!body) return;
  const open = !body.classList.toggle("hidden");
  btn.classList.toggle("open", open);
  btn.setAttribute("aria-expanded", String(open));
  const label = btn.querySelector(".rs-dtlabel");
  if (label) {
    const next = open ? (btn.dataset.openLabel || "收起") : (btn.dataset.closeLabel || "展开");
    label.textContent = next;
  }
  return open;
}

/* ---------------- ① 状态条 ---------------- */
function renderStatusBar(report) {
  const box = $("#rsStatus");
  if (!box) return;
  const stats = report.stats || {};
  const cov = report.coverage || {};
  const issues = report.issues || [];
  const agentName = (report.agent && report.agent.name) || "";
  const severe = (stats.blocker || 0) + (stats.major || 0);
  const verdict = report.verdict;

  // 三种裁定各自一句人话：判「通过」是不给的，pass 必须自带"不等于通过"
  const badge = verdict === "fail"
    ? `存在 ${stats.blocker || 0} 条阻断问题`
    : verdict === "warn" ? "需整改后使用" : "未发现 — 不等于通过";
  const tone = verdict === "fail" ? "bad" : verdict === "warn" ? "warn" : "ok";
  const badgeDesc = verdict === "fail"
    ? "不可直接使用：存在阻断性问题"
    : verdict === "warn" ? "存在严重问题，需整改后使用" : "未发现异议，但引擎有已知漏报，需人工复核";

  // 覆盖：不写会让人误以为全文都审过了——长文截断时这是最危险的错觉
  const charsTotal = Number(cov.chars_total || 0);
  const charsReviewed = Number(cov.chars_reviewed != null ? cov.chars_reviewed : charsTotal);
  const covPct = cov.text_coverage_pct != null
    ? Number(cov.text_coverage_pct)
    : (charsTotal ? Math.round((charsReviewed / charsTotal) * 100) : 100);
  const incomplete = cov.complete === false
    || (cov.input_truncated_dimensions || []).length > 0
    || (cov.output_truncated_dimensions || []).length > 0;
  let covHtml = "";
  if (charsTotal) {
    covHtml = incomplete
      ? `<span class="verdict-cover warn">⚠ 仅审查了 ${esc(String(covPct))}%`
        + `（${esc(charsReviewed.toLocaleString())} / ${esc(charsTotal.toLocaleString())} 字符）</span>`
      : `全文覆盖 <span class="cov">${esc(charsTotal.toLocaleString())} 字符</span>`;
  }
  const issueText = severe
    ? `${severe} 条严重问题`
    : issues.length ? `${issues.length} 条异议` : "未发现异议";

  // 情境 chip：让人一眼知道"这次没报某类问题，是因为整套规则就没问"
  // 而不是"审了没问题"。disabled_detail 里带每条规则的原意，悬停可查。
  const scn = report.scenario || null;
  const scnRules = (scn && scn.disabled_rules) || [];
  const scnChip = scn
    ? `<span class="rs-scn" title="${esc(
        (scn.disabled_detail || []).map((x) => `${x.rule_id}｜${x.desc || ""}`).join("\n")
        || "本情境未关闭任何规则")}">情境：${esc(scn.name || scn.id)}`
      + (scnRules.length ? ` · 关闭 ${esc(String(scnRules.length))} 条规则` : "")
      + `</span>`
    : "";

  // 格式规格 chip：版式结论只在"选定了委托方标准"时才有意义。
  // 没选时必须显示「未提供标准」，否则读者会以为"没报版式问题 = 版式合规"。
  const fmt = report.format_spec || null;
  const fmtCrit = (fmt && fmt.criteria) || {};
  const fmtChip = fmt
    ? `<span class="rs-fmt${fmt.id === "none" ? " is-none" : ""}" title="${esc(
        Object.entries(fmtCrit).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join("\n")
        || "本规格未规定任何版式数值")}${fmt.source ? "\n出处：" + fmt.source : ""}">`
      + (fmt.id === "none" ? "版式：未提供标准" : `版式：${esc(fmt.name || fmt.id)}`)
      + `</span>`
    : "";

  box.innerHTML = `<span class="rs-badge ${tone}" title="${esc(badgeDesc)}"><i></i>${esc(badge)}</span>
    <span class="rs-sid">审查结果 ${esc(report.review_id || "")}<span class="rs-tag">${esc(report.review_mode || "rule-only")}${agentName ? " · " + esc(agentName) : ""}</span></span>
    <div class="rs-meta">
      <div>${esc(basename(report.target_path))} · ${esc(relTime(report.timestamp))}</div>
      <div>${covHtml}${covHtml ? '<span class="rs-sep">·</span>' : ""}${esc(issueText)}${scnChip}${fmtChip}</div>
    </div>`;
}

/* ---------------- ② 审查目的 ---------------- */

function renderPurpose(report) {
  const box = $("#purposeBlock");
  if (!box) return;
  const p = report && report.purpose;
  if (!p || !String(p.text || "").trim()) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");

  const { typed, files } = splitPurpose(p.text);
  const rawText = String(p.text || "");
  const goals = p.goals || [];
  const evs = p.evidence || [];
  const found = goals.filter((g) => g.status === "found").length;

  // 来源：手写 / 目的文件各算一份，让"文件真的参与了审查"看得见
  const hasTyped = !!typed && typed !== "【目的文件：";
  const srcBits = [];
  if (hasTyped) srcBits.push("手写");
  if (files.length) srcBits.push(`${files.length} 个目的文件`);
  const srcLine = srcBits.length
    ? `<span class="purpose-src" title="目的文本的来源，全部参与本轮审查">来源：${esc(srcBits.join(" + "))}</span>`
    : "";

  // 手写目的原样保留换行；文件正文进折叠区（见下）
  const typedHtml = typed
    ? esc(typed).replace(/【目的文件：([^】]{1,120})】/g,
      '<span class="purpose-file-mark">【目的文件：$1】</span>')
    : `<span class="rs-dim">未填写文字目的${files.length ? "，以下内容全部来自目的文件" : ""}</span>`;

  const stats = [];
  if (goals.length) {
    stats.push(`<div class="rs-stat"><span class="n num">${found}/${goals.length}</span><span class="k">目标命中</span></div>`);
  }
  stats.push(`<div class="rs-stat"><span class="n num">${evs.length}</span><span class="k">证据条数</span></div>`);

  const fileLine = files.map((f) => `<div class="rs-file">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
      <span class="fn">目的文件：${esc(f.name)}</span>
    </div>`).join("");

  // 折叠区：文件正文 + 关键词。默认收起——目的本身要一眼读完，
  // 但"到底按什么标准判的"必须可查，否则裁定变成黑箱。
  let detail = "";
  const bodies = files.filter((f) => f.body);
  const terms = (p.terms || []);
  if (bodies.length || terms.length) {
    const bodyHtml = bodies.map((f) => `<h4>【目的文件：${esc(f.name)}】</h4>
      <div class="rs-dttext">${esc(f.body)}</div>`).join("")
      + (terms.length
        ? `<div class="rs-terms">目的关键词：${terms.map((t) => `<code>${esc(t)}</code>`).join(" ")}</div>`
        : "");
    detail = `<div class="rs-detail">
      <button class="rs-dtbtn" type="button" data-target="purposeDtBody"
              data-open-label="收起目的文件原文" data-close-label="查看目的文件原文"
              aria-expanded="false">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
        <span class="rs-dtlabel">查看目的文件原文</span>
      </button>
      <div class="rs-dtbody hidden" id="purposeDtBody">${bodyHtml}</div>
    </div>`;
  }

  // 意图：这一轮按什么标准在判。没有就明说"只按关键词定位"，不留空白让人猜
  const intents = (p.intents || []).map((i) =>
    `<span class="rs-intent" title="${esc((i.matched_keywords || []).join("、"))}">${esc(i.name)}</span>`).join("");
  const intentLine = intents
    ? `<div class="rs-intents">${intents}</div>`
    : `<div class="rs-note">未匹配到已登记的审查意图：本轮只按关键词定位，没有可派生的判定目标。</div>`;

  box.innerHTML = `<h2>审查目的<span class="rs-h2note">${srcLine}</span></h2>
    <div class="rs-purpose">
      <p class="rs-pq">${typedHtml}</p>
      <div class="rs-stats">${stats.join("")}</div>
    </div>
    ${fileLine}
    ${detail}
    ${intentLine}`;
}

/* ---------------- ③ 裁定：逐项结论 ---------------- */

function renderVerdicts(report) {
  const box = $("#verdictBlock");
  if (!box) return;
  const p = report && report.purpose;
  const goals = (p && p.goals) || [];
  if (!goals.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");

  const intentName = ((p.intents || [])[0] || {}).name || "";
  const cards = goals.map((g, i) => {
    const found = g.status === "found";
    const meta = GOAL_CARD[g.id] || [`裁定 ${i + 1}`, "已回应", "未回应"];
    const by = g.decided_by || [];
    // 「找到」有歧义：探针命中＝原文有这类表述，语义命中＝原文回应了这个问题
    const judgeOnly = by.includes("judge") && !by.includes("probe") && !by.includes("rule");
    const how = found ? (judgeOnly ? "语义命中" : "规则/探针命中") : "原文未见回应";
    const chips = (g.evidence_ids || []).map((id) =>
      `<button type="button" class="ev-chip" data-ev="${esc(id)}">${esc(id)}</button>`).join("");
    // 语义裁决给的理由要看得见——只给「命中」，用户无法判断命中的对不对
    const note = String(g.note || "").replace(/^语义裁决：/, "").trim();
    const verdict = note || (found ? "原文已回应此目标" : "原文未回应此目标");
    return `<article class="rs-card${found ? "" : " is-gap"}">
      <h3>${esc(meta[0])}<span class="rs-st ${found ? "good" : "gap"}">${esc(found ? meta[1] : meta[2])}</span></h3>
      <p class="rs-q">${esc(g.question)}</p>
      <p class="rs-v">${esc(verdict)}</p>
      <div class="rs-ev">${chips || `<span class="rs-ev-none">${esc(how)}</span>`}</div>
    </article>`;
  }).join("");

  box.innerHTML = `<h2>审查裁定<span class="rs-h2note">${intentName ? esc(intentName) + " · " : ""}${goals.length} 项</span></h2>
    <div class="rs-cards">${cards}</div>`;
}

/* ---------------- ④ 要求对照矩阵 ---------------- */

function renderMatrix(report) {
  const box = $("#matrixBlock");
  if (!box) return;
  const p = report && report.purpose;
  const mx = p && p.matrix && p.matrix.available ? p.matrix : null;
  if (!mx) {
    // 「没生成」本身是结论：说清原因，别让用户以为要求都对上了
    const reason = p && p.matrix && p.matrix.reason;
    if (reason) {
      box.classList.remove("hidden");
      box.innerHTML = `<h2>要求对照矩阵</h2><div class="rs-note">未生成对照矩阵：${esc(reason)}</div>`;
    } else {
      box.classList.add("hidden");
      box.innerHTML = "";
    }
    return;
  }
  box.classList.remove("hidden");

  const dims = mx.dims || [];
  const items = dims.flatMap((d) => (d.items || []).map((i) => ({ ...i, dim: d.name })));
  const rows = dims.map((d) => `<tr>
      <td class="mx-dim">${esc(d.name)}</td>
      <td title="${esc(d.req_quote || "")}">${esc(d.requirement || "—")}</td>
      <td class="mx-cand">${esc(d.candidate || "—")}</td>
      <td><span class="mx-status mx-s-${esc(d.status)}">${esc(MX_CN[d.status] || d.status)}</span></td>
    </tr>`).join("");
  const cov = mx.coverage || {};
  const notes = [];
  notes.push(`要求侧：${esc(mx.requirement_source || "—")} · 对照 ${esc(String(cov.total || 0))} 项`
    + dims.filter((d) => d.note && d.status === "met").map((d) => ` · ${esc(d.name)}：${esc(d.note)}`).join(""));
  if (items.length) {
    notes.push("技能逐项（出现 ≠ 掌握）：" + items.map((i) =>
      `<span class="mx-item ${i.present ? "on" : "off"}" title="${esc(i.where || "")}">${esc(i.name)}${i.present ? "" : " · 未出现"}</span>`).join(" "));
  }
  (mx.gaps || []).forEach((g) => notes.push(`差距：${esc(g)}`));
  (mx.limitations || []).forEach((l) => notes.push(esc(l)));

  box.innerHTML = `<h2>要求对照矩阵<span class="rs-h2note">${esc(String(cov.total || 0))} 项要求 · 满足 ${esc(String(cov.met || 0))}</span></h2>
    <div class="mx-block">
      <table class="mx-table">
        <colgroup><col class="m1"><col class="m2"><col class="m3"><col class="m4"></colgroup>
        <thead><tr><th>维度</th><th>要求侧</th><th>候选侧</th><th>结论</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="mx-notes">${notes.map((n) => `<span>${n}</span>`).join("")}</div>
    </div>`;
}

/* ---------------- ⑤ 原文证据 ---------------- */

function renderEvidence(report) {
  const box = $("#evidenceBlock");
  if (!box) return;
  const p = report && report.purpose;
  const evs = (p && p.evidence) || [];
  if (!evs.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  const items = evs.map((e) => {
    const rel = REL_CN[e.relation] || [e.relation, "rel-related"];
    const src = SRC_CN[e.source] || e.source;
    return `<div class="ev-item" data-start="${esc(e.start)}" data-quote="${esc(e.quote)}" data-ev="${esc(e.id)}">
      <div class="ev-head">
        <span class="ev-id">${esc(e.id)}</span>
        <span class="ev-rel ${rel[1]}">${esc(rel[0])}</span>
        <span class="ev-src">${esc(src)}${e.rule_id ? " · " + esc(e.rule_id) : ""}${e.judged ? " · 已裁决" : ""}</span>
        <span class="ev-jump">定位原文 ›</span>
      </div>
      <div class="ev-quote">“${esc(e.quote)}”</div>
      ${e.rationale ? `<div class="ev-why">${esc(e.rationale)}</div>` : ""}
    </div>`;
  }).join("");
  box.innerHTML = `<h2>原文证据<span class="rs-h2note">命中 ${evs.length} 条 · 点裁定里的 E 编号可定位</span></h2>
    <div class="rs-detail">
      <button class="rs-dtbtn" type="button" data-target="evBody"
              data-open-label="收起全部证据" data-close-label="展开全部证据（${evs.length} 条）"
              aria-expanded="false">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
        <span class="rs-dtlabel">展开全部证据（${evs.length} 条）</span>
      </button>
      <div id="evBody" class="hidden">
        <div class="ev-list">${items}</div>
      </div>
    </div>`;
}


/** 把字符偏移映射到 DOM 文本节点 */
function textNodeAt(root, offset) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  let acc = 0, node;
  while ((node = walker.nextNode())) {
    const len = node.nodeValue.length;
    if (acc + len >= offset) return { node, offset: Math.max(0, offset - acc) };
    acc += len;
  }
  return null;
}

/** 在原文上闪一下目标区间：用 Range 拿位置，再盖一个临时高亮层 */
function flashRange(root, start, end) {
  const a = textNodeAt(root, start);
  const b = textNodeAt(root, end);
  if (!a || !b) return null;
  const range = document.createRange();
  try {
    range.setStart(a.node, Math.min(a.offset, a.node.nodeValue.length));
    range.setEnd(b.node, Math.min(b.offset, b.node.nodeValue.length));
  } catch (e) { return null; }
  const rect = range.getBoundingClientRect();
  const host = root.getBoundingClientRect();
  if (!rect.height && !rect.width) return null;
  const el = document.createElement("div");
  el.className = "range-flash";
  el.style.left = (rect.left - host.left + root.scrollLeft) + "px";
  el.style.top = (rect.top - host.top + root.scrollTop) + "px";
  el.style.width = Math.max(rect.width, 6) + "px";
  el.style.height = Math.max(rect.height, 16) + "px";
  root.appendChild(el);
  setTimeout(() => el.remove(), 1800);
  return rect;
}

function locateEvidence(start, quote) {
  const root = $("#annotatedText");
  const report = state.currentReport || {};
  const text = report.target_text || "";
  if (!root || !text) {
    toast("这份报告没有原文快照，无法定位（请重新审查一次）", true);
    return;
  }
  const q = String(quote || "").replace(/…$/, "");
  let off = Number(start || 0);
  // 偏移和 quote 对不上（例如报告来自旧版本）就退回文本查找
  if (!q || text.slice(off, off + q.length) !== q) {
    const i = q ? text.indexOf(q) : -1;
    if (i < 0) { toast("原文中找不到这段引用", true); return; }
    off = i;
  }
  const rect = flashRange(root, off, off + Math.max(q.length, 6));
  if (!rect) return;
  const host = root.getBoundingClientRect();
  const delta = rect.top - host.top - host.height / 3;
  root.scrollTo({ top: root.scrollTop + delta, behavior: "smooth" });
  // 平滑滚动是异步的：补一次，保证真的落在视野内
  setTimeout(() => {
    const r2 = root.getBoundingClientRect();
    const rr = rect;
    if (rr.top < r2.top + 20 || rr.top > r2.bottom - 40) {
      root.scrollTop = root.scrollTop + (rr.top - r2.top) - 80;
    }
  }, 320);
}


function renderResearch(report) {
  const box = $("#researchBlock");
  if (!box) return;
  const rs = report.research || {};
  const items = rs.items || [];
  if (!rs.enabled || (!rs.checked && !(rs.errors || []).length)) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  const groups = [
    ["refuted", "反驳（原文与来源不一致）", "rs-bad"],
    ["supported", "支持（找到一致来源）", "rs-good"],
    ["unverified", "未找到可比来源", "rs-muted"],
  ];
  const rows = groups.map(([key, title, cls]) => {
    const list = items.filter((it) => it.status === key);
    if (!list.length) return "";
    return `<div class="research-group"><div class="research-group-title ${cls}">${title}</div>`
      + list.map((it) => `<div class="research-item">
          <div class="research-quote">${esc(it.quote || "")}</div>
          <div class="research-reason">${esc(it.reason || "")}</div>
          ${(it.sources || []).map((s) => s.url
            ? `<a class="research-source" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>`
            : "").join("")}
        </div>`).join("")
      + "</div>";
  }).join("");
  box.classList.remove("hidden");
  box.innerHTML = `<h2>事实核验（外部检索）<span class="rs-h2note">主张 ${rs.claims_found || 0} · 已查 ${rs.checked || 0} · 本地 ${rs.local_hits || 0} · 缓存 ${rs.cache_hits || 0} · 已学 ${rs.learned_hits || 0}</span></h2>
    <div class="research-hint">「未找到」不等于虚假；「反驳」也只说明检索来源与原文数值不一致，最终口径需人工确认。</div>
    ${rows || '<div class="rs-empty">本次没有可核验的库外事实主张。</div>'}`;
}


function renderActionPlan(report) {
  const box = $("#actionPlan");
  if (!box) return;
  const plan = report.action_plan || {};
  const items = plan.top || [];
  if (!items.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  const rows = items.map((it, i) => {
    const to = it.has_rewrite && it.to
      ? `<pre class="plan-to">${esc(it.to)}</pre>`
      : `<span class="plan-manual">需人工：${esc(it.how || "按依据改写")}</span>`;
    return `<tr>
      <td class="plan-no">${i + 1}</td>
      <td class="plan-where">${esc(it.where || "")}</td>
      <td class="plan-from">${esc(it.from || "（新增条款）")}</td>
      <td>${to}</td>
      <td class="plan-consequence">${esc(it.consequence || "")}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `<h2>整改单<span class="rs-h2note">${esc(plan.headline || "")}</span></h2>
    <div class="plan-hint">只列能落到具体文本的改法；「需人工」的条目在下方问题清单里给判断依据。</div>
    <div class="plan-wrap"><table class="plan-table">
      <thead><tr><th>#</th><th>位置</th><th>原文</th><th>改成</th><th>不改的后果</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}


function renderIssueList(report) {
  const box = $("#issueList");
  if (!box) return;
  const issues = report.issues || [];
  box.classList.remove("hidden");
  if (!issues.length) {
    box.innerHTML = `<h2>问题清单</h2>
      <div class="rs-empty">未发现异议 —— <b>未发现不等于通过</b>：引擎有已知漏报，请人工复核下方「本次审查边界」列出的盲区。</div>`;
    return;
  }
  const toFix = (report.to_fix || []).length;
  const cards = issues.map((issue) => {
    const severity = SEV[issue.severity] || SEV.minor;
    const confidence = issue.confidence != null ? Number(issue.confidence) : null;
    const confText = confidence == null ? "" : `置信度 ${confidence.toFixed(2)}`;
    const confirm = confidence != null && confidence < 0.7 ? "（待人工确认）" : "";
    const dim = DIM_FULL[issue.dimension] || issue.dimension;
    const quoteText = issue.fix && issue.fix.action === "insert"
      ? "（缺失条款，应在" + (issue.fix.where || "合同末尾") + "新增）"
      : issue.quote;
    return `<div class="issue">
      <div class="issue-header">
        <span class="issue-severity ${severity[1]}">${severity[0]} · ${esc(issue.rule_id)}</span>
        <span class="issue-dim">${esc(dim)}${issue.open_discovery ? " · 规则外发现" : ""}</span>
        <span class="issue-meta">${esc(issue.location || "")}${confText ? " · " + esc(confText) : ""}${confirm}</span>
      </div>
      <div class="issue-quote">"${esc(quoteText)}"</div>
      <div class="issue-evidence">${esc(issue.evidence)}</div>
      ${issue.fix && issue.fix.has_rewrite && issue.fix.to
        ? `<div class="issue-fix"><span class="issue-fix-label">${issue.fix.action === "insert" ? "新增条款" : "改为"}</span><pre>${esc(issue.fix.to)}</pre></div>`
        : ""}
      ${issue.fix && issue.fix.consequence
        ? `<div class="issue-consequence"><b>不改的后果：</b>${esc(issue.fix.consequence)}</div>`
        : ""}
      ${issue.suggestion ? `<div class="issue-suggestion">建议：${esc(issue.suggestion)}</div>` : ""}
    </div>`;
  }).join("");
  box.innerHTML = `<h2>问题清单<span class="rs-h2note">${issues.length} 条 · 待整改 ${toFix} 条（blocker + major）</span></h2>
    ${cards}`;
}

function renderExcluded(report) {
  const el = $("#excludedBlock");
  const excluded = report.excluded || [];
  if (!excluded.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = '<div class="limitations-title">已排除（模型自否，不计入异议）</div><ul>'
    + excluded.map((x) =>
      `<li>${esc(x.rule_id || "")} — ${esc(String(x.quote || "").slice(0, 60))}`
      + `<span class="excluded-reason">（${esc(x.reason || "模型自否")}）</span></li>`).join("")
    + "</ul>";
}

function renderLimitations(report) {
  const el = $("#limitations");
  const limitations = report.limitations || [];
  if (!limitations.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = '<div class="limitations-title">本次审查边界 · 这些地方没看到，别当成"没问题"</div><ul>'
    + limitations.map((item) => `<li>${esc(item)}</li>`).join("")
    + "</ul>";
}

/** 技术参数：一条细线，回答"这份裁定靠什么跑出来的" */
function renderTech(report) {
  const box = $("#rsTech");
  if (!box) return;
  const cov = report.coverage || {};
  const ex = cov.rule_execution_counts || {};
  const p = report.purpose || {};
  const judge = p.judge || {};
  const { typed, files } = splitPurpose(p.text);
  const srcBits = [];
  if (typed && typed !== "【目的文件：") srcBits.push("手写");
  if (files.length) srcBits.push(`${files.length} 个目的文件`);

  const bits = [];
  if (ex.deterministic != null || ex.prompt_only != null) {
    bits.push(`规则 确定性 <b>${esc(String(ex.deterministic || 0))}</b> / 仅提示 <b>${esc(String(ex.prompt_only || 0))}</b>`);
  }
  bits.push(`事实断言 <b>${esc(String(cov.fact_assertions_matched || 0))}</b> / <b>${esc(String(cov.fact_assertions_total || 0))}</b>`);
  bits.push(`语义裁决：<b>${esc(judge.mode === "rule+llm" ? "模型" : "未启用")}</b>`);
  if (srcBits.length) bits.push(`目的来源：<b>${esc(srcBits.join(" + "))}</b>`);
  if (report.agent && report.agent.version) {
    bits.push(`${esc(report.agent.name || report.agent.id)} <b>v${esc(report.agent.version)}</b>`);
  }
  if (report.scenario) {
    const sc = report.scenario;
    const off = sc.disabled_rules || [];
    bits.push(`情境 <b>${esc(sc.name || sc.id)}</b>`
      + (off.length ? `（关闭 ${esc(String(off.length))} 条：${esc(off.join("、"))}）` : ""));
  }
  // 格式规格：写明"按哪套标准判、判了哪几项"。没选时说明"版式未判"，
  // 不能留空——留空会被读成"版式没问题"。
  if (report.format_spec) {
    const fs = report.format_spec;
    const keys = Object.keys(fs.criteria || {});
    bits.push(`版式规格 <b>${esc(fs.name || fs.id)}</b>`
      + (keys.length ? `（判 ${esc(String(keys.length))} 项：${esc(keys.join("、"))}）`
        : "（<b>未判</b>：未提供格式标准）"));
  }
  box.innerHTML = `<b>技术参数</b> · ${bits.join(" · ")}`;
}

function renderReport(report) {
  $("#resultEmpty").classList.add("hidden");
  $("#resultView").classList.remove("hidden");
  applyAgentMeta(report);

  // 顺序即阅读顺序：先给结论与判据，再给原文依据
  renderStatusBar(report);
  renderPurpose(report);
  renderVerdicts(report);
  renderMatrix(report);
  renderEvidence(report);
  renderAnnotatedText(report);
  renderIssueSummary(report);
  renderActionPlan(report);
  renderResearch(report);
  renderIssueList(report);
  renderExcluded(report);
  renderLimitations(report);
  renderTech(report);

  // 新报告一出来，视线回到顶部状态条：先看"能不能用"，再往下看依据
  const card = $("#page-review .result-card");
  if (card) card.scrollTop = 0;
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
  loadCandidates();
}

function updateRulesTitle() {
  const enabled = state.rules.filter((r) => r.enabled).length;
  $("#rulesTitle").textContent = `规则库（${enabled} 条生效中 / 共 ${state.rules.length} 条）`;
}

/**
 * 规则执行方式标签。
 *
 * 「仅提示」必须显眼：这类规则的结论完全靠模型判断，跨次会漂移；
 * 和控制台里其它灰色小字放一起会让人误以为同样是代码判定。
 */
function execTag(cap) {
  const c = cap || {};
  const kind = c.kind || "prompt_only";
  const label = c.label || "仅提示";
  const tip = c.executor || "没有确定性执行器，只作为模型提示";
  return `<span class="exec-tag exec-${esc(kind)}" title="${esc(tip)}">${esc(label)}</span>`;
}

function renderRules() {
  const body = $("#rulesBody");
  const items = state.rules.filter(
    (r) => state.ruleFilter === "all" || r.dimension === state.ruleFilter
  );
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#9ca3af;padding:32px;">没有匹配的规则</td></tr>';
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
      <td>${execTag(rule.capability)}</td>
      <td><div class="switch${rule.enabled ? " on" : ""}" role="switch"
        aria-checked="${rule.enabled}" data-rule="${esc(rule.rule_id)}"></div></td>
    </tr>`;
  }).join("");
}

/* ---------------- 待人工审核的规则候选 ---------------- *
 *
 * 这一块回答的是「学习闭环往规则库里加东西之前，人能看到什么」：
 *   类型（这条规则在改哪个维度）→ 匹配模式 → 来源证据 → 门禁指标 → 人审记录。
 * 按钮只写候选文件的 human_review 字段，**规则库一个字节都不动**：
 * 写库仍然只有 tools/rule_promote.py 一条路径（见 docs/自学习闭环.md）。
 */
const DIM_HINT = {
  fact: "数字/日期/机构断言应可核验；自动规则只能做措辞级提示",
  logic: "一致性/矛盾/推理；自动规则只能做措辞级提示",
  safety: "危险操作/隐私/注入；自动通道禁止，必须人工确认",
  compliance: "合同要件/披露；自动通道禁止，必须人工确认",
  style: "书面表达规范；自动通道允许",
};
const CAND_STATUS = {
  proposed: "待过门禁",
  passed: "门禁通过",
  rejected: "门禁拒绝",
  approved: "已批准",
  promoted: "已晋升",
  rolled_back: "已回滚",
};

async function loadCandidates() {
  const data = await api("/api/candidates");
  if (!data || !data.ok) {
    $("#pendingPanel").innerHTML =
      '<div class="pending-empty">候选读取失败（接口未就绪或服务已停止）。</div>';
    return;
  }
  state.candidates = data.items || [];
  state.pendingCount = data.pending || 0;
  updatePendingBadge();
  renderPending();
}

function updatePendingBadge() {
  const n = state.pendingCount || 0;
  const badge = $("#pendingBadge");
  const btn = $("#pendingToggle");
  if (!badge || !btn) return;
  badge.textContent = String(n);
  badge.classList.toggle("is-zero", n === 0);
  btn.classList.toggle("has-pending", n > 0);
  btn.title = n
    ? `${n} 条规则候选等待人工审核（点击展开）`
    : "当前没有待审候选（点击可回看历史候选）";
}

function togglePending(force) {
  const box = $("#pendingPanel");
  const btn = $("#pendingToggle");
  if (!box || !btn) return;
  const open = force != null ? force : box.hasAttribute("hidden");
  if (open) box.removeAttribute("hidden"); else box.setAttribute("hidden", "");
  btn.setAttribute("aria-expanded", String(open));
  btn.classList.toggle("is-open", open);
  if (open) renderPending();
}

function renderPending() {
  const box = $("#pendingPanel");
  if (!box) return;
  const items = state.candidates || [];
  if (!items.length) {
    box.innerHTML =
      '<div class="pending-empty">暂无候选规则。学习闭环只在有<b>人工漏检标注</b>时产出候选：' +
      '先 <code>tools/annotate.py</code> 标注漏检 → 再 <code>tools/rule_miner.py mine</code> 生成。</div>';
    return;
  }
  const pending = items.filter((c) => c.pending).length;
  const head = pending
    ? `<div class="pending-summary">共 ${items.length} 条候选，其中 <b>${pending}</b> 条待你决定 ·
       门禁只回答「会不会误报」，是否<b>值得加</b>由你判断</div>`
    : `<div class="pending-summary">当前 <b>0</b> 条待决定；下面 ${items.length} 条是已有结论的历史提案。
       候选由「人工漏检标注 → 挖掘」产生：先在审查台标出漏检，才会出现新的待审项。</div>`;
  box.innerHTML = head + items.map(renderCandidate).join("");
}

function renderCandidate(c) {
  const dim = DIM[c.dimension] || [c.dimension, "dim-style"];
  const hint = DIM_HINT[c.dimension] || "";
  const sev = SEV[c.severity] || SEV.minor;
  const m = c.metrics || {};
  const gate = c.gate || {};
  const hr = c.human_review || {};
  const pending = !!c.pending;
  const reasons = (gate.reasons || [])
    .map((r) => `<li>${esc(r)}</li>`).join("");
  const prov = (c.provenance || [])
    .map((p) => `<li><code>${esc(p.doc)}</code>「${esc(p.quote)}」</li>`).join("");

  const bits = [
    `命中来源文档 ${m.hit_doc_count == null ? "—" : m.hit_doc_count} 份`,
    `误报 ${m.offtarget_count == null ? "—" : m.offtarget_count}`,
    `内部误报 ${m.internal_offtarget_count == null ? "—" : m.internal_offtarget_count}`,
  ];
  if (m.redundant_count) {
    const by = (m.redundant_covered_by || []).join("、") || "现有规则";
    bits.push(`<span class="warn-bit">已被 ${esc(by)} 覆盖 ${m.redundant_count} 处</span>`);
  }

  return `<div class="pending-item${pending ? " is-pending" : " is-settled"}">
    <div class="pending-top">
      <span class="dim-tag ${dim[1]}" title="${esc(hint)}">${esc(dim[0])}</span>
      <span class="pending-id">${esc(c.candidate_id)}</span>
      <span class="issue-severity ${sev[1]}">${esc(c.severity || "")}</span>
      <span class="pending-status st-${esc(c.status || "")}">${esc(CAND_STATUS[c.status] || c.status || "")}</span>
      ${pending ? '<span class="pending-flag">待决定</span>' : ""}
    </div>
    <div class="pending-desc">${esc(c.desc || "")}</div>
    <div class="pending-pattern"><span class="muted">匹配</span> <code>${esc(c.pattern || "—")}</code>${
      c.require ? ` <span class="muted">且同句含</span> <code>${esc(c.require)}</code>` : ""}${
      c.guard ? ` <span class="muted">豁免</span> <code>${esc(c.guard)}</code>` : ""}</div>
    <div class="pending-meta">${bits.join(" · ")}</div>
    ${reasons ? `<ul class="pending-reasons">${reasons}</ul>` : ""}
    ${prov ? `<details class="pending-prov"><summary>来源证据（${c.provenance.length} 条人工漏检标注）</summary><ul>${prov}</ul></details>` : ""}
    ${hr.decision ? `<div class="pending-human">人审：<b>${hr.decision === "approve" ? "批准" : "拒绝"}</b>
      · by ${esc(hr.by || "?")} · ${esc((hr.decided_at || "").slice(0, 19).replace("T", " "))}${
      hr.note ? ` · ${esc(hr.note)}` : ""}</div>` : ""}
    ${pending ? `<div class="pending-actions">
      <input class="pending-note" placeholder="备注（可选，写入候选文件）" maxlength="200">
      <button class="btn-mini approve" type="button" data-decide="approve" data-cid="${esc(c.candidate_id)}">批准</button>
      <button class="btn-mini reject" type="button" data-decide="reject" data-cid="${esc(c.candidate_id)}">拒绝</button>
    </div>` : ""}
  </div>`;
}

async function decideCandidate(cid, decision, note, btn) {
  if (btn) btn.disabled = true;
  const result = await api("/api/candidates/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_id: cid, decision, note: note || "", by: "console" }),
  });
  if (btn) btn.disabled = false;
  if (!result || !result.ok) {
    toast((result && result.error) || "记录失败", true);
    return;
  }
  toast(`${cid} 已记录人工${decision === "approve" ? "批准" : "拒绝"}（未改规则库）`);
  if (result.next_step) {
    toast("晋升仍需在命令行执行：rule_promote.py promote", false);
  }
  loadCandidates();
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
  if (state.scenario) payload.scenario = state.scenario;
  // 格式规格只对 .docx 有意义；不选就不传，让后端用默认 none（并写进 limitations）
  if (state.formatSpec) payload.format_spec = state.formatSpec;
  if (llmOverride === true || llmOverride === false) payload.llm = llmOverride;
  // 库外事实核验：默认勾选；关掉就只比对内置事实库（离线/涉密场景）
  const researchBox = document.getElementById("researchToggle");
  payload.research = !!(researchBox && researchBox.checked);
  const purpose = currentPurpose();
  if (purpose) payload.purpose = purpose;
  // 上传的目的文件：只传 id，文本由服务端从暂存区取出并组装
  // （超量 / id 过期会在启动前被服务端拒绝，不会跑一半才失败）
  if (state.purposeFiles.length) {
    payload.purpose_files = state.purposeFiles.map((f) => f.id);
  }
  const data = await api("/api/review/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!data.ok) { toast(data.error || "启动审查失败", true); return; }
  setPurposeOpen(false);       // 目的已随请求提交，收回面板腾出空间
  state.job = { id: data.job_id, status: "running", report_id: null };
  $("#resultEmpty").classList.add("hidden");
  renderProgress([], state.selected.name);
  updateRunState();
  pollJob();
}

function schedulePoll(delay) {
  clearTimeout(state.poll);
  state.poll = null;
  // 后台标签页不跑轮询：回到前台由 visibilitychange → resumePoll() 恢复。
  if (!state.job || document.hidden) return;
  state.poll = setTimeout(pollJob, delay);
}

// 网络抖动 / 服务重启时不要一次失败就断链：第 1、2 次静默退避，
// 连续 3 次才提示并收起任务、恢复按钮。成功一次就清零。
function retryPoll(message) {
  state.pollFails = (state.pollFails || 0) + 1;
  if (state.pollFails < 3) {
    schedulePoll(state.pollFails === 1 ? 1000 : 2000);
    return;
  }
  toast(message || "查询进度失败", true);
  state.pollFails = 0;
  state.job = null;
  state.poll = null;
  updateRunState();
}

function pausePoll() {
  if (state.poll) {
    clearTimeout(state.poll);
    state.poll = null;
  }
}

function resumePoll() {
  if (document.hidden || !state.job) return;
  if (state.job.status !== "running") return;
  if (state.poll || state.pollBusy) return;
  state.pollFails = 0;
  pollJob();
}

async function pollJob() {
  clearTimeout(state.poll);
  state.poll = null;
  if (!state.job) return;
  // 后台标签页暂停轮询，避免白跑请求；回前台时恢复。
  if (document.hidden) return;
  if (state.pollBusy) return;
  state.pollBusy = true;
  const jobId = state.job.id;
  try {
    let data;
    try {
      data = await api("/api/review/status?job=" + encodeURIComponent(jobId));
    } catch (e) {
      retryPoll((e && e.message) || "查询进度失败");
      return;
    }
    if (!data || !data.ok || !data.job) {
      retryPoll((data && data.error) || "查询进度失败");
      return;
    }
    state.pollFails = 0;
    state.job = data.job;
    renderProgress(data.steps || [], state.selected ? state.selected.name : "");
    if (data.job.status === "running") {
      // 正常节奏保持 350ms；失败重试才用 1s/2s。
      schedulePoll(350);
      return;
    }
    if (data.job.status === "done" && data.report) {
      state.currentReport = data.report;
      state.currentLog = data.report.review_id;
      renderReport(data.report);
      // 跑完就把步骤条收起：结论已经出来了，进度条再占一块只会把裁定往下推
      const progress = $("#progressCard");
      if (progress) progress.classList.add("hidden");
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
  } finally {
    state.pollBusy = false;
  }
}

async function runExample() {
  const sample = state.targets.find((t) => t.rel === "targets/answer_001.txt")
    || state.targets.find((t) => t.reviewable !== false);
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

/* ---------------- 布局：把导航/横幅/页脚的真实高度写进 CSS 变量 ----------------
 *
 * 三栏独立滚动需要「视口 − 导航 − 上下留白 − 横幅 − 页脚」这个高度。
 * 写死 104px 会算漏：页脚 67px、未配置 LLM 时横幅还有 44px，
 * 实测页面照样溢出 83px（scrollHeight 890 / 视口 807），一滚还是三栏一起走。
 * 所以这里量一次真实高度，交给 CSS 去算；窗口变化、横幅显隐都重新量。
 */
function syncViewportMetrics() {
  const root = document.documentElement;
  const h = (el) => (el ? Math.round(el.getBoundingClientRect().height) : 0);
  const px = (v) => parseFloat(v) || 0;
  const nav = document.querySelector(".nav");
  const banner = document.getElementById("llmBanner");
  const footer = document.querySelector(".footer");
  const container = document.querySelector(".container");
  const bannerH = banner && !banner.classList.contains("hidden") ? h(banner) : 0;

  // 上下留白不能写 48：container 的 margin-bottom 与 footer 的 margin-top 会**合并**
  // （实测 24 与 40 合并成 40），少算 16px 页面就又开始滚。
  let gap = 48;
  let footerH = 0;
  if (container && footer) {
    const cs = getComputedStyle(container);
    const fs = getComputedStyle(footer);
    gap = px(cs.marginTop) + Math.max(px(cs.marginBottom), px(fs.marginTop));
    footerH = h(footer);
  }
  root.style.setProperty("--review-nav-h", h(nav) + "px");
  root.style.setProperty("--review-banner-h", bannerH + "px");
  root.style.setProperty("--review-footer-h", footerH + "px");
  root.style.setProperty("--review-gap", gap + "px");
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
  // 横幅会占掉一截视口高度：三栏独立滚动的高度计算要减掉它，
  // 否则横幅在的时候三栏底部会被挤出屏幕（CSS 变量见 style.css 的 :root）
  document.body.classList.toggle("has-banner", !ok);
  syncViewportMetrics();
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
  $("#pendingToggle").addEventListener("click", () => togglePending());
  // 结果区统一委托：证据定位、目的折叠都在这一层，模块重渲染不会丢事件
  $("#resultView").addEventListener("click", (e) => {
    const dt = e.target.closest(".rs-dtbtn");
    if (dt) { toggleDetail(dt); return; }
    const chip = e.target.closest(".ev-chip");
    if (chip) {
      const id = chip.dataset.ev;
      const item = document.querySelector(`.ev-item[data-ev="${id}"]`);
      if (item) {
        // 证据默认折叠：点 E 编号时先把折叠区打开，否则 scrollIntoView 落在隐藏节点上没反应
        const wrap = item.closest("#evBody");
        if (wrap && wrap.classList.contains("hidden")) {
          const btn = wrap.parentElement.querySelector(".rs-dtbtn");
          if (btn) toggleDetail(btn);
        }
        item.scrollIntoView({ block: "nearest", behavior: "smooth" });
        item.classList.add("is-flash");
        setTimeout(() => item.classList.remove("is-flash"), 1200);
      }
      return;
    }
    const row = e.target.closest(".ev-item");
    if (row) locateEvidence(row.dataset.start, row.dataset.quote);
  });
  initPurpose();
  // 决定按钮用**委托**：候选列表每次决定后重渲染，逐条绑定会立刻失效
  $("#pendingPanel").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-decide]");
    if (!btn) return;
    const noteEl = btn.closest(".pending-actions")?.querySelector(".pending-note");
    decideCandidate(btn.dataset.cid, btn.dataset.decide, noteEl ? noteEl.value.trim() : "", btn);
  });
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

  syncViewportMetrics();
  // 窗口尺寸变化会改导航换行/页脚高度，重新量一次（rAF 去抖，拖拽窗口时不做无用功）
  let _metricsTick = null;
  window.addEventListener("resize", () => {
    if (_metricsTick) return;
    _metricsTick = requestAnimationFrame(() => {
      _metricsTick = null;
      syncViewportMetrics();
    });
  });

  initUpload();
  // 待审对象下拉必须真的生效：这里原来漏了监听，用户在下拉里选了 A，
  // 界面显示 A、state.selected 却还是 B，点运行时审的是 B（本次事故的直接原因）。
  $("#targetSelect").addEventListener("change", (e) => {
    const item = state.targets.find((x) => x.rel === e.target.value);
    if (item && item.reviewable === false) {
      toast("该文件是机读标注（标准答案），不能作为待审对象", true);
      if (state.selected) e.target.value = state.selected.rel;
      return;
    }
    selectTarget(e.target.value);
  });
  $("#runBtn").addEventListener("click", () => startReview());
  $("#annoMissBtn").addEventListener("click", markFalseNegative);
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

  // 历史列表的手动/被动刷新：命令行跑完审查后页面不会自己更新
  $("#historyRefresh").addEventListener("click", () => refreshReports({ force: true }));
  // ⭐ 只看收藏：纯前端筛选，不重新请求（收藏状态随 /api/reports 一起来）
  $("#historyFavFilter").addEventListener("click", () => {
    favoritesOnly = !favoritesOnly;
    updateHistoryHead();
    renderHistory();
    if (favoritesOnly && !favCount()) {
      toast("还没有收藏的报告：点每行右侧的 ☆ 即可收藏");
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      pausePoll();
      return;
    }
    refreshReports();
    resumePoll();
  });
  window.addEventListener("focus", () => refreshReports());
  // 页面卸载 = 轮询链必须断掉，不能留下 setTimeout 继续发请求。
  window.addEventListener("pagehide", pausePoll);
  window.addEventListener("beforeunload", pausePoll);

  await loadProviders();
  await loadLlmStatus();
  await loadTargets();
  await loadAgents();
  await loadScenarios();
  await loadFormatSpecs();
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

/* ---------------- 审查情境 ----------------
   同一份材料在不同用途下该问的问题不同：简历初筛不核验"四六级是不是真的"
   （要核实只能索要证书原件，批量初筛没有这个条件）。选情境 = 关掉一类问题，
   关了什么会写字报告 limitations；不改判定标准、不改严重度。 */
async function loadScenarios() {
  const sel = $("#scenarioSelect");
  const hint = $("#scenarioHint");
  if (!sel) return;
  let data;
  try {
    data = await api("/api/scenarios");
  } catch (e) {
    data = { ok: false, error: `读取情境失败：${e && e.message ? e.message : e}` };
  }
  if (!data || !data.ok) {
    // 配置坏了要明说：空下拉会被当成"没有情境可选"，用户看不出问题在哪
    sel.innerHTML = '<option value="">（情境配置读取失败）</option>';
    sel.disabled = true;
    if (hint) {
      hint.textContent = (data && data.error) || "情境配置读取失败";
      hint.classList.add("is-error");
    }
    return;
  }
  const items = data.items || [];
  state.scenarios = items;
  const usable = items.filter((x) => x.id !== (data.default || "general"));
  sel.innerHTML = [`<option value="">通用（全部规则）</option>`]
    .concat(usable.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}`
      + `（关 ${esc(String(x.disabled_count))} 条规则）</option>`))
    .join("");
  sel.disabled = !usable.length;
  const sync = () => {
    state.scenario = sel.value;
    const it = items.find((x) => x.id === sel.value);
    hint.classList.remove("is-error");
    if (!it) {
      hint.textContent = "全部规则参与（默认）";
      hint.classList.remove("is-warn");
      return;
    }
    hint.textContent = `${it.desc}${it.keep_note ? "｜仍然检查：" + it.keep_note : ""}`;
    hint.classList.toggle("is-warn", (it.disabled_count || 0) > 0);
  };
  sel.addEventListener("change", sync);
  sync();
}

/** 格式规格（docx 版式按哪套标准判）。
 *
 * 为什么要选：字体/字号/行距没有"通用正确答案"，只有"委托方要求"。
 * 「宋体小四、固定 22 磅」是某份课程报告的标准，换份期刊就全变。
 * 不选 = 不判这几项，并在报告 limitations 写明「未报出 ≠ 合规」。
 */
async function loadFormatSpecs() {
  const sel = $("#formatSpecSelect");
  const hint = $("#formatSpecHint");
  if (!sel) return;
  let data;
  try {
    data = await api("/api/format-specs");
  } catch (e) {
    data = { ok: false, error: `读取格式规格失败：${e && e.message ? e.message : e}` };
  }
  if (!data || !data.ok) {
    sel.innerHTML = '<option value="">（格式规格读取失败）</option>';
    sel.disabled = true;
    if (hint) {
      hint.textContent = (data && data.error) || "格式规格读取失败";
      hint.classList.add("is-error");
    }
    return;
  }
  const items = data.items || [];
  state.formatSpecs = items;
  const usable = items.filter((x) => x.id !== (data.default || "none"));
  sel.innerHTML = [`<option value="">不判版式（默认）</option>`]
    .concat(usable.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}`
      + (x.checks && x.checks.length ? `（${esc(x.checks.join("/"))}）` : "")
      + `</option>`))
    .join("");
  sel.disabled = !usable.length;
  const sync = () => {
    state.formatSpec = sel.value;
    const it = items.find((x) => x.id === sel.value);
    hint.classList.remove("is-error");
    if (!it) {
      hint.textContent = "仅对 .docx 生效：不判字体/字号/行距";
      hint.classList.remove("is-warn");
      return;
    }
    hint.textContent = `${it.desc}${it.source ? "｜出处：" + it.source : ""}`;
    hint.classList.toggle("is-warn", (it.disabled_count || 0) > 0);
  };
  sel.addEventListener("change", sync);
  sync();
}

document.addEventListener("DOMContentLoaded", init);
