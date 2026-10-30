# -*- coding: utf-8 -*-
"""把「从 822港航 人工驳回意见反向抽出来的规则」装进 rule 表。

── 为什么会有这批规则 ───────────────────────────────────────────────────
822港航 原有 74 条规则全部是从制度 PDF 抽的。把 29 条真实人工驳回意见
和这 74 条对过一遍，结论是**两者基本不重合**：

    驳回理由分类（29 条）        74 条制度规则里的对应
    ────────────────────────   ─────────────────────────
    入账期间填错        8 条     无
    附件不全/缺凭证      8 条     TR-差旅-08 / TR-差旅-21（部分对上）
    审批单未体现用餐用车  5 条     无
    出差地点/日历填错    4 条     只有含糊的沾边项
    招待明细录入不规范    4 条     对不上（既有项讲的是别的事）
    招待场所与发票不一致  3 条     TR-接待-03 讲「禁止高消费场所」，不是这件事
    审批滞后           2 条     无
    报销人与收款人不一致  2 条     无
    代报需授权         1 条     无（福建海运集团有 TR-D1-03）

9 类里 5 类**完全没有对应规则**。原因不是抽漏了：制度文字写的是**实体
规范**（超标准、超范围、票据要素），而共享审核岗实际在拦的是**填报规范**
（期间填错、附件不全、前后不一致、流程滞后）。制度里没有这些话，所以从
制度抽不出这些规则。要补上，只能从审核岗实际的驳回理由反向抽。

── 这批规则和那 74 条的区别，看字段就能分辨 ─────────────────────────
  source_clause.confidence = onto_coord.NON_POLICY_SOURCE（「人工驳回」）
  source_clause.file       = 驳回意见台账名，不是制度文件名
  source_clause.clause     = 引用的驳回意见编号，不是条款号
  dimension                = 跨制度域的那些落在「通用」（本体 v0.1 第 9 个维度）
对账侧（onto_coord.align_reverse）认这个 confidence，会把 basis 写成
「按设计没有制度条款出处」而不是「来源对不上」——别把这两件事混起来。

── 刻意没做的事 ────────────────────────────────────────────────────
  * **不编译**。compiled=false，logic 存自然语言。14 条里 13 条自报
    「判不了」或「判不全」（要读审批单图片、要做发票 OCR、要人工比对），
    现在编译只能编出一个能跑但判不准的东西。发布闸会把它们挡在生效版本外。
  * **priority 用默认值 50**（全是警告级）。排序是人的事，没排就是没排。
  * **不写 risk_class='未分类'**。抽取时给了封闭词表，优先复用福建海运集团
    已有的 39 个取值，两个实例才对得起来。

可重入：按 (instance_id, rule_id, version) 覆盖写，重复跑结果一样。
用法：python -m app.demo.load_822gh_rejection_rules
      **不要加 -I**：隔离模式会把 cwd 从 sys.path 里摘掉，import app 直接失败。
"""
import io
import json
import os

from .. import db, onto_coord

INSTANCE = "822gh"
SRC_FILE = "822港航 共享审核岗人工驳回意见台账（2026 年 29 条）"
CREATED_BY = "ai_draft_from_rejection"

# 维度 → 新规则起始序号。沿用既有编号格式 TR-<制度域>-<两位序号>，
# 接在现有序号后面，**不另起前缀**：来源差异是数据（source_clause），
# 不该编进主键。编号里塞来源标记，等以后来源多了就得再改一次编号格式。
_SEQ_START = {"差旅": 28, "接待": 21, "通用": 1}

_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "822gh_rejection_rules.json")


def _rows():
    d = json.load(io.open(_JSON, encoding="utf-8"))
    reasons = {r["no"]: r for r in d["reasons"]}
    seq = dict(_SEQ_START)
    out = []
    for m in d["merged"]:
        dim = m["dimension"]
        assert dim in seq, "维度 %s 没给起始序号，别让它撞上既有编号" % dim
        rid = "TR-%s-%02d" % (dim, seq[dim])
        seq[dim] += 1
        nos = m["from_reasons"]
        quoted = "；".join("#%d %s" % (n, reasons[n]["text"].replace("\n", " "))
                           for n in nos)
        note_bits = [
            "从人工驳回意见反向抽取（型号 %s）。" % d.get("model", "?"),
            "**可自动判定：%s**" % m["auto_judgeable"],
        ]
        if m.get("auto_blocker"):
            note_bits.append("判不了/判不全的原因：%s" % m["auto_blocker"])
        if m.get("need_fields"):
            note_bits.append("判定需要的数据项：%s" % "、".join(m["need_fields"]))
        if m.get("_merged_from"):
            note_bits.append("分批抽取时同类被抽了 %d 次，已合并：%s。取舍：%s"
                             % (len(m["_merged_from"]),
                                "／".join(m["_merged_from"]),
                                m.get("merge_note") or "（未说明）"))
        note_bits.append("**priority=50 是警告级默认值，未经人工排序。**")
        out.append({
            "instance_id": INSTANCE, "rule_id": rid, "version": 1,
            "name": m["name"], "dimension": dim,
            "risk_class": m["risk_class"], "rule_class": m["rule_class"],
            "control_type": m["control_type"],
            "source_clause": json.dumps({
                "file": SRC_FILE,
                "clause": "驳回意见 " + "、".join("#%d" % n for n in nos),
                "text": quoted[:1800],
                "confidence": onto_coord.NON_POLICY_SOURCE,
            }, ensure_ascii=False),
            "scope": json.dumps({"text": m["scope"]}, ensure_ascii=False),
            "logic": json.dumps({"text": m["logic"], "type": "text",
                                 "exception": None}, ensure_ascii=False),
            "priority": 50,
            "compiled": False,
            "compiled_reason": ("反向抽取草稿尚未编译：logic 存的是自然语言判定"
                                "逻辑，没有可执行表达式。且本条自报可自动判定="
                                "%s，编译前要先解决「%s」"
                                % (m["auto_judgeable"],
                                   m.get("auto_blocker") or "无")),
            "status": "draft",
            "created_by": CREATED_BY,
            "note": "\n".join(note_bits),
        })
    return out


def load():
    rows = _rows()
    cols = list(rows[0].keys())
    sql = ("INSERT INTO rule (%s) VALUES (%s) "
           "ON CONFLICT (instance_id, rule_id, version) DO UPDATE SET %s"
           % (", ".join(cols), ", ".join(["%s"] * len(cols)),
              ", ".join("%s = EXCLUDED.%s" % (c, c) for c in cols
                        if c not in ("instance_id", "rule_id", "version"))))
    for r in rows:
        db.execute(sql, tuple(r[c] for c in cols))
    return [r["rule_id"] for r in rows]


def main():
    ids = load()
    print("装入 %d 条反向抽取规则：%s" % (len(ids), "、".join(ids)))
    n = db.query("""SELECT count(*) AS n FROM rule
                     WHERE instance_id = %s
                       AND source_clause->>'confidence' = %s""",
                 (INSTANCE, onto_coord.NON_POLICY_SOURCE))[0]["n"]
    tot = db.query("SELECT count(*) AS n FROM rule WHERE instance_id = %s",
                   (INSTANCE,))[0]["n"]
    print("822港航 规则合计 %d 条，其中反向抽取 %d 条、制度抽取 %d 条"
          % (tot, n, tot - n))


if __name__ == "__main__":
    main()
