# -*- coding: utf-8 -*-
"""测试跑完把演示库留在一致状态。**这里只修一件测试自己弄坏的事。**

实测（2026-10-20）：跑完一次全量测试，演示库里
    rule(version=1) = 82 条，onto_rule_cover = 0 条
而页面上「统一口径 · 规则覆盖」就显示 0——看着像「一条规则都找不到依据」。

成因不是代码坏了，是测试自己的副作用：
  · tests/test_asset_gate.py:137   rl.load_all()      真的重建 rule 表
  · tests/test_release_pack.py:295 loader.load_all()  同上
而 onto_rule_cover.(rule_id, version) → rule 是 **ON DELETE CASCADE**，
重建 rule 就把这张表整表级联清空；全项目只有第⑦步
（onto_coord._save_align_locked）会重建它，而没有任何测试补跑第⑦步。

这违反的是「别把验证动作的产物留在别人的演示环境里」：客户下一次打开
统一口径那一页，看到的是一个测试留下的 0。

**为什么放在会话收尾，不放在每个装载用例后面：**
后面的用例可能再重建一次 rule，逐个补等于补了还会被清掉；而会话收尾
只跑一次，且**只在真的失配时才跑**——一致的时候一个 SQL 都不发。

**为什么只打印不失败：** 这是收尾清理，不是断言。让它能把会话判红，
等于给测试结果加了一个与被测代码无关的噪声源。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session", autouse=True)
def _把反向覆盖台账留成一致的():
    yield
    try:
        from app import db, onto_coord as oc
        n_rules = db.scalar("SELECT count(*) FROM rule WHERE version=1")
        n_cover = db.scalar("SELECT count(*) FROM onto_rule_cover")
        if not n_rules or n_cover == n_rules:
            return                      # 本来就一致，什么都不做
        fwd, rev = oc.align_forward(), oc.align_reverse()
        n_f, n_r = oc.save_align(fwd, rev)
        print()
        print("[conftest] 反向覆盖台账被测试级联清空（规则 %d / 覆盖 %d），"
              "已补跑第⑦步重建：正向 %d 条、反向 %d 条。" % (n_rules, n_cover, n_f, n_r))
    except Exception as e:              # noqa: BLE001
        print()
        print("[conftest] 想把反向覆盖台账补回来，但没做成：%s: %s。"
              "库里现在可能是「规则有行而覆盖 0 行」，页面上会显示 0 覆盖——"
              "手动补跑一次「统一口径」即可。" % (type(e).__name__, e))

@pytest.fixture(scope="session", autouse=True)
def _把演示审核结果留成一致的():
    """测试跑完把演示单的审核结论补回来。**这里也只修测试自己弄坏的事。**

    实测（2026-10-21）：跑完一次全量测试，演示库里
        expense_bill(is_demo) = 29 张，对应 review_run = 0 条
    AI 审核页上那几个 KPI（已审/通过/待复核/不通过）于是全显示 0——
    看页面的人会以为审核结果丢了，或者以为审核根本没跑成。

    成因同样是测试自己的副作用：
      · tests/test_detection.py:32  generator.generate(verbose=False)
        它内部先 clear()，把演示单的 review_run / review_finding 全删掉，
        然后只在内存里跑引擎做断言、**不落库**。
    所以测试结束时演示单是「有单、无结论」。

    补偿只跑规则链路（S→D→A→F），**不调大模型**：
    补偿是为了把环境留成一致，不该在测试收尾产生真实的模型调用与费用。
    于是补回来的结论里不含 llm_rationale，这是刻意的——
    页面上「模型辅助 N 条」会显示 0，点一次「智能审核」即可补上。

    触发原因写成可辨识的文案，和人点的那一次区分开。
    """
    yield
    try:
        from app import db
        from app.api import ai_review as ar
        n_bill = db.scalar("SELECT count(*) FROM expense_bill WHERE is_demo") or 0
        n_run = db.scalar(
            "SELECT count(DISTINCT rr.bill_no) FROM review_run rr "
            "JOIN expense_bill eb ON eb.bill_no = rr.bill_no "
            "WHERE eb.is_demo") or 0
        if not n_bill or n_run >= n_bill:
            return                      # 本来就一致，什么都不做
        res = ar.audit_demo_bills(reason="测试会话收尾补审（演示数据被测试重新生成）")
        ok = sum(1 for r in res if r.status == "ok")
        print()
        print("[conftest] 演示单被测试重新生成后没有审核结论（单 %d / 结论 %d），"
              "已补跑规则链路：%d 张成功、%d 张失败。**不含大模型辅助**，"
              "需要模型结论请在页面上点一次「智能审核」。"
              % (n_bill, n_run, ok, len(res) - ok))
    except Exception as e:              # noqa: BLE001
        print()
        print("[conftest] 想把演示单的审核结论补回来，但没做成：%s: %s。"
              "库里现在可能是「演示单有行而审核结论 0 行」，AI 审核页上 KPI 会显示 0——"
              "在页面上点一次「智能审核」即可。" % (type(e).__name__, e))
