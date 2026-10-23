# -*- coding: utf-8 -*-
"""规则抽取：**契约说的事，代码里必须真的在做。**

启动自检（app/runtime/yaml_loader.py，270 项）只校验 YAML 的结构：
字段齐不齐、参数有没有 bounds、forbidden 写了没。它**管不了**
「YAML 声明的事情实现里有没有」——而这条链上一轮实测，恰恰是四处声明全部落空：

  sense.select 写「尚无草稿」        → 取数 SQL 里没有这个条件，每点一次重买同样 8 条
  triggers 写「一份文件的候选一起跑」 → ORDER BY c.seq 跨文件交错取
  act.idempotency 写了三段键         → 代码里零引用
  improve.params 写了 4 个可调参数    → 全库零引用，没有 agent_param 表

这组测试就是那道缺掉的闸：**声明与实现不一致要在 CI 里红，不是在客户现场红。**
"""
import io
import os
import re
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, doclib_extract as dx  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, "config", "agents")
SRC = io.open(os.path.join(ROOT, "app", "doclib_extract.py"), encoding="utf-8").read()

# 这条链上的两个 Agent。**只管这两个**：别的 Agent 的参数台账还没盘过，
# 把它们一起拉进来会让这个测试变成「全项目参数普查」，一改就红一片，
# 然后被人加 skip —— 那就等于没有这道闸。
CHAIN = ("fj_l1_02_clause_split", "fj_l1_03_rule_draft")


def _yaml(agent_id):
    return yaml.safe_load(io.open(os.path.join(AGENTS, agent_id + ".yaml"),
                                  encoding="utf-8").read())


# ─────────────────────────────────────────────────────────────────────────────
# 一、参数台账：要么真接线，要么自己说没接
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("agent_id", CHAIN)
def test_每个可调参数要么接线要么明说没接(agent_id):
    """**一页看着能调、实际不起作用的参数，比没有这页更糟。**

    所以每个参数必须二选一：
      wired_to: 指到具体的代码常量或配置列
      wired: false + wired_why: 说明为什么现在接不上
    """
    params = (_yaml(agent_id)["improve"] or {}).get("params") or {}
    assert params, "%s 一个可调参数都没声明" % agent_id
    for k, v in params.items():
        has = "wired_to" in v
        declared_unwired = v.get("wired") is False
        assert has or declared_unwired, (
            "%s.%s 既没写 wired_to 也没标 wired: false——"
            "它现在是文档还是开关，看不出来" % (agent_id, k))
        if declared_unwired:
            assert (v.get("wired_why") or "").strip(), \
                "%s.%s 标了没接线却没说为什么" % (agent_id, k)


@pytest.mark.parametrize("agent_id", CHAIN)
def test_接到代码常量的参数默认值必须相等(agent_id):
    """wired_to 指向 app/xxx.py:CONST 的，YAML 的 default 必须等于那个常量。

    不等的后果很隐蔽：页面上显示 500，代码里按 200 跑，
    于是「为什么只抽了 200 条」这个问题永远找不到答案。
    """
    params = (_yaml(agent_id)["improve"] or {}).get("params") or {}
    checked = 0
    for k, v in params.items():
        tgt = v.get("wired_to") or ""
        m = re.match(r"^app/([\w/]+)\.py:(\w+)$", tgt)
        if not m:
            continue
        mod, const = m.groups()
        assert mod == "doclib_extract", "这个测试只认得 doclib_extract：%s" % tgt
        assert hasattr(dx, const), "%s 指向的常量 %s 不存在" % (k, const)
        assert getattr(dx, const) == v["default"], (
            "%s.%s：YAML 写 %s，代码里是 %s"
            % (agent_id, k, v["default"], getattr(dx, const)))
        checked += 1
    assert checked, "%s 没有任何参数真的接到代码常量上" % agent_id


def test_抽规则的超时不许退回60秒():
    """**实测：一条 1476 字的条款要 150 秒。**

    闸设在 60 秒时，8 次调用 7 次在 62 秒被我们自己掐断——
    模型早就在生成，token 照算，结果丢弃，成功率 1/8。
    这不是模型不行，是闸设错了。谁把它调回 60，这条测试就红。
    """
    cfg = db.query_one("""SELECT config_id, model_name, timeout_s FROM llm_config
                           WHERE purpose='extract' AND is_default AND enabled""")
    if not cfg:
        pytest.skip("还没有为 extract 用途设默认模型")
    assert cfg["timeout_s"] >= 150, (
        "extract 用途的超时是 %s 秒，而实测一条条款要 150 秒。"
        "设在这个值下面，大部分调用会在我们自己这头被掐断（钱花了，结果扔了）"
        % cfg["timeout_s"])


# ─────────────────────────────────────────────────────────────────────────────
# 二、取数：成功过的不重抽，失败过的可重试
# ─────────────────────────────────────────────────────────────────────────────
def test_取数要去重且判据是答没答上来():
    """写法测试。两个方向都要钉：

      没有去重      → 排序确定性，每点一次把同样 8 条重新买一遍
      按「抽过」去重 → 上次超时的那 7 条被判死刑，再也不会被抽
    所以判据必须是 `rulable IS NOT NULL`（模型答上来了），不是 EXISTS 任意草稿。
    """
    fn = SRC[SRC.index("def run_ai("):SRC.index("def list_drafts(")]
    assert "NOT EXISTS" in fn, "run_ai 没有去重条件"
    assert "rulable IS NOT NULL" in fn, \
        "去重判据不是「模型答上来了」——按「抽过」判会让失败的条款永久失踪"


def test_取数按文件再按序号():
    """YAML 写的是「一份文件的候选一起跑」。只按 c.seq 排会跨文件交错取。"""
    fn = SRC[SRC.index("def run_ai("):SRC.index("def list_drafts(")]
    assert "ORDER BY f.file_name, c.seq" in fn, "取数没按文件分组"


# ─────────────────────────────────────────────────────────────────────────────
# 三、条款身份：重切不许把付费草稿清掉
# ─────────────────────────────────────────────────────────────────────────────
def test_条款id是内容定址且与迁移同算法():
    """id = dlc- + md5(file_id|策略|条款号|同号第几次)[:12]。

    Python 这边和 0028 迁移里的 SQL **必须同算法**，否则迁移改过的 id
    和这里重算的不一样，重切一次全部变成新条款，草稿又断一次。
    """
    import hashlib
    got = dx.clause_ident("dlf-abc", "条款体", "第十条", 1)
    want = "dlc-" + hashlib.md5("dlf-abc|条款体|第十条|1".encode("utf-8")).hexdigest()[:12]
    assert got == want
    # 同号第二次必须是另一个身份：实扫有 3 处同份文件条款号重复
    assert dx.clause_ident("dlf-abc", "条款体", "第十条", 2) != got
    # 换策略也是另一条条款（条款号口径从「第十条」变成「第5行」）
    assert dx.clause_ident("dlf-abc", "表格行", "第十条", 1) != got
    # 不含随机量：同样的入参两次必须一样
    assert dx.clause_ident("dlf-abc", "条款体", "第十条", 1) == got


def test_重切不许整份清空条款():
    """**修过的事故。** run_split 原来每份文件先整份 DELETE，

    而 doclib_draft.clause_id 是 ON DELETE CASCADE——
    实测 14 条付费草稿（一条 150 秒）连同采纳/驳回结论被静默清零。
    现在必须是 UPSERT + 只删这次没切出来的。
    """
    fn = SRC[SRC.index("def run_split("):SRC.index("# ── 清单与统计")]
    assert "ON CONFLICT (clause_id) DO UPDATE" in fn, "没有走 UPSERT"
    assert "NOT (clause_id = ANY(%s))" in fn, "没有「只删这次没切出来的」"
    assert "连带作废" in fn, "作废草稿这件事没有说出来——静默作废会被当成模型没跑"
    # 只看**切条款那一段**。单据分支里那句整份 DELETE 是对的：
    # 单据（报账单明细表、审批意见）本来就一条条款都不该有，
    # 之前误切进来的必须清掉，而它名下也不会有草稿。
    body = fn[fn.index("strategy, clauses = pick_strategy(text)"):]
    assert 'DELETE FROM doclib_clause WHERE file_id=%s", (f["file_id"],)' not in \
        body.replace("\n", " "), "切条款这一段还在整份清空"


def test_外键要能跟着id改():
    """clause_id 变更时子表必须跟着走，否则迁移/重算一次就把草稿孤立掉。"""
    # **按列过滤。** 这两张表各有两个外键（task_id / clause_id），
    # 不按列过滤就会去校验 task_id 那个——它本来就不需要 ON UPDATE CASCADE，
    # 于是测试红在一个没问题的约束上（第一版就是这么红的）。
    rows = db.query("""SELECT tc.table_name, kcu.column_name, rc.update_rule
                         FROM information_schema.table_constraints tc
                         JOIN information_schema.referential_constraints rc
                           ON rc.constraint_name = tc.constraint_name
                         JOIN information_schema.key_column_usage kcu
                           ON kcu.constraint_name = tc.constraint_name
                        WHERE tc.constraint_type='FOREIGN KEY'
                          AND kcu.column_name = 'clause_id'
                          AND tc.table_name IN ('doclib_draft','doclib_rule_source')""")
    assert len(rows) == 2, "查到 %d 个 clause_id 外键（期望 2）" % len(rows)
    for r in rows:
        assert r["update_rule"] == "CASCADE", \
            "%s.clause_id 没有 ON UPDATE CASCADE" % r["table_name"]


# ─────────────────────────────────────────────────────────────────────────────
# 四、工作表过滤：两个方向都测
# ─────────────────────────────────────────────────────────────────────────────
def test_联系人页不切条款():
    """实测事故：场景表切出来的 9 条全部是「业务域联系人」页的姓名+手机号，

    被当成「规则条款」入了库。
    """
    text = ("# 业务域联系人\n"
            "序号 | 业务域 | 编写人 | 联系电话\n"
            "1 | 费用报账域 | 张三三 | 13800138000\n"
            "# 差旅费\n"
            "序号 | 校验类型 | 校验规则 | 实现途径 | 校验方式 | 是否已有\n"
            "1 | 付款主体校验 | 付款主体需与核算主体完全一致 | 系统规则 | 警告 | 是\n")
    got = dx.split_table_rows(text)
    bodies = " ".join(c["body"] for c in got)
    assert "13800138000" not in bodies, "联系人页的手机号又被切成条款了"
    assert len(got) == 1 and "付款主体" in got[0]["body"], \
        "过滤把真正的规则行也吃掉了：%r" % got


def test_过滤不许吃掉带长数字的规则行():
    """**误报和漏报同样致命。**

    只在「列少 + 整列就是手机号」时判联系人行。
    规则条款里完全可能出现金额、编码、时间戳这类长数字——
    「行里有 11 位数字就扔」会把一条真规则静默扔掉，而且没人会发现。
    """
    text = ("# Sheet0\n"
            "1 | 住宿费标准控制 | 单笔住宿费不得超过 13800000 元 | 系统规则 | 禁止\n"
            "2 | 算法编码校验 | 算法编码 ITSH0006 必须有效 | 配置器 | 警告\n")
    got = dx.split_table_rows(text)
    assert len(got) == 2, "带长数字的规则行被当成联系人行扔了：%r" % got


def test_真联系人行被识别():
    assert dx._looks_contact_row(["费用报账域", "张三三", "13800138000"]) is True
    # 列多的不判：真表格里第 3 列是手机号的情况几乎不存在，宁可留着让人看见
    assert dx._looks_contact_row(
        ["1", "付款主体校验", "付款主体需与核算主体一致", "系统规则", "警告", "是"]) is False
    # 整列才算，嵌在句子里的不算
    assert dx._looks_contact_row(["1", "报备电话 13800138000 已登记"]) is False
