# -*- coding: utf-8 -*-
"""规则明细：列映射、闭集合标签、以及不许绕过的那两道闸。

这一栏的产物从「条款」变成「规则明细」之后，最容易出错的三件事：

  ① **列错位**。表头 15 列、数据行 14 列，按位置硬映射会让「控制类型」
     读到「自定义」、「状态」读到更新时间。而错位后的明细**看起来完全正常**，
     没有任何技术信号。
  ② **把「取值待定」当成「列错位」**。源表的「实现途径」列，客户自己填的是
     「？」「通过OA控制？」——列是对的，值待定。混成一种，实测挡住 7 条真规则。
  ③ **模型的取值硬塞进闭集合**。模型给的「审核维度」是「差旅费报销」，
     而本体的维度是 D1..D10。硬塞会让一条维度错的规则看起来填得整整齐齐。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, doclib_extract as dx, rule_detail as rd  # noqa: E402

# 《系统审核算法.xlsx》的真实表头（15 列）与一行真实数据
ALGO_HEAD = ["序号", "算法编码", "算法类型", "算法分类", "算法名称", "算法描述",
             "审核算法规则", "控制类型", "算法来源", "配置方式", "创建人", "创建时间",
             "最后更新人", "最后更新时间", "状态"]
ALGO_ROW = ["1", "ITSH0006", "IT审核", "常用算法", "住宿发票开具时间校验",
            "住宿发票开票时间不能早于出差时间", "", "警告", "自定义", "配置器",
            "FJPG/管理员", "2026-09-18 16:16:32", "FJPG/管理员",
            "2026-10-12 15:25:10", "有效"]
# 《差旅费审核规则.xlsx》的真实表头（7 列）
RULE_HEAD = ["序号", "校验类型", "校验规则（需要进一步细化）", "实现途径", "校验方式",
             "是否已有", "评估共享是否可实现"]


# ─────────────────────────────────────────────────────────────────────────────
# 一、按表头名映射，不按列位置
# ─────────────────────────────────────────────────────────────────────────────
def test_列数与表头一致时精确映射():
    f, cm, ok, warn, notes = rd._align(ALGO_HEAD, ALGO_ROW)
    assert ok and not warn
    assert f["algo_code"] == "ITSH0006"
    assert f["control_type"] == "警告"          # 不是「自定义」
    assert f["configurator_status"] == "有效"    # 不是更新时间
    assert f["impl_path"] == "配置器"
    assert f["rule_name"] == "住宿发票开具时间校验"
    assert notes == []
    # 逐列留痕要全：映射对不对，不用翻 xlsx 就能核
    assert cm["控制类型"] == "警告" and cm["状态"] == "有效"


def test_空单元格丢位置就会整体错位():
    """**这是根因回归测试。**

    xlsx 解析如果把空单元格过滤掉（第一版就是），这一行会变成 14 个值，
    「控制类型」读到的就是「自定义」。这里直接拿那种坏数据来跑，
    要求映射**不许**给出错的闭集合值。
    """
    broken = [c for c in ALGO_ROW if c != ""]      # 模拟「过滤掉空格子」
    assert len(broken) == 14
    f, _cm, ok, warn, _n = rd._align(ALGO_HEAD, broken)
    if ok:
        # 允许它靠闭集合把对齐救回来，但**绝不许**把「自定义」当成控制类型
        assert f.get("control_type") in (None, "警告")
        assert f.get("configurator_status") in (None, "有效")
    else:
        assert "14 列" in warn and "15 列" in warn


def test_解析器必须保留空单元格():
    """写法测试：盯住 app/doclib.py 里那一行，别再被「优化」掉。"""
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "doclib.py"), encoding="utf-8").read()
    fn = src[src.index("def _xlsx_text"):src.index("def _xlsx_text") + 2600]
    # **只看代码，不看注释。** 那个函数的 docstring 里原样引用了第一版的坏写法
    # （「第一版写的是…」），照整段去匹配会红在注释上——第一版的测试就是这么红的。
    body = fn[fn.index('"""', fn.index('"""') + 3) + 3:]
    assert 'if v is not None and str(v).strip()' not in body, \
        "空单元格又被过滤掉了——列的位置会整体塌掉，这是实测踩过的根因"
    assert '"" if v is None else _one_line(str(v))' in body
    assert "while cells and not cells[-1]" in body, "尾部空列没裁，每行会变成几十列宽"


def test_解析器必须把单元格里的换行压掉():
    """**第二个结构损失。** 单元格里有换行时，一行会被拆成好几行文本。

    实测《财务共享业务场景…》的「专用审核要点」写的是
    「1.行程日期审核：…\\n2.金额核对：…」，不压换行的话整行断在第一句后面，
    后面几句变成几个 3～4 列的碎片行——列数对不上，要点也丢了大半
    （实测只剩「1.行程日期审核：」七个字）。
    docx 那一路早就 replace 了换行，xlsx 这一路漏了。
    """
    cells = dx.split_scenario_rows(
        "# 业务域\n"
        "业务域 | 系统单据 | 业务大类编码 | 业务大类 | 业务小类编码 | 业务小类 | "
        "业务场景释义 | 报账附件 | 专用审核要点 | 备注\n"
        "费用报账域 | 招待费报账单 | ZD001 | 业务招待费 | ZD001002 | 公务接待 | 释义 | "
        "附件 | 1.费用清单核对： 核对发票税目一致性 2.报销标准： 超标准不予报销 | \n")
    bodies = [c["body"] for c in cells]
    assert len(cells) == 2, bodies
    assert any("核对发票税目一致性" in b for b in bodies)
    assert any("超标准不予报销" in b for b in bodies), "第二条要点丢了"


# ─────────────────────────────────────────────────────────────────────────────
# 二、尾部截断 vs 中间空列
# ─────────────────────────────────────────────────────────────────────────────
def test_尾部空列按尾部处理():
    """Excel 导出会裁掉尾部空列。实扫 12 行 6 列（「评估共享」为空）。"""
    row = ["1", "付款主体校验", "付款主体需与核算主体完全一致", "系统规则", "警告", "是"]
    f, _cm, ok, warn, _n = rd._align(RULE_HEAD, row)
    assert ok, warn
    assert f["impl_path"] == "系统规则" and f["control_type"] == "警告"
    assert f["feasibility"] == "是"          # 不是被挤到「评估共享」里去
    assert "末尾" in warn


def test_并列时尾部截断优先():
    """**修过的漏报。**

    上面那一行，「尾部空列」和「是否已有空着」两种假设都过得了闭集合校验
    （前者 feasibility=是，后者 feasibility=空、评估共享=是）。
    第一版判成「两种对齐都说得通，不替人选」，于是 7 条干干净净的规则被挡下来。
    Excel 裁尾部空列是**已知机制**，中间某列恰好空着是**没有证据的假设**——
    并列时前者优先。
    """
    row = ["1", "付款主体校验", "付款主体需与核算主体完全一致", "系统规则", "警告", "是"]
    f, _cm, ok, _w, _n = rd._align(RULE_HEAD, row)
    assert ok, "并列时没给尾部截断优先权，真规则会被挡住"
    assert f["feasibility"] == "是"


def test_差太多列就不猜():
    f, _cm, ok, warn, _n = rd._align(ALGO_HEAD, ["1", "ITSH0006", "IT审核"])
    assert not ok
    assert "不猜" in warn
    assert f is None


# ─────────────────────────────────────────────────────────────────────────────
# 三、「取值待定」不等于「列错位」
# ─────────────────────────────────────────────────────────────────────────────
def test_取值不在闭集合时留空而不是判错位():
    """实扫：「实现途径」列客户自己填的是「？」「通过OA控制？」「AI智能审核？」。

    那是源表这一格本身待定，不是结构错。判成列错位会把真规则挡在外面；
    硬塞一个最接近的值进去，后面会被当成事实。**所以留空 + 记原值。**
    """
    row = ["24", "出差回填时间校验", "出差回填时间应不早于出差返回时间",
           "通过OA控制？", "警告", "", "无法实现"]
    f, _cm, ok, _w, notes = rd._align(RULE_HEAD, row)
    assert ok, "列是对齐的，不该判成错位"
    assert f["impl_path"] is None, "取值不在闭集合却被塞进了字段"
    assert f["control_type"] == "警告"
    assert notes and "通过OA控制？" in notes[0] and "留空" in notes[0]


# ─────────────────────────────────────────────────────────────────────────────
# 三点五、场景表：去重落在「每一条要点」上
# ─────────────────────────────────────────────────────────────────────────────
SCEN = "\n".join([
    "# 业务域",
    "业务域 | 系统单据 | 业务大类编码 | 业务大类 | 业务小类编码 | 业务小类 | "
    "业务场景释义 | 报账附件 | 专用审核要点 | 备注",
    # 同一条要点，两行（不同业务小类）——只该出一条规则
    "费用报账域 | 单人/多人差旅报销单 | CL006 | 国内差旅费 | CL006001 | 伙食补助费 | "
    "释义 | 附件 | 1.行程日期审核： 行程日期应在出差申请日期范围内 2.金额核对： "
    "确认结算单与发票金额是否一致 | ",
    "费用报账域 | 单人/多人差旅报销单 | CL006 | 国内差旅费 | CL006002 | 住宿费 | "
    "释义 | 附件 | 1.行程日期审核： 行程日期应在出差申请日期范围内 2.金额核对： "
    "确认结算单与发票金额是否一致 | ",
    # 最后一条要点多接了一句——那是**另一条规则**，只适用于这个小类
    "费用报账域 | 单人/多人差旅报销单 | CL006 | 国内差旅费 | CL006003 | 交通费 | "
    "释义 | 附件 | 1.行程日期审核： 行程日期应在出差申请日期范围内 2.金额核对： "
    "确认结算单与发票金额是否一致；机票保险费用不允许报销 | ",
    # 不在本次范围内的单据：必须跳过，并且报出跳过了多少
    "资产核算域 | 收入报账单 | SR001 | 收入 | SR001001 | 主营收入 | 释义 | 附件 | "
    "1.发票核对： 核对发票与合同 | ",
])


def test_场景表按要点去重而不是按行():
    """**修过的重复。** 客户按业务小类逐行重复填同一段要点，

    按行拆会得到一堆几乎一样的「规则」（实测 163 行拆出 41 条，
    其中要点 1~5 逐字相同、各发了 6 次）。去重要落在**每一条要点**上。
    """
    rows = dx.split_scenario_rows(SCEN)
    names = [r["clause_no"] for r in rows]
    assert len(names) == len(set(names)), "同一个条款号出了多条：%s" % names
    bodies = [r["body"] for r in rows]
    # 「行程日期审核」在三行里逐字相同 → 只许出一条
    assert sum(1 for b in bodies if "行程日期应在出差申请日期范围内" in b) == 1
    # 「金额核对」有两个版本（一个多接了机票保险）→ 两条都要在，它们是不同规则
    assert sum(1 for b in bodies if "确认结算单与发票金额是否一致" in b) == 2
    assert any("机票保险费用不允许报销" in b for b in bodies)


def test_场景表覆盖范围要记下来():
    """一条规则背后是 1 行还是 142 行，**必须看得出来**。

    不记的话，一条覆盖 142 行场景的规则和一条只在 1 个场景里出现过的规则
    在清单上长得一模一样，而它们的分量差一个量级。
    """
    rows = dx.split_scenario_rows(SCEN)
    r = [x for x in rows if "行程日期" in x["body"]][0]
    assert r["row_count"] == 3, r["row_count"]
    assert set(r["sub_classes"]) == {"伙食补助费", "住宿费", "交通费"}
    assert r["doc_type"] == "单人/多人差旅报销单"


def test_范围外的单据要跳过并报数():
    """本次 POC 只做差旅+接待两类单据（用户指定）。

    其余 15 类单据不取，**但跳过了多少行必须报出来**——
    静默跳过会让下一个人以为这张表只有这些。
    """
    rows = dx.split_scenario_rows(SCEN)
    assert all(r["doc_type"] in dx.SCENARIO_DOC_TYPES for r in rows)
    assert not any("核对发票与合同" in r["body"] for r in rows), "收入报账单那行没跳过"
    assert rows[0]["skipped_rows"] == 1, "跳过的行数没报出来"


def test_场景拆条是独立的一路来源():
    """不混进「列映射」：列映射是一行一条规则，场景拆条是多行去重后按条拆，

    证据链长相不同（后者要记覆盖了多少行）。混成一档，页面上就看不出
    哪条规则背后是 1 行、哪条背后是 142 行。
    """
    ck = db.query_one("""SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
                          WHERE conname='ck_drd_origin'""")
    assert ck and "场景拆条" in ck["d"], ck
    rows = rd.list_details(origin="场景拆条", limit=5)
    if not rows:
        pytest.skip("还没跑过场景拆条")
    for r in rows:
        assert r["source_kind"] == "场景表"
        assert "覆盖" in (r["note"] or ""), "没记覆盖范围"
        assert "需人工定名" in (r["note"] or ""), "名称是生成的，必须说出来"


# ─────────────────────────────────────────────────────────────────────────────
# 四、闭集合标签
# ─────────────────────────────────────────────────────────────────────────────
def test_标签都带依据():
    """**没有依据的标签等于没有。** 标签是用来筛的，筛错了就抽错样本。"""
    rows = rd.list_details(limit=50)
    if not rows:
        pytest.skip("还没有规则明细")
    for r in rows:
        for t in r["tags"]:
            assert (t.get("src") or "").strip(), \
                "%s 的标签「%s」没写依据" % (r["rule_name"], t["dim"])
            assert isinstance(t["sure"], bool)


def test_要素完整度是算出来的():
    """「还差什么才能进本体」必须在清单上就说清，不是等人点了采纳才报错。"""
    rows = rd.list_details(limit=50)
    if not rows:
        pytest.skip("还没有规则明细")
    r = rows[0]
    dims = [t["dim"] for t in r["tags"]]
    assert "要素完整度" in dims
    assert "来源类别" in dims and "可信度" in dims


def test_模型给的维度不在闭集就不采用():
    """实测模型给的「审核维度」是「差旅费报销」「人员身份，审批手续…」，

    一个都不在 D1..D10 里。**对不上就留空由人填，不硬塞。**
    """
    bad = db.query("""SELECT rule_name, dimension FROM doclib_rule_detail
                       WHERE origin='AI草稿' AND dimension IS NOT NULL""")
    for r in bad:
        assert r["dimension"] in rd.DIMS, \
            "%s 的维度「%s」不是 D1..D10" % (r["rule_name"], r["dimension"])


def test_闭集合标签不另存一张表():
    """七个闭集维度由明细的列派生。**存两处必然有一天对不上**——

    本项目在文件类别上踩过：文件库判一套、抽取判一套，
    清单上出现「文件库说单据、抽取说制度」，没人知道信哪个。
    doclib_detail_tag 只收**人**加的自由标签。
    """
    cols = {c["column_name"] for c in db.query(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name='doclib_detail_tag'""")}
    assert cols == {"detail_id", "tag", "tagged_by", "why", "tagged_at"}, cols
    # 人工标签必须有署名和理由（库里也有 CHECK，这里从业务口走一遍）
    with pytest.raises(ValueError):
        rd.add_tag("drd-不存在", "某标签", "", "理由")
    with pytest.raises(ValueError):
        rd.add_tag("drd-不存在", "某标签", "张三", "")


# ─────────────────────────────────────────────────────────────────────────────
# 五、不许绕过的两道闸
# ─────────────────────────────────────────────────────────────────────────────
def test_明细不许自己写规则表():
    """**写法测试。** 采纳只是标记。写进本体要填维度、优先级、绑定节点，

    那是「规则本体」那一栏的事。这里自动写进去，等于让没人填过的字段
    带着默认值进了规则清单。
    """
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "rule_detail.py"), encoding="utf-8").read()
    assert "INSERT INTO rule" not in src
    assert "insert into rule" not in src.lower().replace("insert into rule_", "")


def test_列对不齐的不许直接采纳():
    """列没对齐就采纳，等于把一条「控制类型可能读到隔壁列」的规则放进清单。"""
    row = db.query_one("SELECT detail_id FROM doclib_rule_detail WHERE NOT mapped_ok LIMIT 1")
    if not row:
        pytest.skip("当前没有列对不齐的明细（这是好事）")
    with pytest.raises(ValueError) as e:
        rd.review(row["detail_id"], "采纳", "张三")
    assert "列没对齐" in str(e.value)


def test_采纳驳回都要署名驳回要原因():
    row = db.query_one("SELECT detail_id FROM doclib_rule_detail LIMIT 1")
    if not row:
        pytest.skip("还没有规则明细")
    with pytest.raises(ValueError):
        rd.review(row["detail_id"], "采纳", "")
    with pytest.raises(ValueError):
        rd.review(row["detail_id"], "驳回", "张三", "")


# ─────────────────────────────────────────────────────────────────────────────
# 六、数值幻觉核对：误报与漏报同样致命
# ─────────────────────────────────────────────────────────────────────────────
SRC = ("可以为接待对象安排用餐 1 次，用餐标准按 150 元/人天执行。"
       "接待对象在 10 人以内的，陪餐人数不得超过三分之一；"
       "白酒每 500 毫升售价不得超过 500 元")


def test_列表序号不算幻觉():
    """**修过的误报。** 实测 8 条草稿报了 3 条存疑，逐条回查**全部是误报**：

    报的是判定逻辑里的列表序号「1.」「2.」「（6）」，
    而真正的阈值（150 元/人天、500/450/400 元、10 人）一个都没编。
    红标喊三次狼来了，第四次真有狼也没人看。
    """
    v = "1. 安排用餐 1 次；2. 标准 150 元/人天；（3）超 10 人；3）白酒 500 毫升 500 元"
    assert dx.check_numbers({"判定逻辑": v}, SRC)["suspect"] == []


def test_编出来的阈值必须命中():
    got = [s["value"] for s in dx.check_numbers({"判定逻辑": "标准 420 元/人天"},
                                                SRC)["suspect"]]
    assert got == ["420 元"]


def test_不许用子串比数字():
    """**修过的漏报。** 「陪餐不得超过 5 人」里的 5 原文没有，

    但原文有「150 元」——而 "5" 是 "150" 的子串，于是这条幻觉被放过了。
    """
    got = [s["value"] for s in dx.check_numbers({"判定逻辑": "陪餐不得超过 5 人"},
                                                SRC)["suspect"]]
    assert got == ["5 人"], "子串比对又回来了"


def test_小数阈值不被序号正则咬掉():
    """**修过的误报。** 「15.5 元」里的「15.」长得像列表序号，

    剔序号的正则把它咬掉，剩下「5 元」去比对，于是报成「5 元是编的」。
    点号后面跟数字就不是序号。
    """
    got = [s["value"] for s in dx.check_numbers({"判定逻辑": "上限 15.5 元"},
                                                SRC)["suspect"]]
    assert got == ["15.5 元"]


def test_原文里有的不报():
    v = "不超过 150 元，超 10 人按三分之一，白酒 500 毫升不超 500 元"
    assert dx.check_numbers({"判定逻辑": v}, SRC)["suspect"] == []
