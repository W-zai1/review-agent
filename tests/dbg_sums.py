#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dbg_sums.py —— 调试 check_sums 内部：r3h 全角百分号污染为何未触发"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import arith_check as ac  # noqa: E402

samples = {
    "r3h": "销售 360、管理 210、研发 480，三项合计 1050，费用率 45 ％。",
    "r3h_half": "销售 360、管理 210、研发 480，三项合计 1050，费用率 45 %。",
    "r3a_single": "销售 360 万元、管理 210 万元、研发 480 万元、本月回款 45 万元（较上月有所增长），三项合计 1050 万元。",
}
for name, txt in samples.items():
    print(f"\n### {name}")
    for m in ac.TOTAL.finditer(txt):
        print(f"TOTAL 命中: 计数词={m.group(1)} 总计={m.group(2)} 单位={m.group(3)!r}")
        prefix = txt[:m.start()]
        print(f"  prefix={prefix!r}")
        for a in ac._amounts_in(prefix, m.group(3) or ""):
            g = ac._is_growth(prefix, a["start"])
            print(f"    amount {a['raw']!r} 值={a['value']} 单位={a['unit']!r} "
                  f"start={a['start']} growth={g}")
    print("  findings:", ac.check_sums(txt))
