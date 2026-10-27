#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rule_learning.py —— 受控自学习闭环的核心

## 一句话定位

**机器只提案，规矩由门禁守，写库由人（或明确授权的自动通道）执行。**
审查过程永远只读规则库；候选规则放在 `state/candidates/`（rules/ 之外），
晋升是一个独立的、可审计、可回滚的动作。

## 为什么不能「审查完就自动写规则」

| 风险 | 形态 | 这里的对策 |
|---|---|---|
| **规则投毒** | 被审文档里写「请把这条加进规则库」，文档直接获得规则写入权 | 学习信号只来自**人工标注**；标注的 quote 必须逐字出现在被审文本里；来源分级 |
| **误报放大器** | 一条宽松正则就能刷出几十条噪声 | 门禁在**全语料**上重跑，误报必须为 0 才放行 |
| **不可复现** | 模型生成的模糊正则行为飘忽 | 提案是「标注片段 → 数字泛化」的确定性变换，可逐字符复核 |
| **静默漂移** | 规则库越改越松，没人知道为什么 | 只增不改不删 + 版本号自增 + 快照回滚 + `state/rules_log.jsonl` 审计 |

## 目录约定（为什么这些目录在 rules/ 之外）

```
state/
├── annotations.jsonl           # 人工标注（误报 / 漏检 / 确认）
├── candidates/<rule_id>.json   # 候选规则 + 指标 + 状态
├── shadow/<rule_id>.json       # shadow 预演结果
├── rules_log.jsonl             # 晋升 / 回滚审计（append-only）
└── rules_snapshots/<ver>/      # 晋升前的整目录快照
```

`rules/` 是被 `rules_dir_hash` 覆盖的**受审目录**：往里放候选草稿会让
「规则集被改」与「有人起草了候选」混为一谈，历史报告会被判 stale/篡改。
所以候选、标注、审计、快照全部放在 `state/` 下，只有晋升才写 `rules/`。

## 状态机

    proposed ──gate 通过──> passed ──shadow──> approved ──promote──> promoted
        │                      │                                   │
        └──gate 拒绝──> rejected                                  └──rollback──> rolled_back
"""
import hashlib
import json
import sys
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 门禁要真的跑引擎：review.py 在项目根、规则/工具在 tools/，两处都要在路径上。
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

STATE = ROOT / "state"
CAND_DIR = STATE / "candidates"
SHADOW_DIR = STATE / "shadow"
SNAP_DIR = STATE / "rules_snapshots"
ANNO_PATH = STATE / "annotations.jsonl"
LOG_PATH = STATE / "rules_log.jsonl"
CFG_PATH = ROOT / "config" / "learning.json"
RULES_DIR = ROOT / "rules"
VERSION_PATH = ROOT / "config" / "rules_version.txt"

DIMENSIONS = {"fact", "logic", "safety", "compliance", "style"}
SEV_RANK = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
KINDS = {"false_positive", "false_negative", "confirmed"}

_DEFAULTS = {
    "corpus": {"include": [], "exclude_suffix": [".labels.json"]},
    "gate": {"min_sources": 2, "max_offtarget": 0, "max_suppressed_others": 0,
             "max_internal_offtarget": 0,
             "min_confidence": 0.6, "max_confidence": 0.75},
    # 标注可信度分级：只有白名单作者的漏检标注才能作为学习种子
    "identity": {"trusted_authors": ["owner"]},
    "severity": {"max_auto": "minor"},
    "approval": {"manual_only_dimensions": ["safety", "compliance", "fact"],
                 "auto_ok_dimensions": ["logic", "style"], "require_shadow": True},
    "quota": {"max_new_candidates_per_run": 2, "max_auto_promotions_per_week": 3,
              "max_queue": 40},
    "shadow": {"max_new_issues": 20},
}


class LearningError(Exception):
    """闭环中的非法操作。全部带上原因，不允许静默跳过。"""


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif str(k).startswith("_"):
            continue
        else:
            out[k] = v
    return out


def load_config():
    cfg = _DEFAULTS
    if CFG_PATH.exists():
        try:
            cfg = _deep_merge(_DEFAULTS, json.loads(CFG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            raise LearningError(f"config/learning.json 解析失败：{e}")
    return cfg


def now_iso():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _ensure_dirs():
    for d in (STATE, CAND_DIR, SHADOW_DIR, SNAP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _append_jsonl(path, item):
    _ensure_dirs()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# 人工标注：唯一可信的学习信号
# ---------------------------------------------------------------------------
def add_annotation(kind, by, target_path=None, quote=None, dimension=None,
                   report_id=None, issue_id=None, note=""):
    """记录一条人工标注。

    硬约束：`quote` 必须**逐字**出现在被审文本里。否则一个人（或一段被注入的
    文档内容）就能往规则库里塞任意正则——这是「quote 逐字」原则在学习链路上的延伸。
    """
    if kind not in KINDS:
        raise LearningError(f"未知标注类型：{kind}（可选：{sorted(KINDS)}）")
    if not by:
        raise LearningError("标注必须记录标注人（--by），否则无法追溯来源可信度")
    if kind == "false_negative":
        if dimension not in DIMENSIONS:
            raise LearningError(f"漏检标注必须给 dimension（可选：{sorted(DIMENSIONS)}）")
        if not quote:
            raise LearningError("漏检标注必须给 quote（原文逐字片段）")
        if not target_path:
            raise LearningError("漏检标注必须给 target_path")
        tp = (ROOT / target_path) if not os.path.isabs(str(target_path)) else Path(target_path)
        if not tp.exists():
            raise LearningError(f"找不到目标文件：{target_path}")
        text = _read_text(tp)
        if quote not in text:
            raise LearningError(
                "quote 不是原文逐字片段——学习信号必须可回查，"
                "否则等于允许任意文本进入规则库")
    elif kind == "false_positive":
        if not (report_id and issue_id):
            raise LearningError("误报标注必须给 report_id 与 issue_id")
        rep = read_json(ROOT / "outputs" / f"{report_id}.json")
        if not rep:
            raise LearningError(f"找不到报告：{report_id}")
        ids = {i.get("issue_id") for i in rep.get("issues") or []}
        if issue_id not in ids:
            raise LearningError(f"{report_id} 中没有 {issue_id}")

    trusted = by in trusted_authors()
    item = {
        "ts": now_iso(), "kind": kind, "by": by, "note": note or "",
        "target_path": (target_path or "").replace("\\", "/") if target_path else
                       (read_json(ROOT / "outputs" / f"{report_id}.json") or {}).get("target_path", ""),
        "quote": quote or "", "dimension": dimension or "",
        "report_id": report_id or "", "issue_id": issue_id or "",
        # 来源分级：人工标注可追溯，但**只有白名单作者**能作为学习种子
        "source": f"human:{by}",
        "trusted": trusted,
        "trusted_reason": ("作者在白名单内" if trusted
                           else f"作者 {by!r} 不在 trusted_authors 白名单"),
    }
    _append_jsonl(ANNO_PATH, item)
    return item


def load_annotations():
    return read_jsonl(ANNO_PATH)


def trusted_authors(cfg=None):
    cfg = cfg or load_config()
    return set((cfg.get("identity") or {}).get("trusted_authors") or [])


def trusted_negatives(cfg=None):
    """可信的漏检标注：自动提案的唯一种子。

    可信 = 人工来源 **且** 标注人在白名单里。只判断 `source` 前缀会让
    任何能调 HTTP 接口的客户端凭一个自报的 by 名就刷种子（第四轮 V5）。
    """
    allow = trusted_authors(cfg)
    return [a for a in load_annotations()
            if a.get("kind") == "false_negative" and a.get("trusted")
            and str(a.get("source", "")).startswith("human:")
            and a.get("by") in allow]


def _read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 候选规则的构造（确定性变换，可逐字符复核）
# ---------------------------------------------------------------------------
def span_to_pattern(span):
    r"""把标注片段变成正则：**数字泛化、文字保留、空白灵活**。

    这是「模板实例化」的最小形态：
        `同比下降 -3.2%`  →  `同比下降\s*-\s*\d+(?:\.\d+)?\s*%`

    三个要点（都是踩出来的）：
      · 数字泛化：同一个表述在不同文档里数值不同，写死就只认那一份；
      · 文字保留：不做语义推断，否则提案不可复核，也会把「一句错话」
        泛化成「一类错话」；
      · 空白转 `\s*`：中文文档里空格有无很随意，把空格吃掉会导致
        规则在来源文档上都不命中（实测门禁 0 命中就是这么来的）。
    """
    out, i = [], 0
    while i < len(span):
        ch = span[i]
        if ch.isspace():
            while i < len(span) and span[i].isspace():
                i += 1
            out.append(r"\s*")
        elif ch.isdigit():
            while i < len(span) and (span[i].isdigit() or span[i] == "."):
                i += 1
            out.append(r"\d+(?:\.\d+)?")
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def cue_for(dimension):
    """每个维度的「结构前提」：没有它，片段匹配到哪都算数，极易误报。"""
    return {
        "fact": r"\d",
        "logic": r"\d|周[一二三四五六日]|季度|同比|环比|合计|总计",
        "style": r"",
        "safety": r"",
        "compliance": r"",
    }.get(dimension, r"")


def _anchor(span):
    """聚类锚点：片段里最长的连续中文。同锚点视为「同一种表述」。"""
    runs = re.findall(r"[\u4e00-\u9fa5]{2,}", span)
    return max(runs, key=len) if runs else ""


def _fmt(sent):
    """句级指纹：去空白与常见标点，用于判断「命中的是不是标注指认的那句」。"""
    return re.sub(r"[\s，,。；;、：:（）()「」“”\"']", "", sent or "")


def _same_sentence(a, b):
    """两句是否指向同一处：一句包含另一句（去格式后），或前 20 字相同。"""
    fa, fb = _fmt(a), _fmt(b)
    if not fa or not fb:
        return False
    if fa in fb or fb in fa:
        return True
    return fa[:20] == fb[:20]


def _hits_provenance(quote, prov_quotes):
    """候选的一条命中，是否对应到「标注指认的那句」。

    这是门禁从「文档级」升级到「句级」的关键：同一份来源文档里，
    只要有一句正常的同模式文本被命中，就必须算误报——否则
    「问题句 + 正常句」放进同一份文档就能把 0 误报指标整个绕过去。
    """
    return any(_same_sentence(quote, q) for q in prov_quotes)


def content_hash(candidate):
    """候选的可判定内容指纹：规则本体 + 来源标注 + 来源文档。

    只保护「判定内容」：状态字段（status/gate/metrics）不参与——
    它们在门禁与晋升时都会被重算，不需要也不应该被信任。
    """
    payload = json.dumps({
        "rule": candidate.get("rule") or {},
        "provenance": candidate.get("provenance") or [],
        "source_docs": sorted(candidate.get("source_docs") or []),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_id(dimension, anchor, pattern):
    h = hashlib.sha256(f"{dimension}|{anchor}|{pattern}".encode("utf-8")).hexdigest()[:6]
    return f"AUTO-{dimension.upper()}-{h.upper()}"


def build_candidate(cluster, cfg):
    """由同一「表述」的多条标注生成一条候选规则。"""
    dim = cluster["dimension"]
    first = cluster["annotations"][0]
    pattern = span_to_pattern(first["quote"])
    cid = candidate_id(dim, cluster["anchor"], pattern)
    # 不再生成 require：pattern 由片段泛化而来、必然含 \d+，
    # 而旧 cue_for 里也有 \d 分支 → require 恒真，纯粹是冗余（第四轮 V6）。
    # 真正的「结构前提」已经编码在 pattern 自身里。
    max_sev = cfg["severity"]["max_auto"]
    return {
        "candidate_id": cid,
        "status": "proposed",
        "created_at": now_iso(),
        "dimension": dim,
        "anchor": cluster["anchor"],
        "pattern": pattern,
        "require": "",
        "guard": "",
        "confidence": 0.7,
        # 自动提案不允许上 blocker/major：错一次的代价是误拦
        "severity": min([max_sev, "minor"], key=lambda s: SEV_RANK[s]),
        "desc": f"（自动提案，源自 {len(cluster['annotations'])} 条人工漏检标注）"
                f"命中「{cluster['anchor']}」类表述时需人工确认",
        "suggestion": "按该表述对应的判定要求整改后复核。",
        "provenance": [{"doc": a["target_path"], "quote": a["quote"],
                        "by": a["by"], "ts": a["ts"]} for a in cluster["annotations"]],
        "source_docs": sorted({a["target_path"] for a in cluster["annotations"]}),
        "metrics": {},
        "gate": {"passed": None, "reasons": []},
        "rule": {
            "rule_id": cid,
            "dimension": dim,
            "severity": min([max_sev, "minor"], key=lambda s: SEV_RANK[s]),
            "desc": f"（自动提案）{cluster['anchor']} 类表述需人工确认",
            "method": "auto_mined_pattern",
            "source": "自学习闭环：人工漏检标注 → 候选 → 门禁通过后晋升",
            "pattern": pattern,
            "require": "",
            # 自动提案标记：报告/控制台据此提示「措辞级匹配，需人工确认」
            "auto_mined": True,
            "confidence": 0.7,
            "suggestion": "按该表述对应的判定要求整改后复核。",
        },
    }


# ---------------------------------------------------------------------------
# 语料与门禁
# ---------------------------------------------------------------------------
_SKIP_DIRS = {"uploads", "learning_demo"}  # learning_demo 单独由配置决定


def corpus(cfg=None):
    """门禁语料：越全越好。只看来源文档等于自证，必须用别的样本打误报。"""
    cfg = cfg or load_config()
    paths = []
    for item in cfg["corpus"].get("include") or []:
        p = ROOT / item
        if p.is_dir():
            paths.extend(sorted(f for f in p.iterdir() if f.is_file()))
        elif p.exists():
            paths.append(p)
    bad = tuple(cfg["corpus"].get("exclude_suffix") or ())
    out = []
    for p in paths:
        if p.name.startswith(".") or (bad and p.name.endswith(bad)):
            continue
        if p.suffix.lower() in (".png", ".jpg", ".pdf", ".docx", ".zip"):
            continue
        out.append(p)
    return out


_BASELINE_CACHE = {}
_EVAL_LOCK = None      # 运行时指向 STATE/.evaluate.lock


def _eval_lock_path():
    return (Path(_EVAL_LOCK) if _EVAL_LOCK else STATE / ".evaluate.lock")


def _acquire_eval_lock(timeout=180):
    """评估会临时改写 review.OUTPUTS/LOGS/LEDGER 三个模块级全局变量。

    同进程并发跑两次 evaluate，就会把报告写进对方的临时目录（或真实目录）。
    这里用一个排他锁把评估串行化——宁可排队，也不要串数据。
    """
    path = _eval_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            if time.time() > deadline:
                raise LearningError(
                    f"另一个评估正在进行（{path.name} 已存在）；"
                    "并发评估会互相覆盖输出目录，故拒绝并发")
            time.sleep(0.2)


def _release_eval_lock(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


# 引擎既定的「同句不叠提示」去重：新规则命中的句子会让 COMP-004 主动退让，
# 这是设计行为，不该记成「候选压掉了别人的结论」。
_DEDUPE_BY_DESIGN = {"COMP-004"}


def _baseline(rules_key, rules, corpus_paths, review, rules_store):
    if rules_key in _BASELINE_CACHE:
        return _BASELINE_CACHE[rules_key]
    base = {}
    for p in corpus_paths:
        rep = review.run_one(p, rules, use_llm=False)
        base[str(p)] = [i["rule_id"] for i in rep["issues"]
                        if i["rule_id"] not in _DEDUPE_BY_DESIGN]
    _BASELINE_CACHE[rules_key] = base
    return base


def evaluate(candidate, corpus_paths=None, cfg=None, allow_in_library=False):
    """在**全语料**上跑一遍：带候选 vs 不带候选。

    这是整个闭环唯一的硬门槛。指标全部可复算：
      · hit_docs       —— 候选在几份「来源文档」里真的找回了被标注的问题
      · offtarget      —— 在**非来源文档**上新增了几条（泛化过头 / 误报）
      · suppressed     —— 候选把别的规则的结论压掉了几条（互相干扰）
    """
    cfg = cfg or load_config()
    import review
    import rules_store

    corpus_paths = corpus_paths or corpus(cfg)
    if not corpus_paths:
        raise LearningError("门禁语料为空：检查 config/learning.json 的 corpus.include")

    rules = rules_store.load_active_rules()
    rule = dict(candidate["rule"])
    if allow_in_library:
        # 复评已晋升规则（漂移监控）时，先把库里同 id 的旧版本摘掉，
        # 否则基线与带候选会同时包含它，指标自相矛盾。
        rules = [r for r in rules if r["rule_id"] != rule["rule_id"]]
    if any(r["rule_id"] == rule["rule_id"] for r in rules):
        # 同一 id 已经在库：基线把它算作"其他规则"、带候选时又算作"候选自己的"，
        # 会凭空得出「压掉 N 条」的假指标。这不是候选的问题，是重复评估。
        raise LearningError(
            f"{rule['rule_id']} 已在规则库中，无需再评估（若要验证效果请直接审查样本）")
    rules_key = tuple(sorted(r["rule_id"] for r in rules))

    work = Path(tempfile.mkdtemp(prefix="ra-learn-"))
    saved = (review.OUTPUTS, review.LOGS, review.LEDGER)
    lock = _acquire_eval_lock()
    try:
        for d in ("outputs", "logs", "ledger"):
            (work / d).mkdir(parents=True, exist_ok=True)
        review.OUTPUTS, review.LOGS, review.LEDGER = (
            work / "outputs", work / "logs", work / "ledger")
        base = _baseline(rules_key, rules, corpus_paths, review, rules_store)
        hits, offtarget, suppressed, redundant = [], [], 0, []
        for p in corpus_paths:
            rep = review.run_one(p, rules + [rule], use_llm=False)
            got = [(i["rule_id"], i["quote"]) for i in rep["issues"]]
            mine = [q for rid, q in got if rid == rule["rule_id"]]
            others_now = len([1 for rid, _ in got
                              if rid != rule["rule_id"]
                              and rid not in _DEDUPE_BY_DESIGN])
            others_before = len(base.get(str(p), []))
            if others_now < others_before:
                suppressed += others_before - others_now
            for q in mine:
                rel = str(p.relative_to(ROOT)).replace("\\", "/")
                hits.append({"doc": rel, "quote": q})
                # 同一句已经被现有规则报出来了 → 这条候选不增加任何检出能力，
                # 只是把同一处问题报第二遍。补了确定性执行器的盲区最容易撞上这个
                # （第六轮 B2 补了 LOGIC-004 之后，自动候选依旧"门禁全绿"）。
                dup = [(rid2, q2) for rid2, q2 in got
                       if rid2 != rule["rule_id"] and rid2 not in _DEDUPE_BY_DESIGN
                       and review._quote_overlap(q, q2) >= 0.8]
                if dup:
                    redundant.append({"doc": rel, "quote": q,
                                      "covered_by": sorted({rid2 for rid2, _ in dup})})
    finally:
        review.OUTPUTS, review.LOGS, review.LEDGER = saved
        shutil.rmtree(work, ignore_errors=True)
        _release_eval_lock(lock)

    src_docs = set(candidate.get("source_docs") or [])
    prov = {}
    for item in candidate.get("provenance") or []:
        prov.setdefault(item["doc"], []).append(item.get("quote") or "")

    off, internal = [], []
    for h in hits:
        if h["doc"] not in src_docs:
            off.append(h)                       # 来源文档之外 → 泛化过头
        elif not _hits_provenance(h["quote"], prov.get(h["doc"]) or []):
            internal.append(h)                  # 来源文档内、但不是标注那句 → 内部误报
    hit_docs = sorted({h["doc"] for h in hits
                       if h["doc"] in src_docs
                       and _hits_provenance(h["quote"], prov.get(h["doc"]) or [])})
    metrics = {
        "corpus_size": len(corpus_paths),
        "hits": hits,
        "hit_docs": hit_docs,
        "hit_doc_count": len(hit_docs),
        "offtarget": off,
        "offtarget_count": len(off),
        # 文档级判定曾经漏掉的那一半：来源文档内部把正常句也报了
        "internal_offtarget": internal,
        "internal_offtarget_count": len(internal),
        "suppressed_others": suppressed,
        "redundant": redundant,
        "redundant_count": len(redundant),
    }
    return metrics


def gate(candidate, metrics, cfg=None):
    """判定候选能不能进入「可晋升」状态。返回 (passed, reasons)。"""
    cfg = cfg or load_config()
    g = cfg["gate"]
    reasons = []

    if len(candidate.get("source_docs") or []) < int(g["min_sources"]):
        reasons.append(
            f"来源文档不足：{len(candidate.get('source_docs') or [])} 份 < 要求 "
            f"{g['min_sources']} 份（单文档学出来的规则必然过拟合）")
    if metrics["hit_doc_count"] < int(g["min_sources"]):
        reasons.append(
            f"在来源文档里只命中了 {metrics['hit_doc_count']} 份 < 要求 "
            f"{g['min_sources']} 份（模式太窄，或提取错了特征）")
    if metrics["offtarget_count"] > int(g["max_offtarget"]):
        docs = "、".join(sorted({h["doc"] for h in metrics["offtarget"]})[:3])
        reasons.append(
            f"在非来源文档上新增 {metrics['offtarget_count']} 条（上限 "
            f"{g['max_offtarget']}）：{docs}——泛化过头，会把正常文本报成问题")
    if metrics.get("internal_offtarget_count", 0) > int(g["max_internal_offtarget"]):
        docs = "、".join(sorted({h["doc"] for h in metrics.get("internal_offtarget") or []})[:3])
        reasons.append(
            f"在来源文档内部命中 {metrics['internal_offtarget_count']} 条**不是标注指认的**句子"
            f"（上限 {g['max_internal_offtarget']}）：{docs}——"
            "同一份文档里正常句也被报，说明规则只看措辞不看语义")
    if metrics["suppressed_others"] > int(g["max_suppressed_others"]):
        reasons.append(
            f"候选压掉了其他规则的 {metrics['suppressed_others']} 条结论"
            "（新增的这条不该改变别的判定的可见性）")
    # 冗余门禁：如果候选命中的句子**已经**被现有规则报出，加进来只是把同一处
    # 问题报第二遍。用户不会因为"多一条规则"而多看到一个问题，只会多一条重复项。
    max_red = int(g.get("max_redundant_hits", 0))
    if metrics.get("redundant_count", 0) > max_red:
        covered = sorted({r for h in metrics.get("redundant") or []
                          for r in (h.get("covered_by") or [])})
        reasons.append(
            f"候选命中的 {metrics['redundant_count']} 处**已被现有规则覆盖**"
            f"（{'、'.join(covered)}，上限 {max_red}）——"
            "新增这条不增加检出能力，只是重复报同一处问题")
    conf = float(candidate.get("confidence") or 0)
    if not (float(g["min_confidence"]) <= conf <= float(g["max_confidence"])):
        reasons.append(f"置信度 {conf} 不在 [{g['min_confidence']}, {g['max_confidence']}] 区间")
    if candidate.get("dimension") not in DIMENSIONS:
        reasons.append(f"维度非法：{candidate.get('dimension')}")
    return (not reasons, reasons)


def save_candidate(cand):
    _ensure_dirs()
    path = CAND_DIR / f"{cand['candidate_id']}.json"
    path.write_text(json.dumps(cand, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


CAND_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{2,39}$")
DECISIONS = ("approve", "reject")
# 「待人工审核」= 还没人做过决定、且门禁没把它毙掉。
# rejected / rolled_back / promoted 都不该再出现在待审队列里。
PENDING_STATUS = ("proposed", "passed")


def load_candidate(cid):
    # 候选 ID 直接拼进文件名：不校验就等于把 state/candidates/ 之外的
    # 任意 JSON 交给调用方读（`../../config/llm.local.json` 这类）。
    if not CAND_ID_RE.match(str(cid or "")):
        raise LearningError(f"候选 ID 非法：{cid!r}")
    path = CAND_DIR / f"{cid}.json"
    if not path.exists():
        raise LearningError(f"找不到候选：{cid}")
    return json.loads(path.read_text(encoding="utf-8"))


def is_pending(cand):
    """是否还要人看一眼：门禁没拒、也没人下过决定。"""
    if cand.get("status") not in PENDING_STATUS:
        return False
    return not (cand.get("human_review") or {}).get("decision")


def decide_candidate(cid, decision, note="", by="console"):
    """记录人工决定。**只写候选文件的 human_review 字段，不动规则库。**

    CLI（tools/rule_review.py）与控制台（POST /api/candidates/decide）共用这一份实现，
    避免两边各写一套、状态判断出现分歧（完整性校验踩过同样的坑）。
    """
    if decision not in DECISIONS:
        raise LearningError(f"decision 只能是 {DECISIONS}，收到 {decision!r}")
    cand = load_candidate(cid)
    cand["human_review"] = {
        "decision": decision,
        "decided_at": now_iso(),
        "by": str(by or "console")[:40],
        "note": str(note or "")[:500],
    }
    save_candidate(cand)
    return {
        "candidate_id": cid,
        "decision": decision,
        "dimension": cand.get("dimension"),
        "status": cand.get("status"),
        "human_review": cand["human_review"],
        # 提示下一步该跑什么命令（晋升仍然只能走 CLI，HTTP 面没有写规则库的能力）
        "next_step": ("python tools/rule_promote.py shadow {0} && "
                      "python tools/rule_promote.py promote {0} --by {1}".format(cid, by or "owner")
                      + (" --confirm-manual"
                         if cand.get("dimension") in ("safety", "compliance", "fact") else ""))
        if decision == "approve" else "",
    }


def console_candidates():
    """给控制台的候选快照（去掉 hits/offtarget 原文列表，只留可读指标）。

    排序：待审在前 → 维度 → ID。控制台一打开就该看到"现在要我审什么"。
    """
    order = {"safety": 0, "compliance": 1, "fact": 2, "logic": 3, "style": 4}
    items = []
    for c in iter_candidates():
        m = c.get("metrics") or {}
        g = c.get("gate") or {}
        items.append({
            "candidate_id": c.get("candidate_id"),
            "dimension": c.get("dimension"),
            "status": c.get("status"),
            "severity": c.get("severity"),
            "anchor": c.get("anchor") or "",
            "pattern": c.get("pattern") or "",
            "require": c.get("require") or "",
            "guard": c.get("guard") or "",
            "desc": c.get("desc") or "",
            "suggestion": c.get("suggestion") or "",
            "confidence": c.get("confidence"),
            "provenance": [{"doc": p.get("doc"), "quote": p.get("quote"),
                            "by": p.get("by"), "ts": p.get("ts")}
                           for p in (c.get("provenance") or [])][:6],
            "metrics": {
                "corpus_size": m.get("corpus_size"),
                "hit_doc_count": m.get("hit_doc_count"),
                "offtarget_count": m.get("offtarget_count"),
                "internal_offtarget_count": m.get("internal_offtarget_count"),
                "suppressed_others": m.get("suppressed_others"),
                "redundant_count": m.get("redundant_count"),
                "redundant_covered_by": sorted({r for h in (m.get("redundant") or [])
                                                for r in (h.get("covered_by") or [])}),
                "offtarget_docs": sorted({h.get("doc") for h in (m.get("offtarget") or [])})[:5],
            },
            "gate": {"passed": g.get("passed"), "reasons": g.get("reasons") or []},
            "human_review": c.get("human_review") or {},
            "pending": is_pending(c),
        })
    items.sort(key=lambda x: (not x["pending"],
                              order.get(x["dimension"], 9),
                              x["candidate_id"] or ""))
    return items


def iter_candidates():
    if not CAND_DIR.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(CAND_DIR.glob("*.json"))]


# ---------------------------------------------------------------------------
# shadow：真实语料上预演，不改任何结论
# ---------------------------------------------------------------------------
def shadow(cid, cfg=None):
    cfg = cfg or load_config()
    cand = load_candidate(cid)
    metrics = evaluate(cand, cfg=cfg)
    passed, reasons = gate(cand, metrics, cfg)
    payload = {
        "candidate_id": cid, "ts": now_iso(), "metrics": metrics,
        "content_hash": content_hash(cand),
        "gate": {"passed": passed, "reasons": reasons},
        "new_issues": len(metrics["hits"]),
        "over_limit": len(metrics["hits"]) > int(cfg["shadow"]["max_new_issues"]),
    }
    _ensure_dirs()
    (SHADOW_DIR / f"{cid}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cand["metrics"] = metrics
    cand["gate"] = {"passed": passed, "reasons": reasons}
    cand["status"] = "passed" if passed else "rejected"
    cand["content_hash"] = payload["content_hash"]
    cand["shadow_at"] = payload["ts"]
    save_candidate(cand)
    return payload


# ---------------------------------------------------------------------------
# 规则库写入（唯一的写入口）
# ---------------------------------------------------------------------------
def rules_version():
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip() or "unversioned"
    except OSError:
        return "unversioned"


def bump_rules_version():
    """版本号自增。**任何写 rules/ 的动作都必须调它**，否则历史报告会被判成篡改。"""
    cur = rules_version()
    m = re.search(r"-a(\d+)$", cur)
    new = (cur[: m.start()] + f"-a{int(m.group(1)) + 1}") if m else f"{cur}-a1"
    VERSION_PATH.write_text(new + "\n", encoding="utf-8")
    return cur, new


def snapshot_rules(tag):
    _ensure_dirs()
    dest = SNAP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}__{tag}"
    if SNAP_DIR.exists():
        for old in sorted(SNAP_DIR.glob("*"))[:-9]:      # 只留最近 10 份
            shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(RULES_DIR, dest)
    return dest


def _rel(path):
    """相对项目根的展示路径；测试里 RULES_DIR 被指到临时目录时退回绝对路径。"""
    try:
        return Path(path).relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def rule_file_for(cid):
    """自动规则的文件名：`AUTO-LOGIC-BE1B78` → `auto_logic_be1b78.yaml`。

    前缀 `auto_` 一眼能看出「这条不是人手写的」；candidate_id 本身已含 auto，
    所以不再重复拼一次（第一版生成了 auto_auto_xxx.yaml，丑且难检索）。
    """
    name = cid.lower().replace("-", "_")
    return RULES_DIR / (f"{name}.yaml" if name.startswith("auto_") else f"auto_{name}.yaml")


def yaml_dump_rule(rule):
    """手写 YAML（不引入依赖），字段顺序固定，便于人读与 diff。"""
    lines = [f"- rule_id: {rule['rule_id']}",
             f"  dimension: {rule['dimension']}",
             f"  severity: {rule['severity']}",
             f"  desc: {_yaml_str(rule['desc'])}",
             f"  method: {rule.get('method', 'auto_mined_pattern')}",
             f"  source: {_yaml_str(rule.get('source', ''))}",
             f"  confidence: {rule.get('confidence', 0.7)}",
             f"  pattern: {_yaml_str(rule['pattern'])}"]
    if rule.get("require"):
        lines.append(f"  require: {_yaml_str(rule['require'])}")
    if rule.get("guard"):
        lines.append(f"  guard: {_yaml_str(rule['guard'])}")
    lines.append(f"  suggestion: {_yaml_str(rule.get('suggestion', ''))}")
    return "\n".join(lines) + "\n"


def _yaml_str(text):
    text = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _week_auto_count():
    cutoff = time.time() - 7 * 86400
    n = 0
    for row in read_jsonl(LOG_PATH):
        if row.get("op") == "promote" and row.get("mode") == "auto":
            try:
                import datetime
                ts = datetime.datetime.fromisoformat(row["ts"]).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                n += 1
    return n


def promote(cid, by, mode="manual", confirm_manual=False, cfg=None):
    """把候选写进规则库。**这是整个项目里唯一会写 rules/ 的代码路径。**

    六道闸门，任何一条不过就拒绝，且拒绝原因要能说清楚：

      1. **重新验证**（不是读文件里的状态）：候选 JSON 可以被任何人改写，
         所以 gate 与 shadow 的结论一律**在晋升时重算**。伪造
         `gate.passed=true` / `shadow_at` 不再有任何作用。
      2. **内容完整性**：候选的规则本体 + 来源标注有 content_hash；
         门禁之后被改过（例如偷换 pattern）就拒绝，必须重新过门禁。
      3. **shadow 噪音上限**：预演时新增条数超限即拒绝（原来是只打印 ⚠）。
      4. **维度授权**：safety/compliance/fact 只能人工确认；auto 只允许 logic/style。
      5. **只增不改**：rule_id 已存在则拒绝。
      6. **配额**：自动晋升每周上限。
    """
    cfg = cfg or load_config()
    cand = load_candidate(cid)
    if cand.get("status") == "promoted":
        raise LearningError(f"{cid} 已经晋升过（{cand.get('promoted_at')}）")
    if cand.get("status") == "rolled_back":
        raise LearningError(f"{cid} 已被回滚，需重新过门禁才能再晋升")

    rule_id = cand["rule"]["rule_id"]
    existing = {r["rule_id"] for r in _load_rule_ids()}
    if rule_id in existing:
        raise LearningError(
            f"{rule_id} 已存在于规则库——自动提案只允许新增，禁止覆盖/修改已有规则")

    if cfg["approval"]["require_shadow"] and not cand.get("shadow_at"):
        raise LearningError(f"{cid} 还没做 shadow 预演，先跑 rule_promote.py shadow {cid}")

    # ---- 闸门 1/2：内容完整性 + 重算 ----
    stored_hash = cand.get("content_hash")
    if stored_hash and stored_hash != content_hash(cand):
        raise LearningError(
            f"{cid} 的判定内容在门禁之后被修改过（content_hash 不匹配）——"
            "请重新执行 gate，让它按新内容重新评估")
    metrics = evaluate(cand, cfg=cfg)
    passed, reasons = gate(cand, metrics, cfg)
    if not passed:
        cand["metrics"] = metrics
        cand["gate"] = {"passed": False, "reasons": reasons}
        cand["status"] = "rejected"
        save_candidate(cand)
        raise LearningError(
            f"{cid} 重新验证未通过门禁，不能晋升。原因：{'；'.join(reasons)}")

    # ---- 闸门 3：shadow 噪音上限（V4）----
    limit = int(cfg["shadow"]["max_new_issues"])
    new_issues = len(metrics["hits"])
    if new_issues > limit:
        raise LearningError(
            f"{cid} 在语料上新增 {new_issues} 条异议，超过 shadow 上限 {limit}"
            "——这类规则太吵，会把人淹掉")

    # ---- 闸门 4：维度授权 ----
    dim = cand["dimension"]
    manual_only = dim in (cfg["approval"]["manual_only_dimensions"] or [])
    if mode == "auto":
        if manual_only:
            raise LearningError(
                f"{dim} 维度只能人工确认：这类误报的代价是误拦，自动通道不碰它")
        if dim not in (cfg["approval"]["auto_ok_dimensions"] or []):
            raise LearningError(f"{dim} 维度不在自动晋升白名单内")
        quota = int(cfg["quota"]["max_auto_promotions_per_week"])
        used = _week_auto_count()
        if used >= quota:
            raise LearningError(f"本周自动晋升已达上限 {quota} 条（已用 {used}）")
    elif manual_only and not confirm_manual:
        raise LearningError(
            f"{dim} 维度需显式人工确认：加 --confirm-manual（并记录 --by），"
            "防止用脚本绕过审批")

    # 复算结果落盘（此刻的状态是我们自己算出来的，不是读来的）
    cand["metrics"] = metrics
    cand["gate"] = {"passed": True, "reasons": []}
    cand["content_hash"] = content_hash(cand)
    cand["verified_at"] = now_iso()
    cand["shadow_at"] = cand.get("shadow_at") or now_iso()
    save_candidate(cand)

    snap = snapshot_rules(rules_version())
    target = rule_file_for(cid)
    if target.exists():
        raise LearningError(f"目标文件已存在：{target.name}（只增不改）")
    target.write_text(yaml_dump_rule(cand["rule"]), encoding="utf-8")
    before, after = bump_rules_version()

    cand["status"] = "promoted"
    cand["promoted_at"] = now_iso()
    cand["promoted_by"] = by
    cand["promotion_mode"] = mode
    save_candidate(cand)
    row = {
        "ts": now_iso(), "op": "promote", "rule_id": rule_id,
        "candidate_id": cid, "dimension": dim, "by": by, "mode": mode,
        "version_before": before, "version_after": after,
        "file": _rel(target),
        "snapshot": _rel(snap),
        "metrics": {k: v for k, v in metrics.items()
                    if k in ("hit_doc_count", "offtarget_count",
                             "internal_offtarget_count", "suppressed_others",
                             "corpus_size")},
        "gate_reasons": [],
        # 晋升时确实重算过，不是读文件里的状态
        "reverified_at": cand["verified_at"],
        "content_hash": cand["content_hash"],
    }
    _append_jsonl(LOG_PATH, row)
    return row


def _load_rule_ids():
    import rules_store
    return rules_store.load_all_rules()


def _rule_ids_in_library():
    return {r["rule_id"] for r in _load_rule_ids()}


def rollback(rule_id, by, cfg=None, reason=""):
    """回滚一条已晋升的规则：删文件 + 升版本 + 记账。"""
    cfg = cfg or load_config()
    hits = [p for p in RULES_DIR.glob("*.yaml")
            if rule_id in p.read_text(encoding="utf-8")]
    if not hits:
        raise LearningError(f"规则库里没有 {rule_id}")
    snap = snapshot_rules(rules_version())
    for p in hits:
        p.unlink()
    before, after = bump_rules_version()
    cand = None
    for c in iter_candidates():
        if c["rule"]["rule_id"] == rule_id:
            cand = c
            cand["status"] = "rolled_back"
            cand["rolled_back_at"] = now_iso()
            save_candidate(cand)
            break
    row = {"ts": now_iso(), "op": "rollback", "rule_id": rule_id, "by": by,
           "reason": reason or "手动回滚",
           "version_before": before, "version_after": after,
           "removed": [p.name for p in hits],
           "snapshot": _rel(snap)}
    _append_jsonl(LOG_PATH, row)
    return row


def audit(rule_id=None, by="audit", cfg=None, auto_rollback=True):
    """对已晋升的自动规则做一次漂移复评（V9）。

    晋升时通过门禁，只说明「在那份语料上没问题」。真实语料一变，
    措辞级规则就可能开始误报，而且没人会知道。所以提供一个可定期执行的
    `audit`：用**当前**语料重跑门禁，不通过就自动回滚并记账。

    注意：`evaluate(allow_in_library=True)` 会先把库里同 id 的规则摘掉再对照，
    否则基线和带候选会同时包含它，指标没有意义。
    """
    cfg = cfg or load_config()
    promoted = [r for r in read_jsonl(LOG_PATH) if r.get("op") == "promote"]
    seen, results = set(), []
    for row in promoted:
        cid = row.get("candidate_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        try:
            cand = load_candidate(cid)
        except LearningError:
            continue
        if cand.get("status") != "promoted":
            continue
        if rule_id and cand["rule"]["rule_id"] != rule_id:
            continue
        metrics = evaluate(cand, cfg=cfg, allow_in_library=True)
        passed, reasons = gate(cand, metrics, cfg)
        over = len(metrics["hits"]) > int(cfg["shadow"]["max_new_issues"])
        ok = passed and not over
        entry = {
            "rule_id": cand["rule"]["rule_id"], "candidate_id": cid,
            "ok": ok, "metrics": {k: v for k, v in metrics.items()
                                  if k in ("hit_doc_count", "offtarget_count",
                                           "internal_offtarget_count",
                                           "suppressed_others", "corpus_size")},
            "reasons": reasons + ([f"新增条数超过 shadow 上限"] if over else []),
            "rolled_back": False,
        }
        if not ok and auto_rollback:
            rollback(cand["rule"]["rule_id"], by=by,
                     reason="漂移复评未通过：" + "；".join(entry["reasons"])[:300])
            entry["rolled_back"] = True
        _append_jsonl(LOG_PATH, {"ts": now_iso(), "op": "audit",
                                 "rule_id": entry["rule_id"], "by": by,
                                 "ok": ok, "rolled_back": entry["rolled_back"],
                                 "metrics": entry["metrics"],
                                 "reasons": entry["reasons"]})
        results.append(entry)
    return results


def summary():
    cands = iter_candidates()
    by_status = {}
    for c in cands:
        by_status[c.get("status", "?")] = by_status.get(c.get("status", "?"), 0) + 1
    log = read_jsonl(LOG_PATH)
    return {
        "rules_version": rules_version(),
        "annotations": len(load_annotations()),
        "negatives": len(trusted_negatives()),
        "candidates": len(cands),
        "by_status": by_status,
        "promoted": [r for r in log if r.get("op") == "promote"],
        "rolled_back": [r for r in log if r.get("op") == "rollback"],
        "auto_this_week": _week_auto_count(),
        "quota_week": int(load_config()["quota"]["max_auto_promotions_per_week"]),
    }


# ---------------------------------------------------------------------------
# 挖掘：从人工标注聚类出候选
# ---------------------------------------------------------------------------
def mine(cfg=None, limit=None):
    """从可信的漏检标注里聚类出候选。

    只做两件事：**聚类**（同维度 + 同表述）与**模板实例化**（数字泛化）。
    不做语义推断——那会让提案不可复核。语义判断留给门禁与人。
    """
    cfg = cfg or load_config()
    limit = int(limit or cfg["quota"]["max_new_candidates_per_run"])
    clusters = {}
    for a in trusted_negatives():
        anchor = _anchor(a.get("quote") or "")
        if len(anchor) < 2:
            continue
        key = (a["dimension"], anchor)
        clusters.setdefault(key, {"dimension": a["dimension"], "anchor": anchor,
                                  "annotations": []})["annotations"].append(a)

    created, skipped = [], []
    for key, cl in sorted(clusters.items()):
        if len({a["target_path"] for a in cl["annotations"]}) < int(cfg["gate"]["min_sources"]):
            skipped.append({"anchor": cl["anchor"], "dimension": cl["dimension"],
                            "reason": "来源文档不足（单文档不生成候选）"})
            continue
        cand = build_candidate(cl, cfg)
        if cand["rule"]["rule_id"] in _rule_ids_in_library():
            skipped.append({"anchor": cl["anchor"], "dimension": cl["dimension"],
                            "reason": "该模式对应的规则已在库中（同一表述不重复学）",
                            "candidate_id": cand["candidate_id"]})
            continue
        if (CAND_DIR / f"{cand['candidate_id']}.json").exists():
            skipped.append({"anchor": cl["anchor"], "dimension": cl["dimension"],
                            "reason": "候选已存在（同一表述不重复提案）",
                            "candidate_id": cand["candidate_id"]})
            continue
        if len(iter_candidates()) >= int(cfg["quota"]["max_queue"]):
            skipped.append({"anchor": cl["anchor"], "dimension": cl["dimension"],
                            "reason": "候选队列已满"})
            break
        if len(created) >= limit:
            skipped.append({"anchor": cl["anchor"], "dimension": cl["dimension"],
                            "reason": f"本轮提案上限 {limit} 条"})
            continue
        save_candidate(cand)
        created.append(cand)
    return created, skipped
