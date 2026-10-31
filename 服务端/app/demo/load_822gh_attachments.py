# -*- coding: utf-8 -*-
r"""822港航 的 570 个附件 → 库（attachment / invoice / trip_application / trip_calendar）。

用法（**不要加 -I**，理由同 load_822gh_bills）：

    cd 服务端
    python -m app.demo.load_822gh_attachments          # 干跑，只解析不写库
    WRITE=1 python -m app.demo.load_822gh_attachments  # 真写库（可重入）

源：材料\822港航、单据、数据\*.zip
    附件是**嵌套 zip**：外层一个包，里面一张单一个内层包，内层才是 PDF/JPG。
    实测 内层包 82 个（= 单据数）→ 附件文件 570 个（388 PDF + 182 JPG）
        招待  9 单 →  53 个
        差旅 20 单 → 115 个
        日常 53 单 → 402 个

几条口径：

1. **零模型调用。** 388 个 PDF 全部有文字层，发票按坐标重建行就能取到
   发票号码/开票日期/购销双方/金额/税额/价税合计；出差审批单是标签—值
   成对的纯文本。只有 182 个 JPG 需要视觉，**不在本程序范围内**。
   我原来在客户文档里写的是「附件解析要调模型、放到最后做」，
   那句话对 PDF 是错的——先看文件再估工作量，不要凭格式猜。

2. **日常费用那 53 张单的 402 个附件装不进来。** attachment.bill_no 有外键
   指向 expense_bill，而那 53 张单没有数据表、从来没入库（见
   load_822gh_bills 的说明）。所以本程序能入的是 53 + 115 = 168 个。
   **剩下 402 个不是解析失败，是没有单可挂**，报告里分开数。

3. **source_kind = 'zip附件包'**（迁移 0054 新增的取值）。不套用「上传」：
   那个值在这套系统里指走文件库上传链路、有 doc_lib_upload 留痕的文件，
   而这批根本没走那条链路。来源靠 source_ref 记内层包名。

4. **attach_type（附件类型）一律留空。** 那一列有枚举外键指向 EN17，
   而 822港航 的本体里没有这个枚举，也**没有客户自己的附件类型词表**
   （福建海运集团的 A01~A05 来自它自己的 xlsx 导出）。
   按文件名猜一套出来是在编客户的分类口径。文件名原样存在 file_name 里，
   分类留给后面的读图或人工指定（type_source 的 CHECK 只认
   读图判定/人工指定/演示合成 三种，正是防这个的）。

5. **枚举取值一律抄客户原话**：发票类型「电子发票（普通发票）」、
   用餐情况「自行用餐（含免费）」、用车情况「自行用车」、餐次「早/中/晚」，
   都是 PDF 里的字，不是本系统的叫法。缺哪个值 FK 会直接拒，
   所以装之前先把闸过一遍。
"""
import hashlib
import io
import os
import re
import sys
import zipfile

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

import fitz                                                      # noqa: E402

from app import config, db                                       # noqa: E402

IID = "822gh"
ROOT = os.path.join(config.PROJECT_ROOT, "材料", "822港航、单据、数据")
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")
ATT_DIR = os.path.join(WORK, "attach")

RE_BILL = re.compile(r"(FJPG-BX\d+)")
RE_INV_NO = re.compile(r"发票号码[：:]\s*([0-9]{8,24})")
RE_INV_DATE = re.compile(r"开票日期[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日")
# 半角 ¥（U+00A5）与全角 ￥（U+FFE5）都要认：铁路电子客票用的是全角。
RE_MONEY = re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)")
RE_RATE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
RE_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

out = []


def p(s=""):
    out.append(str(s))


def gb(n):
    """zip 里的中文名是 cp437 装的 gbk。解不出就原样返回，不猜。"""
    try:
        return n.encode("cp437").decode("gbk")
    except Exception:                                            # noqa: BLE001
        return n


# ─────────────────────────────────────────────────────────────────────────────
# 一、把嵌套 zip 摊平到 var/ 下
# ─────────────────────────────────────────────────────────────────────────────
def extract_all():
    """解到 var/822gh_import/attach/<单据号>/<文件名>，返回每个文件的元信息。

    **不写回客户的材料目录**——那是文件库的引用根，0015 的只读纪律管着它。
    可重入：已存在且 sha256 一致的不重写。
    """
    os.makedirs(ATT_DIR, exist_ok=True)
    items = []
    for zf in sorted(x for x in os.listdir(ROOT) if x.lower().endswith(".zip")):
        z = zipfile.ZipFile(os.path.join(ROOT, zf))
        inners = [n for n in z.namelist() if n.lower().endswith(".zip")]
        for n in inners:
            disp = gb(n)
            m = RE_BILL.search(disp)
            if not m:
                p("!! 内层包名里没有单据号，跳过：%s" % disp)
                continue
            bn = m.group(1)
            bdir = os.path.join(ATT_DIR, bn)
            os.makedirs(bdir, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(z.read(n))) as iz:
                for seq, e in enumerate(
                        [x for x in iz.namelist() if not x.endswith("/")], 1):
                    name = os.path.basename(gb(e))
                    raw = iz.read(e)
                    sha = hashlib.sha256(raw).hexdigest()
                    # 文件名里可能有 / : * ? " < > | 之类，落盘前换掉
                    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name) or ("f%d" % seq)
                    dest = os.path.join(bdir, safe)
                    if not (os.path.exists(dest)
                            and hashlib.sha256(
                                io.open(dest, "rb").read()).hexdigest() == sha):
                        io.open(dest, "wb").write(raw)
                    items.append({
                        "bill_no": bn, "seq": seq, "file_name": name,
                        "stored_path": os.path.relpath(dest, config.SERVER_DIR),
                        "sha256": sha, "byte_size": len(raw),
                        "content_type": _ctype(name),
                        "source_ref": "%s :: %s" % (zf, os.path.basename(disp)),
                        "raw": raw,
                    })
    return items


def _ctype(name):
    e = os.path.splitext(name)[1].lower()
    return {".pdf": "application/pdf", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".png": "image/png"}.get(e)


# ─────────────────────────────────────────────────────────────────────────────
# 二、PDF 文字层
# ─────────────────────────────────────────────────────────────────────────────
def pdf_lines(raw):
    """按坐标把 PDF 的词重建成行。

    **不能直接用 get_text()**：电子发票的标签和值在内容流里是分开的两段，
    按流顺序读出来是「发票号码：」「开票日期：」一串光标签没有值
    （实测第一版就是这样，以为发票没有文字层，差点整批去走视觉）。
    按 y 坐标分桶（4pt 一桶）、桶内按 x 排序，才还原成人看到的那一行。
    """
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        rows = {}
        for pno, pg in enumerate(doc):
            for w in pg.get_text("words"):
                rows.setdefault((pno, round(w[1] / 4)), []).append((w[0], w[4]))
        # sorted(rows) 给的是**键**，不是键值对。第一版写成
        # `for _, v in sorted(rows)`，于是 v 是桶号那个 int，
        # sorted(v) 报「'int' object is not iterable」，113 个 PDF 全算成
        # 「打不开」——**错的是我的遍历，不是 PDF**。
        return [" ".join(t for _, t in sorted(v)) for _, v in sorted(rows.items())]
    finally:
        doc.close()


# 发票票面标题 → 本体 EN09 发票类型。
# **这是一次口径映射，所以写成一张明表、不写成模糊匹配。**
# 左边是票面原话（全电发票的标题），右边是 EN09 的取值
# （和福建海运集团同一套：发票类型是国家口径，不是公司口径）。
# 「数电」= 数字化电子发票 = 全电发票，这是税务口径里的同一样东西。
# 映不上的一律留 None 并在报告里列出来——**不许模糊匹配兜底**，
# 兜错了会把专票算成普票，而专票普票的审核要求不一样。
INV_TYPE_MAP = {
    "电子发票（普通发票）": "数电普票",
    "电子发票（增值税专用发票）": "数电专票",
    "电子发票（铁路电子客票）": "铁路电子客票",
    "增值税普通发票": "增值税普通发票",
    "增值税专用发票": "增值税专用发票",
}
RE_INV_TITLE = re.compile(r"(电子发票（[^）]{2,16}）|增值税[普专][通用]发票)")


def _norm(s):
    """票面上「电⼦」用的是 CJK 部件「⼦」(U+2F26)，不是汉字「子」。
    不归一化的后果：同一种发票出来两个取值，枚举对照永远缺值。"""
    return (s or "").replace("⼦", "子").replace("⽤", "用").replace("⾮", "非")


def parse_invoice(lines):
    """电子发票 → 字段。取不到的留 None，**不猜**。"""
    lines = [_norm(x) for x in lines]
    txt = "\n".join(lines)
    if "发票" not in txt:
        return None
    no = RE_INV_NO.search(txt)
    d = RE_INV_DATE.search(txt)
    # 标题要**截到括号为止**：有些版式的标题和发票号在同一个 y 桶里，
    # 整行拿下来就成了「电子发票（普通发票） 发票号码： 2631700000…」，
    # 于是发票类型多出 3 个各只出现一次的"取值"。
    mt = RE_INV_TITLE.search(txt)
    face_type = mt.group(1) if mt else None
    inv_type = INV_TYPE_MAP.get(face_type)
    # ── 购销双方 ──────────────────────────────────────────────────────────
    # 三种版式都见过，所以按**优先级**取，取不到就留空：
    #   ① 显式写了「购买方名称」「销售方名称」——最可靠，直接用
    #   ② 同一行里两个「名称：」——增值税发票的常见版式，第一个是购买方
    #   ③ 全文里按出现顺序的「名称：」，第一个购买方、第二个销售方
    # **方向错了比取不到更坏**：会把本公司当成供应商，
    # 「供应商是否关联方」那类规则就全反了。
    buyer = seller = None
    m1 = re.search(r"购买方名称[:：]\s*([^\s]+)", txt)
    m2 = re.search(r"销售方名称[:：]\s*([^\s]+)", txt)
    if m1 or m2:
        buyer = m1.group(1) if m1 else None
        seller = m2.group(1) if m2 else None
    else:
        for ln in lines:
            if ln.count("名称：") == 2:
                a, b = ln.split("名称：")[1:3]
                buyer, seller = a.strip() or None, b.strip() or None
                break
        else:
            names = re.findall(r"名\s*称[:：]\s*([^\s]+)", txt)
            if names:
                buyer = names[0]
                seller = names[1] if len(names) > 1 else None

    # ── 金额 ─────────────────────────────────────────────────────────────
    # 铁路电子客票用的是**全角 ￥**（U+FFE5），不是半角 ¥（U+00A5）。
    # 第一版只认半角，于是 39 张火车票一张都没取到金额——
    # 而 39 张正是这批发票里最多的一类，交通费规则全靠它。
    # 取值优先级：价税合计（小写）→ 票价 → 「合 计」行的两个数 → 孤立的一个数
    amt = tax = total = None
    for ln in lines:
        mm = RE_MONEY.findall(ln)
        if ("价税合计" in ln or "发票总金额" in ln) and mm:
            total = float(mm[-1])
        elif "票价" in ln and mm:
            total = total if total is not None else float(mm[0])
        elif "合" in ln and "计" in ln and len(mm) >= 2:
            amt, tax = float(mm[0]), float(mm[1])
    if total is None:
        # 整张票面只出现一个金额（火车票那种）：票价与价税合计是同一个数
        solo = [float(x) for ln in lines for x in RE_MONEY.findall(ln)]
        if amt is None and len(set(solo)) == 1:
            total = solo[0]
    rate = None
    for ln in lines:
        if "*" in ln or "税率" in ln:
            mr = RE_RATE.search(ln)
            if mr:
                rate = float(mr.group(1))
                break
    detail = next((ln.strip() for ln in lines if ln.strip().startswith("*")), None)
    if detail is None and face_type and "铁路" in face_type:
        # 火车票的「项目」是车次+席别那一行。**不取乘车人那一行**——
        # 那一行有身份证号和姓名，而 detail_text 是要出报告的字段。
        detail = next((ln.strip() for ln in lines
                       if re.search(r"[0-9]+车[0-9]+[A-Z]?号", ln)), None)
    return {
        "inv_no": no.group(1) if no else None,
        "inv_type": inv_type,
        "face_type": face_type,
        "issue_date": ("%s-%02d-%02d" % (d.group(1), int(d.group(2)), int(d.group(3)))
                       if d else None),
        "title": buyer, "issuer": seller,
        "total_amount": total if total is not None else amt,
        "tax_amount": tax, "tax_rate": rate, "detail_text": detail,
        "is_eticket": bool(face_type and "电子发票" in face_type),
    }


# 出差审批单的标签，顺序即 PDF 里的出现顺序
TRIP_LABELS = ["填写人", "出差地点", "出差人", "人数", "同行人员", "出差事由",
               "出差起止时间", "出差天数", "经办人", "部室负责人", "分管领导",
               "主管领导", "填报日期", "审批日期"]
TRIP_SET = set(TRIP_LABELS) | {"以下内容由出差人员如实申报", "差旅用餐情况",
                               "差旅用车情况", "附件", "出差人签名", "说明"}


def parse_trip(lines):
    """出差审批单 → 表头字段 + 逐日用餐/用车。

    PDF 是「标签一行、值一行」的纯文本，但值会跨行折断
    （实测「2026-05-20   09:16:1」+「5」是同一个时间戳被切成两行），
    所以取值方式是**累积到下一个已知标签为止**，不是逐行配对。
    照抄 app/perception/approval_pdf.py 那边用代价换来的做法。
    """
    flat = []
    for ln in lines:
        flat.extend(x.strip() for x in ln.split() if x.strip())
    if "出差审批单" not in "".join(flat[:4]):
        return None
    hdr, cur, i = {}, None, 0
    while i < len(flat):
        t = flat[i]
        if t in TRIP_SET:
            cur = t
            i += 1
            buf = []
            while i < len(flat) and flat[i] not in TRIP_SET:
                buf.append(flat[i])
                i += 1
            if cur not in hdr:
                hdr[cur] = " ".join(buf).strip()
            continue
        i += 1

    # 日期块：**这一块的标签排在值后面**，不能按「标签—值」取。
    # 实测 token 流是
    #   … 主管领导 林** 2026-05-20 09:16:1 填报日期 审批日期 审批日期 审批日期
    #     5 2026-05-20 10:28:09 2026-05-20 10:41:39 2026-05-20 13:55:31 …
    # 四个日期单元格在上面一行、四个标签在下面一行，按坐标重建之后
    # 值先出现。第一版按标签取，填报日期 15 份一份都没取到（0/15），
    # 而审批日期碰巧对了——**碰巧对的那一半最容易让人以为整块都对**。
    # 改成：主管领导之后、「以下内容由出差人员如实申报」之前的所有日期，
    # 按顺序第一个是填报日期、其余是审批日期。
    a = flat.index("主管领导") if "主管领导" in flat else 0
    b = (flat.index("以下内容由出差人员如实申报")
         if "以下内容由出差人员如实申报" in flat else len(flat))
    stamps = [t for t in flat[a:b] if RE_DATE.fullmatch(t)]
    approve_dates = stamps[1:]
    hdr["填报日期"] = stamps[0] if stamps else ""

    def dt(s):
        m = RE_DATE.search(s or "")
        return ("%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
                if m else None)

    rng = hdr.get("出差起止时间") or ""
    ds = RE_DATE.findall(rng)
    days = None
    try:
        days = int(re.sub(r"\D", "", hdr.get("出差天数") or "") or 0) or None
    except ValueError:
        days = None
    cnt = None
    try:
        cnt = int(re.sub(r"\D", "", hdr.get("人数") or "") or 0) or None
    except ValueError:
        cnt = None
    # 逐日：用餐段与用车段里出现的日期
    meal_days, car_days = _slots(lines, "差旅用餐情况", "差旅用车情况")
    return {
        "filler": hdr.get("填写人") or None,
        "place": hdr.get("出差地点") or None,
        "traveler": hdr.get("出差人") or None,
        "person_count": cnt,
        "companions": hdr.get("同行人员") or None,
        "reason": hdr.get("出差事由") or None,
        "start_date": ("%s-%02d-%02d" % (ds[0][0], int(ds[0][1]), int(ds[0][2]))
                       if ds else None),
        "end_date": ("%s-%02d-%02d" % (ds[-1][0], int(ds[-1][1]), int(ds[-1][2]))
                     if len(ds) > 1 else None),
        "days": days,
        "approve_date": dt(approve_dates[-1]) if approve_dates else None,
        "backfill_date": dt(hdr.get("填报日期")),
        "n_approvals": len(approve_dates),
        "roles": {k: hdr.get(k) for k in ("经办人", "部室负责人", "分管领导",
                                          "主管领导") if hdr.get(k)},
        "meal_days": meal_days,
        "car_days": car_days,
    }


def _slots(lines, head_meal, head_car):
    """用餐段/用车段里的 (日期, 该日各格的取值)。段的边界靠小标题切。"""
    flat = []
    for ln in lines:
        flat.extend(x.strip() for x in ln.split() if x.strip())
    def seg(a, b):
        try:
            i = flat.index(a)
        except ValueError:
            return []
        j = len(flat)
        for bb in b:
            if bb in flat[i:]:
                j = min(j, flat.index(bb, i))
        return flat[i:j]
    meal, car = [], []
    # 每日取几格：用餐三格（早/中/晚），用车两格（上午/下午）。
    # **只收带汉字的 token**：这两张小表后面还有「金额」「备注」两列，
    # 金额填 0 时会多出一个 '0'，第一版把它也收进取值集合，
    # 于是用餐情况的取值里出现了一个 '0'——那不是业务取值，是隔壁列。
    for sect, bag, k in ((seg(head_meal, [head_car]), meal, 3),
                         (seg(head_car, ["说明", "附件", "出差人签名", "注："]),
                          car, 2)):
        day, vals = None, []
        for t in sect:
            m = RE_DATE.fullmatch(t)
            if m:
                if day:
                    bag.append((day, vals[:k]))
                day = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
                vals = []
            elif day and re.search(r"[一-龥]", t):
                vals.append(t)
        if day:
            bag.append((day, vals[:k]))
    return meal, car


# ─────────────────────────────────────────────────────────────────────────────
# 三、解析一遍，报数
# ─────────────────────────────────────────────────────────────────────────────
items = extract_all()
p("=== 解压 ===")
p("  附件文件 %d 个 / 单据 %d 张" % (len(items), len({x["bill_no"] for x in items})))
p("  落盘到 %s" % os.path.relpath(ATT_DIR, config.SERVER_DIR))

have_bill = {r["bill_no"] for r in db.query(
    "SELECT bill_no FROM expense_bill WHERE instance_id=%s", (IID,))}
mine = [x for x in items if x["bill_no"] in have_bill]
orphan = [x for x in items if x["bill_no"] not in have_bill]
p("  能挂上单据的 %d 个；挂不上的 %d 个（%d 张单不在库里——"
  "日常费用那 53 张只给了附件没给数据表）"
  % (len(mine), len(orphan), len({x["bill_no"] for x in orphan})))

# 同一张单里有**内容完全相同**的两份附件（同一张发票以两个文件名各存一份）。
# 库里 attachment(bill_no, sha256) 是唯一约束——那道闸是对的，
# 它防的就是「同一个附件数两遍」，所以不是去放宽约束，是在这里按内容去重。
# 留文件名更像业务叫法的那一份（「发票附件-…」是客户系统的命名）。
_seen, _dups = {}, []
for x in mine:
    k = (x["bill_no"], x["sha256"])
    if k in _seen:
        _dups.append((x["bill_no"], x["file_name"], _seen[k]["file_name"]))
        if x["file_name"].startswith("发票附件"):
            _seen[k] = x          # 换成业务叫法那一份
    else:
        _seen[k] = x
mine = sorted(_seen.values(), key=lambda v: (v["bill_no"], v["seq"]))
p("  同一张单里内容重复的 %d 个，按 sha256 去重后入库 %d 个" % (len(_dups), len(mine)))
for d in _dups[:5]:
    p("    · %s  %s ≡ %s" % d)

invs, trips, failed = [], [], []
for x in mine:
    if x["content_type"] != "application/pdf":
        continue
    try:
        lines = pdf_lines(x["raw"])
    except Exception as e:                                       # noqa: BLE001
        failed.append((x["file_name"], "PDF 打不开：%s" % e))
        continue
    if not any(ln.strip() for ln in lines):
        failed.append((x["file_name"], "没有文字层（扫描件，要走视觉）"))
        continue
    t = parse_trip(lines)
    if t:
        t.update(bill_no=x["bill_no"], src=x["file_name"])
        trips.append(t)
        continue
    iv = parse_invoice(lines)
    if iv and iv["inv_no"]:
        iv.update(bill_no=x["bill_no"], src=x["file_name"])
        invs.append(iv)

# 同一张发票常以两个文件名各存一份（「发票附件-<号>.pdf」和「<号>-电子发票.pdf」），
# 而 invoice.invoice_id 是主键。按发票号去重，**留字段取得最全的那一份**，
# 不是留先遇到的那一份——两份的版式不同，解析出来的完整度不一样。
def _filled(i):
    return sum(1 for k in ("inv_type", "issue_date", "title", "issuer",
                           "total_amount", "tax_amount", "tax_rate", "detail_text")
               if i.get(k) is not None)


_by_no = {}
for _i in invs:
    _old = _by_no.get(_i["inv_no"])
    if _old is None or _filled(_i) > _filled(_old):
        _by_no[_i["inv_no"]] = _i
dup_n = len(invs) - len(_by_no)
invs = sorted(_by_no.values(), key=lambda i: i["inv_no"])

p("")
p("=== 发票（零模型，按坐标重建文字层）===")
p("  去重后 %d 张（同一发票号重复出现 %d 次，同一张票两个文件名各存一份）"
  % (len(invs), dup_n))
for k in ("inv_type", "issue_date", "title", "issuer", "total_amount",
          "tax_amount", "tax_rate", "detail_text"):
    n = sum(1 for i in invs if i.get(k) is not None)
    p("  %-14s 取到 %d/%d" % (k, n, len(invs)))
p("  票面标题: %s" % sorted({i["face_type"] for i in invs if i["face_type"]}))
p("  映到 EN09: %s" % sorted({i["inv_type"] for i in invs if i["inv_type"]}))
_unmapped = sorted({i["face_type"] for i in invs if i["face_type"] and not i["inv_type"]})
_notitle = [i["src"] for i in invs if not i["face_type"]]
p("  **票面标题映不到 EN09 的**: %s" % (_unmapped or "无"))
p("  **连标题都没认出来的** %d 张: %s" % (len(_notitle), _notitle[:4]))
p("  金额合计 %.2f 元（只算取到金额的那些，不代表全部）"
  % sum(i["total_amount"] or 0 for i in invs))
_noamt = [(i["src"], i["face_type"]) for i in invs if i["total_amount"] is None]
p("  **还取不到金额的** %d 张（第四种版式，留空不猜）: %s"
  % (len(_noamt), _noamt[:4]))
p("  说明：铁路电子客票没有税额/税率是**票面本来就没有**，不是解析漏了；"
  "销售方同理（铁路票不写销售方名称）。")

p("")
p("=== 出差审批单 ===")
p("  解析出 %d 份 / %d 张单" % (len(trips), len({t["bill_no"] for t in trips})))
for k in ("place", "traveler", "person_count", "reason", "start_date",
          "end_date", "days", "approve_date", "backfill_date"):
    n = sum(1 for t in trips if t.get(k) is not None)
    p("  %-14s 取到 %d/%d" % (k, n, len(trips)))
p("  审批时间戳条数分布: %s"
  % {n: sum(1 for t in trips if t["n_approvals"] == n)
     for n in sorted({t["n_approvals"] for t in trips})})
p("  审批角色出现次数: %s"
  % {r: sum(1 for t in trips if r in t["roles"])
     for r in ("经办人", "部室负责人", "分管领导", "主管领导")})
p("  逐日用餐行数 %d / 逐日用车行数 %d"
  % (sum(len(t["meal_days"]) for t in trips), sum(len(t["car_days"]) for t in trips)))
_mv = sorted({v for t in trips for _, vs in t["meal_days"] for v in vs})
_cv = sorted({v for t in trips for _, vs in t["car_days"] for v in vs})
p("  用餐格取值: %s" % _mv[:8])
p("  用车格取值: %s" % _cv[:8])
p("  有同行人员的: %d 份" % sum(1 for t in trips if t["companions"]))

p("")
p("=== 解析不了的 ===")
p("  %d 个" % len(failed))
for fn, why in failed[:12]:
    p("  · %s —— %s" % (fn, why))

p("")
p("=== 枚举对照（缺值会被外键拒）===")
for eid, lab, used in (
        ("EN09", "invoice.inv_type", {i["inv_type"] for i in invs if i["inv_type"]}),
        ("EN05", "trip_application.meal_situation", set(_mv)),
        ("EN06", "trip_application.car_situation", set(_cv)),
        ("EN08", "trip_calendar.meal_slot", {"早", "中", "晚"}),
        ("EN17", "attachment.attach_type", set())):
    have = {r["value"] for r in db.query(
        "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id=%s",
        (IID, eid))}
    p("  %s %-34s 本体里 %d 个；用到 %d 个；缺 %s"
      % (eid, lab, len(have), len(used), sorted(used - have) or "无"))


# ─────────────────────────────────────────────────────────────────────────────
# 四、入库
# ─────────────────────────────────────────────────────────────────────────────
# trip_calendar 的口径由这里定下来（**库里原来一行都没有**，
# 福建海运集团那边 trip_application 是演示合成的、trip_calendar 是空表，
# 所以没有现成约定可抄）：
#   · 一天三行，每行一个餐次（早/中/晚），meal_slot 记餐次
#   · has_meal = 该餐**不是**「自行用餐（含免费）」 → 即接待单位安排了
#   · has_car  = 该日的用车格里出现过「自行用车」以外的取值
#   · place 留空 —— **逐日地点 PDF 里没有**，整单只有一个出差地点
# 这是我定的口径，不是读出来的；要是和业务侧的理解不一样，改这里一处。
SELF_MEAL = "自行用餐（含免费）"
SELF_CAR = "自行用车"
SLOTS = ["早", "中", "晚"]


def _one_or_none(vals):
    """逐日逐格的取值聚合成单据级的一个值：全都一样才给，否则留空。
    挑一个「主要的」出来就是在编——逐日明细在 trip_calendar 里，
    单据级这一格宁可空着。"""
    s = {v for v in vals if v}
    return s.pop() if len(s) == 1 else None


if os.environ.get("WRITE") == "1":
    with db.tx() as conn:
        cur = conn.cursor()
        # 可重入：只清本实例这几张表。**别家一行不动。**
        # trip_calendar 由 trip_application 的外键 ON DELETE CASCADE 带走。
        cur.execute("DELETE FROM trip_application WHERE instance_id=%s", (IID,))
        cur.execute("DELETE FROM invoice WHERE instance_id=%s", (IID,))
        cur.execute("DELETE FROM attachment WHERE instance_id=%s", (IID,))

        # 附件。instance_id 由 0046 的触发器从报账单继承，这里不传。
        # attach_type 留空（见文件头口径 4）。
        for x in mine:
            cur.execute("""INSERT INTO attachment(bill_no, file_name, stored_path,
                               sha256, byte_size, content_type, seq,
                               source_kind, source_ref)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'zip附件包',%s)""",
                        (x["bill_no"], x["file_name"], x["stored_path"],
                         x["sha256"], x["byte_size"], x["content_type"],
                         x["seq"], x["source_ref"]))
        # 发票。invoice_id 用发票号（它本身就是唯一标识，不另造一个）。
        for i in invs:
            cur.execute("""INSERT INTO invoice(invoice_id, bill_no, inv_type, inv_no,
                               issue_date, issuer, title, total_amount, tax_amount,
                               tax_rate, detail_text, is_eticket, ocr_source)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (i["inv_no"], i["bill_no"], i["inv_type"], i["inv_no"],
                         i["issue_date"], i["issuer"], i["title"],
                         i["total_amount"], i["tax_amount"], i["tax_rate"],
                         i["detail_text"], i["is_eticket"], "PDF文字层"))
        # 出差审批单 + 逐日
        n_cal = 0
        for t in trips:
            meal_vals = [v for _, vs in t["meal_days"] for v in vs]
            car_vals = [v for _, vs in t["car_days"] for v in vs]
            # app_no：PDF 文件名里那串长数字是 OA 的流程实例号，是真编号。
            # 取不到就退回「单据号-OA」，并在 extracted_from 里标明来源。
            m = re.search(r"\((\d{8,24})\)", t["src"]) or re.search(
                r"(\d{16,24})", t["src"])
            app_no = m.group(1) if m else ("%s-OA" % t["bill_no"])
            cur.execute("""INSERT INTO trip_application(app_no, bill_no, start_date,
                               end_date, place, days, person_count, approve_date,
                               backfill_date, meal_situation, car_situation,
                               reason, extracted_from)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'审批单读图')
                           ON CONFLICT (app_no) DO NOTHING""",
                        (app_no, t["bill_no"], t["start_date"], t["end_date"],
                         t["place"], t["days"], t["person_count"],
                         t["approve_date"], t["backfill_date"],
                         _one_or_none(meal_vals), _one_or_none(car_vals),
                         t["reason"]))
            car_by_day = {d: vs for d, vs in t["car_days"]}
            for d, vs in t["meal_days"]:
                has_car = any(v and v != SELF_CAR for v in car_by_day.get(d, []))
                for k, slot in enumerate(SLOTS):
                    v = vs[k] if k < len(vs) else None
                    cur.execute("""INSERT INTO trip_calendar(app_no, cal_date,
                                       meal_slot, has_meal, has_car)
                                   VALUES (%s,%s,%s,%s,%s)""",
                                (app_no, d, slot,
                                 (v is not None and v != SELF_MEAL), has_car))
                    n_cal += 1
    p("")
    p("**已入库**")
    for q, lab in (
            ("SELECT count(*) FROM attachment WHERE instance_id=%s", "附件"),
            ("SELECT count(*) FROM invoice WHERE instance_id=%s", "发票"),
            ("""SELECT count(total_amount) FROM invoice WHERE instance_id=%s""",
             "发票·有金额"),
            ("SELECT count(*) FROM trip_application WHERE instance_id=%s",
             "出差审批单"),
            ("SELECT count(*) FROM trip_calendar WHERE instance_id=%s", "逐日行"),
            ("""SELECT count(*) FROM trip_calendar WHERE instance_id=%s
                  AND has_meal""", "逐日行·接待单位安排了餐")):
        p("  %s: %s" % (lab, db.scalar(q, (IID,))))
    p("  别家没被动：fjhy 附件 %s / 发票 %s / 出差审批单 %s"
      % (db.scalar("SELECT count(*) FROM attachment WHERE instance_id='fjhy'"),
         db.scalar("SELECT count(*) FROM invoice WHERE instance_id='fjhy'"),
         db.scalar("SELECT count(*) FROM trip_application "
                   "WHERE instance_id='fjhy'")))
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "attach822.txt"), "w", encoding="utf-8").write("\n".join(out))
print("ok: 附件 %d（可挂 %d）/ 发票 %d / 出差审批单 %d / 解析失败 %d"
      % (len(items), len(mine), len(invs), len(trips), len(failed)))
