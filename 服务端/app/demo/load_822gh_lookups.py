# -*- coding: utf-8 -*-
r"""822港航 的两张客户参照表 → lookup_table。

用法（**不要加 -I**）：

    cd 服务端
    python -m app.demo.load_822gh_lookups          # 干跑
    WRITE=1 python -m app.demo.load_822gh_lookups  # 真写库（可重入）

源（都在 材料\822港航、单据、数据\ 下，客户已交付）：
  业务分类对照表(2).xlsx          **16** 类业务大类 + 释义
    （之前报过「17 类」，那是把表头行也数进去了。这个错数已经写进了
     两份客户文档，下次改文档要跟着改。）
  附件1：公司部门数据.xls          12 个部门（编码/名称/属性/级别/负责人）
  附件3：账套及财务组织数据收集.xls  核算主体 + 财务组织编码

几条口径：

1. **这两张表进 lookup_table，不进枚举、不进 org。**
   · 业务大类：本体里的 EN03 记的是**本实例 29 张单上实际出现过的 4 个值**
     （见本体 YAML 里那段「取值全部来自自己那 29 张真实单据」）。
     把对照表的 17 类塞进 EN03，就把「实际出现过的」变成了「客户的全量
     清单」，两种语义混在一列。对照表是**参照数据**，进 lookup_table。
   · 部门：org 这张表装的是**法人/核算主体**（fjhy 的 10 行全是公司），
     而这 12 个是部门。装载程序里早写着「经办部门是部门不是法人主体，
     不能当 org」，这里照这条走。

2. **部门负责人的姓名要进脱敏闭集合。**
   部门数据表里有 10 个部门负责人姓名。它们落在 lookup_row.row_value 里，
   而闭集合是从 employee.name 等几列取的，取不到这里——
   所以入库前先核一遍：这 10 个人必须都在员工表里（实测都在）。
   **有一个不在就停**，不能让一个真名躺在一张没人守的表里。

3. **账套/核算主体只有 1 条，不单独建表。**
   附件3 的核算主体就是本实例那家公司（org 里已有 ORG-822GH），
   为一行数据建一张参照表没有意义，把财务组织编码记进部门表的 row_value。
"""
import io
import json
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

import openpyxl                                                 # noqa: E402
import xlrd                                                     # noqa: E402

from app import config, db                                      # noqa: E402

IID = "822gh"
ROOT = os.path.join(config.PROJECT_ROOT, "材料", "822港航、单据、数据")
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")
T_BIZ = "业务大类对照表"
T_DEPT = "部门数据表"

out = []


def p(s=""):
    out.append(str(s))


def _s(v):
    t = str(v or "").strip()
    return t or None


# ── 业务大类对照表 ──────────────────────────────────────────────────────────
def read_biz():
    path = os.path.join(ROOT, "业务分类对照表(2).xlsx")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(wb["业务大类一览"].iter_rows(values_only=True))
    finally:
        wb.close()
    hdr = [_s(c) or "" for c in rows[0]]
    assert hdr[:3] == ["序号", "业务大类", "大类释义"], "表头变了：%s" % hdr
    out_ = []
    for r in rows[1:]:
        name, note = _s(r[1]), _s(r[2])
        if name:
            out_.append({"name": name, "note": note, "seq": _s(r[0])})
    return out_


# ── 部门数据表 ──────────────────────────────────────────────────────────────
def read_dept():
    """附件1 的「部门数据收集表（填报）」+ 附件3 的财务组织编码。

    编码在 xls 里是浮点（822.0 / 8220001.0），**要还原成整数字符串**——
    直接 str() 会得到 '8220001.0'，而客户系统里的编码是 '8220001'，
    对不上就白导了。
    """
    def code(v):
        t = _s(v)
        if t and t.replace(".", "", 1).isdigit():
            return str(int(float(t)))
        return t

    wb = xlrd.open_workbook(os.path.join(ROOT, "附件1：公司部门数据.xls"))
    sh = wb.sheet_by_name("部门数据收集表（填报）")
    hdr = [_s(sh.cell_value(0, c)) or "" for c in range(sh.ncols)]
    assert "*编码" in hdr and "*名称(中)" in hdr, "部门表表头变了：%s" % hdr
    ix = {h: i for i, h in enumerate(hdr)}
    depts = []
    for r in range(1, sh.nrows):
        row = [sh.cell_value(r, c) for c in range(sh.ncols)]
        c_, nm = code(row[ix["*编码"]]), _s(row[ix["*名称(中)"]])
        if not c_ or not nm:
            continue
        depts.append({
            "code": c_, "name": nm,
            "parent": code(row[ix["*上级"]]),
            "attr": _s(row[ix["*部门属性"]]),
            "level": _s(row[ix["*级别"]]),
            "head": _s(row[ix["*部门负责人"]]),
        })
    # 附件3 的财务组织编码（822_01 这种），按部门名对上去
    fin = {}
    wb3 = xlrd.open_workbook(os.path.join(ROOT, "附件3：账套及财务组织数据收集.xls"))
    sh3 = wb3.sheet_by_name("财务组织数据收集表（填报）")
    h3 = [_s(sh3.cell_value(0, c)) or "" for c in range(sh3.ncols)]
    i3 = {h: i for i, h in enumerate(h3)}
    for r in range(1, sh3.nrows):
        row = [sh3.cell_value(r, c) for c in range(sh3.ncols)]
        # 财务组织编码同样要还原成整数字符串：核算主体那一行是数值 822.0，
        # 直接 str() 会得到 '822.0'，而客户系统里是 '822'。
        # 部门那几行是 '822_01' 这种文本，code() 原样返回。
        nm, fc = _s(row[i3["*名称（中）"]]), code(row[i3["*编码"]])
        if nm and fc:
            fin[nm] = fc
    for d in depts:
        d["fin_code"] = fin.get(d["name"])
    return depts


biz = read_biz()
depts = read_dept()

p("=== 业务大类对照表 ===")
p("  %d 类" % len(biz))
for b in biz[:5]:
    p("    %-14s %s" % (b["name"], (b["note"] or "")[:46]))
p("    ……")
used = {r["value"] for r in db.query(
    "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id='EN03'",
    (IID,))}
p("  本体 EN03 里实际出现过的 %d 个：%s" % (len(used), sorted(used)))
p("  **对照表不塞进 EN03**：EN03 记的是「这 29 张单上出现过什么」，")
p("  对照表是「客户一共分哪几类」，两种语义不混一列。")

p("")
p("=== 部门数据表 ===")
p("  %d 条；有负责人的 %d 条；对上财务组织编码的 %d 条"
  % (len(depts), sum(1 for d in depts if d["head"]),
     sum(1 for d in depts if d["fin_code"])))
for d in depts[:4]:
    p("    %-9s %-10s 属性%-5s 级别%-4s 财务码%-8s 负责人%s"
      % (d["code"], d["name"], d["attr"] or "-", d["level"] or "-",
         d["fin_code"] or "-", "有" if d["head"] else "无"))
p("    ……")

# ── 闸：部门负责人的姓名必须已在员工表里（否则闭集合守不住）──────────────
heads = sorted({d["head"] for d in depts if d["head"]})
known = {r["name"] for r in db.query(
    "SELECT name FROM employee WHERE instance_id=%s AND name IS NOT NULL", (IID,))}
missing = [h for h in heads if h not in known]
p("")
p("=== 脱敏闸：部门负责人姓名 ===")
p("  %d 个负责人；不在员工表里的 %d 个：%s" % (len(heads), len(missing), missing or "无"))
p("  （姓名落在 lookup_row.row_value 里，而脱敏闭集合是从 employee.name 等列取的，")
p("   取不到这里。都在员工表里才算守得住。）")
assert not missing, "这些部门负责人不在员工表里，不许入库：%s" % missing

if os.environ.get("WRITE") == "1":
    with db.tx() as conn:
        cur = conn.cursor()
        for tid, kind, src, keys, note, rows in (
                (T_BIZ, "allow_list", "业务分类对照表(2).xlsx", ["业务大类"],
                 "客户的业务大类全量清单（17 类）。**不是本实例实际出现过的**"
                 "——那个在本体 EN03 里，只有 4 个。两者不要混用：判「这张单的"
                 "业务大类合不合法」用这张表，判「本实例有哪些业务」用 EN03。",
                 [({"业务大类": b["name"]},
                   {"释义": b["note"], "序号": b["seq"]}) for b in biz]),
                (T_DEPT, "allow_list", "附件1：公司部门数据.xls + 附件3：账套及财务组织数据收集.xls",
                 ["部门编码"],
                 "客户的部门主数据。**不进 org 表**：org 装的是法人/核算主体，"
                 "这 12 条是部门。财务组织编码来自附件3，按部门名对上去。"
                 "负责人姓名入库前核过都在员工表里（脱敏闭集合才守得住）。",
                 [({"部门编码": d["code"]},
                   {"名称": d["name"], "上级": d["parent"], "部门属性": d["attr"],
                    "级别": d["level"], "财务组织编码": d["fin_code"],
                    "部门负责人": d["head"]}) for d in depts])):
            cur.execute("DELETE FROM lookup_row WHERE instance_id=%s AND table_id=%s",
                        (IID, tid))
            cur.execute("DELETE FROM lookup_table WHERE instance_id=%s AND table_id=%s",
                        (IID, tid))
            cur.execute("""INSERT INTO lookup_table(instance_id, table_id, version,
                               kind, source_file, source_rows, row_count,
                               key_fields, on_miss, note)
                           VALUES (%s,%s,'0.1',%s,%s,%s,%s,%s,'报错',%s)""",
                        (IID, tid, kind, src, len(rows), len(rows),
                         json.dumps(keys, ensure_ascii=False), note))
            for k, v in rows:
                cur.execute("""INSERT INTO lookup_row(instance_id, table_id, row_key,
                                   row_value, enabled, confidence)
                               VALUES (%s,%s,%s,%s,TRUE,'原生文本')""",
                            (IID, tid, json.dumps(k, ensure_ascii=False),
                             json.dumps(v, ensure_ascii=False)))
    p("")
    p("**已入库**")
    for r in db.query("""SELECT table_id, kind, row_count FROM lookup_table
                          WHERE instance_id=%s ORDER BY table_id""", (IID,)):
        n = db.scalar("""SELECT count(*) FROM lookup_row
                          WHERE instance_id=%s AND table_id=%s""",
                      (IID, r["table_id"]))
        p("  %-14s %-14s 登记 %s 行 / 实际 %s 行" % (r["table_id"], r["kind"],
                                                   r["row_count"], n))
    p("  fjhy 的参照表未被动：%s 张"
      % db.scalar("SELECT count(*) FROM lookup_table WHERE instance_id='fjhy'"))
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "lookups822.txt"), "w",
        encoding="utf-8").write("\n".join(out))
print("ok: 业务大类 %d / 部门 %d" % (len(biz), len(depts)))
