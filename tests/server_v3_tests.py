#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server_v3_tests.py —— 服务端/摄取层第三轮打点（HTTP）v2
上传接口为裸字节 + ?name= 查询参数；健康检查用 /api/targets。
"""
import json
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures_v3"
BASE = "http://127.0.0.1:8000"
RESULTS = []


def http(method, path, data=None, headers=None, timeout=180):
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def upload(name_q, fname):
    """原始字节上传：body=文件内容，name 走查询参数（模拟服务器真实契约）"""
    blob = (FX / fname).read_bytes()
    return http("POST", f"/api/upload?name={urllib.parse.quote(name_q)}",
                data=blob, headers={"Content-Type": "application/octet-stream"},
                timeout=240)


CASES = [
    ("0a GET /api/targets (健康)", lambda: http("GET", "/api/targets")),

    # ---- 白名单 / 形态 ----
    ("1a 上传 .exe（白名单外）", lambda: upload("run.exe", "evil.py")),
    ("1b 上传 .doc（二进制→摄取）", lambda: upload("legacy.doc", "fake.doc")),
    ("1c 上传 .py（文本允许）", lambda: upload("evil.py", "evil.py")),

    # ---- 编码 / 体积 / 内容 ----
    ("2a GBK 文本（应拒）", lambda: upload("gbk.txt", "gbk.txt")),
    ("2b UTF-16 文本（应拒）", lambda: upload("utf16.txt", "utf16.txt")),
    ("2c BOM UTF-8（应剥离）", lambda: upload("bom.txt", "bom.txt")),
    ("2d NUL 文本（应拒）", lambda: upload("nul.txt", "nul.txt")),
    ("2e 空文件（应拒）", lambda: upload("empty.txt", "empty.txt")),
    ("2f 2.1MB 文本（应超限拒）", lambda: upload("big.txt", "big.txt")),

    # ---- 上传名清洗（穿越） ----
    ("2g name=..%2F..%2Fevil2.txt", lambda: upload("..\\..\\evil2.txt", "evil.py")),
    ("2h name=CON.txt（保留名）", lambda: upload("CON.txt", "evil.py")),

    # ---- zip 家族 ----
    ("3a zip slip（../ 成员）", lambda: upload("slip.zip", "slip.zip")),
    ("3b zip 绝对路径成员", lambda: upload("abs.zip", "abs.zip")),
    ("3c zip 201 成员（超 200）", lambda: upload("many.zip", "many.zip")),
    ("3d zip 嵌套 6 层（声明上限 2）", lambda: upload("nested6.zip", "nested6.zip")),

    # ---- docx / pdf ----
    ("4a docx 解压炸弹 30MB", lambda: upload("bomb.docx", "bomb.docx")),
    ("4b pdf 大流（8MB 上限）", lambda: upload("bomb.pdf", "bomb.pdf")),
    ("4c pdf 空流", lambda: upload("empty.pdf", "empty.pdf")),
    ("4d 图片 OCR（text.png）", lambda: upload("text.png", "text.png")),

    # ---- 报告读取 / 穿越 ----
    ("5a GET /api/report?id=R-20261024-079", lambda: http("GET", "/api/report?id=R-20261024-079")),
    ("5b GET /api/report?id=../R-20261024-079", lambda: http("GET", "/api/report?id=../R-20261024-079")),
    ("5c GET /api/report?id=..%2F..%2Fetc", lambda: http("GET", "/api/report?id=..%2F..%2Fetc")),

    # ---- Host 防护 ----
    ("6a Host: evil.com", lambda: http("GET", "/api/targets", headers={"Host": "evil.com"})),
    ("6b Host: 127.0.0.1:8000", lambda: http("GET", "/api/targets", headers={"Host": "127.0.0.1:8000"})),

    # ---- JSON body 无上限 ----
    ("7a POST 6MB JSON body", lambda: http("POST", "/api/review",
                                           data=json.dumps({"file": "x" * 6 * 1024 * 1024}).encode(),
                                           headers={"Content-Type": "application/json"},
                                           timeout=240)),

    # ---- DELETE 二次确认流 ----
    ("8a DELETE 无 confirm", lambda: http("DELETE", "/api/report?id=R-20261024-079",
                                          data=json.dumps({}).encode(),
                                          headers={"Content-Type": "application/json"})),
    ("8b DELETE 错误 confirm", lambda: http("DELETE", "/api/report?id=R-20261024-079",
                                           data=json.dumps({"confirm": "R-00000000-000"}).encode(),
                                           headers={"Content-Type": "application/json"})),
    ("8c DELETE 正确 confirm", lambda: http("DELETE", "/api/report?id=R-20261024-079",
                                           data=json.dumps({"confirm": "R-20261024-079"}).encode(),
                                           headers={"Content-Type": "application/json"})),
    ("8d GET 已删除报告", lambda: http("GET", "/api/report?id=R-20261024-079")),
    ("8e GET /api/integrity（删除后）", lambda: http("GET", "/api/integrity")),
]


def main():
    print("waiting server...")
    ok = False
    for _ in range(30):
        st, _ = http("GET", "/api/targets", timeout=5)
        if st == 200:
            ok = True
            break
        time.sleep(1)
    if not ok:
        print("server not reachable"); sys.exit(1)
    print("server ready\n")

    for name, fn in CASES:
        try:
            st, body = fn()
        except Exception as e:  # noqa: BLE001
            st, body = -1, f"{type(e).__name__}: {e}"
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        body = str(body).replace("\n", " ")[:170]
        RESULTS.append((name, st, body))
        print(f"{name:42s} status={st}  {body}")


if __name__ == "__main__":
    main()
