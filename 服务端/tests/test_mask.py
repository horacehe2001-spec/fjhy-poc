# -*- coding: utf-8 -*-
"""脱敏测试：正反双向 + 全出口覆盖。

两条纪律在这里落地：
  第 8 条 安全审查要覆盖全部出口，不能只看主输出路径
  第 9 条 检测规则的误报和漏报同样致命，正则写完必须两个方向都测

反例全部取自真实样本（金额 1374.80 / 34.11 / 3264 / 7209、
单据编号 FJPG-BX2610132121、业务小类编码 CL006004），不是编的。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, mask  # noqa: E402

FACT = json.load(io.open(config.FACT_MODEL_JSON, encoding="utf-8"))
POLICIES = {p["id"]: p for p in FACT["masking"]["policies"]}


# ─────────────────────────────────────────────────────────────────────────────
# 正向：该命中的必须命中
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", POLICIES["M02"]["must_hit"])
def test_M02_银行账号必须脱(value):
    out = mask.mask_card(value)
    assert out != value, "账号没被脱：%s" % value
    assert value not in out
    assert out.startswith(value[:6]) and out.endswith(value[-4:]), \
        "前 6 后 4 的口径变了：%s -> %s" % (value, out)


@pytest.mark.parametrize("value", POLICIES["M03"]["must_hit"])
def test_M03_手机号必须脱(value):
    out = mask.mask_mobile(value)
    assert out != value and value not in out


@pytest.mark.parametrize("value", POLICIES["M06"]["must_hit"])
def test_M06_自由文本里的姓名必须脱(value):
    out = mask.mask_free_text(value)
    assert out != value, "摘要里的姓名没被脱：%s" % value
    for n in mask.name_set():
        assert n not in out, "姓名 %s 仍在：%s" % (n, out)


def test_M05_API_Key_必须脱():
    s = "配置：api_key=sk-abcd1234efgh5678 model=qwen3.5-122b-a10b"
    out = mask.mask_text(s)
    assert "sk-abcd1234efgh5678" not in out
    assert "qwen3.5-122b-a10b" in out, "模型名不是敏感信息，不该被动"


def test_工号姓名组合格式只脱姓名段和工号段():
    # 审批记录的 approver 是「工号/姓名」，AI 节点是 aiAudit/null
    assert mask.mask_name("12345/乐知行") == "***45/林**"
    out = mask.mask_name("aiAudit/null")
    assert out.endswith("/null"), "AI 节点的 null 不该被当成姓名：%s" % out


# ─────────────────────────────────────────────────────────────────────────────
# 反向：正常业务内容一个字都不许动
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", POLICIES["M02"]["must_not_hit"])
def test_M02_不许误伤正常内容(value):
    assert mask.mask_card(value) == value, "误伤：%s" % value


@pytest.mark.parametrize("value", POLICIES["M03"]["must_not_hit"])
def test_M03_不许误伤正常内容(value):
    assert mask.mask_mobile(value) == value, "误伤：%s" % value


@pytest.mark.parametrize("value", POLICIES["M06"]["must_not_hit"])
def test_M06_不许误伤单位名和部门名(value):
    assert mask.mask_free_text(value) == value, (
        "误伤单位名/部门名：%s\n"
        "这就是不用通用姓名正则的原因——「闽运长乐公交公司」「厦门海隆」都是中文短词，"
        "任何人名模式都会把它们打掉。" % value)


def test_整句混合文本只脱该脱的():
    s = ("收款账号622908113255360583，手机13800138000，"
         "单据FJPG-BX2610132121，报泉州港务集团驻点差旅费（乐知行，9.28-9.29），"
         "金额1374.80，税金34.11，业务小类CL006004")
    out = mask.mask_text(s)
    # 该脱的
    assert "622908113255360583" not in out
    assert "13800138000" not in out
    assert "乐知行" not in out
    # 一个字都不许动的
    for keep in ("FJPG-BX2610132121", "1374.80", "34.11", "CL006004", "泉州港务集团"):
        assert keep in out, "误伤了 %s：\n%s" % (keep, out)


def test_闭集合内部前缀重叠按长名优先():
    mask.set_name_set(["岑柏", "岑柏舟", "鄢岚"])
    # 「岑柏舟」必须整体脱成「林**」，不能先被「岑柏」切成「林*禾」
    assert mask.mask_free_text("岑柏舟参加培训") == "林**参加培训"
    assert mask.mask_free_text("岑柏参加培训") == "林*参加培训"
    mask.set_name_set(mask._SEED_NAMES)       # 还原，避免污染后续用例


# ─────────────────────────────────────────────────────────────────────────────
# 结构化出口：scrub 递归
# ─────────────────────────────────────────────────────────────────────────────
def test_scrub_递归覆盖嵌套结构():
    payload = {
        "bill_no": "FJPG-BX2610132121",
        "amount": 1374.80,
        "payee": "乐知行",
        "payee_account": "622908113255360583",
        "summary": "报泉州港务集团驻点差旅费（乐知行，9.28-9.29）",
        "details": [
            {"expense_item": "伙食补助费", "amount": 200},
            {"expense_item": "市内交通费补贴", "amount": 160,
             "note": "鄢岚经办，联系13800138000"},
        ],
        "approvals": [{"approver": "12345/宦允文", "opinion": "同意，冼芷清已核"}],
        "llm": {"api_key": "sk-abcd1234efgh5678", "model_name": "qwen3.5-122b-a10b"},
        "nested": {"deep": {"deeper": {"text": "收款账号622908113255360583"}}},
    }
    out = mask.scrub(payload)
    leaks = mask.find_leaks(out)
    assert not leaks, "scrub 之后还有泄露：%s" % leaks
    # 对账抓手必须完好
    assert out["bill_no"] == "FJPG-BX2610132121"
    assert out["amount"] == 1374.80
    assert out["details"][0]["amount"] == 200
    assert out["llm"]["model_name"] == "qwen3.5-122b-a10b"
    assert out["llm"]["api_key"] == "***"
    # 单位名不许动
    assert "泉州港务集团" in out["summary"]


def test_scrub_不认识的字段名也要靠第二层兜住():
    """新加字段、换命名风格都不该漏——漏的那条路正是「只守主输出路径」。"""
    payload = {"some_brand_new_field_nobody_mapped": "账号622908113255360583 手机13800138000 鄢岚"}
    out = mask.scrub(payload)
    assert not mask.find_leaks(out), out


def test_scrub_对畸形输入不崩():
    deep = {"a": None}
    cur = deep
    for _ in range(200):
        cur["a"] = {"a": None}
        cur = cur["a"]
    mask.scrub(deep)          # 不抛异常即通过（内部有深度上限）
    assert mask.scrub(None) is None
    assert mask.scrub(123) == 123
    assert mask.scrub(True) is True


# ─────────────────────────────────────────────────────────────────────────────
# find_leaks 自身要可信：它必须能发现埋进去的雷
# ─────────────────────────────────────────────────────────────────────────────
def test_find_leaks_能发现未脱敏的内容():
    dirty = {"a": "622908113255360583", "b": {"c": "13800138000"}, "d": ["宦允文"],
             "e": "sk-abcd1234efgh5678"}
    kinds = {k for _, k, _ in mask.find_leaks(dirty)}
    assert kinds == {"card", "mobile", "name", "api_key"}, kinds


def test_find_leaks_对干净内容不报():
    clean = {"bill_no": "FJPG-BX2610132121", "amount": "1374.80", "tax": "34.11",
             "item": "CL006004", "org": "泉州港务集团", "ts": "2026-10-13 17:20:52"}
    assert mask.find_leaks(clean) == []


# ─────────────────────────────────────────────────────────────────────────────
# 策略与出口清单的完整性
# ─────────────────────────────────────────────────────────────────────────────
def test_每个敏感概念都能被_mask_concept_处理():
    for cid, field in mask.sensitive_concepts().items():
        out = mask.mask_concept(cid, "乐知行622908113255360583")
        assert out != "乐知行622908113255360583", "概念 %s（%s）没被脱" % (cid, field)


def test_出口清单至少覆盖九个():
    o = mask.outlets()
    assert len(o) >= 9, "出口清单只有 %d 个，少了就说明有旁路没列：%s" % (len(o), o)
    joined = " ".join(o)
    for must in ("审计接口", "调试", "配置", "错误", "导出", "日志", "大模型"):
        assert must in joined, "出口清单里没有「%s」这一类" % must


def test_关掉脱敏开关确实不脱_用来证明开关有效():
    """这个用例的目的是证明「开着的时候是真的在脱」，不是鼓励关掉它。"""
    old = config.MASK_MODE
    try:
        config.MASK_MODE = "off"
        assert mask.mask_text("622908113255360583") == "622908113255360583"
        assert mask.scrub({"payee": "乐知行"})["payee"] == "乐知行"
    finally:
        config.MASK_MODE = old
    assert mask.mask_text("622908113255360583") != "622908113255360583"


# ─────────────────────────────────────────────────────────────────────────────
# 回归：下面两条都是脱敏自检（find_leaks）在跑真实数据时抓出来的，
# 不是设计阶段想到的。钉死，免得再犯。
# ─────────────────────────────────────────────────────────────────────────────
def test_斜杠复合格式两种顺序都要脱对姓名():
    """姓名和工号的顺序不固定，不能按位置猜。

    approval_record.approver = 工号/姓名   如 1000504/甘梓宁
    expense_bill.payee       = 姓名/工号   如 岑柏/1000507
    第一版固定把斜杠后半当姓名，于是 payee 被脱成「岑柏/1******」——
    工号脱了、姓名裸着，正好反了。
    """
    mask.set_name_set(["甘梓宁", "岑柏"])
    try:
        a = mask.mask_name("1000504/甘梓宁")        # 工号/姓名
        assert "甘梓宁" not in a, a
        assert a.endswith("吕**"), a
        b = mask.mask_name("岑柏/1000507")          # 姓名/工号
        assert "岑柏" not in b, b
        assert b.startswith("林*"), b
        assert "1000507" not in b, b
        c = mask.mask_name("aiAudit/null")           # 两侧都不是纯数字
        assert c.endswith("/null"), c
    finally:
        mask.set_name_set(mask._SEED_NAMES)


def test_标识里嵌了姓名时位置型规则不许漏():
    """`SMP-岑柏` 这类占位工号，「保留后 2 位」保留的恰好是姓名。

    位置型规则（保留前 N 后 M）碰上「标识里嵌了姓名」必然漏，
    所以字段级脱敏的结果要再过一遍闭集合。
    实测这个漏洞让 16/40 张单的快照被判定泄露。
    """
    mask.set_name_set(["岑柏", "鄢岚"])
    try:
        out = mask.scrub({"emp_no": "SMP-岑柏", "payee": "岑柏/1000507",
                          "submitter_no": "SMP-鄢岚"})
        leaks = mask.find_leaks(out)
        assert not leaks, "仍有泄露：%s（脱敏后 %s）" % (leaks, out)
    finally:
        mask.set_name_set(mask._SEED_NAMES)


def test_真实快照脱敏后不得有泄露():
    """拿库里真实的一张单走一遍完整快照脱敏，用 find_leaks 验收。

    这是端到端的那道闸：单条函数都对，拼起来仍可能漏
    （漏的那次就是 payee 的斜杠顺序 + 占位工号嵌名两个问题叠加）。
    """
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from app.perception import reader
    from app import db
    bills = [r["bill_no"] for r in db.query("SELECT bill_no FROM expense_bill LIMIT 40")]
    if not bills:
        pytest.skip("库里没有样本数据，先跑 app/demo/seed.py")
    bad = []
    for bn in bills:
        fact, _ = reader.snapshot_fact(bn)
        payload, masked, leaks = reader.masked_payload(fact)
        if not masked:
            bad.append((bn, leaks[:3]))
    assert not bad, "%d 张单的快照脱敏后仍有泄露：\n%s" % (
        len(bad), "\n".join("  %s %s" % x for x in bad[:6]))


def test_标识不得长得像银行卡号():
    """uuid 的十六进制有概率出现 16 位纯数字，会被卡号正则误判成泄露。

    实测踩到过：signal_id 里的 6873475042104470。
    假泄露的代价不比真泄露小——它会让人开始忽略泄露告警。
    """
    import re as _re
    from app.audit import recorder as _rec
    bad = []
    for _ in range(3000):
        for sid in (_rec.new_signal_id(), _rec.new_cycle_id()):
            if _re.search(r"(?<![0-9])[0-9]{16,19}(?![0-9])", sid):
                bad.append(sid)
    assert not bad, "生成的标识里出现了会被当成卡号的数字串：%s" % bad[:3]


def test_白名单字段不做数字模式检查但仍查姓名():
    """对账抓手按设计不脱敏，拿卡号正则扫它们只会产生假泄露。"""
    # 单据号里有长数字串：不该报泄露
    assert not mask.find_leaks({"bill_no": "6873475042104470"})
    assert not mask.find_leaks({"cycle_id": "1234567890123456789"})
    # 但标识里出现姓名仍要报——标识里本来就不该有姓名
    mask.set_name_set(["乐知行"])
    try:
        assert mask.find_leaks({"bill_no": "BX-乐知行-001"})
    finally:
        mask.set_name_set(mask._SEED_NAMES)
    # 非白名单字段里的长数字串照旧要报
    assert mask.find_leaks({"some_field": "6229081132553605"})
