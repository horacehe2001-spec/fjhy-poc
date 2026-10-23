# -*- coding: utf-8 -*-
"""AI 审核页的两个 KPI：通过率与时效。**纯函数部分不碰库、不出网。**

客户关注的就是这两项，所以口径错了比数字错了更贵——数字错一眼看得出来，
口径错会被当成结论带走。钉三条：

1. **三个比率的分母一律是「已审」，不是「全部单据」。**
   把没审的放进分母，通过率就随「还没审的有多少」而变，那衡量的是进度不是结果。

2. **「待人工」单列，既不并进通过也不并进不通过。**
   并进通过＝声称复核了没复核的东西；并进不通过＝把可能完全合规的单记成问题件。
   三率之和必须等于 1（在没有第四种结论的前提下），但三个数不许合并成两个。

3. **没有审核结论时三个比率是 None，不是 0。**
   写 0% 等于给出「一张都没通过」这个结论，而实际情况是没有结论。
   前端照着 None 显示「—」，照着 0 会显示「0.0%」。

时效那支要连库（cycle_log），单独放在最后一条，只验不变式不验具体数字——
数字会随每次重跑变，写死就会在下一次重跑后变红，而那时候红的是测试不是代码。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import ai_review as ar  # noqa: E402


def test_分母是已审不是全部单据():
    """已审 10（通过 2），全库 100 张 → 通过率 20%，**不是 2%**。"""
    r = ar.review_rates(reviewed=10, passed=2, review=5, reject=3)
    assert r["denominator"] == 10
    assert r["pass_rate"] == 0.2


def test_待人工单列且不并进任何一边():
    r = ar.review_rates(reviewed=29, passed=6, review=14, reject=9)
    assert r["pass_rate"] == round(6 / 29, 4)
    assert r["review_rate"] == round(14 / 29, 4)
    assert r["reject_rate"] == round(9 / 29, 4)
    # 待人工没有被算进通过，也没有被算进不通过
    assert r["pass_rate"] != round((6 + 14) / 29, 4)
    assert r["reject_rate"] != round((9 + 14) / 29, 4)


def test_三率之和为一():
    """没有第四种结论时三率相加是 1。**和为 1 才说明一张单都没漏分类。**"""
    r = ar.review_rates(reviewed=29, passed=6, review=14, reject=9)
    total = r["pass_rate"] + r["review_rate"] + r["reject_rate"]
    assert abs(total - 1.0) < 0.001


def test_没审过时是None不是零():
    """**这条是整组里最要紧的。** 0% 是个结论，None 才是「算不出来」。"""
    r = ar.review_rates(reviewed=0, passed=0, review=0, reject=0)
    assert r["pass_rate"] is None
    assert r["review_rate"] is None
    assert r["reject_rate"] is None
    assert r["denominator"] == 0
    assert "不是 0%" in r["note"], "说明里要讲清楚这不是 0"


def test_全部通过与全部不通过():
    assert ar.review_rates(5, 5, 0, 0)["pass_rate"] == 1.0
    assert ar.review_rates(5, 0, 0, 5)["reject_rate"] == 1.0
    assert ar.review_rates(5, 0, 5, 0)["review_rate"] == 1.0


def test_比率保留四位小数():
    """1/3 存成 0.3333，前端再乘 100 显示 33.3%。
    存成整数百分比会在前端想显示一位小数时无中生有。"""
    r = ar.review_rates(reviewed=3, passed=1, review=1, reject=1)
    assert r["pass_rate"] == 0.3333


def test_非正值被排除并报出条数():
    """实测 cycle_log 全量 1226 行里有 4 行 finished_at 早于 started_at
    约 175 秒。静默算进去会把 p50 拉歪，静默丢掉会让人以为数据是干净的——
    **既要排除，又要把排除了几条报出来。**

    喂参数直接测：库里那批最近周期恰好一条非正值都没有，
    走 review_timing 验这条是真空通过（已用探针确认）。
    """
    s = ar.timing_stats([100, 50, -174890, 0, None, 75])
    assert s["n"] == 3, "只剩 50/75/100 三条"
    assert s["excluded_nonpositive"] == 3, "负值、0、None 各一条，都要算进排除数"
    assert s["min_ms"] == 50 and s["max_ms"] == 100
    assert s["min_ms"] > 0


def test_空数据时分位数是None():
    s = ar.timing_stats([])
    assert s == {"n": 0, "excluded_nonpositive": 0, "p50_ms": None,
                 "p90_ms": None, "min_ms": None, "max_ms": None}
    # 全是非正值，等同于没有可用数据，但排除数要如实报
    s2 = ar.timing_stats([0, -5, None])
    assert s2["n"] == 0 and s2["excluded_nonpositive"] == 3
    assert s2["p50_ms"] is None


def test_分位数取的是有序后的位置():
    s = ar.timing_stats([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert s["p50_ms"] == 60
    assert s["p90_ms"] == 100
    assert (s["min_ms"], s["max_ms"]) == (10, 100)


def test_单条数据不越界():
    """len 1 时 int(0.9*1)=0，不许下标越界。"""
    s = ar.timing_stats([42])
    assert s["p50_ms"] == s["p90_ms"] == s["min_ms"] == s["max_ms"] == 42


def test_时效口径的不变式():
    """连库，但**只验不变式不验具体数字**——数字每次重跑都会变。"""
    t = ar.review_timing(llm_assisted=0)
    assert t["includes_llm"] is False, "模型辅助 0 条时不许标成含模型"
    assert "不含模型研判时延" in t["scope"]
    assert t["excluded_nonpositive"] >= 0
    if t["bills"]:
        # 分位数必须有序，且落在 min/max 之间
        assert t["min_ms"] <= t["p50_ms"] <= t["p90_ms"] <= t["max_ms"]
        assert t["min_ms"] > 0, "非正值应当已被排除在外"
    else:
        assert t["p50_ms"] is None and t["max_ms"] is None


def test_有模型介入时口径文案跟着变():
    """模型一介入，量级差两个数量级，`includes_llm` 和说明都要换。"""
    t = ar.review_timing(llm_assisted=37)
    assert t["includes_llm"] is True
    assert t["llm_involved_findings"] == 37
    assert "含模型研判" in t["scope"]
    assert "不含模型研判时延" not in t["scope"]


# ── 模型补判的单轮预算 ───────────────────────────────────────────────────────
# 实测（2026-10-24）：25 条 pending findings 串行调模型，一次点击跑了 25 分钟
# 还没返回，HTTP 一直挂着。根因两条——传输层对「请求已发出」的失败也重试
# （那是计费的，而且 ReadTimeout 要等满 timeout_s=240 秒），以及这个循环
# 没有任何上界。重试那条在 llm/client.py 收口，上界在这里。
#
# **判停必须在调用之前。** 在之后判等于总会多跑一条，而多跑的那一条
# 正是最慢情况下最贵的一条。

def test_条数到顶就停():
    assert ar.should_stop(called=30, elapsed_s=1, budget_s=180, max_calls=30) == "max_calls"
    assert ar.should_stop(called=29, elapsed_s=1, budget_s=180, max_calls=30) is None


def test_时间到顶就停():
    assert ar.should_stop(called=1, elapsed_s=180, budget_s=180, max_calls=30) == "budget"
    assert ar.should_stop(called=1, elapsed_s=179.9, budget_s=180, max_calls=30) is None


def test_两道闸都要在():
    """只留一道都不行：模型快时按条数收，模型慢时按时间收。"""
    # 模型很快：时间远没到，但条数到顶 → 必须停
    assert ar.should_stop(called=30, elapsed_s=5, budget_s=180, max_calls=30) == "max_calls"
    # 模型很慢：才调了 3 次，但时间到顶 → 必须停
    assert ar.should_stop(called=3, elapsed_s=200, budget_s=180, max_calls=30) == "budget"


def test_条数闸优先于时间闸():
    """两个同时到顶时报条数——和循环里的判断顺序一致，口径别两处不一样。"""
    assert ar.should_stop(called=30, elapsed_s=999, budget_s=180, max_calls=30) == "max_calls"


def test_跑完和没跑完的文案结论相反():
    """**这条是这组里最要紧的。** 没跑完而说成跑完了，使用者不会再点第二次，
    剩下的条目就永远停在那儿，而页面上看不出任何异常。"""
    rem, note = ar.supplement_progress(25, 25, 20, 5, "done", 180, 30)
    assert rem == 0
    assert "没有剩余" in note
    assert "还剩" not in note
    assert "再点一次" not in note

    rem, note = ar.supplement_progress(25, 8, 6, 2, "budget", 180, 30)
    assert rem == 17
    assert "还剩 17 条没补判" in note
    assert "再点一次" in note
    assert "没有剩余" not in note


def test_还剩多少按动过的条数算():
    """remaining = 本来要补判的 − 这一轮动过的。**不是按成功数算**——
    失败的那几条也动过了，但它们下一轮还会被挑中（llm_involved 仍是 FALSE），
    所以 remaining 要算它们在剩余里，否则两轮的数字对不上。"""
    rem, _ = ar.supplement_progress(25, 10, 7, 3, "budget", 180, 30)
    assert rem == 15, "动过 10 条（成 7 败 3），剩 15"


def test_两种停法的原因写得出来():
    _, n1 = ar.supplement_progress(25, 8, 8, 0, "budget", 180, 30)
    assert "时间预算 180 秒" in n1
    _, n2 = ar.supplement_progress(50, 30, 30, 0, "max_calls", 180, 30)
    assert "条数上限 30 次" in n2
