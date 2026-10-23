# -*- coding: utf-8 -*-
"""附件读图判类型：**这组测试全程不出网、不花钱**（monkeypatch client.chat_vision）。

这是本系统第二敏感的出口——发出去的是真实发票与出差申请单，而不是制度文件页。
钉的是五条纪律，少一条就会让「判不出」变成「误判」或者把钱花在不该花的地方：

1. **细类编码 → EN17 值的映射必须覆盖全部 12 个枚举值，且没有一个越界。**
   这张表是唯一真相来源，别处不许再抄一份；抄两份，口径一变就会漏改一处。

2. **把握不到「高」不回写 attach_type。** 模型会把看不清的票面编出来，
   而且编得通顺（本项目在制度表格读图上已经实测过）。「中」「低」都只落台账、
   不进字段——TR-D6-01 看到空字段仍然报「判不出」，不会被一个不确定的猜测带偏。

3. **模型答了列表之外的编码，当判不出处理，不强行纠正。** 猜它想说哪个，
   比直接判不出更危险。

4. **成败都落台账，永不覆盖。** 同一张图重读是新的一行，不改旧行——
   这条链路发出去的是客户凭证，少一行就少一条「发给谁了」的证据。

5. **`attachment_ids=[]`（空列表）≠「全部」。** 和 doclib_extract 的同类参数
   保持一致的区分（is not None），否则空列表会悄悄处理全部待分类附件，
   一次就是一批真实调用的钱。

测试数据建在 FJHY-TEST- 前缀的单据与附件下，跑完连磁盘文件带数据库行一起删。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.llm.client import Result  # noqa: E402
from app.perception import attachment_classify as acx  # noqa: E402

PREFIX = "FJHY-TEST-ACX-"


def _png_file(tmp_path, name="a.png", color="red"):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (4, 4), color).save(str(p))
    return str(p)


@pytest.fixture
def att(tmp_path):
    """一张挂在真实单上的测试附件，带磁盘原图。跑完连带附件台账一起删。"""
    bill = PREFIX + "0001"
    db.execute("""INSERT INTO expense_bill(bill_no, doc_type, biz_type, is_demo)
                  VALUES (%s,'差旅费报账单','国内差旅费',FALSE)
                  ON CONFLICT (bill_no) DO NOTHING""", (bill,))
    path = _png_file(tmp_path)
    aid = db.scalar("""INSERT INTO attachment(bill_no, file_name, stored_path, seq,
                                              sha256, source_kind)
                        VALUES (%s,'测试票据.png',%s,1,%s,'xlsx内嵌')
                        RETURNING id""", (bill, path, PREFIX + "sha"))
    yield aid
    db.execute("DELETE FROM attachment_vision WHERE attachment_id=%s", (aid,))
    db.execute("DELETE FROM attachment WHERE id=%s", (aid,))
    db.execute("DELETE FROM expense_bill WHERE bill_no=%s", (bill,))


@pytest.fixture(autouse=True)
def _禁止动真实附件():
    """**兜底闸，不是装饰。** 这个文件踩过一次真实数据污染（见下面那条用例的
    docstring）：mock 只拦住了出网调用，没拦住「从整个库里挑数据」这件事，
    结果假分类结果被写进了 12 条真实客户附件。

    这里在每个用例前后快照一次「真实附件（非 FJHY-TEST- 前缀）里已判类型的
    数量与 attachment_vision 的真实型号行数」，用例跑完必须分毫不差——
    **任何一个新用例只要碰到真实数据就会在这里被当场拦住**，
    不必等人事后去翻 bill_no 是不是忘了加前缀。
    """
    def snap():
        return (
            db.scalar("SELECT count(*) FROM attachment WHERE source_kind='xlsx内嵌' "
                      "AND attach_type IS NOT NULL AND bill_no NOT LIKE %s", (PREFIX + "%",)),
            db.scalar("SELECT count(*) FROM attachment_vision av "
                      "JOIN attachment a ON a.id=av.attachment_id "
                      "WHERE a.bill_no NOT LIKE %s", (PREFIX + "%",)),
        )
    before = snap()
    yield
    after = snap()
    assert after == before, (
        "本文件的用例碰到了真实（非测试）附件数据：判类型数 %s→%s，"
        "真实附件的读图台账行 %s→%s。这个文件 2026-10-21 已经污染过一次真实客户附件，"
        "任何改动都必须先确认测试数据与真实数据完全隔离" % (before[0], after[0],
                                                     before[1], after[1]))


def _fake_cfg():
    db.execute("""INSERT INTO llm_config(config_id, name, provider, purpose,
                                         model_name, enabled, base_url)
                  VALUES (%s,%s,'openai_compatible','vision_read','fake-model',
                          TRUE,'https://x')
                  ON CONFLICT (config_id) DO NOTHING""",
               (PREFIX + "cfg", PREFIX + "假配置（单测用，不出网）"))
    return PREFIX + "cfg"


@pytest.fixture
def cfg():
    cid = _fake_cfg()
    yield cid
    db.execute("DELETE FROM llm_config WHERE config_id=%s", (cid,))


def _mock_chat_vision(monkeypatch, content, ok=True, prompt_tokens=120,
                      completion_tokens=40, err_kind=None, message=None):
    """把 client.chat_vision 换成一个不出网的假实现。

    **必须 patch attachment_classify.client，不是 app.llm.client**——
    acx 用的是 `from ..llm import client` 之后的 `client.chat_vision(...)`，
    patch 错了模块对象，真实现还会被调用，测试就会真的出网。
    """
    def fake(cfg, system, user, image_path, max_tokens=4000):
        assert os.path.exists(image_path), "传给 chat_vision 的图片路径不存在"
        res = Result("vision", ok, model_echo=cfg.get("model_name"),
                    err_kind=err_kind, message=message, latency_ms=10,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        return res, (content if ok else ""), \
            {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    monkeypatch.setattr(acx.client, "chat_vision", fake)


# ─────────────────────────────────────────────────────────────────────────────
# 纪律一 · 类型映射完整且不越界
# ─────────────────────────────────────────────────────────────────────────────
def test_类型映射覆盖全部EN17值且不越界():
    en17_db = {r["value"] for r in db.query(
        "SELECT value FROM enum_value_current WHERE enum_id='EN17'")}
    en17_mapped = {v[1] for v in acx.TYPES.values()}
    assert en17_mapped <= en17_db, "TYPES 里有不在 EN17 枚举表里的值：%s" % (en17_mapped - en17_db)
    assert en17_db <= en17_mapped, "EN17 有值没被任何编码覆盖：%s" % (en17_db - en17_mapped)


def test_提示词里的类型菜单和TYPES一致():
    menu = acx._type_menu()
    for code, (label, _, _) in acx.TYPES.items():
        assert code in menu and label in menu, "菜单里缺了 %s %s" % (code, label)


# ─────────────────────────────────────────────────────────────────────────────
# 纪律二 · 把握不到「高」不回写字段
# ─────────────────────────────────────────────────────────────────────────────
def test_把握高才回写attach_type(att, cfg, monkeypatch):
    _mock_chat_vision(monkeypatch,
        "类型编码: A04\n把握: 高\n依据: 标题写着住宿费发票\n备注: 无")
    r = acx.classify_one(att, config_id=cfg)
    assert r["applied"] is True
    row = db.query_one("SELECT attach_type, type_code, type_source, type_conf, "
                       "typed_by FROM attachment WHERE id=%s", (att,))
    assert row["attach_type"] == "住宿费发票"
    assert row["type_code"] == "A04"
    assert row["type_source"] == "读图判定"
    assert row["type_conf"] == "高"
    assert row["typed_by"] == r["vision_id"]


def test_把握中不回写字段但台账要有(att, cfg, monkeypatch):
    """「中」等于模型自己都说拿不准，不该由它替人做决定——
    但这次调用必须留痕，不能因为没采信就当没发生过。"""
    _mock_chat_vision(monkeypatch,
        "类型编码: A03\n把握: 中\n依据: 像是交通票但看不清具体内容\n备注: 有疑虑")
    r = acx.classify_one(att, config_id=cfg)
    assert r["applied"] is False
    assert db.scalar("SELECT attach_type FROM attachment WHERE id=%s", (att,)) is None
    assert db.scalar("SELECT count(*) FROM attachment_vision WHERE attachment_id=%s",
                     (att,)) == 1, "没留痕——这次调用的记录丢了"


def test_把握低不回写字段(att, cfg, monkeypatch):
    _mock_chat_vision(monkeypatch, "类型编码: A04\n把握: 低\n依据: 很模糊\n备注: 无")
    r = acx.classify_one(att, config_id=cfg)
    assert r["applied"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 纪律三 · 判不出 / 越界编码都不强行纠正
# ─────────────────────────────────────────────────────────────────────────────
def test_模型答无法判断时留空(att, cfg, monkeypatch):
    _mock_chat_vision(monkeypatch,
        "类型编码: 无法判断\n把握: 低\n依据: 图片糊了\n备注: 无")
    r = acx.classify_one(att, config_id=cfg)
    assert r["type_code"] is None and r["applied"] is False


def test_模型编了个不存在的编码当判不出处理(att, cfg, monkeypatch):
    """猜它想说哪个编码，比直接判不出更危险——**不纠正，不映射到最接近的**。"""
    _mock_chat_vision(monkeypatch,
        "类型编码: A99\n把握: 高\n依据: 瞎编的\n备注: 无")
    r = acx.classify_one(att, config_id=cfg)
    assert r["type_code"] is None and r["applied"] is False
    # 台账里原始内容必须留着，供人回查模型当时到底说了什么
    content = db.scalar("SELECT content FROM attachment_vision WHERE attachment_id=%s",
                        (att,))
    assert "A99" in content


def test_调用失败时也要留一行且不回写(att, cfg, monkeypatch):
    _mock_chat_vision(monkeypatch, "", ok=False, err_kind="timeout", message="超时")
    r = acx.classify_one(att, config_id=cfg)
    assert r["ok"] is False and r["applied"] is False
    row = db.query_one("SELECT ok, err_kind, message FROM attachment_vision "
                       "WHERE attachment_id=%s", (att,))
    assert row["ok"] is False and row["err_kind"] == "timeout"


# ─────────────────────────────────────────────────────────────────────────────
# 纪律四 · 成败都落台账，永不覆盖
# ─────────────────────────────────────────────────────────────────────────────
def test_重读不覆盖旧行而是新增attempt(att, cfg, monkeypatch):
    _mock_chat_vision(monkeypatch, "类型编码: A04\n把握: 中\n依据: 第一次\n备注: 无")
    acx.classify_one(att, config_id=cfg)
    _mock_chat_vision(monkeypatch, "类型编码: A04\n把握: 高\n依据: 第二次\n备注: 无")
    acx.classify_one(att, config_id=cfg)
    rows = db.query("SELECT attempt, reason FROM attachment_vision "
                    "WHERE attachment_id=%s ORDER BY attempt", (att,))
    assert [r["attempt"] for r in rows] == [1, 2]
    assert rows[0]["reason"] == "第一次" and rows[1]["reason"] == "第二次"


def test_不是xlsx内嵌来源的附件拒绝读图(cfg):
    """演示数据的类型是生成器直接给的，不该被读图覆盖，也不该花这个钱。"""
    bill = PREFIX + "DEMO"
    db.execute("""INSERT INTO expense_bill(bill_no, doc_type, biz_type, is_demo)
                  VALUES (%s,'差旅费报账单','国内差旅费',TRUE) ON CONFLICT DO NOTHING""",
               (bill,))
    aid = db.scalar("""INSERT INTO attachment(bill_no, file_name, source_kind)
                        VALUES (%s,'演示票据.pdf','演示合成') RETURNING id""", (bill,))
    try:
        with pytest.raises(ValueError, match="演示合成"):
            acx.classify_one(aid, config_id=cfg)
    finally:
        db.execute("DELETE FROM attachment WHERE id=%s", (aid,))
        db.execute("DELETE FROM expense_bill WHERE bill_no=%s", (bill,))


# ─────────────────────────────────────────────────────────────────────────────
# 纪律五 · 空列表 ≠ 全部
# ─────────────────────────────────────────────────────────────────────────────
def test_批量传空列表时一条都不处理(cfg, monkeypatch):
    """原来 doclib_extract 那四处同类陷阱的同族问题：空列表若被当「未传」，
    就会退化成按「待分类清单」挑一批出来调模型——这里的代价是真实的调用费。"""
    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        return Result("vision", True), "类型编码: A04\n把握: 高\n依据: x\n备注: 无", \
            {"prompt_tokens": 1, "completion_tokens": 1}
    monkeypatch.setattr(acx.client, "chat_vision", fake)

    r = acx.run(attachment_ids=[], limit=20, config_id=cfg)
    assert r["tried"] == 0
    assert calls["n"] == 0, "空列表却真的发起了 %d 次调用" % calls["n"]


def test_批量不传attachment_ids才按待分类清单挑(att, cfg, monkeypatch):
    """**这条用例踩过一次真实的数据污染**（2026-10-21）：

    第一版直接调 `acx.run(attachment_ids=None, limit=5, ...)`，而 `pending()`
    不区分测试数据和真实客户附件——它按 `bill_no` 排序从**整个库**里抓，
    limit=5 时连测试附件带真实附件一起抓了进来。mock 拦的是 `chat_vision`
    这个函数本身，不管传进去的是谁的图，于是**假的分类结果被写进了
    12 条真实客户附件**（model_name='fake-model' 的台账行，已手工回滚+清理）。

    教训：验证「不传 ids 时走 pending() 清单」这件事，**不能真的调用会读
    整库 pending 集合的 run()**——哪怕 chat_vision 被 mock 了，写回
    attachment 表的仍然是真实行。应该连 `pending()` 本身也换成假实现，
    这样不管真实库里有多少条待分类附件，这条用例都不可能碰到它们一根毫毛。
    """
    monkeypatch.setattr(acx, "pending",
                        lambda limit=500: [{"id": att, "bill_no": PREFIX + "0001",
                                            "file_name": "测试票据.png",
                                            "seq": 1, "byte_size": 100, "attempts": 0}])
    _mock_chat_vision(monkeypatch, "类型编码: A04\n把握: 高\n依据: x\n备注: 无")
    r = acx.run(attachment_ids=None, limit=5, config_id=cfg)
    assert r["tried"] == 1
    assert r["items"][0]["attachment_id"] == att


def test_批量上限钉在100条():
    """limit 传得再大，一次也不许超过 100 条——那是硬上限，不是建议值。"""
    import inspect
    src = inspect.getsource(acx.run)
    assert "min(100" in src, "run() 的批量上限钉子被改掉了"


