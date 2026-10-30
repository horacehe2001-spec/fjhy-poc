# -*- coding: utf-8 -*-
"""读图作为**独立取文路径**（迁移 0042）。不出网。

这组钉的是三类静默失败——它们都不报错，只给出一个对不上的数字：

1. **两处取文闸分叉。** doclib.pending_extract 判「这份有没有可用正文」，
   doclib_extract.extract 也判一次。两处写法不一致的表现是
   「待抽取列表里有它，点了抽取却不抽它」，或者反过来，
   而界面上只是一个少了几份的数字。

2. **混合型 PDF 选错文本来源。** 封面有薄薄一层文字层、正文全是扫描图的 PDF，
   按「原生文本优先」会拿着那 3 个字去切条款，切出 0 条，
   看起来像「这份制度里没有条款」。实测 822港航 有两份是这样
   （10 页里 9 页无文字层 = 3 字；20 页里 19 页无文字层 = 262 字）。

3. **「已读图」但正文是空的。** 一页都没读成却标成已读图，抽取闸会放它进来、
   抽出 0 条——和第 2 类同一种假象。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, doclib, ocr, scope, vision  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def _src(name):
    return io.open(os.path.join(APP_DIR, name), encoding="utf-8").read()


# ─────────────────────────────────────────────────────────────────────────────
# 一、两处取文闸必须一致
# ─────────────────────────────────────────────────────────────────────────────
def test_待抽取与抽取的取文闸是同三条路径():
    """两处都必须同时认：原生文本、OCR、读图。

    分叉的后果不是报错，是一个对不上的数字——而那个数字出现在
    「这份制度抽过没有」这件事上。
    """
    gate = doclib.HAS_TEXT_SQL
    for frag in ("parse_state='已解析'", "ocr_state='已OCR'", "vision_state='已读图'",
                 "ocr_fingerprint", "vision_fingerprint"):
        assert frag in gate, "pending_extract 的取文闸少了 %s" % frag

    ex = _src("doclib_extract.py")
    seg = ex[ex.index("def run_split("):]
    seg = seg[:seg.index("files = db.query")]
    for frag in ("parse_state='已解析'", "ocr_state='已OCR'", "vision_state='已读图'"):
        assert frag in seg, "doclib_extract 的取文闸少了 %s" % frag


def test_读图支必须比指纹():
    """读过之后文件又被换过，读图文本就对应不上磁盘上的文件了。

    **和 ocr_fingerprint 完全一样的口径。** 不比指纹的表现是：
    抽出来的东西看着有据可查，实际对的是一份已经不存在的文件。
    """
    assert "vision_fingerprint IS NOT DISTINCT FROM fingerprint" in doclib.HAS_TEXT_SQL
    ex = _src("doclib_extract.py")
    assert "vision_fingerprint IS NOT DISTINCT FROM fingerprint" in ex


# ─────────────────────────────────────────────────────────────────────────────
# 二、一页正文的整理
# ─────────────────────────────────────────────────────────────────────────────
def test_剥掉自报行不让它进正文():
    """「看不清格子数: 3」是留痕用的元数据。

    不剥的话，一份 26 页的文件会在正文里多出 26 行这个，
    而条款切分的正则会把它们当内容。
    """
    out = vision._page_text("第一条 出差应当事先审批。\n看不清格子数: 3")
    assert "看不清格子数" not in out
    assert "第一条 出差应当事先审批。" in out


def test_空页回答不当正文():
    """模型照提示词回「本页无文字」。那是**回答**不是正文。

    原样拼进去，26 页的扫描件会得到 26 条假条款。
    """
    assert vision._page_text("本页无文字") == ""
    assert vision._page_text("  本页无文字  ") == ""
    assert vision._page_text("") == ""
    assert vision._page_text(None) == ""


def test_读图文本不做OCR那套去空格():
    """**normalize() 只能用在 tesseract 文本上。**

    那个函数去掉汉字之间的空格，治的是 tesseract「逐字输出、字间插空格」
    的毛病。VL 回的是正常中文，对它做同样处理会把本该有空格的地方粘起来。
    ocr.py 里那句「只对 OCR 文本做，原生文本不碰」管的就是这件事。
    """
    # 只看函数**体**，不看文档字符串——那里正是解释「为什么不调 normalize」
    # 的地方，连注释一起扫会把解释本身当成违规。
    import ast
    tree = ast.parse(_src("vision.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_page_text")
    body = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                       and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.dump(n) for n in body)
    assert "normalize" not in code, "_page_text 调了 ocr.normalize —— 会粘字"
    # 正向验一次：带空格的正常中文原样保留
    assert vision._page_text("甲 乙 丙 丁") == "甲 乙 丙 丁"


# ─────────────────────────────────────────────────────────────────────────────
# 三、两条路径的 spans 形状必须一致
# ─────────────────────────────────────────────────────────────────────────────
def test_两条取文路径的页边界键一致():
    """抽取那边 `ocr.page_at(spans, pos)` 对两条路径共用。

    键对不上的表现是抽取时 KeyError，或者更糟——
    `sp["has_table"]` 取不到于是条款没被标成表格来源，
    而表格来源正是最该被怀疑的那一类。
    """
    v = _src("vision.py")
    seg = v[v.index("def text_with_pages"):v.index("def text_of")]
    for k in ('"start"', '"end"', '"page_no"', '"has_table"', '"mean_conf"'):
        assert k in seg, "vision.text_with_pages 的 span 少了 %s" % k

    o = _src("ocr.py")
    oseg = o[o.index("def text_with_pages"):o.index("def page_at")]
    for k in ('"start"', '"end"', '"page_no"', '"has_table"', '"mean_conf"'):
        assert k in oseg


def test_page_at对读图的spans也能用():
    """纯函数，直接验。"""
    _, spans = [], [{"start": 0, "end": 10, "page_no": 1, "has_table": True,
                     "mean_conf": None, "unreadable": 2},
                    {"start": 11, "end": 20, "page_no": 2, "has_table": False,
                     "mean_conf": None, "unreadable": 0}]
    assert ocr.page_at(spans, 5)["page_no"] == 1
    assert ocr.page_at(spans, 15)["page_no"] == 2
    assert ocr.page_at(spans, 999) is None, "找不到要返回 None，**不许猜**"


# ─────────────────────────────────────────────────────────────────────────────
# 四、混合型 PDF 选哪一份正文
# ─────────────────────────────────────────────────────────────────────────────
def test_取文按字数选而不是原生优先():
    """**修过的 bug。** 原来是「原生文本优先，没有才用 OCR」。

    822港航 实测有两份被判「已解析」的 PDF，其实 10 页里 9 页无文字层
    （全份 3 个字）、20 页里 19 页无文字层（262 字）。按原生优先，
    它们会拿着那 3 个字去切条款，切出 0 条——而这**没有任何报错**。
    """
    src = _src("doclib_extract.py")
    seg = src[src.index("# ── 取文："):src.index("strategy, clauses = pick_strategy")]
    assert 'len(native) < (f["vision_chars"] or 0)' in seg, \
        "取文没有按字数比，退回成了「原生优先」——混合型 PDF 会再次切出 0 条"
    # 原生优先必须排在比字数**之后**
    assert seg.index('len(native) <') < seg.index('elif native:'), \
        "先走了 native 分支，字数比就永远轮不到"


def test_待读文件把薄文本那一档也捞出来():
    """只按 parse_state 挑，混合型 PDF 会永远卡在中间：
    既不会被 OCR 捡起来（它不是「需OCR」），又进了抽取却抽不出东西。"""
    src = _src("vision.py")
    seg = src[src.index("def files_to_read"):]
    seg = seg[:seg.index("return rows")]
    assert "text_chars < %s" in seg, "files_to_read 没有薄文本那一档"


# ─────────────────────────────────────────────────────────────────────────────
# 五、一页都没读成不许标「已读图」
# ─────────────────────────────────────────────────────────────────────────────
def test_零页读成算失败():
    """留一个「已读图」但正文是空的状态是最坏的：
    抽取闸会放它进来，然后抽出 0 条，看起来像这份制度里没有条款。"""
    src = _src("vision.py")
    seg = src[src.index("def read_file"):]
    assert "ok = tot_pages > 0" in seg, "没有「一页都没读成就算失败」这道判断"


def test_汇总从库里重算而不是用本次计数():
    """**补读失败页时这条是对的与错的分界线。**

    本次只跑了 3 页，拿本次计数去写 vision_pages 会把「这份一共读成多少页」
    改小——一次补读反而让文件看起来更差；而 vision_chars 变小还会让抽取的
    取文判断（比字数）倒向那份残缺的原生文本。
    这一列永远描述「库里现在有多少」，不是「这一次跑了多少」。
    """
    src = _src("vision.py")
    seg = src[src.index("def read_file"):]
    assert "have = page_contents(file_id)" in seg, "汇总没有回库重算"
    assert "tot_pages = len(have)" in seg
    assert "mark_read(file_id, tot_pages, tot_chars, tot_unread" in seg, \
        "mark_read 收到的还是本次循环的计数"


def test_已读图不等于读全了():
    """timeout 丢页的那一轮：175 页里 31 页超时，一份 20 页的只读到 6 页。

    那种文件状态是「已读图」、抽取闸也放行，但正文缺了七成，
    而界面上只有一个「已读过」。缺页必须自己说出来。
    """
    src = _src("vision.py")
    seg = src[src.index("def files_to_read"):]
    seg = seg[:seg.index("return rows")]
    assert "pages_without_read" in seg, "files_to_read 没检查缺页"
    assert "missing_pages" in seg
    assert "done = fresh and not miss" in seg, \
        "「已读图」直接当成读完了——缺页的文件会被漏掉"


def test_补读只补没成功过的页():
    """整份重读要花两份钱，而失败多半是超时这种和页面内容无关的原因。

    **和 only_missing_text 不同，补读不会造成正文缺口**：
    它只补一行成功记录都没有的页，已经读好的仍在 doc_page_vision 里。
    """
    src = _src("vision.py")
    seg = src[src.index("def pages_without_read"):src.index("def read_file")]
    assert "bool_or(ok)" in seg, "没有按「这一页有没有任何一次成功」来判"
    assert "if not r[\"any_ok\"]" in seg


def test_落库的状态只有两种写法():
    src = _src("vision.py")
    seg = src[src.index("def mark_read"):src.index("def read_file")]
    assert '"已读图" if ok else "读图失败"' in seg


# ─────────────────────────────────────────────────────────────────────────────
# 六、全量口径：三个数要加得起来
# ─────────────────────────────────────────────────────────────────────────────
def test_在库等于待抽取加已抽取加够不着():
    """**这一页原来只报「待抽取 N 份」。**

    在库 12 份而它显示 5 时，看的人没有任何办法知道另外 7 份去哪了，
    也分不清「抽完了」和「有 7 份系统根本够不着」。差额必须当场交代。
    """
    iid = scope.require()
    p = doclib.pending_extract(instance_id=iid)
    assert p["in_lib"] == p["total"] + p["extracted"] + p["blocked_total"], (
        "在库 %s ≠ 待抽取 %s + 已抽取 %s + 够不着 %s"
        % (p["in_lib"], p["total"], p["extracted"], p["blocked_total"]))
    assert str(p["in_lib"]) in p["coverage_note"]


def test_待抽取每一行都说清正文从哪来():
    """三条路径哪一条喂的这份文件，列表上要直说——
    「读图」和「原生文本」在可信度上是两回事，哪怕用户决定同等对待，
    也不等于可以不告诉人。"""
    iid = scope.require()
    for r in doclib.pending_extract(instance_id=iid)["items"]:
        assert r["text_source"] in ("原生文本", "OCR", "读图"), \
            "%s 的 text_source 是 %r" % (r["file_name"], r["text_source"])


def test_抽取范围接口也报全量口径():
    src = _src("doclib_extract.py")
    seg = src[src.index('"blocked_total": sum'):]
    seg = seg[:seg.index('"ai_batch_limit"')]
    for k in ('"in_lib"', '"extracted"', '"coverage_note"'):
        assert k in seg, "incremental_scope 没把 %s 带出去" % k
