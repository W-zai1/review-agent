#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stress_gen.py —— 生成长文本压力测试样本（可复现）

两档：
  - targets/stress_mid_001.txt   ~11,000 字符（12k 截断限内的极限长度）
  - targets/stress_long_001.txt  ~40,000 字符（超限，用于验证分块审查）

结构：core（复用对抗文本，含 E01-E14）→ 水段（**必须自洽**）→ tail（T1-T3）。

自洽水段的三个历史缺陷（已修，别再犯）：
  1. 费用率写死「约 15%」，而按生成公式实际是 22~23% —— 每段都自带算术错误，
     结果"水段"反而变成新的错误源，长文异议数被凭空放大；
  2. 区域/部门/项目按 i % len 循环复用，同一部门同月出现两套数字 —— 制造跨段矛盾；
  3. 金额未四舍五入（12*2.1 = 25.200000000000003）—— 浮点尾数被当成事实冲突。
现在生成后会跑 self_check()：水段若有任何算术发现/浮点尾数/主体重复，直接报错。
"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = (ROOT / "targets" / "adversarial_meeting_001.txt").read_text(encoding="utf-8").strip()

REGIONS = ["西南", "华东", "华中", "华北", "东北", "西北", "华南二部", "海外一部",
           "海外二部", "大湾区"]
ORDERS = [(1250, 4800), (980, 5200), (1500, 4100), (870, 5900), (1320, 4650),
          (760, 6100), (1430, 4380), (1050, 5300), (920, 5050), (1610, 3990)]
SPEAKERS = ["陈默", "周岚", "赵立", "孙倩", "钱进", "李诚"]
PROJS = ["晨曦", "瀚海", "云帆", "追光", "磐石", "惊雷", "长风", "春山", "揽月", "星河"]


def unique(base, i, span, suffix):
    """给循环复用的主体加编号：西南1区 / 西南2区 —— 避免同一主体两套数字。"""
    return f"{base}{i // span + 1}{suffix}"


def water_channel(i):
    orders, unit = ORDERS[i % len(ORDERS)]
    revenue = round(orders * unit / 10000, 1)
    region = unique(REGIONS[i % len(REGIONS)], i, len(REGIONS), "区")
    op_fee = round(revenue * 0.12, 1)
    head_fee = 40 + i % 9
    market_fee = round(op_fee * 0.3, 1)
    total = round(op_fee + head_fee + market_fee, 1)
    rate = round(total / revenue * 100, 1)      # 费用率必须由数据算出来
    sp = SPEAKERS[i % len(SPEAKERS)]
    return (f"## 议题 {10+i}：{region}渠道复盘（{sp}）\n\n"
            f"{sp}：{region}市场 10 月渠道数据：本月完成订单 {orders} 单，客单价 {unit} 元，"
            f"渠道收入 {revenue} 万元，环比增长 {8 + i % 9}%。新增渠道门店 {2 + i % 5} 家，"
            f"覆盖商场、社区两类场景，11 月计划再开 {1 + i % 4} 家。库存周转天数 {28 + i % 6} 天，"
            f"处于正常区间，暂无压货风险。渠道费用方面：门店运营 {op_fee} 万、人力 {head_fee} 万、"
            f"市场活动 {market_fee} 万，三项合计 {total} 万，费用率 {rate}%，与季度预算一致。\n\n"
            f"补充：{region}大客户续约情况稳定，10 月重点客户回访完成率 100%，"
            f"客户投诉率降至 0.8%，服务响应时效 4 小时内，各项指标均在年度目标内。"
            f"渠道伙伴季度会议定于 11 月中旬召开，议程与预算已报批。\n\n")


def water_proj(i):
    prog = 20 + i % 70
    done = (i % 6) + 1
    remain = 6 - done
    date = 11 + (i % 5)
    people = 12 + i % 8
    proj = unique(PROJS[i % len(PROJS)], i, len(PROJS), "期")
    sp = SPEAKERS[(i + 2) % len(SPEAKERS)]
    budget = round(people * 2.1, 1)             # 必须 round，否则出浮点尾数
    return (f"## 议题 {10+i}：{proj}项目进展（{sp}）\n\n"
            f"{sp}：{proj}项目当前整体进度 {prog}%，已完成 {done} 项里程碑，剩余 {remain} 项。"
            f"关键路径模块均已按时交付，下一阶段计划 11 月 {date} 日启动。人力投入 {people} 人，"
            f"月度预算约 {budget} 万元，与预算一致，无新增缺口。\n\n"
            f"风险登记：{proj}项目当前风险项 {1 + i % 3} 条，均为中低等级，已列入跟踪表。"
            f"每周五由项目组例会统一过一遍，重大事项两小时内上报管理层。测试环境已就绪，"
            f"联调窗口按计划推进，验收标准与项目合同附件一致。\n\n")


def water_finance(i):
    a, b, c = 30 + i, 8 + i % 5, 20 + i % 9
    total = a + b + c
    dept = unique(["市场", "研发", "供应链", "销售", "行政"][i % 5], i, 5, "科")
    sp = SPEAKERS[(i + 3) % len(SPEAKERS)]
    return (f"## 议题 {10+i}：费用补充说明（{sp}）\n\n"
            f"{sp}：{dept}本月费用：人力成本 {a} 万、差旅 {b} 万、市场活动 {c} 万，"
            f"三项合计 {total} 万元，均在季度预算范围内，无需额外追加。"
            f"上月结余结转 {1 + i % 3} 万，用于下月滚动安排。"
            f"报销单据已全部完成财务审核，未发现不合规项。\n\n")


def water_hr(i):
    n, m = 3 + i % 4, 1 + i % 2
    net = n - m
    sp = SPEAKERS[(i + 4) % len(SPEAKERS)]
    region = unique(REGIONS[(i + 3) % len(REGIONS)], i, len(REGIONS), "区")
    return (f"## 议题 {10+i}：招聘与到岗（{sp}）\n\n"
            f"{sp}：{region}团队本月到岗 {n} 人，离职 {m} 人，净增 {net} 人。"
            f"新员工培训周期两周，11 月中旬可全部投入业务。岗位编制与年度人力计划一致，"
            f"薪酬结构未做调整。本月完成两场校园招聘宣讲，收到简历约 {150 + i * 7} 份，"
            f"进入复试 {20 + i} 人，预计下月发放 offer {5 + i % 3} 份。\n\n")


def water_block(i):
    return water_channel(i) + water_proj(i) + water_finance(i) + water_hr(i)


TAIL = (
    "## 议题 58：星火渠道项目专项（陈默）\n\n"
    "陈默：星火渠道项目 11 月 5 日（周五）渠道促销复盘完成，11 月 6 日（周五）正式放量，节奏没问题。\n\n"
    "## 议题 59：售后资源盘点（周岚）\n\n"
    "周岚：星火项目对接的售后团队 50 人，占员工总数的 25%，人手是够的，双 12 旺季也能顶住。\n\n"
    "## 议题 60：星火上线评估（赵立）\n\n"
    "赵立：星火项目联调还差 3 个模块，按目前节奏 12 月 24 日上线时间还不确定，延迟至少三周。\n\n"
    "## 议题 61：收尾（李诚）\n\n"
    "李诚：收尾——星火项目 12 月 24 日上线没有问题，大家放心。本年度最后一次经营复盘会就到这里，散会。\n"
)


def self_check(blocks):
    """水段必须真的自洽：算术零发现、无浮点尾数、同主体不重复出现。

    没有这一步，生成器会在无声无息中把"水段"变成错误源，
    再用它去评测算力/召回时，指标其实测的是样本缺陷。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import arith_check

    problems = []
    for i, b in enumerate(blocks):
        for f in arith_check.run_all(b):
            problems.append(f"块{i} 算术不一致：{f['evidence']}")
        for m in re.finditer(r"\d+\.\d{5,}", b):
            problems.append(f"块{i} 浮点尾数：{m.group(0)}")
        # 同一主体（区域/部门/项目）在一个块内不应出现两套数字
        for name in set(re.findall(r"([\u4e00-\u9fa5]{2,6}\d[区科期])", b)):
            if b.count(f"{name}") > 3:
                problems.append(f"块{i} 主体重复：{name}")
    return problems


def build(mid_blocks, long_blocks):
    mid_blocks_list = [water_block(i) for i in range(mid_blocks)]
    long_blocks_list = [water_block(i) for i in range(long_blocks)]
    problems = self_check(mid_blocks_list + long_blocks_list)
    if problems:
        raise SystemExit("水段自洽校验失败：\n  " + "\n  ".join(problems[:10]))
    mid = CORE + "\n\n" + "".join(mid_blocks_list)
    long_ = CORE + "\n\n" + "".join(long_blocks_list) + "\n" + TAIL
    return mid, long_


def main():
    mid, long_ = build(11, 46)
    (ROOT / "targets" / "stress_mid_001.txt").write_text(mid, encoding="utf-8")
    (ROOT / "targets" / "stress_long_001.txt").write_text(long_, encoding="utf-8")
    print({
        "mid_chars": len(mid),
        "long_chars": len(long_),
        "core_end": len(CORE),
        "tail_start": len(long_) - len(TAIL),
        "tail_within_12000": (len(long_) - len(TAIL)) <= 12000,
        "self_check": "passed",
    })


if __name__ == "__main__":
    main()
