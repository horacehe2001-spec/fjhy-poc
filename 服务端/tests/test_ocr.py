# -*- coding: utf-8 -*-
"""本机 OCR：表格检测的两个方向、页映射、引擎自检。

这组测试盯的是一条实测教训：**OCR 丢的不是字，是结构。**
住宿费标准表 OCR 后数字全在（460/420/430/310/480/440），
但城市↔职级↔金额的对应关系全乱了。所以「这一页是不是表格」这个判断
本身就是一道闸：标出来，数字就只当线索；漏标，错数字就以
「有制度依据」的面目流进规则清单。

而这道闸第一版是坏的——用 fitz.find_tables()（矢量线条），
在扫描件上**全程返回 0**，恰好在最需要它的那批文件上完全失效。
现在改成像素检测，所以这里必须两个方向都测：
  该标的要标（横线多的表格页，含没有竖线的三线表）
  不该标的一条都不许误伤（正文页、只有红头线/落款线的页）

阈值不是拍的，是实测分界：同一份 27 页文件里
  非表格页横线 0 / 3 / 5 条，表格页 19 / 21 / 25 / 29 条。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ocr  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402


def blank(w=800, h=1100):
    return Image.new("L", (w, h), 255)


def draw_text_lines(img, n=30):
    """模拟正文页。

    **必须逐个字画、留字间空隙**，不能画成一条实心长条：
    真实文字行有字间留白，整行的黑像素比例在 30% 上下；
    画成实心长条就有 77%，那在任何「数贯通线」的算法眼里都是一条线——
    第一版夹具就是这么写的，于是测出「正文页被误判成表格」，
    而真实的 27 页数据里正文页最多只有 5 条贯通线。
    **夹具不真实，测出来的红是假红**，照着它去调阈值会把真正的闸调坏。
    """
    d = ImageDraw.Draw(img)
    for i in range(n):
        y = 60 + i * 32
        for k, x in enumerate(range(80, 700, 26)):      # 逐字，字间留 8px 空
            # 画**笔画**，而且**每个字的笔画高度要错开**。
            # 汉字是细笔画（一行文字黑像素约 20~30%，不是实心块的 70%），
            # 更要紧的是：一行里各个字的横笔画**不在同一高度**，
            # 所以没有哪一行像素能横贯整行。画成等高的横杠，
            # 每行文字就贡献 2 条「贯通线」，30 行就是 60 条——假红。
            off = k % 7
            d.rectangle([x, y + off, x + 18, y + off + 1], fill=60)
            d.rectangle([x + 8, y, x + 9, y + 14], fill=60)
    return img


def draw_table(img, rows=12, cols=0):
    """模拟表格页：rows 条贯通整行的横线；cols>0 时再加**贯通整页高**的竖线。

    竖线要画满页高是有讲究的：检测器判一条竖线的标准是「这一列有 55% 以上是黑的」，
    而表格的竖线只在表格范围内。小表格的竖线因此数不到——
    这是检测器的已知局限，所以主判据是横线数（min_h=8），
    「3 横 + 2 竖」只是给满页大表格留的备用通道。
    """
    d = ImageDraw.Draw(img)
    w, h = img.size
    top, bottom = 60, h - 60
    pitch = max(8, (bottom - top) // max(1, rows))
    for i in range(rows):
        y = top + i * pitch
        d.rectangle([0, y, w - 1, y + 3], fill=0)       # 贯通横线
    for j in range(cols):
        x = 40 + j * 120
        d.rectangle([x, top, x + 3, bottom], fill=0)    # 贯通竖线
    return img


# ─────────────────────────────────────────────────────────────────────────────
# 该标的要标
# ─────────────────────────────────────────────────────────────────────────────
def test_有横有竖的表格判为表格():
    ok, hor, ver = ocr.detect_table_image(draw_table(blank(), rows=12, cols=5))
    assert ok, "横 %d 竖 %d 没判成表格" % (hor, ver)


def test_三线表没有竖线也要判为表格():
    """中文公文里大量是只有横线的表。第一版要求「必须有竖线」，
    实测漏掉《差旅管理实施细则》第 15 页（住宿费标准续页）和第 17 页（审批单），
    两页都是 0 竖线——而第 15 页恰恰是费率表，漏标代价最大。"""
    ok, hor, ver = ocr.detect_table_image(draw_table(blank(), rows=12, cols=0))
    assert ver == 0, "这个用例本来就该是 0 竖线"
    assert ok, "只有横线的三线表没判出来（横 %d）" % hor


def test_少量竖线的小表格也算():
    ok, hor, ver = ocr.detect_table_image(draw_table(blank(), rows=4, cols=3))
    assert ok, "横 %d 竖 %d" % (hor, ver)


# ─────────────────────────────────────────────────────────────────────────────
# 不该标的一条都不许误伤
# ─────────────────────────────────────────────────────────────────────────────
def test_正文页不许判成表格():
    """误报的代价：25 页正常条款陪着那 2 页表格一起被打上「不可信」，
    人看两次发现全是好的，第三次就不看了——那道闸就等于没有。"""
    ok, hor, ver = ocr.detect_table_image(draw_text_lines(blank()))
    assert not ok, "正文页被误判成表格（横 %d 竖 %d）" % (hor, ver)


def test_只有红头线落款线的页不许判成表格():
    """实测：非表格页最多 5 条贯通横线（红头线、页眉线、落款线）。"""
    img = draw_text_lines(blank())
    d = ImageDraw.Draw(img)
    for y in (40, 120, 1020, 1060, 1080):
        d.rectangle([0, y, img.size[0] - 1, y + 3], fill=0)
    ok, hor, ver = ocr.detect_table_image(img)
    assert not ok, "5 条横线就被判成表格了（横 %d），阈值太松" % hor


def test_空白页不判表格():
    ok, hor, ver = ocr.detect_table_image(blank())
    assert not ok and hor == 0 and ver == 0


def test_图太小直接返回不判():
    assert ocr.detect_table_image(Image.new("L", (20, 20), 255)) == (False, 0, 0)


# ─────────────────────────────────────────────────────────────────────────────
# 页映射：每一条条款要能回到它所在的页
# ─────────────────────────────────────────────────────────────────────────────
def test_字符位置能映射回页():
    spans = [{"start": 0, "end": 100, "page_no": 1, "has_table": False, "mean_conf": 90.0},
             {"start": 101, "end": 250, "page_no": 2, "has_table": True, "mean_conf": 62.0}]
    assert ocr.page_at(spans, 0)["page_no"] == 1
    assert ocr.page_at(spans, 99)["page_no"] == 1
    assert ocr.page_at(spans, 150)["page_no"] == 2
    assert ocr.page_at(spans, 150)["has_table"] is True


def test_映射不到就返回None不猜():
    """猜一个页号出来，比承认不知道坏得多：
    下游会拿这个页号去回查原文，查到的是另一页。"""
    spans = [{"start": 0, "end": 10, "page_no": 1, "has_table": False, "mean_conf": 90.0}]
    assert ocr.page_at(spans, 999) is None
    assert ocr.page_at([], 0) is None


# ─────────────────────────────────────────────────────────────────────────────
# 引擎自检
# ─────────────────────────────────────────────────────────────────────────────
def test_引擎信息要说清用的哪份语言包():
    """同一份扫描件用 fast 包和 best 包结果不一样。
    事后回看「这段文字当初怎么来的」，没有这个信息就说不清。"""
    info = ocr.engine_info()
    assert "variant" in info and "tessdata" in info and "version" in info
    if not info["ok"]:
        pytest.skip("本机没装 tesseract 或缺 chi_sim：%s" % info.get("reason"))
    assert "chi_sim" in info["langs"]
    assert info["lang_arg"] == "chi_sim+eng"


def test_渲图落盘不在被扫描目录里():
    """只读纪律不止管「不改文件」，也管「不往人家目录里放东西」。
    客户的资料目录是证据，多出一个 .png 就说不清这份资料原本有什么。"""
    d = ocr.out_dir()
    assert os.path.normcase("var" + os.sep + "ocr") in os.path.normcase(d), d
    assert "材料" not in d


# ─────────────────────────────────────────────────────────────────────────────
# OCR 文本规范化：tesseract 对中文逐字插空格
# ─────────────────────────────────────────────────────────────────────────────
def test_去掉汉字之间的空格():
    """实测 tesseract 输出：「第 十 一 条 ”住宿 费 是 指 工作 人 员」。
    不规范化的话，下游所有按中文词组写的正则全部落空——
    27 页《差旅管理实施细则》OCR 完 19438 字，「第X条」匹配 **0 处**。
    看起来像「这份制度里没有条款」，真相是分隔符不对。
    这种失败最隐蔽：没有报错，只有一个 0。"""
    import re
    raw = "第 十 一 条 ”住宿 费 是 指 工作 人 员 因 公 出 差 期 间 入 住 宾馆"
    out = ocr.normalize(raw)
    assert "第十一条" in out, out
    assert "住宿费" in out and "工作人员" in out, out
    assert re.search(r"第[一二三四五六七八九十百零〇\d]{1,8}条", out)


def test_规范化要跑到稳定():
    """「字 字 字」里相邻两对共享同一个空格，一遍替换只去掉一半。"""
    assert ocr.normalize("应 当 按 照 规 定 执 行") == "应当按照规定执行"


def test_不碰非汉字之间的空格():
    """只去汉字之间的空格。英文和数字之间的空格是有意义的，
    抹掉会把两个词粘成一个——那是另一种破坏。"""
    assert ocr.normalize("差旅费 1374.80 元 for the trip") == "差旅费 1374.80 元 for the trip"
    assert ocr.normalize("") == ""
    assert ocr.normalize(None) is None
