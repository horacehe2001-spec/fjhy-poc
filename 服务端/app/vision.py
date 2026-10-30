# -*- coding: utf-8 -*-
"""多模态读图：OCR 丢结构的那几页，用读图模型再读一遍，并让两个模型互相印证。

app/ocr.py 的第 3 条纪律写了上半句：
  实测住宿费标准表 OCR 后数字全在，但城市↔职级↔金额的对应关系全乱了。
  **OCR 丢的不是字，是结构。** 而差旅规则的全部价值就在那个对应关系上。
  交通工具等级表更彻底：OCR 出来是「(1) 分 公 | Ti [Sree 责 人 凭据 报销 等 座」，
  TR-D3 那批规则因此一直编不出来。
  出路两条：**要么人眼读图，要么上多模态模型。**
这个模块是后者。

四条口径。**其中第 2、3 条已被用户在 2026-10-30 明确推翻**，
原文保留在下面并逐条标注，因为「当初为什么要这么定」比「现在怎么做」更难重建：

1. **提示词必须给模型一条「可以说不知道」的出路。**（仍然有效，而且更重要了）
   一个把所有格子都填满的回答，要么是真读清楚了，要么是把看不清的编出来了——
   而这两者在文本上长得一模一样，看回答本身分辨不出来。
   所以提示词里明确要求：看不清写 `?`，不许猜、不许补全、不许合并单元格。
   然后把 `?` 的个数数出来存进 unreadable —— **这个数比内容本身更重要**。
   第 2、3 条取消之后，**它是这条链路上唯一还在报警的东西**。

2. ~~两个模型各读一遍，只有两边一致的格子才算读出来。~~
   **已取消（用户 2026-10-30 决策：单模型 qwen3-vl-plus）。**
   原来的理由是「一个模型读出来的东西，没有任何东西能证伪它」，
   和规则抽取那边的「多源印证」是同一条纪律。
   取消的代价说清楚：**读错的格子现在没有第二个来源能发现。**
   cross_check() / latest_by_model() / numbers_of() 没有删——它们仍然可用，
   只是不再是全量读图的必经之路。哪天要回到双模型，接回去即可。

3. ~~读出来的数字不自动可信，verified_by 为空时不许进 standard_limit
   与任何规则阈值。~~
   **已取消（用户 2026-10-30 决策：VL 文本与原生文本同等对待）。**
   原来的理由是「模型读图不等于人眼读图，它同样会把看不清的格子编出来，
   而且编得通顺」。
   现在 verified_by 退化成**可选的人工复核留痕**，不再是闸：
   read_file() 读出来的文本经 text_with_pages() 直接进抽取，
   和原生文字层走同一条路、同等对待。
   取消的代价说清楚：**没有任何自动闸拦得住「编得通顺的错数字」**，
   只剩第 1 条那个 unreadable 计数。所以界面上不许把它省掉。

4. **这是唯一把客户制度文件发到外部的动作**（OCR 是本机 tesseract）。（仍然有效）
   逐次留痕：哪一页、哪个型号、多少 token、几毫秒、回了什么。

── 两条取文路径的分工（0042 之后）────────────────────────────────────────
    本机 OCR     ocr.ocr_pdf()   → doc_lib_ocr      → ocr.text_with_pages()
    多模态读图   vision.read_file() → doc_page_vision → vision.text_with_pages()
两条**平级**，共用 ocr.iter_rendered_pages() 渲页。
在 0042 之前读图是寄生在 OCR 之上的（pages_to_read 从 doc_lib_ocr 挑页、
连页面图都是 OCR 渲的），于是出现了一个说不通的局面：
**本机 OCR 能让一份扫描件进抽取，效果更好的多模态模型反而不能。**
"""
import io
import os
import re

from . import db
from .llm import client

SYSTEM = (
    "你是一个把扫描件里的表格**原样誊抄**成 Markdown 的工具。"
    "你的唯一职责是誊抄你**看得见**的内容。"
)

PROMPT_TABLE = """请把这一页里的表格原样誊抄成 Markdown 表格。

硬性要求（违反其中任何一条，这次誊抄就是废的）：
1. **看不清的格子写 `?`，不要猜、不要补全、不要按常识填。**
   我宁可要一张有十个 `?` 的表，也不要一张看起来完整但有一个格子是编的表。
2. 行数、列数、合并单元格的范围都要和原图一致。合并的单元格在每一行里重复写它的值。
3. 数字一个字符都不要改：不要补小数点、不要加千分位、不要四舍五入。
4. 表格之外的正文（标题、条款、页码、注释）也要抄，放在表格前后，用普通文本。
5. 如果这一页**根本没有表格**，就直接说「本页无表格」，然后把正文抄下来。
6. 不要解释、不要总结、不要评价。只誊抄。

最后单独起一行，写：`看不清格子数: N`（N 是你写了多少个 `?`）。"""

PROMPT_TEXT = """请把这一页上**看得见的文字**原样抄下来。

硬性要求：
1. 看不清的字写 `?`，不要猜。
2. 保持原来的换行与段落。
3. 如果这一页是空白页或只有扫描噪点，就直接说「本页无文字」。
4. 不要解释、不要总结。只誊抄。

最后单独起一行，写：`看不清格子数: N`。"""

PROMPTS = {"table": PROMPT_TABLE, "text": PROMPT_TEXT}


def _cfg(config_id=None):
    """取一条读图配置。**不回退到文本模型**——那正是要防的事。"""
    if config_id:
        row = db.query_one("SELECT * FROM llm_config WHERE config_id=%s", (config_id,))
    else:
        # **取默认那条，不是「按 config_id 排序的第一条」。**
        # 第一版就是按 config_id 取的，结果用户在页面上新配了一条并设为默认之后，
        # 这里仍然用着旧的那条——而日志里只会看到一个型号名对不上的 404，
        # 没人想得到是「选错了配置」。有默认就用默认，这也是页面上那个
        # 「设为默认」按钮唯一的意义。
        row = db.query_one("""SELECT * FROM llm_config
                               WHERE purpose='vision_read' AND enabled AND is_default
                               LIMIT 1""")
        if not row:
            row = db.query_one("""SELECT * FROM llm_config
                                   WHERE purpose='vision_read' AND enabled
                                   ORDER BY config_id LIMIT 1""")
    if not row:
        raise ValueError("没有可用的读图配置。去「大模型配置」配一条 purpose=vision_read 的。")
    return row


def pages_to_read(limit=40):
    """该读哪些页：OCR 标了表格的，或者一个字都没认出来的。

    **不是「所有页都读一遍」**：27 页的文件全读一遍是几万 token，
    而其中 23 页的原生文字层本来就是好的——拿钱换一份更差的副本。
    """
    return db.query("""SELECT o.file_id, f.file_name, o.page_no, o.chars, o.mean_conf,
                              o.has_table, o.image_path,
                              (SELECT count(*) FROM doc_page_vision v
                                WHERE v.file_id=o.file_id AND v.page_no=o.page_no
                                  AND v.ok) AS read_times
                         FROM doc_lib_ocr o JOIN doc_lib_file f ON f.file_id=o.file_id
                        WHERE o.has_table OR o.chars = 0
                        ORDER BY f.file_name, o.page_no LIMIT %s""", (limit,))


_UNREADABLE_RE = re.compile(r"看不清格子数[:：]\s*(\d+)")


def count_unreadable(content):
    """模型自报的「看不清」个数；自报不了就数 `?`。

    两个来源都要：自报的是它的自觉，数出来的是事实。
    取较大的那个——宁可把不确定说多，不可说少。
    """
    said = _UNREADABLE_RE.search(content or "")
    said_n = int(said.group(1)) if said else None
    body = _UNREADABLE_RE.sub("", content or "")
    counted = body.count("?") + body.count("？")
    if said_n is None:
        return counted
    return max(said_n, counted)


def count_rows(content):
    """数表格的数据行（去掉表头与分隔行）。

    **不要求行首行尾都有竖线。** 实测 qwen3-vl-plus 回的表第一行长这样：
    `其他地区 | 590 | 390 | …`——没有首尾竖线，而第一版非要首尾都有，
    于是把一张 22 行的表数成了 0 行。**0 行会被当成「这页没读出来」**，
    而其实内容是对的。判据改成「这一行有两个以上竖线，且不是分隔行」。
    """
    n = 0
    for ln in (content or "").splitlines():
        t = ln.strip()
        if t.count("|") < 2:
            continue
        if re.match(r"^\|?[\s:\-|]+\|?$", t):      # |---|---| 这类分隔行
            continue
        n += 1
    return max(0, n - 1) if n else 0


def read_page(file_id, page_no, config_id=None, kind="table", max_tokens=4000,
              image_path=None):
    """读一页，落一行。**永不覆盖**：同页同模型重读会 attempt+1 再插一行。

    image_path 显式传了就用传的（read_file 那条路自己渲图，不经过 doc_lib_ocr）；
    不传才回去 doc_lib_ocr 里找——那是 0042 之前「读图寄生在 OCR 之上」的老路，
    界面上「对某一页再读一遍」仍然走它。
    """
    cfg = _cfg(config_id)
    img = image_path
    if not img:
        row = db.query_one("""SELECT image_path FROM doc_lib_ocr
                               WHERE file_id=%s AND page_no=%s""", (file_id, page_no))
        if not row or not row["image_path"]:
            raise ValueError("这一页没有渲好的 PNG（file_id=%s page=%s）。"
                             "先跑一次 OCR 或整份读图，两者都会把图渲到 var/ocr/ 下。"
                             % (file_id, page_no))
        img = row["image_path"]
    if not os.path.exists(img):
        raise ValueError("PNG 不在了：%s" % img)
    size = os.path.getsize(img)

    attempt = (db.scalar("""SELECT max(attempt) FROM doc_page_vision
                             WHERE file_id=%s AND page_no=%s AND model_name=%s""",
                         (file_id, page_no, cfg["model_name"])) or 0) + 1

    res, content, use = client.chat_vision(cfg, SYSTEM, PROMPTS[kind], img,
                                           max_tokens=max_tokens)
    db.execute("""INSERT INTO doc_page_vision
        (file_id, page_no, attempt, config_id, model_name, model_echo, prompt_kind,
         image_path, image_bytes, ok, err_kind, message, content, rows_parsed,
         unreadable, latency_ms, prompt_tokens, completion_tokens)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (file_id, page_no, attempt, cfg["config_id"], cfg["model_name"],
         res.model_echo, kind, img, size, res.ok, res.err_kind, res.message,
         content or None,
         count_rows(content) if res.ok else None,
         count_unreadable(content) if res.ok else None,
         res.latency_ms, use.get("prompt_tokens"), use.get("completion_tokens")))
    return {"file_id": file_id, "page_no": page_no, "attempt": attempt,
            "model_name": cfg["model_name"], "ok": res.ok, "err_kind": res.err_kind,
            "message": res.message, "latency_ms": res.latency_ms,
            "rows": count_rows(content) if res.ok else None,
            "unreadable": count_unreadable(content) if res.ok else None,
            "tokens": use, "content": content}


# ─────────────────────────────────────────────────────────────────────────────
# 整份读图 —— 与本机 OCR 平级的取文路径（0042）
# ─────────────────────────────────────────────────────────────────────────────
# 模型在「这一页没内容」时会照提示词回这两句。它们是**回答**不是正文，
# 原样拼进抽取文本会变成一堆假条款（「本页无文字」出现 26 次）。
_EMPTY_ANSWERS = ("本页无文字", "本页无表格且无正文", "本页为空白页")


def _page_text(content):
    """把一页读图结果整理成可以进抽取的正文。读不出东西就返回空串。

    做两件事，都只做这两件：
      · 去掉结尾那行「看不清格子数: N」——那是留痕用的元数据，不是正文。
      · 整页只有「本页无文字」这类回答时按空页处理。

    **刻意不做 ocr.normalize()。** 那个函数去掉汉字之间的空格，是专门为
    tesseract「逐字输出、字间插空格」这个毛病写的；VL 回的是正常中文，
    对它做同样的处理会把本来就该有空格的地方粘起来。
    ocr.py 里那句「只对 OCR 文本做，原生文本不碰」管的就是这件事。
    """
    t = _UNREADABLE_RE.sub("", content or "").strip()
    if not t:
        return ""
    if t in _EMPTY_ANSWERS or (len(t) <= 12 and any(e in t for e in _EMPTY_ANSWERS)):
        return ""
    return t


def page_contents(file_id):
    """每一页**最近一次成功**的读图结果。

    按 created_at 取最近，不按 attempt：attempt 是「同一页同一个型号」的
    重读次数，换了型号它会从 1 重新开始，拿它排序会让新型号的第一次
    输给旧型号的第三次。
    """
    return db.query("""SELECT DISTINCT ON (page_no)
                              page_no, content, unreadable, rows_parsed, model_name,
                              prompt_kind, created_at
                         FROM doc_page_vision
                        WHERE file_id=%s AND ok AND content IS NOT NULL
                        ORDER BY page_no, created_at DESC""", (file_id,))


def text_with_pages(file_id):
    """→ (整份文本, [{start, end, page_no, has_table, mean_conf, unreadable}])

    签名和 ocr.text_with_pages 一致，**抽取那边才能用同一套写法处理两条路径**。

    mean_conf 恒为 None：读图没有逐词置信度那种东西。对应的警报是 unreadable
    （模型自报看不清的格子数），一起带回去，让抽取能指着**某一条条款**说
    「它所在那一页模型有 7 个格子没读清」——文件级的数字做不到这件事。
    """
    parts, spans, pos = [], [], 0
    for r in page_contents(file_id):
        t = _page_text(r["content"])
        if not t:
            continue
        spans.append({"start": pos, "end": pos + len(t), "page_no": r["page_no"],
                      "has_table": r["prompt_kind"] == "table",
                      "mean_conf": None,
                      "unreadable": r["unreadable"] or 0})
        parts.append(t)
        pos += len(t) + 1          # +1 是下面 join 的那个换行
    return "\n".join(parts), spans


def text_of(file_id):
    """整份读图文本。供抽取用。"""
    return text_with_pages(file_id)[0]


def mark_read(file_id, pages, chars, unreadable, model, note=None, ok=True):
    """把这一份的读图结果钉到 doc_lib_file 上。

    指纹用**当前**的 fingerprint：文件之后再被换过，两个指纹自然不等，
    抽取那边就会把它当没读过。口径和 ocr_fingerprint 完全一致
    （见 0042 的列注释）——拿旧版文本抽现行制度的规则，抽出来的东西
    看着有据可查，实际对的是一份已经不存在的文件。
    """
    return db.execute("""UPDATE doc_lib_file
                            SET vision_state=%s, vision_at=now(), vision_pages=%s,
                                vision_chars=%s, vision_unreadable=%s,
                                vision_model=%s, vision_note=%s,
                                vision_fingerprint=fingerprint
                          WHERE file_id=%s""",
                      ("已读图" if ok else "读图失败", pages, chars, unreadable,
                       model, note, file_id))


def pages_without_read(file_id):
    """这份文件里**还没有任何一次成功读图**的页号。

    补读失败页用。和 only_missing_text 不同，这个**不会造成正文缺口**：
    它只补那些一行成功记录都没有的页，已经读好的页照样留在
    doc_page_vision 里，text_with_pages() 仍然会把它们拼进来。
    """
    rows = db.query("""SELECT page_no, bool_or(ok) AS any_ok
                         FROM doc_page_vision WHERE file_id=%s
                        GROUP BY page_no""", (file_id,))
    return {r["page_no"] for r in rows if not r["any_ok"]}


def read_file(file_id, config_id=None, dpi=None, max_pages=None, max_tokens=4000,
              only_missing_text=False, only_pages=None, retry_failed=False):
    """把一份 PDF **整份**读下来：渲一页、读一页、落一行，最后钉到文件上。

    和 pages_to_read() 的分工：那个是「挑页」——从 doc_lib_ocr 里挑 OCR
    认出表格、或者 OCR 一个字都没认出的页，前提是先跑过 OCR。
    这个不依赖 OCR：渲页自己来（ocr.iter_rendered_pages），整份都读。

    **逐页提示词按渲页时的表格判定选。** detect_table_image() 是纯像素分析，
    不需要 tesseract，所以这个信号在不跑 OCR 的情况下照样拿得到；
    表格页用誊抄表格的提示词，正文页用誊抄文字的——两者混用会让模型
    把一页正文硬塞进 Markdown 表格里。

    only_missing_text=True 时跳过本来就有原生文字层的页。给「混合型 PDF」
    用：那种文件 20 页里 19 页是图、1 页有字，重读那一页是白花钱。

    **但是跳过有代价，用之前想清楚**：text_with_pages() 只拼读图读到的页，
    跳过的那一页**不在里面**。而抽取按「哪份正文更完整」选来源，混合型 PDF
    多半会选中读图这一份——于是那一页原生文字就**整个丢了**。
    省的是一页的钱，丢的是一页的正文。只有在确定那一页没有条款
    （纯封面、纯目录）时才值得开。本项目 2026-10-30 的全量读图**没有开它**。
    """
    from . import ocr                      # 延迟导入：ocr 里有重依赖（fitz/PIL）

    f = db.query_one("""SELECT file_id, abs_path, file_name, ext, fingerprint
                          FROM doc_lib_file WHERE file_id=%s""", (file_id,))
    if not f:
        raise ValueError("没有这个文件：%s" % file_id)
    if (f["ext"] or "").lower() != ".pdf":
        raise ValueError("只支持 PDF，这份是 %s" % f["ext"])
    if not os.path.exists(f["abs_path"]):
        raise ValueError("文件不在磁盘上了：%s" % f["abs_path"])

    cfg = _cfg(config_id)
    native = _native_text_pages(file_id) if only_missing_text else set()
    # retry_failed：只补「一行成功记录都没有」的页。整份重读一遍要花两份钱，
    # 而失败多半是超时这种和页面内容无关的原因。
    want = set(only_pages) if only_pages else (
        pages_without_read(file_id) if retry_failed else None)

    kw = {"file_id": file_id}
    if dpi:
        kw["dpi"] = dpi
    if max_pages:
        kw["max_pages"] = max_pages

    pages_ok = failed = chars = unread = 0
    total = 0
    per_page, errors = [], []
    for r in ocr.iter_rendered_pages(f["abs_path"], **kw):
        total = r["total_pages"]
        if want is not None and r["page_no"] not in want:
            per_page.append({"page_no": r["page_no"], "skipped": "已经读过了"})
            continue
        if r["page_no"] in native:
            per_page.append({"page_no": r["page_no"], "skipped": "本来就有文字层"})
            continue
        kind = "table" if r["has_table"] else "text"
        try:
            got = read_page(file_id, r["page_no"], config_id=cfg["config_id"],
                            kind=kind, max_tokens=max_tokens,
                            image_path=r["image_path"])
        except Exception as e:                                 # noqa: BLE001
            failed += 1
            errors.append("第%d页：%s" % (r["page_no"], e))
            per_page.append({"page_no": r["page_no"], "ok": False, "error": str(e)})
            continue
        if not got["ok"]:
            failed += 1
            errors.append("第%d页：%s" % (r["page_no"], got["message"]))
            per_page.append({"page_no": r["page_no"], "ok": False,
                             "error": got["message"]})
            continue
        pages_ok += 1
        chars += len(_page_text(got["content"]))
        unread += got["unreadable"] or 0
        per_page.append({"page_no": r["page_no"], "ok": True, "kind": kind,
                         "chars": len(_page_text(got["content"])),
                         "unreadable": got["unreadable"],
                         "latency_ms": got["latency_ms"]})

    # ── 汇总**从库里重算，不用本次循环的计数** ──────────────────────────────
    # 补读失败页时本次只跑了几页，拿本次的计数去写 vision_pages 会把
    # 「这份一共读成了多少页」改小——一次补读反而让文件看起来更差了，
    # 而 vision_chars 变小还会让抽取的取文判断（比字数）倒向那份残缺的原生文本。
    # 所以口径是：**这一列永远描述「库里现在有多少」，不是「这一次跑了多少」。**
    have = page_contents(file_id)
    tot_pages = len(have)
    tot_chars = sum(len(_page_text(r["content"])) for r in have)
    tot_unread = sum(r["unreadable"] or 0 for r in have)
    missing = sorted(pages_without_read(file_id))

    # **一页都没读成就算失败，不要留一个「已读图」但正文是空的状态。**
    # 那种状态最坏：抽取闸会放它进来，然后抽出 0 条，看起来像「这份制度
    # 里没有条款」，而真相是一页都没读到。
    ok = tot_pages > 0
    note = "共 %d 页，已读成 %d 页%s；型号 %s" % (
        total, tot_pages,
        ("，仍有 %d 页没读到（%s）" % (len(missing), ",".join(map(str, missing[:12]))))
        if missing else "", cfg["model_name"])
    if errors:
        note += "。本次失败：" + "；".join(errors[:5])
    mark_read(file_id, tot_pages, tot_chars, tot_unread, cfg["model_name"], note, ok=ok)
    return {"file_id": file_id, "file_name": f["file_name"], "ok": ok,
            "total_pages": total, "pages_read": tot_pages,
            "pages_read_this_run": pages_ok, "pages_failed": failed,
            "pages_missing": missing,
            "chars": tot_chars, "unreadable": tot_unread,
            "model_name": cfg["model_name"], "note": note, "pages": per_page}


def _native_text_pages(file_id):
    """这份 PDF 里本来就有文字层的页号。

    用 parse_reason 里那句「共 N 页，其中 M 页无文字层」反推不出**是哪几页**，
    所以直接翻一遍 PDF。只在 only_missing_text=True 时才调。
    """
    import fitz
    row = db.query_one("SELECT abs_path FROM doc_lib_file WHERE file_id=%s", (file_id,))
    if not row:
        return set()
    out = set()
    doc = fitz.open(row["abs_path"])
    try:
        for i, page in enumerate(doc, 1):
            if len((page.get_text() or "").strip()) >= 50:
                out.add(i)
    finally:
        doc.close()
    return out


def files_to_read(instance_id=None, include_thin=True, thin_chars=500):
    """该整份读图的文件：取不到正文的，以及**取到一点点**的。

    为什么 thin 那一档非要有：实测 822港航 有两份 PDF 被判成「已解析」，
    其实是 10 页里 9 页无文字层（总共 3 个字）、20 页里 19 页无文字层
    （262 字）——封面有薄薄一层字，正文全是图。它们**绕过了「需OCR」**，
    于是既不会被 OCR 捡起来，也进了抽取却抽不出东西。
    只按 parse_state 挑，这两份会永远卡在中间。
    """
    sql = ["""SELECT file_id, instance_id, file_name, rel_path, ext, parse_state,
                     text_chars, vision_state, vision_fingerprint, fingerprint
                FROM doc_lib_file
               WHERE NOT missing AND ext='.pdf'"""]
    args = []
    if instance_id:
        sql.append("AND instance_id = %s"); args.append(instance_id)
    if include_thin:
        sql.append("AND (parse_state <> '已解析' OR text_chars < %s)")
        args.append(int(thin_chars))
    else:
        sql.append("AND parse_state <> '已解析'")
    sql.append("ORDER BY instance_id, rel_path")
    rows = db.query(" ".join(sql), tuple(args))
    for r in rows:
        fresh = (r["vision_state"] == "已读图"
                 and r["vision_fingerprint"] == r["fingerprint"])
        # **「已读图」不等于「读全了」。** 实测 2026-10-30 那一轮，读图配置的
        # timeout_s=60 卡在成功页耗时的 p90（57.5 秒）上，175 页里 31 页超时，
        # 其中一份 20 页的只读到 6 页。那种文件状态是「已读图」、抽取闸也放行，
        # 但正文缺了七成——而界面上只有一个「已读过」。缺页必须自己说出来。
        miss = sorted(pages_without_read(r["file_id"])) if fresh else []
        r["missing_pages"] = miss
        done = fresh and not miss
        r["need_read"] = not done
        r["why"] = ("已读过" if done else
                    "读过但缺 %d 页（%s）" % (len(miss), ",".join(map(str, miss[:10])))
                    if miss else
                    "读过但文件后来被换过" if r["vision_state"] == "已读图" else
                    "取不到正文" if r["parse_state"] != "已解析" else
                    "只取到 %s 字，正文基本是图" % r["text_chars"])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 互相印证（纪律 #2 取消后不再是必经之路，保留以便将来接回双模型）
# ─────────────────────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_of(content):
    """抄出来的所有数字（按出现顺序）。比对两个模型用。

    为什么比数字不比整段文本：两个模型的排版、标点、空格必然不同，
    比文本会得到「哪哪都不一样」这种没用的结论。
    而表格的价值全在数字与它所在的行列上——数字对不上才是真的对不上。
    """
    body = _UNREADABLE_RE.sub("", content or "")
    return _NUM_RE.findall(body)


def latest_by_model(file_id, page_no):
    return db.query("""SELECT DISTINCT ON (model_name)
                              model_name, attempt, ok, content, rows_parsed, unreadable,
                              latency_ms, prompt_tokens, completion_tokens, created_at
                         FROM doc_page_vision
                        WHERE file_id=%s AND page_no=%s AND ok
                        ORDER BY model_name, attempt DESC""", (file_id, page_no))


def cross_check(file_id, page_no):
    """两个模型读同一页，比数字序列。

    **只有两边一致的才算读出来。** 一个模型读出来的东西，
    没有任何东西能证伪它——这和规则抽取那边的「多源印证」是同一条纪律。
    """
    reads = latest_by_model(file_id, page_no)
    if len(reads) < 2:
        return {"file_id": file_id, "page_no": page_no, "models": len(reads),
                "verdict": "只有一个模型读过", "agree": None,
                "note": "**一个模型读出来的东西没有任何东西能证伪它。**"
                        "换另一个型号再读一遍，才谈得上印证。"}
    a, b = reads[0], reads[1]
    na, nb = numbers_of(a["content"]), numbers_of(b["content"])
    same = na == nb
    sa, sb = set(na), set(nb)
    only_a = sorted(sa - sb)
    only_b = sorted(sb - sa)
    return {
        "file_id": file_id, "page_no": page_no, "models": len(reads),
        "a": {"model_name": a["model_name"], "numbers": len(na),
              "rows": a["rows_parsed"], "unreadable": a["unreadable"]},
        "b": {"model_name": b["model_name"], "numbers": len(nb),
              "rows": b["rows_parsed"], "unreadable": b["unreadable"]},
        "agree": same,
        "only_a": only_a[:40], "only_b": only_b[:40],
        "verdict": "两边数字完全一致" if same else "两边不一致",
        "note": ("数字序列逐位相同——可以作为**候选**提交人工复核。"
                 if same else
                 "**不一致的部分一律不许用。** 不是取其中一个，也不是取交集："
                 "两个模型在同一个格子上给出不同的数字，说明那个格子本来就读不准。"),
    }


def summary():
    rows = db.query("""SELECT v.file_id, f.file_name, v.page_no,
                              count(*) FILTER (WHERE v.ok) AS reads,
                              count(DISTINCT v.model_name) FILTER (WHERE v.ok) AS models,
                              sum(v.prompt_tokens) AS prompt_tokens,
                              sum(v.completion_tokens) AS completion_tokens,
                              max(v.created_at) AS last_at,
                              bool_or(v.verified_by IS NOT NULL) AS verified
                         FROM doc_page_vision v JOIN doc_lib_file f ON f.file_id=v.file_id
                        GROUP BY v.file_id, f.file_name, v.page_no
                        ORDER BY f.file_name, v.page_no""")
    tot = db.query_one("""SELECT count(*) AS calls,
                                 count(*) FILTER (WHERE NOT ok) AS failed,
                                 coalesce(sum(prompt_tokens),0) AS pt,
                                 coalesce(sum(completion_tokens),0) AS ct,
                                 count(DISTINCT model_name) AS models
                            FROM doc_page_vision""")
    return {"pages": rows, "totals": tot,
            "note": "**读图结果不自动可信。** verified_by 为空的行，"
                    "数字不许进 standard_limit、不许进任何规则阈值——"
                    "模型读图不等于人眼读图，它同样会把看不清的格子编出来，"
                    "而且编得通顺。"}


def storage():
    return {"items": [
        {"table_name": "doc_lib_ocr", "what": "本机 OCR 的逐页文本与**渲好的 PNG 路径**",
         "written_by": "文件库的「跑 OCR」（本机 tesseract）", "on_recalc": "按文件重跑",
         "why": "读图发出去的就是这里记的那张 PNG，不是客户目录里的文件"},
        {"table_name": "doc_page_vision",
         "what": "读图结果：一页一模型一行，含 token 数与「看不清格子数」",
         "written_by": "读图动作（**唯一会把制度文件发到外部的动作**）",
         "on_recalc": "**永不覆盖**，重读加一行",
         "why": "两份独立证据都要留着才能互相印证；也要能回答「哪一页的图离开过本系统」"},
        {"table_name": "standard_limit", "what": "规则引擎真正用的限额费率",
         "written_by": "规则资产装载", "on_recalc": "装载时整表重建",
         "why": "**读图结果不会自动流到这里。** 要人工签字之后，"
                "由人决定录入哪几行——阈值错一位，硬规则就从保护变成伤害"},
    ], "note": "读图 → 印证 → **人工签字** → 才谈得上录入。中间少一步都不行。"}
