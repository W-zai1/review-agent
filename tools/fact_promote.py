#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact_promote.py —— **唯一**会写 knowledge/facts.json 的入口

设计见 `docs/知识库自学习.md`（§4 数据流、§5 门禁、§6 审批、§7 治理）。
姊妹工具 `rule_promote.py` 管"判定标准"，本工具管"事实依据"。

## 为什么必须只有一个入口

- 审查过程（`review.py`）永不写事实库：**被审文档不能获得事实写入权**。
- HTTP 面上没有写知识库的接口。
- 候选断言待在 `state/fact_candidates/`，不进 `facts.json`——否则"有人起草了事实"
  和"事实库真的改了"混为一谈，历史报告的 `knowledge_hash` 会被判成篡改。

## 状态机

    proposed ──gate──> passed ──promote(人工)──> promoted
        │                  │                        │
        ├─ 草稿没填完 ──> incomplete                └──rollback──> rolled_back
        ├─ 实质被否决 ──> rejected
        └─ 测不出来   ──> unverified（需 --allow-unmeasured 才继续）

**fact 维度没有自动通道**：`--auto` 会被明确拒绝。存错一条事实会污染
未来**所有**判断，且比漏检隐蔽十倍（CLAUDE 原则：宁可漏、不可错）。

## 门禁（缺一不可）

| 指标 | 门槛 | 为什么 |
|---|---|---|
| `schema` | 通过 | entity/attr/expected/note/review_due 缺一不可 |
| `entity_confirmed` | 必须为 true | 草稿里的实体是正则猜的；猜错则冲突检查全按错实体走 |
| `source_verifiable` | 通过 | 有 URL 或条款号，且**不能是被审文档** |
| `value_from_target` | 通过 | 期望值与被审文档里的数字重合时，必须拿来源自己的原话 `source_quote` 佐证 |
| `type_correct` | 通过 | 上限必须 `type=max`+`unit_hint`（对称容差会把合法值判错） |
| `no_conflict` | 通过 | 同实体同属性同年度不许有两个不同期望值 |
| `off_target` | **增量 = 0** | `after_off − before_off`：候选**新引入**的误报必须为零（存量误报不归咎它，但要报出来） |
| `on_target_hits` / `measurable` | 不能是"没测过" | 语料压不到该实体 → `unmeasured`；压到了但零正贡献 → `weak_signal`；两者都算未通过，必须人工 `--allow-unmeasured` |

**预演跑在生产同款模式**（rule+llm）。纯规则模式下 FACT-002/003/004、LOGIC-004
根本不存在，在那儿算"误报零容忍"测的是另一个系统。`--rule-only` 只用于快速排查，
结论会带 `mode=rule-only` 标记。

## 用法

    py -3 tools/fact_promote.py status
    py -3 tools/fact_promote.py list
    py -3 tools/fact_promote.py show   FACT-CAND-1A2B3C4D
    py -3 tools/fact_promote.py confirm-entity FACT-CAND-1A2B3C4D --entity 华为 --by wang
    py -3 tools/fact_promote.py gate   FACT-CAND-1A2B3C4D
    py -3 tools/fact_promote.py shadow FACT-CAND-1A2B3C4D
    py -3 tools/fact_promote.py promote FACT-CAND-1A2B3C4D --by wang --confirm-manual
    py -3 tools/fact_promote.py rollback FACT-CAND-1A2B3C4D --by wang --reason "取数口径错"
    py -3 tools/fact_promote.py log
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---- 可被测试重定向的路径（全部是模块级，便于 unittest 打补丁） ----
KDIR = ROOT / "knowledge"
FACTS = KDIR / "facts.json"
VERSION_FILE = ROOT / "config" / "knowledge_version.txt"
STATE = ROOT / "state"
CAND_DIR = STATE / "fact_candidates"
SNAP_DIR = STATE / "knowledge_snapshots"
LOG = STATE / "fact_log.jsonl"
GAP_LOG = STATE / "gap_log.jsonl"
GOLDEN = ROOT / "tests" / "golden"

KEEP_SNAPSHOTS = 10
ALLOWED_TYPES = ("number", "max", "date", "text")
UNIT_HINTS = ("个月", "年", "天", "日", "小时", "万台", "亿台", "台", "元", "亿元",
              "万元", "%", "倍", "人", "家")


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.date.today().isoformat()


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def knowledge_version():
    p = Path(VERSION_FILE)
    return p.read_text(encoding="utf-8").strip() if p.exists() else "0.0.0"


def bump_version(ver=None):
    """1.1.0 → 1.1.1。解析不了就在后面挂时间戳，绝不静默不升版本。

    版本号自增是硬要求：不升版本而 facts.json 变了，`integrity.py verify`
    会把历史报告判成"知识库被篡改"（红），而不是"依据更新过"（黄）。
    """
    ver = ver or knowledge_version()
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", ver)
    if not m:
        return f"{ver}-{time.strftime('%Y%m%d%H%M%S')}"
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"


def load_candidate(cid, cand_dir=None):
    d = Path(cand_dir) if cand_dir else CAND_DIR
    p = d / f"{cid}.json"
    if not p.exists():
        raise FileNotFoundError(f"候选断言不存在：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_candidate(cand, cand_dir=None):
    d = Path(cand_dir) if cand_dir else CAND_DIR
    _write_json(d / f"{cand['candidate_id']}.json", cand)


def list_candidates(cand_dir=None):
    d = Path(cand_dir) if cand_dir else CAND_DIR
    if not Path(d).exists():
        return []
    out = []
    for p in sorted(Path(d).glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------
def expected_of(cand):
    """候选里的期望值 → facts.json 的 `expected` 结构。"""
    exp = cand.get("expected")
    if isinstance(exp, dict) and exp.get("num") is not None:
        return {"num": exp["num"], "unit": exp.get("unit") or ""}
    num, unit = cand.get("expected_num"), cand.get("expected_unit")
    if num in (None, ""):
        return None
    try:
        return {"num": float(num), "unit": str(unit or "")}
    except (TypeError, ValueError):
        return None


def _source_ok(cand):
    """来源必须可回查，且**不能是被审文档**。

    这一条是整个闭环的安全线：缺口清单里的 quote 来自被审文档，
    谁都可以在文档里写"华为出货 5 亿台"然后指望它变成事实库口径。
    所以来源只认两种形态：http(s) 链接，或「《法名》第 X 条」。

    返回 `(ok, why, hard)`：`hard=True` 表示**原则性违规**（拿被审文档当来源），
    不是"字段没填完"——改个 URL 糊过去不算修好，结论必须是否决。
    """
    src = str(cand.get("source_url") or "").strip()
    if not src:
        return False, "缺少 source_url（没有来源的断言一律停在 proposed）", False
    low = src.lower()
    if "targets/" in low or "targets\\" in low or low.startswith("file:"):
        return False, "source_url 指向被审文档/targets 目录——被审对象不能当证据", True
    if re.match(r"^https?://", low):
        if not str(cand.get("source_title") or "").strip():
            return False, "有 URL 但缺少 source_title（半年后没人知道那页是什么）", False
        if not str(cand.get("retrieved_at") or "").strip():
            return False, "有 URL 但缺少 retrieved_at（网页会变，取数时间必须留）", False
        return True, "", False
    if re.match(r"^《[^》]+》\s*第[一二三四五六七八九十百零\d]+条", src):
        return True, "", False
    return False, "source_url 必须是 http(s) 链接或「《法名》第 X 条」", False


def _type_ok(cand):
    t = str(cand.get("type") or "").strip().lower()
    if t not in ALLOWED_TYPES:
        return False, f"未知 type：{t or '(空)'}（支持 {'/'.join(ALLOWED_TYPES)}）"
    if t == "max":
        if not str(cand.get("unit_hint") or "").strip():
            return False, "type=max 必须给 unit_hint（上限只在带同单位时判，否则会把别的数字当上限）"
        if str(cand.get("unit_hint")) not in UNIT_HINTS:
            return False, f"unit_hint「{cand.get('unit_hint')}」不在已知单位表里，判不出来"
        if cand.get("tolerance"):
            return False, "type=max 是单侧判断，不能带 tolerance（对称容差会把合法的小值判成偏差）"
    if t == "number":
        tol = cand.get("tolerance")
        if tol in (None, ""):
            return False, "type=number 必须给 tolerance（否则默认 2 倍，等于没写口径）"
        try:
            if float(tol) <= 1:
                return False, "tolerance 必须 > 1（它是对称倍数容差，1 表示零容差）"
        except (TypeError, ValueError):
            return False, "tolerance 不是数字"
    if t == "date":
        y = cand.get("expected_year")
        if y in (None, ""):
            return False, "type=date 必须给 expected_year"
        if not re.match(r"^(19|20)\d{2}$", str(y)):
            return False, "expected_year 必须是四位年份"
    if t == "text":
        if not str(cand.get("expected_text") or "").strip():
            return False, "type=text 必须给 expected_text"
    return True, ""


def _check_conflict(cand, assertions):
    entity = str(cand.get("entity") or "").strip()
    attr = str(cand.get("attr") or "").strip()
    exp = expected_of(cand)
    year_ctx = cand.get("year_ctx")
    for a in assertions:
        if str(a.get("entity")) != entity or str(a.get("attr")) != attr:
            continue
        if str(a.get("year_ctx") or "") != str(year_ctx or ""):
            continue
        if a.get("type") != cand.get("type"):
            return False, (f"与现有断言类型冲突：{entity}·{attr} 已存在 type={a.get('type')}，"
                           f"候选是 type={cand.get('type')}")
        if cand.get("type") in ("number", "max"):
            old = a.get("expected") or {}
            if exp and old.get("num") is not None and float(old["num"]) != float(exp["num"]):
                return False, (f"与现有断言冲突：{entity}·{attr} 已声明 "
                               f"{old['num']:g}{old.get('unit') or ''}，候选写 "
                               f"{exp['num']:g}{exp['unit']}——同一口径不能有两个值，"
                               "冲突请先人工裁定（或加 year_ctx 区分年度）")
        elif cand.get("type") == "date":
            if str(a.get("expected_year")) != str(cand.get("expected_year")):
                return False, (f"与现有断言冲突：{entity}·{attr} 已声明 "
                               f"{a.get('expected_year')} 年")
    return True, ""


def validate_schema(cand):
    errs = []
    if not str(cand.get("candidate_id") or "").startswith("FACT-CAND-"):
        errs.append("candidate_id 必须以 FACT-CAND- 开头")
    if not str(cand.get("entity") or "").strip():
        errs.append("entity 不能为空")
    if not str(cand.get("attr") or "").strip():
        errs.append("attr 不能为空（要写具体属性，如「2024 全球手机出货量」）")
    if cand.get("type") in ("number", "max") and not expected_of(cand):
        errs.append("缺少期望值（expected 或 expected_num/expected_unit）")
    if not str(cand.get("note") or "").strip():
        errs.append("note 不能为空（写口径/条款号，供半年后复查）")
    if not str(cand.get("review_due") or "").strip():
        errs.append("缺少 review_due（事实会过期，不设复查日等于埋雷）")
    return errs


# ---- shadow：在历史语料上预演，不改任何结论 ----
#
# 这道门是本模块唯一真正守门的地方，所以三件事必须做对
# （2026-10-28 复核：三条全错，见 CHANGELOG）：
#   1. 算**增量**而不是绝对值：`after_off - before_off`。事实库里的存量误报
#      不能算到新候选头上——否则语料里只要有一条存量误报，任何候选都过不了。
#   2. **逐句对齐**：用（语料名 + 规则 + 归一化 quote）做指纹，只有"加进候选
#      之后才出现"的那几条才算它的负贡献。
#   3. 走**生产同款模式**（rule+llm）。纯规则模式下 FACT-002/003/004、LOGIC-004
#      根本不存在，在那里算"误报零容忍"测的是另一个系统。
SCALE = {"亿": 1e8, "万": 1e4}
_NUM_TOKEN = re.compile(r"(\d[\d,，]*(?:\.\d+)?)\s*(亿|万)?")


def _shadow_llm(use_llm=None):
    """预演是否启用 LLM：**默认跟随生产配置**。

    `REVIEW_AGENT_FACT_SHADOW_LLM=0` 只给测试用；metrics 里会写明
    `mode=rule-only`，读报告的人不会把它当成生产模式下的结论。
    """
    if use_llm is not None:
        return bool(use_llm)
    env = os.environ.get("REVIEW_AGENT_FACT_SHADOW_LLM")
    if env is not None and str(env).strip() != "":
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    try:
        import llm_client
        cfg = llm_client.load_config()
        local = bool(re.search(r"//(localhost|127\.0\.0\.1)", str(cfg.base_url or "")))
        return bool(cfg.enabled) and bool(cfg.base_url) and (local or bool(cfg.api_key))
    except Exception:                                        # noqa: BLE001
        return False


def _corpus_cases():
    """金标准语料：(path, labels, text)。"""
    cases = Path(GOLDEN) / "cases"
    if not cases.is_dir():
        return []
    out = []
    for p in sorted(cases.glob("*.txt")):
        lab = Path(GOLDEN) / "labels" / f"{p.stem}.labels.json"
        labels = json.loads(lab.read_text(encoding="utf-8"))["errors"] if lab.exists() else []
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            text = ""
        out.append((p, labels, text))
    return out


def _exercised_by(cand, text):
    """本文本是否可能被这条候选"开火"。

    FACT-001 的触发前提是正文里出现 entity（`review.fact_review` 里
    `if idx < 0 or entity not in text: continue`）。所以没提到该实体的语料
    **不可能**因它产生新误报——跳过是等价变换，不是抽样。
    但跳掉多少份必须写进 metrics：否则"35 份语料"会被读成"35 份都验过"。
    """
    ent = str(cand.get("entity") or "").strip()
    return bool(ent) and ent in text


def _label_key(x):
    """把 `(语料名, 标注 id)` 拍成「语料:标注」；测试替身给的纯 id 原样放过。"""
    if isinstance(x, tuple) and len(x) == 2:
        return f"{x[0]}:{x[1]}"
    return str(x)


def _shadow_pass(facts_obj, cases, use_llm):
    """跑一轮语料，返回 (off, on)。

    off: {(语料名, 指纹): 片段}   —— 未命中任何标注的 FACT-001（把对的判错了）
    on:  {(语料名, 标注 id)}      —— 被 FACT-001 命中的标注（把真错误找回来了）

    `on` **必须带语料名**。标注 id 只在单份语料内唯一（p11 和 p12 的标注都叫 `L1`），
    只用 id 做 key 会让"另一份语料的同名标注"顶掉本语料的贡献：p11 明明被找回来了，
    却因为 p12 也有个 `L1` 而算成零贡献 → gate 判 `weak_signal` 误杀好断言。
    （这一点是 `tools/fact_audit.py` 回测老库时暴露的：豆包·月活被算成"零正贡献"。）
    """
    import eval_review
    import retrieve
    import review
    import rules_store

    orig = retrieve.load_facts
    retrieve.load_facts = (lambda path=None, _f=facts_obj: copy.deepcopy(_f)
                           if path is None else orig(path))
    off, on = {}, set()
    try:
        for path, labels, _text in cases:
            work = Path(tempfile.mkdtemp(prefix="ra-shadow-"))
            try:
                for d in ("outputs", "logs", "ledger"):
                    (work / d).mkdir(parents=True, exist_ok=True)
                saved = (review.OUTPUTS, review.LOGS, review.LEDGER)
                review.OUTPUTS, review.LOGS, review.LEDGER = (
                    work / "outputs", work / "logs", work / "ledger")
                try:
                    report = review.run_one(path, rules_store.load_active_rules(),
                                            use_llm=use_llm)
                finally:
                    review.OUTPUTS, review.LOGS, review.LEDGER = saved
            finally:
                shutil.rmtree(work, ignore_errors=True)
            for i in report.get("issues") or []:
                if str(i.get("rule_id")) != "FACT-001":
                    continue
                # 只认**事实库判的** FACT-001（tool=regex_check）。
                # 模型也能自己把 rule_id 写成 FACT-001——`validate` 只把"quote 里没有
                # 事实库实体"的降级成 FACT-002，quote 里有实体（如"豆包"）就原样保留，
                # 于是模型裸判混进了事实库的账本。后果实测到了：同一份 c08 连跑三次
                # 都不出 FACT-001，而 26 轮预演里偶发出一条 → `off_target` 会把它当成
                # "候选把对的判错了"（LLM 非确定性直接变成误杀好候选的证据）。
                if str(i.get("tool") or "").startswith("llm"):
                    continue
                matched = [lb["id"] for lb in labels
                           if eval_review.label_matches(lb, eval_review.issue_haystack(i), i)]
                if matched:
                    on.update((path.name, lid) for lid in matched)
                else:
                    off[(path.name, eval_review.normalize_issue(i))] = str(i.get("quote") or "")[:60]
    finally:
        retrieve.load_facts = orig
    return off, on


def shadow(cand, use_llm=None):
    """用「现有断言 + 候选」各跑一遍金标准语料，算**增量**贡献。

    `off_target` = |after_off - before_off|：只有"加进去之后才出现"的误报算候选的账。
    存量误报单独记在 `off_target_baseline`——不归咎候选，但必须报出来
    （那是金标准集自己的债，得另开工单，否则它会静默地吃掉所有候选）。
    """
    trial_assertion = to_assertion(cand)
    base = _read_json(FACTS, {"assertions": []})
    trial = copy.deepcopy(base)
    trial.setdefault("assertions", []).append(trial_assertion)

    mode = _shadow_llm(use_llm)
    all_cases = _corpus_cases()
    cases = [c for c in all_cases if _exercised_by(cand, c[2])]
    before_off, before_on = _shadow_pass(base, cases, mode)
    after_off, after_on = _shadow_pass(trial, cases, mode)

    delta = {k: v for k, v in after_off.items() if k not in before_off}
    on_new = sorted(after_on - before_on, key=str)
    return {
        "mode": "rule+llm" if mode else "rule-only",
        "corpus_total": len(all_cases),
        "corpus_exercised": len(cases),
        "corpus_skipped": len(all_cases) - len(cases),
        "on_target_hits": len(on_new),
        # 落盘给人看的是「语料:标注」，保留定位信息（JSON 里数组嵌套没人读得懂）
        "on_target_labels": [_label_key(x) for x in on_new],
        "off_target": len(delta),
        "off_target_detail": [{"case": k[0], "issue": k[1], "quote": v}
                              for k, v in sorted(delta.items())[:10]],
        "off_target_cases": sorted({k[0] for k in delta}),
        "off_target_baseline": len(before_off),
        "off_target_baseline_cases": sorted({k[0] for k in before_off}),
        "label_hits_before": len(before_on),
        "label_hits_after": len(after_on),
        "weak_signal": not on_new,
        "measurable": bool(cases),
    }


# ---- 安全线：数值也不能来自被审文档 ----
def _num_set(text):
    """文本里的数值集合（亿/万归一到同一量纲，同时保留原数）。

    两侧用同一个归一化函数，否则"5,000 万台"和"5000万"比不起来。
    """
    out = set()
    for m in _NUM_TOKEN.finditer(str(text or "")):
        raw = m.group(1).replace(",", "").replace("，", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        out.add(round(v, 6))
        unit = m.group(2) or ""
        if unit in SCALE:
            out.add(round(v * SCALE[unit], 6))
    return out


def _expected_numbers(cand):
    """候选期望值 → 可比较的数值集合。"""
    t = str(cand.get("type") or "").strip().lower()
    if t == "date":
        try:
            return {float(cand.get("expected_year"))}
        except (TypeError, ValueError):
            return set()
    exp = expected_of(cand)
    if not exp:
        return set()
    try:
        v = float(exp["num"])
    except (TypeError, ValueError):
        return set()
    out = {round(v, 6)}
    unit = str(exp.get("unit") or "")
    if unit in SCALE:
        out.add(round(v * SCALE[unit], 6))
    return out


def _value_from_target(cand):
    """期望值不能只出现在被审文档里。

    缺口清单里的 `evidence_quote` 是**被审文档原句**。人填草稿时最省事的做法
    就是把里面那个数字抄进 expected，再随手贴一个真 URL——`source_verifiable`
    照样放行。这就是"拿文档当链接"防住了、"拿文档当数字"没防住。

    判据不是"数值相同就拒"（那会误伤"来源与文档恰好都对"的情形），而是：
    数值与被审文档重合时，必须拿出**来源自己的原话** `source_quote`，
    且那段原话不能就是被审文档的句子。
    """
    quote = str(cand.get("evidence_quote") or "")
    if not quote:
        return True, ""
    hit = _expected_numbers(cand) & _num_set(quote)
    if not hit:
        return True, ""
    src = str(cand.get("source_quote") or "").strip()
    val = "、".join(f"{v:g}" for v in sorted(hit))
    if not src:
        return False, (f"期望值 {val} 与被审文档 evidence_quote 里的数字相同，"
                       "但没有 source_quote：无法证明这个数抄自来源而不是抄自被审文档")
    if not (_expected_numbers(cand) & _num_set(src)):
        return False, f"source_quote 里找不到期望值 {val}（来源原话必须包含这个数）"
    # 比对时去掉空白**和标点**：有人会顺手把「，」删掉再贴过来，
    # 只比空白的话同一句话能溜过去
    def norm(x):
        return re.sub(r"[\s，,。；;：:、“”\"'（）()]+", "", str(x))

    if norm(src) in norm(quote) or norm(quote) in norm(src):
        return False, "source_quote 与被审文档原句重复——被审对象不能当证据"
    return True, ""


VETO_KINDS = ("no_conflict", "off_target", "value_from_target",
              "source_from_target", "shadow_failed")
UNMEASURED_KINDS = ("unmeasured", "weak_signal")


def gate(cand, run_shadow=True, use_llm=None, allow_unmeasured=False):
    """门禁：全部通过才允许晋升。返回 {passed, verdict, reasons[], warnings[], metrics{}}。

    verdict 四档，**别混**（混了会把"草稿没填完"当成"思路被否决"）：
      pass        全部通过
      incomplete  草稿没填完（缺字段 / 缺来源标题 / 实体还没确认）——补完可重提
      rejected    实质否决（与现有断言冲突 / 把对的判错 / 数值来自被审文档）
      unverified  预演跑了但**测不出来**（语料没提到该实体，或零正贡献）
                  ——必须人显式 allow_unmeasured 才能继续，不允许默认放行
    """
    reasons, warnings, counts = [], [], {}

    def add(kind, msg):
        reasons.append(f"{kind}: {msg}")
        counts[kind] = counts.get(kind, 0) + 1

    for e in validate_schema(cand):
        add("schema", e)
    if not cand.get("entity_confirmed"):
        add("entity_confirmed", "entity 还没人工确认：草稿里的实体是正则猜的"
            "（`entity_confirmed: false`），猜错了后面所有冲突检查都按错实体走")
    ok, why, hard = _source_ok(cand)
    if not ok:
        add("source_from_target" if hard else "source_verifiable", why)
    ok, why = _value_from_target(cand)
    if not ok:
        add("value_from_target", why)
    ok, why = _type_ok(cand)
    if not ok:
        add("type_correct", why)
    ok, why = _check_conflict(cand, _read_json(FACTS, {"assertions": []}).get("assertions") or [])
    if not ok:
        add("no_conflict", why)

    metrics = {}
    # 实体/数值都还没定型的草稿跑预演毫无意义（还会把"测不出来"当成结论）
    blocked_by_draft = any(k in counts for k in ("schema", "entity_confirmed", "type_correct"))
    if not run_shadow:
        metrics = {"skipped": True}
        warnings.append("预演被显式跳过（--no-shadow）：本次**没有**负贡献证据，"
                        "通过只代表静态检查通过")
    elif blocked_by_draft:
        warnings.append("预演未运行：草稿还没填完，补齐后再跑 shadow 才有意义")
    else:
        try:
            metrics = shadow(cand, use_llm=use_llm)
        except Exception as e:                               # noqa: BLE001
            metrics = {"error": str(e)[:300]}
            add("shadow_failed", f"预演跑不起来（{e}）——没有负贡献证据就不能算通过")

    if metrics and not metrics.get("error") and not metrics.get("skipped"):
        if metrics.get("off_target", 0) > 0:
            detail = "；".join(f"{d['case']}「{d['quote'][:24]}」"
                              for d in (metrics.get("off_target_detail") or [])[:3])
            add("off_target", f"候选让 {metrics['off_target']} 条**正确**陈述变成误报"
                f"（{detail}）——误报零容忍")
        if metrics.get("off_target_baseline", 0) > 0:
            warnings.append(
                f"金标准集里本来就有 {metrics['off_target_baseline']} 条误报"
                f"（{'、'.join(metrics['off_target_baseline_cases'][:3])}），"
                "已从候选账上剔除；但这是金标准集自己的债，应另开工单")
        if metrics.get("on_target_hits", 0) == 0:
            if not metrics.get("measurable"):
                add("unmeasured", f"金标准语料里没有一份提到「{cand.get('entity')}」，"
                    "这次预演什么都没验到——**没测过不等于通过**。"
                    "要让这条断言可验，先补一条提到该实体的金标准用例"
                    "（tests/golden/cases/ + labels/）")
            else:
                add("weak_signal", f"在 {metrics.get('corpus_exercised')} 份提到该实体的语料上"
                    "零正贡献：既没找回真错误，也没制造误报（语料没覆盖这个属性）。"
                    "补一条该属性的阳性用例，或人工放行")

    overridden = []
    if allow_unmeasured:
        overridden = [r for r in reasons if r.split(":", 1)[0] in UNMEASURED_KINDS]
        reasons = [r for r in reasons if r not in overridden]
        for r in overridden:
            warnings.append(f"人工放行（allow_unmeasured）：{r}")
        if overridden:
            metrics = dict(metrics, override="allow_unmeasured",
                           overridden=len(overridden))

    kinds = {r.split(":", 1)[0] for r in reasons}
    if not reasons:
        verdict = "pass"
    elif kinds & set(VETO_KINDS):
        verdict = "rejected"
    elif kinds & set(UNMEASURED_KINDS):
        verdict = "unverified"
    else:
        verdict = "incomplete"
    return {"passed": verdict == "pass", "verdict": verdict,
            "reasons": reasons, "warnings": warnings, "metrics": metrics}

# ---------------------------------------------------------------------------
# 写入（快照 → 只增 → 升版本 → 记账）
# ---------------------------------------------------------------------------
def to_assertion(cand):
    a = {
        "entity": str(cand.get("entity") or "").strip(),
        "attr": str(cand.get("attr") or "").strip(),
        "type": str(cand.get("type") or "number").strip().lower(),
        "rule_id": cand.get("rule_id") or "FACT-001",
        "note": str(cand.get("note") or "").strip(),
    }
    if a["type"] in ("number", "max"):
        exp = expected_of(cand) or {"num": 0, "unit": ""}
        a["expected"] = exp
        if a["type"] == "number":
            a["tolerance"] = float(cand.get("tolerance") or 2)
        else:
            a["unit_hint"] = cand.get("unit_hint")
    elif a["type"] == "date":
        a["expected_year"] = int(cand["expected_year"])
    elif a["type"] == "text":
        a["expected_text"] = cand.get("expected_text")
    for k in ("label", "year_ctx", "exclude_ctx"):
        if cand.get(k):
            a[k] = cand[k]
    # 溯源字段：半年后必须能回答"这条断言是谁、什么时候、依据哪一页写进来的"
    a.update({
        "candidate_id": cand.get("candidate_id"),
        "source_url": cand.get("source_url") or "",
        "source_title": cand.get("source_title") or "",
        "retrieved_at": cand.get("retrieved_at") or "",
        "effective_date": cand.get("effective_date") or "",
        "review_due": cand.get("review_due") or "",
        "promoted_by": cand.get("promoted_by") or "",
        "promoted_at": cand.get("promoted_at") or "",
    })
    return a


def snapshot(note=""):
    """快照 knowledge/ → state/knowledge_snapshots/<ver>__<ts>/，只留最近 N 份。"""
    d = Path(SNAP_DIR)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = d / f"{knowledge_version()}__{stamp}"
    shutil.copytree(KDIR, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    keep = sorted([p for p in d.iterdir() if p.is_dir()])[-KEEP_SNAPSHOTS:]
    for p in d.iterdir():
        if p.is_dir() and p not in keep:
            shutil.rmtree(p, ignore_errors=True)
    if note:
        (dest / "_note.txt").write_text(note, encoding="utf-8")
    return str(dest)


def _log(row):
    p = Path(LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = dict(row, ts=_now())
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _assertion_key(a):
    """去重键：同实体同属性同类型同年份口径，期望值也相同 = 同一条断言。"""
    return (a.get("entity"), a.get("attr"), a.get("type"),
            str(a.get("year_ctx") or ""),
            json.dumps(a.get("expected") or a.get("expected_year")
                       or a.get("expected_text"), ensure_ascii=False, sort_keys=True))


def _apply(new_assertions, by, op, note="", candidate_id="", metrics=None,
           from_gap=""):
    """把断言写进 facts.json：快照 → 只增 → 升版本 → 记账。返回审计行。

    幂等：与现有断言完全相同的条目会被跳过（批量导入反复跑不会把库撑大）。
    """
    if not by:
        raise ValueError("必须给 --by（谁写的这条事实，半年后要能问）")
    facts = _read_json(FACTS, {"version": "事实库", "updated": _today(),
                               "assertions": [], "watchlist": []})
    have = {_assertion_key(a) for a in facts.get("assertions") or []}
    fresh = [a for a in new_assertions if _assertion_key(a) not in have]
    if not fresh:
        return {"op": op, "skipped": "已存在相同断言", "written": 0,
                "version_before": knowledge_version(),
                "version_after": knowledge_version()}
    before = knowledge_version()
    snap = snapshot(f"{op} by {by}: {note[:120]}")
    # 先升版本再写内容：万一崩在两步之间，历史报告会变「黄（依据已变）」，
    # 而不是「红（知识库被篡改）」——后者是在指控用户篡改，代价高得多。
    after = bump_version(before)
    Path(VERSION_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(VERSION_FILE).write_text(after + "\n", encoding="utf-8")
    facts.setdefault("assertions", []).extend(fresh)
    facts["updated"] = _today()
    _write_json(FACTS, facts)
    row = {
        "op": op,
        "candidate_id": candidate_id,
        "written": len(fresh),
        "entities": [f"{a.get('entity')}·{a.get('attr')}" for a in fresh],
        "by": by,
        "source": [a.get("source_url") for a in fresh],
        "version_before": before,
        "version_after": after,
        "snapshot": snap,
        "metrics": metrics or {},
        "note": note[:300],
    }
    _log(row)
    return row


def _bulk_conflicts(items, existing):
    """批量导入的静态检查：不许造出"同实体同属性两个值"的断言。

    批量路径跑不了 shadow（一条候选要跑两轮全语料，导入 20 条就是 40 轮），
    但**冲突检查不能省**：两个相互矛盾的断言同时进库，`fact_review` 会拿两个
    不同的期望值去判同一句话，结论取决于谁先被遍历到。
    """
    errs, pool = [], list(existing)
    for n, it in enumerate(items, 1):
        ok, why = _check_conflict(it, pool)
        if not ok:
            errs.append(f"第 {n} 行（{it.get('entity')}·{it.get('attr')}）：{why}")
        pool.append(it)
    return errs


def bulk_append(items, by, note="批量导入"):
    """批量追加（fact_import 走这里）：同样快照 + 升版本 + 记账。

    **不跑 shadow**：代价是 N 条候选 × 2 轮全语料。这一点会写进审计行
    （`metrics.shadow = "skipped-bulk"`），别把"批量导入了"读成"逐条验过了"。
    想逐条验，走 `fact_gap draft` → `gate` → `promote` 那条路。
    """
    if not by:
        raise ValueError("必须给 --by（谁写的这些事实，半年后要能问）")
    facts = _read_json(FACTS, {"assertions": []})
    errs = _bulk_conflicts(items, facts.get("assertions") or [])
    if errs:
        raise ValueError("批量导入会造出自相矛盾的断言（同实体同属性两个值），已拒绝：\n  "
                         + "\n  ".join(errs[:10]))
    return _apply(items, by=by, op="bulk-append", note=note,
                  metrics={"shadow": "skipped-bulk"})


def replace_all(items, by, note="批量覆盖", expect_current=None, allow_shrink=False):
    """批量覆盖：**会删掉旧断言**，只在初始化/迁移时用。

    之所以还留着这个口子：手工编辑 facts.json 更容易出错（且不留审计）。
    但必须快照 + 升版本 + 记账，否则历史报告会从「黄（依据已变）」变成
    「红（被篡改）」——那是在指控用户篡改知识库。
    watchlist 必须原样保留：它是"刻意不写断言"的清单，覆盖掉等于失去这层说明。

    与"只增不改不删"的原则冲突，所以加了三条硬约束——**别把它当普通导入用**：
      1. `expect_current` 必填，且必须等于当前条数（乐观锁：别人改过就重来）；
      2. 条数减少必须显式 `allow_shrink`（防"传错文件把库清了"）；
      3. 空列表一律拒绝（清空事实库不是一次"导入"，是事故）。
    """
    if not by:
        raise ValueError("必须给 --by")
    facts = _read_json(FACTS, {"assertions": [], "watchlist": []})
    current = facts.get("assertions") or []
    if expect_current is None:
        raise ValueError(
            f"replace 会删掉现有 {len(current)} 条断言：必须给 expect_current={len(current)} "
            "做二次确认（防误操作清空事实库）")
    if int(expect_current) != len(current):
        raise ValueError(
            f"当前事实库有 {len(current)} 条，与 expect_current={expect_current} 不符——"
            "可能有人在此期间改过，请重新读取后再确认")
    if not items:
        raise ValueError("replace 拒绝空列表：清空事实库不是导入，是事故")
    if len(items) < len(current) and not allow_shrink:
        raise ValueError(
            f"replace 会让断言从 {len(current)} 条降到 {len(items)} 条；"
            "「只增不改」是原则，真要删减请显式加 allow_shrink 并说明原因")
    before = knowledge_version()
    snap = snapshot(f"replace by {by}: {note[:120]}")
    after = bump_version(before)
    Path(VERSION_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(VERSION_FILE).write_text(after + "\n", encoding="utf-8")
    removed = len(facts.get("assertions") or [])
    facts["assertions"] = list(items)
    facts["updated"] = _today()
    _write_json(FACTS, facts)
    row = {"op": "bulk-replace", "entities": [f"{a.get('entity')}·{a.get('attr')}"
                                              for a in items],
           "by": by, "removed": removed, "written": len(items),
           "version_before": before, "version_after": after,
           "snapshot": snap, "note": note[:300]}
    _log(row)
    return row


def promote(cid, by, confirm_manual=False, skip_shadow=False,
            use_llm=None, allow_unmeasured=False):
    """晋升：门禁通过 + 人工确认 + 写库。**没有自动通道。**"""
    if not confirm_manual:
        raise PermissionError(
            "fact 维度只能人工晋升：加 --confirm-manual 表示你已核对过来源与数值。"
            "（存错一条事实会污染未来所有判断，比漏检隐蔽十倍）")
    cand = load_candidate(cid)
    if cand.get("status") == "promoted":
        raise ValueError(f"{cid} 已晋升过（{cand.get('promoted_at')}），不要重复写入")
    g = gate(cand, run_shadow=not skip_shadow, use_llm=use_llm,
             allow_unmeasured=allow_unmeasured)
    cand["gate"] = {"passed": g["passed"], "verdict": g["verdict"],
                    "reasons": g["reasons"], "warnings": g["warnings"],
                    "mode": (g.get("metrics") or {}).get("mode")}
    cand["metrics"] = g.get("metrics") or {}
    if not g["passed"]:
        # 状态必须区分"草稿没填完"和"实质被否决"：混成一个 rejected，
        # 人审队列里就分不出"待补"和"别再提了"
        cand["status"] = g["verdict"] if g["verdict"] in ("incomplete", "unverified") else "rejected"
        cand.setdefault("history", []).append({"at": _now(), "op": "gate-" + g["verdict"],
                                               "reasons": g["reasons"]})
        save_candidate(cand)
        raise ValueError({"incomplete": "草稿还没填完：",
                          "unverified": "预演测不出来（不是通过）：",
                          "rejected": "门禁否决："}[g["verdict"]]
                         + "；".join(g["reasons"]))
    cand["promoted_by"] = by
    cand["promoted_at"] = _now()
    row = _apply([to_assertion(cand)], by=by, op="promote",
                 note=cand.get("note") or "", candidate_id=cid,
                 metrics=g.get("metrics"), from_gap=cand.get("from_gap") or "")
    cand["status"] = "promoted"
    cand["version_after"] = row["version_after"]
    cand["snapshot"] = row["snapshot"]
    cand.setdefault("history", []).append({"at": _now(), "op": "promote",
                                           "by": by, "version": row["version_after"]})
    save_candidate(cand)
    # 缺口闭环：这条缺口现在有依据了
    gid = cand.get("from_gap")
    if gid:
        try:
            import fact_gap
            fact_gap.resolve([gid], by_candidate=cid, path=Path(GAP_LOG))
        except Exception:                                     # noqa: BLE001
            pass
    return row


def rollback(cid, by, reason=""):
    """回滚：按 candidate_id 删掉断言（不是改值），升版本，记账。"""
    if not by:
        raise ValueError("必须给 --by")
    facts = _read_json(FACTS, {"assertions": []})
    keep = [a for a in facts.get("assertions", []) if a.get("candidate_id") != cid]
    removed = len(facts.get("assertions", [])) - len(keep)
    if removed == 0:
        raise ValueError(f"facts.json 里没有 candidate_id={cid} 的断言（可能已回滚过）")
    before = knowledge_version()
    snap = snapshot(f"rollback {cid} by {by}")
    after = bump_version(before)
    Path(VERSION_FILE).write_text(after + "\n", encoding="utf-8")
    facts["assertions"] = keep
    facts["updated"] = _today()
    _write_json(FACTS, facts)
    row = {"op": "rollback", "candidate_id": cid, "removed": removed, "by": by,
           "version_before": before, "version_after": after, "snapshot": snap,
           "note": reason[:300]}
    _log(row)
    try:
        cand = load_candidate(cid)
        cand["status"] = "rolled_back"
        cand.setdefault("history", []).append({"at": _now(), "op": "rollback",
                                               "by": by, "reason": reason})
        save_candidate(cand)
    except FileNotFoundError:
        pass
    return row


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_status(args):
    v = knowledge_version()
    facts = _read_json(FACTS, {"assertions": []})
    cands = list_candidates()
    by_status = {}
    for c in cands:
        by_status[c.get("status")] = by_status.get(c.get("status"), 0) + 1
    print(f"知识库版本：{v}")
    print(f"事实断言：{len(facts.get('assertions') or [])} 条"
          f"｜watchlist：{len(facts.get('watchlist') or [])} 条")
    try:
        import fact_gap
        gaps = fact_gap.load(Path(GAP_LOG))
        s = fact_gap.stats(gaps)
        print(f"未解决缺口：{s['unresolved']} 条（累计出现 {s['mentions']} 次）"
              f"｜{'、'.join(f'{k} {v}' for k, v in s['kind'].items()) or '—'}")
    except Exception as e:                                    # noqa: BLE001
        print(f"缺口清单不可读：{e}")
    print(f"候选队列：{len(cands)} 条 {by_status or ''}")
    print(f"晋升审计：{sum(1 for r in _read_json_lines(LOG) if r.get('op') == 'promote')} 条"
          f"｜回滚：{sum(1 for r in _read_json_lines(LOG) if r.get('op') == 'rollback')} 条")
    print("\n审批口径：fact 维度**只能人工**（--confirm-manual），没有自动通道。")
    return 0


def _read_json_lines(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def cmd_list(args):
    cands = list_candidates()
    if not cands:
        print("候选队列为空。用 tools/fact_gap.py draft <gap_id> 从缺口起草。")
        return 0
    for c in cands:
        mark = {"proposed": "待填", "incomplete": "草稿未填完", "unverified": "测不出来",
                "passed": "待审", "rejected": "被否决",
                "promoted": "已晋升", "rolled_back": "已回滚"}.get(c.get("status"), "?")
        print(f"{c['candidate_id']}  [{mark}]  {c.get('entity') or '(空)'}·"
              f"{c.get('attr') or '(空)'}  type={c.get('type')}")
        if c.get("gate"):
            v = c["gate"].get("verdict")
            head = VERDICT_MARK.get(v, "通过" if c["gate"].get("passed") else "拒绝")
            mode = c["gate"].get("mode")
            if (c.get("metrics") or {}).get("override"):
                head += "·人工放行"
            print(f"    门禁：{head}" + (f"（预演 {mode}）" if mode else "")
                  + (f"｜{c['gate']['reasons'][0][:80]}" if c["gate"].get("reasons") else ""))
    return 0


def cmd_show(args):
    print(json.dumps(load_candidate(args.candidate_id), ensure_ascii=False, indent=1))
    return 0


VERDICT_MARK = {"pass": "通过", "incomplete": "草稿未填完",
                "unverified": "测不出来（未通过）", "rejected": "否决"}


def cmd_gate(args):
    cand = load_candidate(args.candidate_id)
    g = gate(cand, run_shadow=not args.no_shadow,
             use_llm=False if args.rule_only else None,
             allow_unmeasured=args.allow_unmeasured)
    cand["gate"] = {"passed": g["passed"], "verdict": g["verdict"],
                    "reasons": g["reasons"], "warnings": g["warnings"],
                    "mode": (g.get("metrics") or {}).get("mode")}
    cand["metrics"] = g.get("metrics") or {}
    if cand.get("status") in ("proposed", "passed", "rejected", "incomplete", "unverified"):
        cand["status"] = ("passed" if g["passed"]
                          else g["verdict"] if g["verdict"] in ("incomplete", "unverified")
                          else "rejected")
    save_candidate(cand)
    print(f"{args.candidate_id} 门禁：{VERDICT_MARK[g['verdict']]}")
    for r in g["reasons"]:
        print(f"  ✗ {r}")
    for w in g["warnings"]:
        print(f"  ! {w}")
    m = g.get("metrics") or {}
    if m and not m.get("skipped") and not m.get("error"):
        print(f"  预演（{m.get('mode')}）：语料 {m.get('corpus_total')} 份中"
              f"压到 {m.get('corpus_exercised')} 份（跳过 {m.get('corpus_skipped')} 份未提该实体）")
        print(f"    on_target {m.get('on_target_hits')}"
              f"｜off_target 增量 {m.get('off_target')}"
              f"（存量 {m.get('off_target_baseline')} 条不归咎本候选）")
    return 0 if g["passed"] else 1


def cmd_shadow(args):
    cand = load_candidate(args.candidate_id)
    m = shadow(cand, use_llm=False if args.rule_only else None)
    print(f"{args.candidate_id} shadow 预演（{m['mode']}，"
          f"{m['corpus_total']} 份语料中压到 {m['corpus_exercised']} 份，"
          f"跳过 {m['corpus_skipped']} 份未提「{cand.get('entity')}」）")
    print(f"  标注命中：{m['label_hits_before']} → {m['label_hits_after']}"
          f"（正贡献 {m['on_target_hits']}）")
    print(f"  负贡献（增量）：{m['off_target']}"
          + (f" ← 涉及 {'、'.join(m['off_target_cases'][:3])}" if m["off_target"] else ""))
    print(f"  存量误报（不归咎本候选）：{m['off_target_baseline']}"
          + (f" ← {'、'.join(m['off_target_baseline_cases'][:3])}"
             if m["off_target_baseline"] else ""))
    if not m["measurable"]:
        print("  ! 语料里没有任何一份提到该实体：**这次什么都没验到**")
    elif m["weak_signal"]:
        print("  ! 零正贡献（语料没覆盖这个属性）：未通过，需 --allow-unmeasured 人工放行")
    return 0 if m["off_target"] == 0 and m["measurable"] and not m["weak_signal"] else 1


def cmd_promote(args):
    if args.auto:
        print("拒绝：fact 维度没有自动通道。事实错了会污染未来所有判断，"
              "必须人工核对来源后用 --confirm-manual。")
        return 2
    try:
        row = promote(args.candidate_id, by=args.by,
                      confirm_manual=args.confirm_manual, skip_shadow=args.no_shadow,
                      use_llm=False if args.rule_only else None,
                      allow_unmeasured=args.allow_unmeasured)
    except (PermissionError, ValueError, FileNotFoundError) as e:
        print(f"未晋升：{e}")
        return 1
    print(f"已晋升 {args.candidate_id} → knowledge/facts.json")
    print(f"  知识库版本：{row['version_before']} → {row['version_after']}")
    print(f"  快照：{row['snapshot']}")
    print(f"  审批：人工确认（by {args.by}）")
    print("\n注意：事实库版本变了，历史报告会转 stale（黄），需重新审查才能用新断言。")
    return 0


def cmd_confirm(args):
    """确认实体：把正则猜出来的 `entity` 改成你核对过的写法。

    草稿里 `entity` 是 `fact_gap.guess_entity` 从句子剥出来的，**可能剥错**
    （「华为」被剥成「华」这类）。门禁要求 `entity_confirmed: true`，但没有
    正经入口时人只会去手工改 JSON——改了还不留痕。这条命令就是那个入口。
    """
    cand = load_candidate(args.candidate_id)
    old = cand.get("entity")
    new = (args.entity or "").strip() or old
    if not new:
        print("必须给 --entity（规范写法，如「华为」「微信」）")
        return 2
    cand["entity"] = new
    cand["entity_confirmed"] = True
    cand["entity_confirmed_by"] = args.by
    cand["entity_confirmed_at"] = _now()
    cand.setdefault("history", []).append(
        {"at": _now(), "op": "confirm-entity", "by": args.by,
         "from": old, "to": new})
    save_candidate(cand)
    print(f"{args.candidate_id} 实体已确认：「{old}」→「{new}」（by {args.by}）")
    print("  注意：改实体后冲突检查的对象也变了，请重新 gate。")
    return 0


def cmd_rollback(args):
    try:
        row = rollback(args.candidate_id, by=args.by, reason=args.reason or "")
    except (ValueError, FileNotFoundError) as e:
        print(f"回滚失败：{e}")
        return 1
    print(f"已回滚 {args.candidate_id}：移除 {row['removed']} 条断言")
    print(f"  知识库版本：{row['version_before']} → {row['version_after']}")
    print(f"  快照：{row['snapshot']}")
    return 0


def cmd_log(args):
    rows = _read_json_lines(LOG)
    if not rows:
        print("暂无晋升/回滚记录")
        return 0
    for r in rows[-args.limit:]:
        print(f"{r.get('ts')}  {r.get('op'):9} {r.get('candidate_id') or ''} "
              f"{'、'.join(r.get('entities') or [])[:40]}  v{r.get('version_before')}"
              f"→{r.get('version_after')}  by {r.get('by')}")
        if r.get("note"):
            print(f"    {r['note'][:120]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="事实断言晋升（唯一写 facts.json 的入口）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("log").add_argument("--limit", type=int, default=20)

    def _shadow_flags(p):
        p.add_argument("--no-shadow", action="store_true",
                       help="跳过语料预演（正式晋升不要跳：跳了就没有负贡献证据）")
        p.add_argument("--rule-only", action="store_true",
                       help="预演只跑规则（快，但**不是生产模式**，结论要标出来）")
        p.add_argument("--allow-unmeasured", action="store_true",
                       help="语料压不到该实体时人工放行（会写进 metrics 与审计）")

    for name in ("show", "gate", "shadow"):
        p = sub.add_parser(name)
        p.add_argument("candidate_id")
        if name != "show":
            _shadow_flags(p)

    p = sub.add_parser("promote")
    p.add_argument("candidate_id")
    p.add_argument("--by", required=True)
    p.add_argument("--confirm-manual", action="store_true",
                   help="人工确认已核对来源与数值（fact 维度必须）")
    p.add_argument("--auto", action="store_true", help="（会被拒绝）fact 不支持自动晋升")
    _shadow_flags(p)

    p = sub.add_parser("rollback")
    p.add_argument("candidate_id")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("confirm-entity")
    p.add_argument("candidate_id")
    p.add_argument("--entity", default="", help="核对后的实体写法（省略=沿用草稿猜测）")
    p.add_argument("--by", required=True)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    return {
        "status": cmd_status, "list": cmd_list, "show": cmd_show,
        "gate": cmd_gate, "shadow": cmd_shadow, "promote": cmd_promote,
        "rollback": cmd_rollback, "log": cmd_log,
        "confirm-entity": cmd_confirm,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
