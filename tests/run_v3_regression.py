#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_v3_regression.py —— 第三轮批量回归：旧 18 份 + 新 9 份，rule-only
输出：每样本裁定、报告 ID、异议总数与按维度/severity 统计。
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "review.py"
OUT = ROOT / "outputs"

SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "info": 3}


def run(sample: Path):
    p = subprocess.run([sys.executable, str(REVIEW), str(sample), "--no-llm"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT), timeout=180)
    tail = (p.stdout or "") + (p.stderr or "")
    rid = None
    for ln in tail.splitlines():
        if "R-2026" in ln and "裁定" in ln:
            rid = ln.split("[")[1].split("]")[0]
            verdict = ln.split("裁定=")[1].split(" |")[0]
            n = int(ln.split("异议 ")[1].split(" 条")[0])
            break
    else:
        return {"sample": sample.name, "rid": None, "verdict": "CRASH/ERR",
                "issues": 0, "detail": tail.strip()[-200:]}
    j = json.loads((OUT / f"{rid}.json").read_text(encoding="utf-8"))
    issues = j.get("issues", [])
    dim = {}
    sev = {}
    for it in issues:
        d = it.get("dimension", "?")
        s = it.get("severity", "?")
        dim[d] = dim.get(d, 0) + 1
        sev[s] = sev.get(s, 0) + 1
    return {"sample": sample.name, "rid": rid, "verdict": verdict,
            "issues": len(issues), "dim": dim, "sev": sev}


def main():
    groups = [("第二轮旧样本（回归）", ROOT / "targets" / "adversarial_v2"),
              ("第三轮新样本（绕过/新面）", ROOT / "targets" / "adversarial_v3")]
    all_rows = []
    for label, d in groups:
        print(f"\n=== {label} ===")
        for f in sorted(d.glob("*")):
            if f.suffix not in (".txt", ".py"):
                continue
            r = run(f)
            sev = " ".join(f"{k}:{v}" for k, v in sorted(r["sev"].items(),
                                                         key=lambda x: SEV_ORDER.get(x[0], 9)))
            dim = " ".join(f"{k}:{v}" for k, v in sorted(r["dim"].items()))
            print(f"{r['sample']:28s} {r['rid'] or '--':17s} 裁定={r['verdict']:5s} "
                  f"异议={r['issues']:3d}  {sev}  {dim}")
            all_rows.append(r)
    return all_rows


if __name__ == "__main__":
    main()
