#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
review.py —— 审查 Agent 主流程 S1~S6 (Demo v0.9.0)

定位：独立审查裁判。只读 targets/，所有产物只落到 outputs/ 与 logs/。
不修改原文件、不自动整改，只给出带证据（quote + rule_id + source + tool + confidence）的裁定。

用法：
    python review.py targets/answer_001.txt
    python review.py targets/contract_001.md
    python review.py targets/code_001.py
    python review.py --list
    python review.py targets/answer_001.txt --no-llm   # 强制纯规则审
    python review.py targets/answer_001.txt --llm      # 强制调用大模型 API
"""
import importlib.util
import asyncio
import atexit
import os
import sys
import re
import json
import glob
import time
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
import agent_spec
import purpose as purpose_mod
import scenario as scenario_mod
import format_spec as format_spec_mod
import match_matrix
import logic_checks
import pattern_rules
import regex_check
import rule_exec
import arith_check
import retrieve
import law_index
import law_cite
import fact_gap
import lint
import integrity
import llm_client
import llm_review
import remediation
import research
import version
# Architecture v1：裁定规则只有一份实现（领域策略）。旧流程调用它，不再自己复制一套。
from review_agent.domain import ids as domain_ids
from review_agent.domain import ReviewProfile, RunStatus, Stage
from review_agent.domain.enums import Verification
from review_agent.domain.errors import ConfigError, DomainError, InputError, NotFoundError
from review_agent.domain.policy import DEFAULT_POLICY
from review_agent.adapters.issue_codec import (
    CodecMismatch,
    EncodedIssue,
    decode_issue,
    encode_issues,
    export_issues,
)
from review_agent.capabilities import (
    BaseTool,
    Candidate,
    IngestContext,
    Permission,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    batch1_tools,
    ingest_candidates,
)
from review_agent.text import sentence_no as text_sentence_no
from review_agent.application import (
    FileTaskStore,
    PipelinePorts,
    ReportService,
    ReviewApplication,
    TaskOutcome,
    TaskService,
    audit_to_legacy,
    build_audit_stream,
    build_shadow_report,
    render_shadow_md,
    write_audit_stream,
)
from review_agent.runtime import (
    FileRunStore,
    MemoryCheckpointStore,
    NodeResult,
    Orchestrator,
    RunState,
    Workflow,
    state_to_dict,
    surviving_findings,
)
from review_agent.runtime.nodes import (
    CrossReviewerNode,
    DecisionEngineNode,
    DocumentAnalyzerNode,
    EvidenceAnalyzerNode,
    PlannerNode,
    RuleAnalyzerNode,
    SemanticStepOutcome,
    anchor_findings,
    build_decision,
    classify_target,
    coverage_from_meta as semantic_coverage_from_meta,
    covered_chars as semantic_covered_chars,
    dispute_to_report_dict,
    evidence_to_report,
    normalize_quote,
    plan_rule_steps,
    quote_overlap,
    review_findings as review_cross,
    run_rule_plan,
    verify_quote,
)

try:
    import yaml
except ImportError:
    print("请先安装 pyyaml：pip install pyyaml")
    sys.exit(1)

import rules_store  # noqa: E402
import docx_extract
import docx_format_checks

TARGETS = ROOT / "targets"
RULES = ROOT / "rules"
KNOWLEDGE = ROOT / "knowledge"
OUTPUTS = ROOT / "outputs"
LEDGER = ROOT / "ledger"
LOGS = ROOT / "logs"
STATE = ROOT / "state"
# 知识库缺口日志。None = 自动推断（见 _gap_log_path）：
# 测试把 OUTPUTS 重定向到临时目录时，缺口也跟着走，不会污染真实 state/。
GAP_LOG = None

# review_id 锁的兜底清理。正常路径会 unlink；run_one 中间抛异常时，
# 900 行的函数体没有统一的 finally（P1 拆分时再收），这里用 atexit 兜住
# 进程退出，避免 server 子进程异常后留下 .R-*.lock 挡住后续分配。
_ID_LOCKS = set()


def _release_id_locks():
    for lock in list(_ID_LOCKS):
        try:
            lock.unlink()
        except OSError:
            pass
    _ID_LOCKS.clear()


atexit.register(_release_id_locks)

for d in (OUTPUTS, LEDGER, LOGS):
    d.mkdir(exist_ok=True)

SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
# 版本号唯一事实源：根目录 VERSION（tools/version.py 读取）。
REPORT_MODEL_VERSION = version.report_version()
UNIT_FACTOR = {"亿": 1e8, "万": 1e4, "%": 0.01, "倍": 1.0, "": 1.0}

# ---- 结构化错误行（CLI → server 的错误分类通道）----
# 人类可读的错误照旧打在 stdout（既有输出与退出码都不变）；这一行打到 stderr，
# 让 server 不必再靠「猜 stderr 里那句话是什么意思」来区分输入问题与系统故障。
ERROR_MARKER = "REVIEW_ERROR "


def _emit_error(message, *, kind, http_status, retryable=False):
    try:
        payload = {"kind": kind, "http_status": http_status,
                   "retryable": bool(retryable), "message": str(message)[:500]}
        print(ERROR_MARKER + json.dumps(payload, ensure_ascii=False),
              file=sys.stderr, flush=True)
    except (OSError, TypeError, ValueError):
        # 错误上报失败不能覆盖原始错误
        pass


def _emit_domain_error(exc: DomainError):
    """按领域错误自带的分类上报（kind / http_status / retryable）。"""
    _emit_error(str(exc), kind=exc.kind, http_status=exc.http_status,
                retryable=exc.retryable)

# 事实审的确定性启发式（FACT-002/003/004）
SOURCE_CLAIM_RE = re.compile(
    r"(据统计|数据显示|研究表明|调查显示|报告显示|报告称|据[^，。；\n]{0,12}(显示|统计|报道|消息))")
SOURCE_MARK_RE = re.compile(
    r"(https?://|来源\s*[:：]|文献|《[^》]+》|\[\d+\]|统计口径|公开数据)")
CAUSAL_RE = re.compile(r"(因此|所以|由此可见|这说明|这意味着|导致|证明了)")
CAUSAL_EVIDENCE_RE = re.compile(
    r"(据|来源|数据|研究|实验|样本|统计|报告|https?://)")
# 因果句里的「成效/归因」词。有它 = 真在拿结果归因（"销量翻倍，因此定价策略成功"），
# 没有它 = 只是行文顺承（"因此我们下周补测"）。同一规则按这两种结构分档，
# 而不是靠调阈值——结构判断比分数判断可复现。（红队 B1）
EFFECT_RE = re.compile(
    r"(成功|成效|大幅|翻倍|倍增|归功于|归因|证明了|说明了|领先|第一|显著提升|显著增长)")
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
CALIBER_RE = re.compile(
    r"(同比|环比|占比|增长率|转化率|毛利率|覆盖率|完成率|口径|"
    r"占.{0,8}(总数|总人数|员工|人口|营收|收入)|比重|比例|份额)")
TIMES_RE = re.compile(r"\d+(?:\.\d+)?\s*倍")
# 合同/制度里的工资比例是**约定值**，不是对外披露的统计数据。
# 要求「试用期工资为转正工资的 85%」标同比/环比是范畴错误（金标准 c16 踩过）。
CONTRACT_RATIO_RE = re.compile(
    r"(试用期[^。；\n]{0,12}工资|转正工资|约定工资|工资[^。；\n]{0,6}的\s*\d+\s*[%％])")
BASELINE_RE = re.compile(r"(同比|环比|相比|较|增长|下降|提升|基线)")
NEGATIVE_PREFIXES = ("无", "没有", "不存在", "不涉及", "未", "已解决", "已处理", "不包含")

# 不同待审类型激活的审查维度
DIM_FULL_NAME = {"fact": "事实", "logic": "逻辑", "safety": "安全",
                 "compliance": "合规", "style": "风格", "format": "格式"}

# 只聚合「提示类」规则：口径、来源、口语化。
# 逻辑/事实类错误**绝不合并**——同一条规则下的每一处都可能是独立真错误。
ADVISORY_RULES = {"FACT-004", "COMP-004", "STYLE-002", "STYLE-003", "STYLE-005"}
AGGREGATE_MIN = 3          # 同规则出现 ≥3 次才聚合成一条

# 用词规范（STYLE-005）：只认「截止/截至 + 时间点」这一结构，
# 「报名截止日期」「投标截止时间」是正确用法，不能被这条规则误伤。
TYPO_WORD = re.compile(
    r"(截止\s*(?:目前|当前|现在|今日|今天|昨日|本月|上月|今年|去年|目前为止)|"
    r"帐户|帐目|按装|既使)")

MINOR_KEEP_PER_DIM = 8     # 长文分块时，同维度 minor/nit 在清单里最多展开的条数

DIM_FOR_TYPE = {
    "llm_response": ["fact", "logic", "safety", "style", "compliance"],
    "prompt": ["fact", "logic", "safety", "style", "compliance"],
    "contract": ["fact", "logic", "safety", "compliance", "style"],
    "code": ["safety", "style"],
    "docx": ["fact", "logic", "safety", "style", "compliance", "format"],
}

# 审查任务书：让模型知道「这次要判定什么、标准是什么、后果多大」
CHARTER_PATH = ROOT / "config" / "charter.json"
TYPE_PURPOSE = {
    "llm_response": "通用文本 / 对外或对内发布的内容",
    "contract": "合同 / 协议文本",
    "code": "源代码",
    "prompt": "提示词 / 指令文本",
    "docx": "Word 文档（.docx）",
}
STRICT_MAP = {
    "strict": "严格度：宁可多报可疑点，也要把每处矛盾的具体冲突双方指出来",
    "standard": "严格度：只报有明确原文依据、能指出冲突双方的问题",
    "lenient": "严格度：只报会造成实质后果的严重矛盾，忽略表述风格类问题",
}


def build_charter(target_type, agent=None):
    """构造「审查任务书」文本，注入每个维度的 prompt。

    三层来源，优先级从高到低：
      ① config/charter.json 的非空字段（本次审查的人工口径）
      ② Agent manifest 的 charter（这套判定标准的默认口径）
      ③ 内置按类型的默认值

    没有任务书的审查就是「泛扫一遍」——这是方向感缺失的根因；
    而安全审与文档审的「后果等级」根本不同（一个是可被利用，一个是可被引用），
    所以任务书必须跟着 Agent 走，不能全局一份。
    """
    cfg = {}
    try:
        cfg = json.loads(CHARTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    if agent is not None:
        merged = dict(getattr(agent, "charter", None) or {})
        merged.update({k: v for k, v in cfg.items()
                       if v not in (None, "", [], {}) and not str(k).startswith("_")})
        cfg = merged
    purpose = cfg.get("purpose") or TYPE_PURPOSE.get(target_type, "通用文本")
    lines = [
        f"用途：{purpose}",
        f"受众：{cfg.get('audience') or '未指定'}",
        f"后果等级：{cfg.get('consequence') or '未指定（按中等问题对待）'}",
        STRICT_MAP.get(cfg.get("strictness") or "standard", STRICT_MAP["standard"]),
    ]
    focus = cfg.get("focus")
    if isinstance(focus, list) and focus:
        lines.append("重点关注：" + "、".join(str(x) for x in focus))
    if cfg.get("notes"):
        lines.append(f"特别说明：{cfg['notes']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 基础工具函数
# ----------------------------------------------------------------------------
def now_iso():
    return datetime.datetime.now().astimezone().isoformat()


def _gap_log_path():
    """知识库缺口清单落哪。

    正常情况：`state/gap_log.jsonl`。
    **测试把 OUTPUTS 重定向到临时目录时，缺口日志跟着产物目录走**——
    否则跑一次全量回归就有几十份测试语料被当成"该补的缺口"写进真实清单，
    清单立刻变成噪声（工单只能靠人读，脏了就没人读了）。
    """
    if GAP_LOG is False:
        return None
    if GAP_LOG:
        return Path(GAP_LOG)
    out = Path(OUTPUTS)
    try:
        if out.resolve() != (ROOT / "outputs").resolve():
            return out.parent / "gap_log.jsonl"
    except OSError:
        pass
    return STATE / "gap_log.jsonl"


def _success_rate(tool_logs):
    completed = sum(1 for t in tool_logs if t["status"] in ("success", "failed"))
    success = sum(1 for t in tool_logs if t["status"] == "success")
    if completed:
        return f"{success}/{completed} = {success / completed * 100:.0f}%"
    return "N/A（全部 skipped）"


def load_rules():
    """只返回启用中的规则；启停状态在 config/rules.state.json。"""
    return rules_store.load_active_rules()


def rule_index(rules):
    return {r["rule_id"]: r for r in rules}


def detect_type_detail(path: Path, text: str):
    """类型判定 + 置信度 + 依据。

    实现只有一份，在文档分析节点里（`review_agent.runtime.nodes.document.classify_target`）：
    「标题与开头加权、正文里提到合同不算合同」这套规则必须两边一致，
    否则同一个文件在旧流程与新链路里会被判成两种类型，判定标准跟着变。
    """
    return classify_target(path, text)


def detect_type(path: Path, text: str):
    """兼容旧调用：只返回类型字符串。"""
    return detect_type_detail(path, text)["type"]


def load_source_meta(target_path: Path):
    """读取摄取元数据：若该文本是由 PDF / Word / 压缩包派生而来，返回来源信息。

    由 server.py 上传时写入 targets/uploads/_meta/<派生文件名>.json。
    读取失败一律返回 None，不影响审查主流程。
    """
    meta_path = TARGETS / "uploads" / "_meta" / f"{target_path.name}.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def sentence_at(text, idx):
    """返回包含 idx 的整句（逐字，用于 issue.quote）。"""
    punct = set("。！？\n")
    start = idx
    while start > 0 and text[start - 1] not in punct:
        start -= 1
    end = idx
    while end < len(text) and text[end] not in punct:
        end += 1
    return text[start:end].strip()


def _sent_start(text, idx):
    """idx 所在句的起点（含句首标点边界，与 sentence_at 同一口径）。"""
    punct = set("。！？\n")
    start = idx
    while start > 0 and text[start - 1] not in punct:
        start -= 1
    return start


def _sent_end(text, idx):
    """idx 所在句的终点（不含句末标点）。"""
    punct = set("。！？\n")
    end = idx
    while end < len(text) and text[end] not in punct:
        end += 1
    return end


def sentence_no(text, idx):
    """第几句（1 起算）。实现已上移到 review_agent.text。"""
    return text_sentence_no(text, idx)


def doc_anchor(text):
    """缺失要件类 issue 没有可引用的病句，用文档标题作为逐字锚点。"""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:60]
    return ""


def clause_anchor(text, where="tail"):
    """缺失条款类 issue 的锚点：指到「该条款应该出现的位置」。

    旧实现一律用首行（"甲方：北京示例科技有限公司"）当锚点，于是"缺争议解决条款"
    这条的证据指向的是"有甲方"，逐字可回查在**缺失型**问题上等于失效（红队 C1）。
    主体缺失（COMP-001）锚开头是对的；生效/争议这类尾部条款必须锚文末——
    读者一眼就能对上"本该在这一条后面再补一条"。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    return (lines[0] if where == "head" else lines[-1])[:80]


def note_fact(s, assertion):
    """把事实库里已核对过的口径记进 key_facts，避免写死成某一对象的结论。"""
    note = (assertion.get("note") or "").strip()
    if note and note not in s.verified_facts:
        s.verified_facts.append(note)


def iter_sentences(text):
    """逐句产出 (句子, 起始下标)，用于逐字 quote。"""
    for m in re.finditer(r"[^。！？\n]+[。！？]?", text):
        sentence = m.group(0).strip()
        if sentence:
            yield sentence, m.start()


def has_affirmative_term(text, terms):
    """条款关键词存在性 + 否定语境过滤（如「无争议」「未生效」不算命中）。"""
    pattern = re.compile("|".join(re.escape(t) for t in terms))
    for m in pattern.finditer(text):
        prefix = text[max(0, m.start() - 8):m.start()]
        if any(neg in prefix for neg in NEGATIVE_PREFIXES):
            continue
        return True
    return False


# ----------------------------------------------------------------------------
# 会话：承载日志/证据/步骤
# ----------------------------------------------------------------------------
class Session:
    def __init__(self, review_id, target_rel, target_type, progress_path=None, log_dir=None):
        self.review_id = review_id
        self.target_rel = target_rel
        self.target_type = target_type
        # 类型判定的置信度：猜测不可靠时，只允许它「增加」检查，不允许「关闭」检查
        self.type_confidence = 1.0
        self.progress_path = progress_path
        self.log_dir = log_dir
        self.issues = []
        self.disputes = []
        self.decisions = []
        self.tool_logs = []
        self.steps = []
        self.verified_facts = []
        # 同一片段被「线索级」规则和「结论级」规则同时命中时，折叠掉较弱的那条
        # （数据不丢，进报告 suppressed 并写明归口到哪条规则）
        self.folded = []
        # 文本里出现、但事实库没有断言的高频实体：写进 limitations，明说"没判"，
        # 而不是让模型拿参数记忆去判真伪
        self.unverified_hits = []
        # 法条库：本次引用到的条文（进报告顶层 law_library），以及
        # 「本该跑但库里缺条文」的检查项（进 limitations——没查 ≠ 没问题）
        self.law_cites = []
        self.law_skipped = []
        self.llm_usage = {"input": 0, "output": 0, "calls": 0, "failures": 0}
        self.issue_counter = 0
        self.tool_ok = 0
        self.tool_skipped = 0
        # 规则层按「同句多信号」作出的语境豁免：(rule_id, 原句, 理由)。
        # 模型判同一句、同一规则时要让位——否则 regex 已经判「简历主动留电话
        # 不算泄露」，模型照样按字面报一条 blocker，豁免等于没做。
        self.context_exempt = []
        if log_dir:
            # 先清空，后续决策 / 工具日志逐条 append，进程崩溃也不丢已发生部分
            (log_dir / "decisions.jsonl").write_text("", encoding="utf-8")
            (log_dir / "tools.jsonl").write_text("", encoding="utf-8")

    def exempt(self, rule_id, quote, reason):
        """登记一条语境豁免：同一规则、同一句的模型命中要跟着让位。"""
        key = (str(rule_id), str(quote or ""))
        if not any((r, q) == key for r, q, _ in self.context_exempt):
            self.context_exempt.append((key[0], key[1], str(reason or "")))

    def exempt_reason(self, rule_id, quote):
        for rid, sent, reason in self.context_exempt:
            if rid == rule_id and _quote_overlap(sent, quote or "") >= 0.8:
                return reason
        return ""

    def add_issue(self, issue, merge_overlap=0.9):
        """加入异议，同一规则 + 同一句只保留一条（依据合并）。

        事实库对同一句可能命中两条不同断言（如"豆包 MAU"与"豆包推出时间"），
        直接 append 会让同一句话在清单里出现两次，读起来像重复报。
        """
        for old in self.issues:
            if (old.get("rule_id") == issue.get("rule_id")
                    and _quote_overlap(old.get("quote", ""), issue.get("quote", ""))
                    >= merge_overlap):
                extra = issue.get("evidence") or ""
                if extra and extra not in (old.get("evidence") or ""):
                    old["evidence"] = ((old.get("evidence") or "") + "；另：" + extra)[:900]
                return old
        self.issues.append(issue)
        return issue

    def _append_log(self, name, item):
        if not self.log_dir:
            return
        try:
            with open(self.log_dir / name, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
                f.flush()
        except OSError:
            pass

    def step(self, label, name, status="success", latency_ms=0, abort_reason=None,
             detail=None):
        item = {
            "step": label, "name": name, "status": status,
            "ts": now_iso(), "latency_ms": round(latency_ms),
        }
        # 步骤明细：没有它，任务日志就只剩"S1 接收解析 0ms"这种空壳，
        # 用户看到一屏空行，等于没有日志。
        if detail:
            item["detail"] = detail
        if abort_reason:
            item["abort_reason"] = abort_reason
        self.steps.append(item)
        if self.progress_path:
            try:
                with open(self.progress_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def make_issue(self, dim, rule_id, quote, location, evidence, tool, confidence,
                   suggestion, basis="rule", severity=None, open_discovery=False):
        self.issue_counter += 1
        rule = RULE_BY_ID.get(rule_id, {})
        rule_text = rule.get("desc", "")
        if open_discovery and not rule_text:
            rule_text = ("规则清单外的独立发现（未经规则库定义；"
                         "严重度取系统默认档，待人工复核）")
        sev = severity or rule.get("severity", "minor")
        # 严重度必须与置信度对齐：置信度 <0.7 的发现不该占 blocker/major 的位置。
        # 实测长文里 117 条 major 中有 54 条置信度只有 0.6（"百分比未标口径"这类启发式），
        # 它们把真错误埋了。降一级并在依据里写明，避免"看起来很严重其实不确定"。
        ev = evidence
        if (sev in ("blocker", "major")
                and isinstance(confidence, (int, float)) and confidence < 0.7):
            sev = "major" if sev == "blocker" else "minor"
            ev = f"（置信度 {confidence} < 0.7，严重度已下调至 {sev}）{evidence}"
        return {
            "issue_id": f"ISS-{self.issue_counter:04d}",
            "dimension": dim,
            "severity": sev,
            "quote": quote,
            "location": location,
            "rule_id": rule_id,
            "rule_text": rule_text,
            "evidence": ev,
            "tool": tool,
            "confidence": confidence,
            "confidence_basis": basis,
            "suggestion": suggestion,
            "open_discovery": bool(open_discovery),
        }

    def decision(self, decision, rule_id, rationale, alternatives, why_not, **extra):
        item = {
            "ts": now_iso(), "decision": decision, "rule_id": rule_id,
            "rationale": rationale, "alternatives": alternatives,
            "why_not_alternative": why_not,
        }
        # 新增字段只增不改：历史 decisions.jsonl 的键集合不能变。
        item.update(extra)
        self.decisions.append(item)
        self._append_log("decisions.jsonl", item)

    def tool(self, name, input_, output_summary, latency_ms, status="success", retry=0):
        item = {
            "ts": now_iso(), "tool": name, "input": input_,
            "output_summary": output_summary, "latency_ms": round(latency_ms),
            "status": status, "retry": retry,
        }
        self.tool_logs.append(item)
        self._append_log("tools.jsonl", item)
        if status == "success":
            self.tool_ok += 1
        elif status == "skipped":
            self.tool_skipped += 1


# ----------------------------------------------------------------------------
# 四个子审查器（Demo：单 Agent 内四个函数顺序跑）
# ----------------------------------------------------------------------------
def fact_heuristics(s: Session, text, active_ids):
    """FACT-002/003/004 的确定性启发式，补上纯规则模式下的事实维度空档。

    这些判定置信度刻意压在 0.6~0.65（进 to_confirm），只作为初筛线索。
    """
    counters = {"FACT-002": 0, "FACT-003": 0, "FACT-004": 0}

    def add(rule_id, sentence, pos, evidence, suggestion, confidence):
        counters[rule_id] += 1
        s.add_issue(s.make_issue(
            "fact", rule_id, sentence, f"第 {sentence_no(text, pos)} 句",
            evidence, "regex (heuristic)", confidence, suggestion,
            basis="heuristic"))
        if counters[rule_id] == 1:
            s.decision(f"事实启发式命中 {rule_id}", rule_id, evidence,
                       ["LLM 语义审", "不报"], "先给低置信线索，交人工确认")

    for sentence, pos in iter_sentences(text):
        if "FACT-002" in active_ids and counters["FACT-002"] < 3:
            if SOURCE_CLAIM_RE.search(sentence) and not SOURCE_MARK_RE.search(sentence):
                add("FACT-002", sentence, pos,
                    "正则启发式：出现引用类表述，但未给出 URL / 文献 / 来源标注",
                    "补充可核查的来源链接、文献名或统计口径。", 0.65)
        if "FACT-003" in active_ids and counters["FACT-003"] < 3:
            if CAUSAL_RE.search(sentence) and not CAUSAL_EVIDENCE_RE.search(sentence):
                # 红队 B1：同一现象不能"fact 侧 minor、logic 侧 major"。
                # 分档依据是结构：句子里有成效/归因词 → 真的在拿结果归因，
                # 按规则原级（major）报；只有连接词 → 线索级，进待确认。
                strong = bool(EFFECT_RE.search(sentence))
                add("FACT-003", sentence, pos,
                    ("正则启发式：因果连接词 + 成效/归因词同现，但同句未给出数据、"
                     "研究或来源前提" if strong else
                     "正则启发式：出现因果连接词，但同句未给出数据、研究或来源前提"),
                    "补充支撑该因果结论的前提与证据，或改为弱化表述。",
                    0.72 if strong else 0.6)
        if "FACT-004" in active_ids and counters["FACT-004"] < 3:
            percent_issue = PERCENT_RE.search(sentence) and not CALIBER_RE.search(sentence)
            times_issue = TIMES_RE.search(sentence) and not BASELINE_RE.search(sentence)
            # 工资比例是约定值，不是披露数据：豁免并留痕（模型同规则命中也跟着让位）
            if CONTRACT_RATIO_RE.search(sentence):
                s.exempt("FACT-004", sentence,
                         "工资比例是合同约定值，不是对外披露的统计数据")
            elif percent_issue or times_issue:
                detail = "百分比未标注同比/环比/占比等口径" if percent_issue else "倍数未说明比较基线"
                add("FACT-004", sentence, pos,
                    f"正则启发式：{detail}",
                    "补充统计口径（时间范围、同比/环比、比较基线）。", 0.6)
        if all(v >= 3 for v in counters.values()):
            break


def fact_review(s: Session, text, rules):
    """事实审：数字/日期/人名/机构 与事实库比对。"""
    active_ids = {r["rule_id"] for r in rules}
    facts = retrieve.load_facts()
    assertions = facts.get("assertions", [])
    for a in assertions:
        if a.get("rule_id") not in active_ids:
            continue
        entity, attr = a["entity"], a["attr"]
        idx = text.find(attr)
        if idx < 0:
            idx = text.find(entity)
        if idx < 0 or entity not in text:
            continue
        # 实体名碰撞：所谓「豆包大模型/豆包 API」与「豆包 App」是两个产品线，
        # 口径不同不能互相核验（第三轮 N7：1000 万 vs 1 亿被判 blocker）
        excl = a.get("exclude_ctx") or []
        if excl:
            near = text[max(0, idx - 6): idx + len(entity) + 14]
            hit_excl = next((w for w in excl if w in near), None)
            if hit_excl:
                s.decision(f"{entity}{attr} 命中但属另一口径（含「{hit_excl}」），未比对",
                           a["rule_id"],
                           f"实体附近出现排除词：{hit_excl}",
                           ["按同一实体核验"], "不同产品线的同名指标不能互相校验")
                continue
        window = text[idx: idx + 50]
        # 年度口径约束（2026-10-28，红队 st03）：同一指标不同年份量级可以差十倍
        # （华为手机出货量 2019 年 2.4 亿台 → 2024 年 4,600 万台）。断言声明了
        # year_ctx，就只核验**同一句里写着该年份**的那一处；句内没有该年份不判。
        # 宁漏不误：拿 2024 年的口径去判 2019 年的数字，比漏报更糟。
        if a.get("year_ctx") and str(a["year_ctx"]) not in sentence_at(text, idx):
            s.decision(
                f"{entity}{attr} 所在句未写明 {a['year_ctx']} 年，未比对",
                a["rule_id"], "断言限定年度口径（year_ctx），跨年比较无意义",
                ["按任意年份比对"], "不同年份的量级不可互换，硬比就是误报")
            continue
        if a["type"] == "number":
            tr = time.perf_counter()
            nums = regex_check.extract_numbers(window)
            s.tool("regex_check", f"{entity} 数字抽取", f"候选 {len(nums)} 个",
                   (time.perf_counter() - tr) * 1000, "success")
            if not nums:
                continue
            # 跳过"2024 年"这类年份：它不是被比对的指标值。
            # 旧实现取窗口第一个数字，把年份当基数，得出"偏差约 0 倍"的垃圾证据（实测 f01）
            # 2026-10-28 补：「9 月」「18 日」这类日期成分同样不是指标值——
            # 「豆包是字节跳动 2023 年 9 月推出…」里没有「月活」时，旧代码会把 9 当
            # 月活去跟 1 亿比，产出垃圾 issue（实测 F 案例）。
            values = [n for n in nums
                      if not (1900 <= n["num"] <= 2100
                              and window[n["end"]:n["end"] + 1] == "年")
                      and window[n["end"]:n["end"] + 1] not in ("月", "日")]
            if not values:
                continue
            v = values[0]
            note_fact(s, a)
            base = v["num"] * UNIT_FACTOR.get(v["unit"], 1)
            exp = a["expected"]
            exp_base = exp["num"] * UNIT_FACTOR.get(exp["unit"], 1)
            ratio = base / exp_base if exp_base else 0
            tol = a.get("tolerance", 2)
            if ratio > tol or ratio < 1 / tol:
                # 引用「数字所在句」，不是「指标第一次出现的位置」：
                # 合同里「第二条 试用期」是标题，数字在下一句；引标题读者找不到错在哪。
                abs_pos = idx + v["start"]
                quote = sentence_at(text, abs_pos)
                loc = f"第 {sentence_no(text, abs_pos)} 句"
                tr = time.perf_counter()
                hits = retrieve.grep_knowledge(f"{entity} {attr}")
                s.tool("retrieve", f"{entity} {attr}", f"命中 {len(hits)} 条知识库文档",
                       (time.perf_counter() - tr) * 1000, "success")
                ev = (f"知识库命中：{hits[0]['file']}:{hits[0]['line']} —— {hits[0]['text']}"
                      if hits else a.get("note", "事实库无对应条目"))
                # label 是给人看的量级写法：「0.5 亿」对读者没意义，
                # 「5,000 万台」才是同一件事的正常说法（数值仍然只从 facts 来）
                _label = a.get("label") or f"{exp['num']:g}{exp['unit']}"
                sugg = (f"与公开口径偏差约 {ratio:.0f} 倍，建议改为「{_label}量级」"
                        f"并标注统计口径与时间。")
                s.add_issue(s.make_issue(
                    "fact", a["rule_id"], quote, loc, ev,
                    "retrieve + regex", 0.85, sugg))
                s.decision(
                    f"{entity}{attr} {v['raw']} 定为 {RULE_BY_ID[a['rule_id']]['severity']}",
                    a["rule_id"],
                    f"与事实库偏差约 {ratio:.0f} 倍（{v['raw']} vs {exp['num']}{exp['unit']}）",
                    ["major", "minor"], "major 适用小偏差，此处差一个数量级属事实硬伤")
            else:
                s.decision(
                    f"{entity}{attr} {v['raw']} 与事实库一致，不报",
                    a["rule_id"],
                    f"{v['raw']} 落在 {exp['num']}{exp['unit']} ±{tol} 倍容差内",
                    ["blocker"], "事实一致无需报")
        elif a["type"] == "max":
            # 法定/规范「上限」是一侧判断（红队 st04：《劳动合同法》试用期法定上限
            # 6 个月，写 12 个月要报）。旧的 number 分支是对称容差，直接拿来做上限
            # 会反过来误报：「试用期 3 个月」远低于上限完全合法，却会被算成
            # 「偏差 0.5 倍」。所以上限必须单独成一种类型，只在超限时报。
            tr = time.perf_counter()
            nums = regex_check.extract_numbers(window)
            s.tool("regex_check", f"{entity}{attr} 数字抽取", f"候选 {len(nums)} 个",
                   (time.perf_counter() - tr) * 1000, "success")
            if not nums:
                continue
            hint = str(a.get("unit_hint") or "")
            values = [n for n in nums
                      if not (1900 <= n["num"] <= 2100
                              and window[n["end"]:n["end"] + 1] == "年")
                      # 单位必须紧跟在数字后（允许空格）：只认「12 个月」，
                      # 不把「试用期工资 80%」「试用期 3 人」当上限值
                      and (not hint or window[n["end"]:n["end"] + 6].lstrip().startswith(hint))]
            if not values:
                s.decision(f"{entity}{attr} 未找到带「{hint}」的数值，不判", a["rule_id"],
                           "上限类断言必须带上限单位才有可比性",
                           ["按任意数字比对"], "没有单位的数字可能指别的量，判了就是误报")
                continue
            v = values[0]
            note_fact(s, a)
            exp = a["expected"]
            exp_base = exp["num"] * UNIT_FACTOR.get(exp["unit"], 1)
            base = v["num"] * UNIT_FACTOR.get(v["unit"] or exp["unit"], 1)
            if base > exp_base * (1 + 1e-9):
                abs_pos = idx + v["start"]
                quote = sentence_at(text, abs_pos)
                loc = f"第 {sentence_no(text, abs_pos)} 句"
                s.add_issue(s.make_issue(
                    "fact", a["rule_id"], quote, loc,
                    a.get("note") or f"{entity}{attr} 超出上限",
                    "retrieve + regex", 0.85,
                    f"超出法定/规范上限：上限为 {exp['num']:g}"
                    f"{a.get('unit_hint') or exp['unit']}，文中为 {v['num']:g}"
                    f"{a.get('unit_hint') or exp['unit']}，请按上限改写。"))
                s.decision(
                    f"{entity}{attr} {v['raw']} 超过上限 {exp['num']}{exp['unit']}，报 issue",
                    a["rule_id"], f"超限（{v['raw']} > {exp['num']}{exp['unit']}）",
                    ["minor"], "上限属可核验硬事实，超限直接报")
            else:
                s.decision(f"{entity}{attr} {v['raw']} 未超上限，不报", a["rule_id"],
                           f"{v['raw']} ≤ 上限 {exp['num']}{exp['unit']}",
                           ["blocker"], "未超限即合规，低于是合法区间不构成问题")
        elif a["type"] == "date":
            # 旧实现：attr 前 40 字窗口取第一个日期。两个坑：
            #   ① 窗口跨句，把落款、前文另一个日期当成断言年份；
            #   ② 同段出现多部法律（网安法修正 2026 + 数据条例 2025）必然张冠李戴。
            # 新实现（2026-10-28）：逐个实体出现位置，只在**同一句**内找日期；
            # attr（如「施行」）也必须在句内——「网络安全法」和「施行」不同句
            # 就不算同一断言，宁可不判。锚点取 attr 的句内位置（无 attr 退回实体
            # 位置），取距锚点最近的日期作为被核验年份。
            tr = time.perf_counter()
            checked = 0
            for m_ent in re.finditer(re.escape(entity), text):
                sent_start = _sent_start(text, m_ent.start())
                sent_end = _sent_end(text, m_ent.end())
                sent = text[sent_start:sent_end]
                anchor_off = sent.find(attr) if attr and attr != entity else -1
                if attr and attr != entity and anchor_off < 0:
                    continue            # attr 不在同句 → 不是同一断言，跳过该出现处
                anchor = anchor_off if anchor_off >= 0 else m_ent.start() - sent_start
                dates = regex_check.extract_dates(sent)
                s.tool("regex_check", f"{entity} 日期抽取", f"候选 {len(dates)} 个",
                       (time.perf_counter() - tr) * 1000, "success")
                if not dates:
                    continue
                nearest = min(dates, key=lambda d: min(
                    abs(d["start"] - anchor), abs(d["end"] - anchor)))
                checked += 1
                yr = nearest["year"]
                if yr != a["expected_year"]:
                    note_fact(s, a)
                    quote = sentence_at(text, sent_start + nearest["start"])
                    loc = f"第 {sentence_no(text, m_ent.start())} 句"
                    ev = a.get("note", "事实库无对应条目")
                    s.add_issue(s.make_issue(
                        "fact", a["rule_id"], quote, loc, ev,
                        "retrieve + regex", 0.85,
                        f"年份不符，建议核对为 {a['expected_year']} 年。"))
                    s.decision(f"{entity}{attr} 年份 {yr} 与事实库 {a['expected_year']} 不符，报 issue",
                               a["rule_id"], f"年份偏差（{yr} vs {a['expected_year']}）",
                               ["minor"], "年份属可核验硬事实，直接报")
                else:
                    s.decision(f"{entity}{attr} 年份 {yr} 与事实库一致，不报", a["rule_id"],
                               "年份匹配", ["blocker"], "事实一致无需报")
            if not checked:
                s.decision(f"{entity}{attr} 未找到实体与日期同句的出现处，不判",
                           a["rule_id"], "同句内无日期（宁漏不误）",
                           ["minor"], "跨句猜年份是上一版的误报源")
    # 事实库里「被点名但还没有断言」的高频实体：显式记录下来，交给 limitations。
    # 这一步的意义不是多报一条问题，而是让读者知道"这些实体本次没判真伪"——
    # 否则「没报事实错误」会被读成「事实都对」。
    watch = facts.get("watchlist") or []
    seen_watch = []
    for w in watch:
        name = str(w.get("entity") or "")
        if name and name in text and name not in seen_watch:
            seen_watch.append(name)
    if seen_watch:
        s.unverified_hits = seen_watch
    fact_heuristics(s, text, active_ids)
    return s


def safety_review(s: Session, text, rules):
    """安全审：攻击指令/违法操作/隐私/注入。"""
    # 防御性语境：安全运维、攻防演练、安审文档里「入侵检测」「密码破解防护」是正常术语，
    # 关键词命中不能直接判 blocker（实测 b02 两份合法文档被判 fail）。
    # 安全运营/攻防统计的高频词：拦截、阻断、告警、研判、处置、溯源…
    # 只覆盖「检测/防护」会被「拦截网络入侵 12 起」绕过（第三轮 N5）
    defensive = re.compile(
        r"(检测|监测|监控|防护|防御|预防|演练|加固|审计|排查|应急|治理|对抗|"
        r"拦截|阻断|告警|研判|处置|响应|溯源|态势|巡检|值守|封禁|清洗|"
        r"方案|白皮书|合规|渗透测试|安全建设|防护体系|防泄露|止损|应急预案|"
        r"安全运营|统计|报表|起$|次$)")
    # 主动提供联系方式的语境：简历 / 名片 / 邮件签名本来就要写电话邮箱。
    # SAFE-003 的 desc 限定为「**未公开**个人数据」，主动写「电话：…」
    # 不属于泄露。不加这条豁免，一份正常简历会被判 blocker、verdict 直接 fail
    # （实测：首条 = 「电话：15182683954 | 邮箱：…」，结论成了"不录用"）。
    # 只豁免**手机号**：身份证 / 银行卡 / 住址不因这句话免检。
    # 注意 `手机(?:号码)?` 不能写成 `手机号码?`——后者要求「号」必出现，
    # 于是「手机：138…」整句不匹配（同一个坑在年限正则上踩过：`以上?`）。
    contact_ctx = re.compile(
        r"(?:联系方式|联系电话|联系邮箱|手机(?:号码)?|电话|邮箱|E-?mail|微信|QQ)\s*[:：]",
        re.I)
    patterns = {
        "SAFE-001": (r"(如何制作|制毒|炸药|入侵|攻击指令|毒品配方)", "命中攻击/危险操作关键词"),
        # 「密码破解」是「破解密码」的倒序，两种语序都要认。
        # 刷单/跑分/代开发票/伪造公章属"违法但不像攻击"的一类，
        # 早期词表只覆盖"绕过安全/破解密码"，整类漏检（第六轮 B7）。
        "SAFE-002": (r"(违法操作|绕过.{0,4}安全|破解密码|密码破解|非法获取|"
                     r"刷单|跑分|代开(?:发票|增值税)|虚开(?:发票|增值税专用)|"
                     r"伪造(?:公章|发票|签名|证件|印章)|洗钱|套现|阴阳合同|"
                     r"售卖(?:个人信息|用户数据)|买卖(?:个人信息|简历))",
                     "命中违法操作关键词"),
        "SAFE-003": (r"(1[3-9]\d{9}|[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])",
                     "命中疑似个人身份/手机号信息"),
        # 注入句式早期只认「忽略以上所有指令」这类显式命令，
        # 「系统设定已被覆盖 / 不必遵守内容政策」是同一意图的改写，必须按
        # **语义结构**（主张已获授权 + 免除约束）识别，而不是再加一条词（第六轮 B8）。
        "SAFE-004": (r"(忽略(以上|所有|之前)|无视.{0,4}(限制|规则|内容政策)|扮演无限制|DAN\s*模式|"
                     r"绕过.{0,6}(审查|规则|检查)|直接判定为通过|判定为通过|"
                     r"把.{0,8}(合计|金额|数字)改为|隐藏.{0,4}(问题|矛盾|错误)|"
                     r"(?:系统|初始|安全|先前)?(?:设定|指令|规则|限制|约束|提示词).{0,8}"
                     r"(?:已被|被|已经).{0,6}(?:覆盖|替换|清除|解除|重置|作废)|"
                     r"(?:不需要|无需|不必|不再|不用).{0,8}遵守.{0,8}(?:政策|规则|限制|约束|指引)|"
                     r"不受任何?(?:约束|限制)|(?:内容政策|安全策略).{0,6}(?:不适用|失效|已停用))",
                     "命中 prompt 注入关键词"),
    }
    # 隐私类单独走结构判定：手机号/身份证用正则，银行卡用 Luhn 校验，
    # 住址用「市区县+路街+门牌 且 带单元/室」双条件。
    privacy_hits = []
    if "SAFE-003" in {r["rule_id"] for r in rules}:
        p = patterns["SAFE-003"][0]
        for m in re.finditer(p, text):
            raw = m.group(0)
            # 区分手机号与身份证：豁免只给手机号（简历主动留的联系方式），
            # 身份证号不会因为和「电话：」同句就变成正常披露。
            kind = "身份证号" if len(raw) >= 15 else "手机号"
            privacy_hits.append((m.start(), raw, kind))
        for c in regex_check.find_bank_cards(text):
            privacy_hits.append((c["start"], c["raw"],
                                 f"疑似银行卡号 {c['digits'][:4]}****{c['digits'][-4:]}，"
                                 + ("Luhn 校验通过" if c.get("luhn")
                                    else "未通过 Luhn 校验（可能为伪造/占位号，需人工确认）")))
        for a in regex_check.find_residence_addrs(text):
            privacy_hits.append((a["start"], a["raw"], "住宅地址（含单元/室门牌）"))
    for rid, (pat, desc) in patterns.items():
        if rid not in {r["rule_id"] for r in rules}:
            continue
        matches = ([h for h in privacy_hits] if rid == "SAFE-003"
                   else [(m.start(), m.group(0), "") for m in re.finditer(pat, text)])
        if not matches:
            continue
        # 逐句去重：同一句命中多个手机号/卡号只报一条，否则一句话刷 5 条
        seen_sent, reported = set(), 0
        for pos, raw, extra in matches:
            sent = sentence_at(text, pos)
            # 语境豁免必须先判、且**不占用去重 key**：否则
            # 「联系方式：电话：151…，身份证 110…」里手机号被豁免，
            # 同句的身份证号会被「这句已经处理过」连坐掉——那不是少报一条，
            # 是把真正的泄露放过去了。
            if rid in ("SAFE-001", "SAFE-002") and defensive.search(sent):
                s.decision(f"{rid} 命中但属防御性语境，未报", rid,
                           f"命中「{raw}」，同句含检测/防护类表述",
                           ["直接判 blocker"], "安全运维文档里「入侵检测」「破解防护」是正常术语")
                continue
            if rid == "SAFE-003" and extra == "手机号" and contact_ctx.search(sent):
                # 简历/名片/签名档主动写「电话：…」是正常披露，不是泄露未公开数据。
                # 不豁免会让一份正常简历被 blocker 打成 fail——这正是"不相干的规则"
                # 里代价最大的一种：它会改结论，而不只是多个噪声条目。
                s.exempt(rid, sent, "同句含「联系方式/电话：/邮箱：」标签，属主动提供")
                s.decision(f"{rid} 命中但属主动提供联系方式，未报", rid,
                           f"命中「{raw}」，同句含「联系方式/电话：/邮箱：」标签",
                           ["判为隐私泄露 blocker"], "SAFE-003 限定「未公开」个人数据")
                continue
            key = _norm_quote(sent)[:40]
            if key in seen_sent:
                continue
            seen_sent.add(key)
            ev = f"{desc}：命中「{raw}」" + (f"（{extra}）" if extra else "")
            s.add_issue(s.make_issue(
                "safety", rid, sent, f"第 {sentence_no(text, pos)} 句",
                ev, "regex + llm_judge(stub)", 0.8,
                "删除相关危险/敏感内容，必要时应转人工安全复核。"))
            s.decision(f"安全命中 {rid} 定为 {RULE_BY_ID[rid]['severity']}", rid,
                       desc, ["minor"], "安全红线属高严重度")
            reported += 1
            if reported >= 5:      # 单规则上限，避免一份文件刷出几十条同类
                break
    return s


def style_review(s: Session, text, rules):
    """风格审：emoji/口语/情绪化。quote 一律逐字摘句，不改写。"""
    ridset = {r["rule_id"] for r in rules}
    if "STYLE-001" in ridset:
        m = regex_check.EMOJI_RE.search(text)
        if m:
            s.add_issue(s.make_issue(
                "style", "STYLE-001", sentence_at(text, m.start()),
                f"第 {sentence_no(text, m.start())} 句",
                f"regex 命中 emoji「{m.group(0)}」", "regex", 0.95,
                "正式文书移除 emoji。"))
    if "STYLE-002" in ridset:
        cas = regex_check.find_casual(text)
        if cas:
            pos = text.find(cas[0])
            s.add_issue(s.make_issue(
                "style", "STYLE-002", sentence_at(text, pos),
                f"第 {sentence_no(text, pos)} 句",
                f"regex 命中口语/网络语气词：{'、'.join(cas)}", "regex + llm_judge(stub)", 0.85,
                "替换为中性书面表达。"))
    if "STYLE-003" in ridset:
        emo = regex_check.find_emotion(text)
        if emo:
            pos = text.find(emo[0])
            s.add_issue(s.make_issue(
                "style", "STYLE-003", sentence_at(text, pos),
                f"第 {sentence_no(text, pos)} 句",
                f"regex 命中第一人称情绪化表达：{'、'.join(emo)}", "regex + llm_judge(stub)", 0.85,
                "去除情绪化表述，保持客观。"))
    if "STYLE-005" in ridset:
        m = TYPO_WORD.search(text)
        if m:
            s.add_issue(s.make_issue(
                "style", "STYLE-005", sentence_at(text, m.start()),
                f"第 {sentence_no(text, m.start())} 句",
                f"用词规范：命中「{m.group(0)}」，表示「到某时为止」应用「截至」",
                "regex", 0.9,
                "改为「截至…」；异形词（帐户/帐目）统一为「账户/账目」。"))
    return s


# ---------------------------------------------------------------- COMP-004 判定面
#
# 核心区分：**「句子里有数字」≠「披露了一个数据」**。
# 银行卡号、门牌号、合同金额、年龄都含数字，但不存在"统计口径与来源"这回事，
# 拿数字当条件去报，只会把 6/6 的样本都刷成一条无关噪声（第六轮实证）。
# 只有指标类数字才谈得上口径：要么带 %/万/亿/倍 这类量纲，要么紧跟在
# 「覆盖率/营收/规模/数量」这类指标名词后面。
DISCLOSURE_UNIT = {"%", "％", "万", "亿", "倍"}
METRIC_HEAD = re.compile(
    r"(率|占比|比重|份额|规模|总量|数量|用户数|客户数|活跃度|营收|营业收入|收入|"
    r"利润|毛利|净利|成本|费用|覆盖|渗透|转化|留存|复购|订单量|销量|产量|产能|"
    r"客单价|人数|家数|台数|件数|亩产|面积|时长|频次|指数|得分|评分|增长|下降|"
    r"同比|环比|缺口|预算|投入|融资额|估值)")
# 金额/账号/编码语境：数字在这里是"标识"或"钱"，不是统计指标
NON_METRIC_CTX = re.compile(
    r"([¥￥]\s*$|人民币|元整|[账帐]户|[账帐]号|卡号|流水号|发票|编号|订单号|"
    r"版本|电话|手机|邮编|身份证|传真|工号|合同号|单号)")
# 门牌/住址语境
ADDRESS_CTX = re.compile(r"(路|街|巷|弄|号|室|单元|栋|幢|户|座|层)\s*$")
# 「外部数据」语境：需要标注来源的是**引用来的**统计结论与行业数据，
# 不是本文件自己的经营数字（内部报表本身就是原始来源，要求它给自己标来源是噪声）。
EXTERNAL_SCOPE = re.compile(
    r"(行业|市场|全国|全球|全省|全市|全行业|第三方|公开数据|白皮书|研究报告|"
    r"调查|统计|问卷|抽样|样本|竞品|对手|排名|榜单|指数)")


def _is_disclosure_number(text, n):
    """判断某个数字是不是「披露指标」。不是就不该走 COMP-004。"""
    pos, end = n["start"], n["end"]
    digits = re.sub(r"\D", "", n["raw"])
    if len(digits) >= 15:                 # 卡号/身份证/长流水号，一眼不是指标
        return False
    before = text[max(0, pos - 14):pos]
    if NON_METRIC_CTX.search(before) or ADDRESS_CTX.search(before):
        return False
    head_hit = METRIC_HEAD.search(before)
    if n["unit"] in DISCLOSURE_UNIT:
        # 「3.5 万元人民币」里的"万"是金额单位，不是指标量纲
        if n["unit"] in ("万", "亿") and "元" in text[end:end + 3] and not head_hit:
            return False
        return True
    return bool(head_hit)


def _metric_context(text, n):
    before = text[max(0, n["start"] - 14):n["start"]]
    m = METRIC_HEAD.search(before)
    return f"{m.group(0)}{n['raw']}" if m else f"{n['raw']}{n['unit']}"


def compliance_review(s: Session, text, rules, target_type):
    """合规审：合同要件 checklist + 数据披露。"""
    ridset = {r["rule_id"] for r in rules}
    type_conf = getattr(s, "type_confidence", 1.0)
    # 只有"确实是合同"才跑要件检查：类型证据不足时宁可不跑，
    # 否则一次误判会产出 3 条「缺甲方/缺争议条款」的无意义异议。
    if target_type == "contract" and type_conf >= 0.8:
        checks = {
            "COMP-001": ((has_affirmative_term(text, ["甲方"])
                          and has_affirmative_term(text, ["乙方"])),
                         "未检索到明确的双方主体（甲方/乙方）", "head"),
            "COMP-002": ((has_affirmative_term(text, ["生效"])
                          and (has_affirmative_term(text, ["终止"])
                               or has_affirmative_term(text, ["有效期"]))),
                         "未检索到生效/终止条款", "tail"),
            "COMP-003": (has_affirmative_term(text, ["争议", "仲裁", "诉讼"]),
                         "未检索到争议解决条款（仲裁/诉讼）", "tail"),
        }
        s.decision("合同审采用关键词 + 否定语境预筛", "OVERALL-005",
                   "Demo 无条款结构解析器，仅做要件存在性初筛",
                   ["条款结构解析", "人工复核"], "先给可解释的初筛结论，并在报告中标注局限")
        ok_notes = {
            "COMP-001": "已核验：合同明确双方主体（甲方/乙方）",
            "COMP-002": "已核验：合同含生效与终止条款",
            "COMP-003": "已核验：合同含争议解决条款",
        }
        for rid, (ok, msg, where) in checks.items():
            if rid not in ridset:
                continue
            if not ok:
                anchor = clause_anchor(text, where)
                loc = ("合同开头（主体信息处）" if where == "head"
                       else "文末（最后一条之后，应在此处补该条款）")
                s.add_issue(s.make_issue(
                    "compliance", rid, anchor, loc,
                    f"clause_presence_check 未命中：{msg}。"
                    f"缺失定位：{'开头' if where == 'head' else '文末'}「{anchor[:24]}」"
                    f"{'之前' if where == 'head' else '之后'}未见该要件（全文 {len(text)} 字符）",
                    "regex + checklist", 0.9,
                    f"补充 {rid} 要求的条款，否则合同要件不完整。"))
                s.decision(f"合规缺失 {rid} 定为 {RULE_BY_ID[rid]['severity']}", rid,
                           msg, ["minor"], "合同要件缺失影响可执行性，属 major")
            elif ok_notes.get(rid) and ok_notes[rid] not in s.verified_facts:
                s.verified_facts.append(ok_notes[rid])
    elif target_type == "contract":
        # 疑似合同但证据不足：明说"没跑要件检查"，而不是悄悄跳过
        s.decision("类型证据不足，未执行合同要件检查", "OVERALL-005",
                   f"类型推断置信度 {type_conf:.2f} < 0.80",
                   ["强制执行要件检查"], "错误主体上跑要件检查只会制造误报")
    # COMP-004 数据披露（文本类对象）
    # 注意：类型**猜**成合同时也要跑——猜测只允许增加检查，不允许关闭检查。
    # 与类型**解耦**：只要不是代码，就保留数据披露检查。
    # 旧逻辑在类型被判 contract 时静默关闭它——一次猜测删掉了一项检查，
    # 与「类型猜测只允许增加检查」的承诺相违背（实测 d01）。
    if "COMP-004" in ridset and target_type != "code":
        nums = regex_check.extract_numbers(text)
        # 标题行、纯年份不算"裸数据"（实测 9/17 份报告把标题和日期当问题报）
        def _is_noise(n):
            pos, end = n["start"], n["end"]
            # 量词紧跟在数字后（允许空格）：个/次/天/小时…都不是"披露的数据"
            nxt = text[end:end + 3].lstrip()[:1]
            # 年份、月日、编号都不是"披露的数据"：实测 a02 把「7 月 1 日」当裸数据报
            if nxt in ("年", "月", "日", "号", "周", "届", "期", "名", "个", "次",
                       "天", "人", "件", "台", "家", "项",
                       "室", "栋", "幢", "单元", "层", "户", "座", "位"):
                return True
            if text[max(0, pos - 1):pos] in ("第", "周", "月"):
                return True
            if 1900 <= n["num"] <= 2100 and text[end:end + 1] == "年":
                return True
            line_start = text.rfind("\n", 0, pos) + 1
            line = text[line_start: text.find("\n", pos) if text.find("\n", pos) > 0 else len(text)]
            if line.strip().startswith(("#", "【", "标题")):
                return True
            return len(line.strip()) <= 12 and "。" not in text[pos:pos + 40]
        nums = [n for n in nums if not _is_noise(n)]
        has_source = bool(re.search(
            r"(据|来源|公开数据|报道|https?://|\[\d+\]|口径见|详见|参见|见.{0,4}(附件|附录|文档|报表))",
            text))
        # ---- 判定面收紧（第六轮 P0）----
        # 旧逻辑「句子里有数字 + 全文没写来源」就报，于是银行卡号、住址门牌、
        # 合同金额、年龄全被当成「裸数据无口径」——第四轮 13/27、第六轮 6/6 样本
        # 都是这条规则贡献的噪声。**"有数字" 不等于 "披露了数据"**：
        # 只有指标类数字（占比/规模/收入/覆盖…）才有"口径与来源"这回事。
        metric_nums = [n for n in nums if _is_disclosure_number(text, n)]
        # 再收一道：只有「引用来的外部数据」才要求标来源。内部经营数字（本季度营收、
        # 行政占员工总数 10%）自己就是原始来源，要求它给自己标口径只会制造噪声。
        metric_nums = [n for n in metric_nums
                       if EXTERNAL_SCOPE.search(sentence_at(text, n["start"]))]
        first_sentence = sentence_at(text, metric_nums[0]["start"]) if metric_nums else ""
        if metric_nums and not has_source and not any(
                i["rule_id"] != "COMP-004"
                and _quote_overlap(i["quote"], first_sentence) >= 0.6
                for i in s.issues):
            s.add_issue(s.make_issue(
                "compliance", "COMP-004", first_sentence,
                f"第 {sentence_no(text, metric_nums[0]['start'])} 句",
                f"source_presence_check：披露指标「{_metric_context(text, metric_nums[0])}」"
                "但无标注口径/来源",
                "regex + checklist", 0.6,
                "为数字补充统计口径、时间与可核查来源。"))
            s.decision("COMP-004 数据披露缺失定为 minor(待确认)", "COMP-004",
                       "仅为软性启发式，置信度偏低", ["blocker"], "非硬伤，置信度 0.6 < 0.7 进 to_confirm")
    return s


def static_scan(s: Session, target_path: Path, text, rules, target_type, force=False):
    """S3 静态扫描：代码 lint + 硬编码密钥（任何文本都扫密钥）。

    force=True 时不受 STYLE-004 是否存在的影响——由 Agent 声明驱动：
    「这个 Agent 要跑静态扫描」是判定标准的一部分，不该被另一条风格规则
    的启停状态间接决定（安全审 Agent 根本没有 STYLE-004）。
    """
    if not force and "STYLE-004" not in {r["rule_id"] for r in rules}:
        return s
    tr = time.perf_counter()
    result = lint.run_lint(str(target_path))
    findings = result.get("findings") or []
    summary = (result.get("details") or "无输出").replace("\n", " ")[:180]
    if findings:
        summary = f"{summary}；结构化告警 {len(findings)} 条"
    status = "success"
    if result.get("status") == "skipped" and not findings:
        status = "skipped"
    s.tool("static_scan", target_path.name, summary,
           (time.perf_counter() - tr) * 1000, status)

    lines = text.splitlines()
    for f in findings[:10]:
        line_no = int(f.get("line") or 0)
        quote = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
        if not quote:
            continue
        if f.get("kind") == "secret":
            evidence = f"regex 命中硬编码密钥字段 {f.get('key')}（值已脱敏：{f.get('preview', '')}）"
            conf = 0.95
            sugg = "移除明文密钥，改用环境变量或密钥管理服务，并立即轮换该密钥。"
        else:
            code = f.get("code") or f.get("kind")
            evidence = f"{f.get('tool')} 报错 {code}: {f.get('message', '')}"[:240]
            conf = 0.85
            sugg = "修复 lint 告警后重新提交；若为误报，在配置中显式忽略并说明理由。"
        s.add_issue(s.make_issue(
            "style", "STYLE-004", quote, f"第 {line_no} 行",
            evidence, f.get("tool") or "regex", conf, sugg))
        s.decision(f"STYLE-004 命中第 {line_no} 行（{f.get('kind')}）",
                   "STYLE-004", evidence[:120],
                   ["minor"], "硬编码密钥/lint 告警属代码安全基线问题，定级 major")
    return s


# 同片段判定：实现只有一份（review_agent.runtime.nodes.text），这里保留旧名字，
# 避免二十多处调用点跟着改；两处各写一份就会出现「折叠判为同句、复核判为不同句」。
_norm_quote = normalize_quote
_quote_overlap = quote_overlap


CROSS_PAIRS = [
    ("fact", "safety", "事实审与安全审对同一片段结论冲突，需确认是否涉及未公开敏感信息"),
    ("compliance", "style", "合规审已就该片段提出要件/披露问题，风格审可能低估红线"),
]


def drop_covered_comp004(s: Session):
    """同一句若已被算术/事实规则报出，就不再叠一条"数字无来源"的提示。

    COMP-004 在 S2 靠前执行，内联判断看不到后面才产生的 LOGIC/FACT 异议，
    所以要在 S2 结束时统一收一遍（第三轮 N9）。
    """
    others = [i for i in s.issues if i["rule_id"] != "COMP-004"]
    kept, dropped = [], 0
    for i in s.issues:
        if i["rule_id"] == "COMP-004" and any(
                _quote_overlap(o["quote"], i["quote"]) >= 0.6 for o in others):
            dropped += 1
            continue
        kept.append(i)
    if dropped:
        s.issues[:] = kept
        s.decision(f"COMP-004 去重 {dropped} 条", "COMP-004",
                   "该句已由算术/事实规则报出，披露提示不再重复叠加",
                   ["逐条保留"], "同一句话重复两条不同规则的提示，只会稀释信噪比")
    return s


# 同片段「归口」表：键是线索级规则，值是它该让位的结论级规则。
# 红队 B1/B2 说的是同一件事——同一句话被两条规则命中、一条 minor 一条 major，
# 用户看到的是"口径不清"，真正的"量级矛盾"被埋了。
# 处理原则不是删问题，而是**定归口**：结论级规则在就折叠线索级，进 suppressed 留痕。
FOLD_INTO = {
    "FACT-003": ("LOGIC-003", "LOGIC-005", "LOGIC-002", "LOGIC-008"),
    # LAW-001 也在这条链上：合同里写「试用期工资 60%」时，LAW-001 已经用
    # 第二十条原文判了违法（major），FACT-004 再补一句「百分比未标口径」
    # 只是同一句上的噪声——条款比例本来就不需要标同比/环比。
    "FACT-004": ("LOGIC-001", "LOGIC-002", "LOGIC-009", "LAW-001"),
}

# 内部工作进度语境：这些数字是"自己干了多少"，本身就是原始记录，
# 要求它给自己标外部来源是范畴错误（红队 B3：「联调完成 8/10」被判无出处）。
INTERNAL_SCOPE = re.compile(
    r"(本周|上周|本月|上月|本季|内部|对内|本项目|项目组|团队|进度|里程碑|"
    r"联调|内测|排期|交付|剩余|待办|计划|完成率|收尾)")


def drop_internal_progress_comp004(s: Session):
    """内部进度类数字不按「对外披露」要求来源。

    COMP-004 的原文是"披露具体数据/统计结论时须标注口径与时间"——对内的工作
    进度（8/10 个接口联调完成）既不是披露，也没有外部来源可标。模型按字面套用
    会稳定误报一条 minor，把"周报本来就没问题"变成"周报有问题"。
    只在**没有外部数据语境**时豁免，内部周报里引用行业数据照样要求标来源。
    """
    kept, dropped = [], []
    for it in s.issues:
        if it["rule_id"] != "COMP-004":
            kept.append(it)
            continue
        if INTERNAL_SCOPE.search(it["quote"]) and not EXTERNAL_SCOPE.search(it["quote"]):
            it = dict(it)
            it["fold_reason"] = "内部工作进度不属于「对外披露数据」，不要求外部来源"
            dropped.append(it)
            continue
        kept.append(it)
    if dropped:
        s.issues[:] = kept
        s.folded.extend(dropped)
        s.decision(f"COMP-004 内部进度豁免 {len(dropped)} 条", "COMP-004",
                   "命中内部语境（本周/进度/完成…）且无外部数据语境",
                   ["按披露规范报"], "对内进度不是披露数据，要求外部来源属范畴错误")
    return s


def collapse_rule_overlap(s: Session):
    """同一片段上线索级 fact 规则让位给结论级 logic 规则（数据不丢，进 suppressed）。"""
    rank = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
    kept, folded = [], []
    for it in s.issues:
        rivals = FOLD_INTO.get(it["rule_id"]) or ()
        rival = next((o for o in s.issues
                      if o is not it and o["rule_id"] in rivals
                      and rank.get(o["severity"], 9) <= rank.get(it["severity"], 9)
                      and _quote_overlap(o["quote"], it["quote"]) >= 0.6), None)
        if rival is None:
            kept.append(it)
            continue
        it = dict(it)
        it["folded_into"] = rival["rule_id"]
        it["fold_reason"] = (
            f"同一片段已由 {rival['rule_id']}（{rival['severity']}）判定，"
            f"{it['rule_id']}（{it['severity']}）作为同源线索不重复列出")
        folded.append(it)
    if folded:
        s.issues[:] = kept
        s.folded.extend(folded)
        s.decision(
            f"同片段归口折叠 {len(folded)} 条", folded[0].get("folded_into", "OVERALL"),
            "；".join(f"{f['rule_id']}→{f['folded_into']}" for f in folded)[:200],
            ["逐条并列展示"], "同一现象两条规则一轻一重，用户只会看到轻的那条；"
                              "折叠后仍可在报告 suppressed 里查到，不丢证据")
    return s


def merge_law_into_fact(s: Session):
    """同一句已由 FACT-001 判为事实错误时，LAW-001 只并法条依据，不再重复列一条。

    例：试用期 12 个月，事实库（第十九条上限）和法条库（第十九条原文）会各报一条。
    读者看到的是「同一句话两条 major」，一条改了另一条还在。保留 FACT-001（通常更重），
    把 LAW-001 的 citation 并进证据，LAW-001 进 suppressed。
    """
    def same_topic(a, b):
        qa, qb = str(a.get("quote") or ""), str(b.get("quote") or "")
        # 「试用期时长」和「试用期工资」是两件事，不能因为都有「试用期」就合并
        if ("工资" in qa) != ("工资" in qb):
            return False
        if ("竞业" in qa) != ("竞业" in qb):
            return False
        for kw in ("试用期", "竞业", "工资", "年休假", "加班", "经济补偿", "服务期"):
            if kw in qa and kw in qb:
                return True
        return _quote_overlap(qa, qb) >= 0.6

    facts = [i for i in s.issues if i.get("rule_id") == "FACT-001"]
    kept = []
    for it in s.issues:
        if it.get("rule_id") == "LAW-001":
            rival = next((f for f in facts if f is not it and same_topic(f, it)), None)
            if rival is not None:
                cites = it.get("citations") or ([it["citation"]] if it.get("citation") else [])
                if cites:
                    rival.setdefault("citations", [])
                    rival["citations"].extend(c for c in cites if c not in rival["citations"])
                if it.get("evidence"):
                    rival["evidence"] = ((rival.get("evidence") or "") +
                                         "；法条依据：" + str(it["evidence"])[:300])
                s.folded.append(dict(
                    it, folded_into=rival["issue_id"],
                    fold_reason="同句已由 FACT-001 判定；LAW-001 法条依据并入，不重复列出"))
                continue
        kept.append(it)
    s.issues[:] = kept
    return s


def arith_review(s: Session, text, rules):
    """确定性算术/日历校验：分项加总、日期-星期、历时月数、占比。

    为什么单独成层：压力测试里稳定漏报的几条，有一半是「算一下就知道」的题。
    模型会把 360+210+480 算成 1080，也会说 2026-10-18 是周六——这不是能力波动，
    是把确定性任务交给了概率模型。搬到代码里，结果才是 100% 可复现的。

    保守优先：只在关系明确时开火（带「三项/四项」计数词的合计、带年份的日期-星期、
    明确的日期区间+历时、唯一总人数下的占比），见 tools/arith_check.py 的设计说明。
    """
    ridset = {r["rule_id"] for r in rules}
    findings = arith_check.run_all(text, ridset)
    added = 0
    for f in findings:
        quote = f["quote"]
        # 同一规则 + 同一片段已被报过就不重复（与 LLM 层去重共用同一判定）
        if any(i["rule_id"] == f["rule_id"] and _quote_overlap(i["quote"], quote) >= 0.8
               for i in s.issues):
            continue
        pos = int(f.get("start") or 0)
        s.add_issue(s.make_issue(
            f.get("dimension") or "logic", f["rule_id"], quote,
            f"第 {sentence_no(text, pos)} 句", f["evidence"], f["tool"],
            f["confidence"], f["suggestion"]))
        added += 1
    if added:
        s.decision(
            f"确定性算术/日历校验命中 {added} 条", "LOGIC-001",
            "加总 / 星期 / 历时 / 占比由代码按日历与算术计算，不依赖模型推断",
            ["全部交给 LLM 判断"],
            "算术题交给概率模型会随机算错，代码结果可复现、可测试")
    return s


def cross_review(s: Session):
    """S5 交叉复核（两轮）：四维互查同一片段上的定级冲突 → disputes。

    规则：对框架指定的两对视角，若两条 issue 的 quote 指向同一片段，
    且至少一方为 blocker/major，则判为跨维度冲突：
      - 记入 disputes，交人工裁定；
      - 双方 confidence 各下调 0.05（下限 0.5），并记决策日志。
    """
    found = 0
    for d1, d2, point in CROSS_PAIRS:
        left = [i for i in s.issues if i["dimension"] == d1]
        right = [i for i in s.issues if i["dimension"] == d2]
        for a in left:
            for b in right:
                if _quote_overlap(a["quote"], b["quote"]) < 0.6:
                    continue
                if SEV_ORDER.get(a["severity"], 9) > 1 and SEV_ORDER.get(b["severity"], 9) > 1:
                    continue  # 两边都是 minor/nit，不构成红线冲突
                s.disputes.append({
                    "between": [f"{d1}-reviewer", f"{d2}-reviewer"],
                    "point": (f"{a['issue_id']}({a['rule_id']}/{a['severity']}) 与 "
                              f"{b['issue_id']}({b['rule_id']}/{b['severity']}) 指向同一片段："
                              f"{a['quote'][:40]}… — {point}"),
                    "resolution_hint": "交人工裁定：确认严重度是否被高估/低估，以及是否漏判",
                    "issue_ids": [a["issue_id"], b["issue_id"]],
                })
                for i in (a, b):
                    i["confidence"] = round(max(0.5, i["confidence"] - 0.05), 2)
                found += 1
    if found:
        s.decision(f"交叉复核发现 {found} 处跨维度冲突", "OVERALL-002",
                   "同一原文片段被两个维度分别标记且定级不一致",
                   ["维持原判", "直接取最高严重度"], "冲突需人工裁定，同时下调相关 issue 置信度")
    else:
        s.decision("交叉复核：四维结论无冲突", "OVERALL-002",
                   "事实/安全/合规/风格 无互斥结论", ["有冲突"], "样本无重叠冲突点")
    return s


# ----------------------------------------------------------------------------
# S3 语义审（大模型 API）
# ----------------------------------------------------------------------------
def aggregate_repeats(issues):
    """把同一「提示类」规则的重复条目聚合成一条。

    长文里 COMP-004/FACT-004 这类"数字未标口径"会按段落刷几十条，
    内容几乎相同、逐条读没有增量信息，却把真问题挤到后面。
    聚合后保留一处作为示例并注明总处数，其余进 suppressed（数据不丢）。
    """
    groups = {}
    for i in issues:
        if i["rule_id"] in ADVISORY_RULES:
            groups.setdefault(i["rule_id"], []).append(i)
    folded = set()
    for rid, items in groups.items():
        if len(items) < AGGREGATE_MIN:
            continue
        items.sort(key=lambda x: -x["confidence"])
        head = items[0]
        head["aggregated_count"] = len(items)
        head["aggregated_ids"] = [x["issue_id"] for x in items[1:]]
        head["evidence"] = (
            f"全文共 {len(items)} 处同类问题（{rid}），以下为其中一处示例；"
            f"其余位置见 aggregated_ids" + "｜" + (head.get("evidence") or "")
        )[:900]
        folded.update(x["issue_id"] for x in items[1:])
    return [i for i in issues if i["issue_id"] not in folded], folded


def plan_chunk_spans(text, limit, overlap=400):
    """把长文按段落边界切成 ≤limit 的块，返回 [(start, end), ...] 区间。

    为什么按段落切：句子被切断会让模型读到残缺语义，反而制造误报。
    重叠是为了让跨块的相邻句至少在某一块里是完整的。

    覆盖率必须和分块用同一套区间：事后再用 text.find(chunk) 反查并不可靠——
    重复段落会把后面的块钉到前面出现的位置，重叠块的并集也会算错。
    """
    if len(text) <= limit:
        return [(0, len(text))]
    spans, pos, n = [], 0, len(text)
    while pos < n:
        end = min(pos + limit, n)
        if end < n:
            window = text[pos:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"))
            if cut < limit * 0.5:
                cut = window.rfind("。")
            if cut < limit * 0.5:
                cut = limit - 1
            end = pos + cut + 1
        spans.append((pos, end))
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return spans


def plan_chunks(text, limit, overlap=400):
    """plan_chunk_spans 的文本版：输出必须与切分区间逐字节一致（缓存键依赖）。"""
    return [text[start:end] for start, end in plan_chunk_spans(text, limit, overlap)]


def covered_chars(spans):
    """求若干 [start, end) 区间的并集长度（块间重叠只算一次）。

    实现只有一份，在语义审节点里（`review_agent.runtime.nodes.semantic`）：
    否则「节点算的覆盖」与「报告算的覆盖」迟早各说各话。
    """
    return semantic_covered_chars(spans)


def _span_label(start, end):
    """把字符区间渲染成「6000–11800」；拿不到合法整数就说未知。"""
    if isinstance(start, int) and isinstance(end, int):
        return f"{start}–{end}"
    return "未知区间"


def failed_chunk_limitation(failed_chunks, chunks):
    """把块级失败写成 limitations 的一条说明（前 3 条展开，其余给数量）。

    只写"部分块失败"没有信息量：用户要知道**哪一段**没进模型。
    """
    items = list(failed_chunks or [])
    if not items:
        return ""
    parts = []
    for f in items[:3]:
        parts.append(
            f"第 {f.get('chunk')}/{chunks} 块"
            f"（字符 {_span_label(f.get('start'), f.get('end'))}，维度 {f.get('dim')}）"
            "语义审失败，该段仅经过规则层，未进入模型复核")
    note = "；".join(parts)
    if len(items) > 3:
        note += (f"；另有 {len(items) - 3} 处块级失败未逐条展开"
                 "（见 coverage.failed_chunks）")
    return note + "——本次结论不代表上述字符段已审。"


# ---------------------------------------------------------------------------
# Agent 调度层：把「维度 → 处理器」变成可声明、可替换的映射
# ---------------------------------------------------------------------------
# 内置处理器与已有维度实现一一对应。Agent 用 `builtin:xxx` 引用；
# 新场景用 `pack:xxx` 指向 agents/<id>/checks.py 里的 CHECKERS。
# 处理器签名统一为 (session, text, rules, ctx) —— 统一了才能被 manifest 调度，
# 否则每加一个场景都要在主流程里写一段 if。
def _h_fact(s, text, rules, ctx):
    return fact_review(s, text, rules)


def _h_safety(s, text, rules, ctx):
    return safety_review(s, text, rules)


def _h_style(s, text, rules, ctx):
    return style_review(s, text, rules)


def _h_compliance(s, text, rules, ctx):
    return compliance_review(s, text, rules, ctx["target_type"])


def _h_static_scan(s, text, rules, ctx):
    return static_scan(s, ctx["target_path"], text, rules, ctx["target_type"])


def _h_static_scan_force(s, text, rules, ctx):
    return static_scan(s, ctx["target_path"], text, rules, ctx["target_type"], force=True)


def _h_arith(s, text, rules, ctx):
    return arith_review(s, text, rules)


def _h_logic(s, text, rules, ctx):
    """逻辑维度：跑窄口径确定性检查（年龄工龄/样本代表性/结论冲突/绝对化）。

    LOGIC-003（因果归因）刻意不在这里：它需要语义判断，硬做正则只会误报，
    所以继续留在 prompt，并在报告里标注为「仅提示」。
    """
    ridset = {r["rule_id"] for r in rules}
    added = 0
    for f in logic_checks.run_all(text, ridset):
        if any(i["rule_id"] == f["rule_id"] and _quote_overlap(i["quote"], f["quote"]) >= 0.8
               for i in s.issues):
            continue
        pos = int(f.get("start") or 0)
        s.add_issue(s.make_issue(
            f.get("dimension") or "logic", f["rule_id"], f["quote"],
            f"第 {sentence_no(text, pos)} 句", f["evidence"], f["tool"],
            f["confidence"], f["suggestion"]))
        added += 1
    if added:
        s.decision(f"逻辑确定性检查命中 {added} 条", "OVERALL-001",
                   "年龄工龄矛盾 / 样本代表性 / 结论与正文冲突 / 绝对化表述由代码判定",
                   ["全部交给 LLM 判断"],
                   "这几类有明确的结构特征，代码判定可复现且不受模型波动影响")
    return s


def _h_pattern(s, text, rules, ctx):
    """执行所有带 `pattern` 的声明式规则（YAML 里写判定条件，这里只做匹配）。

    没有这个执行器，规则里的 pattern 字段就是死数据——安全审 Agent 有自己的
    执行器，文档审 Agent 没有，于是「自动挖掘出来的规则」在文档审里完全不生效。
    实测：候选规则门禁 0 命中，查了半天才发现是引擎缺这个能力。
    """
    return pattern_rules.scan(
        s, text, rules, dimension=None, tool="pattern_rule",
        target_suffix=str(ctx.get("target_path") or ""))


def _h_comp004(s, text, rules, ctx):
    return drop_covered_comp004(s)


def _dedupe_cites(cites):
    """同一条文被多句引用时只留一份：报告的「依据清单」是按条看的，不是按句。"""
    out, seen = [], set()
    for c in cites or []:
        key = (c.get("law"), c.get("article"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _h_law(s, text, rules, ctx):
    """法定量断言 vs 真实条文（LAW-001）。

    与 FACT-001 的分工：FACT-001 拿 `facts.json` 的数值断言判真伪，它手里
    没有原文，所以只能说"和口径不符"；这里的依据是**法条原文 + 条号**，
    读者能自己翻回去核对。两者对同一句开口时 LAW-001 更硬（有原文）。
    """
    ridset = {r["rule_id"] for r in rules}
    if "LAW-001" not in ridset:
        return s
    try:
        found, verified, skipped = law_cite.check(text)
    except Exception as e:                                  # noqa: BLE001
        # 法条库坏了必须显式记账：静默变成"本次没有法律依据可查"，
        # 读者会把 pass 读成"法律上没问题"。
        s.tool("law_cite", "法定量比对", f"法条库不可用：{e}"[:180], 0, status="failed")
        s.law_skipped.append(f"法条库不可用（{e}），法定量断言未与条文比对。")
        return s
    s.law_skipped.extend(skipped)

    # 同句多条违法合成**一条**异议，依据并列：一句里既超试用期又压低工资，
    # 读者需要知道"这句话有两处问题"，而不是在清单里看到两条几乎一样的行。
    # 检查器层面仍返回两条（law_cite 不丢信息），合并在报告层做。
    groups = {}
    for f in found:
        if not f.get("quote") or f["quote"] not in text:
            continue
        groups.setdefault(f["quote"], []).append(f)

    added, merged = 0, 0
    for quote, fs in groups.items():
        if any(i["rule_id"] == "LAW-001"
               and _quote_overlap(i["quote"], quote) >= 0.85 for i in s.issues):
            continue
        citations = [f["citation"] for f in fs if f.get("citation")]
        # 同一句话若已由 FACT-001（blocker，事实库断言）判过，不再并列一条：
        # 把法条原文挂到它身上。结论取强的那条，依据补上更硬的原文——
        # 两条并列会让读者以为是两个独立问题（金标准 p16 就是这么多出 1 条误报的）。
        rival = next((i for i in s.issues if i["rule_id"] == "FACT-001"
                      and _quote_overlap(i["quote"], quote) >= 0.6), None)
        if rival is not None:
            if citations:
                rival.setdefault("citation", citations[0])
                have = {c.get("article") for c in (rival.get("citations") or [])}
                rival["citations"] = (rival.get("citations") or []) + [
                    c for c in citations if c.get("article") not in have]
                s.law_cites.extend(citations)
                note = "；另有法条原文：" + "；".join(
                    c["citation"][:110] for c in citations)
                if note not in (rival.get("evidence") or ""):
                    rival["evidence"] = ((rival.get("evidence") or "") + note)[:900]
            merged += 1
            continue
        if len(fs) == 1:
            evidence = fs[0]["evidence"]
        else:
            evidence = "；".join(f"{chr(0x2460 + n)}{f['evidence']}"
                                for n, f in enumerate(fs))
        pos = max(0, text.find(quote))
        issue = s.make_issue(
            "compliance", "LAW-001", quote, f"第 {sentence_no(text, pos)} 句",
            evidence, " + ".join(dict.fromkeys(f["tool"] for f in fs)),
            max(f["confidence"] for f in fs),
            "；".join(dict.fromkeys(f["suggestion"] for f in fs)))
        issue["citation"] = citations[0] if citations else None
        issue["citations"] = citations
        s.add_issue(issue)
        s.law_cites.extend(citations)
        added += 1
    for v in verified:
        if v["text"] not in s.verified_facts:
            s.verified_facts.append(v["text"])
    if added:
        s.decision(f"法条核对命中 {added} 条（依据为条文原文）", "LAW-001",
                   "；".join(i["tool"] for i in found)[:200],
                   ["交给模型凭常识判断是否违法"],
                   "法定量（试用期/竞业限制/试用期工资）有明确条文与上限，"
                   "代码判定可复现，且报告能给出条号供读者核对")
    if merged:
        s.decision(f"{merged} 条法条依据并入 FACT-001（不新增异议）", "LAW-001",
                   "同句已由事实库断言主判，法条原文作为更强依据附在其上",
                   ["并列两条"],
                   "同一现象只留一条主判定；并列会让人以为是两个独立问题")
    return s


def docx_format_review(s: Session, text, rules, ctx):
    """格式维度：对 .docx 版式结构做确定性检查（FORMAT-001~006）。

    只对 Word 文档生效：非 docx 路径直接跳过（该维度只进入 docx 类型的
    dims_for，其它类型不会走到这里）。文本与版式分离：文本链路已由
    read_target_text 提取，这里重读结构（解析开销小，保证结构新鲜）。

    **阈值来自格式规格**（ctx["format_spec"]），不来自代码常量。
    规格里没写的那一项就不检查——没有委托方标准时，"字号对不对"没有答案。
    """
    path = ctx.get("target_path")
    if not path or Path(path).suffix.lower() not in (".docx", ".docm"):
        return s
    spec = ctx.get("format_spec") or {}
    try:
        info = docx_extract.extract(path)
    except Exception as e:                                     # noqa: BLE001
        s.tool("docx_format", str(path), f"版式提取失败：{e}"[:160], 0, status="failed")
        s.decision("docx 版式结构解析失败，格式维不产出（文本链路可能已给出告警）",
                   "OVERALL-009", str(e)[:150], ["中止审查"],
                   "版式解析失败不等于内容无法审；格式维跳过、其余维度照常")
        return s
    ridset = {r["rule_id"] for r in rules}
    added = 0
    for f in docx_format_checks.run_all(info, ridset, spec):
        if any(i["rule_id"] == f["rule_id"] and _quote_overlap(i["quote"], f["quote"]) >= 0.8
               for i in s.issues):
            continue
        pos = int(f.get("start") or 0)
        s.add_issue(s.make_issue(
            f.get("dimension") or "format", f["rule_id"], f["quote"],
            f.get("location") or f"第 {sentence_no(text, pos)} 句",
            f["evidence"], f["tool"], f["confidence"],
            f.get("suggestion")))
        added += 1
    if added:
        s.decision(f"docx 版式确定性检查命中 {added} 条",
                   "OVERALL-010",
                   "字体/字号/缩进/行距/标题/图表编号由 OOXML 属性逐段比对判定",
                   ["交给 LLM 肉眼判断"],
                   "版式属性是结构数据，代码比对可复现且不受模型波动影响")
    return s


BUILTIN_HANDLERS = {
    "builtin:fact": _h_fact,
    "builtin:safety": _h_safety,
    "builtin:style": _h_style,
    "builtin:compliance": _h_compliance,
    "builtin:static_scan": _h_static_scan,
    "builtin:static_scan_force": _h_static_scan_force,
    "builtin:arith": _h_arith,
    "builtin:pattern": _h_pattern,
    "builtin:logic": _h_logic,
    "builtin:comp004_dedupe": _h_comp004,
    "builtin:law_cite": _h_law,
    "builtin:format": docx_format_review,
}

_PACK_MODULES = {}


class _HandlerTool(BaseTool):
    """把旧规则处理器包成工具（迁移期适配器）。

    为什么要包：规则处理器今天都是 `handler(session, text, rules, ctx)`，
    副作用写进 Session。直接改成纯函数等于停掉主流程一个迭代；包成工具后，
    分发走注册表（存在性 / 权限 / 入参 / 审计一条管线），实现先不动。

    权限是 `WRITE`：处理器会往 Session 里追加发现——按权限表属于「修改审查数据」。
    但这是**审查流程自己**在写，所以调用方传 `actor="pipeline"` 并显式授予 WRITE；
    不是「模型可以自己开写权限」。
    """

    permission = Permission.WRITE
    arguments_type = None          # 入参由调用方负责：只有可选的 dimension

    def __init__(self, key, handler, *, session, text, active, legacy_ctx, run_id):
        self.key = key
        # 工具名规则只允许小写字母/数字/点/下划线：旧键里的冒号换成点，
        # 对外仍是同一个处理器（`builtin:fact` ⇄ `builtin.fact`）。
        self.name = _tool_name(key)
        self.description = f"旧规则处理器 {key}（Architecture v1 迁移期适配器）"
        self._handler = handler
        self._session = session
        self._text = text
        self._active = active
        self._ctx = legacy_ctx
        self._run_id = run_id

    async def execute(self, arguments, context):
        before = len(self._session.issues)
        dimension = (arguments or {}).get("dimension")
        if dimension is not None:
            self._ctx["dimension"] = dimension
        self._handler(self._session, self._text, self._active, self._ctx)
        added = self._session.issues[before:]
        try:
            findings = tuple(e.finding for e in encode_issues(added, run_id=self._run_id))
        except CodecMismatch as e:
            # 编不出领域对象不影响报告（报告仍用 Session.issues），但这类结论
            # 进不了 RunState。留痕比吞掉强。
            self._session.decision(
                "规则命中未能编成领域对象", self.key, str(e)[:150],
                ["丢弃该批结论"], "报告照出，但运行态少了一批发现，必须留痕")
            return ToolResult.success(data={"findings": (), "unencoded": len(added)})
        return ToolResult.success(data={"findings": findings, "added": len(added)})


def _tool_name(handler_key: str) -> str:
    """处理器键 → 工具名（`builtin:fact` → `builtin.fact`）。"""
    return str(handler_key).replace(":", ".")


def build_capability_registry(*, on_invoke=None):
    """批次 1 的确定性工具注册表：**脱离 Session** 也能调用。

    与 `build_rule_registry` 的分工（两者共用同一个 `ToolRegistry` 与执行管线）：

        build_rule_registry    按 run 建、绑定 Session、只服务规则流水线，
                               适配的是 `handler(session, text, rules, ctx)`
        build_capability_registry  纯数据进出，能单独测、能交给 Agent 按需调用

    算法本体仍来自 `tools/`（注入，不复制）：`arith_check.run_all` /
    `logic_checks.run_all`。`capabilities` 层不许 import `tools/`，所以注入点在这里。
    """
    registry = ToolRegistry(on_invoke=on_invoke)
    for tool in batch1_tools(
            math_run_all=arith_check.run_all,
            logic_run_all=logic_checks.run_all,
            rule_summarize=rule_exec.summarize,
            rule_counts=rule_exec.counts,
            sha256_bytes=integrity.sha256_bytes,
            pattern_scan_hits=pattern_rules.scan_hits):
        registry.register(tool)
    return registry


def build_rule_registry(session, text, active, legacy_ctx, run_id, agent):
    """把本次要用的规则处理器注册进 ToolRegistry。

    注册表按 run 建：处理器绑定了 Session（迁移期必然如此），跨 run 复用会串数据。
    """
    registry = ToolRegistry()

    def _add(key, handler):
        registry.register(_HandlerTool(
            key, handler, session=session, text=text, active=active,
            legacy_ctx=legacy_ctx, run_id=run_id))

    for key, handler in BUILTIN_HANDLERS.items():
        _add(key, handler)
    try:
        pack = pack_handlers(agent)
    except agent_spec.AgentError:
        # 规则包坏了不能吞：装配阶段就该失败（resolve_handler 也会报同样错）
        raise
    for name, handler in pack.items():
        _add(f"pack:{name}", handler)
    return registry


def rule_executor_from(registry, review_id, run_id):
    """注册表 → 计划执行器（async）：一步 = 一次工具调用。"""
    context = ToolContext(
        review_id=review_id, run_id=run_id, actor="pipeline",
        permissions=frozenset({Permission.READ, Permission.ANALYZE,
                               Permission.WRITE}),
    )

    async def execute(step):
        try:
            result, _invocation = await registry.execute(
                _tool_name(step.handler), {"dimension": step.dimension}, context)
        except ToolError as e:
            # 保持旧语义：配置里写了不存在的处理器 = Agent 配置错误，
            # 由 main() 归到「配置问题」退出码，而不是当成运行时崩溃。
            raise agent_spec.AgentError(str(e)) from e
        if not result.ok:
            raise agent_spec.AgentError(
                f"{step.handler} 执行失败：{'；'.join(result.errors)}")
        return result.data.get("findings", ())

    return execute


def pack_handlers(agent):
    """加载 agents/<id>/checks.py 里的 CHECKERS（按 mtime 缓存）。"""
    path = agent.dir / "checks.py"
    if not path.exists():
        return {}
    key = (agent.id, path.stat().st_mtime)
    if key in _PACK_MODULES:
        return _PACK_MODULES[key]
    mod_name = "review_agent_pack_" + agent.id.replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise agent_spec.AgentError(f"{path} 无法加载")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    handlers = getattr(module, "CHECKERS", None)
    if not isinstance(handlers, dict) or not handlers:
        raise agent_spec.AgentError(f"{path} 必须定义非空 CHECKERS 字典")
    _PACK_MODULES[key] = handlers
    return handlers


def resolve_handler(key, agent):
    """把 manifest 里的处理器名解析成可调用对象；解析不到必须报错。

    静默跳过是最坏的结果：报告看起来一切正常，实际少跑了整整一类检查。

    **已被 Tool Registry 分发取代**（见 `build_rule_registry` / `rule_executor_from`）：
    现在不再有调用点。按 AGENT_RUNTIME §10.3 的迁移规矩保留到阶段 5——
    处理器实现在 `_h_*` 里，删掉解析器等于提前宣布迁移完成，而实现还没纯化。
    两者读的是同一份 `BUILTIN_HANDLERS` / `pack_handlers`，不会各说各话。
    """
    if key in BUILTIN_HANDLERS:
        return BUILTIN_HANDLERS[key]
    if key.startswith("pack:"):
        name = key.split(":", 1)[1]
        handlers = pack_handlers(agent)
        if name not in handlers:
            raise agent_spec.AgentError(
                f"Agent {agent.id} 声明了 {key}，但 checks.py 里没有名为 "
                f"{name!r} 的检查器（现有：{sorted(handlers)}）")
        return handlers[name]
    raise agent_spec.AgentError(f"未知处理器：{key}（只支持 builtin:* / pack:*）")


def merge_agent_rules(rules, agent):
    """调用方传入的规则 + Agent 声明来源的规则，按 rule_id 去重。

    同 rule_id 以 Agent 的为准：Agent 是在「换一套判定标准」，
    继承来的同名规则应被覆盖，否则实际生效的严重度会和 manifest 对不上。
    """
    merged = {r["rule_id"]: r for r in (rules or [])}
    for r in agent_spec.load_pack_rules(agent):
        merged[r["rule_id"]] = r
    return list(merged.values())


def set_rule_index(rules):
    """刷新全局规则索引。

    make_issue 靠 RULE_BY_ID 取 severity 与 rule_text：换成 Agent 自带规则后
    必须重建，否则新规则的 severity 取不到（默认降成 minor）、rule_text 为空——
    报告仍然"看起来正常"，这是最难发现的一类错。
    """
    global RULE_BY_ID
    RULE_BY_ID = rule_index(rules)


def _agent_rule_sources(agent):
    """规则来源的可读形式（相对项目根），用于日志与报告。"""
    out = []
    for p in agent.rule_paths:
        try:
            rel = p.relative_to(ROOT).as_posix()
        except ValueError:
            rel = str(p)
        out.append(rel + ("/*.yaml" if p.is_dir() else ""))
    return out or ["(未声明规则来源)"]


def run_agent_steps(s, agent, text, active, target_path, format_spec=None):
    """按 manifest 执行：先维度处理器（声明顺序），再附加检查。

    返回执过的处理器名列表，写进 S3 日志——用户要能看到"这次到底跑了哪些检查"。

    **分发已由 Tool Registry 接管**：处理器先注册成工具（`_HandlerTool`），
    再经 `registry.execute()` 走「存在性 → 权限 → 执行 → 结果」同一条管线。
    注册表按 run 建（处理器绑定了 Session），所以不存在跨 run 串数据。

    调度顺序由 `plan_rule_steps` 生成（维度绑定 → 附加检查），
    执行由 `run_rule_plan` 压成每步一条 `RuleStepOutcome`。

    format_spec：格式规格的 check 块（只有 format 维用得上），随 ctx 传给处理器。
    """
    ctx = {"target_path": target_path, "target_type": s.target_type, "agent": agent,
           "format_spec": format_spec}
    dims = agent.dims_for(s.target_type)
    plan = plan_rule_steps(
        dims,
        {dim: (agent.handler_for(dim) or "") for dim in dims},
        agent.checks,
    )
    run_id = domain_ids.format_run_id(s.review_id, 1)
    registry = build_rule_registry(s, text, active, ctx, run_id, agent)
    execute = rule_executor_from(registry, s.review_id, run_id)
    result = asyncio.run(run_rule_plan(plan, execute))   # on_error="raise"：与改造前一致
    return s, list(result.executed)


def semantic_review(s: Session, text, active, target_type, use_llm=None, dims=None,
                    agent=None, purpose_info=None):
    """调用 OpenAI 兼容模型按维度补充 issue；失败/未配置则降级为纯规则审。

    返回 (s, review_mode, llm_meta)；review_mode ∈ {"rule+llm", "rule-only"}。
    """
    try:
        cfg = llm_client.load_config(use_llm)
    except Exception as e:
        s.tool("llm_api", "load_config", f"配置加载失败：{e}"[:160], 0, status="failed")
        s.decision("LLM 配置加载失败，降级为纯规则审", "OVERALL-004",
                   str(e)[:150], ["中止审查"], "配置问题不应阻塞规则审")
        return s, "rule-only", {"enabled": False, "available": False,
                                "error": str(e)[:200], "calls": 0, "failures": 0,
                                "tokens": {"input": 0, "output": 0}, "errors": [str(e)[:200]]}

    meta = {
        "enabled": cfg.enabled,
        "available": cfg.available,
        "model": cfg.model,
        "base_url": cfg.base_url,
        "api_key": cfg.masked_key(),
        "source": cfg.source,
        "calls": 0,
        "failures": 0,
        "tokens": {"input": 0, "output": 0},
        "errors": [],
        "dimensions": {},
    }

    if not cfg.available:
        if use_llm is False:
            reason = "已通过 --no-llm 关闭"
        elif not cfg.enabled:
            reason = "config 中 enabled=false"
        else:
            reason = "未配置 API Key 且非本地端点"
        s.tool("llm_api", f"model={cfg.model}", f"跳过语义审：{reason}", 0, status="skipped")
        s.decision("LLM 语义审未启用，降级为纯规则审", "OVERALL-004",
                   reason, ["强制调用（--llm）"], "缺少凭据时不得伪造 LLM 结论")
        meta["reason"] = reason
        return s, "rule-only", meta

    client = llm_client.LLMClient(cfg)
    dims = [d for d in (dims if dims is not None
                        else DIM_FOR_TYPE.get(target_type, []))
            if any(r["dimension"] == d for r in active)]
    existing = list(s.issues)
    charter = build_charter(target_type, agent=agent)
    # 事实库进 prompt：事实维度必须拿 facts.json 当唯一证据，
    # 否则模型会退回参数记忆，"独立裁判"就只剩一条哈希链（2026-10-28）。
    llm_facts = retrieve.load_facts()
    # 用户给的「审查目的」并入任务书：模型据此判断哪些问题值得报，
    # 但 quote 必须仍来自原文（校验不放宽）。
    _pblock = purpose_mod.charter_block(purpose_info)
    if _pblock:
        charter = f"{charter}\n\n{_pblock}" 

    def _sent_key(q):
        """句级指纹：取首句前 20 个有效字符。

        分块审查时相邻块有 400 字符重叠，模型会在两块里对同一句给出
        长度不同的引用（整段 vs 单句），单纯按整段重合度去重挡不住，
        会重复计数。用句级指纹兜住这种情况。
        """
        head = re.split(r"[。；！？\n]", q or "")[0]
        return re.sub(r"[\s，,、：:（）()]", "", head)[:20]

    def already_covered(rule_id, quote):
        """同一规则 + 同一片段/同一句视为重复，避免整句、半句、跨块重复报。"""
        key = _sent_key(quote)
        return any(
            i["rule_id"] == rule_id
            and (_quote_overlap(i["quote"], quote) >= 0.8
                 or (key and _sent_key(i["quote"]) == key))
            for i in existing
        )

    success_calls = 0
    cache_hits = 0
    output_truncated_dims = []
    input_truncated_dims = []
    excluded = []
    chars_total = len(text)
    # 长文分块：不再截断后半段（截断等于"没审"，而报告还要据此下结论）
    chunk_size = min(cfg.max_input_chars, getattr(cfg, "chunk_chars", 6000))
    if getattr(cfg, "chunking", True):
        chunk_spans = plan_chunk_spans(text, chunk_size,
                                       getattr(cfg, "chunk_overlap", 400))
        chunks = [text[start:end] for start, end in chunk_spans]
    else:
        chunks = [text]
        chunk_spans = [(0, len(text))]
    chunked = len(chunks) > 1
    # 每个 (dimension, chunk) 的尝试/成败都要留痕：失败块绝不能静默算成已审。
    chunks_attempted = 0
    chunks_succeeded = 0
    failed_chunks = []
    reviewed_spans = []
    if chunked:
        s.decision(f"原文 {chars_total:,} 字符超出单次上限，改为 {len(chunks)} 块送审",
                   "OVERALL-005",
                   f"每块 ≤{chunk_size:,} 字符，重叠 {getattr(cfg, 'chunk_overlap', 0)} 字符",
                   ["只审前 N 字符（截断）"],
                   "截断意味着后半段根本没审，用户会误以为全文已审；分块用成本换覆盖")

    for dim in dims:
        dim_rules = [r for r in active if r["dimension"] == dim]
        if not dim_rules:
            continue
        dim_added = dim_dup = dim_dropped = dim_cand = dim_open = 0
        dim_retracted = 0
        dim_exempted = 0
        dim_tokens = {"input": 0, "output": 0}
        dim_trunc_in = dim_trunc_out = False
        dim_finish = ""
        for ci, chunk in enumerate(chunks, 1):
            tag = f"{dim}" + (f"|块{ci}/{len(chunks)}" if chunked else "")
            span_start, span_end = chunk_spans[ci - 1]
            chunks_attempted += 1
            started = time.perf_counter()
            try:
                res = llm_review.review_dimension(
                    client, dim, dim_rules, chunk, cfg, charter=charter,
                    full_text=text if chunked else None, facts=llm_facts)
            except llm_client.LLMError as e:
                elapsed = (time.perf_counter() - started) * 1000
                s.llm_usage["failures"] += 1
                s.tool("llm_api", f"{tag} | rules={len(dim_rules)} | model={cfg.model}",
                       f"失败：{e}"[:200], elapsed, status="failed", retry=cfg.max_retries)
                s.decision(f"{tag} 语义审调用失败", "OVERALL-004",
                           str(e)[:150], ["重试", "换端点"], "重试已耗尽，不阻塞其他块/维度")
                meta["errors"].append(f"{dim}#{ci}: {str(e)[:160]}")
                failed_chunks.append({
                    "dim": dim, "chunk": ci,
                    "start": span_start, "end": span_end,
                    "error": str(e)[:200],
                })
                continue

            chunks_succeeded += 1
            # 成功块按真实区间记覆盖。单块模式（chunking=False）下 review_dimension
            # 只会把前 max_input_chars 字符送进模型，覆盖区间也必须同步截到那里；
            # 分块模式下块本身 ≤max_input_chars，这一步是恒等变换。
            covered_end = min(span_end,
                              span_start + max(0, int(cfg.max_input_chars)))
            reviewed_spans.append((span_start, covered_end))
            elapsed = (time.perf_counter() - started) * 1000
            success_calls += 1
            if res.get("cached"):
                cache_hits += 1
            # 输入截断（原文超出上限）与输出截断（max_tokens 写不完）必须分开记
            if res.get("truncated"):
                dim_trunc_in = True
            if res.get("output_truncated"):
                dim_trunc_out = True
            dim_finish = res.get("finish_reason") or dim_finish
            s.llm_usage["calls"] += 1
            s.llm_usage["input"] += res["usage"].get("input", 0)
            s.llm_usage["output"] += res["usage"].get("output", 0)
            dim_tokens["input"] += res["usage"].get("input", 0)
            dim_tokens["output"] += res["usage"].get("output", 0)
            dim_cand += len(res["issues"])
            dim_dropped += len(res["dropped"])
            dim_open += res.get("open_discoveries", 0)

            # 模型自否：单独记「已排除」，既不进待整改清单，也不再静默丢弃
            for ret in res.get("retracted") or []:
                excluded.append({
                    "rule_id": ret.get("rule_id"),
                    "dimension": dim,
                    "quote": ret.get("quote"),
                    "evidence": ret.get("evidence"),
                    "reason": ret.get("reason", "模型自否"),
                })
                dim_retracted += 1

            added = dup = 0
            for cand in res["issues"]:
                if already_covered(cand["rule_id"], cand["quote"]):
                    dup += 1
                    continue
                # 规则层已按语境豁免（如「电话：…」属简历主动提供），
                # 模型按字面再报同一条时必须让位：确定性判据 > 模型判断。
                # 不静默丢——进 excluded 区，报告里能看到「模型曾报、按语境未采纳」。
                _ex = s.exempt_reason(cand["rule_id"], cand["quote"])
                if _ex:
                    excluded.append({
                        "rule_id": cand["rule_id"],
                        "dimension": dim,
                        "quote": cand["quote"],
                        "evidence": cand.get("evidence"),
                        "reason": f"规则层语境豁免：{_ex}",
                    })
                    dim_exempted += 1
                    continue
                pos = text.find(cand["quote"])
                if pos < 0:  # validate 已保证，双保险
                    continue
                issue = s.make_issue(
                    dim, cand["rule_id"], cand["quote"], f"第 {sentence_no(text, pos)} 句",
                    cand["evidence"], f"llm_api({res['model']})", cand["confidence"],
                    cand["suggestion"], basis="model",
                    severity=cand.get("severity"),
                    open_discovery=cand.get("open_discovery", False))
                s.issues.append(issue)
                existing.append(issue)
                added += 1
            dim_added += added
            dim_dup += dup
            s.tool("llm_api", f"{tag} | rules={len(dim_rules)} | model={res['model']}"
                   + (" | 缓存命中" if res.get("cached") else ""),
                   (f"候选 {len(res['issues'])} / 新增 {added} / 去重 {dup} / "
                    f"丢弃 {len(res['dropped'])} / 自否 {len(res.get('retracted') or [])}；"
                    f"规则外 {res.get('open_discoveries', 0)}；"
                    f"tokens in={res['usage'].get('input', 0)} out={res['usage'].get('output', 0)}"
                    + ("；原文已截断" if res["truncated"] else "")
                    + ("；模型输出被截断(finish_reason=length)"
                       if res.get("finish_reason") == "length" else "")),
                   elapsed, status="success")
        if dim_trunc_in:
            input_truncated_dims.append(dim)
        if dim_trunc_out:
            output_truncated_dims.append(dim)
        if dim_added or dim_retracted or dim_dropped or dim_exempted:
            s.decision(
                f"{dim} 语义审新增 {dim_added} 条（去重 {dim_dup} / 丢弃 {dim_dropped}"
                + (f" / 自否排除 {dim_retracted}" if dim_retracted else "")
                + (f" / 语境豁免 {dim_exempted}" if dim_exempted else "") + "）",
                "OVERALL-004",
                "模型输出经 rule_id 白名单 + quote 逐字校验；"
                "minor/nit 按置信度取舍，blocker/major 不占配额",
                ["直接采信模型输出"], "模型输出不可信，必须逐字回查原文")
        meta["dimensions"][dim] = {
            "added": dim_added, "duplicates": dim_dup, "dropped": dim_dropped,
            "candidates": dim_cand, "open_discoveries": dim_open,
            "retracted": dim_retracted, "context_exempted": dim_exempted,
            "tokens": dim_tokens, "truncated": dim_trunc_in,
            "finish_reason": dim_finish,
            "chunks": len(chunks),
        }


    meta["calls"] = s.llm_usage["calls"]
    meta["failures"] = s.llm_usage["failures"]
    meta["tokens"] = {"input": s.llm_usage["input"], "output": s.llm_usage["output"]}
    meta["charter"] = charter
    # 兼容旧字段名：truncated_dimensions 一直指"输出被截断的维度"
    meta["truncated_dimensions"] = output_truncated_dims
    meta["output_truncated_dimensions"] = output_truncated_dims
    meta["input_truncated_dimensions"] = input_truncated_dims
    meta["chars_total"] = chars_total
    # 真实覆盖 = 成功块区间的并集（重叠只算一次）；失败块覆盖的字符不计入。
    meta["chars_reviewed"] = min(covered_chars(reviewed_spans), chars_total)
    meta["chunks_attempted"] = chunks_attempted
    meta["chunks_succeeded"] = chunks_succeeded
    meta["failed_chunks"] = failed_chunks
    meta["cache_hits"] = cache_hits
    meta["chunked"] = chunked
    meta["chunks"] = len(chunks)
    meta["excluded"] = excluded
    meta["open_discoveries"] = sum(
        1 for i in s.issues if i.get("open_discovery"))
    return s, ("rule+llm" if success_calls else "rule-only"), meta


def coverage_from_llm_meta(llm_meta, chars_total):
    """由语义审 meta 推导真实文本覆盖与「是否完整」。

    实现只有一份，在 `review_agent.runtime.nodes.semantic.coverage_from_meta`：
    节点与旧流程必须用同一份推导，否则「节点说审了 80%」「报告说审了 100%」
    会同时存在，而用户只会看到后者。
    """
    return semantic_coverage_from_meta(llm_meta, chars_total)



# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
LEDGER_LOCK_NAME = ".chain.lock"


def _acquire_ledger_lock(timeout=30.0):
    """拿台账写锁（O_EXCL 锁文件，进程内线程与多进程共用一套）。

    为什么必须锁：追加一条台账 = 「读最后一行取 prev_hash → 算 report_hash → 追加」。
    这三步不是原子的，两个审查同时收尾会读到同一个 prev，写出两条 seq 相同的记录，
    链条当场断裂（实测 6 路并发 → `序号不连续 @line2`），而且**报告本身看起来都正常**，
    只有事后 verify 才发现。这类"生产时无感、审计时才炸"的损坏必须从源头堵。
    """
    LEDGER.mkdir(parents=True, exist_ok=True)
    lock = LEDGER / LEDGER_LOCK_NAME
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock
        except PermissionError:
            # Windows 上「锁文件已存在且被别的进程打开」时，O_EXCL 抛的可能是
            # PermissionError 而不是 FileExistsError（实测 6 路并发必现）。
            # 这里不能先判断「锁文件还在不在」：持有者可能在两次系统调用之间就删了锁，
            # 检查会误判成「目录不可写」而直接抛错。统一按争用处理，靠超时兜底。
            _wait_for_ledger_lock(lock, deadline, timeout)
        except FileExistsError:
            # 进程被强杀会留下锁文件：超龄（60s）视为陈旧，直接清掉，不能永久堵死
            _wait_for_ledger_lock(lock, deadline, timeout)


def _wait_for_ledger_lock(lock, deadline, timeout):
    """锁被别人持有：清超龄锁、等一小会、超时给出可操作报错。"""
    try:
        if time.time() - lock.stat().st_mtime > 60:
            lock.unlink()
            return
    except OSError:
        pass
    if time.time() > deadline:
        raise TimeoutError(
            f"等待台账写锁超时（{timeout:.0f}s）：{lock}；"
            "如确认没有审查在跑，可手工删除该锁文件")
    time.sleep(0.01)


def _release_ledger_lock(lock):
    try:
        lock.unlink()
    except OSError:
        pass


def allocate_review_id(date):
    """从 outputs/ + ledger + 新引擎状态目录 已用 ID 中取下一个，并用 O_EXCL 锁文件防并发覆盖。"""
    used = {p.stem for p in OUTPUTS.glob(f"R-{date}-*.json")}
    # 新引擎（--engine v1）只落状态、不写报告：不把它的 ID 算进来，
    # 第二次 v1 会拿到同一个 R-id 并覆盖上一份状态。
    runs_dir = STATE / "runs"
    if runs_dir.is_dir():
        for path in runs_dir.glob("R-*.state.json"):
            stem = path.name.split(".state.json", 1)[0]
            review_id = stem.split("-RUN", 1)[0]
            if review_id:
                used.add(review_id)
    # 任务记录也占号：TaskService 建任务时先落 `state/tasks/`，再执行。
    # 不把这里算进来，一次「建了任务但执行前失败」就会让下一个任务拿到同一个 R-id，
    # 把前一条记录覆盖掉（任务层要能证明「这个号确实存在过」）。
    tasks_dir = STATE / "tasks"
    if tasks_dir.is_dir():
        used.update(path.stem for path in tasks_dir.glob("R-*.json"))
    ledger = LEDGER / "chain.log"
    if ledger.exists():
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = json.loads(line).get("review_id")
                except Exception:
                    continue
                if rid:
                    used.add(rid)
        except OSError:
            pass
    seq = 1
    while True:
        review_id = f"R-{date}-{seq:03d}"
        lock = OUTPUTS / f".{review_id}.lock"
        if review_id in used:
            seq += 1
            continue
        if lock.exists():
            try:
                if time.time() - lock.stat().st_mtime < 3600:
                    seq += 1
                    continue
                lock.unlink()
            except OSError:
                seq += 1
                continue
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            _ID_LOCKS.add(lock)
            return review_id, lock
        except FileExistsError:
            seq += 1


TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "utf-16", "utf-16-le", "utf-16-be")


class UnreadableTargetError(InputError):
    """待审目标不可读（docx 损坏/缺失/非文档）。CLI 与 server 捕获后给出明确错误。

    归到领域错误家族（`InputError`，kind=input / 400）而不是裸 ValueError：
    上层要能区分「用户路径写错了」与「我们服务坏了」。它仍然继承 ValueError，
    所以既有 `except ValueError` 的调用点行为不变。
    """


def read_target_text(path: Path):
    """按候选编码读取待审文本，返回 (text, encoding, warning)。

    旧实现直接 read_text(encoding="utf-8")：喂一个 GBK 文件就 UnicodeDecodeError 崩溃，
    连报告都不产出。审查工具的底线是"对任意输入都给结论"，不是"只接受 UTF-8"。
    .docx 是 zip 二进制，不能按编码解码：走 docx_extract 提取正文文本
    （文本链路与 ingest.py 一致，版式属性另由格式维读取）。
    """
    if path.suffix.lower() in (".docx", ".docm"):
        info = docx_extract.extract(path)
        return (info["text"], "docx-xml",
                None if info.get("text", "").strip()
                else "Word 文档未提取到正文文本（内容为空或仅图片/嵌入对象）")
    raw = path.read_bytes()
    for enc in TEXT_ENCODINGS:
        try:
            return raw.decode(enc), enc, None
        except (UnicodeDecodeError, UnicodeError):
            continue
    # 全部失败：用替换字符解码并明确告警，绝不静默丢内容
    return (raw.decode("utf-8", errors="replace"), "utf-8(replace)",
            "文件编码无法确定，已用替换字符解码（?），部分字符可能失真；建议另存为 UTF-8 后复审")


def run_one(target_path: Path, rules, use_llm=None, progress_path=None,
            type_override=None, agent_id=None, purpose=None, purpose_max=None,
            purpose_notes=None, scenario=None, format_spec=None,
            research_enabled=False):
    t0 = time.perf_counter()
    # 非 UTF-8（GBK/UTF-16）不能让整个进程崩掉：按候选编码依次尝试。
    try:
        text, source_encoding, enc_warn = read_target_text(target_path)
    except (ValueError, OSError) as e:
        # 目标不可读（docx 损坏 / 文件不存在 / 不是文档）：明确报错，绝不带 traceback
        # 崩溃、也不产出「看起来没问题」的空报告。server 侧已有 returncode!=0 兜底，
        # 会把这里的错误文本透传给前端。
        raise UnreadableTargetError(
            f"无法读取待审文件 {target_path.name}：{e}") from e
    except Exception as e:                                     # noqa: BLE001
        # 兜底：解析器的异常类型不受我们控制（xml.etree.ParseError、zipfile 的
        # 各种子类、第三方库的私有异常…）。全系统暴力测试里 bomb.docx 就是抛
        # ParseError 一路冒到进程顶部的——审查工具对任意输入的底线是
        # 「要么给结论、要么给明确错误」，不是「打 traceback」。
        raise UnreadableTargetError(
            f"无法读取待审文件 {target_path.name}：{type(e).__name__}: {e}") from e
    # 零宽字符会让「争议」「360万」这类关键词与数字整段失配，
    # 同时制造误报（条款存在却报缺失）与漏检（金额被拆断）——必须在所有匹配前剥离。
    text, invisible_n = regex_check.strip_invisible(text)
    target_rel = str(target_path.relative_to(ROOT)).replace("\\", "/")

    # Agent 决定「按哪套标准审」：规则来源、维度、检查器、任务书默认值都来自它。
    # 优先级：显式指定 > 自动路由 > 默认 Agent（见 tools/agent_spec.py）
    agent, agent_reason = agent_spec.resolve(target_path, text, agent_id=agent_id)
    rules = merge_agent_rules(rules, agent)
    # 审查情境：决定「哪些规则参与」。必须在 set_rule_index 之前过滤——
    # 索引是用来执行 pattern 规则的，过滤晚了等于关掉的规则还在跑。
    scenario_info = None
    if scenario:
        # 选错情境**不能静默回退到默认**：用户以为用了 A 尺度、实际跑了 B，
        # 这种"看起来正常"的结果比直接报错危险得多。异常由调用方（main / 测试）处理。
        scenario_info = scenario_mod.apply(scenario, rules)
        rules = scenario_info["rules"]

    # 格式规格：决定「版式按哪套标准判」。它和情境是两个维度——
    # 情境决定"问不问这类问题"，规格决定"按什么数值判"。
    # 同样必须在 set_rule_index 之前过滤：没选规格时 FORMAT-001~005 不参与，
    # 晚过滤等于"关掉的规则还在跑"。
    #
    # 只对 docx 生效：纯文本/代码目标不涉及版式，套一遍只会往 limitations 塞噪音。
    # 没显式选规格时用配置里的 default（当前是 none = 不做版式判定），
    # 而不是"留空跳过"——否则规则数虚高、报告里也没有"这一项没查"的说明。
    format_info = None
    if target_path.suffix.lower() in (".docx", ".docm"):
        chosen_spec = format_spec or format_spec_mod.load()["default"]
        format_info = format_spec_mod.apply(chosen_spec, rules)
        rules = format_info["rules"]
    set_rule_index(rules)

    # 生成 review_id（ledger 已用 ID + O_EXCL 锁，避免并发/清理后覆盖）
    date = datetime.date.today().strftime("%Y%m%d")
    review_id, id_lock = allocate_review_id(date)
    log_dir = LOGS / review_id
    log_dir.mkdir(parents=True, exist_ok=True)

    source_meta = load_source_meta(target_path)
    type_info = detect_type_detail(target_path, text)
    detected_type = type_info["type"]
    chosen_type = type_override or detected_type
    s = Session(review_id, target_rel, chosen_type,
                progress_path=progress_path, log_dir=log_dir)
    # 人工指定类型 = 满置信；自动推断则沿用推断置信度
    s.type_confidence = 1.0 if type_override else float(type_info.get("confidence") or 0.6)
    # 本次执行的 run_id：旧流程没有「多次执行」概念，第一次执行就是 RUN01。
    # S4（证据锚定）与 S6（裁定）都要它——证据与裁定必须能归属到同一次执行。
    run_id = domain_ids.format_run_id(review_id, 1)

    if scenario_info:
        _ids = [x["rule_id"] for x in scenario_info["skipped"]]
        s.decision(
            f"审查情境：{scenario_info['name']}（关闭 {len(_ids)} 条规则）",
            "OVERALL-008",
            f"情境 {scenario_info['id']}｜关闭：{'、'.join(_ids) or '无'}",
            ["通用情境全规则审查（会报出初筛阶段无法行动的核验类问题）"],
            "情境只决定哪些规则参与，不改判定标准与严重度；关闭项已写入报告 limitations")

    if format_info:
        _ids = [x["rule_id"] for x in format_info["skipped"]]
        _kn = sorted(format_info["check"])
        s.decision(
            f"格式规格：{format_info['name']}（判定项 {len(_kn)} 项）",
            "OVERALL-011",
            f"规格 {format_info['id']}@{format_info['version']}｜判定项：{'、'.join(_kn) or '无'}"
            f"｜不参与：{'、'.join(_ids) or '无'}",
            ["按内置的某一套固定版式标准判（换份委托要求就不适用）"],
            "版式标准随委托方格式要求变化，规格化后换标准不必改代码；"
            "未判定项已写入报告 limitations")

    # 「审查目的」解析放在最前面：它决定后面 LLM 的任务书内容，
    # 也决定 S5.5 要比对哪些目标。解析本身是确定性的（词典 + 二元组，不调模型）。
    # purpose_max：目的文本参与解析的字数上限（手写默认 300；上传目的文件时更大，
    # 由 server 组装后经 --purpose-max 传入，截断会写进 limitations）。
    purpose_info = None
    if purpose and str(purpose).strip():
        purpose_info = purpose_mod.parse(str(purpose), max_chars=purpose_max)
        # 目的来源的质量风险**放在 limitations 最前**：图片 OCR 认错一个字，
        # 改的是判定标准本身。这条信息不能只留在上传时的 toast 里——
        # 用户事后翻报告只会看到「未找到/不满足」，看不出标准已经被认错了。
        if purpose_notes:
            extra = [str(n) for n in purpose_notes if str(n).strip()]
            purpose_info["source_notes"] = extra
            purpose_info["limitations"] = extra + [
                l for l in purpose_info.get("limitations") or [] if l not in extra]
        s.decision(f"审查目的已解析：{len(purpose_info['intents'])} 个意图 / "
                   f"{len(purpose_info['goals'])} 个目标",
                   "OVERALL-007",
                   f"目的：{purpose_info['text'][:80]}｜意图："
                   + ("、".join(i["name"] for i in purpose_info["intents"]) or "未匹配"),
                   ["忽略目的、按全维度泛扫"],
                   "目的决定'哪些问题值得报'，但不放宽 quote 逐字校验")
        if purpose_info["limitations"]:
            s.tool("purpose_parse", purpose_info["text"][:40],
                   purpose_info["limitations"][0][:180], 0, status="skipped")
    if type_override:
        type_info = dict(type_info, type=chosen_type, confidence=1.0,
                         reason=f"人工指定为 {chosen_type}（自动推断为 {detected_type}）")
    s.decision(f"目标类型判定为 {chosen_type}（置信 {s.type_confidence:.2f}）",
               "OVERALL-005", type_info.get("reason", ""),
               [t for t in agent.dims_by_type if t != chosen_type],
               "类型决定审查任务书与检查项，猜错会让整套判定标准跑偏，故记录置信度")

    # ---- S1 接收解析 ----
    t = time.perf_counter()
    meta = {"target_type": s.target_type, "chars": len(text)}
    s.step("S1", "接收解析", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "待审对象": target_rel,
        "类型判定": f"{s.target_type}（置信 {s.type_confidence:.2f}，"
                    f"{'人工指定' if type_override else '自动推断'}）",
        "类型依据": type_info.get("reason", ""),
        "字符数": f"{len(text):,}",
        "来源": (source_meta or {}).get("source_name") or "原始文本",
        "审查 Agent": f"{agent.name}（{agent.id} v{agent.version}）",
        "选用依据": agent_reason,
    })

    # ---- S2 规则匹配 ----
    t = time.perf_counter()
    # 维度由 Agent 声明：换 Agent 就是换一套判定标准，主流程不再写死
    dims = agent.dims_for(s.target_type)
    active = [r for r in rules if r["dimension"] in dims]
    rule_dim_count = {}
    for r in active:
        rule_dim_count[r["dimension"]] = rule_dim_count.get(r["dimension"], 0) + 1
    dims_text = " / ".join(f"{k} {v}" for k, v in rule_dim_count.items())
    # 执行方式统计：让用户一眼看出本次有多少规则是「代码在跑」、多少只是「给模型的提示」
    exec_summary = rule_exec.summarize(active)
    exec_counts = {k: len(exec_summary[k])
                   for k in ("deterministic", "declarative", "prompt_only")}
    s.step("S2", "规则匹配", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "激活维度": "、".join(dims) or "无（该类型未声明维度）",
        "生效规则": f"{len(active)} 条（{dims_text}）",
        "规则来源": "、".join(_agent_rule_sources(agent))
                    + " + config/rules.state.json（停用的不生效）",
        "执行方式": (f"确定性 {exec_counts['deterministic']} / 声明式 "
                    f"{exec_counts['declarative']} / 仅提示 {exec_counts['prompt_only']}"),
    })
    s.decision(f"激活维度 {dims}，生效规则 {len(active)} 条", "OVERALL-001",
               f"Agent={agent.id} v{agent.version}；按 target_type 匹配维度",
               ["全量规则"], "按类型裁剪避免无关误报")
    s.decision(f"选用审查 Agent：{agent.name}", "OVERALL-005", agent_reason,
               [a["id"] for a in agent_spec.list_agents() if a["id"] != agent.id],
               "不同 Agent 的规则与维度不同，报告必须能追溯到用的是哪一套标准")

    try:
        fact_assertions = retrieve.load_facts().get("assertions", [])
    except Exception:
        fact_assertions = []
    matched_assertions = [
        a for a in fact_assertions
        if a.get("entity") in text and a.get("attr") in text
    ]

    # ---- S3 分维审查 ----
    t = time.perf_counter()
    # 按 Agent manifest 调度：维度处理器（声明顺序）→ 附加检查 → 语义审
    s, executed = run_agent_steps(s, agent, text, active, target_path,
                                  format_spec=(format_info or {}).get("check"))
    s, review_mode, llm_meta = semantic_review(s, text, active, s.target_type,
                                              use_llm, dims=dims, agent=agent,
                                              purpose_info=purpose_info)
    # 归口必须在语义审之后：线索级规则在 S2 就产生了，但"让位给谁"要等模型
    # 给出结论级判定才知道（纯规则模式下没有对手，线索照常保留）。
    collapse_rule_overlap(s)
    merge_law_into_fact(s)
    drop_internal_progress_comp004(s)
    dim_detail = {}
    for d, info in (llm_meta.get("dimensions") or {}).items():
        line = (f"新增 {info.get('added', 0)} / 候选 {info.get('candidates', 0)}"
                f" / 丢弃 {info.get('dropped', 0)}")
        if info.get("retracted"):
            line += f" / 自否 {info['retracted']}"
        if info.get("truncated"):
            line += " / 输入被截断"
        dim_detail[DIM_FULL_NAME.get(d, d)] = line
    tokens = llm_meta.get("tokens") or {}
    s.step("S3", "分维审查", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "审查模式": review_mode,
        "模型": (llm_meta.get("model") if review_mode == "rule+llm"
                 else "未调用（纯规则审）"),
        "审查维度": "、".join(dim_detail) or "仅规则层（未调用模型）",
        "分块": (f"{llm_meta.get('chunks', 1)} 块（长文分块送审）"
                 if llm_meta.get("chunked") else "1 块（未分块）"),
        "缓存命中": f"{llm_meta.get('cache_hits', 0)}/{llm_meta.get('calls', 0)} 次"
                    "（命中不消耗 token）",
        "本步新增异议": f"{len(s.issues)} 条（其中确定性算术 "
                        f"{sum(1 for i in s.issues if i['tool'].startswith('arith_check'))} 条）",
        "执行的检查": "、".join(executed) or "无",
        # 纯规则审时没有"各维结果"，用一句话说清楚，别渲染成 "—: 未调用模型"
        "各维结果": (dim_detail if dim_detail
                     else "未调用模型；本步异议全部来自规则层与确定性算术层"),
        "tokens": f"in {tokens.get('input', 0)} / out {tokens.get('output', 0)}"
                  if review_mode == "rule+llm" else "0（纯规则审）",
        "输出截断维度": "、".join(llm_meta.get("output_truncated_dimensions") or []) or "无",
        "自否排除": f"{len(llm_meta.get('excluded') or [])} 条",
        "调用失败": f"{llm_meta.get('failures', 0)} 次",
    })

    # 语义审全部失败（非未配置）时，在结论里明确标注降级，避免误读为「已多模型复核」
    if review_mode == "rule-only" and llm_meta.get("available"):
        s.decision("LLM 调用全部失败，本次按纯规则审出报告", "OVERALL-004",
                   "；".join(llm_meta.get("errors", []))[:200],
                   ["中止出报告"], "已重试仍失败，规则审结论仍可独立成立")

    # ---- S4 证据锚定（已在各 reviewer 内绑定 quote/rule/evidence/tool/confidence）----
    t = time.perf_counter()
    # 校验规则只有一份实现（review_agent.runtime.nodes.evidence.verify_quote）：
    # 非空 quote 无法逐字回查原文的条目移出报告；缺失型问题（quote 为空）保留。
    # 节点接管后的做法：把 issue 编成领域对象交给证据节点，按节点结论过滤报告条目；
    # 编不出对象时退回同一函数的逐条入口，并把降级写进决策日志（不假装锚定过）。
    _anchor = None
    try:
        _anchor = anchor_findings(
            [e.finding for e in encode_issues(s.issues, run_id=run_id)],
            text, run_id=run_id)
        _dropped_ids = set(_anchor.dropped_ids)
        dropped_unanchored = [i for i in s.issues if i["issue_id"] in _dropped_ids]
    except CodecMismatch as e:
        dropped_unanchored = [
            i for i in s.issues
            if verify_quote(i.get("quote", ""), text) is Verification.UNANCHORED]
        s.decision("S4 证据锚定降级", "OVERALL-004",
                   f"issue 未能编成领域对象，按逐条规则校验：{e}"[:200],
                   ["放弃锚定"], "旧链路必须能跑完；降级本身要留痕")
    if dropped_unanchored:
        _dropped_ids = {i["issue_id"] for i in dropped_unanchored}
        s.issues = [i for i in s.issues if i["issue_id"] not in _dropped_ids]
        _sample = "、".join(
            f"{i.get('issue_id')}/{i.get('rule_id')}"
            for i in dropped_unanchored[:5])
        s.tool("evidence_anchor", "quote 逐字校验",
               f"丢弃 {len(dropped_unanchored)} 条 quote 未命中原文：{_sample}"[:200],
               0, status="failed")
        s.decision(
            f"S4 丢弃 {len(dropped_unanchored)} 条 quote 不可回溯的异议",
            "OVERALL-004", f"示例：{_sample}"[:150],
            ["保留并在报告标注不可回溯"],
            "quote 无法逐字回查原文 = 结论不可审计，不能进入报告")
    traceable = sum(1 for i in s.issues
                    if verify_quote(i.get("quote", ""), text) is Verification.ANCHORED)
    # 证据段落：由节点产出的 Evidence 回写（新增顶层字段，不改既有格式）。
    # 被丢弃的条目不在 _anchor.evidence 里，所以不会出现指向已删结论的悬空证据。
    evidence_block = evidence_to_report(_anchor.evidence) if _anchor else []
    dim_count = {}
    for i in s.issues:
        dim_count[DIM_FULL_NAME.get(i["dimension"], i["dimension"])] = \
            dim_count.get(DIM_FULL_NAME.get(i["dimension"], i["dimension"]), 0) + 1
    s.step("S4", "证据锚定", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "异议总数": f"{len(s.issues)} 条",
        "quote 可回溯": f"{traceable}/{len(s.issues)} 条逐字命中原文",
        "按类型分布": " / ".join(f"{k} {v}" for k, v in dim_count.items()) or "无异议",
        "校验规则": "quote 必须逐字出现在原文，否则丢弃（安全线）",
    })

    # ---- S5 交叉复核 ----
    t = time.perf_counter()
    before_cross = len(s.issues)
    # 规则实现已挪进节点（review_agent.runtime.nodes.cross_review）：
    # 同片段跨维度冲突 → disputes + 双方各降 0.05（下限 0.5）。
    # 发现不可变，所以降权走修订（revisions）而不是就地改 dict——旧实现改完
    # 只剩最终值，「为什么从 0.75 变成 0.70」在报告里查不出来。
    try:
        _cross = review_cross(
            [e.finding for e in encode_issues(s.issues, run_id=run_id)],
            run_id=run_id)
        _adjusted = {f.id: f.confidence for f in _cross.revisions}
        for _it in s.issues:
            if _it["issue_id"] in _adjusted:
                _it["confidence"] = _adjusted[_it["issue_id"]]
        # 回写报告用的 disputes（形状与旧格式一致，见 dispute_to_report_dict）
        s.disputes.extend(dispute_to_report_dict(d) for d in _cross.disputes)
        _via_node = True
    except CodecMismatch:
        # 编不出领域对象时退回旧实现（阶段 5 前旧函数不删）：行为与改造前一致，
        # 且旧实现自己会写决策日志，这里只记一笔降级。
        s = cross_review(s)
        _via_node = False
        s.decision("S5 交叉复核降级", "OVERALL-002",
                   "issue 未能编成领域对象，退回旧实现复核",
                   ["跳过复核"], "旧链路必须能跑完：带着未复核的结论出报告是假绿灯")
    if _via_node:
        if _cross.disputes:
            s.decision(f"交叉复核发现 {len(_cross.disputes)} 处跨维度冲突",
                       "OVERALL-002",
                       "同一原文片段被两个维度分别标记且定级不一致",
                       ["维持原判", "直接取最高严重度"],
                       "冲突需人工裁定，同时下调相关 issue 置信度")
        else:
            s.decision("交叉复核：四维结论无冲突", "OVERALL-002",
                       "事实/安全/合规/风格 无互斥结论", ["有冲突"], "样本无重叠冲突点")
    s.step("S5", "交叉复核", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "争议": f"{len(s.disputes)} 处待人工裁定",
        "复核前后异议数": f"{before_cross} → {len(s.issues)}",
        "规则": "同段多维度互相冲突时进入 disputes，不自动取舍",
    })

    # ---- S5.5 目的比对（用户填了目的才有）----
    # 顺序放在 S5 之后：证据要引用**最终**的 issue 集合，早于交叉复核会拿到已撤销的条目。
    purpose_block = None
    if purpose_info:
        t = time.perf_counter()
        judge = None
        if use_llm is not False and review_mode == "rule+llm":
            try:
                pcfg = llm_client.load_config(use_llm)
                if pcfg.available:
                    pclient = llm_client.LLMClient(pcfg)

                    def judge(messages, _c=pclient):
                        # 只返回 relation/rationale，不返回 quote——见 tools/purpose.py 设计说明
                        return _c.chat(messages).content
            except Exception as e:              # noqa: BLE001
                s.tool("llm_api", "purpose_judge", f"目的语义裁决不可用：{e}"[:160],
                       0, status="skipped")
                judge = None
        purpose_block = purpose_mod.analyze(text, purpose_info, s.issues, judge=judge)
        ev = purpose_block.get("evidence") or []
        by_source = {}
        for e in ev:
            by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        s.step("S5.5", "目的比对", latency_ms=(time.perf_counter() - t) * 1000, detail={
            "目的": purpose_block["text"][:60],
            "识别意图": "、".join(i["name"] for i in purpose_block["intents"]) or "未匹配",
            "目标": f"{sum(1 for g in purpose_block['goals'] if g['status'] == 'found')}"
                    f"/{len(purpose_block['goals'])} 个找到证据",
            "证据": f"{len(ev)} 条（" + "、".join(
                f"{purpose_mod.SOURCE_CN.get(k, k)} {v}" for k, v in by_source.items()) + "）"
                    if ev else "0 条",
            "判定模式": purpose_block["judge"]["mode"],
            "未找到的目标": f"{sum(1 for g in purpose_block['goals'] if g['status'] == 'not_found')} 个"
                            "（未找到 ≠ 不存在，已写入 limitations）",
            "对照矩阵": (match_matrix.summarize(purpose_block.get("matrix"))
                        or (purpose_block.get("matrix") or {}).get("reason") or "未生成"
                        )[:120],
        })

    # ---- S5.6 库外事实核验（可选，默认关闭；CLI/控制台显式开启）----
    # 旧行为：库外主张一律不判，只在报告里写「未命中事实库，覆盖为 0」。
    # 这对用户没有信息量——他要知道的是「这句话到底是不是真的」。
    # 新行为：抽「实体 + 指标 + 数值」，先查本地来源库，再查公开网页；
    # 支持/反驳都带来源，未找到就如实说「查过哪里、没找到」，不再伪造结论。
    research_block = None
    if research_enabled and "fact" in dims:
        t = time.perf_counter()
        try:
            research_block = research.review_claims(
                s, text, list(iter_sentences(text)), fact_assertions,
                enabled=True, web=True)
            # 检索新增的 FACT-001 可能和已有的 FACT-002/004 同句，再归口一次
            collapse_rule_overlap(s)
            merge_law_into_fact(s)
        except Exception as e:                              # noqa: BLE001
            # 检索失败不能影响主报告；把失败写进报告，不静默
            s.tool("research", "库外事实核验", f"失败：{e}"[:180], 0, status="failed")
            research_block = {"enabled": True, "error": f"{type(e).__name__}: {e}",
                              "claims_found": 0, "checked": 0,
                              "supported": [], "refuted": [], "unverified": []}
        s.step("S5.6", "库外事实核验", latency_ms=(time.perf_counter() - t) * 1000, detail={
            "主张": f"{research_block.get('claims_found', 0)} 条",
            "支持": f"{len(research_block.get('supported') or [])} 条",
            "反驳": f"{len(research_block.get('refuted') or [])} 条",
            "未找到": f"{len(research_block.get('unverified') or [])} 条",
            "缓存命中": f"{research_block.get('cache_hits', 0)} 条（不联网）",
            "检索失败": f"{len(research_block.get('errors') or [])} 条",
        })

    # ---- S6 裁定汇总 ----
    t = time.perf_counter()
    issues_sorted = sorted(s.issues, key=lambda i: SEV_ORDER.get(i["severity"], 9))
    stats = {k: 0 for k in SEV_ORDER}
    for i in s.issues:
        stats[i["severity"]] = stats.get(i["severity"], 0) + 1
    # 长文分块后 minor/nit 会按块累积（8 块 × 5 维），几百条同类"无来源/口径"提示
    # 会把真问题淹没。这里把同维度超额的 minor/nit 折叠进 suppressed——
    # **数据不丢**（报告 JSON 完整保留），只是不在清单里逐条展开，并明确告知折叠了多少条。
    suppressed = []
    if (llm_meta or {}).get("chunked"):
        per_dim, kept_list = {}, []
        for i in issues_sorted:
            if i["severity"] in ("minor", "nit"):
                d = i["dimension"]
                per_dim[d] = per_dim.get(d, 0) + 1
                if per_dim[d] > MINOR_KEEP_PER_DIM:
                    suppressed.append(i)
                    continue
            kept_list.append(i)
        issues_sorted = kept_list
        # 同类聚合只在长文里做：短文本清单本来就短，聚合反而可能把
        # 有代表性的那条换成另一处示例，改变结论指向。
        issues_sorted, folded_ids = aggregate_repeats(issues_sorted)
        if folded_ids:
            suppressed.extend(i for i in s.issues if i["issue_id"] in folded_ids)
            s.decision(f"同类提示聚合 {len(folded_ids)} 条", "OVERALL-006",
                       f"规则 {sorted(ADVISORY_RULES)} 下重复条目合并为一条示例，其余进 suppressed",
                       ["逐条展示"], "同一句提示重复几十条会淹没真问题；数据未丢失，可查 suppressed")
    # 归口折叠的条目（同片段让位给结论级规则 / 内部进度豁免）也要落到报告里：
    # 折叠 ≠ 删除，每条都带 folded_into + fold_reason，可在 JSON 里回查。
    suppressed.extend(getattr(s, "folded", []) or [])
    # 裁定交给 Decision Engine（review_agent.runtime.nodes.decision）：
    # blocker→fail、仅 major→warn、库外未核完不得 pass。规则只在 domain.policy
    # 一处实现——历史报告回放、单测、线上走的都是同一个函数。
    research_unverified = bool(research_block and (
        research_block.get("unverified") or research_block.get("needs_confirm")))
    codec_problem = ""
    try:
        findings = [e.finding for e in encode_issues(issues_sorted, run_id=run_id)]
    except CodecMismatch as e:
        # 装不下就降级到计数口径（与旧实现等价），而不是让审查崩在 S6。
        # 降级本身留痕：决策日志 + limitations 各写一条。
        findings = []
        codec_problem = f"issue 未能编成领域对象，裁定降级为计数口径：{e}"
        s.decision("裁定口径降级", "OVERALL-003", codec_problem[:300],
                   ["领域对象裁定"], "旧链路必须能跑完：降级不等于差异可以不留痕")
    if codec_problem:
        _draft = DEFAULT_POLICY.decide_from_counts(
            blocker=stats["blocker"], major=stats["major"],
            external_unverified=research_unverified,
            total=sum(stats.values()),
        )
        verdict, _risk, _human = _draft.verdict.value, _draft.risk_level.value, \
            _draft.requires_human_review
        _reasons, _policy = list(_draft.reasons), _draft.policy_id
    else:
        _decision = build_decision(findings, run_id=run_id,
                                   external_unverified=research_unverified)
        verdict, _risk, _human = _decision.verdict.value, _decision.risk_level.value, \
            _decision.requires_human_review
        _reasons, _policy = list(_decision.reasons), _decision.policy_id
    # 决策日志的既有字段一字未改（历史 logs 要能并排比对），新增字段只增不改。
    s.decision(f"verdict={verdict}", "OVERALL-003",
               f"blocker={stats['blocker']}, major={stats['major']}",
               ["pass", "warn", "fail"], "存在 blocker→fail；仅 major→warn；否则 pass",
               risk_level=_risk, requires_human_review=_human,
               policy=_policy, reasons=_reasons)

    # 阶段 1 收口：报告里的 issues / suppressed 由领域对象回写（编码→解码→逐键校验）。
    # 校验不过不静默：保留原 dict 照常出报告，但差异进决策日志与 limitations。
    # 编解码前面已经失败过就不再试：同一份输入不可能第二次通过，重复报错只会淹没真因。
    if not codec_problem:
        try:
            issues_sorted = export_issues(issues_sorted, run_id=run_id)
            suppressed = export_issues(suppressed, run_id=run_id)
        except CodecMismatch as e:
            codec_problem = f"issue 回写校验未通过，报告保留旧 dict：{e}"
            s.decision("issue 未由领域对象回写", "OVERALL-003", str(e)[:300],
                       ["保留旧 dict"], "报告照出，但差异必须留痕，不能悄悄放过")

    to_confirm = [i for i in issues_sorted if i["confidence"] < 0.7]
    to_fix = [i for i in issues_sorted if i["severity"] in ("blocker", "major")]

    # ---- 整改单：每条 issue 必须回答「改哪一句 / 改成什么 / 不改会怎样」----
    # 只给「建议」的报告仍然把改写工作留给用户；这一层把能确定性改的
    # （法条上限、事实数值、用词、隐私脱敏、缺失条款模板）直接给出可替换文本，
    # 改不了的明确标「需人工」，不假装有方案。
    action_plan = remediation.attach(issues_sorted, fact_assertions)
    for it in issues_sorted:
        if it.get("fix") and it["fix"].get("consequence"):
            it["consequence"] = it["fix"]["consequence"]

    core = (f"整体裁定 {verdict}：共 {len(s.issues)} 条异议"
            f"（blocker {stats['blocker']} / major {stats['major']} / minor {stats['minor']} / nit {stats['nit']}）。"
            + (f"交叉复核 {len(s.disputes)} 处争议待人工裁定。" if s.disputes else "")
            + ("需整改后使用。"
               if verdict != "pass"
               else "未发现阻断性问题，但「未发现」不等于「通过」："
                    "本引擎存在已知漏报（跨段推理、日期计算等），请结合人工复核确认。"))
    if (verdict == "warn" and stats["major"] == 0 and research_block
            and (research_block.get("unverified")
                 or research_block.get("needs_confirm"))):
        _bits = []
        if research_block.get("needs_confirm"):
            _bits.append(f"{len(research_block['needs_confirm'])} 条单一来源反驳待人工确认")
        if research_block.get("unverified"):
            _bits.append(f"{len(research_block['unverified'])} 条库外事实未找到可比来源")
        core = ("整体裁定 warn：规则层未发现 major 问题，但"
                + "、".join(_bits) + "（详见「事实核验」），需人工核验后再使用。")
    if research_block and (research_block.get("refuted") or research_block.get("supported")):
        core += (f" 外部事实核验：驳回 {len(research_block.get('refuted') or [])} 条、"
                 f"支持 {len(research_block.get('supported') or [])} 条"
                 f"（来源见报告「事实核验」）。")
    if review_mode == "rule-only" and llm_meta.get("available"):
        core += "注意：LLM 语义审调用失败，已降级为纯规则审。"

    rule_by_dim = {}
    for r in active:
        rule_by_dim[r["dimension"]] = rule_by_dim.get(r["dimension"], 0) + 1
    total_assertions = len(fact_assertions)
    matched_count = len(matched_assertions)
    coverage = {
        "fact_assertions_total": total_assertions,
        "fact_assertions_matched": matched_count,
        "fact_coverage_pct": round(matched_count / total_assertions * 100)
        if total_assertions else 0,
        "active_rules": len(active),
        # 声明总数 = 当前审查维度集合内的规则数（未停用口径）：format 维只对
        # docx 类型激活，若把全库规则都算进来，txt 审查会误报"大量规则未启用"
        "declared_rules": len([r for r in rules_store.load_all_rules()
                               if r["dimension"] in dims]),
        "active_by_dimension": rule_by_dim,
        "review_mode": review_mode,
        "agent": agent.id,
        "agent_version": agent.version,
        "dimensions_checked": list(dims),
        "rule_execution_counts": exec_counts,
    }
    # 文本覆盖率：审了多少字 / 一共多少字。只报问题数不报覆盖率，
    # 用户会默认"全文都审过了"——长文截断时这是最危险的错觉。
    # 真实口径从语义审 meta 推导：成功块区间的并集 + 失败块清单。
    chars_total = len(text)
    text_cov = coverage_from_llm_meta(llm_meta, chars_total)
    coverage.update(text_cov)
    chars_reviewed = text_cov["chars_reviewed"]
    coverage["chunked"] = bool((llm_meta or {}).get("chunked"))
    coverage["chunks"] = int((llm_meta or {}).get("chunks") or 1)
    coverage["source_encoding"] = source_encoding
    coverage["invisible_removed"] = invisible_n
    if invisible_n:
        core += f" （原文含 {invisible_n} 个不可见字符，已剥离后再审查）"
    coverage["type_detection"] = {
        "type": s.target_type,
        "confidence": round(float(getattr(s, "type_confidence", 1.0)), 2),
        "source": "manual" if type_override else "auto",
        "reason": type_info.get("reason", ""),
    }
    if not coverage["complete"]:
        # 结论正文也要说，不能只藏在 limitations 里（很多人只看裁定那一段）
        if chars_reviewed < chars_total:
            core += (f" 注意：本次语义审仅覆盖 {coverage['text_coverage_pct']}% 的原文"
                     f"（{chars_reviewed:,}/{chars_total:,} 字符），未覆盖部分未被审查。")
        if coverage["failed_chunks"]:
            core += (f" 另有 {len(coverage['failed_chunks'])} 个「维度×块」语义审失败，"
                     "相关字符段未完成模型复核，本次结论不代表该段已审。")
    if source_meta:
        # 文本由 PDF / Word / 压缩包抽取而来：标注来源，便于回溯原件
        coverage["source"] = source_meta
    charter_text = (llm_meta or {}).get("charter")
    if charter_text:
        coverage["charter"] = charter_text
    # 明确声明「本次没查什么」——只报问题数而不说覆盖边界，会给人虚假的安全感
    if research_enabled and "fact" in dims:
        _r = research_block or {}
        _un = len(_r.get("unverified") or [])
        coverage["uncovered"] = [
            (f"外部事实核验已执行：核对 {_r.get('checked', 0)} 条库外主张"
             f"（支持 {len(_r.get('supported') or [])} / 反驳 {len(_r.get('refuted') or [])} / "
             f"未找到 {_un}）。未找到不等于虚假；来源为公开网页，口径/时效可能不一致"),
            "未逐条抓取所有引用来源的全文：核验以搜索摘要与已收录来源为主",
            "未审查图片/图表/附件内容（仅审可提取的文本层）",
            "未做法律效力判断与条款可执行性评估（合规维仅检查要件齐备性）",
            "模型输出存在推理与识别误差，所有结论均需人工复核",
        ]
    else:
        coverage["uncovered"] = [
            "未做外部事实核验：仅比对项目内置事实库"
            f"（本次命中 {matched_count}/{total_assertions} 条断言）；"
            "如需联网核验，用 --research 运行",
            "未验证文中引用的外部来源、链接与数据是否真实可访问",
            "未审查图片/图表/附件内容（仅审可提取的文本层）",
            "未做法律效力判断与条款可执行性评估（合规维仅检查要件齐备性）",
            "模型输出存在推理与识别误差，所有结论均需人工复核",
        ]
    if s.target_type == "code":
        # by-design，但必须让用户看见：代码对象只查安全与风格，不查事实/逻辑/合规
        coverage["uncovered"].append(
            "代码对象只运行 安全 + 风格 两个维度：注释/docstring 中的事实性错误、"
            "数值/日期矛盾不在本次检查范围内（如需请以文本方式单独审查）")

    limitations = []
    # 引擎自身的表达力缺口（见 review_agent/adapters/issue_codec.py）：
    # 报告照出，但必须自报「这次有结论没能走领域对象回写」。
    if codec_problem:
        limitations.append(
            f"本次报告有条目未能由领域模型回写（{codec_problem[:160]}）："
            "结论按旧格式输出，相关字段口径未经验证。")
    # 情境说明紧跟"未审范围"类信息：它解释的是"这一整类问题本次不问"，
    # 比"某条断言未命中"更影响结论解读，所以要排在前面。
    # 后面的编码告警与输入截断会 insert(0)，仍排在它之前（那两种更严重）。
    if scenario_info and scenario_info["limitations"]:
        limitations.extend(scenario_info["limitations"])
    # 格式规格的说明同样属于"这一整类问题本次没查"：没选规格时字体/字号/缩进/
    # 行距/标题层级一律不判。「未报出版式问题」不能被读成「版式合规」。
    if format_info and format_info["limitations"]:
        limitations.extend(format_info["limitations"])
    if invisible_n:
        limitations.append(
            f"原文含 {invisible_n} 个零宽/不可见字符（U+200B 等），已剥离后再匹配；"
            "报告中的 quote 取自剥离后的文本。")
    if enc_warn:
        limitations.insert(0, enc_warn)
    elif source_encoding not in ("utf-8", "utf-8-sig"):
        limitations.append(
            f"原文件编码为 {source_encoding}，已按该编码解码；若出现乱码请另存为 UTF-8 后复审。")
    if total_assertions == 0:
        limitations.append("事实库为空，事实审未执行。")
    elif matched_count == 0:
        limitations.append(
            f"本次未命中事实库断言（共 {total_assertions} 条），事实审覆盖为 0。")
    elif matched_count < total_assertions:
        limitations.append(
            f"事实库共 {total_assertions} 条断言，本次仅匹配 {matched_count} 条，未覆盖部分需人工核验。")
    # 事实审的弹药边界必须自报：库外实体只判"缺出处"，不判真伪。
    # 不写这句，读者会把"没报事实错误"当成"事实全对"。
    if ("fact" in dims and total_assertions
            and not (research_block and research_block.get("enabled"))):
        _ents = []
        for _a in fact_assertions:
            _e = str(_a.get("entity") or "")
            if _e and _e not in _ents:
                _ents.append(_e)
        limitations.append(
            f"事实审的证据仅限事实库内 {total_assertions} 条断言（涉及："
            f"{'、'.join(_ents[:8])}{'等' if len(_ents) > 8 else ''}）；"
            "库外实体只判「缺可核验出处」，不判真伪——要判真伪请先补进 knowledge/facts.json。")
    if research_block and (research_block.get("checked") or research_block.get("error")):
        _r = research_block
        if _r.get("error"):
            limitations.append(f"库外事实核验失败（{_r['error']}），本次结论未包含外部检索。")
        elif _r.get("enabled"):
            limitations.append(
                f"库外事实核验：检索 {_r.get('checked', 0)} 条主张"
                f"（支持 {len(_r.get('supported') or [])} / 反驳 {len(_r.get('refuted') or [])} / "
                f"未找到 {len(_r.get('unverified') or [])}）。"
                "「未找到」表示公开检索没有给出可比来源，不等于该说法虚假；"
                "「反驳」也只说明检索来源与原文数值不一致，最终口径需人工确认。")
            if _r.get("errors"):
                limitations.append(
                    "部分检索失败：" + "；".join(str(e) for e in _r["errors"][:3])[:300])
    # 被点名但还没断言的高频实体：说清"没判"，避免"没报错=都对"
    if "fact" in dims and (getattr(s, "unverified_hits", None) or []):
        limitations.append(
            f"本次文本涉及事实库暂无断言的高频实体："
            f"{'、'.join(s.unverified_hits[:6])}——其数字/日期**未做真伪判定**"
            "（不是已核验通过）。")
    # 规则清单外的模型发现：必须自报来源，否则读者会当成"规则级判定"
    open_n = int((llm_meta or {}).get("open_discoveries") or 0)
    if open_n:
        limitations.append(
            f"模型另报 {open_n} 条「规则清单外发现」（rule_id 以 OPEN- 开头）："
            "来源=模型推测、未经规则库定义、不参与规则覆盖率统计，严重度取默认 minor 档，"
            "须人工确认后再采信。")
    if review_mode == "rule-only":
        limitations.append("本次为纯规则审查，LLM 语义审未成功执行，复杂语义/长文推理未覆盖。")
    # 输入截断：最严重的一种"没审"，必须排在最前，避免被后面的条目淹没
    if coverage["input_truncated_dimensions"]:
        unseen = chars_total - chars_reviewed
        limitations.insert(0,
                           f"原文共 {chars_total:,} 字符，语义审仅覆盖前 {chars_reviewed:,} 字符"
                           f"（{coverage['text_coverage_pct']}%），其后 {unseen:,} 字符"
                           f"**完全未进入审查**（涉及维度："
                           f"{'、'.join(coverage['input_truncated_dimensions'])}）——"
                           "本次结论不得视为对全文有效，请分段复审未覆盖部分。")
    if coverage["output_truncated_dimensions"]:
        limitations.append(
            f"以下维度的模型输出被 max_tokens 截断（finish_reason=length）："
            f"{'、'.join(coverage['output_truncated_dimensions'])}——"
            "已救回写完的条目，但排在其后的发现可能丢失，这些维度不能视为「已审完」。")
    # 块级失败：旧实现失败块 continue 后仍按全文算覆盖，报告因此写「全覆盖」。
    # 逐条列出失败块字符区间，并说明该段只经过规则层。
    if coverage["failed_chunks"]:
        _note = failed_chunk_limitation(coverage["failed_chunks"],
                                        coverage["chunks"])
        limitations.insert(0 if not coverage["input_truncated_dimensions"] else 1,
                           _note)
    if coverage.get("chunked"):
        if coverage["failed_chunks"]:
            limitations.append(
                f"长文分块审查：原文 {chars_total:,} 字符按段落切成 "
                f"{coverage['chunks']} 块分别送审；其中 "
                f"{len(coverage['failed_chunks'])} 处「维度×块」失败，"
                "失败块对应的字符段未完成语义审（区间见上一条失败块说明）。")
        else:
            limitations.append(
                f"长文分块审查：原文 {chars_total:,} 字符按段落切成 {coverage['chunks']} 块分别送审，"
                "全文均已覆盖；但**相隔很远的段落之间的语义冲突**没有被模型同时看到，"
                "这类跨块问题依赖确定性算术层与人工复核兜底。")
    folded_n = sum(i.get("aggregated_count", 0) - 1 for i in issues_sorted
                   if i.get("aggregated_count"))
    if folded_n:
        limitations.append(
            f"同类提示已聚合：{folded_n} 条重复的口径/来源类提示并入代表条目"
            "（条目上标 aggregated_count 与 aggregated_ids），避免淹没真问题。")
    if suppressed:
        limitations.append(
            f"另有 {len(suppressed)} 条 minor/nit 同类过多，已折叠到报告 suppressed 字段"
            f"（每维度清单最多展开 {MINOR_KEEP_PER_DIM} 条）——数据未丢失，"
            "查看 JSON 或按维度筛选即可复核。")
    if exec_summary["prompt_only"]:
        limitations.append(
            f"以下 {len(exec_summary['prompt_only'])} 条规则没有确定性执行器，"
            f"只作为模型提示：{'、'.join(exec_summary['prompt_only'])}——"
            "涉及这些规则的结论完全依赖模型判断，跨次可能漂移，需按模型输出对待。")
    # Agent 自己声明「不查什么」：只报问题数会给人虚假安全感
    for b in agent.boundaries:
        limitations.append(f"{agent.name} 不覆盖：{b}")
    excluded_all = list((llm_meta or {}).get("excluded") or [])
    ctx_excluded = [e for e in excluded_all
                    if str(e.get("reason") or "").startswith("规则层语境豁免")]
    self_neg = [e for e in excluded_all if e not in ctx_excluded]
    if self_neg:
        limitations.append(
            f"{len(self_neg)} 条模型输出被判为「模型自否」（依据里自己写了不成立/撤回/无需整改），"
            "已移入报告 excluded 区，不计入异议数与待整改清单。")
    if ctx_excluded:
        # 确定性判据优先于模型：规则层已按同句信号判定「不属于问题」，
        # 模型按字面再报同一条时不采纳。必须写出来，否则用户会以为模型没报过。
        limitations.append(
            f"{len(ctx_excluded)} 条模型命中被规则层的语境豁免否决（同句存在"
            "「主动提供」类信号，如简历里的「电话：…」不构成隐私泄露），"
            "已移入 excluded 区；原文与被否理由都可查。")
    if source_meta:
        kind_label = {"pdf": "PDF", "docx": "Word", "image": "图片",
                      "zip": "压缩包"}.get(source_meta.get("kind"), "文档")
        src_name = source_meta.get("source_name") or "原始文档"
        if source_meta.get("kind") == "image":
            engine = (source_meta.get("ocr") or {}).get("engine") or "未知引擎"
            limitations.append(
                f"待审文本由图片「{src_name}」经 {engine} 引擎 OCR 转写生成，"
                "识别文本可能存在识别误差，结论请以原图为准；"
                "图片中的版式、图表与图形信息未纳入审查。")
        else:
            limitations.append(
                f"待审文本由{kind_label}「{src_name}」抽取生成，"
                "仅覆盖可提取的文本层；原文件的版式、表格线、图表与内嵌图片未纳入审查。")
        for warn in (source_meta.get("warnings") or [])[:5]:
            limitations.append(f"摄取提示：{warn}")
    if s.target_type == "contract":
        limitations.append("合同审为关键词 + 否定语境预筛，不构成法律意见，需人工复核条款结构。")
    # 法条库的覆盖范围必须自报：读者要知道"这次引的依据是哪几部法、哪一版、
    # 有没有限缩"。不写清楚，一条「依据：第十九条」会被当成"全部法律都查过了"。
    law_lib_block = None
    try:
        _laws, _arts = law_index.load_all()
        _desc = "、".join(
            f"《{l['name']}》{l['status']}·{l['effective_date']}施行·{l['_loaded']} 条"
            for l in _laws)
        limitations.append(
            f"法律依据仅限已收录的法条库：{_desc}，共 {len(_arts)} 条。"
            "库外法律/条款不作引用，也不以模型记忆代替条文；"
            "命中条文 ≠ 条文适用（例外情形仍需人工判断）。")
        law_lib_block = {
            "available": True,
            "articles_total": len(_arts),
            "laws": [{"id": l["id"], "name": l["name"], "status": l["status"],
                      "effective_date": l["effective_date"],
                      "articles": l["_loaded"], "source_url": l["source_url"],
                      "retrieved_at": l["retrieved_at"]} for l in _laws],
            # 本次真正被引用到的条文（去重，按法名+条号）
            "cited": _dedupe_cites(s.law_cites),
            "skipped": list(s.law_skipped),
        }
    except Exception as e:                                   # noqa: BLE001
        limitations.append(f"法条库不可用（{e}）：本次未做法条级依据比对，"
                           "涉及法律条款的结论仅为线索。")
        law_lib_block = {"available": False, "error": str(e)[:200]}
    for note in s.law_skipped:
        if note not in limitations:
            limitations.append(note)
    if len(active) < coverage["declared_rules"]:
        limitations.append(
            f"本次仅启用 {len(active)}/{coverage['declared_rules']} 条规则，结论范围相应收窄。")
    if verdict == "pass":
        limitations.append("pass 仅表示未命中当前规则与事实库，不等于内容真实可信。")
    # 目的比对的边界单独立项：它回答的是"围绕这个目的看到了什么"，
    # 不能替代全维度审查，也不能因为"目标全部未找到"就被读成"没问题"。
    if purpose_block:
        # 来源质量（目的文件是图片 OCR 等）排在最前：判定标准存疑时，
        # 后面任何「覆盖了多少规则」的说法都不该先被读到。
        quality = [l for l in (purpose_info or {}).get("source_notes") or []
                   if l not in limitations]
        if quality:
            limitations[:0] = quality
        for line in purpose_block.get("limitations") or []:
            if line not in limitations:
                limitations.append(line)
        if not purpose_block.get("intents"):
            limitations.append(
                "本次目的未派生审查目标：报告里的证据只按关键词定位，"
                "不代表已按该目的做过判定。")

    # ------------------------------------------------------------------
    # 知识库缺口记账（docs/知识库自学习.md §8）
    #
    # 这不是给读者的结论，是给下一版 facts.json 的工单：本次有多少事实判断
    # 是在「库里没有依据」的情况下作出的。它只写 state/，**不写事实库**——
    # 缺口里的 quote 来自被审文档，永远只是「待审主张」，不是证据。
    # ------------------------------------------------------------------
    gap_block = None
    try:
        gap_path = _gap_log_path()
        gap_rows = fact_gap.collect(
            issues=issues_sorted,
            unverified=getattr(s, "unverified_hits", None) or [],
            law_skipped=list(s.law_skipped),
            review_id=review_id, target=target_rel)
        if gap_path is None:
            # 显式关闭：报告必须写出来，否则「没记录」会被读成「没缺口」
            gap_block = {"recorded": 0, "disabled": True, "usable_as_evidence": False,
                         "reason": "本次以 --no-gap-log 运行，缺口未落盘（不写被审文本片段）"}
        else:
            written = fact_gap.record(gap_rows, path=gap_path) if gap_rows else {
                "new": 0, "repeat": 0, "path": str(gap_path)}
            gap_block = {
                "recorded": len(gap_rows),
                "new": written["new"],
                "repeat": written["repeat"],
                "log": str(gap_path),
                "kinds": {},
                "top": [{"gap_id": r["gap_id"], "kind": r["kind"],
                         "entity_guess": r["entity_guess"],
                         "quote": r["quote"][:60]} for r in gap_rows[:5]],
                "usable_as_evidence": False,
            }
            for r in gap_rows:
                gap_block["kinds"][r["kind"]] = gap_block["kinds"].get(r["kind"], 0) + 1
            s.tool("fact_gap", "知识库缺口记账",
                   f"{len(gap_rows)} 条（新增 {written['new']} / 已存在 {written['repeat']}）",
                   0, status="success")
            if gap_rows:
                limitation = (
                    f"本次有 {len(gap_rows)} 条判断缺少库内依据（模型按常识下的判断 / "
                    "缺条文未执行的法定量检查 / 有主张但无出处），已记入知识库缺口清单"
                    f"（{gap_block['log']}）。**没有依据的沉默不等于没有问题**；"
                    "缺口只说明「当时没得比」，不说明「文中说法是错的」。")
                if limitation not in limitations:
                    limitations.append(limitation)
                s.decision(
                    f"记录知识库缺口 {len(gap_rows)} 条", "FACT-001",
                    "；".join(f"{k} {v}" for k, v in gap_block["kinds"].items()),
                    ["不记账（下次仍然靠人回忆该补什么）"],
                    "缺口清单是下一版事实库的工时来源；不记就永远是手灌那几条")
    except Exception as e:                                   # noqa: BLE001
        # 记账失败不能影响审查结论，但必须留痕——否则「没记录」会被读成「没缺口」
        s.tool("fact_gap", "知识库缺口记账", f"失败：{e}"[:180], 0, status="failed")
        gap_block = {"error": str(e)[:200], "recorded": 0, "usable_as_evidence": False}

    # 完整性哈希（S1 起已可算 target/knowledge；logs 待写后算）
    target_hash = integrity.sha256_file(str(target_path))
    knowledge_hash = integrity.sha256_dir(str(KNOWLEDGE))
    rules_dir_hash = integrity.sha256_dir(str(RULES))
    ruleset_hash = integrity.sha256_bytes(
        integrity.serialize_active_rules(active).encode("utf-8"))

    report = {
        # 报告字段契约的版本：新增/删除顶层字段时改这里，读取方据此兼容。
        # 1.2：新增顶层 evidence[]（逐条结论的引用校验结论），既有字段未动。
        "schema_version": "1.2",
        "review_id": review_id,
        "target_type": s.target_type,
        "target_type_source": "manual" if type_override else "auto",
        # 本次用的判定标准：Agent 决定规则/维度/检查器，报告必须能追溯到它。
        # 否则「同一份文件两次结论不同」会无法解释是换了标准还是换了模型。
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "version": agent.version,
            "selected_by": agent_reason,
            "dimensions": agent.dimension_meta(),
            "checked_dimensions": list(dims),
            "checks": list(agent.checks),
            "rule_sources": _agent_rule_sources(agent),
            "boundaries": list(agent.boundaries),
        },
        # 审查情境：这次按哪套尺度判、关了哪些规则。null 表示没指定（= 全规则）。
        # 只记 id 不够：规则名会随版本变，报告要能自己解释"当时关了什么"。
        "scenario": ({
            "id": scenario_info["id"],
            "name": scenario_info["name"],
            "version": scenario_info["version"],
            "desc": scenario_info["desc"],
            "disabled_rules": [x["rule_id"] for x in scenario_info["skipped"]],
            "disabled_detail": scenario_info["skipped"],
        } if scenario_info else None),
        # 格式规格：这次版式按哪套标准判。只对 docx 有值（其余类型为 null）。
        # 记 source 是为了半年后能回答「这条 22 磅是从哪份文件来的」。
        "format_spec": ({
            "id": format_info["id"],
            "name": format_info["name"],
            "version": format_info["version"],
            "source": format_info["source"],
            "desc": format_info["desc"],
            "criteria": format_info["check"],
            "disabled_rules": [x["rule_id"] for x in format_info["skipped"]],
            "disabled_detail": format_info["skipped"],
        } if format_info else None),
        "target_path": target_rel,
        # 原文快照：标注视图用（与 issue.quote 的校验基准完全一致，
        # 不受原文件之后被修改/删除的影响）
        "target_text": text,
        "review_mode": review_mode,
        "active_rules": active,
        "integrity": {
            "target_hash": target_hash,
            "ruleset_hash": ruleset_hash,
            "rules_dir_hash": rules_dir_hash,
            # 规则集版本号：用于区分「规则后来升级过」与「规则库被篡改」
            "rules_version": integrity.rules_version(),
            # Agent 判定标准（manifest + 自带规则/检查器）也进哈希链：
            # 规则生效与否由 ruleset_hash 管，但「维度/检查器/任务书」变了只有这里能发现
            "agent_dir_hash": (integrity.sha256_dir(str(agent.dir))
                               if agent.dir.is_dir() else None),
            "agent_version": agent.version,
            "knowledge_hash": knowledge_hash,
            # 知识库版本：区分「补数据」与「偷改事实库」
            "knowledge_version": integrity.knowledge_version(),
            "report_hash": None,
            "logs_hash": None,
            "prev_report_hash": None,
        },
        "verdict": verdict,
        "verdict_scale": ["pass", "warn", "fail"],
        "core_conclusion": core,
        "key_facts": list(s.verified_facts),
        "coverage": coverage,
        # 用户填的「审查目的」→ 派生目标 → 原文证据。为空表示本次没填目的。
        # 与 issues 分开：issues 是"哪里错了"，purpose 是"按你的目的，看到了什么"。
        "purpose": purpose_block,
        # 规则执行方式：deterministic / declarative / prompt_only 三类 rule_id 清单。
        # 没有这块，读者无法区分「代码算出来的」和「模型读出来的」。
        "rule_execution": exec_summary,
        # 法条库：收录了什么、本次引到哪几条、哪些检查因缺条文没跑。
        # 与 issues 分开：issues 说"哪里错了"，这里说"依据从哪来"。
        "law_library": law_lib_block,
        # 知识库缺口：本次有多少判断是「库里没依据」时下的，记进 state/gap_log.jsonl。
        # 它回答的是"下一版事实库该补什么"，不是"文中哪里错了"。
        "fact_gaps": gap_block,
        # 库外事实核验：抽了哪些主张、查到了什么、支持/反驳/未找到各多少。
        # 没有这一块，「未命中事实库」会被读成「事实没问题」。
        "research": research_block,
        # 整改单：每条 issue 的 fix（改哪一句/改成什么/不改会怎样）+ 前 N 条优先项。
        "action_plan": action_plan,
        "limitations": limitations,
        "issues": issues_sorted,
        # 证据段落：每条结论的引用核过没有（anchored / placeholder / …）。
        # issues[].quote 只说「引了哪句」，这里回答「这句能不能回查原文」。
        "evidence": evidence_block,
        # 模型自否的条目：不进 to_fix / 统计，但保留下来（模型曾怀疑过也是信息）
        "excluded": list((llm_meta or {}).get("excluded") or []),
        # 规则清单外发现（OPEN-*）：单独列一份，标明来源=模型。
        # 不列出来，这些条目会混在 issues 里看着像"规则判定"，审计时无法区分。
        "open_discoveries": [
            {"rule_id": i["rule_id"], "severity": i["severity"],
             "quote": (i.get("quote") or "")[:120],
             "confidence": i.get("confidence"), "source": "model"}
            for i in issues_sorted if i.get("open_discovery")
        ],
        # 折叠的次要条目：JSON 完整保留，清单不展开（数量在 limitations 里说明）
        "suppressed": suppressed,
        "disputes": s.disputes,
        "to_confirm": to_confirm,
        "to_fix": to_fix,
        "stats": stats,
        "llm": llm_meta,
        "reviewer": "independent-review-agent",
        "model_version": REPORT_MODEL_VERSION,
        "timestamp": now_iso(),
    }

    s.step("S6", "裁定汇总", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "裁定": f"{verdict}（blocker {stats['blocker']} / major {stats['major']} / "
                f"minor {stats['minor']} / nit {stats['nit']}）",
        "待整改": f"{len(to_fix)} 条（blocker + major）",
        "可直接改": f"{action_plan.get('concrete', 0)} 条（附改写文本）",
        "需人工改": f"{action_plan.get('manual', 0)} 条",
        "待人工确认": f"{len(to_confirm)} 条（置信度 < 0.7）",
        "文本覆盖": f"{coverage['text_coverage_pct']}%"
                    f"（{coverage['chars_reviewed']:,}/{coverage['chars_total']:,} 字符）",
        "长文分块": (f"{coverage['chunks']} 块" if coverage.get("chunked") else "未分块"),
        "同类折叠": f"{len(suppressed)} 条 minor/nit 未在清单展开",
        "模型自否": f"{len((llm_meta or {}).get('excluded') or [])} 条已排除",
        "审查边界": f"{len(limitations)} 条（见报告 limitations）",
    })

    # 写任务/成本日志（decisions/tools 已由 Session 逐条 append）
    (log_dir / "task.json").write_text(json.dumps({
        "review_id": review_id,
        "user_request": f"审查 {target_rel}",
        "steps": s.steps,
        "final_status": "success",
        "abort_reason": None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    cost = {
        "total_latency_ms": round((time.perf_counter() - t0) * 1000),
        "llm_tokens": {
            "input": s.llm_usage["input"],
            "output": s.llm_usage["output"],
        },
        "llm_model": llm_meta.get("model") if review_mode == "rule+llm" else None,
        "llm_calls": s.llm_usage["calls"],
        "llm_failures": s.llm_usage["failures"],
        "tool_calls": len(s.tool_logs),
        "tool_success": sum(1 for t in s.tool_logs if t["status"] == "success"),
        "tool_failures": sum(1 for t in s.tool_logs if t["status"] == "failed"),
        "tool_skipped": sum(1 for t in s.tool_logs if t["status"] == "skipped"),
        "success_rate": _success_rate(s.tool_logs),
        "errors": list(llm_meta.get("errors", [])),
    }
    (log_dir / "cost.json").write_text(json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8")

    logs_hash = integrity.sha256_dir(str(log_dir))
    report["integrity"]["logs_hash"] = logs_hash

    # ledger 链（文件可能尚不存在，需先判断：先确定 prev_report_hash，再算 report_hash）
    #
    # 「读 prev → 算 report_hash → 追加」必须在同一把锁内完成：report_hash 里含
    # prev_report_hash，读和写之间只要插进另一个审查，两条记录就会指向同一个 prev，
    # 链条断裂。锁的粒度只覆盖这三步，不包住文件写盘（那部分按 review_id 互不相干）。
    ledger_path = LEDGER / "chain.log"
    lock = _acquire_ledger_lock()
    try:
        prev = "0" * 64
        seq_no = 1
        if ledger_path.exists():
            lines = [l for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if lines:
                prev = json.loads(lines[-1]).get("report_hash", "0" * 64)
                seq_no = len(lines) + 1
        report["integrity"]["prev_report_hash"] = prev

        # report_hash（自指处理：必须在 prev_report_hash 写入之后再算，否则校验对不上）
        report["integrity"]["report_hash"] = integrity.compute_report_hash(report)

        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "seq": seq_no, "review_id": review_id, "target_hash": target_hash,
                "report_hash": report["integrity"]["report_hash"], "prev_hash": prev,
                "rules_dir_hash": rules_dir_hash,
                "rules_version": integrity.rules_version(),
                "ts": now_iso(),
            }, ensure_ascii=False) + "\n")
    finally:
        _release_ledger_lock(lock)

    # 输出 JSON + MD
    out_json = OUTPUTS / f"{review_id}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUTS / f"{review_id}.md").write_text(render_md(report), encoding="utf-8")

    try:
        id_lock.unlink()
    except OSError:
        pass
    _ID_LOCKS.discard(id_lock)
    return report


# ----------------------------------------------------------------------------
# 影子流水线（Architecture v1）：六节点 + 编排器
# ----------------------------------------------------------------------------
# 这不是 run_one 的替代，是**并行跑的对照实现**：同一份输入，旧 S1~S6 与新的
# 六节点流水线各跑一遍，比 stats / findings 集合 / verdict。差异必须先解释，
# 才允许把默认链路切过去（REFACTOR_PLAN 阶段 2 的硬判据）。
#
# 目前只接 rule-only：语义审的入队（去重、语境豁免、make_issue）还是 Session 语义，
# 硬接会改变行为，留到能力层整理之后。所以这个函数在 `use_llm is not False` 时
# 直接抛错，而不是假装审了模型。

class _FinalizeNode:
    """finalize 阶段：把状态收敛成影子报告。

    不是旧报告：`purpose` / `law_library` / `research` / `action_plan` / 哈希链
    这几块新流水线还没算，报告里显式列进 `unsupported_blocks`。
    「没算就不写」比「写个空块」诚实，也是切流的验收单。
    """

    name = "finalize"
    stage = Stage.FINALIZE
    produces = frozenset({"report", "audit_export"})

    def __init__(self, review_id: str) -> None:
        self.review_id = review_id

    async def run(self, ctx):
        report = build_shadow_report(
            ctx.state, target_meta=ctx.state.artifacts.get("target_meta"))
        return NodeResult.ok(output={
            "report": report,
            "audit_export": {"report_id": self.review_id,
                             "schema_version": report["schema_version"]},
        })


def run_typed_pipeline(target_path: Path, rules, *, review_id=None,
                       agent_id=None, type_override=None, store=None,
                       semantic_review_fn=None, use_llm=False,
                       llm_meta_out=None, purpose_info=None,
                       integrity_out=None):
    """用新的六节点流水线跑一次审查，返回 (RunState, RunOutcome, Session)。

    失败时抛 UnreadableTargetError（与 run_one 同语义），调用方不必区分两套异常。

    `store`：给一个目录就落盘状态（`FileRunStore`），进程重启后可以按 run_id 续跑。
    `semantic_review_fn`：给了就把语义审接进 analysis 阶段（一步模型调用，返回
    `llm_review.review_dimension` 同形状的 dict）；不给则只跑规则审。
    `use_llm`：`False`（默认）纯规则；`None` 能配就配、配不上降级；`True` 配不上报错。
    `llm_meta_out`：传入 dict 时回填模型可用性与来源（CLI 摘要要用）。

    `use_llm=True` 但模型不可用时抛 `ConfigError`——**显式要求过模型就不许静默降级**。
    """
    if review_id is None:
        date = datetime.date.today().strftime("%Y%m%d")
        review_id, id_lock = allocate_review_id(date)
        try:
            id_lock.unlink()
        except OSError:
            pass
        _ID_LOCKS.discard(id_lock)

    try:
        text, source_encoding, enc_warn = read_target_text(target_path)
    except (ValueError, OSError) as e:
        raise UnreadableTargetError(
            f"无法读取待审文件 {target_path.name}：{e}") from e

    type_info = detect_type_detail(target_path, text)
    chosen_type = type_override or type_info["type"]
    target_rel = str(target_path.relative_to(ROOT)).replace("\\", "/")

    agent, _reason = agent_spec.resolve(target_path, text, agent_id=agent_id)
    rules = merge_agent_rules(rules, agent)
    set_rule_index(rules)
    active = [r for r in rules if r.get("enabled", True)]
    dims = agent.dims_for(chosen_type)

    run_id = domain_ids.format_run_id(review_id, 1)
    session = Session(review_id, target_rel, chosen_type)
    session.type_confidence = float(type_info.get("confidence") or 0.6)
    legacy_ctx = {"target_path": target_path, "target_type": chosen_type,
                  "agent": agent, "format_spec": None}

    registry = build_rule_registry(session, text, active, legacy_ctx, run_id, agent)
    _rule_executor = rule_executor_from(registry, review_id, run_id)

    profile = ReviewProfile(
        id=agent.id, name=agent.name, version=agent.version,
        dimensions=tuple(dims),
    )
    # 完整性指纹：v1 报告也要能验证「结论没被改过」。算一次、两处用
    # （初始状态的 target_hash + 影子报告的 integrity 块）。
    integrity_inputs = _integrity_inputs(target_path, rules, active, agent)
    if integrity_out is not None:
        integrity_out.update(integrity_inputs)
    app = ReviewApplication(
        store=FileRunStore(store) if store else None,
        checkpoints=MemoryCheckpointStore(),
    )
    state = app.initial_state(
        review_id=review_id, run_id=run_id, profile=profile,
        target_hash=integrity_inputs["target_hash"],
    )
    if semantic_review_fn is None and use_llm is not False:
        semantic_review_fn, llm_meta = make_llm_review_fn(
            active, text, chosen_type, use_llm=use_llm, agent=agent,
            purpose_info=purpose_info)
        if llm_meta_out is not None:
            llm_meta_out.update(llm_meta)
        if semantic_review_fn is None and use_llm is True:
            raise ConfigError(
                f"LLM 不可用：{llm_meta.get('reason')}；"
                "如需纯规则审请显式传 --no-llm")
    semantic_executor = None
    if semantic_review_fn is not None:
        semantic_executor = make_semantic_executor(
            session=session, text=text, run_id=run_id,
            review_fn=semantic_review_fn)
    ports = PipelinePorts(
        reader=read_target_text, rule_executor=_rule_executor,
        finalize_node=_FinalizeNode(review_id),
        semantic_executor=semantic_executor,
    )
    config = {
        "target_path": str(target_path),
        "dimensions": list(dims),
        "handlers": {dim: (agent.handler_for(dim) or "") for dim in dims},
        "checks": list(agent.checks),
    }
    outcome = asyncio.run(app.run(state, ports, config=config))
    return outcome.state, outcome, session


def make_semantic_executor(*, session, text, run_id, review_fn,
                           tool_label="llm_api"):
    """把「调一次模型」与「候选入库」接起来，供 SemanticReviewerNode 使用。

    `review_fn(step)` 负责真正的模型调用（返回与 `llm_review.review_dimension`
    同形状的 dict）。把它作为参数注入，而不是在这里直接建 client：
    节点测试与离线校验要能跑，不需要网络与 API Key。

    去重用的「已有结论」在构造时取一次快照，之后每步新收的发现追加进去——
    分块审查时后面的块要能看到前面块已经报过什么。
    """
    known = [e.finding for e in encode_issues(session.issues, run_id=run_id)]
    exemptions = tuple((rid, q, reason) for rid, q, reason in session.context_exempt)

    def executor(step, start_seq):
        res = review_fn(step) or {}
        candidates = [
            Candidate(
                rule_id=str(item.get("rule_id") or ""),
                quote=str(item.get("quote") or ""),
                evidence=str(item.get("evidence") or ""),
                confidence=float(item.get("confidence") or 0.0),
                suggestion=str(item.get("suggestion") or ""),
                severity=item.get("severity"),
                open_discovery=bool(item.get("open_discovery")),
            )
            for item in (res.get("issues") or ())
        ]
        model = str(res.get("model") or "")
        result = ingest_candidates(
            candidates,
            IngestContext(
                run_id=run_id, text=text, dimension=step.dimension,
                existing=tuple(known), exemptions=exemptions,
                tool=f"{tool_label}({model})" if model else tool_label,
            ),
            start_seq=start_seq,
        )
        known.extend(result.findings)
        return SemanticStepOutcome(
            findings=result.findings,
            candidates=len(candidates),
            dropped=len(res.get("dropped") or ()),
            retracted=tuple(res.get("retracted") or ()),
            cached=bool(res.get("cached")),
            usage=res.get("usage") or {},
            truncated=bool(res.get("truncated")),
            output_truncated=bool(res.get("output_truncated")),
            finish_reason=str(res.get("finish_reason") or ""),
        )

    return executor


def compare_with_report(report: dict, state) -> dict:
    """双跑比对：新流水线的发现 vs 旧报告。

    判据（REFACTOR_PLAN 阶段 2 的硬要求）：
      * 新流水线保留下来的每条发现，必须能对上旧报告的 `issues` **或** `suppressed`；
        对不上就是新实现的 bug，不许用「模型波动」搪塞。
      * 旧报告里的每条 issue，必须在新流水线里存在。
      * verdict 必须一致。

    `suppressed` 也算对上，是因为同片段归口折叠（`collapse_rule_overlap`）等动作
    目前仍只在旧流程里跑：折叠不等于丢结论，只是「不在清单里展开」。
    """
    from review_agent.runtime import surviving_findings as _surviving

    def key(rule_id, quote):
        return (str(rule_id or ""), str(quote or ""))

    typed = [key(f.rule.rule_id if f.rule else "", f.quote)
             for f in _surviving(state)]
    legacy = [key(i.get("rule_id"), i.get("quote"))
              for i in (report.get("issues") or [])]
    folded = [key(i.get("rule_id"), i.get("quote"))
              for i in (report.get("suppressed") or [])]
    legacy_set, folded_set, typed_set = set(legacy), set(folded), set(typed)
    unexplained = [k for k in typed if k not in legacy_set and k not in folded_set]
    missing = [k for k in legacy if k not in typed_set]
    folded_matches = [k for k in typed if k in folded_set and k not in legacy_set]
    verdict_new = state.decision.verdict.value if state.decision else None
    return {
        "ok": not unexplained and not missing and verdict_new == report.get("verdict"),
        "verdict_new": verdict_new,
        "verdict_legacy": report.get("verdict"),
        "unexplained": unexplained,
        "missing": missing,
        "folded": folded_matches,
        "typed_count": len(typed),
        "legacy_count": len(legacy),
    }


_MD_DANGEROUS_PROTO = re.compile(r"(?i)\b(javascript|data|vbscript)\s*:")


def _md_safe(text):
    """把原文/证据里的 Markdown 语法与危险协议转义。

    报告 .md 会原样嵌入 issue.quote：一份被污染的文档可以借此在报告里
    渲染出可点击的 `[官方文档](javascript:alert(1))`（第三轮 N8）。
    转义后链接语法失效、协议不可执行，但文字仍可读。
    """
    if text is None:
        return ""
    # 先占位再统一转义，避免协议里的反斜杠被二次转义成 `\\:`
    s = _MD_DANGEROUS_PROTO.sub(lambda m: m.group(1) + "\x00P\x00", str(text))
    for ch in ("\\", "`", "*", "_", "[", "]", "|", "<", ">"):
        s = s.replace(ch, "\\" + ch)
    return s.replace("\x00P\x00", "\\:")


def _md_flat(text):
    """单行化 + 转义：给"本应是一行"的字段（目的/引用/依据）用。

    `_md_safe` 只处理语法字符，**不动换行**——而 Markdown 的标题、表格、列表
    全部靠行首生效。所以一行 `查承诺\\n\\n## 伪造标题` 足以在报告里插一章。
    换行压成空格后，行内语法才真正失效。
    """
    return _md_safe(re.sub(r"\s+", " ", str(text if text is not None else "")).strip())


def render_md(report):
    lines = []
    lines.append(f"# 审查报告 {report['review_id']}\n")
    lines.append(f"- 待审对象：`{report['target_path']}`（{report['target_type']}）")
    lines.append(f"- 裁定：**{report['verdict']}**（可选：{', '.join(report['verdict_scale'])}）")
    mode = report.get("review_mode", "rule-only")
    llm = report.get("llm") or {}
    mode_line = f"- 审查模式：**{mode}**"
    if mode == "rule+llm":
        tokens = llm.get("tokens") or {}
        mode_line += (f"（模型 {llm.get('model', '?')}，"
                      f"tokens in {tokens.get('input', 0)} / out {tokens.get('output', 0)}）")
    elif llm.get("reason"):
        mode_line += f"（{llm['reason']}）"
    ex = report.get("rule_execution") or {}
    if ex:
        po = ex.get("prompt_only") or []
        line = (f"- 规则执行：确定性 {len(ex.get('deterministic') or [])} / 声明式 "
                f"{len(ex.get('declarative') or [])} / 仅提示 {len(po)}")
        if po:
            line += f"（{'、'.join(po)} 只由模型判断，结论不可复现）"
        lines.append(line)
    head_agent = report.get("agent") or {}
    if head_agent:
        mode_line += (f"｜审查 Agent：**{head_agent.get('name')}**"
                      f"（{head_agent.get('id')} v{head_agent.get('version')}）")
    lines.append(mode_line)
    lines.append(f"- 一句话结论：{report['core_conclusion']}")
    cov = report.get("coverage") or {}
    if cov.get("chars_total"):
        verb = "全覆盖" if cov.get("complete") else "**未全覆盖**"
        lines.append(f"- 文本覆盖：{verb} —— 已审 {cov.get('chars_reviewed'):,} / "
                     f"{cov.get('chars_total'):,} 字符（{cov.get('text_coverage_pct')}%）")
        if not cov.get("complete", True):
            un = cov.get("input_truncated_dimensions") or []
            ou = cov.get("output_truncated_dimensions") or []
            if un:
                lines.append(f"  - 输入截断（其后内容未审）：{'、'.join(un)}")
            if ou:
                lines.append(f"  - 输出截断（结论可能不完整）：{'、'.join(ou)}")
            failed = cov.get("failed_chunks") or []
            for f in failed[:3]:
                lines.append(
                    f"  - 块级语义审失败：第 {f.get('chunk')}/{cov.get('chunks', '?')} 块"
                    f"（字符 {_span_label(f.get('start'), f.get('end'))}，"
                    f"维度 {f.get('dim')}）——该段仅经过规则层，未进入模型复核")
            if len(failed) > 3:
                lines.append(f"  - 另有 {len(failed) - 3} 处块级语义审失败，"
                             "详见报告 coverage.failed_chunks")
    lines.append("")
    plan = report.get("action_plan") or {}
    if plan.get("top"):
        lines.append("## 整改单（先改这几处）")
        lines.append("")
        lines.append("> 只列能落到具体文本的改法；「需人工」的条目在下方异议清单里给判断依据。")
        lines.append("")
        lines.append("| # | 位置 | 原文 | 改成 | 不改的后果 |")
        lines.append("|---|---|---|---|---|")
        for idx, it in enumerate(plan["top"], 1):
            frm = (it.get("from") or "（新增条款）").replace("|", "丨").replace("\n", " ")
            if it.get("has_rewrite") and it.get("to"):
                to = str(it["to"]).replace("|", "丨").replace("\n", " ")
            else:
                to = "需人工：" + str(it.get("how") or "按依据改写").replace("|", "丨").replace("\n", " ")
            lines.append(
                f"| {idx} | {_md_flat(it.get('where'))} | {_md_safe(frm[:60])} "
                f"| {_md_safe(to[:80])} | {_md_safe(str(it.get('consequence') or '')[:60])} |")
        lines.append("")
        lines.append(f"> {plan.get('headline', '')}")
        lines.append("")
    _rs = report.get("research") or {}
    if _rs.get("enabled") and (_rs.get("checked") or _rs.get("errors")):
        lines.append("## 事实核验（外部检索）")
        lines.append("")
        lines.append(
            f"- 主张 **{_rs.get('claims_found', 0)}** 条，已查 **{_rs.get('checked', 0)}** 条："
            f"支持 {len(_rs.get('supported') or [])} / 反驳 {len(_rs.get('refuted') or [])} / "
            f"未找到 {len(_rs.get('unverified') or [])}；缓存命中 {_rs.get('cache_hits', 0)} 条；"
            f"复用已学结论 {_rs.get('learned_hits', 0)} 条；命中本地来源 {_rs.get('local_hits', 0)} 条。")
        lines.append("- 「未找到」只说明公开检索没有可比来源，**不等于虚假**；"
                     "「反驳」说明检索来源与原文数值不一致，最终口径仍需人工确认。")
        if _rs.get("needs_confirm"):
            lines.append(f"- 其中 **{len(_rs['needs_confirm'])} 条**为单一来源反驳，"
                         "已转入「待人工确认」，未进入整改单。")
        for title, key in (("反驳（原文与来源不一致）", "refuted"),
                           ("支持（找到一致来源）", "supported"),
                           ("未找到可比来源", "unverified")):
            items = _rs.get(key) or []
            if not items:
                continue
            lines.append("")
            lines.append(f"**{title}**")
            for it in items[:5]:
                lines.append(f"- {_md_flat(it.get('quote'))}")
                lines.append(f"  - 检索：`{_md_flat(it.get('query'))}`；结论："
                             f"{_md_flat(it.get('reason'))}")
                for s in (it.get("sources") or [])[:2]:
                    if s.get("url"):
                        lines.append(f"  - 来源：[{_md_flat(s.get('title'))[:60]}]({s.get('url')})")
        if _rs.get("errors"):
            lines.append("")
            lines.append("> 部分检索失败：" + "；".join(_md_flat(e) for e in _rs["errors"][:3]))
        lines.append("")
    pur = report.get("purpose") or None
    if pur:
        lines.append("## 审查目的与目标证据")
        # purpose 块的每个字段都来自外部（用户输入 / 原文摘句），必须和 issues 部分
        # 一样过 _md_flat：_md_safe 只转义语法字符，**换行不转义**，
        # 一行 "查承诺\n\n## 伪造标题" 就能在报告里插入一张伪表格/伪标题。
        lines.append(f"**目的**：{_md_flat(pur.get('text'))}")
        if pur.get("intents"):
            lines.append("- 识别意图：" + "、".join(
                f"{_md_flat(i['name'])}（{'/'.join(_md_flat(k) for k in i['matched_keywords'][:3])}）"
                for i in pur["intents"]))
        if pur.get("terms"):
            lines.append("- 目的关键词：" + "、".join(
                _md_flat(t) for t in pur["terms"][:10]))
        lines.append(f"- 判定模式：**{_md_flat(pur.get('judge', {}).get('mode'))}**"
                     + ("（语义裁决由模型完成，quote 仍逐字取自原文）"
                        if pur.get("judge", {}).get("mode") == "rule+llm" else
                        "（仅规则层与探针定位，未做语义裁决）"))
        if pur.get("goals"):
            lines.append("")
            lines.append("| 目标 | 结论 | 证据 |")
            lines.append("|---|---|---|")
            for g in pur["goals"]:
                by = g.get("decided_by") or []
                # 「找到」有歧义：探针是正则（原文有这类表述），语义命中是模型
                # （原文回应了这个问题）。分开标，结论的可信度才看得出来。
                judge_only = "judge" in by and "probe" not in by and "rule" not in by
                mark = ("✅ 语义命中" if judge_only else "✅ 找到") \
                    if g["status"] == "found" else "— 未找到"
                ev_cell = "、".join(g["evidence_ids"])
                if not ev_cell and g.get("rule_missing"):
                    ev_cell = "规则判定缺失：" + "、".join(g["rule_missing"])
                lines.append(f"| {_md_flat(g['question'])} | {mark} | {_md_flat(ev_cell) or '—'} |")
            # 判定依据单独列：只给"命中"不给理由，读者无法判断命中的对不对
            notes = [(g["question"], g["note"]) for g in pur["goals"] if g.get("note")]
            if notes:
                lines.append("")
                for q, n in notes:
                    lines.append(f"- 目标「{_md_flat(q)}」：{_md_flat(n)}")
        # 要求侧 vs 候选侧：证据层回答"哪句话相关"，矩阵回答"要求逐项对上了没有"。
        # 放在目标表之后、证据清单之前——读者先看结论，再下去看原文。
        mx = pur.get("matrix") or {}
        if mx.get("available"):
            cov = mx.get("coverage") or {}
            lines.append("")
            lines.append("### 要求对照矩阵")
            lines.append(f"要求侧：{_md_flat(mx.get('requirement_source') or '—')}；"
                         f"候选侧：{_md_flat(mx.get('candidate_source') or '—')}；"
                         f"对照 {cov.get('total', 0)} 项")
            lines.append("")
            lines.append("| 维度 | 要求 | 候选 | 结论 |")
            lines.append("|---|---|---|---|")
            for d in mx.get("dims") or []:
                st = match_matrix.STATUS_CN.get(d.get("status"), d.get("status") or "")
                lines.append(f"| {_md_flat(d.get('name'))} | {_md_flat(d.get('requirement') or '—')} "
                             f"| {_md_flat(d.get('candidate') or '—')} | {st} |")
            # 只有「满足」行的说明单独列（“要求本科，候选侧硕士”这种信息只在这行）；
            # 未满足/未提及/需确认的行，说明已经在下面的「差距与待确认」里，
            # 两处都写一遍会让读者以为是两件事。
            notes_mx = [(d.get("name"), d.get("note")) for d in mx.get("dims") or []
                        if d.get("note") and d.get("status") == "met"]
            if notes_mx:
                lines.append("")
                for n, note in notes_mx:
                    lines.append(f"- {_md_flat(n)}：{_md_flat(note)}")
            items = [i for d in mx.get("dims") or [] for i in (d.get("items") or [])]
            if items:
                lines.append("")
                lines.append("**技能逐项**（出现≠掌握）")
                for i in items:
                    mark = "原文出现" if i.get("present") else "**未出现**"
                    where = f"（{_md_flat(i['where'])}）" if i.get("where") else ""
                    lines.append(f"- {_md_flat(i.get('name'))}：{mark}{where}")
            if mx.get("gaps"):
                lines.append("")
                lines.append("**差距与待确认**")
                for g in mx["gaps"]:
                    lines.append(f"- {_md_flat(g)}")
            for l in mx.get("limitations") or []:
                lines.append(f"> {_md_flat(l)}")
        elif mx.get("reason"):
            lines.append("")
            lines.append(f"> 未生成对照矩阵：{_md_flat(mx['reason'])}")
        evs = pur.get("evidence") or []
        if evs:
            lines.append("")
            lines.append("**证据清单**（quote 逐字取自原文）")
            for e in evs:
                rel = purpose_mod.RELATION_CN.get(e["relation"], e["relation"])
                src = purpose_mod.SOURCE_CN.get(e["source"], e["source"])
                extra = f"｜{_md_flat(e['rule_id'])}" if e.get("rule_id") else ""
                lines.append(f"- `{e['id']}` **{rel}**（{src}{extra}）：{_md_flat(e['quote'])}")
                if e.get("rationale"):
                    lines.append(f"  - 依据：{_md_flat(e['rationale'])}")
        if pur.get("limitations"):
            lines.append("")
            for l in pur["limitations"]:
                lines.append(f"> {_md_flat(l)}")
        lines.append("")
    _lib = report.get("law_library") or {}
    if _lib.get("cited"):
        lines.append("## 引用法条（依据原文）")
        lines.append("")
        lines.append("> 以下条文来自本地法条库，报告只引库内条文；"
                     "命中条文不等于条文适用（例外情形需人工判断）。")
        lines.append("")
        for _c in _lib["cited"]:
            lines.append(f"- **《{_c.get('law')}》{_c.get('article')}**"
                         f"（{_c.get('status', '')}）")
            lines.append(f"  {_md_safe(_c.get('text') or '')}")
        if _lib.get("skipped"):
            lines.append("")
            lines.append("- 未执行的检查（法条库缺条文，不是没问题）：")
            for _s in _lib["skipped"]:
                lines.append(f"  - {_md_safe(_s)}")
        lines.append("")
    _gaps = report.get("fact_gaps") or {}
    if _gaps.get("recorded"):
        lines.append("## 知识库缺口（本次判断缺依据的地方）")
        lines.append("")
        lines.append("> 这一段说的是**我们手里缺什么**，不是文中哪里错了。"
                     "缺口只说明当时没有库内依据可比，不说明文中说法是错的。")
        lines.append("")
        for _k, _n in (_gaps.get("kinds") or {}).items():
            lines.append(f"- `{_k}`：{_n} 条 —— {fact_gap.KIND_DESC.get(_k, '')}")
        for _t in (_gaps.get("top") or [])[:5]:
            _q = (_t.get("quote") or "").replace("|", "丨")
            lines.append(f"  - `{_t.get('gap_id')}` P{fact_gap.KIND_PRIORITY.get(_t.get('kind'), 9)}"
                         f"｜{_t.get('entity_guess') or '未识别实体'}｜{_md_flat(_q)}")
        lines.append("")
        lines.append(f"> 清单：`{_gaps.get('log')}`；"
                     "补齐只能走 `tools/fact_promote.py`（唯一写入口）+ 人工确认来源。")
        lines.append("")
    if report["key_facts"]:
        lines.append("## 关键事实（已核验）")
        for k in report["key_facts"]:
            lines.append(f"- {k}")
        lines.append("")
    lines.append("## 异议清单（共 {} 条）".format(len(report["issues"])))
    for i in report["issues"]:
        lines.append(f"\n### [{i['issue_id']}] {i['dimension']} / {i['severity']}（conf {i['confidence']}）")
        fix = i.get("fix") or {}
        if fix.get("action") == "insert":
            lines.append(f"- 原文：（缺失条款；应在{_md_safe(fix.get('where') or '合同末尾')}新增）")
        else:
            lines.append(f"- 原文：{_md_safe(i['quote'])}")
        lines.append(f"- 位置：{_md_safe(i['location'])}")
        auto_tag = "（自动提案·措辞级匹配，需人工确认）" if i["rule_id"].startswith("AUTO-") else ""
        lines.append(f"- 规则：{i['rule_id']}{auto_tag} — {_md_safe(i['rule_text'])}")
        lines.append(f"- 证据：{_md_safe(i['evidence'])}")
        # 法条依据单独一行：读者要的是"哪一条"，不是在证据长文里找
        _cites = i.get("citations") or ([i["citation"]] if i.get("citation") else [])
        for _c in _cites:
            if _c and _c.get("citation"):
                _label = ("依据" if str(_c["citation"]).startswith("外部检索")
                          else "法条依据")
                lines.append(f"- {_label}：{_md_safe(_c['citation'])}")
        lines.append(f"- 工具：{i['tool']}")
        lines.append(f"- 建议：{_md_safe(i['suggestion'])}")
        fix = i.get("fix") or {}
        if fix:
            lines.append(f"- 怎么改：{_md_safe(fix.get('how') or fix.get('summary') or '需人工复核')}")
            if fix.get("has_rewrite") and fix.get("to"):
                label = "新增条款" if fix.get("action") == "insert" else "改为"
                body = str(fix["to"])
                body = "\n".join("  > " + _md_safe(x) for x in body.splitlines())
                lines.append(f"- {label}：\n{body}")
            elif fix.get("action") == "insert" and fix.get("to"):
                body = "\n".join("  > " + _md_safe(x) for x in str(fix["to"]).splitlines())
                lines.append(f"- 新增条款：\n{body}")
            if fix.get("consequence"):
                lines.append(f"- 不改的后果：{_md_safe(fix['consequence'])}")
    if report["to_confirm"]:
        lines.append("\n## 待人工确认（confidence < 0.7）")
        for i in report["to_confirm"]:
            lines.append(f"- [{i['issue_id']}] {i['rule_id']}：{_md_safe(i['quote'][:40])}…")
    if report["to_fix"]:
        lines.append("\n## 待整改（blocker + major，按严重度）")
        for i in report["to_fix"]:
            lines.append(f"- [{i['issue_id']}] {i['severity']} {i['rule_id']}：{_md_safe(i['suggestion'])}")
    if report["disputes"]:
        lines.append("\n## 交叉复核争议")
        for d in report["disputes"]:
            lines.append(f"- {d['between']}：{d['point']} → {d['resolution_hint']}")
    st = report["stats"]
    lines.append(f"\n## 统计：blocker {st['blocker']} / major {st['major']} / minor {st['minor']} / nit {st['nit']}")
    cov = report.get("coverage") or {}
    if cov:
        dims = cov.get("active_by_dimension") or {}
        dim_text = " / ".join(f"{k} {v}" for k, v in dims.items())
        lines.append("\n## 覆盖度与边界")
        lines.append(
            f"- 事实库断言：匹配 {cov.get('fact_assertions_matched', 0)} / "
            f"{cov.get('fact_assertions_total', 0)} 条（{cov.get('fact_coverage_pct', 0)}%）")
        lines.append(
            f"- 生效规则：{cov.get('active_rules', 0)} / {cov.get('declared_rules', 0)} 条（{dim_text}）")
        lines.append(f"- 审查模式：{cov.get('review_mode', report.get('review_mode', 'rule-only'))}")
        ag = report.get("agent") or {}
        if ag:
            label_of = {d["id"]: d.get("label") or d["id"]
                        for d in (ag.get("dimensions") or [])}
            dims_cn = "、".join(label_of.get(d, d)
                               for d in (ag.get("checked_dimensions") or [])) or "无"
            lines.append(f"- 审查 Agent：{ag.get('name')}（{ag.get('id')} v{ag.get('version')}）"
                         f"｜维度：{dims_cn}"
                         f"｜检查器：{len(ag.get('checks') or [])} 项")
        for item in report.get("limitations") or []:
            lines.append(f"- 局限：{item}")
    inc = report["integrity"]
    lines.append("\n## 完整性哈希")
    lines.append(f"- target_hash: `{inc['target_hash'][:16]}…`")
    lines.append(f"- ruleset_hash: `{inc['ruleset_hash'][:16]}…`")
    lines.append(f"- rules_dir_hash: `{inc['rules_dir_hash'][:16]}…`")
    if inc.get("agent_dir_hash"):
        lines.append(f"- agent_dir_hash: `{inc['agent_dir_hash'][:16]}…`"
                     f"（{inc.get('agent_version')}）")
    lines.append(f"- knowledge_hash: `{inc['knowledge_hash'][:16]}…`")
    lines.append(f"- report_hash: `{inc['report_hash'][:16]}…`")
    lines.append(f"- logs_hash: `{inc['logs_hash'][:16]}…`")
    lines.append(f"- prev_report_hash: `{inc['prev_report_hash'][:16]}…`")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
RULE_BY_ID = rule_index(rules_store.load_all_rules())


ENGINE_ENV = "REVIEW_ENGINE"
ENGINES = ("legacy", "v1", "shadow")
DEFAULT_ENGINE = "legacy"


def _take_engine(args):
    """从 argv 里取出 `--engine X` / `--engine=X`，缺省读环境变量，再缺省 legacy。

    返回 (engine, 剩余 args)。非法值直接报错退出——静默回退到 legacy 是最坏的结果：
    用户以为跑了新引擎，实际还是旧链路。
    """
    engine = None
    rest = list(args)
    for index, item in enumerate(list(rest)):
        if item == "--engine":
            if index + 1 >= len(rest):
                print(f"--engine 缺少取值。可选：{' / '.join(ENGINES)}")
                sys.exit(2)
            engine = rest[index + 1]
            del rest[index:index + 2]
            break
        if item.startswith("--engine="):
            engine = item.split("=", 1)[1]
            del rest[index]
            break
    if engine is None:
        engine = os.environ.get(ENGINE_ENV, "").strip() or DEFAULT_ENGINE
    engine = str(engine).strip().lower()
    if engine not in ENGINES:
        print(f"未知引擎：{engine!r}；可选：{' / '.join(ENGINES)}")
        sys.exit(2)
    return engine, rest


def _run_shadow(report, target_path, rules, *, type_override=None, agent_id=None,
                purpose=None, purpose_max=None):
    """双跑：旧报告已生成，再跑一遍六节点流水线并比对。

    差异写进 `state/double_run/<review_id>.json`，控制台打印一句话结论。
    这是切流前的刹车：只有连续零未解释差异，才允许把默认链路换成 v1。
    """
    purpose_info = (purpose_mod.parse(str(purpose), max_chars=purpose_max)
                    if purpose and str(purpose).strip() else None)
    integrity_inputs: dict = {}
    state, outcome, session = run_typed_pipeline(
        target_path, rules, review_id=report["review_id"],
        agent_id=agent_id, type_override=type_override,
        store=STATE / "runs", integrity_out=integrity_inputs,
        purpose_info=purpose_info)
    diff = compare_with_report(report, state)
    payload = {
        "review_id": report["review_id"],
        "engine": "shadow",
        "status": outcome.status,
        "errors": list(outcome.errors),
        "diff": diff,
        "state": state_to_dict(state),
    }
    out_dir = STATE / "double_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{report['review_id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # 双跑也留一份影子报告：差异要能对着「新链路自己怎么说」看，而不是只看计数
    _write_shadow_report(state, outcome, integrity_inputs=integrity_inputs,
                         session=session, purpose_info=purpose_info)
    if diff["ok"]:
        print(f"[shadow] {report['review_id']} 双跑一致："
              f"verdict={diff['verdict_new']}，提及折叠 {len(diff['folded'])} 条")
    else:
        print(f"[shadow] {report['review_id']} 双跑存在未解释差异："
              f"unexplained={len(diff['unexplained'])} missing={len(diff['missing'])} "
              f"verdict {diff['verdict_new']} vs {diff['verdict_legacy']}；"
              f"详见 state/double_run/{report['review_id']}.json")
    return diff


def make_llm_review_fn(active, text, target_type, *, use_llm=None, agent=None,
                       purpose_info=None):
    """构造「一次模型调用」的函数，返回 (review_fn | None, meta)。

    None 表示模型不可用（未配置 / 被关闭）；**调用方决定降级还是报错**——
    这里不悄悄降级：静默降级会让用户以为结论里有模型判断。

    口径与旧 `semantic_review` 一致：按维度取生效规则、任务书（含用户目的）、
    事实库进 prompt、quote 对全文校验。
    """
    try:
        cfg = llm_client.load_config(use_llm)
    except Exception as e:                              # noqa: BLE001 - 配置加载失败
        return None, {"available": False, "reason": f"配置加载失败：{e}"}
    if not cfg.available:
        reason = ("已通过 --no-llm 关闭" if use_llm is False
                  else "config 中 enabled=false" if not cfg.enabled
                  else "未配置 API Key 且非本地端点")
        return None, {"available": False, "reason": reason, "model": cfg.model}

    client = llm_client.LLMClient(cfg)
    by_dim = {}
    for rule in active:
        by_dim.setdefault(rule.get("dimension"), []).append(rule)
    charter = build_charter(target_type, agent=agent)
    block = purpose_mod.charter_block(purpose_info)
    if block:
        charter = f"{charter}\n\n{block}"
    facts = retrieve.load_facts()

    def review_fn(step):
        return llm_review.review_dimension(
            client, step.dimension, by_dim.get(step.dimension, []),
            step.text, cfg, charter=charter, full_text=text, facts=facts)

    return review_fn, {"available": True, "model": cfg.model,
                       "source": cfg.source, "base_url": cfg.base_url}


def _run_v1(target_path, rules, *, type_override=None, agent_id=None,
            use_llm=False, purpose=None, purpose_max=None):
    """只跑六节点流水线（影子）：**不写正式报告**，只落状态并打印摘要。

    为什么不写报告：v1 还没有接报告写出（哈希链 / ledger / limitations）。
    此时往 `outputs/` 写一份同名报告，等于让用户看到一份「看起来一样、实际少一半」的东西。
    """
    purpose_info = None
    if purpose and str(purpose).strip():
        purpose_info = purpose_mod.parse(str(purpose), max_chars=purpose_max)
    llm_meta: dict = {}
    integrity_inputs: dict = {}
    try:
        state, outcome, session = run_typed_pipeline(
            target_path, rules, agent_id=agent_id, type_override=type_override,
            store=STATE / "runs", use_llm=use_llm, llm_meta_out=llm_meta,
            purpose_info=purpose_info, integrity_out=integrity_inputs)
    except ConfigError as e:
        print(f"[v1] {e}")
        _emit_domain_error(e)
        sys.exit(2)
    if not outcome.ok:
        print(f"[v1] 流水线未完成：{outcome.status}；{'；'.join(outcome.errors)[:300]}")
        _emit_error("；".join(outcome.errors) or "流水线未完成", kind="system",
                    http_status=500)
        sys.exit(1)
    if use_llm is False:
        print("[v1] 语义审：已通过 --no-llm 关闭（本次为纯规则审）")
    elif llm_meta.get("available"):
        print(f"[v1] 语义审：model={llm_meta.get('model')}"
              f"（{llm_meta.get('source') or '未知来源'}）")
    else:
        print(f"[v1] 语义审未启用：{llm_meta.get('reason') or '未配置'}"
              "（本次为纯规则审）")
    verdict = state.decision.verdict.value if state.decision else "-"
    report_path = _write_shadow_report(state, outcome,
                                       integrity_inputs=integrity_inputs,
                                       session=session,
                                       purpose_info=purpose_info)
    try:
        shown = report_path.relative_to(ROOT).as_posix()
    except ValueError:
        # 测试会把 OUTPUTS 重定向到临时目录：显示绝对路径，不因为「不在仓库里」而崩
        shown = report_path.as_posix()
    print(f"[v1] {state.review_id} 流水线完成：verdict={verdict} | "
          f"发现 {len(state.findings)} 条 | 证据 {len(state.evidence)} 条 | "
          f"影子报告：{shown} | "
          f"状态：state/runs/{state.run_id}.state.json")
    return state


def _integrity_inputs(target_path, rules, active, agent):
    """算一次完整性指纹：目标 / 规则集 / Agent / 知识库 + 各自版本号。

    与旧报告的 `integrity` 块同口径（同一批 integrity.* 函数），
    这样 v1 报告与历史报告可以用同一套工具验证「规则集变没变」。
    """
    return {
        "target_hash": integrity.sha256_file(str(target_path)),
        "ruleset_hash": integrity.sha256_bytes(
            integrity.serialize_active_rules(active).encode("utf-8")),
        "rules_dir_hash": integrity.sha256_dir(str(RULES)),
        "rules_version": integrity.rules_version(),
        "agent_dir_hash": (integrity.sha256_dir(str(agent.dir))
                           if agent.dir.is_dir() else None),
        "agent_version": agent.version,
        "knowledge_hash": integrity.sha256_dir(str(KNOWLEDGE)),
        "knowledge_version": integrity.knowledge_version(),
    }


def _write_v1_logs(session, outcome, review_id):
    """把审计事件流写成 `logs/v1/<review_id>/`，返回目录哈希。

    旧报告会对 `logs/<review_id>/` 取哈希进 `integrity.logs_hash`。v1 之前没有这一层，
    报告只能证明「结论没被改过」，不能证明「过程也没被改过」。这里补上，
    而且放在**新命名空间** `logs/v1/`，不占正式日志目录。

    四套记录（节点事件 / 工具 / 决策 / 检查点）先汇成**一条** `AuditEvent` 流写进
    `events.jsonl`；旧的 `task.json` / `tools.jsonl` / `decisions.jsonl` 由这条流
    **投影**生成，格式逐字不变（UI 与历史脚本还在读它们）。
    事实来源只有一份，但消费者不用改——这是切流前能做到的最小改动。
    """
    log_dir = LOGS / "v1" / review_id
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = build_audit_stream(
        run_id=outcome.state.run_id,
        node_events=outcome.events,
        tool_logs=session.tool_logs,
        decisions=session.decisions,
        checkpoints=outcome.checkpoints,
    )
    write_audit_stream(log_dir / "events.jsonl", stream)
    legacy = audit_to_legacy(stream)
    for name, items in (("decisions.jsonl", legacy["decisions"]),
                        ("tools.jsonl", legacy["tools"])):
        log_dir.joinpath(name).write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
            encoding="utf-8")
    log_dir.joinpath("task.json").write_text(json.dumps({
        "review_id": review_id,
        "engine": "architecture-v1",
        "final_status": outcome.status,
        "errors": list(outcome.errors),
        "steps": legacy["steps"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return integrity.sha256_dir(str(log_dir))


def _attach_purpose(report, purpose_info, text):
    """把「审查目的 → 目标 → 原文证据」接进影子报告。

    直接复用 `purpose_mod.analyze()`：同一套意图词典、同一套证据定位规则。
    `judge=None`：v1 暂不做模型语义裁决（那要额外一次调用），但确定性部分
    （目标定位、覆盖、未找到说明）已经完整——这正是"目的比对"最不可替代的部分。
    """
    if not purpose_info:
        return report
    block = purpose_mod.analyze(text or "", purpose_info, report["issues"])
    if block:
        report["purpose"] = block
        not_found = sum(1 for g in block.get("goals") or []
                        if g.get("status") == "not_found")
        if not_found:
            report["limitations"].append(
                f"目的比对：{not_found} 个目标未在原文找到证据（未找到 ≠ 不存在）")
    return report


def _attach_action_plan(report):
    """给影子报告的每条发现挂整改建议（改哪句 / 怎么改 / 不改会怎样）。

    整改单不是「再加一句建议」：它要能让人照着动手。所以直接复用旧链路的
    `remediation.attach()`——同一份规则表、同一套改写模板，避免两套口径。
    必须在算 `report_hash` 之前调用：报告哈希要覆盖整改建议本身。
    """
    try:
        assertions = retrieve.load_facts().get("assertions", [])
    except Exception:                                  # noqa: BLE001 - 事实库坏了不该拖垮报告
        assertions = []
    report["action_plan"] = remediation.attach(report["issues"], assertions)
    for issue in report["issues"]:
        fix = issue.get("fix") or {}
        if fix.get("consequence"):
            issue["consequence"] = fix["consequence"]
    return report


def _report_already_written(json_path, run_id):
    """幂等键 `(run_id, stage=finalize)`：同一次运行只写一份报告、只进一次链。

    为什么按 `run_id` 而不是按内容：同一次执行的结论是确定的（节点必须确定性），
    所以「这个 run_id 已经写过报告」就足以判定重复。按内容比对反而危险——
    内容里含 audit / logs_hash 这类每次都会变的东西，永远比不相等。

    读不出来的坏文件不算「已经写过」：宁可重写一份，也不拿坏文件当既成事实。
    """
    if not run_id or not json_path.exists():
        return False
    try:
        existing = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return existing.get("run_id") == run_id


def _write_shadow_report(state, outcome, *, integrity_inputs=None, session=None,
                         purpose_info=None):
    """把影子报告写到 `outputs/v1/`，并给它补上可校验的完整性链。

    为什么 v1 也要哈希链：报告是交付物，读者必须能验证「这份结论没被改过」。
    为什么单独一条链（`ledger/v1-chain.log`）：v1 报告在 `outputs/v1/`，
    混进官方 `ledger/chain.log` 会让 `integrity verify` 找不到对应报告文件。
    切流时两条链合并（或 v1 链转正），那是发布动作，不能顺手做。

    幂等：同一次运行（相同 `run_id`）第二次调用直接返回已有路径——
    不重写报告、不重写日志、不往链上追加第二条记录。否则「恢复后重跑一次」
    会在审计链里留下两条同一份结论的记录，链就没法用来证明「跑了几次」。
    """
    out_dir = OUTPUTS / "v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{state.review_id}.json"
    if _report_already_written(json_path, state.run_id):
        return json_path

    logs_hash = (_write_v1_logs(session, outcome, state.review_id)
                 if session is not None else None)
    report = build_shadow_report(
        state,
        target_meta=state.artifacts.get("target_meta"),
        events=outcome.events,
        checkpoints=outcome.checkpoints,
        integrity_inputs=integrity_inputs,
        logs_hash=logs_hash,
    )
    _attach_purpose(report, purpose_info, state.artifacts.get("target_text"))
    _attach_action_plan(report)      # 必须早于 report_hash：哈希要覆盖整改建议
    ledger_path = LEDGER / "v1-chain.log"
    lock = _acquire_ledger_lock()
    try:
        prev, seq_no = "0" * 64, 1
        if ledger_path.exists():
            lines = [l for l in ledger_path.read_text(encoding="utf-8").splitlines()
                     if l.strip()]
            if lines:
                prev = json.loads(lines[-1]).get("report_hash", "0" * 64)
                seq_no = len(lines) + 1
        report["integrity"]["prev_report_hash"] = prev
        report["integrity"]["report_hash"] = integrity.compute_report_hash(report)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "seq": seq_no,
                "review_id": report["review_id"],
                "engine": report["engine"],
                "target_hash": report["integrity"]["target_hash"],
                "report_hash": report["integrity"]["report_hash"],
                "prev_hash": prev,
                "rules_dir_hash": report["integrity"]["rules_dir_hash"],
                "rules_version": report["integrity"]["rules_version"],
                "ts": now_iso(),
            }, ensure_ascii=False) + "\n")
            f.flush()
    finally:
        _release_ledger_lock(lock)

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    (out_dir / f"{report['review_id']}.md").write_text(
        render_shadow_md(report), encoding="utf-8")
    return json_path


# ---------------------------------------------------------------------------
# Task / Report 服务（阶段 2 收尾）
# ---------------------------------------------------------------------------
def _allocate_task_review_id(day):
    """给 TaskService 用的号分配：拿到号就立刻释放锁文件。

    与 `run_typed_pipeline` 内的做法一致——锁只保护「分配」这一瞬间，
    不跨越整个执行过程（跨越会让一次崩溃把当天的号锁死一小时）。

    注意：`ReviewIdAllocator` 端口约定入参是 `datetime.date`（见 domain/ports.py），
    而旧 `allocate_review_id` 吃的是 `"YYYYMMDD"` 字符串。适配放在这里，
    不把「日期怎么格式化」扩散到任务层——否则两处各写一遍，迟早对不上
    （实测第一版就是漏了这步，产出了 `R-2026-10-31-001` 这种非法 ID）。
    """
    key = day.strftime("%Y%m%d") if hasattr(day, "strftime") else str(day)
    review_id, lock = allocate_review_id(key)
    try:
        lock.unlink()
    except OSError:
        pass
    _ID_LOCKS.discard(lock)
    return review_id


def _path_for_record(path: Path) -> str:
    """记录里存相对仓库根的正斜杠路径：换盘符、换目录都还能对上。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def make_task_runner(*, rules=None, type_override=None, agent_id=None):
    """把六节点流水线包成 TaskService 的 runner（不打印、不 sys.exit）。

    与 `_run_v1` 的分工：`_run_v1` 是「人跑一次」的入口（打印摘要、错误码），
    这里只返回结果。失败以 `TaskOutcome(status=FAILED)` 返回，由任务层记账。
    """

    def runner(target, *, review_id, profile_id=None, purpose=None,
               engine="v1", use_llm=False):
        # profile 目前就是「用哪个 Agent / 规则包」：v1 的 ReviewProfile 由 agent 派生，
        # 所以 profile_id 直接当 agent 选择器用；传空则走既有的扩展名自动路由。
        std_rules = rules if rules is not None else load_rules()
        purpose_info = (purpose_mod.parse(str(purpose))
                        if purpose and str(purpose).strip() else None)
        integrity_inputs: dict = {}
        state, outcome, session = run_typed_pipeline(
            target, std_rules, review_id=review_id,
            agent_id=profile_id or agent_id, type_override=type_override,
            store=STATE / "runs", use_llm=use_llm,
            purpose_info=purpose_info, integrity_out=integrity_inputs)
        if not outcome.ok:
            return TaskOutcome(
                status=RunStatus.FAILED, stage=state.stage,
                error="；".join(outcome.errors) or f"流水线未完成：{outcome.status}")
        report_path = _write_shadow_report(
            state, outcome, integrity_inputs=integrity_inputs,
            session=session, purpose_info=purpose_info)
        return TaskOutcome(
            status=RunStatus.COMPLETED, stage=state.stage,
            verdict=state.decision.verdict.value if state.decision else None,
            findings=len(state.findings), evidence=len(state.evidence),
            report_path=_path_for_record(report_path),
        )

    return runner


def make_task_service(*, rules=None, type_override=None, agent_id=None) -> TaskService:
    """装配任务服务：执行走六节点流水线，记录落 `state/tasks/`。

    `STATE` 在调用时才读：测试会把模块级目录重定向到临时目录，
    如果在导入时算好路径，重定向就失效（`test_engine_switch` 用的是同一套做法）。
    """
    return TaskService(
        runner=make_task_runner(rules=rules, type_override=type_override,
                                agent_id=agent_id),
        store=FileTaskStore(STATE / "tasks"),
        allocate=_allocate_task_review_id,
        root=ROOT,
    )


def make_report_service() -> ReportService:
    """装配报告服务：v1 影子报告优先，其次旧链路正式报告。"""
    return ReportService(
        {"v1": OUTPUTS / "v1", "legacy": OUTPUTS},
        compute_hash=integrity.compute_report_hash,
    )


def main():
    args = sys.argv[1:]
    use_llm = None
    progress_path = None
    type_override = None
    agent_id = None
    purpose = None
    purpose_max = None
    purpose_notes = None
    scenario = None
    research_enabled = False
    # 引擎选择：默认旧链路；v1 = 只跑六节点流水线（影子）；shadow = 两条都跑并比对。
    engine, args = _take_engine(args)
    if engine == "off":
        return
    if "--scenario" in args:
        i = args.index("--scenario")
        if i + 1 < len(args):
            scenario = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            try:
                opts = "、".join(s["id"] for s in scenario_mod.load()["scenarios"])
            except scenario_mod.ScenarioError as e:
                opts = f"（情境配置读取失败：{e}）"
            print(f"--scenario 缺少取值。可用：{opts}")
            sys.exit(2)
    if "--list-scenarios" in args:
        data = scenario_mod.load()
        print(f"可用审查情境（默认 {data['default']}，配置版本 {data['version']}）：")
        for s in data["scenarios"]:
            print(f"  {s['id']:<20}{s['name']}")
            print(f"      关闭规则：{'、'.join(s['disable_rules']) or '无'}"
                  + (f"｜关闭维度：{'、'.join(s['disable_dimensions'])}"
                     if s["disable_dimensions"] else ""))
            if s["keep_note"]:
                print(f"      仍然检查：{s['keep_note']}")
        return
    if scenario and scenario_mod.get(scenario) is None:
        try:
            opts = "、".join(s["id"] for s in scenario_mod.load()["scenarios"])
        except scenario_mod.ScenarioError as e:
            print(f"审查情境错误：{e}")
            sys.exit(2)
        print(f"未知审查情境：{scenario}；可选：{opts}")
        sys.exit(2)
    format_spec = None
    if "--format-spec" in args:
        i = args.index("--format-spec")
        if i + 1 < len(args):
            format_spec = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            try:
                opts = "、".join(s["id"] for s in format_spec_mod.load()["specs"])
            except format_spec_mod.FormatSpecError as e:
                opts = f"（格式规格配置读取失败：{e}）"
            print(f"--format-spec 缺少取值。可用：{opts}")
            sys.exit(2)
    if "--list-format-specs" in args:
        data = format_spec_mod.load()
        print(f"可用格式规格（默认 {data['default']}，配置版本 {data['version']}）：")
        for s in data["specs"]:
            print(f"  {s['id']:<24}{s['name']}")
            print(f"      判定项：{'、'.join(sorted(s['check'])) or '无'}")
            if s["source"]:
                print(f"      来源：{s['source']}")
            if s["disable_rules"]:
                print(f"      不参与：{'、'.join(s['disable_rules'])}")
        return
    if format_spec and format_spec_mod.get(format_spec) is None:
        try:
            opts = "、".join(s["id"] for s in format_spec_mod.load()["specs"])
        except format_spec_mod.FormatSpecError as e:
            print(f"格式规格错误：{e}")
            sys.exit(2)
        print(f"未知格式规格：{format_spec}；可选：{opts}")
        sys.exit(2)
    if "--agent" in args:
        i = args.index("--agent")
        if i + 1 < len(args):
            agent_id = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            print("--agent 缺少取值。可用："
                  + "、".join(a["id"] for a in agent_spec.list_agents()))
            sys.exit(2)
    if "--list-agents" in args:
        print("可用审查 Agent（每个 Agent = 一套判定标准）：")
        for a in agent_spec.list_agents():
            if a.get("error"):
                print(f"  {a['id']:<16} [无效] {a['error'][:100]}")
                continue
            print(f"  {a['id']:<16}{a['version']:<8}{a['name']}")
            print(f"      维度：{'、'.join(d['label'] for d in a['dimensions'])}")
            print(f"      规则：{a['rules']} 条｜检查器：{len(a['checks'])} 项｜"
                  f"自动路由：{'是' if a['auto'] else '否（需 --agent 显式指定）'}")
        return
    if "--type" in args:
        i = args.index("--type")
        if i + 1 < len(args):
            type_override = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    valid_types = agent_spec.all_target_types() or list(DIM_FOR_TYPE)
    if type_override and type_override not in valid_types:
        print(f"未知类型：{type_override}，可选：{', '.join(valid_types)}")
        sys.exit(2)
    if "--purpose" in args:
        i = args.index("--purpose")
        if i + 1 < len(args):
            purpose = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            print("--purpose 缺少取值（例：--purpose \"这是给客户的报价邮件，找承诺过头的地方\"）")
            sys.exit(2)
    if "--purpose-max" in args:
        i = args.index("--purpose-max")
        if i + 1 < len(args):
            try:
                purpose_max = int(args[i + 1])
            except ValueError:
                print("--purpose-max 必须是整数（目的文本参与解析的字数上限）")
                sys.exit(2)
            if not 1 <= purpose_max <= 20000:
                print("--purpose-max 需在 1-20000 之间")
                sys.exit(2)
            del args[i:i + 2]
        else:
            del args[i]
            print("--purpose-max 缺少取值（例：--purpose-max 2000，配合上传的目的文件使用）")
            sys.exit(2)
    if "--purpose-notes" in args:
        i = args.index("--purpose-notes")
        if i + 1 < len(args):
            raw = args[i + 1]
            try:
                parsed = json.loads(raw)
            except Exception:
                print("--purpose-notes 必须是 JSON 数组（目的来源的质量提示）")
                sys.exit(2)
            if not isinstance(parsed, list):
                print("--purpose-notes 必须是 JSON 数组（目的来源的质量提示）")
                sys.exit(2)
            purpose_notes = [str(x) for x in parsed]
            del args[i:i + 2]
        else:
            del args[i]
            print("--purpose-notes 缺少取值（JSON 数组）")
            sys.exit(2)
    if "--progress" in args:
        i = args.index("--progress")
        if i + 1 < len(args):
            progress_path = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    if "--no-llm" in args:
        use_llm = False
        args = [a for a in args if a != "--no-llm"]
    if "--no-research" in args:
        # 离线/涉密场景：不联网核验，报告会写明「未做外部事实核验」
        research_enabled = False
        args = [a for a in args if a != "--no-research"]
    if "--research" in args:
        research_enabled = True
        args = [a for a in args if a != "--research"]
    if "--no-gap-log" in args:
        # 缺口清单里会存被审文本的片段。涉密/涉个人信息的材料可以不落这份工单
        # （代价：下次仍然不知道该补什么，报告里 fact_gaps.disabled=True 会写明）。
        globals()["GAP_LOG"] = False
        args = [a for a in args if a != "--no-gap-log"]
    if "--llm" in args:
        use_llm = True
        args = [a for a in args if a != "--llm"]
    if not args or args[0] == "--list":
        print("可用待审对象：")
        paths = sorted(glob.glob(str(TARGETS / "*")))
        paths += sorted(glob.glob(str(TARGETS / "uploads" / "*")))
        for p in paths:
            if os.path.isfile(p):
                print("  ", str(Path(p).relative_to(ROOT)).replace("\\", "/"))
        print("\n可选参数：--llm 强制调用大模型；--no-llm 强制纯规则审；"
              "--type 显式指定对象类型；--agent 指定审查 Agent（--list-agents 查看）；"
              "--scenario 指定审查情境（--list-scenarios 查看：不同用途关掉不同规则）；"
              "--format-spec 指定格式规格（--list-format-specs 查看：docx 版式按哪套标准判，"
              "默认 none 即不判字体字号行距）；"
              "--purpose \"这份文本想干什么\"（按目的派生目标并定位证据，默认解析前 300 字）；"
              "--purpose-max N 目的文本参与解析的字数上限（1-20000，上传目的文件时用）；"
              "--research 对事实库外的数字/日期主张联网检索来源并核验（默认关闭）；"
              "--no-research 强制关闭联网核验；"
              "--engine legacy|v1|shadow 选择执行引擎（legacy 默认；v1 跑六节点流水线、"
              "写影子报告 outputs/v1/ 并落状态，不改旧报告；shadow 出旧报告并双跑比对，"
              "差异写 state/double_run/；shadow 需配合 --no-llm，v1 可带 --llm）；"
              "--no-gap-log 不把知识库缺口写进 state/（缺口里含被审文本片段，涉密材料用）")
        return
    target_arg = args[0]
    target_path = (ROOT / target_arg) if not os.path.isabs(target_arg) else Path(target_arg)
    if not target_path.exists():
        print(f"找不到待审文件：{target_path}")
        _emit_domain_error(NotFoundError(f"找不到待审文件：{target_path}"))
        sys.exit(1)
    rules = load_rules()
    if engine == "shadow" and use_llm is not False:
        # 双跑比对不能拿模型输出比：语义审本身有波动，差异无从判定是 bug 还是随机。
        # 显式拒绝，比产出一堆「未解释差异」强。
        print("--engine shadow 目前只支持纯规则模式：请加 --no-llm")
        _emit_error("--engine shadow 需要 --no-llm", kind="input", http_status=400)
        sys.exit(2)
    if engine == "v1":
        _run_v1(target_path, rules, type_override=type_override,
                agent_id=agent_id, use_llm=use_llm,
                purpose=purpose, purpose_max=purpose_max)
        return
    try:
        report = run_one(target_path, rules, use_llm=use_llm,
                         progress_path=progress_path, type_override=type_override,
                         agent_id=agent_id, purpose=purpose, purpose_max=purpose_max,
                         purpose_notes=purpose_notes, scenario=scenario,
                         format_spec=format_spec,
                         research_enabled=research_enabled)
    except agent_spec.AgentError as e:
        print(f"Agent 配置错误：{e}")
        _emit_domain_error(ConfigError(f"Agent 配置错误：{e}"))
        sys.exit(2)
    except scenario_mod.ScenarioError as e:
        print(f"审查情境错误：{e}")
        _emit_domain_error(ConfigError(f"审查情境错误：{e}"))
        sys.exit(2)
    except format_spec_mod.FormatSpecError as e:
        print(f"格式规格错误：{e}")
        _emit_domain_error(ConfigError(f"格式规格错误：{e}"))
        sys.exit(2)
    except UnreadableTargetError as e:
        print(f"目标不可读：{e}")
        _emit_domain_error(e)
        sys.exit(1)
    if engine == "shadow":
        _run_shadow(report, target_path, rules,
                    type_override=type_override, agent_id=agent_id)
    print(f"[{report['review_id']}] 裁定={report['verdict']} | "
          f"Agent={(report.get('agent') or {}).get('id')} | "
          f"情境={(report.get('scenario') or {}).get('id') or '-'} | "
          f"规格={(report.get('format_spec') or {}).get('id') or '-'} | "
          f"模式={report['review_mode']} | 异议 {len(report['issues'])} 条 | "
          f"输出：outputs/{report['review_id']}.json/.md")


if __name__ == "__main__":
    main()
