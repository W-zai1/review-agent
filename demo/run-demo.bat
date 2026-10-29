@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo === 1/2 劳动合同：整改单 ===
py -3 review.py demo\contract_labor.txt --no-llm --no-research
echo.
echo === 2/2 事实核验：小米 / 渗透率 / 豆包 ===
py -3 review.py demo\claim_report.txt --no-llm --research
echo.
echo 打开 outputs\ 下最新的 R-*.md 查看报告。
