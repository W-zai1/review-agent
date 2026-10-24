#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_v3_details.py —— 抽取指定 R-ID 报告的 issue 明细与 coverage 信息"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"


def show(rid: str):
    j = json.loads((OUT / f"{rid}.json").read_text(encoding="utf-8"))
    print(f"\n===== {rid} 裁定={j.get('verdict')} 类型={j.get('target_type')} =====")
    cov = j.get("coverage") or {}
    if isinstance(cov, dict):
        print("  coverage:", json.dumps(cov, ensure_ascii=False)[:300])
    for it in j.get("issues", []):
        print(f"  [{it.get('severity')}] {it.get('rule_id')} "
              f"({it.get('dimension')}, conf={it.get('confidence')})")
        print(f"     原文: {it.get('quote', '')[:80]}")
        print(f"     依据: {it.get('evidence', '')[:160]}")


for rid in sys.argv[1:]:
    show(rid)
