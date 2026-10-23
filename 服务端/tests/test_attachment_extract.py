# -*- coding: utf-8 -*-
"""xlsx 内嵌附件抽取：只读、幂等、不判类型、对不上的如实报。

这个文件钉的是这条链的四条纪律，外加一条口径。

背景：0004 的表注释写着「附件源数据在 40 张样本里 100% 为空（G-29，阻塞）」。
**事实没记错，归因错了。** B 列的单元格**文本**确实是空的，但附件一直在
交付物里——它们是内嵌在 xlsx 里的 PNG（219 张，覆盖全部 40 张单），
而 xlsx 轻解析只读 XML 文本，看不见 xl/media/ 下的图片。
**「我们的解析器读不到」和「客户没给」是两件事。**

1. **源目录只读。** 材料/ 是客户交付物，是证据不是工作区。
   抽取前后把源文件的大小、修改时间、内容摘要完整比一遍。
   这条不靠自觉靠测试——「不小心往客户资料目录写了个临时文件」
   在 code review 里基本看不出来。

2. **幂等。** 重跑不许把 219 张变成 438 张。

3. **不判类型。** attach_type 必须留空。它要的是读图，而这一步没有读图。
   **留空 = 还没判，不等于没有类型**——TR-D6-01 看到空值必须报「判不出」，
   不许当「缺件」去拦单。

4. **对不上的如实报。** 单据号库里没有、图锚在没单号的行上、浮动锚点——
   每一种都单独计数。静默跳过 = 事后没人说得清「抽了 219 张库里只有 200 条」差在哪。

口径：**申报张数（attach_pages）与实际附件数（attach_count）是两个指标**，
谁也不许覆盖谁。实测 34/40 两者不等，而那不等于「申报不实」。

测试数据一律建在 pytest 的 tmp_path 与 FJHY-TEST- 前缀的单据下，跑完删干净。
"""
import hashlib
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db  # noqa: E402
from app.perception import attachment_extract as ax  # noqa: E402

PREFIX = "FJHY-TEST-ATT-"
MATERIAL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "材料", "单据明细表与审批意见")
REAL_XLSX = os.path.join(MATERIAL, "差旅报账单明细表.xlsx")


def _fingerprint(path):
    st = os.stat(path)
    with open(path, "rb") as f:
        return st.st_size, int(st.st_mtime), hashlib.sha1(f.read()).hexdigest()


def _png(color):
    """造一张 1x1 PNG。内容不同 → sha256 不同，用来验去重。"""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_xlsx(path, rows, images, sheet_label="1报销附件"):
    """造一份带内嵌图片的 xlsx。

    rows:   [单据号, ...] 写进 A 列（A1 是表头）
    images: [(第几行(1基), 列号(0基), 字节), ...]
    """
    import io
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from PIL import Image as PILImage

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_label
    ws["A1"] = "单据编号"
    ws["B1"] = "附件"
    for i, b in enumerate(rows, start=2):
        ws.cell(row=i, column=1, value=b)
    for r, c, data in images:
        img = XLImage(PILImage.open(io.BytesIO(data)))
        # openpyxl 的锚点用 A1 式地址；列 0 基 → B 是 1
        ws.add_image(img, "%s%d" % (chr(ord("A") + c), r))
    wb.save(path)


@pytest.fixture
def bill():
    """一张真实（非演示）测试单。跑完连带附件一起删。"""
    no = PREFIX + "0001"
    # doc_type / biz_type 是 NOT NULL 且带枚举外键，必须给真值
    db.execute("""INSERT INTO expense_bill(bill_no, doc_type, biz_type, is_demo,
                                           attach_pages)
                  VALUES (%s, '差旅费报账单', '国内差旅费', FALSE, 7)
                  ON CONFLICT (bill_no) DO NOTHING""", (no,))
    yield no
    db.execute("DELETE FROM attachment WHERE bill_no=%s", (no,))
    db.execute("DELETE FROM expense_bill WHERE bill_no=%s", (no,))
    from app import doclib
    inst = doclib.resolve_instance()
    shutil.rmtree(ax.attach_dir(inst, no), ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# 纪律一 · 源目录只读
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(not os.path.isfile(REAL_XLSX), reason="客户样本不在")
def test_抽取不写入源文件():
    """材料/ 是客户交付物。抽取只开 zip 读，一个字节都不往那边写。"""
    before = {f: _fingerprint(os.path.join(MATERIAL, f))
              for f in os.listdir(MATERIAL)}
    ax.extract_xlsx(REAL_XLSX)                      # 已抽过，这次应全是「已存在」
    after = {f: _fingerprint(os.path.join(MATERIAL, f))
             for f in os.listdir(MATERIAL)}
    assert before == after, "抽取动了客户资料目录：%s" % (
        set(before.items()) ^ set(after.items()))


@pytest.mark.skipif(not os.path.isfile(REAL_XLSX), reason="客户样本不在")
def test_空跑一行都不落():
    before = db.scalar("SELECT count(*) FROM attachment") or 0
    r = ax.extract_xlsx(REAL_XLSX, dry_run=True)
    assert r["dry_run"] is True
    assert (db.scalar("SELECT count(*) FROM attachment") or 0) == before


# ─────────────────────────────────────────────────────────────────────────────
# 纪律二 · 幂等
# ─────────────────────────────────────────────────────────────────────────────
def test_重跑不重复入库(bill, tmp_path):
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red")), (2, 2, _png("blue"))])
    a = ax.extract_xlsx(p)
    assert a["saved"] == 2, a
    b = ax.extract_xlsx(p)
    assert b["saved"] == 0 and b["already"] == 2, b
    assert db.scalar("SELECT count(*) FROM attachment WHERE bill_no=%s", (bill,)) == 2


def test_同一张单里同内容的两张图只留一条(bill, tmp_path):
    """靠 (bill_no, sha256) 唯一索引。判的是内容不是文件名。"""
    same = _png("green")
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, same), (2, 2, same)])
    r = ax.extract_xlsx(p)
    assert r["saved"] == 1 and r["already"] == 1, r


# ─────────────────────────────────────────────────────────────────────────────
# 纪律三 · 不判类型
# ─────────────────────────────────────────────────────────────────────────────
def test_抽图不判类型且留空不等于没有类型(bill, tmp_path):
    """从一张 PNG 判它是出差审批单还是住宿费发票必须读图，而这一步没有读图。
    **留空表示「还没判」**——TR-D6-01 看到空值必须报「判不出」，不许当「缺件」拦单。"""
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red"))])
    r = ax.extract_xlsx(p)
    assert r["saved"] == 1
    types = [x["attach_type"] for x in db.query(
        "SELECT attach_type FROM attachment WHERE bill_no=%s", (bill,))]
    assert types == [None], "抽图这一步不该判类型，却填了 %r" % types
    assert "TR-D6-01" in ax.status()["gate_note"], \
        "必须明说 TR-D6-01 还开不了，否则「有附件了」会被当成「附件规则能跑了」"


def test_gate_note必须随已判类型数动态变化(bill, tmp_path):
    """**实测踩过的真缺陷**：gate_note 第一版是写死的固定文案
    「附件类型全部为空」，等 attachment_classify.py 真的判出几条之后，
    这句话就成了假话——而假话比没有这句话更危险，它会让人以为
    「一条都没判过」从而跳过去核实已经判出来的那些。

    **不假设库里 typed=0 这个基线。** 这张表是全库共享状态，可能已经有
    真实附件被判过类型（本项目 2026-10-21 验证读图功能时就手工判过 2 条，
    是真实、正确的分类结果，没有理由为了测试把它们撤掉）——
    所以这里只断言「新判一条之后文案里的数字跟着变」，不断言绝对值。
    """
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red"))])
    ax.extract_xlsx(p)
    note0 = ax.status()["gate_note"]
    assert "TR-D6-01" in note0

    fid = db.scalar("SELECT id FROM attachment WHERE bill_no=%s", (bill,))
    db.execute("""UPDATE attachment SET attach_type='住宿费发票', type_code='A04',
                         type_source='读图判定', type_conf='高' WHERE id=%s""", (fid,))
    note1 = ax.status()["gate_note"]
    assert note1 != note0, "判了一条之后 gate_note 纹丝不动，是写死的文案"
    assert "TR-D6-01" in note1


# ─────────────────────────────────────────────────────────────────────────────
# 纪律四 · 对不上的如实报
# ─────────────────────────────────────────────────────────────────────────────
def test_库里没有的单据号不入库也不建单(tmp_path):
    """凭空造一张单比少一张附件危险得多。"""
    ghost = PREFIX + "GHOST"
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [ghost], [(2, 1, _png("red"))])
    r = ax.extract_xlsx(p)
    assert r["saved"] == 0
    assert [u["bill_no"] for u in r["unknown_bills"]] == [ghost]
    assert any("找不到" in w for w in r["warnings"]), r["warnings"]
    assert not db.scalar("SELECT count(*) FROM expense_bill WHERE bill_no=%s", (ghost,)), \
        "**给不存在的单据建了行** —— 凭空造单比少一张附件危险得多"


def test_锚在没有单据号的行上的图要报出来(bill, tmp_path):
    p = str(tmp_path / "t.xlsx")
    # 第 2 行有单号，第 5 行没有（A5 空着）却挂了图
    _make_xlsx(p, [bill], [(2, 1, _png("red")), (5, 1, _png("blue"))])
    r = ax.extract_xlsx(p)
    assert r["saved"] == 1
    assert len(r["orphan_rows"]) == 1, r
    assert any("不知道属于哪张单" in w for w in r["warnings"]), r["warnings"]


def test_共享字符串和内联字符串两种编码都要认(bill, tmp_path):
    """xlsx 存字符串有好几种方式，**不同工具导出的结果不一样**：
    客户样本（WPS 导出）用的是共享字符串表（t="s"），
    而 openpyxl 之类的库默认写内联字符串（t="inlineStr"，整份文件可以没有
    sharedStrings.xml）。

    **只认一种的后果实测过**：第一版只认 t="s"，拿 openpyxl 造的 xlsx 来抽，
    单据号一个都读不到 → 40 张单变成 0 张 → 表现和「客户没给附件」一模一样。
    而 G-29 那条归因错误（「附件 100% 为空」）就是这么来的。
    """
    import zipfile
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red"))])
    with zipfile.ZipFile(p) as z:
        assert "xl/sharedStrings.xml" not in z.namelist(), \
            "这份造出来的 xlsx 用的不是内联字符串，这条用例就白测了"
    r = ax.extract_xlsx(p)
    assert r["bills"] == 1 and r["saved"] == 1, \
        "内联字符串的单据号没读到：%s" % r


def test_没有报销附件表时直接报错而不是抽出0张(tmp_path):
    """抽出 0 张和「这份文件里根本没有附件表」是两件事。
    混了的话，换一次导出模板就会静默变成「客户没给附件」。"""
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, ["X"], [], sheet_label="费用明细")
    with pytest.raises(ax.ExtractError) as e:
        ax.extract_xlsx(p)
    assert "报销附件" in str(e.value)


def test_按表名认附件表不按第几张表(tmp_path, bill):
    """实测两份样本里表名分别是「1报销附件」「2多人差旅-报销附件」，
    位置都在第三张；写死序号会在客户换导出模板时静默抽到别的表。"""
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red"))], sheet_label="2多人差旅-报销附件")
    r = ax.extract_xlsx(p)
    assert r["sheet"] == "2多人差旅-报销附件" and r["saved"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 口径 · 两个指标并存
# ─────────────────────────────────────────────────────────────────────────────
def test_申报张数与实际附件数并存且互不覆盖(bill, tmp_path):
    """attach_pages 来自审批意见 PDF 的申报值；attach_count 是实际抽出来的。
    实测 34/40 两者不等，但那**不等于「申报不实」**，所以两个数都要留着。"""
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red")), (2, 2, _png("blue"))])
    ax.extract_xlsx(p)
    row = db.query_one("SELECT attach_pages, attach_count FROM expense_bill "
                       "WHERE bill_no=%s", (bill,))
    assert row["attach_pages"] == 7, "申报张数被实际数覆盖了"
    assert row["attach_count"] == 2, "实际附件数没回写"


def test_演示合成的附件不和抽出来的混在一起():
    """演示数据必须一眼可辨（全局纪律第 12 条）。
    实测本表在抽取之前的 54 行全部是演示单的合成数据。"""
    kinds = {r["source_kind"]: r["n"] for r in db.query(
        "SELECT source_kind, count(*) n FROM attachment GROUP BY 1")}
    assert "演示合成" in kinds and "xlsx内嵌" in kinds, kinds
    mixed = db.scalar("""SELECT count(*) FROM attachment a
                           JOIN expense_bill b ON b.bill_no=a.bill_no
                          WHERE b.is_demo AND a.source_kind='xlsx内嵌'""")
    assert mixed == 0, "演示单上挂了 %d 条「xlsx内嵌」的附件" % mixed


# ─────────────────────────────────────────────────────────────────────────────
# 安全 · 落盘位置
# ─────────────────────────────────────────────────────────────────────────────
def test_附件落在附件区不混进制度文件库(bill, tmp_path):
    """制度文件库是**规则的上游**，单据附件是**业务数据**。
    混在一个目录下之后，「规则是从哪堆资料里抽的」就再也说不清。"""
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red"))])
    ax.extract_xlsx(p)
    path = db.scalar("SELECT stored_path FROM attachment WHERE bill_no=%s", (bill,))
    full = os.path.abspath(path)
    assert full.startswith(os.path.abspath(config.ATTACH_DIR)), full
    assert not full.startswith(os.path.abspath(config.DOCLIB_DIR)), \
        "单据附件落进了制度文件库的托管区"


def test_取图的路径闸跨盘符时要拒绝而不是抛异常():
    """实测（2026-10-21）：第一版用 os.path.commonpath 做这道闸，
    Windows 上两个路径在不同盘符时它抛 `ValueError: Paths don't have the same drive`，
    于是闸是以 **500 崩掉**的方式「拦住」的——文件确实没发出去，
    但接口回的是「服务器故障」而不是「这个路径不许读」。
    **一个会抛异常的安全检查，下一个人很容易用 try/except 包起来然后 pass 掉。**

    两个方向都测：越界的拒掉，附件区内的必须放行——
    只测拒绝方向的话，把 happy path 写坏了也发现不了（这条实测踩过）。
    """
    from app.api.review import _under
    base = config.ATTACH_DIR
    assert _under(os.path.join(base, "audit", "fjhy", "X", "1.png"), base) is True
    assert _under(base, base) is True
    # 大小写不敏感（Windows 上这是同一个目录，判成越界就会误伤合法路径）
    assert _under(base.upper(), base) is True
    assert _under(r"C:\Windows\win.ini", base) is False      # 跨盘符，不许抛
    assert _under(os.path.join(config.DOCLIB_DIR, "x.png"), base) is False
    assert _under(base.rstrip("\\/") + "_evil", base) is False, \
        "前缀相同但不是子目录（attachments_evil），必须判越界"


def test_出处精确到行与media而不只是文件名(bill, tmp_path):
    """「只写文件名不够」——同一个单据号在两份 xlsx 里都可能出现，
    而「哪一行、哪张图」是事后回查原件的唯一入口。"""
    p = str(tmp_path / "t.xlsx")
    _make_xlsx(p, [bill], [(2, 1, _png("red"))])
    ax.extract_xlsx(p)
    ref = db.scalar("SELECT source_ref FROM attachment WHERE bill_no=%s", (bill,))
    assert "t.xlsx" in ref and "第2行" in ref and "media" in ref, ref
