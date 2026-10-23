# -*- coding: utf-8 -*-
"""把 xlsx 里**内嵌的**单据附件图片抽出来，落盘 + 入库。

为什么需要这个模块（2026-10-21，用户指出后实测核实）：

  0004 的表注释写着「附件源数据在 40 张样本里 100% 为空（G-29，阻塞）」。
  **事实没记错，归因错了。** B 列「附件」的**单元格文本**确实是空的，
  但附件一直在交付物里——它们是**内嵌在 xlsx 里的 PNG**：

    差旅报账单明细表.xlsx     「1报销附件」      143 张
    多人差旅报账单明细表.xlsx 「2多人差旅-报销附件」 76 张
    合计 219 张 / 37.3MB，**覆盖全部 40 张单**，每单 1~21 张

  看不见的原因是：xlsx 轻解析只读 XML 文本（sharedStrings + 单元格），
  而图片在 xl/media/ 下、靠 xl/drawings/drawing1.xml 的**锚点**挂到行列上。
  **「我们的解析器读不到」和「客户没给」是两件事**——混了就会去跟客户
  要一份已经给过的东西。

三条纪律：

1. **源目录只读。** 材料/ 是客户交付物，是证据不是工作区。这里只开 zip 读，
   一个字节都不往那边写。落盘一律在 var/attachments/ 下。

2. **抽图不判类型。** attach_type 留空。TR-D6-01（必要附件完整性）要的是
   **类型**（A01 出差审批单 / A03 交通费凭证 / A04 住宿费发票…），
   而决策表 11 种类型里 5 种标着 ocr_needed:true——从一张 PNG 判它是什么，
   必须读图。**有 219 张图 ≠ 能跑 TR-D6-01**，这一步只解决
   「这张单有没有附件、几张、能不能点开看」。类型留空表示「还没判」，
   **不等于「没有类型」**。

3. **对不上的如实报，不丢不猜。** 单据号在库里找不到、图片没有锚点、
   一张单一张图都没有——每一种都单独计数并列出来。
   静默跳过的后果是「抽了 219 张，库里只有 200 条」而没人说得清差在哪。
"""
import hashlib
import os
import re
import zipfile
from xml.etree import ElementTree as ET

from .. import config, db

# OOXML 的那几个命名空间。写全称不是啰嗦——缩写在不同生成器（Excel / WPS）
# 里会变，而这几个 URI 是固定的。实测本项目的样本出自 WPS（etCustomData 命名空间）。
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

# 附件表的识别：按**工作表名**认，不按 sheet 序号。
# 实测两份文件里分别叫「1报销附件」和「2多人差旅-报销附件」，前缀不同、
# 位置都在第三张，但写死 sheet3 会在客户换一次导出模板时静默抽到别的表。
SHEET_PAT = re.compile(r"报销附件")

EXT_BY_TYPE = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tif", "image/emf": ".emf",
}


class ExtractError(Exception):
    """抽取不了。接口层回 400——「这个文件里没有附件表」是使用者的问题。"""


def _rels(z, part):
    """读某个 part 的 .rels。→ {rId: target（已按 part 所在目录解析成包内路径）}"""
    base = os.path.dirname(part)
    rel_path = os.path.join(base, "_rels", os.path.basename(part) + ".rels").replace("\\", "/")
    if rel_path not in z.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(z.read(rel_path)):
        t = rel.get("Target")
        if t.startswith("/"):
            out[rel.get("Id")] = t.lstrip("/")
        else:
            out[rel.get("Id")] = os.path.normpath(os.path.join(base, t)).replace("\\", "/")
    return out


def _shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter("{%s}t" % NS_MAIN)) for si in root]


def _find_sheet(z):
    """找「*报销附件」那张表。→ (工作表名, 包内路径)"""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = _rels(z, "xl/workbook.xml")
    for sh in wb.iter("{%s}sheet" % NS_MAIN):
        label = sh.get("name") or ""
        if SHEET_PAT.search(label):
            rid = sh.get("{%s}id" % NS_R)
            target = rels.get(rid)
            if target:
                return label, target
    raise ExtractError(
        "这份 xlsx 里没有名字含「报销附件」的工作表，找到的是：%s。"
        "附件表是按**表名**认的，不按第几张表——写死序号会在客户换一次"
        "导出模板时静默抽到别的表"
        % "、".join(s.get("name") or "?" for s in wb.iter("{%s}sheet" % NS_MAIN)))


def _cell_text(c, ss):
    """一个单元格的文本。**四种编码都要认。**

    xlsx 存字符串有好几种方式，而**不同工具导出的结果不一样**：
      t="s"          共享字符串表的下标（Excel / WPS 的常规做法，客户样本就是这种）
      t="inlineStr"  内联 <is><t>（openpyxl 等库的默认做法，整份文件可以没有
                     sharedStrings.xml）
      t="str"        公式的计算结果，直接在 <v> 里
      无 t           数字

    只认 t="s" 的后果实测过：拿 openpyxl 造的 xlsx 来抽，单据号**一个都读不到**，
    于是 40 张单变成 0 张，表现和「客户没给附件」一模一样——
    而这整件事（G-29 归因错）就是这么来的。所以这里四种一起认。
    """
    t = c.get("t")
    if t == "inlineStr":
        is_ = c.find("{%s}is" % NS_MAIN)
        if is_ is not None:
            return "".join(x.text or "" for x in is_.iter("{%s}t" % NS_MAIN))
        return ""
    v = c.find("{%s}v" % NS_MAIN)
    if v is None or v.text is None:
        return ""
    if t == "s":
        try:
            return ss[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def _bill_by_row(z, sheet_part, ss):
    """A 列 = 单据编号。→ {行号(1基): 单据号}"""
    sh = ET.fromstring(z.read(sheet_part))
    out = {}
    for row in sh.iter("{%s}row" % NS_MAIN):
        r = int(row.get("r"))
        for c in row.iter("{%s}c" % NS_MAIN):
            if not (c.get("r") or "").startswith("A"):
                continue
            txt = _cell_text(c, ss).strip()
            if txt:
                out[r] = txt
    return out


def _anchors(z, sheet_part):
    """这张表上的图片锚点。→ [(行号1基, 列号0基, 图片包内路径)]

    三种锚点都收（oneCellAnchor / twoCellAnchor / absoluteAnchor）：
    实测本项目样本全是 oneCellAnchor，但只认一种的话，
    客户换个工具导出就会抽出 0 张，而那看起来和「没有附件」一模一样。
    absoluteAnchor 没有 from，落不到行上——单独报，不丢。
    """
    drels = _rels(z, sheet_part)
    drawing = next((t for t in drels.values() if "/drawings/" in t), None)
    if not drawing or drawing not in z.namelist():
        return [], []
    irels = _rels(z, drawing)
    root = ET.fromstring(z.read(drawing))
    placed, floating = [], []
    for anc in list(root):
        blip = anc.find(".//{%s}blip" % NS_A)
        if blip is None:
            continue
        emb = blip.get("{%s}embed" % NS_R)
        img = irels.get(emb)
        if not img:
            continue
        frm = anc.find("{%s}from" % NS_XDR)
        if frm is None:
            floating.append(img)          # absoluteAnchor：定位到页面不是单元格
            continue
        r = int(frm.find("{%s}row" % NS_XDR).text) + 1      # 锚点是 0 基，行号用 1 基
        c = int(frm.find("{%s}col" % NS_XDR).text)
        placed.append((r, c, img))
    return placed, floating


def _content_type(z, part):
    """从 [Content_Types].xml 取这个 part 的媒体类型。取不到就按扩展名回落。"""
    ext = os.path.splitext(part)[1].lower().lstrip(".")
    try:
        ct = ET.fromstring(z.read("[Content_Types].xml"))
        for d in ct.iter("{http://schemas.openxmlformats.org/package/2006/content-types}Default"):
            if (d.get("Extension") or "").lower() == ext:
                return d.get("ContentType")
    except (KeyError, ET.ParseError):
        pass
    return "image/%s" % ("jpeg" if ext in ("jpg", "jpeg") else ext or "png")


def attach_dir(inst, bill_no):
    """落盘目录：var/attachments/{领域id}/{实例id}/{单据号}/

    **刻意不放进 var/doclib 的托管区**（0034）：那是制度文件库，是规则的上游；
    单据附件是业务数据。混在一起之后，「规则是从哪堆资料里抽的」就再也说不清，
    而制度文件库的文件数、待抽取、溯源统计也会被业务单据稀释。
    """
    return os.path.join(config.ATTACH_DIR, inst["domain_id"], inst["instance_id"], bill_no)


def extract_xlsx(path, instance_id=None, dry_run=False):
    """从一份 xlsx 抽内嵌附件。**只读源文件**，落盘一律在 var/attachments 下。

    可反复重跑：同一张单同一份内容只留一条（靠 (bill_no, sha256) 唯一索引）。
    """
    from .. import doclib                      # 复用「当前实例」的唯一入口
    inst = doclib.resolve_instance(instance_id)

    if not os.path.isfile(path):
        raise ExtractError("文件不存在：%s" % path)
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ExtractError("打不开（不是有效的 xlsx）：%s" % path)

    with z:
        sheet_label, sheet_part = _find_sheet(z)
        ss = _shared_strings(z)
        bills = _bill_by_row(z, sheet_part, ss)
        placed, floating = _anchors(z, sheet_part)

        # 按单据分组，组内按（行，列）排 —— 列号就是 B 列往右的顺序
        by_bill = {}
        orphan_rows = []
        for r, c, img in sorted(placed, key=lambda t: (t[0], t[1])):
            bill = bills.get(r)
            if not bill:
                orphan_rows.append({"row": r, "image": img.split("/")[-1]})
                continue
            # 行号一并带着走。**不要事后拿图片路径反查行号**：
            # 同一张图可以被锚在多处（OOXML 允许多个锚点引用同一个 media），
            # 反查会取到第一个匹配，于是 source_ref 指向错误的行——
            # 而出处指错地方比没有出处更危险。
            by_bill.setdefault(bill, []).append((c, img, r))

        known = {r["bill_no"] for r in db.query(
            "SELECT bill_no FROM expense_bill WHERE bill_no = ANY(%s)",
            (list(by_bill),))} if by_bill else set()

        src_name = os.path.basename(path)
        per_bill, unknown_bills = [], []
        saved = skipped = 0
        for bill, imgs in sorted(by_bill.items()):
            if bill not in known:
                # 库里没有这张单。**不建单**：凭空造一张单比少一张附件危险得多。
                unknown_bills.append({"bill_no": bill, "images": len(imgs)})
                continue
            d = attach_dir(inst, bill)
            if not dry_run:
                os.makedirs(d, exist_ok=True)
            n_new = n_dup = 0
            for seq, (col, img, row_no) in enumerate(imgs, 1):
                data = z.read(img)
                sha = hashlib.sha256(data).hexdigest()
                ct = _content_type(z, img)
                ext = EXT_BY_TYPE.get(ct, os.path.splitext(img)[1] or ".png")
                fname = "%02d_%s%s" % (seq, os.path.splitext(os.path.basename(img))[0], ext)
                full = os.path.join(d, fname)
                ref = "%s · %s · 第%d行 · %s" % (src_name, sheet_label, row_no, img)
                if dry_run:
                    n_new += 1
                    continue
                # 先查重再写盘：唯一索引在 (bill_no, sha256) 上，
                # 重跑时不该白写一遍磁盘再让数据库拒绝。
                if db.scalar("SELECT 1 FROM attachment WHERE bill_no=%s AND sha256=%s",
                             (bill, sha)):
                    n_dup += 1
                    continue
                with open(full, "wb") as f:
                    f.write(data)
                db.execute("""
                    INSERT INTO attachment(bill_no, file_name, stored_path, seq, sha256,
                                           byte_size, content_type, source_kind, source_ref)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'xlsx内嵌',%s)
                    ON CONFLICT DO NOTHING""",
                    (bill, fname, full, seq, sha, len(data), ct, ref))
                n_new += 1
            saved += n_new
            skipped += n_dup
            per_bill.append({"bill_no": bill, "images": len(imgs),
                             "new": n_new, "already": n_dup, "dir": d})

        # 实际附件数回写。**与 attach_pages（申报张数）并存，不覆盖它**——
        # 实测 34/40 两者不等，而那不等于「申报不实」（见迁移 0035 的列注释）。
        if not dry_run:
            for p in per_bill:
                db.execute("""UPDATE expense_bill SET attach_count =
                                (SELECT count(*) FROM attachment a
                                  WHERE a.bill_no = expense_bill.bill_no
                                    AND a.source_kind = 'xlsx内嵌')
                              WHERE bill_no = %s""", (p["bill_no"],))

    return {
        "file": src_name, "sheet": sheet_label, "dry_run": bool(dry_run),
        "instance_id": inst["instance_id"], "instance_label": inst["instance_label"],
        "rows_with_bill": len(bills) - 1 if bills else 0,   # 减掉表头那行
        "images_anchored": len(placed),
        "bills": len(by_bill),
        "saved": saved, "already": skipped,
        "per_bill": per_bill,
        "unknown_bills": unknown_bills,
        "orphan_rows": orphan_rows,
        "floating_images": len(floating),
        "note": "附件类型（attach_type）**留空，没有判**：从一张 PNG 判它是出差审批单"
                "还是住宿费发票必须读图（必要附件决策表 11 种类型里 5 种 ocr_needed）。"
                "所以这一步解除的是「有没有附件」，**不是** TR-D6-01（必要附件完整性）。",
        "warnings": _warn(unknown_bills, orphan_rows, floating),
    }


def _warn(unknown_bills, orphan_rows, floating):
    """对不上的都要说出来。静默跳过 = 事后没人说得清差额在哪。"""
    w = []
    if unknown_bills:
        w.append("有 %d 个单据号在 expense_bill 里找不到，这些附件**没有入库**（%s）。"
                 "不为它们建单：凭空造一张单比少一张附件危险得多"
                 % (len(unknown_bills),
                    "、".join(u["bill_no"] for u in unknown_bills[:5])))
    if orphan_rows:
        w.append("有 %d 张图锚在没有单据号的行上，不知道属于哪张单，**没有入库**"
                 % len(orphan_rows))
    if floating:
        w.append("有 %d 张图是浮动锚点（absoluteAnchor，定位到页面而不是单元格），"
                 "落不到行上，**没有入库**" % floating)
    return w


def status(instance_id=None):
    """抽了多少、还差多少、和申报张数差多少。页面与接口都读这一份。"""
    rows = db.query("""
        SELECT b.bill_no, b.is_demo, b.attach_pages, b.attach_count,
               count(a.id) FILTER (WHERE a.source_kind = 'xlsx内嵌') AS extracted,
               count(a.id) FILTER (WHERE a.source_kind = '演示合成') AS demo_rows
          FROM expense_bill b LEFT JOIN attachment a ON a.bill_no = b.bill_no
         GROUP BY 1,2,3,4 ORDER BY b.bill_no""")
    real = [r for r in rows if not r["is_demo"]]
    with_att = [r for r in real if r["extracted"]]
    # 申报张数 vs 实际附件数。**两个指标并存**，这里只如实并列，不判谁对。
    cmp_rows = [{"bill_no": r["bill_no"], "declared": r["attach_pages"],
                 "actual": r["extracted"],
                 "delta": (r["attach_pages"] - r["extracted"])
                          if r["attach_pages"] is not None else None}
                for r in with_att]
    diff = [c for c in cmp_rows if c["delta"] not in (None, 0)]
    # 磁盘上有文件、库里却没有登记 = 有人重跑过真实样本装载器。
    # app/demo/seed.py 的 clear() 会 **DELETE 整张 attachment 表**（它和
    # expense_bill 等十几张表一起被清掉重建），而磁盘上的图不会跟着删。
    # **这不是故障，是需要重跑一次抽取**——但如果不报出来，
    # 页面上会显示「40 张单全都没有附件」，看着和缺口 G-29 一模一样，
    # 而那个结论刚刚才被推翻过。
    on_disk = 0
    if os.path.isdir(config.ATTACH_DIR):
        for _, _, fs in os.walk(config.ATTACH_DIR):
            on_disk += len(fs)
    in_db = sum(r["extracted"] for r in real)
    typed = db.scalar("SELECT count(*) FROM attachment "
                      "WHERE source_kind='xlsx内嵌' AND attach_type IS NOT NULL") or 0
    untyped = db.scalar("SELECT count(*) FROM attachment "
                        "WHERE source_kind='xlsx内嵌' AND attach_type IS NULL") or 0

    return {
        "real_bills": len(real),
        "real_bills_with_attachment": len(with_att),
        "extracted_total": in_db,
        "files_on_disk": on_disk,
        "orphan_files": max(0, on_disk - in_db),
        "orphan_note": (
            "磁盘上有 %d 个附件文件，库里只有 %d 条登记——**差 %d 条**。"
            "最常见的原因是重跑过真实样本装载器（app/demo/seed.py 的 clear() 会清空 "
            "attachment 表，磁盘上的图不跟着删）。**重跑一次抽取即可**，它是幂等的。"
            "不要据此判断「客户没给附件」——那个结论 2026-10-21 已经被推翻过一次。"
            % (on_disk, in_db, on_disk - in_db)) if on_disk > in_db else None,
        "demo_rows": sum(r["demo_rows"] for r in rows),
        "declared_vs_actual": cmp_rows,
        "mismatch_count": len(diff),
        "declared_more": sum(1 for c in diff if c["delta"] > 0),
        "declared_less": sum(1 for c in diff if c["delta"] < 0),
        "typed": typed, "untyped": untyped,
        "dir": config.ATTACH_DIR,
        "note": "**申报张数（attach_pages，来自审批意见 PDF）与实际附件数是两个指标**，"
                "实测 34/40 张单两者不等。这不等于「申报不实」：「张数」可能按纸质页数算、"
                "一张图可能是两页合并、申报数也可能含未扫描的原件。"
                "在口径搞清楚之前不做成规则——那会是一条 32/40 命中的误报机。",
        # **这条必须随 typed/untyped 动态变化，不能是写死的固定文案。**
        # 实测踩过：先写成「附件类型全部为空」，等 app/perception/attachment_classify.py
        # 真的判出几条之后，这句话就成了假话——而假话比没有这句话更危险，
        # 它会让人以为「一条都没判过」从而跳过去核实已经判出来的那些。
        "gate_note": (
            "附件类型全部为空（一条都没判），所以 **TR-D6-01（必要附件完整性）"
            "仍然不能开**：它要的是类型，不是张数。类型要读图才知道，"
            "见 /api/attachments/classify。"
            if typed == 0 else
            "附件类型已判 %d / %d 条（还差 %d 条），**TR-D6-01 仍然不能开**——"
            "它要求**一张单的全部必要附件都判出类型**才能判断「齐不齐」，"
            "只判了一部分的单据，缺的那几张到底是「真的没附件」还是「附件还没判」"
            "分不清，开了等于对未判完的单报缺件" % (typed, typed + untyped, untyped)),
    }
