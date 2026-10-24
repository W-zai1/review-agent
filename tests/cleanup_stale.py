#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cleanup_stale.py —— 通过正规 DELETE 流删除因样本重生成而过期的 R-068/R-075"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def req(method, path, payload=None):
    data = json.dumps(payload or {}).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return f"{e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


for rid in ("R-20261024-068", "R-20261024-075"):
    print(rid, "->", req("DELETE", f"/api/report?id={rid}", {"confirm": rid}))
