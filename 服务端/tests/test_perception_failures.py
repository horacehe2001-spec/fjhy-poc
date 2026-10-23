# -*- coding: utf-8 -*-
"""读图台账的失败视图：**全程不出网、不花钱**（只读台账，一次模型都不调）。

钉的是一条口径，它比看起来要紧：

    **「失败了几次」和「丢了几张图」是两个数。**

实测（2026-10-24）判类型 25 次失败 + 抽字段 24 次失败 = 49 次，而曾失败的
24 + 20 张图**全部在后续 attempt 里成功了，一张都没丢**。只报失败次数，
看页面的人会得出「有 49 张图没读出来」这个相反的结论——然后去重跑一轮
已经跑完的读图，而那是真出网真计费的动作。

所以 lost_imgs 必须等于「曾失败 − 后来成了」，note 必须按 lost_imgs 分叉：
lost=0 说「是成本留痕，不是待办」，lost>0 说「这几张是待办」。
两句话正好相反，闸失效而没人发现，比没有这道闸更危险。

**断言用增量，不用绝对值**：这个视图按整张台账聚合，而台账里有真实数据
（219 张附件的读图留痕）。写死「失败 25 次」的断言会在下一次真实读图之后
变红，而那时候红的是测试、不是代码。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, perception  # noqa: E402

PREFIX = "FJHY-TEST-PFAIL-"


@pytest.fixture
def att():
    """一张挂在测试单上的附件。不需要磁盘原图——本模块一次模型都不调。"""
    bill = PREFIX + "0001"
    db.execute("""INSERT INTO expense_bill(bill_no, doc_type, biz_type, is_demo)
                  VALUES (%s,'差旅费报账单','国内差旅费',FALSE)
                  ON CONFLICT (bill_no) DO NOTHING""", (bill,))
    aid = db.scalar("""INSERT INTO attachment(bill_no, file_name, stored_path, seq,
                                              sha256, source_kind)
                        VALUES (%s,'失败视图测试票.png',NULL,1,%s,'xlsx内嵌')
                        RETURNING id""", (bill, PREFIX + "sha"))
    yield aid
    db.execute("DELETE FROM attachment_vision WHERE attachment_id=%s", (aid,))
    db.execute("DELETE FROM attachment_extract WHERE attachment_id=%s", (aid,))
    db.execute("DELETE FROM attachment WHERE id=%s", (aid,))
    db.execute("DELETE FROM expense_bill WHERE bill_no=%s", (bill,))


def _vision(aid, attempt, ok, err_kind=None, message=None):
    db.execute("""INSERT INTO attachment_vision(attachment_id, attempt, model_name,
                                                ok, err_kind, message)
                  VALUES (%s,%s,'fake-model',%s,%s,%s)""",
               (aid, attempt, ok, err_kind, message))


def test_失败次数与丢图张数是两个数(att):
    """一张图失败两次、从没成功 → 失败次数 +2，丢图 +1。**不是 +2。**"""
    before = perception.ledger_failures("attachment_vision")
    _vision(att, 1, False, "timeout", "读超时")
    _vision(att, 2, False, "timeout", "又超时")
    after = perception.ledger_failures("attachment_vision")

    assert after["calls_failed"] - before["calls_failed"] == 2
    assert after["failed_imgs"] - before["failed_imgs"] == 1
    assert after["lost_imgs"] - before["lost_imgs"] == 1
    assert after["recovered_imgs"] == before["recovered_imgs"]


def test_后来成功的不算丢图(att):
    """同一张图第三次成了 → 丢图回到原值，已恢复 +1。失败次数**不减**。"""
    base = perception.ledger_failures("attachment_vision")
    _vision(att, 1, False, "tls", "握手失败")
    _vision(att, 2, True)
    after = perception.ledger_failures("attachment_vision")

    assert after["calls_failed"] - base["calls_failed"] == 1, "失败是花过的钱，不该因为后来成了就抹掉"
    assert after["failed_imgs"] - base["failed_imgs"] == 1
    assert after["recovered_imgs"] - base["recovered_imgs"] == 1
    assert after["lost_imgs"] == base["lost_imgs"], "后来成了就不是丢图"


def test_lost_为零和不为零的说明文案相反(att):
    """note 必须按 lost_imgs 分叉。两句话结论相反，不能混。"""
    _vision(att, 1, False, "timeout", "读超时")
    bad = perception.ledger_failures("attachment_vision")
    assert bad["lost_imgs"] >= 1
    assert "待办" in bad["note"]
    assert "不是待办" not in bad["note"]

    _vision(att, 2, True)
    good = perception.ledger_failures("attachment_vision")
    # 全库此刻可能仍有别的丢图（真实数据），所以只在真的归零时验那句话
    if good["lost_imgs"] == 0:
        assert "不是待办" in good["note"]
        assert "一张都没丢" in good["note"]


def test_非网络失败时不许说全是网络层(att):
    """`all_network` 是一句会被当证据用的话，只在真的全是网络类时才为真。"""
    _vision(att, 1, False, "parse", "模型答了列表外的编码")
    f = perception.ledger_failures("attachment_vision")
    assert f["all_network"] is False
    assert "没有一次是模型拒答" not in f["note"]


def test_每条失败都带着后来成没成(att):
    """recent 的 later_ok 是整张表最要紧的一列：没它只能看到一片红。"""
    _vision(att, 1, False, "http", "连接中断")
    rows = [r for r in perception.ledger_failures("attachment_vision", 500)["recent"]
            if r["attachment_id"] == att]
    assert rows and rows[0]["later_ok"] is False
    assert rows[0]["file_name"] == "失败视图测试票.png", "要能看出是哪张图"
    assert rows[0]["bill_no"] == PREFIX + "0001", "要能跳回单据"

    _vision(att, 2, True)
    rows = [r for r in perception.ledger_failures("attachment_vision", 500)["recent"]
            if r["attachment_id"] == att]
    assert rows and all(r["later_ok"] is True for r in rows), \
        "同一张图后来成了，它名下每一条失败记录都该标成已成功"


def test_表名白名单():
    """表名要拼进 SQL，**白名单不是装饰**——哪怕现在的调用方全是字面量。"""
    for bad in ("attachment", "attachment_vision; DROP TABLE attachment", "rule", ""):
        with pytest.raises(ValueError):
            perception.ledger_failures(bad)


def test_两张台账都能读():
    """判类型和抽字段共用这一支。少验一张，改了另一张没人知道。"""
    for t, stage in (("attachment_vision", "判类型"),
                     ("attachment_extract", "抽字段")):
        f = perception.ledger_failures(t, 5)
        assert f["stage"] == stage
        assert f["table"] == t
        assert f["lost_imgs"] == f["failed_imgs"] - f["recovered_imgs"]
        assert len(f["recent"]) <= 5
        assert f["calls_failed"] == sum(r["n"] for r in f["by_kind"])


# ── 文案与判定逻辑：喂参数直接测，不经过库 ──────────────────────────────────
# **为什么不走 ledger_failures**：台账里永远有真实数据的失败留痕，清不空，
# 空表那一支在带库的测试里根本走不到。这里原先写的是
#     f = ledger_failures(...);  if not f["by_kind"]:  assert ...
# 那是**真空通过**——用探针验过：把 `bool(by_kind) and` 删掉，它照样绿。
# 所以判定逻辑抽成了 summarize_failures，分支在这里逐个钉死。

def _kind(err_kind, n=1):
    return {"err_kind": err_kind, "n": n, "sample": "x"}


def test_空表不许冒充全是网络层():
    """all([]) 是 True —— 「没有失败」和「失败全怪网络」是两句不同的话。"""
    calls, all_net, note = perception.summarize_failures([], 0, 0)
    assert calls == 0
    assert all_net is False, "空表说『全是网络层』等于凭空给出一个结论"
    assert note == "没有失败记录。"


def test_全网络层且零丢图_说的是不是待办():
    calls, all_net, note = perception.summarize_failures(
        [_kind("timeout", 11), _kind("http", 8), _kind("tls", 6)], 24, 24)
    assert (calls, all_net) == (25, True)
    assert "一张都没丢" in note
    assert "没有一次是模型拒答" in note
    assert "不是待办" in note


def test_有丢图时不许说不是待办():
    """lost>0 和 lost==0 的两句文案结论相反，混了就是在页面上说假话。"""
    calls, all_net, note = perception.summarize_failures(
        [_kind("timeout", 3)], 3, 1)
    assert calls == 3
    assert "还有 2 张" in note
    assert "这 2 张是待办" in note
    assert "不是待办" not in note
    assert "一张都没丢" not in note


def test_混入非网络失败就不提网络层():
    """哪怕只有一条 parse，也不许说『全部在网络层』。"""
    _, all_net, note = perception.summarize_failures(
        [_kind("timeout", 20), _kind("parse", 1)], 21, 21)
    assert all_net is False
    assert "没有一次是模型拒答" not in note
    assert "一张都没丢" in note, "丢图为零这件事照说，不受网络层判定影响"


def test_err_kind_为空也不算网络层():
    """err_kind 没写（NULL）的失败，归因不明，不许算进网络层。"""
    _, all_net, _ = perception.summarize_failures([_kind(None, 2)], 2, 2)
    assert all_net is False


def test_失败次数按各类相加():
    calls, _, _ = perception.summarize_failures(
        [_kind("http", 9), _kind("tls", 9), _kind("timeout", 6)], 20, 20)
    assert calls == 24
