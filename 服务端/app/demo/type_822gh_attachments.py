# -*- coding: utf-8 -*-
r"""822港航 附件的**确定性定类**：能算出来的不去读图。

用法（**不要加 -I**）：

    cd 服务端
    python -m app.demo.type_822gh_attachments          # 干跑
    WRITE=1 python -m app.demo.type_822gh_attachments  # 真写库（可重入）

147 个附件里有一批的类型是**算得出来的**，不需要发给读图模型：

  · 文件名是「发票附件-<发票号>.pdf」，而那个发票号已经解析进 invoice 表
    （PDF 文字层，零模型），票面类型与项目明细都在库里
  · 文件名是「港航本部出差审批流程*.pdf」「出差申请单*.pdf」

type_source 记成 `文字层解析`（迁移 0055 新增的取值），
**不记成「读图判定」**——事后问「哪些附件的类型是模型判的、该不该复核」，
记错了答案就全错，而这条链路的全部价值就在于模型判的结论能被挑出来复核。

两条纪律（照抄 attachment_classify 的）：

1. **算不出就留空，不猜。** 文件名看不出来的一律不填，交给读图那一批。
   一个错的类型比空值危险：空值让规则报「判不出」，错类型让规则给出
   一个看着自信、实则错误的结论。

2. **类型必须落在 EN17 枚举里。** attach_type 有外键
   (instance_id, type_enum, attach_type) → enum_value_current，
   而 **822港航 的本体里 EN17 是空的**（没有客户自己的附件类型词表）。
   所以这个程序先把 EN17 建起来——取值用**福建海运集团那 12 个**，
   因为附件类型是「报销要交哪几种凭证」的分类，两家的报销制度同源
   （都在福建港口集团下、同一套共享中心口径）；
   而且它和 TYPES 表（A01..A15 → EN17）是配套的，换一套词表就得改那张表。
   这一条是**我定的口径**，不是从 822港航 材料里读出来的，所以写在这里
   而不是埋在代码里：客户要是说「我们的附件分类不是这 12 类」，改这里。
"""
import io
import os
import re
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

from app import config, db                                      # noqa: E402
from app.perception.attachment_classify import TYPES            # noqa: E402

IID = "822gh"
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")
SRC = "文字层解析"

out = []


def p(s=""):
    out.append(str(s))


def type_of(att, inv_by_no):
    """这个附件的类型算不算得出来。返回 (细类编码, 依据) 或 (None, 原因)。"""
    fn = att["file_name"] or ""
    # ① 出差审批单 / 出差申请单：文件名直说
    if re.search(r"出差审批|出差申请|港航本部出差审批流程", fn):
        return "A01", "文件名含「出差审批/出差申请」"
    # ② 发票：文件名里的发票号能对上已解析的 invoice
    m = re.search(r"(\d{8,24})", fn)
    inv = inv_by_no.get(m.group(1)) if m else None
    if inv:
        d = inv["detail_text"] or ""
        if inv["inv_type"] == "铁路电子客票":
            return "A03", "已解析为铁路电子客票（交通费凭证）"
        if "住宿" in d:
            return "A04", "已解析为住宿服务发票（项目明细：%s）" % d[:18]
        if "机票" in d or "客票" in d or "车票" in d:
            return "A03", "已解析为交通类发票（项目明细：%s）" % d[:18]
        # 餐饮、会务、其他服务：都属于「其他差旅费发票」这一类
        return "A05", "已解析为发票但非住宿非交通（项目明细：%s）" % (d[:18] or "空")
    return None, "文件名看不出类型，且对不上已解析的发票号"


# ── 读数据 ────────────────────────────────────────────────────────────────
atts = db.query("""SELECT id, bill_no, file_name, content_type, attach_type,
                          type_code, type_source
                     FROM attachment WHERE instance_id = %s
                    ORDER BY bill_no, seq""", (IID,))
invs = db.query("""SELECT inv_no, inv_type, detail_text FROM invoice
                    WHERE instance_id = %s""", (IID,))
inv_by_no = {i["inv_no"]: i for i in invs}

decided, undecided = [], []
for a in atts:
    code, why = type_of(a, inv_by_no)
    (decided if code else undecided).append((a, code, why))

p("=== 确定性定类 ===")
p("  附件 %d 个：算得出类型的 %d 个，算不出的 %d 个（交给读图）"
  % (len(atts), len(decided), len(undecided)))
by_code = {}
for a, code, why in decided:
    by_code.setdefault(code, []).append(why)
for code in sorted(by_code):
    p("  %s %-14s %d 个    例：%s"
      % (code, TYPES[code][0], len(by_code[code]), by_code[code][0][:40]))
p("")
p("=== 算不出类型的（这些才要读图）===")
p("  共 %d 个；按扩展名：%s"
  % (len(undecided),
     {k: sum(1 for a, _, _ in undecided if (a["content_type"] or "") == k)
      for k in sorted({a["content_type"] or "?" for a, _, _ in undecided})}))
for a, _, _ in undecided[:10]:
    p("    · %-46s %s" % (a["file_name"][:46], a["bill_no"]))
if len(undecided) > 10:
    p("    …… 还有 %d 个" % (len(undecided) - 10))

# ── EN17 闸：类型写不进去的话，上面算得再对也没用 ──────────────────────────
need_en17 = sorted({TYPES[c][1] for c in by_code})
have = {r["value"] for r in db.query(
    "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id='EN17'",
    (IID,))}
p("")
p("=== EN17 附件类型枚举 ===")
p("  本体里现有 %d 个；这批要用到 %d 个；缺 %s"
  % (len(have), len(need_en17), sorted(set(need_en17) - have) or "无"))
if set(need_en17) - have:
    p("  **缺值要先补进本体 YAML 再装载**，attach_type 有外键，缺一个就整条拒绝。")
    p("  （取值用福建海运集团那 12 个，理由见本文件头口径 2——这是我定的口径）")

if os.environ.get("WRITE") == "1":
    if set(need_en17) - have:
        raise SystemExit("EN17 缺值，先补本体再跑：%s" % sorted(set(need_en17) - have))
    with db.tx() as conn:
        cur = conn.cursor()
        n = 0
        for a, code, why in decided:
            cur.execute("""UPDATE attachment
                              SET attach_type = %s, type_code = %s,
                                  type_source = %s, type_conf = '高',
                                  typed_at = now()
                            WHERE id = %s AND instance_id = %s""",
                        (TYPES[code][1], code, SRC, a["id"], IID))
            n += cur.rowcount
    p("")
    p("**已入库**")
    p("  定类 %d 个" % n)
    for r in db.query("""SELECT type_source, type_code, count(*) n FROM attachment
                          WHERE instance_id=%s AND attach_type IS NOT NULL
                          GROUP BY 1,2 ORDER BY 3 DESC""", (IID,)):
        p("  %-10s %-5s %d" % (r["type_source"], r["type_code"], r["n"]))
    p("  仍未定类（待读图）: %s"
      % db.scalar("""SELECT count(*) FROM attachment
                      WHERE instance_id=%s AND attach_type IS NULL""", (IID,)))
    p("  fjhy 未被动: %s 个已定类"
      % db.scalar("""SELECT count(*) FROM attachment
                      WHERE instance_id='fjhy' AND attach_type IS NOT NULL"""))
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "type822.txt"), "w",
        encoding="utf-8").write("\n".join(out))
print("ok: 算得出 %d / 算不出 %d" % (len(decided), len(undecided)))
