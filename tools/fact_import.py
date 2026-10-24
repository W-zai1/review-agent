#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact_import.py —— 批量导入事实断言到 knowledge/facts.json

支持 CSV（表头见下）或 JSON 数组：

    entity,attr,type,expected_num,expected_unit,expected_year,tolerance,rule_id,note
    豆包,月活,number,1,亿,,2,FACT-001,公开披露口径：约 1 亿量级
    豆包,推出,date,,,,2023,FACT-001,2023 年推出

用法：
    python tools/fact_import.py --csv my_facts.csv
    python tools/fact_import.py --csv my_facts.json --replace
"""
import argparse
import csv
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTS_PATH = ROOT / "knowledge" / "facts.json"


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
    parser = argparse.ArgumentParser(description="批量导入事实断言")
    parser.add_argument("--csv", required=True, help="CSV 或 JSON 文件")
    parser.add_argument("--replace", action="store_true", help="替换而不是合并")
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

    payload = {
        "version": "事实库 v1",
        "updated": datetime.date.today().isoformat(),
        "assertions": list(merged.values()),
    }
    FACTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "imported": len(imported),
        "added": added,
        "replaced": replaced,
        "total": len(payload["assertions"]),
        "file": str(FACTS_PATH.relative_to(ROOT)).replace("\\", "/"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
