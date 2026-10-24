#!/usr/bin/env python3
"""retrieve.py —— 知识库检索 (Demo 工具)

功能：对 knowledge/ 目录做关键词 grep 检索；加载机器可读事实库 facts.json。
可被 review.py 导入，也可 CLI 运行：
    python retrieve.py "豆包 MAU"
"""
import os
import sys
import json
import glob


def knowledge_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")


def grep_knowledge(query, kdir=None):
    kdir = kdir or knowledge_dir()
    q = (query or "").lower()
    hits = []
    if not q:
        return hits
    for path in sorted(glob.glob(os.path.join(kdir, "*"))):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if q in line.lower():
                    hits.append({"file": os.path.basename(path), "line": i, "text": line.strip()})
    return hits


def load_facts(path=None):
    if path is None:
        path = os.path.join(knowledge_dir(), "facts.json")
    if not os.path.exists(path):
        return {"assertions": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    q = " ".join(sys.argv[1:])
    print(json.dumps(grep_knowledge(q), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
