#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact_import.py —— 批量导入事实断言到 knowledge/facts.json

**这不是绕过门禁的后门**：写入与 `fact_promote.py` 走同一条治理路径
（快照 → 升版本 → 记账），只是省掉"逐条起草候选"的流程。
因此每一行都必须带来源与复查日，缺了直接拒绝——否则它就变成了
"往事实库灌来源不明数字"的快捷方式，而假事实比漏检隐蔽十倍。

支持 CSV（表头见下）或 JSON 数组：

    entity,attr,type,expected_num,expected_unit,expected_year,tolerance,rule_id,note,source_url,review_due
    豆包,月活,number,1,亿,,2,FACT-001,公开披露口径：约 1 亿量级,https://example.com/a,2027-01-01
    豆包,推出,date,,,,2023,FACT-001,2023 年推出,https://example.com/b,2027-01-01

用法：
    python tools/fact_import.py --csv my_facts.csv --by wang
    python tools/fact_import.py --csv my_facts.json --by wang --replace   # 覆盖式迁移
"""
import argparse
import csv
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTS_PATH = ROOT / "knowledge" / "facts.json"
sys.path.insert(0, str(ROOT / "tools"))
import fact_promote  # noqa: E402

REQUIRED_PROVENANCE = ("source_url", "review_due")


def _rel(path):
    """相对路径用于回显；指向仓库外时不要让工具崩（相对化只是显示，不是校验）。"""
    try:
        return str(Path(path).relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _number(value):
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def normalize(row):
    entity = str(row.get("entity", "")).strip()
    attr = str(row.get("attr", "")).strip()
    kind = str(row.get("type", "number")).strip().lower()
    if not entity or not attr:
        raise ValueError("entity / attr 不能为空")
    if kind not in ("number", "date"):
        raise ValueError(f"未知 type：{kind}（只支持 number / date）")
    item = {
        "entity": entity,
        "attr": attr,
        "type": kind,
        "rule_id": str(row.get("rule_id") or "FACT-001").strip(),
        "note": str(row.get("note") or "").strip(),
    }
    if kind == "number":
        num = _number(row.get("expected_num"))
        if num is None:
            raise ValueError("number 类型缺少 expected_num")
        item["expected"] = {
            "num": int(num) if num.is_integer() else num,
            "unit": str(row.get("expected_unit") or "").strip(),
        }
        item["tolerance"] = float(row.get("tolerance") or 2)
    else:
        year = str(row.get("expected_year") or "").strip()
        if not year:
            raise ValueError("date 类型缺少 expected_year")
        item["expected_year"] = int(year)
    # 溯源字段：没有它们，这条断言半年后无法复查，也不能进库
    for k in ("source_url", "source_title", "retrieved_at", "effective_date", "review_due"):
        if str(row.get(k) or "").strip():
            item[k] = str(row[k]).strip()
    return item


def load_rows(path):
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("assertions", [])
        return [normalize(row) for row in data]
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [normalize(row) for row in csv.DictReader(f)]


def main():
    parser = argparse.ArgumentParser(description="批量导入事实断言（与单条晋升同一条治理路径）")
    parser.add_argument("--csv", required=True, help="CSV 或 JSON 文件")
    parser.add_argument("--by", required=True, help="谁导入的（审计需要）")
    parser.add_argument("--replace", action="store_true",
                        help="覆盖已有断言（只在初始化/迁移时用；会快照并升版本）")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="replace 时允许条数减少（默认拒绝：只增不改是原则）")
    args = parser.parse_args()

    source = Path(args.csv)
    if not source.exists():
        print(f"找不到文件：{source}")
        return 2
    try:
        imported = load_rows(source)
    except Exception as e:
        print(f"解析失败：{e}")
        return 2

    # 每行都必须带来源与复查日：批量导入最容易变成"灌来源不明的数字"
    missing = []
    for n, item in enumerate(imported, 1):
        lack = [k for k in REQUIRED_PROVENANCE if not str(item.get(k) or "").strip()]
        if lack:
            missing.append(f"第 {n} 行（{item.get('entity')}·{item.get('attr')}）缺 {'、'.join(lack)}")
    if missing:
        print("拒绝导入：以下行缺少来源/复查日——没有出处的断言不许进库。")
        for m in missing[:10]:
            print(f"  · {m}")
        if len(missing) > 10:
            print(f"  … 共 {len(missing)} 行")
        return 2

    existing = []
    if not args.replace and FACTS_PATH.exists():
        try:
            existing = json.loads(FACTS_PATH.read_text(encoding="utf-8")).get("assertions", [])
        except Exception:
            existing = []

    merged = {}
    for item in existing:
        merged[(item.get("entity"), item.get("attr"), item.get("type"))] = item
    added = replaced = 0
    for item in imported:
        key = (item["entity"], item["attr"], item["type"])
        if key in merged:
            replaced += 1
        else:
            added += 1
        merged[key] = item
    written = list(merged.values()) if not args.replace else imported

    if args.replace:
        # replace 会删旧断言：把"当前条数"当作乐观锁传下去（别人改过就失败重来），
        # 条数变少还要再显式 --allow-shrink
        cur = 0
        if FACTS_PATH.exists():
            cur = len(json.loads(FACTS_PATH.read_text(encoding="utf-8")).get("assertions") or [])
        row = fact_promote.replace_all(written, by=args.by,
                                       note=f"批量覆盖 {source.name}",
                                       expect_current=cur,
                                       allow_shrink=args.allow_shrink)
    else:
        try:
            row = fact_promote.bulk_append(written, by=args.by,
                                           note=f"批量导入 {source.name}")
        except ValueError as e:
            print(f"拒绝导入：{e}")
            return 2

    print(json.dumps({
        "ok": True,
        "imported": len(imported),
        "added": added,
        "replaced": replaced,
        "total": len(written),
        "version": f"{row['version_before']} → {row['version_after']}",
        "snapshot": row.get("snapshot"),
        "file": _rel(FACTS_PATH),
    }, ensure_ascii=False, indent=2))
    if not row.get("written"):
        print("\n未写入：与现有断言完全相同的条目已跳过（幂等），版本未变。")
    else:
        print("\n注意：知识库版本已自增，历史报告会转 stale（黄），"
              "需重新审查才用上新断言。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
