#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mk_ocr_img.py —— 生成 OCR 测试图：白底黑字中文，含植入的算术错误
目标：验证 upload->ingest->OCR(winrt)->review 链路；图中数字应被检出 LOGIC-001。
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures_v3" / "text.png"

lines = [
    "【2026 年 10 月财务简报】",
    "销售 360 万元、管理 210 万元、研发 480 万元，",
    "三项合计 1080 万元。",   # 真实合计应为 1050，植入错误
]
font_paths = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]
font = None
for fp in font_paths:
    if Path(fp).exists():
        try:
            font = ImageFont.truetype(fp, 44)
            break
        except Exception:
            continue
assert font, "无可用中文字体"

W, H = 900, 320
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)
y = 12
for ln in lines:
    d.text((12, y), ln, fill="black", font=font)
    y += 62
img.save(OUT)
print(f"OCR 测试图已生成: {OUT} ({img.size})")
