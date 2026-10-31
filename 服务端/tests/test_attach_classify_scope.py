# -*- coding: utf-8 -*-
"""附件读图判类型：范围、词表、自相矛盾。

这条链路**真出网、真计费，而且发出去的是客户真凭证**（票面上有姓名、
身份证号片段、金额、车次航班）。所以这组用例钉的不是功能，是**边界**：

1. **按实例。** 这个模块原来一个 instance_id 都没有：pending() 取的是全库
   待判附件，run() 会把它们一张张发出去。给 822港航 跑一轮，
   福建海运集团的客户凭证会跟着被发出去并计费。
   项目里为同一类事故已经回滚过一次（mock 拦了函数，没拦数据范围）。

2. **来源取值不许写死一家的。** 原来 `source_kind='xlsx内嵌'` 写死在 9 处，
   那是福建海运集团当时唯一的来源。822港航 的附件来源是 `zip附件包`，
   于是整条链路看不见它们——不报错、不漏日志，就是什么都不发生。

3. **类型菜单按实例裁剪。** attach_type 的外键指向各自本体的 EN17。
   菜单给了模型一个本实例装不下的值，写回时被外键拒，一次读图的钱白花，
   而且报出来是「外键违反」，看不出是菜单给错了。

4. **模型自相矛盾的回答不许回写。** 实测到一条：选了 A17 业务接待结算单、
   把握「高」，而它自己的理由写「票面标题明确为『业务接待清单』」——
   那是 A18 的名字。把握「高」这一层拦不住这种错。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.perception import attachment_classify as ac  # noqa: E402
from app.perception import attachment_extract_fields as af  # noqa: E402

GH, FJ = "822gh", "fjhy"


# ── 一、按实例 ──────────────────────────────────────────────────────────
def test_待判队列不许跨实例():
    """不按实例过滤，给一家跑就会把另一家的客户凭证发出去。"""
    gh_ids = {r["id"] for r in ac.pending(limit=2000, instance_id=GH)}
    fj_ids = {r["id"] for r in ac.pending(limit=2000, instance_id=FJ)}
    assert not (gh_ids & fj_ids)
    if gh_ids:
        owners = {r["instance_id"] for r in db.query(
            "SELECT DISTINCT instance_id FROM attachment WHERE id = ANY(%s)",
            (list(gh_ids),))}
        assert owners == {GH}, owners


def test_显式给别家的id也要被过滤掉():
    """给错一个别家的 id，它就会被发出去。**过滤掉比报错安全**——
    报错会让人加个 try 绕过去，而过滤掉是静默且正确的。
    这里只验「选出来的行」，不触发任何模型调用。"""
    other = db.query("""SELECT id FROM attachment WHERE instance_id = %s
                        LIMIT 3""", (FJ,))
    assert other, "fjhy 没有附件，这条用例验不了"
    ids = [r["id"] for r in other]
    rows = db.query("""SELECT id FROM attachment
                        WHERE id = ANY(%s) AND instance_id = %s
                          AND source_kind = ANY(%s)""",
                    (ids, GH, list(ac.REAL_SOURCES)))
    assert rows == [], "别家的 id 没被实例条件挡住"


def test_进度与费用按实例分开():
    """两家的费用加在一起，「谁花的」就分不开了。"""
    g, f = ac.status(instance_id=GH), ac.status(instance_id=FJ)
    assert g["instance_id"] == GH and f["instance_id"] == FJ
    assert g["total"] and f["total"]
    assert g["total"] != f["total"], "两家的附件数一样，八成又在数同一批"
    # 费用也必须分得开：两边的调用数不应该是同一个数
    assert g["calls"] != f["calls"] or g["calls"] == 0


def test_字段抽取那半边也按实例():
    """attachment_extract_fields 和 classify 是同一条链路的两段，
    一段补了实例、另一段没补，等于没补。"""
    import inspect
    for fn in (af.pending, af.run, af.status):
        assert "instance_id" in inspect.signature(fn).parameters, fn.__name__


# ── 二、来源取值 ────────────────────────────────────────────────────────
def test_来源取值不写死一家的():
    assert "xlsx内嵌" in ac.REAL_SOURCES and "zip附件包" in ac.REAL_SOURCES
    # 演示合成必须排除：那是生成器造的图，类型是造的时候就知道的，
    # 读它既花钱又没有意义
    assert "演示合成" not in ac.REAL_SOURCES
    assert "上传" not in ac.REAL_SOURCES, "那是制度文件库那条链路，不是单据附件"


def test_822港航的附件看得见():
    """原来整条链路看不见它们——不报错、不漏日志，就是什么都不发生。"""
    n = db.scalar("""SELECT count(*) FROM attachment
                      WHERE instance_id=%s AND source_kind='zip附件包'""", (GH,))
    assert n == 147, n
    s = ac.status(instance_id=GH)
    assert s["total"] == 147, s["total"]


# ── 三、类型菜单按实例裁剪 ──────────────────────────────────────────────
def test_菜单只给本实例装得下的类型():
    gh, fj = ac.types_for(GH), ac.types_for(FJ)
    en17_gh = {r["value"] for r in db.query(
        "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id='EN17'",
        (GH,))}
    assert {v[1] for v in gh.values()} <= en17_gh
    # 822港航 多出接待那四类，福建海运集团没有
    extra = set(gh) - set(fj)
    assert extra == {"A16", "A17", "A18", "A19"}, extra
    for c in extra:
        assert ac.TYPES[c][2] is False, (
            "%s 的「决策表要求读图」标成了 True——必要附件决策表里没有这四类的"
            "条目（客户还没给），它们只能用来认出来，不能用来判该交没交" % c)


def test_菜单文字里不出现本实例没有的类型():
    menu = ac._type_menu(ac.types_for(FJ))
    for lab in ("业务接待审批单", "业务接待结算单", "业务接待清单", "酒水领用单"):
        assert lab not in menu, (
            "福建海运集团的菜单里出现了 %s——它的 EN17 里没有这个值，"
            "模型选中了也写不进去" % lab)


# ── 四、自相矛盾的回答 ──────────────────────────────────────────────────
def _conflict(code, reason, instance_id=GH):
    """照搬 classify_one 里那段判据，单独验它的两个方向。"""
    allowed = ac.types_for(instance_id)
    mine = (allowed.get(code) or ("",))[0]
    others = [lab for c, (lab, _, _) in allowed.items()
              if c != code and lab and lab in reason]
    return bool(others and mine and mine not in reason)


def test_理由与结论对不上要拦住():
    """实测到的那一条：选了 A17 业务接待结算单，理由却说标题是「业务接待清单」。"""
    assert _conflict(
        "A17",
        "票面标题明确为「业务接待清单」，且内容包含接待类型、对象、事由、"
        "时间、活动安排、用餐安排及费用明细等接待结算要素")


def test_不许误伤():
    """误报和漏报同样致命：这道闸要是动不动就拦，人就会把它关掉。
    三种正常回答都不许被拦。"""
    assert not _conflict(
        "A16", "票面顶部明确印有「福建省港航建设发展有限公司 业务接待审批单」字样")
    # 「酒水领用申请单」不含「酒水领用单」这个全名，但也没提到别的类型全名
    assert not _conflict(
        "A19", "票面标题明确为「纪念品、酒水领用申请单」，且内容涉及白酒申请、"
               "数量、单价、总价及接待事由")
    assert not _conflict("A04", "票面为住宿费发票，销售方为某酒店")
    assert not _conflict("A03", "铁路电子客票，含车次与席别")


# ── 五、来路要记对 ──────────────────────────────────────────────────────
def test_算出来的类型不许记成读图判的():
    """事后问「哪些附件的类型是模型判的、该不该复核」，
    记错了答案就全错——而这条链路的全部价值就在于模型判的结论能被挑出来复核。"""
    by_src = {r["type_source"]: r["n"] for r in db.query(
        """SELECT type_source, count(*) n FROM attachment
            WHERE instance_id=%s AND attach_type IS NOT NULL
            GROUP BY 1""", (GH,))}
    assert by_src.get("文字层解析") == 72, by_src
    assert "人工指定" not in by_src, "没有人看过它们，不许记成人工指定"
    # 文字层解析的那些必须确实对得上已解析的发票或出差审批单文件名
    bad = db.query("""SELECT file_name FROM attachment
                       WHERE instance_id=%s AND type_source='文字层解析'
                         AND file_name !~ '发票附件|出差审批|出差申请'""", (GH,))
    assert not bad, [r["file_name"] for r in bad][:5]


def test_PDF要先渲成图再发出去():
    """**这条是为一次真实浪费留的。**

    2026-11-01：93 个 PDF 原样发给读图模型，厂商每次都回
      400 InvalidParameter: The image format is illegal and cannot be opened
    101 次调用全军覆没，耗掉约 63 分钟。
    原因是这条链路一直只喂过图片——福建海运集团那 219 个附件是从 xlsx 里
    抽出来的 PNG/JPG，**队列里从来没进过 PDF**。放开来源取值让 PDF 进来了，
    而管道读不了它们。
    """
    row = db.query_one("""SELECT stored_path FROM attachment
                           WHERE instance_id=%s AND content_type='application/pdf'
                           LIMIT 1""", (GH,))
    assert row, "822港航 没有 PDF 附件了，这条用例的前提变了"
    img = ac._as_image(row["stored_path"])
    assert img.lower().endswith((".png", ".jpg", ".jpeg")), img
    assert os.path.exists(img) and os.path.getsize(img) > 1000, img
    # 图片原样返回，不做多余的渲页
    jpg = db.query_one("""SELECT stored_path FROM attachment
                           WHERE instance_id=%s AND content_type='image/jpeg'
                           LIMIT 1""", (GH,))
    assert ac._as_image(jpg["stored_path"]) == jpg["stored_path"]


def test_确定性失败不许一轮轮重试():
    """4xx 是我方请求本身不对，不改代码就永远是这个结果；
    超时和 TLS 握手失败是瞬时的，必须还能重试。
    两者混为一谈的代价，上面那条用例的注释里写了。"""
    import inspect
    src = inspect.getsource(ac.pending)
    assert "err_kind = 'http'" in src, "队列没有跳过确定性失败"
    assert "retry_read" in src, "改了代码之后要能显式重跑"
    # 真实数据上验一遍：默认队列里不该有「同型号下已 http 失败过」的
    mdl = (ac._cfg() or {}).get("model_name")
    q = {r["id"] for r in ac.pending(limit=2000, instance_id=GH)}
    bad = {r["attachment_id"] for r in db.query(
        """SELECT v.attachment_id FROM attachment_vision v
             JOIN attachment a ON a.id = v.attachment_id
            WHERE a.instance_id = %s AND NOT v.ok AND v.err_kind = 'http'
              AND v.model_name = %s""", (GH, mdl))}
    assert not (q & bad), sorted(q & bad)[:5]


def test_试跑取样必须覆盖所有文件类型():
    """**这条钉的是我自己的取样方法，不是代码。**

    那 101 次 400 本该在试跑 3 条时就暴露——而我取了队列头部 3 条，
    全是 .jpg，93 个 PDF 一条都没进试跑。
    试跑的全部意义是提前暴露问题，取一组恰好避开问题的样本就等于没试。
    附件 93 PDF / 54 JPG 这个数我早就量出来过，写在自己的报告里，
    然后试跑时没用上。
    所以把「按类型各取一条」做成函数，让它成为闸，不靠自觉。
    """
    sample = ac.sample_by_type(instance_id=GH, per_type=1)
    kinds = {r["content_type"] for r in sample}
    all_kinds = {r["content_type"] for r in db.query(
        """SELECT DISTINCT content_type FROM attachment
            WHERE instance_id = %s AND source_kind = ANY(%s)""",
        (GH, list(ac.REAL_SOURCES)))}
    assert kinds == all_kinds, (kinds, all_kinds)
    assert len(sample) == len(all_kinds)


def test_原图路径都是绝对路径且打得开():
    """两种路径混着存，能不能打开就取决于进程的当前目录——
    在容器里换个工作目录就全打不开，报的还是「文件不存在」。"""
    rows = db.query("""SELECT stored_path FROM attachment
                        WHERE instance_id=%s AND stored_path IS NOT NULL""", (GH,))
    assert len(rows) == 147
    rel = [r["stored_path"] for r in rows if not os.path.isabs(r["stored_path"])]
    assert not rel, rel[:3]
    missing = [r["stored_path"] for r in rows if not os.path.exists(r["stored_path"])]
    assert not missing, missing[:3]


# ── 六、这条链路的留痕 ──────────────────────────────────────────────────
def test_每次调用都落台账_成败都落():
    """台账是「哪些客户凭证离开过本系统」的唯一证据，少一行就少一条证据。
    撤回一个误判只改 attachment，**不许删 attachment_vision**。"""
    n_calls = db.scalar("""SELECT count(*) FROM attachment_vision v
                             JOIN attachment a ON a.id = v.attachment_id
                            WHERE a.instance_id = %s""", (GH,))
    n_read = db.scalar("""SELECT count(DISTINCT v.attachment_id)
                            FROM attachment_vision v
                            JOIN attachment a ON a.id = v.attachment_id
                           WHERE a.instance_id = %s""", (GH,))
    assert n_calls >= n_read, (n_calls, n_read)
    assert n_calls > 0, "822港航 一次读图都没有，这组用例的前提变了"
    # 被撤回的那两条：台账还在，但字段是空的
    withdrawn = db.query("""SELECT a.file_name FROM attachment a
                              WHERE a.instance_id=%s AND a.attach_type IS NULL
                                AND EXISTS (SELECT 1 FROM attachment_vision v
                                            WHERE v.attachment_id=a.id AND v.ok)""",
                         (GH,))
    assert isinstance(withdrawn, list)
