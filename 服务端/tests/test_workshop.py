# -*- coding: utf-8 -*-
"""本体工场：八步一条流水线。

这组测试守三件**坏了不报错**的事：

1. **顺序。** ③ 装规则资产必须排在 ② 装本体之后——`rule_concept` 没有指向
   `ontology_concept` 的外键，顺序反了不报错，只留下一批静默的坏引用。
2. **谁干的。** ②③ 是装载器、⑧ 是人，只有 ①④⑤⑥⑦ 是 Agent。
   这个标注丢了，页面就变成「AI 全自动搭本体」的幻觉。
3. **一键只跑到 ⑦。** ⑧ 改的是客户的源文件，进了一键链就是越权。

外加一条今天赔了两次学费的：**对外字段不许叫 name**。
脱敏是按字段名选策略的，叫 name 的一律当人名打星。第一次是装载闸的检查项
（「规*************」），第二次是八步的步骤名（「素***」）。两次都不报错，
坏掉的只是「页面上那段字变成星号」——所以这里**遍历整棵响应树**去查，
而不是逐个字段写断言：逐个写的下场就是下一个新字段再踩一次。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import onto_workshop as ws  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


# ── 顺序 ────────────────────────────────────────────────────────────────────
def test_装规则资产排在装本体之后():
    order = [s["step"] for s in ws.STEPS]
    assert order.index("装载规则资产") > order.index("装载本体源文件")


def test_执行顺序与产品步骤一一对上():
    """步骤覆盖的执行单元，顺序必须和链上的真实执行顺序一致。

    对不上就会出现「页面上第④步，实际第②个跑」——
    而页面是客户理解这条链的唯一依据。
    """
    covered = [u for s in ws.STEPS for u in s["covers"]]
    assert covered == [a["id"] for a in ws.AGENTS]


def test_每个执行单元都有runner():
    missing = [a["id"] for a in ws.AGENTS if a["id"] not in ws.RUNNERS]
    assert not missing, "没有 runner 的执行单元：%s" % missing


def test_不复制另外两条链的runner():
    """**引用，不复制。** 复制一份的下场是改了一处忘了另一处，
    然后「从本体页跑」和「从工场页跑」产出不一样，没人说得清哪个对。"""
    from app import onto_build_orchestrator as borch, onto_orchestrator as orch
    for k, v in borch.RUNNERS.items():
        assert ws.RUNNERS[k] is v, k
    for k, v in orch.RUNNERS.items():
        assert ws.RUNNERS[k] is v, k


# ── 谁干的 ──────────────────────────────────────────────────────────────────
def test_八步不是八个智能体():
    actors = {s["step"]: s["actor"] for s in ws.STEPS}
    assert actors["装载本体源文件"] == "装载器"
    assert actors["装载规则资产"] == "装载器"
    assert actors["采纳入本体"] == "人"
    assert sum(1 for a in actors.values() if a.startswith("智能体")) == 5


def test_一键只跑到第七步():
    assert ws.ONE_CLICK_LAST == 7
    one = {s["no"] for s in ws.steps()["steps"] if s["one_click"]}
    assert one == {1, 2, 3, 4, 5, 6, 7}
    adopt = [s for s in ws.steps()["steps"] if s["step"] == "采纳入本体"][0]
    assert adopt["one_click"] is False
    assert not adopt["covers"], "⑧ 不许有执行单元：它不是链上的一环"


def test_采纳那一步说清了要回到第二步():
    """采纳改的是源文件，不重装不生效。页面上不说，客户会以为已经进库了。"""
    adopt = [s for s in ws.STEPS if s["step"] == "采纳入本体"][0]
    assert "重装" in adopt["why"]


def test_装载器那一步不在任何一条智能体链里():
    """装载不是 Agent 干的活。它混进 Agent 列表就等于给了它 A 阶段的写库资格，
    而装载器的写入范围（整表重建）比任何 Agent 都大。"""
    from app import onto_build_orchestrator as borch, onto_orchestrator as orch
    ids = {a["id"] for a in borch.AGENTS} | {a["id"] for a in orch.AGENTS}
    assert "ws_load_ontology" not in ids
    assert "ws_load_assets" not in ids


# ── 闸 ──────────────────────────────────────────────────────────────────────
def test_第三步跑之前过闸且不过就抛():
    """闸没过要**抛**，不是记个警告继续跑。

    继续的后果是一批静默的坏引用，而调用方从返回值里看不出来。
    """
    import inspect
    src = inspect.getsource(ws._run_load_assets)
    assert "precheck()" in src
    assert "raise RuntimeError" in src
    assert src.index("precheck()") < src.index("load_all"), "闸要在装载之前"


# ── 对外字段：今天赔了两次学费的那个坑 ──────────────────────────────────────
def _walk(x, path="$"):
    """把响应树里所有 (路径, 键, 值) 摊平。"""
    if isinstance(x, dict):
        for k, v in x.items():
            yield path, k, v
            yield from _walk(v, path + "." + str(k))
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from _walk(v, path + "[%d]" % i)


OUTLETS = ["/api/onto/workshop/steps", "/api/onto/workshop/state",
           "/api/onto/workshop/progress", "/api/onto/workshop/agents",
           "/api/onto/load-assets/ready", "/api/onto/load-assets-note"]


@pytest.mark.parametrize("url", OUTLETS)
def test_对外字段不许叫name(url):
    """**脱敏按字段名选策略，叫 name 的一律当人名打星。**

    实测两次：闸的检查项变成「规*************」，八步的步骤名变成「素***」。
    两次都不报错。所以这里遍历整棵树——逐个字段写断言的下场就是
    下一个新字段再踩一次。
    """
    bad = [(p, k) for p, k, _ in _walk(client.get(url).json()) if k == "name"]
    assert not bad, "这些字段叫 name，会被脱敏打星：%s" % bad


@pytest.mark.parametrize("url", OUTLETS)
def test_中文串没有被打成星号(url):
    """脱敏真打星了也要在这里红。

    判据：一个中文字后面跟着三个以上连续星号——正常文案里的 **加粗**
    星号前面是中文标点或行首，不会长成这样。
    """
    import json
    import re
    blob = json.dumps(client.get(url).json(), ensure_ascii=False)
    hit = re.findall(r"[一-龥]\*{3,}", blob)
    assert not hit, "%s 里有被打星的中文：%s" % (url, hit[:3])


# ── 现状接口：没跑过也要能看 ────────────────────────────────────────────────
def test_没跑过也看得到库里的事实():
    """第一次打开时作业快照是空的，但库里有事实。
    只拿快照画页面，等于告诉客户「什么都没有」，而他上次明明搭过。"""
    st = client.get("/api/onto/workshop/state").json()
    assert st["onto"]["version"]
    assert st["onto"]["概念"] > 0
    assert st["assets"]["规则"] > 0
    assert "ok" in st["gate"]


def test_现状报的是当前版本不是两版加总():
    """`ontology_*` 里存着不止一个版本（0.1 和 v0.1-draft）。

    不带 version 条件地 count，会把两版加起来——实测一度把
    17 实体 / 121 概念报成了 34 / 240，而这个数是要写进汇报的。
    """
    from app import db
    st = client.get("/api/onto/workshop/state").json()
    v = st["onto"]["version"]
    assert st["onto"]["概念"] == db.scalar(
        "SELECT count(*) FROM ontology_concept WHERE version=%s", (v,))
    assert st["onto"]["概念"] < db.scalar("SELECT count(*) FROM ontology_concept")


# ── 感知 Agent 读的键，必须真的在 perceive() 的产出里 ──────────────────────
def test_感知读的每一个键都存在():
    """**实测踩过**：perceive() 里一个键改了名（「装载为什么没做成按钮」→
    「装载会动什么」），而读它的 L0 runner 没跟着改。

    全量 pytest 当时是 660 全绿——因为没有任何一条测试真的跑过那条链的 runner。
    于是第一次点「一键搭建」就 KeyError，整条链在第①步停住，
    后面七步全标「未执行」。链的失败语义是对的，坏的是没人替它守这个键。
    """
    import re

    from app import onto_build as ob, onto_build_orchestrator as borch
    src = open(borch.__file__, encoding="utf-8").read()
    body = src[src.index("def _run_perceive"):src.index("def _run_field")]
    have = set(ob.perceive())
    want = set(re.findall(r'p\["([^"]+)"\]', body))
    assert want <= have, "runner 读了 perceive() 里没有的键：%s" % sorted(want - have)


# ── 反向覆盖台账：0 不等于「一条都没覆盖」 ──────────────────────────────────
# **这一组钉的是一件已经发生过的事。** onto_rule_cover.(rule_id, version)
# 到 rule 是 ON DELETE CASCADE，第③步重建 rule 表会把它整表清空，
# 而全项目只有第⑦步的 onto_coord._save_align_locked() 会重建它。
# 实测（2026-10-20）：一次双击测试之后这张表从 82 掉到 0，
# 而页面上显示「规则覆盖 0」——看不出任何异常。
#
# 判据是确定性的：align_reverse() 的每个分支都 out.append()，
# 每条 version=1 的规则必然一行。所以「规则有行、覆盖 0 行」
# 不可能是正常跑完的结果。
#
# **两个方向都测。** 漏报让这张表继续静默留空；而误报更贵——
# 把「还没装规则」说成「台账坏了」，页面上常驻一条红字，
# 下一次真坏了就没人看了。
def test_覆盖为零要说成没算而不是没覆盖():
    c = ws.cover_check(82, 0)
    assert c["ok"] is False
    assert "没算" in c["detail"], c["detail"]
    assert "第⑦步" in c["detail"], c["detail"]


def test_规则表空着的时候覆盖为零是正常的():
    """**误报方向。** 还没跑第③步时规则表是空的，覆盖当然是 0。
    这时候报红，等于把「没开」说成「坏了」。"""
    c = ws.cover_check(0, 0)
    assert c["ok"] is True, c["detail"]


def test_数对不上也要报():
    """采纳进来的规则、重装过规则资产，都会让规则数变而覆盖台账是旧的。
    每条规则必然一行，所以数不相等就是不同步。"""
    assert ws.cover_check(82, 60)["ok"] is False
    assert ws.cover_check(82, 82)["ok"] is True


def test_判据和真实写入者对得上():
    """**前提没了判据就不成立。** 判据依赖两件事：
    ① align_reverse() 每条规则产出一行（每个分支都 append）
    ② onto_rule_cover 只有一个写入者
    哪天多了一个写入者、或者某个分支不再 append，这条会红。
    """
    import inspect
    from app import onto_coord as oc
    src = inspect.getsource(oc.align_reverse)
    # 一行 out.append，在循环体最后，所有分支都流到它
    assert src.count("out.append(") == 1, "分支数变了，每条规则一行这个前提要重核"
    whole = open(oc.__file__, encoding="utf-8").read()
    assert whole.count("INSERT INTO onto_rule_cover") == 1
    for mod in ("app/onto_build.py", "app/rules/loader.py", "app/onto_yaml.py"):
        txt = open(mod, encoding="utf-8").read()
        assert "INSERT INTO onto_rule_cover" not in txt, "%s 也写这张表了" % mod


def test_现状接口把失配摆出来():
    """**和「库里有什么」同一个接口给。** 分两个接口的下场是
    页面只拉了前一个，于是这件事又看不见了。"""
    st = client.get("/api/onto/workshop/state").json()
    assert "freshness" in st
    got = [c["check"] for c in st["freshness"]]
    assert "反向覆盖台账与规则表同步" in got, got
    for c in st["freshness"]:
        assert set(c) == {"check", "level", "ok", "detail"}, c
        assert c["level"] in ("blocker", "warning")


# **这里原本有一条「当前库里这两个数对得上」的基线自检，已撤掉。**
# 它当场就红了（规则 82 / 覆盖 0），而红的原因不是代码坏了——是本套
# 测试里任何一条跑过第③步（装载规则资产）的用例都会把这张表级联清空，
# 之后没有任何东西补跑第⑦步。也就是说它的结果取决于**跑了哪些文件、
# 什么顺序**。这种时绿时红的断言等于这道闸不可信，不如不要。
# 判据本身由上面四条纯函数测试钉住，不碰库。


def test_失配告警是纯文本():
    """**这一条告警不许用 markdown 强调。**

    前端模板里的 {{ }} 是纯文本插值，全项目一个 markdown 渲染器都没有
    （实测：前端 0 处 marked / 0 处 v-html），所以后端写的 `**加粗**`
    在页面上是原样显示两个星号。后端约 200 处文案都这么写着，
    怎么收是一条还没定的口径——但**这条告警自己要今天就读得通**：
    它存在的意义就是让人一眼看懂「0 不是没覆盖，是没算」，
    而一句带着星号的话会先让人以为页面坏了。
    """
    for args in [(82, 0), (82, 60), (0, 0), (82, 82)]:
        d = ws.cover_check(*args)["detail"]
        assert "**" not in d, (args, d)


# ── 演示可执行也要按版本算（和本体那侧对称） ───────────────────────────────
def test_演示可执行按当前版本算():
    """**和 test_现状报的是当前版本不是两版加总 对称。**

    本体那侧踩过：不带 version 条件地 count，把 17 实体 / 121 概念
    报成了 34 / 240，而这个数是要写进汇报的。规则这侧今天只有
    version=1（实测 82 条、demo_executable 12），所以这一条
    **今天两边都是 12，断言的是口径而不是当下的差值**——
    等 rule 表出现第二个版本时它才会分出真假，而那时候没人会想起来查。
    """
    from app import db
    st = client.get("/api/onto/workshop/state").json()
    assert st["assets"]["演示可执行"] == db.scalar(
        "SELECT count(*) FROM rule WHERE version=1 AND demo_executable")


def test_数演示可执行的地方都带版本():
    """**所有出口一起守，不只守页面那一个。**

    实测扫出三处漏了版本条件：onto_workshop.state（页面）、
    rules/loader 里写进 release.executable_count 的那次、
    以及命令行 status()。逐个写断言的下场是下一个新出口再漏一次。
    """
    import re
    import pathlib
    bad = []
    for f in pathlib.Path("app").rglob("*.py"):
        txt = f.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:count\(\*\)|CASE WHEN)[^;"]*?demo_executable', txt, re.S):
            seg = txt[max(0, m.start() - 200):m.end() + 200]
            if "version" not in seg:
                bad.append("%s:%d" % (f, txt[:m.start()].count(chr(10)) + 1))
    assert not bad, "这些地方数 demo_executable 没带版本条件：%s" % bad
