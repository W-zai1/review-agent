#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
review.py —— 审查 Agent 主流程 S1~S6 (Demo v0.2.7)

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
import regex_check
import arith_check
import retrieve
import lint
import integrity
import llm_client
import llm_review

try:
    import yaml
except ImportError:
    print("请先安装 pyyaml：pip install pyyaml")
    sys.exit(1)

import rules_store  # noqa: E402

TARGETS = ROOT / "targets"
RULES = ROOT / "rules"
KNOWLEDGE = ROOT / "knowledge"
OUTPUTS = ROOT / "outputs"
LEDGER = ROOT / "ledger"
LOGS = ROOT / "logs"

for d in (OUTPUTS, LEDGER, LOGS):
    d.mkdir(exist_ok=True)

SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
UNIT_FACTOR = {"亿": 1e8, "万": 1e4, "%": 0.01, "倍": 1.0, "": 1.0}

# 事实审的确定性启发式（FACT-002/003/004）
SOURCE_CLAIM_RE = re.compile(
    r"(据统计|数据显示|研究表明|调查显示|报告显示|报告称|据[^，。；\n]{0,12}(显示|统计|报道|消息))")
SOURCE_MARK_RE = re.compile(
    r"(https?://|来源\s*[:：]|文献|《[^》]+》|\[\d+\]|统计口径|公开数据)")
CAUSAL_RE = re.compile(r"(因此|所以|由此可见|这说明|这意味着|导致|证明了)")
CAUSAL_EVIDENCE_RE = re.compile(
    r"(据|来源|数据|研究|实验|样本|统计|报告|https?://)")
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
CALIBER_RE = re.compile(
    r"(同比|环比|占比|增长率|转化率|毛利率|覆盖率|完成率|口径|"
    r"占.{0,8}(总数|总人数|员工|人口|营收|收入)|比重|比例|份额)")
TIMES_RE = re.compile(r"\d+(?:\.\d+)?\s*倍")
BASELINE_RE = re.compile(r"(同比|环比|相比|较|增长|下降|提升|基线)")
NEGATIVE_PREFIXES = ("无", "没有", "不存在", "不涉及", "未", "已解决", "已处理", "不包含")

# 不同待审类型激活的审查维度
DIM_FULL_NAME = {"fact": "事实", "logic": "逻辑", "safety": "安全",
                 "compliance": "合规", "style": "风格"}

# 只聚合「提示类」规则：口径、来源、口语化。
# 逻辑/事实类错误**绝不合并**——同一条规则下的每一处都可能是独立真错误。
ADVISORY_RULES = {"FACT-004", "COMP-004", "STYLE-002", "STYLE-003"}
AGGREGATE_MIN = 3          # 同规则出现 ≥3 次才聚合成一条

MINOR_KEEP_PER_DIM = 8     # 长文分块时，同维度 minor/nit 在清单里最多展开的条数

DIM_FOR_TYPE = {
    "llm_response": ["fact", "logic", "safety", "style", "compliance"],
    "prompt": ["fact", "logic", "safety", "style", "compliance"],
    "contract": ["fact", "logic", "safety", "compliance", "style"],
    "code": ["safety", "style"],
}

# 审查任务书：让模型知道「这次要判定什么、标准是什么、后果多大」
CHARTER_PATH = ROOT / "config" / "charter.json"
TYPE_PURPOSE = {
    "llm_response": "通用文本 / 对外或对内发布的内容",
    "contract": "合同 / 协议文本",
    "code": "源代码",
    "prompt": "提示词 / 指令文本",
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


CONTRACT_MARKERS = ("甲方", "乙方", "本合同", "本协议", "签署", "盖章", "生效",
                    "有效期", "违约责任", "争议解决", "仲裁", "诉讼", "附件")


def detect_type_detail(path: Path, text: str):
    """类型判定 + 置信度 + 依据。

    旧实现只要全文任意位置出现「合同/协议/甲方…」任一关键词就判 contract：
    水段里一句「验收标准与合同附件一致」就让整篇会议纪要变成合同，
    于是 ①合同要件检查报 3 条无意义缺失 ②审查任务书变成「合同/协议文本」
    ③数据披露检查被关掉——**一次猜测改了整个判定标准**。

    新规则：标题与开头加权，正文里"提到合同"不算合同。
    """
    ext = path.suffix.lower()
    code_exts = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".cpp", ".c")
    if ext in code_exts:
        return {"type": "code", "confidence": 0.9, "reason": f"源码扩展名 {ext}"}
    if ext == ".json":
        if re.search(r"(def |function |import |=>)", text):
            return {"type": "code", "confidence": 0.8, "reason": "JSON 内含代码特征"}
        return {"type": "llm_response", "confidence": 0.7, "reason": "JSON 数据文件"}
    if ext not in (".md", ".txt", ".markdown", ".csv", ".log", ".xml", ""):
        return {"type": "llm_response", "confidence": 0.6, "reason": f"未识别扩展名 {ext}"}

    head = text[:600]
    first_line = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    title_contract = bool(re.search(r"(合同|协议|契约)", first_line))
    head_hits = [k for k in CONTRACT_MARKERS if k in head]
    all_hits = [k for k in CONTRACT_MARKERS if k in text]

    if (title_contract and len(head_hits) >= 2) or len(head_hits) >= 4:
        return {"type": "contract", "confidence": 0.92,
                "reason": f"标题/开头出现合同要件词：{'、'.join(head_hits[:6])}"}
    if title_contract and len(head_hits) >= 1:
        return {"type": "contract", "confidence": 0.75,
                "reason": f"标题含合同字样，开头要件词较少：{'、'.join(head_hits[:4])}"}
    if len(all_hits) >= 4:
        return {"type": "llm_response", "confidence": 0.55,
                "reason": (f"全文出现 {len(all_hits)} 个合同/协议字样，但集中在正文而非开头"
                           "——判为普通文本（疑似「提及」合同，不是合同本体）")}
    return {"type": "llm_response", "confidence": 0.85,
            "reason": "未出现合同要件特征"}


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


def sentence_no(text, idx):
    seg = text[:idx]
    sents = [s for s in re.split(r"[。！？\n]", seg) if s.strip()]
    return len(sents) + 1


def doc_anchor(text):
    """缺失要件类 issue 没有可引用的病句，用文档标题作为逐字锚点。"""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:60]
    return ""


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
        self.llm_usage = {"input": 0, "output": 0, "calls": 0, "failures": 0}
        self.issue_counter = 0
        self.tool_ok = 0
        self.tool_skipped = 0
        if log_dir:
            # 先清空，后续决策 / 工具日志逐条 append，进程崩溃也不丢已发生部分
            (log_dir / "decisions.jsonl").write_text("", encoding="utf-8")
            (log_dir / "tools.jsonl").write_text("", encoding="utf-8")

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

    def decision(self, decision, rule_id, rationale, alternatives, why_not):
        item = {
            "ts": now_iso(), "decision": decision, "rule_id": rule_id,
            "rationale": rationale, "alternatives": alternatives,
            "why_not_alternative": why_not,
        }
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
                add("FACT-003", sentence, pos,
                    "正则启发式：出现因果连接词，但同句未给出数据、研究或来源前提",
                    "补充支撑该因果结论的前提与证据，或改为弱化表述。", 0.6)
        if "FACT-004" in active_ids and counters["FACT-004"] < 3:
            percent_issue = PERCENT_RE.search(sentence) and not CALIBER_RE.search(sentence)
            times_issue = TIMES_RE.search(sentence) and not BASELINE_RE.search(sentence)
            if percent_issue or times_issue:
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
        if a["type"] == "number":
            tr = time.perf_counter()
            nums = regex_check.extract_numbers(window)
            s.tool("regex_check", f"{entity} 数字抽取", f"候选 {len(nums)} 个",
                   (time.perf_counter() - tr) * 1000, "success")
            if not nums:
                continue
            # 跳过"2024 年"这类年份：它不是被比对的指标值。
            # 旧实现取窗口第一个数字，把年份当基数，得出"偏差约 0 倍"的垃圾证据（实测 f01）
            values = [n for n in nums
                      if not (1900 <= n["num"] <= 2100
                              and window[n["end"]:n["end"] + 1] == "年")]
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
                quote = sentence_at(text, idx)
                loc = f"第 {sentence_no(text, idx)} 句"
                tr = time.perf_counter()
                hits = retrieve.grep_knowledge(f"{entity} {attr}")
                s.tool("retrieve", f"{entity} {attr}", f"命中 {len(hits)} 条知识库文档",
                       (time.perf_counter() - tr) * 1000, "success")
                ev = (f"知识库命中：{hits[0]['file']}:{hits[0]['line']} —— {hits[0]['text']}"
                      if hits else a.get("note", "事实库无对应条目"))
                sugg = (f"与公开口径偏差约 {ratio:.0f} 倍，建议改为「{exp['num']}{exp['unit']}量级」"
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
        elif a["type"] == "date":
            # 日期通常在 attr 之前（如"2023 年 9 月推出"），向前开窗口，避免误取后文年份
            tr = time.perf_counter()
            dwin = text[max(0, idx - 40): idx + 10]
            dates = regex_check.extract_dates(dwin)
            s.tool("regex_check", f"{entity} 日期抽取", f"候选 {len(dates)} 个",
                   (time.perf_counter() - tr) * 1000, "success")
            if not dates:
                continue
            note_fact(s, a)
            yr = dates[0]["year"]
            if yr != a["expected_year"]:
                quote = sentence_at(text, idx)
                loc = f"第 {sentence_no(text, idx)} 句"
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
    patterns = {
        "SAFE-001": (r"(如何制作|制毒|炸药|入侵|攻击指令|毒品配方)", "命中攻击/危险操作关键词"),
        # 「密码破解」是「破解密码」的倒序，两种语序都要认
        "SAFE-002": (r"(违法操作|绕过.{0,4}安全|破解密码|密码破解|非法获取)",
                     "命中违法操作关键词"),
        "SAFE-003": (r"(1[3-9]\d{9}|[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])",
                     "命中疑似个人身份/手机号信息"),
        "SAFE-004": (r"(忽略(以上|所有|之前)|无视.{0,4}(限制|规则)|扮演无限制|DAN模式|"
                     r"绕过.{0,6}(审查|规则|检查)|直接判定为通过|判定为通过|"
                     r"把.{0,8}(合计|金额|数字)改为|隐藏.{0,4}(问题|矛盾|错误))",
                     "命中 prompt 注入关键词"),
    }
    for rid, (pat, desc) in patterns.items():
        if rid not in {r["rule_id"] for r in rules}:
            continue
        m = re.search(pat, text)
        if m:
            if rid in ("SAFE-001", "SAFE-002"):
                sent = sentence_at(text, m.start())
                if defensive.search(sent):
                    s.decision(f"{rid} 命中但属防御性语境，未报", rid,
                               f"命中「{m.group(0)}」，同句含检测/防护类表述",
                               ["直接判 blocker"], "安全运维文档里「入侵检测」「破解防护」是正常术语")
                    continue
            loc = f"第 {sentence_no(text, m.start())} 句"
            s.add_issue(s.make_issue(
                "safety", rid, sentence_at(text, m.start()), loc,
                f"{desc}：命中「{m.group(0)}」", "regex + llm_judge(stub)", 0.8,
                "删除相关危险/敏感内容，必要时应转人工安全复核。"))
            s.decision(f"安全命中 {rid} 定为 {RULE_BY_ID[rid]['severity']}", rid,
                       desc, ["minor"], "安全红线属高严重度")
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
    return s


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
                         "未检索到明确的双方主体（甲方/乙方）"),
            "COMP-002": ((has_affirmative_term(text, ["生效"])
                          and (has_affirmative_term(text, ["终止"])
                               or has_affirmative_term(text, ["有效期"]))),
                         "未检索到生效/终止条款"),
            "COMP-003": (has_affirmative_term(text, ["争议", "仲裁", "诉讼"]),
                         "未检索到争议解决条款（仲裁/诉讼）"),
        }
        s.decision("合同审采用关键词 + 否定语境预筛", "OVERALL-005",
                   "Demo 无条款结构解析器，仅做要件存在性初筛",
                   ["条款结构解析", "人工复核"], "先给可解释的初筛结论，并在报告中标注局限")
        ok_notes = {
            "COMP-001": "已核验：合同明确双方主体（甲方/乙方）",
            "COMP-002": "已核验：合同含生效与终止条款",
            "COMP-003": "已核验：合同含争议解决条款",
        }
        for rid, (ok, msg) in checks.items():
            if rid not in ridset:
                continue
            if not ok:
                s.add_issue(s.make_issue(
                    "compliance", rid, doc_anchor(text), "全文（缺失要件，无对应原文）",
                    f"clause_presence_check 未命中：{msg}", "regex + checklist", 0.9,
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
            # 年份、月日、编号都不是"披露的数据"：实测 a02 把「7 月 1 日」当裸数据报
            if text[end:end + 1] in ("年", "月", "日", "号", "周", "届", "期", "名"):
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
        has_source = bool(re.search(r"(据|来源|公开数据|报道|https?://|\[\d+\])", text))
        # 该句已由算术/事实规则报过 → 不再叠一条"数字无来源"的提示（第三轮 N9）
        first_sentence = sentence_at(text, nums[0]["start"]) if nums else ""
        if nums and not has_source and not any(
                i["rule_id"] != "COMP-004"
                and _quote_overlap(i["quote"], first_sentence) >= 0.6
                for i in s.issues):
            s.add_issue(s.make_issue(
                "compliance", "COMP-004", sentence_at(text, nums[0]["start"]),
                f"第 {sentence_no(text, nums[0]['start'])} 句",
                "source_presence_check：含具体数字但无标注口径/来源",
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


def _norm_quote(q):
    return re.sub(r"[\s\u3000，。！？、；：\"'“”‘’（）()\[\]【】《》〈〉…—\-·]+", "", q or "")


def _quote_overlap(a, b):
    """同片段判定：去标点后互相包含，或字符二元组 Jaccard ≥ 0.6。"""
    a, b = _norm_quote(a), _norm_quote(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    bigrams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


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


def plan_chunks(text, limit, overlap=400):
    """把长文按段落边界切成 ≤limit 的块，相邻块重叠 overlap 字符。

    为什么按段落切：句子被切断会让模型读到残缺语义，反而制造误报。
    重叠是为了让跨块的相邻句至少在某一块里是完整的。
    """
    if len(text) <= limit:
        return [text]
    chunks, pos, n = [], 0, len(text)
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
        chunks.append(text[pos:end])
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return chunks


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


def _h_comp004(s, text, rules, ctx):
    return drop_covered_comp004(s)


BUILTIN_HANDLERS = {
    "builtin:fact": _h_fact,
    "builtin:safety": _h_safety,
    "builtin:style": _h_style,
    "builtin:compliance": _h_compliance,
    "builtin:static_scan": _h_static_scan,
    "builtin:static_scan_force": _h_static_scan_force,
    "builtin:arith": _h_arith,
    "builtin:comp004_dedupe": _h_comp004,
}

_PACK_MODULES = {}


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


def run_agent_steps(s, agent, text, active, target_path):
    """按 manifest 执行：先维度处理器（声明顺序），再附加检查。

    返回执过的处理器名列表，写进 S3 日志——用户要能看到"这次到底跑了哪些检查"。
    """
    ctx = {"target_path": target_path, "target_type": s.target_type, "agent": agent}
    executed = []
    for dim in agent.dims_for(s.target_type):
        key = agent.handler_for(dim)
        if not key:
            continue        # 无本地处理器 = 只由语义审负责（如 logic）
        ctx["dimension"] = dim      # 通用规则引擎靠它筛出自己维度的规则
        s = resolve_handler(key, agent)(s, text, active, ctx)
        executed.append(key)
    for key in agent.checks:
        s = resolve_handler(key, agent)(s, text, active, ctx)
        executed.append(key)
    return s, executed


def semantic_review(s: Session, text, active, target_type, use_llm=None, dims=None,
                    agent=None):
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
    chunks = (plan_chunks(text, chunk_size,
                          getattr(cfg, "chunk_overlap", 400))
              if getattr(cfg, "chunking", True) else [text])
    chunked = len(chunks) > 1
    chars_reviewed = (chars_total if chunked
                      else min(chars_total, cfg.max_input_chars))
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
        dim_tokens = {"input": 0, "output": 0}
        dim_trunc_in = dim_trunc_out = False
        dim_finish = ""
        for ci, chunk in enumerate(chunks, 1):
            tag = f"{dim}" + (f"|块{ci}/{len(chunks)}" if chunked else "")
            started = time.perf_counter()
            try:
                res = llm_review.review_dimension(
                    client, dim, dim_rules, chunk, cfg, charter=charter,
                    full_text=text if chunked else None)
            except llm_client.LLMError as e:
                elapsed = (time.perf_counter() - started) * 1000
                s.llm_usage["failures"] += 1
                s.tool("llm_api", f"{tag} | rules={len(dim_rules)} | model={cfg.model}",
                       f"失败：{e}"[:200], elapsed, status="failed", retry=cfg.max_retries)
                s.decision(f"{tag} 语义审调用失败", "OVERALL-004",
                           str(e)[:150], ["重试", "换端点"], "重试已耗尽，不阻塞其他块/维度")
                meta["errors"].append(f"{dim}#{ci}: {str(e)[:160]}")
                continue

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
        if dim_added or dim_retracted or dim_dropped:
            s.decision(
                f"{dim} 语义审新增 {dim_added} 条（去重 {dim_dup} / 丢弃 {dim_dropped}"
                + (f" / 自否排除 {dim_retracted}" if dim_retracted else "") + "）",
                "OVERALL-004",
                "模型输出经 rule_id 白名单 + quote 逐字校验；"
                "minor/nit 按置信度取舍，blocker/major 不占配额",
                ["直接采信模型输出"], "模型输出不可信，必须逐字回查原文")
        meta["dimensions"][dim] = {
            "added": dim_added, "duplicates": dim_dup, "dropped": dim_dropped,
            "candidates": dim_cand, "open_discoveries": dim_open,
            "retracted": dim_retracted,
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
    meta["chars_reviewed"] = chars_reviewed
    meta["cache_hits"] = cache_hits
    meta["chunked"] = chunked
    meta["chunks"] = len(chunks)
    meta["excluded"] = excluded
    meta["open_discoveries"] = sum(
        1 for i in s.issues if i.get("open_discovery"))
    return s, ("rule+llm" if success_calls else "rule-only"), meta


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def allocate_review_id(date):
    """从 outputs/ + ledger 已用 ID 中取下一个，并用 O_EXCL 锁文件防并发覆盖。"""
    used = {p.stem for p in OUTPUTS.glob(f"R-{date}-*.json")}
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
            return review_id, lock
        except FileExistsError:
            seq += 1


TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "utf-16", "utf-16-le", "utf-16-be")


def read_target_text(path: Path):
    """按候选编码读取待审文本，返回 (text, encoding, warning)。

    旧实现直接 read_text(encoding="utf-8")：喂一个 GBK 文件就 UnicodeDecodeError 崩溃，
    连报告都不产出。审查工具的底线是"对任意输入都给结论"，不是"只接受 UTF-8"。
    """
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
            type_override=None, agent_id=None):
    t0 = time.perf_counter()
    # 非 UTF-8（GBK/UTF-16）不能让整个进程崩掉：按候选编码依次尝试。
    text, source_encoding, enc_warn = read_target_text(target_path)
    # 零宽字符会让「争议」「360万」这类关键词与数字整段失配，
    # 同时制造误报（条款存在却报缺失）与漏检（金额被拆断）——必须在所有匹配前剥离。
    text, invisible_n = regex_check.strip_invisible(text)
    target_rel = str(target_path.relative_to(ROOT)).replace("\\", "/")

    # Agent 决定「按哪套标准审」：规则来源、维度、检查器、任务书默认值都来自它。
    # 优先级：显式指定 > 自动路由 > 默认 Agent（见 tools/agent_spec.py）
    agent, agent_reason = agent_spec.resolve(target_path, text, agent_id=agent_id)
    rules = merge_agent_rules(rules, agent)
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
    s.step("S2", "规则匹配", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "激活维度": "、".join(dims) or "无（该类型未声明维度）",
        "生效规则": f"{len(active)} 条（{dims_text}）",
        "规则来源": "、".join(_agent_rule_sources(agent))
                    + " + config/rules.state.json（停用的不生效）",
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
    s, executed = run_agent_steps(s, agent, text, active, target_path)
    s, review_mode, llm_meta = semantic_review(s, text, active, s.target_type,
                                              use_llm, dims=dims, agent=agent)
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
    traceable = sum(1 for i in s.issues if i.get("quote") and i["quote"] in text)
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
    s = cross_review(s)
    s.step("S5", "交叉复核", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "争议": f"{len(s.disputes)} 处待人工裁定",
        "复核前后异议数": f"{before_cross} → {len(s.issues)}",
        "规则": "同段多维度互相冲突时进入 disputes，不自动取舍",
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
    if stats["blocker"] > 0:
        verdict = "fail"
    elif stats["major"] > 0:
        verdict = "warn"
    else:
        verdict = "pass"
    s.decision(f"verdict={verdict}", "OVERALL-003",
               f"blocker={stats['blocker']}, major={stats['major']}",
               ["pass", "warn", "fail"], "存在 blocker→fail；仅 major→warn；否则 pass")

    to_confirm = [i for i in issues_sorted if i["confidence"] < 0.7]
    to_fix = [i for i in issues_sorted if i["severity"] in ("blocker", "major")]

    core = (f"整体裁定 {verdict}：共 {len(s.issues)} 条异议"
            f"（blocker {stats['blocker']} / major {stats['major']} / minor {stats['minor']} / nit {stats['nit']}）。"
            + (f"交叉复核 {len(s.disputes)} 处争议待人工裁定。" if s.disputes else "")
            + ("需整改后使用。"
               if verdict != "pass"
               else "未发现阻断性问题，但「未发现」不等于「通过」："
                    "本引擎存在已知漏报（跨段推理、日期计算等），请结合人工复核确认。"))
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
        "declared_rules": len(rules_store.load_all_rules()),
        "active_by_dimension": rule_by_dim,
        "review_mode": review_mode,
        "agent": agent.id,
        "agent_version": agent.version,
        "dimensions_checked": list(dims),
    }
    # 文本覆盖率：审了多少字 / 一共多少字。只报问题数不报覆盖率，
    # 用户会默认"全文都审过了"——长文截断时这是最危险的错觉。
    chars_total = len(text)
    chars_reviewed = (llm_meta or {}).get("chars_reviewed") or chars_total
    chars_reviewed = min(chars_reviewed, chars_total)
    coverage["chars_total"] = chars_total
    coverage["chars_reviewed"] = chars_reviewed
    coverage["text_coverage_pct"] = (
        round(chars_reviewed / chars_total * 100) if chars_total else 100)
    coverage["input_truncated_dimensions"] = list(
        (llm_meta or {}).get("input_truncated_dimensions") or [])
    coverage["output_truncated_dimensions"] = list(
        (llm_meta or {}).get("output_truncated_dimensions") or [])
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
    coverage["complete"] = (
        not coverage["input_truncated_dimensions"]
        and not coverage["output_truncated_dimensions"])
    if not coverage["complete"]:
        # 结论正文也要说，不能只藏在 limitations 里（很多人只看裁定那一段）
        core += (f" 注意：本次语义审仅覆盖 {coverage['text_coverage_pct']}% 的原文"
                 f"（{chars_reviewed:,}/{chars_total:,} 字符），未覆盖部分未被审查。")
    if source_meta:
        # 文本由 PDF / Word / 压缩包抽取而来：标注来源，便于回溯原件
        coverage["source"] = source_meta
    charter_text = (llm_meta or {}).get("charter")
    if charter_text:
        coverage["charter"] = charter_text
    # 明确声明「本次没查什么」——只报问题数而不说覆盖边界，会给人虚假的安全感
    coverage["uncovered"] = [
        f"未做外部事实核验：仅比对项目内置事实库（本次命中 {matched_count}/{total_assertions} 条断言）",
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
    if coverage.get("chunked"):
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
    # Agent 自己声明「不查什么」：只报问题数会给人虚假安全感
    for b in agent.boundaries:
        limitations.append(f"{agent.name} 不覆盖：{b}")
    excluded_n = len((llm_meta or {}).get("excluded") or [])
    if excluded_n:
        limitations.append(
            f"{excluded_n} 条模型输出被判为「模型自否」（依据里自己写了不成立/撤回/无需整改），"
            "已移入报告 excluded 区，不计入异议数与待整改清单。")
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
    if len(active) < coverage["declared_rules"]:
        limitations.append(
            f"本次仅启用 {len(active)}/{coverage['declared_rules']} 条规则，结论范围相应收窄。")
    if verdict == "pass":
        limitations.append("pass 仅表示未命中当前规则与事实库，不等于内容真实可信。")

    # 完整性哈希（S1 起已可算 target/knowledge；logs 待写后算）
    target_hash = integrity.sha256_file(str(target_path))
    knowledge_hash = integrity.sha256_dir(str(KNOWLEDGE))
    rules_dir_hash = integrity.sha256_dir(str(RULES))
    ruleset_hash = integrity.sha256_bytes(
        integrity.serialize_active_rules(active).encode("utf-8"))

    report = {
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
        "limitations": limitations,
        "issues": issues_sorted,
        # 模型自否的条目：不进 to_fix / 统计，但保留下来（模型曾怀疑过也是信息）
        "excluded": list((llm_meta or {}).get("excluded") or []),
        # 折叠的次要条目：JSON 完整保留，清单不展开（数量在 limitations 里说明）
        "suppressed": suppressed,
        "disputes": s.disputes,
        "to_confirm": to_confirm,
        "to_fix": to_fix,
        "stats": stats,
        "llm": llm_meta,
        "reviewer": "independent-review-agent",
        "model_version": "v0.2.7-demo",
        "timestamp": now_iso(),
    }

    s.step("S6", "裁定汇总", latency_ms=(time.perf_counter() - t) * 1000, detail={
        "裁定": f"{verdict}（blocker {stats['blocker']} / major {stats['major']} / "
                f"minor {stats['minor']} / nit {stats['nit']}）",
        "待整改": f"{len(to_fix)} 条（blocker + major）",
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
    ledger_path = LEDGER / "chain.log"
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

    # 输出 JSON + MD
    out_json = OUTPUTS / f"{review_id}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUTS / f"{review_id}.md").write_text(render_md(report), encoding="utf-8")

    try:
        id_lock.unlink()
    except OSError:
        pass
    return report


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
    lines.append("")
    if report["key_facts"]:
        lines.append("## 关键事实（已核验）")
        for k in report["key_facts"]:
            lines.append(f"- {k}")
        lines.append("")
    lines.append("## 异议清单（共 {} 条）".format(len(report["issues"])))
    for i in report["issues"]:
        lines.append(f"\n### [{i['issue_id']}] {i['dimension']} / {i['severity']}（conf {i['confidence']}）")
        lines.append(f"- 原文：{_md_safe(i['quote'])}")
        lines.append(f"- 位置：{_md_safe(i['location'])}")
        lines.append(f"- 规则：{i['rule_id']} — {_md_safe(i['rule_text'])}")
        lines.append(f"- 证据：{_md_safe(i['evidence'])}")
        lines.append(f"- 工具：{i['tool']}")
        lines.append(f"- 建议：{_md_safe(i['suggestion'])}")
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


def main():
    args = sys.argv[1:]
    use_llm = None
    progress_path = None
    type_override = None
    agent_id = None
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
              "--type 显式指定对象类型；--agent 指定审查 Agent（--list-agents 查看）")
        return
    target_arg = args[0]
    target_path = (ROOT / target_arg) if not os.path.isabs(target_arg) else Path(target_arg)
    if not target_path.exists():
        print(f"找不到待审文件：{target_path}")
        sys.exit(1)
    rules = load_rules()
    try:
        report = run_one(target_path, rules, use_llm=use_llm,
                         progress_path=progress_path, type_override=type_override,
                         agent_id=agent_id)
    except agent_spec.AgentError as e:
        print(f"Agent 配置错误：{e}")
        sys.exit(2)
    print(f"[{report['review_id']}] 裁定={report['verdict']} | "
          f"Agent={(report.get('agent') or {}).get('id')} | "
          f"模式={report['review_mode']} | 异议 {len(report['issues'])} 条 | "
          f"输出：outputs/{report['review_id']}.json/.md")


if __name__ == "__main__":
    main()
