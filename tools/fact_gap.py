#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact_gap.py —— 知识库缺口清单（docs/知识库自学习.md §8 第一步）

一句话：**让审查器自己承认"这次的判断库里没有依据"，并把它记下来。**

## 为什么要有它

在此之前，"该往 facts.json 补哪条断言"完全靠人回忆。审到库外实体时系统有两种表现：

- 沉默：facts.json 里没有对应断言 → 直接跳过，报告里看不出"这里本来该判"；
- 裸判：模型按参数记忆下一个判断，`tool=llm_api`，但库里没有任何东西支撑它。

两种情况都不留痕迹，于是事实库永远是手灌的那几条，覆盖率不涨。
本模块把这两种情形变成**可累加的工单**：`state/gap_log.jsonl`。

## 三条硬约束（与规则自学习同源）

1. **被审文档不是证据**。缺口里存的 `quote` 只是"待审主张"，每一行都写死
   `usable_as_evidence: false`。想拿它当依据，必须过 `tools/fact_promote.py`
   的门禁并人工确认来源（`source_url` 不能指向被审文档）。
2. **只增不改**。jsonl 只追加：重复出现追加一条 `op=repeat` 的旁证行，不原地改计数
   ——否则"第一次是什么时候出现的"这个信息就丢了。缺口被补上后追加 `op=resolve`。
3. **不写事实库**。本模块只碰 `state/`，没有 `knowledge/facts.json` 的写权限。

## 四类缺口（按"是否真的下过判断"排序）

| kind | 触发 | 缺什么 |
|---|---|---|
| `model_judgment` | 模型在 fact 维度下了判断（`tool` 含 llm），**包括模型自己写的 FACT-001** | facts.json 断言或 laws/ 条文 |
| `law_missing` | LAW-001 因库里缺条文没跑（如经济补偿/加班费） | knowledge/laws/ 对应条文 |
| `unsourced_claim` | FACT-002/003 命中：句内有可核验主张但无出处 | 可引用的断言（判真伪）或出处 |
| `watchlist_unasserted` | 语料点名了 watchlist 实体，库内刻意无断言 | 断言 + 时效口径 |

前两类是真缺口（系统在没有依据的情况下开了口）；后两类是潜在缺口（有主张可查，
但库里没弹药）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
GAP_LOG = STATE / "gap_log.jsonl"
CAND_DIR = STATE / "fact_candidates"

# 单份文档最多记多少条：缺口清单是"下一版要补什么"的工单，不是全文索引。
# 不设上限时，一份长报告能刷出上百条同类缺口，人看一眼就放弃（同"误报淹没有效信号"）。
MAX_PER_DOC = 20
# quote 截断：它来自被审文档，落盘越长，越等于把原文复制进 state/。
QUOTE_CHARS = 80

KIND_PRIORITY = {
    "model_judgment": 1,
    "law_missing": 2,
    "unsourced_claim": 3,
    "watchlist_unasserted": 4,
}
KIND_DESC = {
    "model_judgment": "模型在事实维度下了判断，库内无依据（裸判）",
    "law_missing": "法定量检查因缺条文未执行",
    "unsourced_claim": "句内有可核验主张，但原文无出处、库内无断言",
    "watchlist_unasserted": "点名了库里刻意不写断言的高频实体",
}

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_NUM = re.compile(r"\d[\d,.]*\s*[%％]?|\d+(?:\.\d+)?\s*[万亿]")
# 数字前面那段汉字通常是「实体 + 指标 + 动词」，去掉后两截才是实体。
# 「微信月活跃用户达到 14 亿」→ 微信；不剥就是「微信月活跃用户达到」这种不是实体的东西。
_VERB_TAIL = re.compile(
    r"(达到|达到约|超过|超出|约为|约合|约|高达|增至|增长到|增长至|增长|下降至|下降|"
    r"下滑至|下滑|突破|只有|仅有|共计|合计|一共|为|是|有|达|至|共|在|已)$")
_METRIC_TAIL = re.compile(
    r"(月活跃用户数|月活跃用户|月活用户|月活|日活跃用户|日活用户|日活|活跃用户|"
    r"用户规模|用户数|用户量|用户|出货量|销量|保有量|装机量|营收|收入|净利润|"
    r"市场份额|占有率|市占率|发行价|起售价|售价|价格|人数|规模|数量|总量|金额|"
    r"订单量|订单|台数|占比|增速|增长率|同比|环比|的|了)+$")
_LEAD = re.compile(r"^(据|根据|按照|统计|数据显示|报告显示|报告称|显示|表明|其中|另外|同时|目前|截至)+")
# 以时间词开头说明前面的数字是年份：「2024 年公司营收」剥到「公司」才是主体
_LEAD_TIME = re.compile(r"^(年|月|日|年度|全年|上半年|下半年|本年度|该年度|季度)+")


def gap_id(kind, payload):
    """缺口指纹：kind + 载荷（quote/note）归一化后的 sha256。

    刻意**不含 review_id**：同一句话在十份文档里出现，应该收成同一条缺口
    （计数 10），而不是十条各自为战的工单。
    """
    norm = re.sub(r"\s+", "", str(payload or ""))[:120]
    return "GAP-" + hashlib.sha256(f"{kind}|{norm}".encode("utf-8")).hexdigest()[:10]


def guess_entity(quote):
    """从句子猜一个"实体"提示（只给人看，不参与判定）。

    没有分词器，最省事且不容易出洋相的做法：取数字前那段汉字，逐层剥掉
    动词尾（达到/超过/为…）、指标尾（月活/出货量/营收…）和前缀（据/报告显示…）。
    **永远标 `entity_guess_is_confident: false`**——猜错了不能变成事实。
    """
    q = str(quote or "")
    cand = ""
    m = _NUM.search(q)
    if m:
        runs = _CJK_RUN.findall(q[:m.start()])
        if runs:
            cand = runs[-1]
    if not cand:
        runs = _CJK_RUN.findall(q)
        cand = max(runs, key=len) if runs else ""
    if not cand:
        return ""
    cand = _LEAD.sub("", _LEAD_TIME.sub("", cand))
    # 长句里第一个「的/了」之前通常是主体（「抖音的日活…」→ 抖音）
    for sep in ("的", "了"):
        if len(cand) > 6 and sep in cand:
            cand = cand.split(sep)[0]
    for _ in range(3):
        new = _METRIC_TAIL.sub("", cand)
        if new != cand and len(new) >= 2:
            cand = new
            continue
        m2 = _VERB_TAIL.search(cand)
        # 单字动词尾（为/是/有/达）容易误伤「华为」「有限」：剩下不足 2 字就不剥
        if m2 and len(cand) - len(m2.group(0)) >= 2:
            cand = cand[:m2.start()]
            continue
        break
    return cand.strip()[:12]


def _row(kind, *, review_id="", target="", rule_id="", quote="",
         why="", missing="", entity=None, ts=None, quote_chars=QUOTE_CHARS):
    q = "" if quote_chars <= 0 else str(quote or "").strip()[:quote_chars]
    # entity=None 表示"没给实体"，才去猜；显式给 "" 表示"这类缺口没有实体"，
    # 不能顺手从 note 里猜出「经济补偿未比对」这种假实体。
    guessed = guess_entity(q) if entity is None else entity
    payload = q or f"{kind}|{missing}|{guessed}"
    return {
        "op": "new",
        "gap_id": gap_id(kind, payload),
        "kind": kind,
        "priority": KIND_PRIORITY.get(kind, 9),
        "review_id": review_id,
        "target": target,
        "rule_id": rule_id,
        "quote": q,
        "quote_stored": bool(q),
        "entity_guess": guessed or "",
        "entity_guess_is_confident": False,
        "why": why,
        "what_is_missing": missing,
        # 这一行是这套机制的安全线：缺口里的一切都只是「待审主张」，
        # 谁要拿它当依据，必须走 fact_promote 的门禁 + 人工确认来源。
        "usable_as_evidence": False,
        "ts": ts or time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def collect(issues=None, unverified=None, law_skipped=None,
            review_id="", target="", quote_chars=QUOTE_CHARS, limit=MAX_PER_DOC):
    """从一次审查的结果里抽出缺口行（纯函数，不落盘，便于单测）。

    参数都是 Session / report 里已有的东西：
      issues     最终 issues（已排序、已归口折叠）
      unverified Session.unverified_hits（命中的 watchlist 实体）
      law_skipped Session.law_skipped（缺条文 / 法条库不可用）
    """
    rows, seen_quote = [], set()

    def push(row):
        if len(rows) >= limit:
            return False
        if row["gap_id"] in {r["gap_id"] for r in rows}:
            return False
        rows.append(row)
        return True

    for it in issues or []:
        if str(it.get("dimension") or "") != "fact":
            continue
        rid = str(it.get("rule_id") or "")
        quote = str(it.get("quote") or "")
        tool = str(it.get("tool") or "")
        if rid == "FACT-001" and "llm" not in tool.lower():
            # 事实库判的 FACT-001 自带依据，不是缺口。
            # 但**模型自己写的 FACT-001** 是裸判：`validate` 只把"quote 里没有事实库
            # 实体"的降级成 FACT-002，quote 里只要有实体（"豆包""华为"）就原样保留
            # rule_id。按 rid 一律跳过，等于把最该记的缺口漏掉——系统明明在没有依据
            # 的情况下开了口，缺口清单里却查不到。
            continue
        if "llm" in tool.lower() or it.get("open_discovery"):
            push(_row("model_judgment", review_id=review_id, target=target,
                      rule_id=rid, quote=quote, quote_chars=quote_chars,
                      why="模型以常识/记忆作出事实判断，规则库未提供可比对的断言",
                      missing="knowledge/facts.json 的同实体同属性断言，"
                              "或 knowledge/laws/ 的条文"))
            seen_quote.add(quote)
        elif rid in ("FACT-002", "FACT-003") and quote not in seen_quote:
            push(_row("unsourced_claim", review_id=review_id, target=target,
                      rule_id=rid, quote=quote, quote_chars=quote_chars,
                      why="句内有可核验主张，但原文未给出处，库内也没有可对照的断言",
                      missing="可引用的断言（用它判真伪）或权威出处（用它判出处）"))
            seen_quote.add(quote)

    for name in unverified or []:
        name = str(name or "").strip()
        if not name:
            continue
        push(_row("watchlist_unasserted", review_id=review_id, target=target,
                  rule_id="FACT-001", quote=name, quote_chars=quote_chars,
                  entity=name,
                  why="该实体在 knowledge/facts.json 的 watchlist 里，刻意未写断言",
                  missing="带口径与复查日的断言（注意：口径会随季度变，"
                          "写死容易变成过期误报源）"))

    for note in law_skipped or []:
        note = str(note or "").strip()
        if not note:
            continue
        push(_row("law_missing", review_id=review_id, target=target,
                  rule_id="LAW-001", quote=note, quote_chars=max(quote_chars, 120),
                  entity="",
                  why="法定量检查因为库里没有对应条文而没有执行（没查 ≠ 没问题）",
                  missing="knowledge/laws/ 里对应法律的条文（走 law_ingest.py 入库）"))
    return rows


# ---------------------------------------------------------------------------
# 落盘：只追加
# ---------------------------------------------------------------------------
def path_of(path=None):
    return Path(path) if path else Path(GAP_LOG)


def record(rows, path=None):
    """追加缺口行；返回 {new, repeat, path}。

    重复出现的缺口不新开一条，而是追加 `op=repeat`（旁证）。这样：
    「同一缺口被 8 份文档点到」= 该优先补，而首次出现时间不会被覆盖。
    """
    p = path_of(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    known = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("op") == "new" and row.get("gap_id"):
                known.add(row["gap_id"])
    new = repeat = 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(p, "a", encoding="utf-8") as f:
        for row in rows or []:
            if row.get("gap_id") in known:
                f.write(json.dumps({"op": "repeat", "gap_id": row["gap_id"],
                                    "review_id": row.get("review_id"),
                                    "ts": ts}, ensure_ascii=False) + "\n")
                repeat += 1
            else:
                known.add(row.get("gap_id"))
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                new += 1
    return {"new": new, "repeat": repeat, "path": str(p)}


def resolve(gap_ids, by_candidate="", path=None):
    """缺口被补上后登记一条 `op=resolve`（不删行：删了就不知道它曾存在过）。"""
    p = path_of(path)
    if not Path(p).exists():
        return 0
    ids = [g for g in (gap_ids or []) if g]
    if not ids:
        return 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(p, "a", encoding="utf-8") as f:
        for gid in ids:
            f.write(json.dumps({"op": "resolve", "gap_id": gid,
                                "by_candidate": by_candidate, "ts": ts},
                               ensure_ascii=False) + "\n")
    return len(ids)


def load(path=None, include_resolved=False):
    """折叠 jsonl → 缺口视图（次数、首末次出现、是否已解决）。"""
    p = path_of(path)
    if not Path(p).exists():
        return []
    rows, order = {}, []
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        gid = row.get("gap_id")
        if not gid:
            continue
        op = row.get("op") or "new"
        if op == "new":
            if gid not in rows:
                item = dict(row)
                item.update(seen=1, first_seen=row.get("ts"), last_seen=row.get("ts"),
                            resolved_by=[])
                rows[gid] = item
                order.append(gid)
        elif gid in rows:
            item = rows[gid]
            if op == "repeat":
                item["seen"] += 1
                item["last_seen"] = row.get("ts") or item["last_seen"]
            elif op == "resolve":
                item["resolved_by"].append(row.get("by_candidate") or "")
    out = []
    for gid in order:
        item = rows[gid]
        item["resolved"] = bool(item["resolved_by"])
        if item["resolved"] and not include_resolved:
            continue
        out.append(item)
    out.sort(key=lambda r: (r.get("resolved", False), r.get("priority", 9),
                            -r.get("seen", 1), r.get("first_seen") or ""))
    return out


def stats(rows):
    by_kind, by_entity = {}, {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        ent = r.get("entity_guess") or "（未识别）"
        by_entity[ent] = by_entity.get(ent, 0) + 1
    return {
        "total": len(rows),
        "unresolved": sum(1 for r in rows if not r.get("resolved")),
        "kind": by_kind,
        "top_entity": sorted(by_entity.items(), key=lambda kv: -kv[1])[:8],
        "mentions": sum(r.get("seen", 1) for r in rows),
    }


def render_table(rows, limit=30):
    if not rows:
        return "缺口清单为空（不是「没有缺口」，是「还没记录过」——先跑一次审查）"
    lines = ["| 优先级 | 缺口 | 类型 | 实体（自动猜） | 出现 | 首次 | 原文片段 |",
             "|---|---|---|---|---|---|---|"]
    for r in rows[:limit]:
        lines.append(
            f"| P{r['priority']} | `{r['gap_id']}` | {r['kind']} | "
            f"{r.get('entity_guess') or '—'} | {r.get('seen', 1)} | "
            f"{(r.get('first_seen') or '')[:10]} | "
            f"{(r.get('quote') or '').replace('|', '丨')[:36] or '—'} |")
    if len(rows) > limit:
        lines.append(f"| … | 另 {len(rows) - limit} 条 | | | | | |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 起草候选断言（只写 state/，不碰 facts.json）
# ---------------------------------------------------------------------------
def _cand_id(seed):
    return "FACT-CAND-" + hashlib.sha256(
        str(seed).encode("utf-8")).hexdigest()[:8].upper()


def draft(gap, cand_dir=None, by=""):
    """把一条缺口变成**待填**的候选断言草稿。

    草稿里 `expected` 空着、`source_url` 空着——这是刻意的：数值与来源只能由人
    从权威源抄进来，机器替不了。填完才能过 `fact_promote.py` 的门禁。
    """
    if gap.get("kind") == "law_missing":
        raise ValueError("法条缺口请走 tools/law_ingest.py 入库，不是事实断言")
    d = Path(cand_dir) if cand_dir else CAND_DIR
    d.mkdir(parents=True, exist_ok=True)
    cid = _cand_id(gap["gap_id"])
    payload = {
        "candidate_id": cid,
        "from_gap": gap["gap_id"],
        "entity": gap.get("entity_guess") or "",
        "entity_confirmed": False,
        "attr": "",
        "type": "number",
        "expected": None,
        "tolerance": None,
        "tolerance_required": True,
        "rule_id": "FACT-001",
        "source_url": "",
        "source_title": "",
        "retrieved_at": "",
        "effective_date": "",
        "review_due": "",
        "note": f"由缺口 {gap['gap_id']} 起草：{gap.get('why') or ''}"[:300],
        "evidence_quote": gap.get("quote") or "",
        "evidence_is_from_target": True,
        # 来源自己的原话（**必须自己抄，不能抄被审文档那句**）。
        # 只有当期望值和 evidence_quote 里的数字重合时才必须填——那是
        # "拿文档当数字"最容易发生的地方，见 fact_promote._value_from_target。
        "source_quote": "",
        "status": "proposed",
        "created_by": by or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = d / f"{cid}.json"
    if not path.exists():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload, path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="知识库缺口清单（只读 state/，不写事实库）")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["list", "stat", "show", "draft", "export", "resolve-all"])
    ap.add_argument("target", nargs="?", help="gap_id（show / draft 用）")
    ap.add_argument("--path", default=None, help="缺口文件（默认 state/gap_log.jsonl）")
    ap.add_argument("--kind", default=None, help="只看某一类缺口")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by", default="", help="draft 的创建人")
    args = ap.parse_args(argv)

    rows = load(args.path)
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]

    if args.cmd == "list":
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            s = stats(rows)
            print(f"未解决缺口 {s['unresolved']} 条（累计出现 {s['mentions']} 次）")
            for k, n in sorted(s["kind"].items(), key=lambda kv: KIND_PRIORITY.get(kv[0], 9)):
                print(f"  {k}: {n}  —— {KIND_DESC.get(k, '')}")
            print()
            print(render_table(rows, args.limit))
        return 0

    if args.cmd == "stat":
        s = stats(rows)
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0

    if args.cmd == "show":
        hit = next((r for r in rows if r["gap_id"] == args.target), None)
        if not hit:
            print(f"未找到 {args.target}（已解决的不在列表里，用 --json 也看不到）")
            return 1
        print(json.dumps(hit, ensure_ascii=False, indent=1))
        print("\n下一步：填 expected / source_url / review_due 后跑")
        print(f"  py -3 tools/fact_promote.py gate {hit['gap_id']}")
        return 0

    if args.cmd == "draft":
        hit = next((r for r in rows if r["gap_id"] == args.target), None)
        if not hit:
            print(f"未找到缺口 {args.target}")
            return 1
        try:
            payload, path = draft(hit, by=args.by)
        except ValueError as e:
            print(f"不能起草：{e}")
            return 1
        print(f"已起草候选断言 {payload['candidate_id']} → {path}")
        print("草稿里 expected / source_url 是空的，这是刻意的：数值与来源只能从权威源抄。")
        print(f"  1) 编辑 {path}")
        print(f"  2) py -3 tools/fact_promote.py gate {payload['candidate_id']}")
        print(f"  3) py -3 tools/fact_promote.py promote {payload['candidate_id']} --by <你> --confirm-manual")
        return 0

    if args.cmd == "resolve-all":
        if not args.target:
            print("需要候选 id：resolve-all FACT-CAND-XXXX")
            return 1
        n = resolve([r["gap_id"] for r in rows], by_candidate=args.target, path=args.path)
        print(f"已登记 {n} 条缺口为已解决（by {args.target}）")
        return 0

    if args.cmd == "export":
        md = ["# 知识库缺口清单", ""]
        s = stats(rows)
        md.append(f"- 未解决 {s['unresolved']} 条，累计出现 {s['mentions']} 次")
        for k, n in sorted(s["kind"].items(), key=lambda kv: KIND_PRIORITY.get(kv[0], 9)):
            md.append(f"- `{k}`：{n} 条 —— {KIND_DESC.get(k, '')}")
        md += ["", render_table(rows, 1000), "",
               "> 缺口只说明「当时没有库内依据」，不是「文中说法是错的」。",
               "> 补齐要走 `tools/fact_promote.py`（唯一写入口）+ 人工确认来源。"]
        print("\n".join(md))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
