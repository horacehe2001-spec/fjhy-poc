# -*- coding: utf-8 -*-
"""带**已知缺陷**的演示数据生成器 + 检出矩阵。

为什么必须有这个：
  客户给的 40 张真实样本全部是审批完成、人工放行的**合规**单据。
  在它们上面跑出「0 命中」只能证明「没误拦」，**证明不了「抓得住」**。
  要验证检出能力，只能自己造带已知缺陷的单据，并事先写明每张单
  该触发哪条规则——这就是下面的 CASES。

两个方向都要造：
  期望命中（positive）—— 证明抓得住
  期望不命中（negative）—— 证明不误伤。**后者同样重要**：
  一个把所有单据都判违规的系统，命中率 100%，但毫无用处。

全局纪律第 12 条：演示数据要一眼可辨、事后好清理。
  单据号一律 `FJHY-DEMO-` 前缀，`is_demo = TRUE`，
  姓名用「演示甲/演示乙」而不是真名——真名会让人误以为是真实业务数据。
"""
import argparse
import datetime
import json
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config, db  # noqa: E402

PREFIX = config.DEMO_BILL_PREFIX
D0 = datetime.date(2026, 9, 1)

# 演示用核算主体。有些规则的 scope 限定了主体（闽货、八方物流…），
# 不建这些主体，那些规则在演示里永远不触发。
DEMO_ORGS = [
    {"org_id": "DEMO-ORG-CZ", "name": "【演示】财智分公司", "city": None},
    {"org_id": "DEMO-ORG-MH", "name": "【演示】闽货", "city": "福州市"},
    {"org_id": "DEMO-ORG-BF", "name": "【演示】八方物流", "city": "福州市"},
    {"org_id": "DEMO-ORG-FG", "name": "【演示】福港下属单位", "city": "福州市"},
    # scope_matches 对 entity 走子串匹配，所以主体名必须**包含**规则 scope 里的
    # 那个词（「闽运集团」「漳州港务」…），否则这些分公司专属规则在演示里永不触发。
    {"org_id": "DEMO-ORG-MY", "name": "【演示】闽运集团", "city": "福州市"},
    {"org_id": "DEMO-ORG-ZZ", "name": "【演示】漳州港务", "city": "漳州市"},
    {"org_id": "DEMO-ORG-QZ", "name": "【演示】泉州港务", "city": "泉州市"},
    {"org_id": "DEMO-ORG-MS", "name": "【演示】闽深公司", "city": "福州市"},
    {"org_id": "DEMO-ORG-TL", "name": "【演示】福建福港拖轮", "city": "福州市"},
]
DEMO_PEOPLE = [
    {"emp_no": "DEMO-E001", "name": "演示甲", "rank": "其他员工", "city": "福州市"},
    {"emp_no": "DEMO-E002", "name": "演示乙", "rank": "其他员工", "city": "福州市"},
    {"emp_no": "DEMO-E003", "name": "演示丙", "rank": "总经理",   "city": "福州市"},
]

# ─────────────────────────────────────────────────────────────────────────────
# 检出矩阵。expect_hit / expect_not_hit / expect_pending 三者都要写。
# ─────────────────────────────────────────────────────────────────────────────
# 注：TR-D9-01「例外情形的补充说明是否充分」会**跟着别的命中一起出现**——
# 它的触发条件是「有例外情形被触发 且 补充说明为空」。
# 所以凡是命中了别的规则又没写补充说明的用例，都要把它列进 expect_hit，
# 否则矩阵就不完整（漏声明和漏实现看起来一样）。
_D = datetime.date   # 简写

CASES = [
    # ── 干净单：什么都不该触发 ──────────────────────────────────────────────
    {"id": "CLEAN-01", "label": "完全合规：2 天出差，补助与交通都在标准内",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 1060,
     "summary": "【演示数据】演示甲 9.1-9.2 福州驻点",
     "remark": "无特殊情况",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "福州"},
     "details": [("伙食补助费", 200), ("市内交通费补贴", 160), ("交通费", 700)],
     "expect_hit": [], "expect_pending": []},

    {"id": "CLEAN-02", "label": "住宿费日均 340，低于最低档 350，必定不超标",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 880,
     "summary": "【演示数据】演示甲 9.3-9.4 福州驻点（含住宿）",
     "trip": {"start": _D(2026, 9, 3), "end": _D(2026, 9, 4), "place": "福州"},
     "details": [("伙食补助费", 200), ("住宿费", 680)],
     "expect_hit": [], "expect_pending": []},

    # ── 期望命中 ────────────────────────────────────────────────────────────
    # **不能给 OA 出差审批单**：有起止日期就直接得出天数，不确定性消失，
    # 这个用例要测的「倒算不整除 → 天数算不准 → 转人工」那条路就走不到了。
    {"id": "PEND-MEAL-02", "label": "伙食补助 350 不是日标准 100 的整数倍，天数算不准",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 350,
     "summary": "【演示数据】演示甲出差（补助金额非整数倍）",
     "details": [("伙食补助费", 350)],
     "expect_hit": [], "expect_pending": ["TR-D4-04"]},

    {"id": "HIT-HOTEL-01", "label": "住宿费日均 1200，超过最高档 980",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 2600,
     "summary": "【演示数据】演示甲 9.7-9.8 北京出差",
     "trip": {"start": _D(2026, 9, 7), "end": _D(2026, 9, 8), "place": "北京"},
     "details": [("伙食补助费", 200), ("住宿费", 2400)],
     "expect_hit": ["TR-D4-02", "TR-D9-01"], "expect_pending": []},

    {"id": "HIT-HOTEL-02", "label": "超标住宿且无书面说明，连带触发 TR-D4-06",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 2600,
     "summary": "【演示数据】演示乙 9.9-9.10 北京出差",
     "trip": {"start": _D(2026, 9, 9), "end": _D(2026, 9, 10), "place": "北京"},
     "claimant": "DEMO-E002",
     "details": [("伙食补助费", 200), ("住宿费", 2400)],
     "expect_hit": ["TR-D4-02", "TR-D4-06", "TR-D9-01"], "expect_pending": []},

    {"id": "HIT-REST-01", "label": "摘要含「疗休养」，不得从差旅费列支",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 500,
     "summary": "【演示数据】演示甲参加工会疗休养活动",
     "details": [("伙食补助费", 100), ("交通费", 400)],
     "expect_hit": ["TR-D5-10", "TR-D9-01"], "expect_pending": []},

    {"id": "HIT-BIG-01", "label": "单笔 250 万，达到 200 万大额审批线",
     "org": "DEMO-ORG-FG", "biz": "国内差旅费", "amount": 2500000,
     "summary": "【演示数据】演示丙大额支付",
     "details": [("其他差旅费", 2500000)],
     "expect_hit": ["TR-D10-06", "TR-D9-01"], "expect_pending": []},

    {"id": "HIT-SPLIT-A", "label": "拆单之一：120 万",
     "org": "DEMO-ORG-FG", "biz": "国内差旅费", "amount": 1200000,
     "summary": "【演示数据】拆单测试 A", "day": 20,
     "details": [("其他差旅费", 1200000)],
     "expect_hit": ["TR-D10-06", "TR-D9-01"], "expect_pending": []},
    {"id": "HIT-SPLIT-B", "label": "拆单之二：110 万，与 A 相隔 1 天，合计 230 万",
     "org": "DEMO-ORG-FG", "biz": "国内差旅费", "amount": 1100000,
     "summary": "【演示数据】拆单测试 B", "day": 21,
     "details": [("其他差旅费", 1100000)],
     "expect_hit": ["TR-D10-06", "TR-D9-01"], "expect_pending": []},

    {"id": "HIT-SECRET-01", "label": "补充说明提涉密，但审核意见缺固定话术",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 300,
     "summary": "【演示数据】演示甲涉密项目差旅",
     "remark": "涉密项目，附件不便上传",
     "approvals": [("开始", "DEMO/演示甲", None),
                   ("共享审核", "DEMO/演示审核", "其他,请在意见区按要求备注涉密说明")],
     "details": [("伙食补助费", 100), ("交通费", 200)],
     "expect_hit": ["TR-D10-05"], "expect_pending": []},

    {"id": "HIT-MH-01", "label": "闽货不应有市内交通补贴，却报了 160 元",
     "org": "DEMO-ORG-MH", "biz": "国内差旅费", "amount": 360,
     "summary": "【演示数据】闽货演示甲出差",
     "trip": {"start": _D(2026, 9, 15), "end": _D(2026, 9, 16), "place": "福州"},
     "details": [("伙食补助费", 200), ("市内交通费补贴", 160)],
     "expect_hit": ["TR-D4-13", "TR-D9-01"], "expect_pending": []},

    {"id": "HIT-BF-01", "label": "八方物流单据日期超出综合办授权有效期",
     "org": "DEMO-ORG-BF", "biz": "国内差旅费", "amount": 300,
     "summary": "【演示数据】八方物流驾驶员出差",
     "doc_date": datetime.date(2025, 6, 1),
     "details": [("伙食补助费", 100), ("交通费", 200)],
     "expect_hit": ["TR-D10-03", "TR-D9-01"], "expect_pending": []},

    # ── 期望「待人工」 ────────────────────────────────────────────────────────
    # PEND-HOTEL-01 刻意不给 place，让住宿费落在中间档→判不了。
    {"id": "PEND-HOTEL-01", "label": "住宿费日均 500，落在 350~980 之间，取哪档要看出差地",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 1200,
     "summary": "【演示数据】演示甲 9.11-9.12 出差（住宿日均 500）",
     "trip": {"start": _D(2026, 9, 11), "end": _D(2026, 9, 12), "place": None},
     "details": [("伙食补助费", 200), ("住宿费", 1000)],
     "expect_hit": [], "expect_pending": ["TR-D4-02", "TR-D4-06"]},

    # remark 是为了把本用例隔离在 TR-D4-04 上：2 天行程没有住宿费，
    # 不写说明会被 TR-D6-03/TR-D6-06 正确命中，那不是本用例要考的事。
    {"id": "PEND-MEAL-01", "label": "伙食补助人日均 110，落在 100~120 之间",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 220,
     "summary": "【演示数据】演示甲 9.13-9.14 出差",
     "remark": "住权属企业内部招待所，无住宿费发票",
     "trip": {"start": _D(2026, 9, 13), "end": _D(2026, 9, 14), "place": "福州"},
     "details": [("伙食补助费", 220)],
     "expect_hit": [], "expect_pending": ["TR-D4-04"]},

    {"id": "PEND-DELEG-01", "label": "提单人≠报账人，而授权记录取不到",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 300,
     "summary": "【演示数据】演示乙代演示甲提单",
     "submitter": "DEMO-E002", "claimant": "DEMO-E001",
     "details": [("伙食补助费", 100), ("交通费", 200)],
     "expect_hit": [], "expect_pending": ["TR-D1-03"]},

    # ── 期望不命中：防误伤 ──────────────────────────────────────────────────
    # 这两个负例都要给**当天往返**的 OA 行程：没有行程时天数只能由伙食补助倒算，
    # 算出来的是「累计出差日数」而非单次连续天数，TR-D6-03/TR-D6-06 只能挂 pending，
    # 于是负例单上也带着待人工项，考不出「不得误伤」这件事。
    {"id": "NEG-REST-01", "label": "摘要含「治疗费」「休养生息」，不得被「疗休养」误伤",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 300,
     "summary": "【演示数据】演示甲陪同就医治疗费报销，休养生息期间差旅",
     "trip": {"start": _D(2026, 9, 23), "end": _D(2026, 9, 23), "place": "福州"},
     "details": [("伙食补助费", 100), ("交通费", 200)],
     "expect_hit": [], "expect_not_hit": ["TR-D5-10"], "expect_pending": []},

    {"id": "NEG-BIG-01", "label": "199 万，差 1 万到阈值，不得命中",
     "org": "DEMO-ORG-FG", "biz": "国内差旅费", "amount": 1990000,
     "summary": "【演示数据】演示丙 199 万支付", "day": 25,
     "trip": {"start": _D(2026, 9, 25), "end": _D(2026, 9, 25), "place": "福州"},
     "details": [("其他差旅费", 1990000)],
     "expect_hit": [], "expect_not_hit": ["TR-D10-06"], "expect_pending": []},

    {"id": "NEG-MEAL-01", "label": "伙食补助正好 100/人日，不得判超标",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 400,
     "summary": "【演示数据】演示甲 4 天出差",
     "remark": "住权属企业内部招待所，无住宿费发票",
     "trip": {"start": _D(2026, 9, 17), "end": _D(2026, 9, 20), "place": "福州"},
     "details": [("伙食补助费", 400)],
     "expect_hit": [], "expect_not_hit": ["TR-D4-04"], "expect_pending": []},

    {"id": "NEG-MH-01", "label": "闽货没有市内交通补贴、交通费在上限内，不得命中",
     "org": "DEMO-ORG-MH", "biz": "国内差旅费", "amount": 280,
     "summary": "【演示数据】闽货演示乙出差",
     "remark": "住权属企业内部招待所，无住宿费发票",
     "trip": {"start": _D(2026, 9, 21), "end": _D(2026, 9, 22), "place": "福州"},
     "details": [("伙食补助费", 200), ("交通费", 80)],
     "expect_hit": [], "expect_not_hit": ["TR-D4-13"], "expect_pending": []},

    # ── 发票类三条 ──────────────────────────────────────────────────────────
    # 住宿费都取 600 / 2 天 = 300 元日均，低于 TR-D4-02 的下界 350，
    # 这样这三个用例只考发票字段本身，不会被住宿费超标连带命中。
    {"id": "HIT-TITLE-01", "label": "住宿发票抬头不是本单核算主体",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 600,
     "summary": "【演示数据】演示甲 9.1-9.2 出差（发票抬头开错）",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "福州"},
     "hotel_title": "【演示】另一家公司",
     "details": [("住宿费", 600)],
     "expect_hit": ["TR-D1-02"], "expect_pending": []},

    {"id": "HIT-ISSUER-01", "label": "住宿发票开票单位含「KTV」",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 600,
     "summary": "【演示数据】演示甲 9.1-9.2 出差（开票单位异常）",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "福州"},
     "hotel_issuer": "【演示】金色年华KTV会所",
     "details": [("住宿费", 600)],
     "expect_hit": ["TR-D3-01"], "expect_pending": []},

    {"id": "HIT-ITEM-01", "label": "发票明细含「洗浴」",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 600,
     "summary": "【演示数据】演示甲 9.1-9.2 出差（发票明细异常）",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "福州"},
     "hotel_detail": "住宿费、洗浴服务",
     "details": [("住宿费", 600)],
     "expect_hit": ["TR-D3-02"], "expect_pending": []},

    # ── 分公司专属标准五条 ──────────────────────────────────────────────────
    {"id": "HIT-MY-01", "label": "闽运集团市内交通费人日均 100，超包干上限 80",
     "org": "DEMO-ORG-MY", "biz": "国内差旅费", "amount": 200,
     "summary": "【演示数据】闽运集团演示甲 9.1-9.2 出差",
     "remark": "住权属企业内部招待所，无住宿费发票",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "厦门"},
     "details": [("市内交通费补贴", 200)],
     "expect_hit": ["TR-D4-09"], "expect_pending": []},

    {"id": "HIT-ZZ-01", "label": "漳州港务在古雷港区内，不属于出差",
     "org": "DEMO-ORG-ZZ", "biz": "国内差旅费", "amount": 100,
     "summary": "【演示数据】漳州港务演示甲古雷港区内公务",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 1),
              "place": "古雷港经济开发区"},
     "details": [("伙食补助费", 100)],
     "expect_hit": ["TR-D4-10"], "expect_pending": []},

    {"id": "HIT-QZ-01", "label": "泉州港务伙食补助人日均 150，超最高档 120",
     "org": "DEMO-ORG-QZ", "biz": "国内差旅费", "amount": 300,
     "summary": "【演示数据】泉州港务演示甲 9.1-9.2 出差",
     "remark": "住权属企业内部招待所，无住宿费发票",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "厦门"},
     "details": [("伙食补助费", 300)],
     "expect_hit": ["TR-D4-11"], "expect_pending": []},

    {"id": "HIT-MS-01", "label": "闽深公司不应有市内交通补贴",
     "org": "DEMO-ORG-MS", "biz": "国内差旅费", "amount": 100,
     "summary": "【演示数据】闽深公司演示甲当天往返",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 1), "place": "厦门"},
     "details": [("市内交通费补贴", 100)],
     "expect_hit": ["TR-D4-12"], "expect_pending": []},

    # 住宿按「晚」算：9.1-9.3 是 3 天 2 晚，上限 200×2=400，本单 600 超标。
    {"id": "HIT-TL-01", "label": "拖轮公司外派住宿费 600 超 200 元/人·晚",
     "org": "DEMO-ORG-TL", "biz": "外派差旅费", "amount": 600,
     "summary": "【演示数据】福港拖轮演示甲 9.1-9.3 外派",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 3), "place": "厦门"},
     "details": [("住宿费", 600)],
     "expect_hit": ["TR-D4-14"], "expect_pending": []},

    # 单次连续 2 天、无住宿费、无补充说明：TR-D6-03 要补件，TR-D6-06 连带否掉
    # 其余费用，TR-D9-01 则因「触发了例外却没写说明」一并命中。
    # 伙食补助 200 / 2 天 = 100，正好压在 TR-D4-04 下界上，不连带超标。
    {"id": "HIT-NOHOTEL-01", "label": "单次连续出差 2 天无住宿费，也没有补充说明",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 200,
     "summary": "【演示数据】演示甲 9.1-9.2 出差（缺住宿费发票）",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 2), "place": "厦门"},
     "details": [("伙食补助费", 200)],
     "expect_hit": ["TR-D6-03", "TR-D6-06", "TR-D9-01"], "expect_pending": []},

    # ── 交通凭证 ────────────────────────────────────────────────────────────
    # 当天往返（1 天）避开 TR-D6-03；不写 remark 才能让 TR-D6-05 命中。
    {"id": "HIT-ONEWAY-01", "label": "交通费凭证只有去程，且无情况说明",
     "org": "DEMO-ORG-CZ", "biz": "国内差旅费", "amount": 700,
     "summary": "【演示数据】演示甲 9.1 当天往返北京（仅上传去程）",
     "trip": {"start": _D(2026, 9, 1), "end": _D(2026, 9, 1), "place": "北京"},
     "one_way": True,
     "details": [("交通费", 700)],
     "expect_hit": ["TR-D6-05"], "expect_pending": []},
]


def _add_trip_application(cur, bill_no, case, day):
    """为有出差行程的演示单插 trip_application（模拟 OA 同步数据）。

    `synced_from_oa = TRUE` 是关键：引擎的 scope_matches 检查这个标志，
    FALSE 时依赖 C0302/C0303/C0304 的规则仍被视为「字段不可得」。
    """
    trip = case.get("trip")
    if not trip:
        return
    app_no = bill_no + "-OA"
    items = {item for item, _ in case.get("details", [])}
    days = None
    if trip.get("start") and trip.get("end"):
        days = (trip["end"] - trip["start"]).days + 1
    cur.execute("""INSERT INTO trip_application
        (app_no, bill_no, start_date, end_date, place, days, synced_from_oa)
        VALUES (%s,%s,%s,%s,%s,%s,TRUE)
        ON CONFLICT (app_no) DO NOTHING""",
        (app_no, bill_no, trip.get("start"), trip.get("end"), trip.get("place"), days))


def _add_invoices(cur, bill_no, case, day):
    """为演示单插发票 + 住宿明细 / 交通凭证（模拟票据 OCR 结果落库）。

    规则引擎靠 invoice / hotel_invoice / transport_voucher 表取发票字段，
    演示数据里没有实体 PDF，用这里的 stub 记录等效替代 OCR 输出。
    ocr_confidence = 0.92 表示「置信度足够高，不触发人工复核」。
    """
    items = {item: amt for item, amt in case.get("details", [])}
    trip = case.get("trip") or {}
    start = trip.get("start") or day
    end   = trip.get("end")   or day
    tag   = case["id"]
    # 报账人姓名（用于发票抬头）
    cla   = case.get("claimant", case.get("submitter", "DEMO-E001"))
    title = {"DEMO-E001": "演示甲", "DEMO-E002": "演示乙", "DEMO-E003": "演示丙"}.get(cla, "演示甲")
    org_name = {"DEMO-ORG-CZ": "【演示】财智分公司",
                "DEMO-ORG-MH": "【演示】闽货",
                "DEMO-ORG-BF": "【演示】八方物流",
                "DEMO-ORG-FG": "【演示】福港下属单位"}.get(case.get("org"), "【演示】财智分公司")

    # ── 住宿费发票 ─────────────────────────────────────────────────────────
    hotel_amt = items.get("住宿费") or items.get("住宿费（参加会议产生）")
    if hotel_amt:
        inv_id = bill_no + "-INV-HTL"
        nights = (end - start).days or 1
        # issuer 与 detail_text 都要给值：TR-D3-01 扫开票单位、TR-D3-02 扫发票明细，
        # 留空它们只能返回「扫不出结果」的 pending，干净单上也会挂着待人工。
        cur.execute("""INSERT INTO invoice
            (invoice_id, bill_no, inv_type, issue_date, issuer, title, detail_text,
             total_amount, tax_amount, tax_rate, is_eticket, ocr_confidence, ocr_source)
            VALUES (%s,%s,'增值税专用发票',%s,%s,%s,%s,%s,%s,0.06,FALSE,0.92,'【演示stub】')""",
            (inv_id, bill_no, end,
             case.get("hotel_issuer") or "【演示】某酒店",
             case.get("hotel_title") or org_name,
             case.get("hotel_detail") or "住宿费",
             hotel_amt, round(hotel_amt * 0.06 / 1.06, 2)))
        cur.execute("""INSERT INTO hotel_invoice
            (invoice_id, check_in, check_out, nights, rooms)
            VALUES (%s,%s,%s,%s,1)""",
            (inv_id, start, end, nights))

    # ── 交通费发票（城际：高铁；涉密/短途：出租车）───────────────────────
    transport_amt = items.get("交通费") or items.get("城市间交通费")
    if transport_amt:
        inv_id   = bill_no + "-INV-TRP"
        place    = trip.get("place") or "福州"
        # 涉密单或本地出差 → 出租车，否则高铁
        is_local = place in ("福州", "福州市") or not trip.get("place")
        vehicle  = "出租车" if is_local else "高铁"
        inv_type = "增值税普通发票" if is_local else "铁路电子客票"
        tax_rate = 0.03 if is_local else 0.09
        cur.execute("""INSERT INTO invoice
            (invoice_id, bill_no, inv_type, issue_date, issuer, title, detail_text,
             total_amount, tax_amount, tax_rate, is_eticket, ocr_confidence, ocr_source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0.92,'【演示stub】')""",
            (inv_id, bill_no, inv_type, start,
             "【演示】铁路客运" if vehicle == "高铁" else "【演示】出租车公司",
             title,
             case.get("transport_detail") or ("旅客运输服务" if vehicle == "高铁"
                                              else "客运服务费"),
             transport_amt, round(transport_amt * tax_rate / (1 + tax_rate), 2),
             tax_rate, vehicle == "高铁"))
        vch_id = bill_no + "-VCH-TRP"
        from_place = "福州"
        to_place   = place if not is_local else "福州"
        # one_way 的用例只给单程，用来触发 TR-D6-05
        round_trip = not case.get("one_way")
        cur.execute("""INSERT INTO transport_voucher
            (voucher_id, bill_no, invoice_id, vehicle, from_place, to_place,
             ride_date, has_round_trip)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (vch_id, bill_no, inv_id, vehicle, from_place, to_place, start, round_trip))


def _add_attachments(cur, bill_no, case, day):
    """为演示单生成对应的附件记录（type_enum='EN17'，值来自 enum_value_current）。

    附件类型按费用明细项推导：有住宿费就有住宿费发票，有交通费就有交通票据。
    涉密单（remark 含「涉密」）只给出差审批单，其余附件不便上传——
    这是 HIT-SECRET-01 case 的场景，TR-D10-05 正是判「审核意见有没有固定话术」。
    **附件只是展示用**：D6 附件类规则因缺 OA/OCR 数据全部在剔除集，
    这些记录不影响任何规则的结论。

    文件名用「演示-case_id-类型.pdf」，一眼可辨、事后好清理。
    stored_path 留空：演示数据没有实体文件。
    attach_type 取 EN17 的合法值：出差审批单/差旅费明细表、住宿费发票、
    交通费报销凭证、其他差旅费发票、事权审批单、情况说明。
    """
    remark = case.get("remark") or ""
    items = {item for item, _ in case.get("details", [])}
    tag = case["id"]
    upload_base = datetime.datetime.combine(day, datetime.time(8, 30))

    def ins(attach_type, suffix, is_required, pages):
        # type_enum 是生成列（由 attach_type 自动推导），不能手动写入
        # source_kind 必须**显式**写「演示合成」。
        # 不靠列默认值：默认值一旦设成别的（迁移 0035 曾把它设成 'xlsx内嵌'），
        # 这里的演示附件就会被标成客户真实附件——实测踩过一次，
        # 跑完一轮测试之后 attachment 表 273 行全成了「xlsx内嵌」。
        # 演示数据必须一眼可辨（全局纪律第 12 条），而「一眼可辨」不能依赖
        # 另一个文件里的默认值。
        cur.execute("""INSERT INTO attachment
            (bill_no, attach_type, file_name, is_required, pages, uploaded_at,
             source_kind)
            VALUES (%s,%s,%s,%s,%s,%s,'演示合成')""",
            (bill_no, attach_type,
             "【演示】%s-%s.pdf" % (tag, suffix),
             is_required, pages,
             upload_base + datetime.timedelta(minutes=pages * 2)))

    # 1. 出差审批单/差旅费明细表（每张单必须有）
    ins("出差审批单/差旅费明细表", "出差审批单", True, 1)

    # 涉密单只有出差审批单，其余「不便上传」——与 HIT-SECRET-01 场景吻合
    if "涉密" in remark:
        return

    # 2. 交通费报销凭证
    if any(i in items for i in ("交通费", "市内交通费补贴", "市内交通补贴", "城市间交通费")):
        ins("交通费报销凭证", "交通票据", True, 2)

    # 3. 住宿费发票
    if any(i in items for i in ("住宿费", "住宿费（参加会议产生）")):
        ins("住宿费发票", "住宿发票", True, 1)

    # 4. 其他差旅费发票
    if "其他差旅费" in items:
        ins("其他差旅费发票", "其他凭证", False, 1)

    # 5. 大额审批（>= 200万触发大额审批线）
    if case.get("amount", 0) >= 2000000:
        ins("事权审批单", "大额审批", True, 2)

    # 6. 代提单情况说明（提单人 ≠ 报账人时）
    sub = case.get("submitter", "DEMO-E001")
    cla = case.get("claimant", sub)
    if sub != cla:
        # 纸质情况说明在这里；共享系统电子授权记录仍取不到（缺口 G-01），
        # 所以 TR-D1-03 仍会 pending——纸质附件 ≠ 电子授权记录。
        ins("情况说明", "代提单说明", True, 1)


def clear(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM review_finding WHERE run_id IN "
                "(SELECT run_id FROM review_run WHERE bill_no LIKE %s)", (PREFIX + "%",))
    for t in ("manual_review", "review_run", "expense_detail", "bill_traveler",
              "approval_record", "attachment"):
        cur.execute("DELETE FROM %s WHERE bill_no LIKE %%s" % t, (PREFIX + "%",))
    # hotel_invoice / transport_voucher 通过 invoice_id / bill_no 关联，
    # 必须在 expense_bill 和 invoice 之前删；hotel_invoice 没有 bill_no，
    # 只能通过 invoice_id 反查。
    cur.execute("DELETE FROM hotel_invoice WHERE invoice_id IN "
                "(SELECT invoice_id FROM invoice WHERE bill_no LIKE %s)", (PREFIX + "%",))
    cur.execute("DELETE FROM transport_voucher WHERE bill_no LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM invoice WHERE bill_no LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM trip_application WHERE bill_no LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM expense_bill WHERE bill_no LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM employee WHERE emp_no LIKE 'DEMO-%'")
    cur.execute("DELETE FROM org WHERE org_id LIKE 'DEMO-%'")


def generate(verbose=True):
    with db.tx() as conn:
        cur = conn.cursor()
        clear(conn)
        for o in DEMO_ORGS:
            cur.execute("INSERT INTO org (org_id, name, city, is_internal) "
                        "VALUES (%s,%s,%s,TRUE)", (o["org_id"], o["name"], o["city"]))
        for p in DEMO_PEOPLE:
            cur.execute("INSERT INTO employee (emp_no, name, rank_level, org_id, resident_city) "
                        "VALUES (%s,%s,%s,'DEMO-ORG-CZ',%s)",
                        (p["emp_no"], p["name"], p["rank"], p.get("city")))
        for i, c in enumerate(CASES, 1):
            bill_no = "%s%03d-%s" % (PREFIX, i, c["id"])
            day = c.get("doc_date") or (D0 + datetime.timedelta(days=c.get("day", i)))
            sub = c.get("submitter", "DEMO-E001")
            cla = c.get("claimant", sub)
            cur.execute("""INSERT INTO expense_bill
                (bill_no, doc_type, doc_date, submitter_no, claimant_no, claim_dept,
                 org_id, biz_type, amount, pay_amount, currency, summary, remark,
                 applicant_rank, is_multi_person, is_demo)
                VALUES (%s,'差旅费报账单',%s,%s,%s,'【演示】运营管理部',%s,%s,%s,%s,
                        '人民币',%s,%s,'其他员工',FALSE,TRUE)""",
                (bill_no, day, sub, cla, c["org"], c["biz"], c["amount"], c["amount"],
                 c["summary"], c.get("remark")))
            for k, (item, amt) in enumerate(c["details"], 1):
                cur.execute("""INSERT INTO expense_detail
                    (bill_no, expense_item, amount, line_no) VALUES (%s,%s,%s,%s)""",
                    (bill_no, item, amt, k))
            # **演示单默认不带「共享审核」记录。**
            # 它们代表「刚提交、还没经人工审核」的新单。
            # 第一版给每张都安了一条「共享审核=同意」，结果 F 阶段把
            # 9 条正确的检出当成了误报——人工真值是编的，就会得出编的偏差。
            # 没有人工结论时，F 阶段记 human=null、不计偏差，这才是对的。
            apps = c.get("approvals") or [("开始", "DEMO/演示甲", None),
                                          ("部门负责人", "DEMO/演示乙", "同意")]
            for k, (node, approver, op) in enumerate(apps, 1):
                cur.execute("""INSERT INTO approval_record
                    (bill_no, seq, node, approver, opinion, approved_at)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (bill_no, k, node, approver, op,
                     datetime.datetime.combine(day, datetime.time(9, k))))
            _add_trip_application(cur, bill_no, c, day)
            _add_invoices(cur, bill_no, c, day)
            _add_attachments(cur, bill_no, c, day)
            c["_bill_no"] = bill_no
    if verbose:
        pos = sum(1 for c in CASES if c["expect_hit"])
        neg = sum(1 for c in CASES if c.get("expect_not_hit"))
        pend = sum(1 for c in CASES if c["expect_pending"])
        print("演示数据已生成：%d 张单（前缀 %s，is_demo=TRUE）" % (len(CASES), PREFIX))
        print("  期望命中 %d 张 · 期望不命中 %d 张 · 期望待人工 %d 张 · 干净单 %d 张"
              % (pos, neg, pend,
                 sum(1 for c in CASES if not c["expect_hit"] and not c["expect_pending"])))
        print("  覆盖规则：%s" % sorted({r for c in CASES
                                       for r in c["expect_hit"] + c["expect_pending"]}))
    return CASES


def expectations():
    """返回 {bill_no: case}，给测试与演示用。"""
    return {"%s%03d-%s" % (PREFIX, i, c["id"]): c for i, c in enumerate(CASES, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    if args.clear:
        with db.tx() as conn:
            clear(conn)
        print("演示数据已清空（真实样本未动）")
        return 0
    generate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
