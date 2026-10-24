#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server_sync_review.py —— /api/review 同步审查闭环 + file 越界"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def post(path, payload):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


print("A /api/review 同步审查 OCR 派生文件:")
st, body = post("/api/review", {"file": "targets/uploads/text.image.txt", "llm": False})
print(f"  status={st} {body[:300]}")
try:
    j = json.loads(body)
    if j.get("ok") and j.get("report_id"):
        rid = j["report_id"]
        issues = j.get("report", {}).get("issues", [])
        print(f"  报告 {rid}：{len(issues)} 条异议")
        for it in issues[:4]:
            print(f"    [{it.get('severity')}] {it.get('rule_id')}: {it.get('evidence','')[:90]}")
except Exception:  # noqa: BLE001
    pass

print("\nB file 越界（../../server.py）:")
st, body = post("/api/review", {"file": "../../server.py", "llm": False})
print(f"  status={st} {body[:200]}")

print("\nC file 绝对路径（C:/Windows/win.ini）:")
st, body = post("/api/review", {"file": "C:/Windows/win.ini", "llm": False})
print(f"  status={st} {body[:200]}")
