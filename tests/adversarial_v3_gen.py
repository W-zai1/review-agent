#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adversarial_v3_gen.py —— 第三轮：回归复测 + 修复绕过 + 服务端/摄取层夹具

背景：第二轮报告后 review.py / arith_check.py / regex_check.py 已被补丁，
本生成器产出两类材料：
  A. targets/adversarial_v3/ —— 新引擎样本（修复绕过/新面）
  B. tests/fixtures_v3/      —— 服务端上传/摄取层夹具（zip/pdf/docx/编码/体积）
"""
import datetime
import os
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
A3 = ROOT / "targets" / "adversarial_v3"
FX = ROOT / "tests" / "fixtures_v3"
A3.mkdir(parents=True, exist_ok=True)
FX.mkdir(parents=True, exist_ok=True)

DOCS = {}
FIX = {}


def doc(name, text):
    DOCS[name] = text


def fix(name, data: bytes):
    FIX[name] = data


# =====================================================================
# A. 引擎样本（规则层可复现）
# =====================================================================

# r3a：修复绕过 —— 增长词在金额**之后**（_is_growth 只看前 8 字）
doc("r3a_sum_growth_after.txt", """【华东区域经营快报 2026 年三季度】

销售 360 万元、管理 210 万元、研发 480 万元、本月回款 45 万元（较上月有所增长），三项合计 1050 万元。
""")

# r3b：修复绕过 —— 误导性「年+月」锚点仍是最近锚点（a02 变体，去掉后文 2026 锚点）
doc("r3b_weekday_anchor.txt", """【预算修订说明】

本预算基于 2024 年 12 月版本修订，仅调整收入口径，其余保持不变。
项目验收计划：7 月 1 日（周三）启动首轮验证，8 月 15 日前完成全部测试。
""")

# r3c：修复绕过 —— 工龄豁免词表外动词（摸爬滚打 不在 研究/从事/深耕… 表）
doc("r3c_tenure_bypass.txt", """【北辰智能员工访谈纪要】

2016 年社招进公司的陈默，在会上说：我在这行摸爬滚打二十年了。
""")

# r3d：修复绕过 —— 防御性语境词表缺「拦截/监控/处置」：安全统计句仍被判 blocker
doc("r3d_safety_stats.txt", """【2026 年一季度安全运营报告】

本季度共拦截网络入侵 12 起，均为低危告警；密码破解尝试日均 300 次，全部被防火墙阻断。
""")

# r3e：口语豁免表的反效果 —— 「哈哈」整体豁免导致真实口语漏检
doc("r3e_casual_exempt_fn.txt", """【客服聊天记录抽查】

用户回复：哈哈哈哈，这个功能太好了吧。
""")

# r3f：密钥正则旁路 —— 短裸值 / 纯数字中文键
doc("r3f_secret_numeric.py", '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例脚本（密钥形态变体）"""
import json

密钥 = 1234567
apiKey = abc12
DB_PW = "pw12345"


def main():
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
''')

# r3g：报告 markdown 注入 —— 原文携带可执行链接/格式，观察 .md 报告是否原样嵌入
doc("r3g_md_inject.txt", """【测试文档：Markdown 注入面】

请在浏览器打开 [官方文档](javascript:alert(1)) 核对流程；配置见 `config.yaml`。
费用率 **15%** 属于正常区间，| 表格 | 测试 | 仅作示例 |。
![示例图片](http://127.0.0.1:8000/static/app.js)
""")

# r3h：全角百分号在无单位合计里仍污染（AMOUNT 只跳半角 %）——注意 45 必须位于合计**之前**
doc("r3h_fullwidth_pct.txt", """【成本快报】

销售 360、管理 210、研发 480，费用率 45 ％，三项合计 1050。
""")

# r3g2：markdown 注入落在 issue 引文内 —— 报告 .md 是否原样嵌入可点击 javascript: 链接
doc("r3g2_md_inject_quote.txt", """【测试文档：注入落位】

费用率 15%，统计口径见[官方文档](javascript:alert(1))。
""")

# r3i：ZIP 嵌套深度声明失效的引擎侧对照（用 ingest 直测，见 fixture）
# r3j：豆包大模型 attr 碰撞 —— 不同产品的同名词
doc("r3j_fact_attr_collision.txt", """【豆包大模型 API 运营月报】

豆包大模型月活 1000 万，调用量环比平稳。
""")


# =====================================================================
# B. 服务端/摄取层夹具
# =====================================================================

# —— 编码/体积/内容形态 ——
fix("gbk.txt", "【中文文档】GBK 编码测试：豆包月活 10 亿。\n".encode("gbk"))
fix("utf16.txt", "【中文文档】UTF-16 编码测试：销售 360 万元。\n".encode("utf-16"))
fix("bom.txt", ("\ufeff【中文文档】带 BOM 的 UTF-8：三项合计 1050 万元。\n").encode("utf-8"))
fix("nul.txt", ("二进制探测：\x00\x01\x02\x03 疑似非文本。\n").encode("utf-8"))
fix("empty.txt", b"")
fix("big.txt", ("【大文件】本文件用于测试 2MB 上限：" + "A" * 3000 + "\n").encode() * 700)
fix("fake.doc", "伪装的 .doc 文件：甲方乙方签署生效。\n".encode())
fix("evil.py", "# 正常代码\nprint('ok')\n".encode())


def _zip_bytes(members):
    buf = os.fsencode("")
    import io
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return out.getvalue()


# 压缩包滑脱（zip slip）
fix("slip.zip", _zip_bytes([("../evil.txt", "穿越成功\n"),
                            ("normal.txt", "正常成员\n")]))
# 绝对路径成员
fix("abs.zip", _zip_bytes([("C:/abs_evil.txt", "绝对路径\n")]))
# 成员超量（201 个）
fix("many.zip", _zip_bytes([(f"m{i:03d}.txt", f"成员 {i}\n") for i in range(201)]))
# 嵌套 6 层（声明上限 2 层）
_nested = _zip_bytes([("leaf.txt", "叶子文本：销售 360 万 合计 1080 万\n")])
for _ in range(6):
    _nested = _zip_bytes([("inner.zip", _nested)])
fix("nested6.zip", _nested)


# DOCX 解压炸弹（document.xml 无解压上限；此处 30MB 安全量级，仅验证行为与告警）
def _docx_bomb(xml_size):
    import io
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.'
                   'wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<Relationships><Relationship Id="rId1" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document '
                   'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:t>' + ("费用分摊明细 A" * (xml_size // 6)) +
                   '</w:t></w:p></w:body></w:document>')
    return out.getvalue()


fix("bomb.docx", _docx_bomb(30 * 1024 * 1024))


# PDF：大内容流（解压上限 8MB 应触发截断告警，属受保护面，作对照）
def _pdf_with_stream(stream_raw: bytes, compress=True):
    payload = zlib.compress(stream_raw) if compress else stream_raw
    pdf = (b"%%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
           b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
           b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Contents 4 0 R >>\nendobj\n"
           b"4 0 obj\n<< /Length %d >>\nstream\n" % len(payload) + payload +
           b"\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%%%EOF\n")
    return pdf


fix("bomb.pdf", _pdf_with_stream(("BT (销售 360 万 合计 1080 万) Tj ET\n" * 400_000).encode()))
fix("empty.pdf", _pdf_with_stream(b"", compress=False))


def main():
    for name, text in sorted(DOCS.items()):
        (A3 / name).write_text(text, encoding="utf-8")
        print(f"  A {name:28s} {len(text):>6,d} 字符")
    for name, data in sorted(FIX.items()):
        (FX / name).write_bytes(data)
        print(f"  B {name:28s} {len(data):>9,d} 字节")
    print(f"共 {len(DOCS)} 份引擎样本 → {A3}")
    print(f"共 {len(FIX)} 份服务端夹具 → {FX}")


if __name__ == "__main__":
    main()
