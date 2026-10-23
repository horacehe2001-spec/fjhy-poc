# -*- coding: utf-8 -*-
"""本机 OCR：把文件库里的纯扫描件 PDF 补出文字。

**只在本机跑，不出网、不计费、不把客户制度文件发给任何第三方。**
引擎是 tesseract 5.4，语言包用项目自带的 tessdata_best（见 vendor/tessdata）。

四条纪律，每一条都有实测代价在背后：

1. **渲出来的 PNG 一律写在 var/ocr/ 下，绝不写回被扫描的目录。**
   只读纪律不止管「不改文件」，也管「不往人家目录里放东西」。
   客户的资料目录是证据，多出一个 .png 就说不清这份资料原本有什么。

2. **逐页记置信度，不记整份平均。**
   27 页里 3 页糊，整份平均分照样好看，而规则阈值可能恰恰就在那 3 页上。
   要能指着页说「这一页不可信」。

3. **表格页单独标红。**
   实测：住宿费标准表 OCR 后数字全在（460/420/430/310/480/440），
   但城市↔职级↔金额的对应关系全乱了。**OCR 丢的不是字，是结构。**
   而差旅规则的全部价值就在那个对应关系上——数字对了关系错了，
   比识别不出来更危险：它会生成一条看起来有据可查的错规则。
   表格页的 OCR 文本只能当线索，要么人眼读图（PNG 已经渲好了），
   要么上多模态模型。

4. **OCR 文本与原生文本必须一路区分到规则草稿上。**
   项目可信度三档（原生文本 / 人眼读图 / OCR），只有前两档能进硬规则。
   OCR 来的数字一律标待人工复核——阈值错一位，整条硬规则就从保护变成伤害。

为什么不在扫描阶段跑：OCR 一页几百毫秒到几秒，27 页就是一分钟起步。
扫描要快到可以随时重跑，重扫一慢就没人跑，快照也就不再反映现状。
所以 OCR 是单独一个动作，由人点，针对「需OCR」那批文件。
"""
import datetime
import io
import os
import re
import shutil
import subprocess

from . import config, db

# ── 引擎定位 ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
VENDOR_TESSDATA = os.path.join(_SERVER, "vendor", "tessdata")

CANDIDATE_EXES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

LANG = "chi_sim+eng"
DEFAULT_DPI = 170          # 项目先例：住宿费标准表就是用 dpi=170 渲 PNG 读出来的
LOW_CONF = 60              # 低于这个分数的词算「可疑词」
MAX_PAGES = 200            # 单份文件的页数上限，防止一份巨型 PDF 把请求挂死


def tesseract_exe():
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in CANDIDATE_EXES:
        if os.path.exists(p):
            return p
    return None


def engine_info():
    """引擎与语言包的实际情况。**装没装、用的哪份语言包，必须能在界面上看到**——
    同一份扫描件用 fast 包和 best 包出来的结果差很多，
    事后回看「这段文字当初是怎么来的」，没有这个信息就说不清。"""
    exe = tesseract_exe()
    if not exe:
        return {"ok": False, "reason": "本机没找到 tesseract。装了之后重启后端即可。",
                "exe": None, "version": None, "langs": [], "tessdata": None,
                "variant": None}
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True, timeout=20)
        version = (ver.stdout or ver.stderr).decode("utf-8", "replace").splitlines()[0].strip()
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": "tesseract 调不起来：%s" % e, "exe": exe,
                "version": None, "langs": [], "tessdata": None, "variant": None}

    use_vendor = os.path.exists(os.path.join(VENDOR_TESSDATA, "chi_sim.traineddata"))
    tessdata = VENDOR_TESSDATA if use_vendor else None
    langs = []
    try:
        cmd = [exe, "--list-langs"]
        if tessdata:
            cmd += ["--tessdata-dir", tessdata]
        out = subprocess.run(cmd, capture_output=True, timeout=20)
        txt = (out.stdout or out.stderr).decode("utf-8", "replace")
        langs = [l.strip() for l in txt.splitlines()[1:] if l.strip()]
    except Exception:                                          # noqa: BLE001
        pass

    size_mb = None
    if use_vendor:
        try:
            size_mb = round(os.path.getsize(
                os.path.join(VENDOR_TESSDATA, "chi_sim.traineddata")) / 1048576, 1)
        except OSError:
            pass
    return {
        "ok": "chi_sim" in langs,
        "exe": exe, "version": version, "langs": langs,
        "tessdata": tessdata or "（系统自带）",
        "variant": ("tessdata_best（项目自带，chi_sim %s MB）" % size_mb) if use_vendor
                   else "系统自带语言包",
        "lang_arg": LANG, "dpi": DEFAULT_DPI,
        "reason": "" if "chi_sim" in langs else
                  "没有 chi_sim 语言包，中文识别不了。",
        "note": "OCR 全程在本机跑，不出网、不计费、不把文件发给第三方。",
    }


def out_dir():
    """渲图落盘的位置。**在项目 var/ 下，不在被扫描目录里。**"""
    d = os.path.join(os.path.dirname(_SERVER), "var", "ocr")
    os.makedirs(d, exist_ok=True)
    return d


# ── 单页 OCR ────────────────────────────────────────────────────────────────
def detect_table_image(img, min_h=8, min_h_with_v=3, min_v=2, ink=0.55):
    """从**图像像素**判断这一页像不像表格 → (是否, 横线数, 竖线数)。

    为什么非要有这个：`fitz.find_tables()` 靠的是页面里的矢量线条和文字块，
    而扫描件整页就是一张位图，什么结构都没有——实测 27 页《差旅管理实施细则》
    用 find_tables() 全部返回 0 个表格。也就是说「表格页标红」这个机制，
    **恰恰在最需要它的那批文件上完全失效**。
    而标红失效的后果不是少个提示：住宿费标准表那种页会被当成普通文本页，
    OCR 出来的 460/420/430 被下游当作可用数字，可城市↔职级的对应关系早乱了。

    做法很笨但够用：灰度 → 二值化 → 数「整行/整列里有多少比例是黑的」。
    表格的边框线会让某一行（或某一列）大部分像素都是黑的，正文不会。

    **不能要求必须有竖线。** 中文公文里大量是「三线表」——只有横线没有竖线。
    第一版定成「3 横 + 2 竖」，实测 27 页《差旅管理实施细则》漏掉两页：
    第 15 页（住宿费标准表续页，新疆各地区）和第 17 页（附件3 出差审批单），
    两页都是 0 条竖线。而第 15 页恰恰是费率表——漏标的代价就是
    那页 OCR 出来的数字被当成可用数据，可城市↔职级的对应关系早乱了。

    阈值按实测分界取（《差旅管理实施细则》27 页逐页量，表格页真值 13/14/15/17）：
        非表格页：横线 0 / 2 / 4 条（红头线、页眉线、落款线）
        表格页　：横线 15 / 18 / 24 / 25 条
    中间空出 5~14 这一大段，所以门槛定在 8 条横线，分得很干净；
    另留一条「3 横 + 2 竖」给竖线明确的小表格。
    判不准的代价是不对称的——漏标会让错数字流进规则，多标只是多看一眼。
    """
    g = img.convert("L")
    w, h = g.size
    if w < 50 or h < 50:
        return False, 0, 0
    # 缩到宽 800 以内再数，纯粹为了快；表格线这么粗的特征不会因此消失
    if w > 800:
        g = g.resize((800, int(h * 800 / w)))
        w, h = g.size
    px = g.load()
    dark = [[1 if px[x, y] < 128 else 0 for x in range(w)] for y in range(h)]
    def runs(flags):
        """把连续的暗行并成一段。**数的是「几条线」，不是「几行暗像素」。**
        一条 4px 粗的表格线本来就是一条线，按像素行数会算成 4 条；
        一行 14px 高的文字会算成 14 条。两边都被放大十几倍之后，
        阈值就彻底失去意义了——这是测试逼出来的，用实心块画的假正文页
        一下子数出 450「条」线。
        """
        n, prev = 0, False
        for f in flags:
            if f and not prev:
                n += 1
            prev = f
        return n

    hor = runs([sum(dark[y]) >= w * ink for y in range(h)])
    ver = runs([sum(dark[y][x] for y in range(h)) >= h * ink for x in range(w)])
    is_table = hor >= min_h or (hor >= min_h_with_v and ver >= min_v)
    return is_table, hor, ver


def _ocr_image(img, exe, tessdata):
    """→ (text, mean_conf, low_conf_words, word_cnt)。用 TSV 拿逐词置信度。

    **语言包目录走 TESSDATA_PREFIX 环境变量，不走 `--tessdata-dir` 参数。**
    实测踩到：pytesseract 把 config 字符串交给 shlex.split（POSIX 模式）切分，
    Windows 路径里的反斜杠会被当成转义符吃掉，而本项目路径既有空格又有中文，
    拼出来的参数必然是断的——tesseract 退出码非 0，
    报错还因为中文 Windows 用 GBK 输出而在 pytesseract 里引发 UnicodeDecodeError，
    真正的原因被埋了两层。环境变量没有这个问题。
    注意 tesseract 5 的 TESSDATA_PREFIX 指向 **tessdata 目录本身**（4.x 是它的父目录）。
    """
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = exe
    cfg = "--oem 1 --psm 6"       # LSTM 引擎；psm 6 = 当作一整块文本，适合版式规整的公文
    old = os.environ.get("TESSDATA_PREFIX")
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    try:
        data = pytesseract.image_to_data(img, lang=LANG, config=cfg,
                                         output_type=pytesseract.Output.DICT)
    except UnicodeDecodeError as e:
        # pytesseract 用 utf-8 解 tesseract 的 stderr，中文 Windows 下是 GBK。
        # 不让这个解码错误盖住真正的失败原因。
        raise RuntimeError("tesseract 执行失败，且错误信息不是 UTF-8（中文 Windows 常见）。"
                           "常见原因：语言包路径不对、或 PDF 页渲图失败。原始异常：%s" % e)
    finally:
        if tessdata:
            if old is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = old
    words, confs = [], []
    for txt, conf in zip(data.get("text", []), data.get("conf", [])):
        t = (txt or "").strip()
        if not t:
            continue
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c < 0:                  # -1 表示这行不是词（是块/段的占位行）
            continue
        words.append((t, c))
        confs.append(c)
    text = " ".join(w for w, _ in words)
    mean = round(sum(confs) / len(confs), 2) if confs else None
    low = sum(1 for c in confs if c < LOW_CONF)
    return text, mean, low, len(words)


def ocr_pdf(path, dpi=DEFAULT_DPI, keep_images=True, file_id=None, max_pages=MAX_PAGES):
    """对一份 PDF 逐页 OCR。→ [{page_no, text, mean_conf, ...}]

    每页都渲 PNG 并留下来：表格页 OCR 不可信时，人眼读图是项目里唯一拿到过
    完整 15 行费率的办法，图得在手上才能读。
    """
    import fitz
    from PIL import Image

    exe = tesseract_exe()
    if not exe:
        raise RuntimeError("本机没找到 tesseract，无法 OCR")
    tessdata = (VENDOR_TESSDATA
                if os.path.exists(os.path.join(VENDOR_TESSDATA, "chi_sim.traineddata"))
                else None)
    engine = "tesseract %s + %s%s" % (
        (engine_info().get("version") or "?").replace("tesseract ", ""),
        LANG, "（tessdata_best）" if tessdata else "")

    img_dir = os.path.join(out_dir(), file_id or os.path.basename(path)[:40])
    if keep_images:
        os.makedirs(img_dir, exist_ok=True)

    doc = fitz.open(path)
    try:
        pages = []
        for i, page in enumerate(doc, 1):
            if i > max_pages:
                break
            # 表格检测走**两条路取并集**：
            #   矢量：fitz.find_tables() —— 对有文字层的 PDF 准
            #   像素：detect_table_image() —— 扫描件只有位图，矢量那条全程返回 0，
            #        实测 27 页《差旅管理实施细则》find_tables() 一页都没认出来
            # 只留矢量那条，表格标红就恰好在最需要它的扫描件上失效。
            try:
                tf = page.find_tables()
                vec_tables = len(getattr(tf, "tables", []) or [])
            except Exception:                                  # noqa: BLE001
                vec_tables = 0
            pix = page.get_pixmap(dpi=dpi)
            png = pix.tobytes("png")
            img_path = None
            if keep_images:
                img_path = os.path.join(img_dir, "p%03d.png" % i)
                with open(img_path, "wb") as f:
                    f.write(png)
            img = Image.open(io.BytesIO(png))
            px_table, hor, ver = detect_table_image(img)
            tables = vec_tables or (1 if px_table else 0)
            text, mean, low, wc = _ocr_image(img, exe, tessdata)
            pages.append({
                "page_no": i, "text": text, "chars": len(text),
                "mean_conf": mean, "low_conf_words": low, "word_cnt": wc,
                "has_table": tables > 0,
                "table_note": (
                    "检测到表格（%s；横线 %d 竖线 %d）。**OCR 会丢掉行列对应关系**——"
                    "数字可能都在，但「哪个城市、哪个职级、多少钱」对不上。"
                    "实测这类页的置信度也明显更低（本份文件文本页 88~91，表格页 53~84）。"
                    "请人眼读图（PNG 已渲好：%s）或上多模态模型，不要直接拿去抽规则。"
                    % ("矢量线条" if vec_tables else "像素线条", hor, ver,
                       img_path or "未保存")) if tables else None,
                "engine": engine, "dpi": dpi, "image_path": img_path,
            })
        return pages, doc.page_count
    finally:
        doc.close()


# ── 入库 ────────────────────────────────────────────────────────────────────
def run(file_ids=None, dpi=DEFAULT_DPI, limit=5, keep_images=True):
    """对「需OCR」的文件跑 OCR 并落库。

    默认一次最多 limit 份：OCR 慢，一次把几十份全跑了会让请求超时，
    而超时之后人不知道跑到哪了。宁可分几次点，每次都有确定的结果。
    """
    info = engine_info()
    if not info["ok"]:
        raise RuntimeError(info["reason"] or "OCR 引擎不可用")

    sql = ["""SELECT file_id, abs_path, file_name, ext, fingerprint, parse_state
                FROM doc_lib_file
               WHERE NOT missing AND ext='.pdf'"""]
    args = []
    if file_ids:
        sql.append("AND file_id = ANY(%s)"); args.append(list(file_ids))
    else:
        # 没指定就挑「需OCR」的，以及 OCR 过但文件后来又被换过的
        sql.append("""AND (parse_state='需OCR'
                           OR (ocr_state='已OCR'
                               AND ocr_fingerprint IS DISTINCT FROM fingerprint))""")
    sql.append("ORDER BY rel_path LIMIT %s")
    args.append(max(1, min(50, int(limit))))
    files = db.query(" ".join(sql), tuple(args))

    done, failed, results = 0, 0, []
    t0 = datetime.datetime.now()
    for f in files:
        try:
            pages, total_pages = ocr_pdf(f["abs_path"], dpi=dpi,
                                         keep_images=keep_images, file_id=f["file_id"])
        except Exception as e:                                 # noqa: BLE001
            failed += 1
            db.execute("""UPDATE doc_lib_file SET ocr_state='OCR失败', ocr_at=now(),
                                 ocr_note=%s WHERE file_id=%s""",
                       ("%s: %s" % (type(e).__name__, e), f["file_id"]))
            results.append({"file_id": f["file_id"], "file_name": f["file_name"],
                            "ok": False, "error": str(e)})
            continue

        db.execute("DELETE FROM doc_lib_ocr WHERE file_id=%s", (f["file_id"],))
        chars = 0
        confs = []
        tbl_pages = 0
        for p in pages:
            db.execute("""
                INSERT INTO doc_lib_ocr(file_id, page_no, text, chars, mean_conf,
                    low_conf_words, word_cnt, has_table, table_note, engine, dpi, image_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (f["file_id"], p["page_no"], p["text"], p["chars"], p["mean_conf"],
                 p["low_conf_words"], p["word_cnt"], p["has_table"], p["table_note"],
                 p["engine"], p["dpi"], p["image_path"]))
            chars += p["chars"]
            if p["mean_conf"] is not None:
                confs.append(p["mean_conf"])
            if p["has_table"]:
                tbl_pages += 1

        mean = round(sum(confs) / len(confs), 2) if confs else None
        empty_pages = sum(1 for p in pages if not p["chars"])
        note_bits = []
        if tbl_pages:
            note_bits.append("%d 页含表格，行列对应关系不可信，需人眼读图或多模态" % tbl_pages)
        if empty_pages:
            note_bits.append("%d 页一个字都没认出来（可能是整页插图或扫描质量太差）" % empty_pages)
        if mean is not None and mean < 75:
            note_bits.append("整份平均置信度 %.1f 偏低，数字一律需人工复核" % mean)
        db.execute("""UPDATE doc_lib_file
                         SET ocr_state='已OCR', ocr_at=now(), ocr_chars=%s, ocr_pages=%s,
                             ocr_table_pages=%s, ocr_mean_conf=%s, ocr_engine=%s,
                             ocr_fingerprint=fingerprint, ocr_note=%s
                       WHERE file_id=%s""",
                   (chars, len(pages), tbl_pages, mean,
                    pages[0]["engine"] if pages else None,
                    "；".join(note_bits) or None, f["file_id"]))
        done += 1
        results.append({"file_id": f["file_id"], "file_name": f["file_name"], "ok": True,
                        "pages": len(pages), "total_pages": total_pages, "chars": chars,
                        "mean_conf": mean, "table_pages": tbl_pages,
                        "empty_pages": empty_pages,
                        "note": "；".join(note_bits) or None})

    secs = round((datetime.datetime.now() - t0).total_seconds(), 1)
    return {"engine": info["version"], "variant": info["variant"], "dpi": dpi,
            "tried": len(files), "done": done, "failed": failed,
            "elapsed_s": secs, "items": results,
            "note": "OCR 文本**不进文件快照的正文字段**，单独存（doc_lib_ocr）。"
                    "它是加工产物，不是磁盘上的事实。"
                    "可信度比原生文本低一档：数字一律需人工复核，不得直接进硬规则阈值。"}


def pages_of(file_id, limit=400):
    return db.query("""SELECT page_no, chars, mean_conf, low_conf_words, word_cnt,
                              has_table, table_note, engine, dpi, image_path, ocr_at,
                              left(text, 1200) AS preview
                         FROM doc_lib_ocr WHERE file_id=%s
                        ORDER BY page_no LIMIT %s""", (file_id, limit))


def text_of(file_id):
    """整份 OCR 文本，按页拼。供抽取用。"""
    rows = db.query("SELECT text FROM doc_lib_ocr WHERE file_id=%s ORDER BY page_no",
                    (file_id,))
    return "\n".join(r["text"] for r in rows if r["text"])


def stats():
    row = db.query_one("""SELECT count(*) AS files, COALESCE(sum(ocr_pages),0) AS pages,
                                 COALESCE(sum(ocr_chars),0) AS chars,
                                 COALESCE(sum(ocr_table_pages),0) AS table_pages,
                                 avg(ocr_mean_conf) AS mean_conf
                            FROM doc_lib_file WHERE ocr_state='已OCR'""")
    pending = db.scalar("""SELECT count(*) FROM doc_lib_file
                            WHERE NOT missing AND parse_state='需OCR'
                              AND (ocr_state IS NULL OR ocr_state<>'已OCR')""") or 0
    stale = db.scalar("""SELECT count(*) FROM doc_lib_file
                          WHERE ocr_state='已OCR'
                            AND ocr_fingerprint IS DISTINCT FROM fingerprint""") or 0
    return {"ocr_files": row["files"], "ocr_pages": row["pages"],
            "ocr_chars": int(row["chars"] or 0),
            "table_pages": row["table_pages"],
            "mean_conf": round(float(row["mean_conf"]), 2) if row["mean_conf"] else None,
            "pending": pending, "stale": stale,
            "engine": engine_info()}


# ── OCR 文本规范化 ──────────────────────────────────────────────────────────
# tesseract 对中文是**逐字输出、字间插空格**的：
#     "第 十 一 条 ”住宿 费 是 指 工作 人 员 因 公 出 差 期 间 ..."
# 这让下游所有按中文词组写的正则全部落空。实测 27 页《差旅管理实施细则》
# 的 19438 字 OCR 文本里：
#     「第X条」原样匹配 0 处 → 去掉汉字间空格后 36 处
#     「应当」 原样 0 次     → 去空格后 13 次
# 也就是说不规范化的话，OCR 跑完了、字也都在，**抽取一条都切不出来**——
# 看起来像「这份制度里没有条款」，而真相是分隔符不对。
# 这种失败最隐蔽：没有报错，只有一个 0。
_CJK = r"一-鿿㐀-䶿　-〿＀-￯"
_CJK_GAP = re.compile(r"(?<=[%s])[ 	]+(?=[%s])" % (_CJK, _CJK))


def normalize(text):
    """去掉汉字之间的空格。**只对 OCR 文本做，原生文本不碰**——
    原生文本里的空格是排版本来就有的，抹掉会把两个词粘成一个。"""
    if not text:
        return text
    prev = None
    # 「字 字 字」里相邻两对共享同一个空格，一遍替换只能去掉一半，所以跑到稳定为止
    while prev != text:
        prev = text
        text = _CJK_GAP.sub("", text)
    return text


def text_with_pages(file_id):
    """→ (整份文本, [{start, end, page_no, has_table, mean_conf}])。

    带页边界回来，是为了能把**每一条条款**映射回它所在的页，进而回答两个问题：
      这条条款的字是哪一页 OCR 出来的？那一页是不是表格页？
    只在文件级记「这份文件有 2 页是表格」没用——抽规则的人要知道的是
    **手上这一条**是不是从那 2 页里来的。文件级的标记会让 25 页正常内容
    陪着那 2 页一起被怀疑，而真正该怀疑的那条反而混在里面。
    """
    rows = db.query("""SELECT page_no, text, has_table, mean_conf
                         FROM doc_lib_ocr WHERE file_id=%s ORDER BY page_no""", (file_id,))
    parts, spans, pos = [], [], 0
    for r in rows:
        t = normalize(r["text"] or "")
        if not t:
            continue
        spans.append({"start": pos, "end": pos + len(t), "page_no": r["page_no"],
                      "has_table": r["has_table"],
                      "mean_conf": float(r["mean_conf"]) if r["mean_conf"] is not None else None})
        parts.append(t)
        pos += len(t) + 1          # +1 是下面 join 的那个换行
    return "\n".join(parts), spans


def page_at(spans, char_pos):
    """某个字符位置落在哪一页。找不到就返回 None——**不猜**。"""
    for s in spans:
        if s["start"] <= char_pos < s["end"]:
            return s
    return None
