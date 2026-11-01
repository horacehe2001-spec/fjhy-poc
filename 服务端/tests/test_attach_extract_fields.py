# -*- coding: utf-8 -*-
"""附件字段抽取的**解析与回写闸**（不出网、不写库）。

这一组钉的是 2026-11-01 读图批里真实踩过/差点踩过的坑。和
test_822gh_attach_data.py 的分工：那边测「库里是什么」，这边测
「给定一段模型回答，解析和回写该怎么做」——后者不依赖当前库里的数据，
改了口径立刻会红。

坑的清单（每条下面都有一个用例）：

1. **标签里混进非汉字，整份回答静默全空**。`_LINE_RE` 是 `([一-龥]+)[:：]`，
   票面原文「工作餐/简餐安排」带斜杠，按原文去问就一个字段都解不出来，
   而回答看起来是正常的——不报错、不抛异常，只是 fields 变成 {}。

2. **金额口径被悄悄放开**。2026-10-22 用户决策：这一轮只抽文字/日期/布尔。
   业务接待审批单票面上「标准（元/人）」「预算总额」是金额，**不问**。
   要不要放开是口径决策，决策权在用户，不在代码里顺手改。

3. **PDF 原样发出去**。厂商回 400「The image format is illegal」。
   A06 通知那 7 份全是扫描 PDF，少了渲页就是 7 次全废。

4. **meeting_notice 不给 instance_id**。那一列 NOT NULL，这条回写路径
   从来没跑通过——全库只有 1 行，是另一个程序写的，所以没人撞见。

5. **A16 这一桶里混着别的单子**。`纪念品、酒水领用申请单`、`简餐审批单`
   都被定类成了 A16。它们没有「接待时间」，放进 entertain_approval
   会让 TR-接待-01 的「审批单份数」虚高、整单被拖进 pending。

6. **活动起止反了**。notice_start/end 是 TR-差旅-30 的第三方判据，
   反过来的区间会让正常单据误报——误报和漏报同样致命。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.perception import attachment_extract_fields as F  # noqa: E402


# ── 1. 标签必须是纯汉字 ─────────────────────────────────────────────────
def test_字段标签全是汉字_否则整份回答静默全空():
    import re
    bad = [k for k in F._FIELD_LABELS
           if not re.fullmatch(r"[一-龥]+", k)]
    assert not bad, "这些标签 _LINE_RE 匹配不上，字段会静默全空：%s" % bad
    # 反向钉死：票面原文那个带斜杠的标签**不许**直接拿去问
    assert "工作餐/简餐安排" not in F._FIELD_LABELS
    assert "工作餐安排" in F._FIELD_LABELS


def test_每个提示词里问到的标签都在映射表里():
    """问了却没进 _FIELD_LABELS 的标签 = 白问：模型答了，解析器丢掉。"""
    import re
    for code, spec in F.FIELD_SPECS.items():
        for line in spec["prompt_fields"].splitlines():
            m = re.match(r"([一-龥]+)[:：]", line)
            if not m:
                continue
            key = F._FIELD_LABELS.get(m.group(1))
            assert key, "%s 问了「%s」但映射表里没有" % (code, m.group(1))
            assert key in spec["keys"], \
                "%s 问了「%s」→%s，但它不在 keys 里，解析后会被丢掉" % (
                    code, m.group(1), key)


# ── 2. 金额口径 ─────────────────────────────────────────────────────────
def test_这一轮一个金额字段都不许问():
    """2026-10-22 用户决策。业务接待审批单上「标准」「预算总额」是金额，
    票面上看得见，但**不问**——放开与否是口径决策，决策权在用户。"""
    money_keys = {"total_amount", "tax_amount", "tax_rate",
                  "meal_standard", "budget_total"}
    for code, spec in F.FIELD_SPECS.items():
        assert not (set(spec["keys"]) & money_keys), \
            "%s 的 keys 里混进了金额字段：%s" % (
                code, set(spec["keys"]) & money_keys)
    assert "预算总额" not in F.A16_PROMPT
    assert "标准" not in F.A16_PROMPT
    assert "不要回答任何金额" in F.SYSTEM


# ── 3. PDF 必须先渲页 ───────────────────────────────────────────────────
def test_pdf不许原样发出去_要先渲成图(monkeypatch, tmp_path):
    """厂商对 PDF 回 400「The image format is illegal」。
    A06 那 7 份通知全是扫描 PDF，少了这一步就是 7 次调用全废。"""
    seen = {}

    def fake_as_image(path):
        seen["asked"] = path
        return str(tmp_path / "rendered.png")

    monkeypatch.setattr(F, "_as_image", fake_as_image)
    monkeypatch.setattr(F.db, "query_one", lambda *a, **k: {
        "id": 1, "bill_no": "B1", "file_name": "通知.pdf",
        "stored_path": "/x/通知.pdf", "source_kind": F.REAL_SOURCES[0],
        "type_code": "A06", "instance_id": "822gh"})
    monkeypatch.setattr(F, "_cfg", lambda c=None: {
        "config_id": "c1", "model_name": "m", "temperature": 0})
    monkeypatch.setattr(F.db, "scalar", lambda *a, **k: 0)

    sent = {}

    def fake_vision(cfg, system, user, image_path, max_tokens=600):
        sent["path"] = image_path
        raise RuntimeError("stop-here")

    monkeypatch.setattr(F.client, "chat_vision", fake_vision)
    with pytest.raises(RuntimeError):
        F.extract_one(1)
    assert seen["asked"] == "/x/通知.pdf"
    assert sent["path"].endswith("rendered.png"), \
        "发出去的还是 PDF 原文件，渲页没接上"


# ── 4/5/6. 回写闸 ───────────────────────────────────────────────────────
class _Spy:
    """把 db.execute 记下来，不真写库。"""

    def __init__(self):
        self.calls = []

    def __call__(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def find(self, frag):
        return [c for c in self.calls if frag in c[0]]


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(F.db, "execute", s)
    return s


def test_会议通知回写必须带实例(spy):
    """meeting_notice.instance_id 是 NOT NULL。原来这条分支没给它，
    也就是说 A06/A07 的字段回写**从来没跑通过**——一跑就是 NOT NULL 违反，
    而全库 meeting_notice 当时只有 1 行（另一个程序写的），没人撞见。"""
    F._apply("A06", "B1", 9, {"notice_start": "2026-08-22",
                              "notice_end": "2026-08-28",
                              "notice_place": "北京"},
             "m", "高", "822gh")
    ins = spy.find("INSERT INTO meeting_notice")
    assert ins, "没往 meeting_notice 写"
    assert "instance_id" in ins[0][0]
    assert "822gh" in ins[0][1]


def test_活动起止反了就两个都丢掉(spy):
    """notice_start/end 是 TR-差旅-30 的第三方判据。反过来的区间
    会让正常单据误报——**宁可取不到，不许取错**。"""
    F._apply("A06", "B1", 9, {"notice_start": "2026-08-28",
                              "notice_end": "2026-08-22",
                              "notice_place": "北京"},
             "m", "高", "822gh")
    params = spy.find("INSERT INTO meeting_notice")[0][1]
    assert "2026-08-28" not in params and "2026-08-22" not in params
    assert "北京" in params, "地点是另一回事，不该被连坐"


def test_酒水领用单不许当成接待审批单入库(spy):
    """`纪念品、酒水领用申请单`（附件6）和 `简餐审批单` 都被定类成了 A16。
    它们没有「接待时间」。放进 entertain_approval 不会造成误报，
    但会让 TR-接待-01 的「审批单份数」虚高、整单被拖进 pending——
    看起来像「判过了但拿不准」，其实根本不该算它。"""
    got = F._apply("A16", "B1", 9,
                   {"dept": "综合办公室", "meal_persons": 3,
                    "gift_arrange": "习酒 2 瓶"},
                   "m", "高", "822gh")
    assert got is None
    assert not spy.find("INSERT INTO entertain_approval")


def test_手写纸质审批单只有接待时间也要入库(spy):
    """实测 FJPG-BX2603110060：手写表，经办人/承办部门/分管领导/主管领导
    **只有签名、没有日期**。它给不出审批日期（TR-接待-01 会 pending），
    但给出了接待日期、用餐场所、陪餐人数，是别的规则的输入——不许丢。"""
    got = F._apply("A16", "B1", 9,
                   {"entertain_at": "2026-02-06", "meal_place": "福州",
                    "meal_persons": 7},
                   "m", "中", "822gh")
    assert got and got.startswith("entertain_approval:ATT-9")
    assert spy.find("INSERT INTO entertain_approval")
    assert not spy.find("INSERT INTO approval_record"), \
        "票面上没有审批日期，不许凭空造一条流水"


def test_审批流水按份先删后插_不许按整张报账单删(spy):
    """一张报账单挂多份业务接待审批单（实测最多 9 份）。
    按 bill_no 删会把同一张单上另外几份的流水一起删掉。"""
    F._apply("A16", "B1", 9,
             {"entertain_at": "2026-07-15", "appr_initiate": "2026-07-15",
              "appr_chief": "2026-07-16"},
             "m", "高", "822gh")
    dels = spy.find("DELETE FROM approval_record")
    assert len(dels) == 1
    assert "app_no" in dels[0][0] and "ATT-9" in dels[0][1]
    assert "bill_no" not in dels[0][0], "删除作用域不许放大到整张报账单"
    ins = spy.find("INSERT INTO approval_record")
    assert len(ins) == 2
    nodes = [c[1][2] for c in ins]
    assert nodes == ["发起节点", "主管领导"]
    for c in ins:
        assert "业务接待审批" in c[0], "不标流程，报账流程图的实测次数就被污染"
        assert "审批单读图" in c[0]


def test_读图写进去的审批节点必须是本体枚举里的那四个():
    """approval_record.node_enum 是 GENERATED ALWAYS AS 'EN16'，
    外键按 (instance_id,'EN16',node) 校验。模型读出别的节点名，
    **对的做法是往本体加枚举值，不是放宽外键**——这里只认这四个。"""
    from app import db
    have = {r["value"] for r in db.query(
        """SELECT value FROM enum_value_current
            WHERE instance_id='822gh' AND enum_id='EN16'""")}
    assert {n for _, n in F.A16_NODE_KEYS} <= have


# ── 解析 ────────────────────────────────────────────────────────────────
def test_月份越界的日期判不出而不是插进去():
    """entertain_at 是 TIMESTAMPTZ，「13 月」插进去直接 500。
    拿不到比拿错好：TR-接待-01 对日期缺失的反应是 pending，不是命中。"""
    assert F._clean("接待时间", "2026-13-05") is None
    assert F._clean("接待时间", "2026-02-32") is None
    assert F._clean("接待时间", "2026年2月6日") == "2026-02-06"
    assert F._clean("主管领导审批日期", "2026-07-16 10:44:32") == "2026-07-16"


def test_接待类型要去掉勾选标记():
    """票面是「公务接待（ ）　商务接待（√）」，模型把勾一起抄回来。
    勾是标记不是值，留着会让「商务接待」和「商务接待（√）」变成两个取值。"""
    assert F._clean("接待类型", "商务接待（√）") == "商务接待"
    assert F._clean("接待类型", "公务接待( )") == "公务接待"
    assert F._clean("接待类型", "商务接待") == "商务接待"


def test_不确定和无一律变成空值而不是字符串():
    for raw in ("不确定", "无", "看不清", ""):
        assert F._clean("用餐场所", raw) is None


def test_一份完整回答能整段解出来():
    """实测回答原文（FJPG-BX2607162084 那张 OA 打印件的版式）。"""
    content = """接待部门: 综合办公室
接待类型: 商务接待
接待时间: 2026-07-15
用餐时间: 2026-07-15
用餐场所: 福州
陪餐人数: 6
工作餐安排: 无
用车安排: 无
住宿安排: 无
赠送纪念品: 无
发起节点审批日期: 2026-07-15
部门负责人审批日期: 2026-07-15
分管领导审批日期: 2026-07-16
主管领导审批日期: 2026-07-16
把握: 高"""
    f, conf = F._parse(content, "A16")
    assert conf == "高"
    assert f["dept"] == "综合办公室"
    assert f["entertain_at"] == "2026-07-15"
    assert f["meal_persons"] == 6
    assert f["appr_chief"] == "2026-07-16"
    assert f["work_meal"] is None          # 「无」是空值，不是字符串「无」
    # 这正是人工驳「业务审批清单，审批流程滞后」的那张：
    # 最后审批 07-16 晚于接待日期 07-15。
    assert max(v for k, v in f.items()
               if k.startswith("appr_") and v) > f["entertain_at"]


# ── 7. A01 逐日表与签名日期（2026-11-01 第四轮）─────────────────────────
# 守的是同一条纪律的另一面：第三轮**只抽表头就入库**，
# 规则把「没读」判成「没填」，一次造出 6 条误报。
# 这一组钉的是「读了多少必须记下来」，而不只是「读得对不对」。
@pytest.fixture
def a01(monkeypatch):
    """A01 回写要查一次 expense_bill.doc_date 当年份基准，这里给个假的。"""
    import datetime
    monkeypatch.setattr(F.db, "query_one",
                        lambda *a, **k: {"doc_date": datetime.date(2026, 4, 20)})
    monkeypatch.setattr(F.db, "scalar", lambda *a, **k: 0)


def _trip_params(spy):
    return spy.find("INSERT INTO trip_application")[0][1]


def test_逐日表的取值菜单必须正好是本体里的EN05和EN06():
    """trip_application.meal_situation/car_situation 有外键
    → enum_value_current。菜单和本体对不上，读出来的值一落库就 500；
    而把外键放宽是错的改法（那等于让模型自己编枚举值）。"""
    from app import db
    for enum_id, menu in (("EN05", F.EN05_MENU), ("EN06", F.EN06_MENU)):
        have = {r["value"] for r in db.query(
            """SELECT value FROM enum_value_current
                WHERE instance_id='822gh' AND enum_id=%s""", (enum_id,))}
        assert set(menu) == have, (enum_id, sorted(set(menu) ^ have))


def test_菜单以外的说法一律丢掉_不猜它想选哪个():
    assert F._menu_pick("自行用餐", F.EN05_MENU) == "自行用餐（含免费）"
    assert F._menu_pick("自行用餐（含免费）", F.EN05_MENU) == "自行用餐（含免费）"
    assert F._menu_pick("领导安排", F.EN05_MENU) is None
    assert F._menu_pick("空", F.EN05_MENU) is None


def test_手写表不写年份时按出差年份补():
    """实测 image_t0000000788：日期格手写「4 月 29 日」，没有年份。
    补不出年份的整行丢掉——一个没有年份的「月29日」落进 trip_calendar，
    判覆盖率时只会算错。"""
    rows, bad = F._day_rows("4月29日=自行用餐|自行用餐|自行用餐", 3,
                            F.EN05_MENU, 2026)
    assert rows == [("2026-04-29", ["自行用餐（含免费）"] * 3)], rows
    assert bad == 0
    rows2, bad2 = F._day_rows("月 日=自行用餐", 3, F.EN05_MENU, 2026)
    assert rows2 == [] and bad2 == 1


def test_年份读差十年的逐日行要被丢掉并且记成没读准():
    """实测 image_t0000000896/897：整页年份被读成 2016（实为 2026），
    而模型自报把握是「高」——**把握不是这类错误的检测手段**。
    丢掉这些行是对的；但丢完还说「读过了」，规则就会按剩下的 0 行
    报「逐日表缺 N 天」，那是误报。所以第二个返回值必须是 1。"""
    rows, bad = F._day_rows("2016-08-11=自行用餐|自行用餐|自行用餐", 3,
                            F.EN05_MENU, 2026)
    assert rows == [] and bad == 1


def test_三块都答了才算读过(spy, a01):
    F._apply("A01", "B1", 9,
             {"start_date": "2026-04-14", "end_date": "2026-04-15",
              "place": "莆田",
              "meal_days": "2026-04-14=自行用餐|自行用餐|自行用餐；"
                           "2026-04-15=自行用餐|自行用餐|自行用餐",
              "car_days": "2026-04-14=自行用车|自行用车；"
                          "2026-04-15=自行用车|自行用车",
              "backfill_date": "2026-04-16"},
             "m", "高", "822gh")
    assert _trip_params(spy)[-1] is True, "三块都给了明确答复，detail_read 该是 True"
    assert len(spy.find("INSERT INTO trip_calendar")) == 6, "两天 × 三个餐次"
    assert len(spy.find("DELETE FROM trip_calendar")) == 1, \
        "重读是新 attempt，不先清会累加，而累加只会把缺漏填平（漏报）"


def test_票面上空着和没读清是两回事(spy, a01):
    """「无」= 读了、票面上这张小表是空的 → 规则可以据此命中。
    「不确定」= 没读清 → 规则必须 pending。
    _clean 里这两个词原来都塌成 None，塌完就再也分不开了。"""
    assert F._clean("逐日用餐", "无") == ""
    assert F._clean("逐日用餐", "不确定") is None
    F._apply("A01", "B1", 9,
             {"start_date": "2026-04-14", "end_date": "2026-04-15",
              "meal_days": "", "car_days": "", "backfill_date": ""},
             "m", "高", "822gh")
    p = _trip_params(spy)
    assert p[-1] is True, "三块都答了「无」，这是读过了"
    assert not spy.find("INSERT INTO trip_calendar")


def test_有一块没读清就不算读过(spy, a01):
    F._apply("A01", "B1", 9,
             {"start_date": "2026-04-14", "end_date": "2026-04-15",
              "meal_days": "2026-04-14=自行用餐|自行用餐|自行用餐",
              "car_days": None,                      # 模型答了「不确定」
              "backfill_date": "2026-04-16"},
             "m", "高", "822gh")
    assert _trip_params(spy)[-1] is False, \
        "用车表没读清却标成读过了，规则会拿空值去报「缺 N 天」"


def test_签名日期也要过年份闸(spy, a01):
    """少这一道的后果实测过：image_t0000000896 整页年份读成 2016，
    起止日期被闸挡住了，签名日期 2016-08-13 却原样落了库——
    「有没有回填」判「有」是对的，值是错的。"""
    F._apply("A01", "B1", 9,
             {"start_date": "2016-08-13", "end_date": "2016-08-13",
              "meal_days": "", "car_days": "", "backfill_date": "2016-08-17"},
             "m", "高", "822gh")
    p = _trip_params(spy)
    assert "2016-08-17" not in p, "差十年的签名日期不许落库"
    assert p[-1] is False, "签名日期被闸丢掉了，就不算读过"
