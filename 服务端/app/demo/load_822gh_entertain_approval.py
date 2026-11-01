# -*- coding: utf-8 -*-
r"""业务接待审批单（A16）与会议/培训通知里**有文字层**的那几份 → 库。

用法（**不要加 -I**，理由同 load_822gh_bills）：

    cd 服务端
    python -m app.demo.load_822gh_entertain_approval          # 干跑
    WRITE2=1 python -m app.demo.load_822gh_entertain_approval # 真写库（可重入）

【范围：只装文字层，一次模型调用都不花】
  业务接待审批单 29 份：**2 份 PDF 有完整文字层**、26 份 JPG、1 份扫描 PDF。
  会议/培训通知   8 份：**1 份 PDF 有文字层**、7 份扫描。
  剩下的 33 份要读图，是下一批。表结构两批共用（见 migration 0057）。

  **先看文件再估工作量。** 我在客户文档里写过「附件解析要调模型、
  放到最后做」，那句话对有文字层的 PDF 是错的——388 个 PDF 零模型调用
  就解出了发票 53 / 出差审批单 15。这里又多了 3 份。

【写入三张表】
  entertain_approval  审批单票面字段（migration 0057 新建）
  approval_record     审批节点与时间戳（本实例原来 0 行，fjhy 388 行不动）
  meeting_notice      通知上的活动起止与地点（原来全库 0 行）

【approval_record 的配对靠「节点标签数 == 时间戳数」这道闸】
  票面上节点标签和签批人是按坐标错开的：
      发起节点 经营发展部/朱**2026-04-20 15:05:42
      部门负责人 同意。
      经营发展部/檀**2026-04-20 15:06:32
  标签有时和签批人同一行、有时隔一行，所以按**文档顺序**各收一串再配对。
  两串长度不等就整份跳过并报出来——**不许按位置硬凑**：
  凑错一位，「分管领导什么时候审的」就挂到了部门负责人头上，
  而 TR-接待-01 判的正是「最后一个审批时间 vs 接待日期」。

【接待对象不进 fact】
  guest_roster 入库但 app/perception/reader.py 不取（见 0057 的列注释）。
  这个程序的干跑输出里**也不打印它**——输出文件会进 var/ 和复核材料。
"""
import io
import os
import re
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

os.environ.pop("WRITE", None)   # 见 fix_822gh_trip_fields 文件头：这是闸

from app import config, db                                       # noqa: E402
from app.demo.load_822gh_attachments import pdf_lines            # noqa: E402

IID = "822gh"
WRITE = os.environ.get("WRITE2") == "1"
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")

out = []


def p(s=""):
    out.append(str(s))


# ─────────────────────────────────────────────────────────────────────────────
# 业务接待审批单
# ─────────────────────────────────────────────────────────────────────────────
A16_LABELS = ["接待部门", "接待类型", "接待对象", "接待事由", "接待时间",
              "活动安排", "时间", "内容", "用餐安排", "场所", "标准", "人数",
              "工作餐/简餐安排", "用车安排", "住宿安排", "赠送纪念品",
              "预算总额", "其中", "附件"]
# 审批节点标签，顺序即票面上的流转顺序
A16_NODES = ["发起节点", "部门负责人", "分管领导", "主管领导"]

RE_TS = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
# 「<部门>/<姓名><时间戳>」黏在一起。姓名那一段**不取**（进不了库也不打印）：
# 签批人姓名在 approval_record.approver 里是既有口径（fjhy 388 行就是这么存的），
# 所以这里取，但干跑输出不打印。
RE_SIGN = re.compile(r"([^/\s]+)/(\S+?)(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
RE_NUM = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")


def _num(s):
    m = RE_NUM.search(str(s or "").replace(",", ""))
    return float(m.group(1)) if m else None


def _int(s):
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None


def parse_a16(lines):
    """业务接待审批单 → 票面字段 + 审批节点。不是这个版式就返回 None。"""
    flat = [x.strip() for x in lines if x and x.strip()]
    if not any("业务接待审批单" in x for x in flat[:4]):
        return None

    # 票面字段：标签—值，值可能在同一行标签后面、也可能在下一行。
    # **数值那三格要多走一步**：票面是「预算总额 元」+ 换行 +「6,000.00」,
    # 标签行后面跟的是**单位**不是数值。只看同一行会取到「元」——
    # 而「元」不为空，于是不去看下一行，预算总额永远是 None。
    # 空值兜底和「取到了但不是数」是两种情况，第一版只处理了前一种。
    numeric = {"标准", "人数", "预算总额"}
    val = {}
    for i, ln in enumerate(flat):
        for lab in A16_LABELS:
            if not ln.startswith(lab):
                continue
            rest = ln[len(lab):].lstrip("：: ").strip()
            need_more = (not rest) or (lab in numeric
                                       and not re.search(r"\d", rest))
            if need_more and i + 1 < len(flat):
                rest = flat[i + 1].strip()
            val.setdefault(lab, rest)
    # 「用餐安排 时间 2026-04-22 18:30」——用餐那一行里的时间要单独摘
    meal_line = next((x for x in flat if x.startswith("用餐安排")), "")
    m_meal = re.search(r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)", meal_line)

    # 审批节点：标签一串、签批一串，各按文档顺序收，再配对
    nodes = [x for ln in flat for x in A16_NODES if ln.startswith(x)]
    signs = []
    for ln in flat:
        for dept, who, ts in RE_SIGN.findall(ln):
            signs.append((dept, who, ts))
    return {
        "dept": val.get("接待部门") or None,
        "entertain_type": val.get("接待类型") or None,
        "guest_roster": val.get("接待对象") or None,
        "reason": val.get("接待事由") or None,
        "entertain_at": val.get("接待时间") or None,
        "meal_at": m_meal.group(1) if m_meal else None,
        "meal_place": val.get("场所") or None,
        "meal_standard": _num(val.get("标准")),
        "meal_persons": _int(val.get("人数")),
        "work_meal": val.get("工作餐/简餐安排") or None,
        "car_arrange": val.get("用车安排") or None,
        "lodging_arrange": val.get("住宿安排") or None,
        "gift_arrange": val.get("赠送纪念品") or None,
        "budget_total": _num(val.get("预算总额")),
        "nodes": nodes,
        "signs": signs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 会议/培训通知
# ─────────────────────────────────────────────────────────────────────────────
# 通知正文里那句**自报活动起止**的话：「定于8 月22 日至28 日……举办」。
# 数字里夹着空格（PDF 的字距），所以每一处都允许 \s*。
#
# **这个正则只在 1 份文件上验过**（8 份通知里 7 份是扫描件）。
# 所以它是个下限不是通用解析器：匹配不上就留 NULL（规则那边会 pending），
# 而匹配上之后还要过三道闸（见 parse_notice）——
# 匹配错一个日期比取不到更坏，它会让 TR-差旅-30 对正常单据误报。
RE_NOTICE_SPAN = re.compile(
    r"定\s*于\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*至\s*"
    r"(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日")
RE_NOTICE_YEAR = re.compile(r"〔\s*(\d{4})\s*〕")
RE_NOTICE_PLACE = re.compile(r"日\s*在\s*(\S{4,30}?)\s*举\s*办")


def parse_notice(lines):
    import datetime
    txt = re.sub(r"\s+", " ", " ".join(x.strip() for x in lines if x))
    ms = RE_NOTICE_SPAN.search(txt)
    my = RE_NOTICE_YEAR.search(txt)
    if not ms or not my:
        return None
    y = int(my.group(1))
    m1, d1 = int(ms.group(1)), int(ms.group(2))
    m2 = int(ms.group(3)) if ms.group(3) else m1
    d2 = int(ms.group(4))
    try:
        a = datetime.date(y, m1, d1)
        b = datetime.date(y, m2, d2)
    except ValueError:
        return None
    # 三道闸：起 ≤ 止、跨度 ≤ 31 天、年份是文号里那个年份。
    # 过不了就返回 None 留 NULL——**宁可取不到，不许取错**。
    if a > b or (b - a).days > 31:
        return None
    mp = RE_NOTICE_PLACE.search(txt)
    return {"notice_start": a.isoformat(), "notice_end": b.isoformat(),
            "notice_place": (mp.group(1) if mp else None)}


# ─────────────────────────────────────────────────────────────────────────────
# 跑
# ─────────────────────────────────────────────────────────────────────────────
def main():
    rows = db.query("""SELECT bill_no, attach_type, file_name, stored_path
                         FROM attachment
                        WHERE instance_id = %s
                          AND content_type = 'application/pdf'
                          AND attach_type IN ('业务接待审批单',
                                              '会议或培训通知/报名回执')
                        ORDER BY attach_type, bill_no, file_name""", (IID,))
    p("=== 候选（已定类 + 是 PDF）%d 份 ===" % len(rows))

    a16s, notices, skipped = [], [], []
    for r in rows:
        try:
            lines = pdf_lines(open(r["stored_path"], "rb").read())
        except Exception as e:                        # noqa: BLE001
            skipped.append((r["file_name"], "打不开：%s" % e))
            continue
        if not lines:
            skipped.append((r["file_name"], "文字层为空（扫描件，等读图）"))
            continue
        if r["attach_type"] == "业务接待审批单":
            t = parse_a16(lines)
            if not t:
                skipped.append((r["file_name"], "不是业务接待审批单版式"))
                continue
            if len(t["nodes"]) != len(t["signs"]):
                skipped.append((r["file_name"],
                                "节点标签 %d 个、签批 %d 条，配不上——不硬凑"
                                % (len(t["nodes"]), len(t["signs"]))))
                continue
            m = (re.search(r"\((\d{8,24})\)", r["file_name"])
                 or re.search(r"(\d{16,24})", r["file_name"]))
            t["app_no"] = m.group(1) if m else ("%s-A16" % r["bill_no"])
            t["bill_no"] = r["bill_no"]
            t["src"] = r["file_name"]
            a16s.append(t)
        else:
            n = parse_notice(lines)
            if not n:
                skipped.append((r["file_name"], "正文里没有「定于X月X日至X日」"
                                                "那句自报活动起止的话"))
                continue
            n["bill_no"] = r["bill_no"]
            n["src"] = r["file_name"]
            notices.append(n)

    p("")
    p("=== 业务接待审批单 解出 %d 份 ===" % len(a16s))
    for t in a16s:
        # **不打印 guest_roster**（接待对象是外部人员姓名+职务的自由文本）
        p("  %s  流程号 %s" % (t["bill_no"], t["app_no"]))
        p("     接待部门=%s 接待类型=%s" % (t["dept"], t["entertain_type"]))
        p("     接待时间=%s  用餐时间=%s 场所=%s"
          % (t["entertain_at"], t["meal_at"], t["meal_place"]))
        p("     标准=%s 人数=%s 预算总额=%s"
          % (t["meal_standard"], t["meal_persons"], t["budget_total"]))
        p("     工作餐=%s 用车=%s 住宿=%s 纪念品=%s"
          % (t["work_meal"], t["car_arrange"], t["lodging_arrange"],
             t["gift_arrange"]))
        p("     审批节点 %d 个：%s" % (len(t["nodes"]), t["nodes"]))
        p("     审批时间戳：%s" % [s[2] for s in t["signs"]])
        last = max(s[2] for s in t["signs"]) if t["signs"] else None
        p("     最后审批 %s  vs 接待时间 %s  → 按日期%s"
          % (last, t["entertain_at"],
             ("审批不晚于接待" if last and t["entertain_at"]
              and last[:10] <= t["entertain_at"][:10] else "**审批晚于接待**")))
    p("")
    p("=== 会议/培训通知 解出 %d 份 ===" % len(notices))
    for n in notices:
        p("  %s  活动 %s~%s  地点=%s  （%s）"
          % (n["bill_no"], n["notice_start"], n["notice_end"],
             n["notice_place"], n["src"]))
    p("")
    p("=== 没解出来的 %d 份（等读图那一批）===" % len(skipped))
    for fn, why in skipped:
        p("  · %s —— %s" % (fn, why))

    if WRITE:
        with db.tx() as conn:
            c = conn.cursor()
            n_att_before = db.scalar("SELECT count(*) FROM attachment")
            # **删除作用域只到「文字层解出来的那几份」。**
            # 原来这三句是 `WHERE instance_id=%s`，也就是「把本实例的全删了
            # 重来」。那时候这三张表里只有本程序写的行，所以没出事；
            # 读图那一批（attachment_extract_fields，extracted_from='审批单读图'）
            # 入库之后，再跑一次本程序就会把 27 份读图的成果整批删掉——
            # 而本程序是「可重入」的，重跑是预期动作，不是误操作。
            # entertain_approval / approval_record 靠 extracted_from 区分
            # （后者的这一列是 migration 0060 加的，就是为这件事）。
            c.execute("""DELETE FROM entertain_approval
                          WHERE instance_id=%s AND extracted_from=%s""",
                      (IID, "审批单文字层"))
            # `OR extracted_from IS NULL` 是为了 0060 之前本程序自己写下的
            # 那 8 行——它们没有这一列可打标，光按 extracted_from 删删不到，
            # 于是重跑一次就插成双份（实测：8 行变 16 行）。
            # **flow='业务接待审批' 这个条件必须在**：报账流程的流水
            # （fjhy 388 行，flow IS NULL）也满足 extracted_from IS NULL，
            # 少了它这一句会把别家的审批记录删光。
            c.execute("""DELETE FROM approval_record
                          WHERE instance_id=%s AND flow=%s
                            AND (extracted_from=%s OR extracted_from IS NULL)""",
                      (IID, "业务接待审批", "审批单文字层"))
            # meeting_notice 没有 extracted_from 列，而它是 UNIQUE(bill_no)、
            # 读图与文字层会写进**同一行**（按字段 COALESCE 合并）。
            # 所以这里不删整行，只把本程序负责的三列清掉再重写——
            # 清整行会连带删掉读图填的 meal_provided / shuttle。
            c.execute("""UPDATE meeting_notice
                            SET notice_start=NULL, notice_end=NULL, notice_place=NULL
                          WHERE instance_id=%s""", (IID,))
            n_ap = 0
            for t in a16s:
                c.execute("""INSERT INTO entertain_approval(app_no, bill_no, dept,
                                 entertain_type, reason, guest_roster,
                                 entertain_at, meal_at, meal_place,
                                 meal_standard, meal_persons, work_meal,
                                 car_arrange, lodging_arrange, gift_arrange,
                                 budget_total, extracted_from, instance_id)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                     %s,%s,'审批单文字层',%s)""",
                          (t["app_no"], t["bill_no"], t["dept"],
                           t["entertain_type"], t["reason"], t["guest_roster"],
                           t["entertain_at"], t["meal_at"], t["meal_place"],
                           t["meal_standard"], t["meal_persons"],
                           t["work_meal"], t["car_arrange"],
                           t["lodging_arrange"], t["gift_arrange"],
                           t["budget_total"], IID))
                for seq, (node, (_dept, who, ts)) in enumerate(
                        zip(t["nodes"], t["signs"]), start=1):
                    # flow 必须写死「业务接待审批」（迁移 0059）：
                    # 这张表原本装的是**报账流程**的审批记录，两条流程都有
                    # 「分管领导」这个角色名。不标流程，报账流程本体页上的
                    # 「实测次数」会把这几条算进去——而那 29 张报账单
                    # 一次都没被分管领导审过。
                    # app_no / extracted_from 是 migration 0060 加的两列：
                    # 前者让「一张单挂多份审批单」时能按份定位，
                    # 后者让本程序的 DELETE 不碰读图那一批。
                    c.execute("""INSERT INTO approval_record(bill_no, seq, node,
                                     approver, opinion, approved_at, flow,
                                     app_no, extracted_from, instance_id)
                                 VALUES (%s,%s,%s,%s,%s,%s,'业务接待审批',
                                         %s,'审批单文字层',%s)""",
                              (t["bill_no"], seq, node, who, None, ts,
                               t["app_no"], IID))
                    n_ap += 1
            for n in notices:
                # UNIQUE(bill_no)：读图那一批可能已经为同一张单建了行
                # （带 meal_provided / shuttle）。所以是 UPSERT 而不是 INSERT，
                # 而且**只覆盖本程序负责的三列**，不动另一批填的字段。
                c.execute("""INSERT INTO meeting_notice(bill_no, notice_start,
                                 notice_end, notice_place, instance_id)
                             VALUES (%s,%s,%s,%s,%s)
                             ON CONFLICT (bill_no) DO UPDATE SET
                                 notice_start=EXCLUDED.notice_start,
                                 notice_end=EXCLUDED.notice_end,
                                 notice_place=EXCLUDED.notice_place""",
                          (n["bill_no"], n["notice_start"], n["notice_end"],
                           n["notice_place"], IID))
            c.execute("SELECT count(*) FROM attachment")
            if c.fetchone()[0] != n_att_before:
                raise SystemExit("中止：attachment 行数变了，本程序不该碰它")
        p("")
        p("**已入库**")
        for lab, q in (
                ("业务接待审批单", "SELECT count(*) FROM entertain_approval "
                              "WHERE instance_id=%s"),
                ("审批流水", "SELECT count(*) FROM approval_record "
                         "WHERE instance_id=%s"),
                ("会议/培训通知", "SELECT count(*) FROM meeting_notice "
                            "WHERE instance_id=%s")):
            p("  %s: %s" % (lab, db.scalar(q, (IID,))))
        p("  别家没被动：fjhy 审批流水 %s 行"
          % db.scalar("SELECT count(*) FROM approval_record "
                      "WHERE instance_id='fjhy'"))
    else:
        p("")
        p("（干跑，没写库。设 WRITE2=1 才写）")

    os.makedirs(WORK, exist_ok=True)
    io.open(os.path.join(WORK, "a16_822.txt"), "w",
            encoding="utf-8").write("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
