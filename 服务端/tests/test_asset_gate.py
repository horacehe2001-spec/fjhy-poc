# -*- coding: utf-8 -*-
"""规则资产装载的那道闸：**顺序点反了必须拦住，而且拦的理由要说得出来。**

为什么需要这道闸——实测依据：

  `rule_concept` 上没有指向 `ontology_concept` 的外键
  （pg_constraint 里只有指向 rule 的那一条）。
  所以「先装规则、后装本体」或者「本体改了没重装」时，
  71 个概念引用会**静默**落进库里指向不存在的概念：
  不报错、不回滚，装载脚本的输出里一个字都看不出来，
  只是从此每条引用它的规则都判不出，而页面上一切正常。

这类「不报错的错」是最贵的，所以闸装在装载器自己身上（命令行也绕不过），
并且在这里钉死：闸的分档、拦的方向、以及**装载脚本的地盘边界**。

还钉一条文案：2026-10-20 之前，页面和接口都写着
「不做按钮，因为它会清空 release_current」——而代码里早就不清了。
一句关于自己的假话，挡住的是「客户能不能自助搭本体」这个功能，所以它不许回来。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rules import loader as rl  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = io.open(os.path.join(ROOT, "app", "rules", "loader.py"), encoding="utf-8").read()
API = io.open(os.path.join(ROOT, "app", "api", "onto.py"), encoding="utf-8").read()


# ── 闸的分档 ────────────────────────────────────────────────────────────────
def test_闸在当前库上是过的():
    """真库上跑一遍：现在应该是过的。过不了说明环境坏了，别让后面的断言骗人。"""
    g = rl.precheck()
    assert set(g) == {"ok", "checks", "reason"}
    assert g["ok"] is True, g["reason"]
    assert g["reason"] is None


def test_四档拦一档警():
    """概念/实体/本体在不在、同不同步是 blocker；节点是 warning。

    **节点只能是 warning**：实测库里就有一条 binding_node='OUT' 绑不上
    （process_node 里查无此节点，而 rule.binding_node 没有外键）。
    把它定成 blocker，今天起谁也装不了规则资产——
    用一道新闸把既有的功能锁死，比没有这道闸更糟。
    """
    g = rl.precheck()
    lv = {c["check"]: c["level"] for c in g["checks"]}
    assert len(lv) == 5
    assert sum(1 for v in lv.values() if v == "blocker") == 4
    assert sum(1 for v in lv.values() if v == "warning") == 1
    assert "节点" in [c for c in g["checks"] if c["level"] == "warning"][0]["check"]


def test_没过的那一项必须解释清楚():
    """灰着的按钮不给理由，和没有按钮一样挡路。

    通过的那些只要求不为空：「当前版本 0.1」就够了，没必要凑字数。
    """
    for c in rl.precheck()["checks"]:
        assert c["detail"].strip(), c["check"]
        if not c["ok"]:
            assert len(c["detail"]) > 20, "没过却不解释：" + c["check"]


def test_节点警告点名了OUT():
    """已知欠账要在每次装载时露脸，而不是躺在库里没人知道。"""
    node = [c for c in rl.precheck()["checks"] if c["level"] == "warning"][0]
    if not node["ok"]:
        assert "OUT" in node["detail"]
        assert "P4" in node["detail"], "欠账要说清楚归谁清，否则就是永远的噪音"


# ── 拦的方向：两个方向都测 ──────────────────────────────────────────────────
class _FakeDB:
    """把「本体没装」这种情况造出来。真库上没法造，而这恰恰是要拦的那一种。"""

    def __init__(self, current=None, rows=None):
        self._current = current
        self._rows = rows or {}

    def query_one(self, sql, params=()):
        return self._current

    def query(self, sql, params=()):
        for k, v in self._rows.items():
            if k in sql:
                return v
        return []


def _hash_of_yaml():
    import hashlib

    from app import config
    return hashlib.sha256(io.open(config.ONTOLOGY_YAML, "rb").read()).hexdigest()


def test_本体没装就拦住(monkeypatch):
    monkeypatch.setattr(rl, "db", _FakeDB(current=None))
    g = rl.precheck()
    assert g["ok"] is False
    assert "第②步" in g["reason"] or "本体" in g["reason"]


def test_源文件改了没重装就拦住(monkeypatch):
    monkeypatch.setattr(rl, "db", _FakeDB(
        current={"version": "0.1", "source_hash": "对不上的哈希"}))
    g = rl.precheck()
    assert g["ok"] is False
    sync = [c for c in g["checks"] if "同步" in c["check"]][0]
    assert sync["ok"] is False
    assert "旧本体" in sync["detail"] or "重装" in sync["detail"]


def test_概念缺了就拦住(monkeypatch):
    """本体在、也同步，但库里就是没有那些概念——照样不许装。"""
    monkeypatch.setattr(rl, "db", _FakeDB(
        current={"version": "0.1", "source_hash": _hash_of_yaml()},
        rows={"ontology_concept": [], "ontology_entity": [], "process_node": []}))
    g = rl.precheck()
    assert g["ok"] is False
    c = [x for x in g["checks"] if "概念" in x["check"]][0]
    assert c["ok"] is False
    assert "不会报错" in c["detail"], "要说清后果：静默，而不是报错"


def test_闸没过就不许装(monkeypatch):
    monkeypatch.setattr(rl, "precheck", lambda: {
        "ok": False, "checks": [], "reason": "造出来的理由"})
    with pytest.raises(RuntimeError) as e:
        rl.load_all(verbose=False)
    assert "造出来的理由" in str(e.value)


def test_force能越过闸但必须是显式的():
    """命令行留了 --force，接口没有。签名里有 force 才算留了这条路。"""
    import inspect
    assert "force" in inspect.signature(rl.load_all).parameters
    assert "--force" in SRC
    assert "明知有问题仍然装载" in SRC, "越闸必须打印原因"
    assert "load_all(force=" not in API, "接口永远不许越闸：要越得上机器敲命令行"
    assert "force=True" not in API


# ── 装载脚本的地盘边界 ──────────────────────────────────────────────────────
def _executed_sql():
    """装载器里**真的交给数据库执行**的那些 SQL 字面量。

    为什么不直接在源码文本里 grep：这份文件的 docstring 和注释里记着
    「原来这里是 DELETE FROM release_current」——那段历史要留着，
    不然下一个人会把它改回去。grep 会被这些话绊倒，
    于是要么测试长期红着，要么有人把历史删掉去迁就测试。两种都不该发生。
    """
    import ast
    out = []
    for node in ast.walk(ast.parse(SRC)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "execute"):
            continue
        for a in node.args:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append(" ".join(sub.value.split()))
    assert out, "一句 execute 都没捞到，说明这个解析写错了"
    return out



def test_只删自己的规则():
    """无条件 DELETE FROM rule 会把人采纳进来的规则静默删掉。"""
    assert "DELETE FROM rule WHERE created_by = %s" in SRC
    assert "'asset_loader'" in SRC


def test_只删自己的草稿release():
    """已发布 / 审阅中 / 已回滚的版本一律不碰；当前生效指针更不碰。"""
    sql = _executed_sql()
    assert any("DELETE FROM release WHERE created_by = 'asset_loader'" in q
               and "status = 'draft'" in q for q in sql),         "删 release 必须同时限定 created_by 和 draft，少一个就会碰到别人的版本"
    # **查的是真会执行的 SQL，不是源码文本。** 文件里有两处写着
    # 「原来这里是 DELETE FROM release_current」——一处注释、一处模块
    # docstring，那段历史要留着，不然下一个人会把它改回去。
    # 所以用 ast 把 execute() 的字面量 SQL 捞出来查，扫文本会被自己的注释绊倒。
    assert "DELETE FROM release_current" not in " ".join(_executed_sql()), \
        "当前生效指针是发布管理的事，装载脚本不碰"


# ── 文案：那句过期的解释不许回来 ────────────────────────────────────────────
def test_不许再对外说装载会清空release_current():
    """**查的是对外响应，不是源码文本。**

    源码里允许留注释解释这段历史（不然下一个人会把它改回去）；
    不允许的是**接口真的把这句话发出去**。这个区别不是洁癖：
    扫源码只是碰巧能查到，扫出口才算查到了。
    """
    from app.api import onto as api_onto
    note = api_onto.load_assets_note()
    blob = " ".join(str(v) for v in note.values())
    assert "清空 release_current" not in blob
    assert "没做成按钮" not in blob
    assert "why_not_a_button" not in note
    # 该说的要说到：动什么、绝不动什么、闸是什么
    assert note["touches"] and note["never_touches"] and note["gate"]
    assert "release_current 也不清" in note["never_touches"]


def test_L0感知那个出口也不许留那句():
    """同一句话还抄在素材感知 Agent 的输出里，那也是对外出口。"""
    from app import onto_build as ob
    d = ob.perceive()
    # **键和值一起查。** 「装载会动什么」是个键名，页面上也照样显示。
    blob = " ".join(str(k) + " " + str(v) for k, v in d.items())
    assert "清空 release_current" not in blob
    assert "故意没做成按钮" not in blob
    assert "装载会动什么" in blob or "装载命令" in blob


def test_接口拦住时给的是409():
    """409 Conflict：不是参数错（400），是当前状态不允许。"""
    assert 'HTTPException(409, g["reason"])' in API


def test_装载接口把保住的东西报出来():
    """「采纳的规则没被删」「release_current 没动」要在返回值里看得见——
    说了不算，返回数字才算。"""
    assert "adopted_kept" in API
    assert "release_current_untouched" in API


def test_闸的字段不许叫name():
    """**脱敏按字段名选策略，叫 name 的一律当人名打星。**

    实测踩过：字段叫 name 时，接口返回的五项检查名字全变成
    「规*************」，页面上那张检查表等于废了——而闸本身是对的，
    坏掉的只是「为什么灰」说不出来，这比闸失效更难查。
    项目里为这个坑已经赔过三次（规则名 / 本体名 / 服务名）。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    g = TestClient(app).get("/api/onto/load-assets/ready").json()
    # 和 precheck() 直出的那份逐字比：**过了一遍脱敏中间件之后还必须一模一样**。
    # 不能只查「有没有星号」——detail 里本来就有 **加粗** 的星号。
    raw = {c["check"]: c["detail"] for c in rl.precheck()["checks"]}
    assert len(raw) == 5
    for c in g["checks"]:
        assert "name" not in c, "闸的字段叫 name 会被脱敏打星"
        assert c["check"] in raw, "检查项名字被改过了：" + c["check"]
        assert c["detail"] == raw[c["check"]], "理由被改过了：" + c["detail"]
