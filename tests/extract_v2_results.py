#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_v2_results.py —— 提取第二轮对抗测试全部报告的关键字段"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "outputs")


def main():
    for n in range(2, 19):
        rid = "R-20261024-%03d" % n
        p = os.path.join(OUT, rid + ".json")
        if not os.path.exists(p):
            print("=" * 10, rid, "(no report)")
            continue
        r = json.load(open(p, encoding="utf-8"))
        cov = r.get("coverage", {}) or {}
        td = cov.get("type_detection", {}) or {}
        print("=" * 10, rid, "| type=%s conf=%s | verdict=%s | mode=%s | chunks=%s" % (
            r.get("target_type"), td.get("confidence"), r.get("verdict"),
            cov.get("review_mode"), cov.get("chunks")))
        for i in r.get("issues", []):
            print("  [%s] %s %s conf=%s tool=%s" % (
                i["rule_id"], i["severity"], i["issue_id"], i["confidence"], i["tool"]))
            print("      q: %s" % i["quote"][:70].replace("\n", " "))
            print("      ev: %s" % i["evidence"][:170].replace("\n", " "))
            if i.get("suggestion"):
                print("      sg: %s" % i["suggestion"][:120].replace("\n", " "))


if __name__ == "__main__":
    main()
