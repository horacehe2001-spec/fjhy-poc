# -*- coding: utf-8 -*-
r"""822港航 的三类差旅标准 → 库（standard_limit / lookup_table）。

用法（**不要加 -I**，理由同 load_822gh_bills）：

    cd 服务端
    python -m app.demo.load_822gh_standards          # 干跑
    WRITE=1 python -m app.demo.load_822gh_standards  # 真写库（可重入）

源：**822港航 自己的制度条款**（已切好在 doclib_clause 里，343 条），
不是从福建海运集团搬过来的。取值逐字抄自条款正文，条款号记进 source_clause。

背景：standard_limit 里原来只有住宿费限额 317 条，交通工具等级标准、
伙食补助费标准、市内交通费标准各 0 行，于是 11 条规则判不了
（见 rule_feasibility 的 EXTRA_NEEDS）。**缺的不是客户的数据，
是我方没把制度里写着的标准抽成系统能比对的表。**

几条口径：

1. **以 闽港发资财〔2026〕3 号《关于修订…差旅费管理办法》为准。**
   库里还有一份 2023 年 21 号的旧办法，两份的伙食补助费与市内交通费
   **金额完全一致**（省内 100 / 西藏青海新疆 120 / 其他省份 100 / 市内 80）。
   一致这件事记进 cross_checked，**但来源只记现行那一份**——
   记两份会让「这条标准出自哪」有两个答案。

2. **交通工具等级标准不进 standard_limit。**
   那张表的每一行是一个**金额**（limit_amount NUMERIC），而交通工具等级
   是「允许坐什么等级」，不是钱。硬塞进去就得把等级写进 region 或者
   另造一列，又是一列两种语义——这个项目里已经为此踩过三次
   （payee_account、rule.feasibility、attachment.source_kind）。
   福建海运集团也没把它放进 standard_limit，它的同类参照数据
   （市内交通费扣减档位、金额阈值表）都在 lookup_table 里。
   所以这一类进 lookup_table，kind=decision_table：
   给定（级别, 交通工具）查「允许等级」。

3. **「误餐补助 50 元」没有入库。**
   第二十六条/第三十条写的是「不报销伙食补助费…每人每天发放误餐补助 50 元」。
   误餐补助和伙食补助费在制度里是**两样东西**（一个发、一个不发），
   把它当成伙食补助费标准的一行，就是把两种语义塞进同一个 std_type。
   这一项列进待确认，不自己定。

4. **交通工具等级表的档位名与住宿费标准表不是一套。**
   住宿表用「厅局级 / 其他人员」（EN21），交通工具表用
   「公司正职领导、正高级职称、随同省级人员 / 其他人员」。
   两张表的「其他人员」是不是同一群人，**要客户确认**，这里不做映射。
   所以 lookup_row 的 tier 原样抄制度，不往 EN21 上靠。
"""
import io
import json
import os
import re
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

from app import config, db                                      # noqa: E402

IID = "822gh"
CUR_DOC = "闽港发资财〔2026〕3号《关于修订〈福建省港航建设发展有限公司差旅费管理办法〉的通知》"
OLD_DOC = "2023年21号《福建省港航建设发展有限公司差旅费管理办法》"
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")

out = []


def p(s=""):
    out.append(str(s))


# ── 一、金额型标准 → standard_limit ──────────────────────────────────────────
# 逐字抄自 闽港发资财〔2026〕3 号 第十四条、第十七条。
# unit 的写法沿用福建海运集团那边（元/人·天（包干使用）），
# 因为制度原文用的就是「包干使用」这个词，不是我方的叫法。
MEALS = [
    ("省内", 100, "第十四条「省内出差每人每天100元包干使用」"),
    ("省外（西藏、青海、新疆）", 120,
     "第十四条「省外出差除西藏、青海、新疆每人每天120元包干使用外」"),
    ("省外（其他省份）", 100, "第十四条「其他省份出差每人每天100元包干使用」"),
]
CITY_TRAFFIC = ("", 80, "第十七条「市内交通费按出差自然（日历）天数计算，"
                        "每人每天 80 元包干使用」")

# ── 二、非金额型标准 → lookup_table ──────────────────────────────────────────
# 逐字抄自 闽港发资财〔2026〕3 号 第二十三条的表格。
# 级别名**原样抄**，不往 EN21 上映射（见文件头口径 4）。
VEHICLES = ["火车（含高铁、动车、全列软席列车）", "轮船（不包括旅游船）", "飞机",
            "其他符合规定的公路交通工具（不包括出租小汽车）"]
TIER_HIGH = ("1、公司正职领导；2、企业聘任的正高级职称人员；"
             "3、因工作需要，随同省级及相当职务人的出差人员")
TIER_LOW = "其他人员"
CLASS_TABLE = {
    TIER_HIGH: ["火车软席（软座、软卧），高铁/动车一等座，全列软席列车一等软座",
                "二等舱", "经济舱", "凭据报销"],
    TIER_LOW: ["火车硬席（硬座、硬卧），高铁/动车二等座，全列软席列车二等软座",
               "三等舱", "经济舱", "凭据报销"],
}
LOOKUP_ID = "交通工具等级标准"


_CLAUSES = None


def clause_hits(needle):
    """这段文字在本实例的哪些制度文件、哪些条款里出现过。

    **返回全部命中，不是 LIMIT 1。** 第一版只取一条，于是报告上显示的是
    2023 年旧办法，而来源写的是 2026 年现行办法——看报告的人会以为
    我抄的是旧版。更要紧的是：`cross_checked` 的依据正是「两版金额一致」，
    只取一条就验不了这件事，那个值就成了假设。

    **比对前把空白全去掉。** 旧版 docx 写「每人每天80元」，
    现行版 PDF 的文字层写「每人每天 80 元」——同一句话、同一个数，
    只是排版带了空格。第一版用 SQL LIKE 直接比，这一条就只命中 1 份文件，
    于是 cross_checked 被写成 False。**那是我的匹配太死，不是两版不一致**，
    而一个假的 False 会让人以为这条标准没核对过。
    """
    global _CLAUSES
    if _CLAUSES is None:
        _CLAUSES = db.query("""SELECT c.clause_no, c.body, f.file_name
                                 FROM doclib_clause c JOIN doc_lib_file f
                                   ON f.file_id = c.file_id
                                WHERE f.instance_id = %s
                                ORDER BY f.file_name""", (IID,))
        for c in _CLAUSES:
            c["flat"] = re.sub(r"\s+", "", c["body"] or "")
    k = re.sub(r"\s+", "", needle)
    return [c for c in _CLAUSES if k in c["flat"]]


def in_current(hits):
    """命中里有没有现行那一份（闽港发资财〔2026〕3 号）。"""
    return [h for h in hits if "2026" in h["file_name"] and "3号" in h["file_name"]]


p("=== 抄录核对：我写进代码的数字，必须能在**现行**制度条款里原样找到 ===")
CHECKS = [
    ("省内出差每人每天100元包干使用", "省内出差每人每天100元包干使用"),
    ("西藏、青海、新疆每人每天120元", "西藏、青海、新疆每人每天120元"),
    ("市内交通费…每人每天80元包干使用", "每人每天80元包干使用"),
    ("交通工具等级表（高档）", "火车软席（软座、软卧），高铁/动车一等座"),
    ("交通工具等级表（其他人员）", "火车硬席（硬座、硬卧），高铁/动车二等座"),
]
missing, cross = [], {}
for lab, like in CHECKS:
    hits = clause_hits(like)
    cu = in_current(hits)
    cross[lab] = len({h["file_name"] for h in hits})
    if cu:
        p("  ✓ %-28s 现行办法 %s；共 %d 份文件里出现过"
          % (lab, cu[0]["clause_no"], cross[lab]))
    elif hits:
        missing.append("%s（只在旧版里找到：%s）"
                       % (lab, hits[0]["file_name"][:30]))
        p("  ✗ %-28s **现行办法里找不到**，只在 %s"
          % (lab, hits[0]["file_name"][:30]))
    else:
        missing.append(lab)
        p("  ✗ %-28s **任何条款里都找不到**" % lab)
assert not missing, (
    "这些文字在**现行**制度条款里找不到，不许入库：%s" % missing)

p("")
p("=== 交叉核对（cross_checked 这一列的依据）===")
p("  新旧两版都出现的条目：%d / %d"
  % (sum(1 for v in cross.values() if v >= 2), len(cross)))
for lab, n in cross.items():
    p("    %-28s 出现在 %d 份文件里%s" % (lab, n, "" if n >= 2 else "（只有现行这一份）"))
CROSSED = all(v >= 2 for v in cross.values())
p("  → cross_checked 写 %s（%s）"
  % (CROSSED, "新旧两版逐条对得上" if CROSSED else "有条目只在一份文件里，不能算交叉核对过"))
p("  来源只记现行那一份（%s）：记两份会让「这条标准出自哪」有两个答案" % CUR_DOC[:24])

p("")
p("=== 要入库的 ===")
p("  伙食补助费标准 %d 行：%s" % (len(MEALS), [(a, b) for a, b, _ in MEALS]))
p("  市内交通费标准 1 行：%s 元/人·天" % CITY_TRAFFIC[1])
p("  交通工具等级标准 %d 行（%d 个级别 × %d 种交通工具）→ lookup_table"
  % (len(CLASS_TABLE) * len(VEHICLES), len(CLASS_TABLE), len(VEHICLES)))
p("")
p("  **没入库的**：「误餐补助 50 元」——制度里它和伙食补助费是两样东西")
p("              （一个发、一个不发），当成伙食补助费的一行就是两种语义")
p("              塞进同一个 std_type。列进待确认。")
p("  **没做映射的**：交通工具等级表的级别名是「公司正职领导…/其他人员」，")
p("              住宿费标准表是「厅局级/其他人员」，两张表不是一套档位。")
p("              两个「其他人员」是不是同一群人，要客户确认。")

if os.environ.get("WRITE") == "1":
    with db.tx() as conn:
        cur = conn.cursor()
        # 可重入：只清本实例这三类，**住宿费限额 317 行一行不动**
        cur.execute("""DELETE FROM standard_limit WHERE instance_id = %s
                        AND std_type IN ('伙食补助费标准','市内交通费标准')""",
                    (IID,))
        for region, amt, src in MEALS:
            cur.execute("""INSERT INTO standard_limit(instance_id, std_type, tier,
                               region, limit_amount, unit, source_clause,
                               confidence, cross_checked)
                           VALUES (%s,'伙食补助费标准',NULL,%s,%s,
                                   '元/人·天（包干使用）',%s,'原生文本',%s)""",
                        (IID, region, amt, "%s %s" % (CUR_DOC, src), CROSSED))
        cur.execute("""INSERT INTO standard_limit(instance_id, std_type, tier,
                           region, limit_amount, unit, source_clause,
                           confidence, cross_checked)
                       VALUES (%s,'市内交通费标准',NULL,%s,%s,
                               '元/人·天（包干使用）',%s,'原生文本',%s)""",
                    (IID, CITY_TRAFFIC[0], CITY_TRAFFIC[1],
                     "%s %s" % (CUR_DOC, CITY_TRAFFIC[2]), CROSSED))
        # 交通工具等级标准 → lookup_table（kind=decision_table）
        cur.execute("DELETE FROM lookup_row WHERE instance_id=%s AND table_id=%s",
                    (IID, LOOKUP_ID))
        cur.execute("DELETE FROM lookup_table WHERE instance_id=%s AND table_id=%s",
                    (IID, LOOKUP_ID))
        n_rows = len(CLASS_TABLE) * len(VEHICLES)
        cur.execute("""INSERT INTO lookup_table(instance_id, table_id, version, kind,
                           source_file, source_rows, row_count, key_fields,
                           on_miss, note)
                       VALUES (%s,%s,'0.1','decision_table',%s,%s,%s,%s,%s,%s)""",
                    (IID, LOOKUP_ID, CUR_DOC + " 第二十三条", n_rows, n_rows,
                     json.dumps(["tier", "vehicle"], ensure_ascii=False),
                     "报错",
                     "交通工具等级不是金额，所以不进 standard_limit（那张表的值是"
                     "NUMERIC）。级别名原样抄制度，**没有往住宿费档位（EN21）上"
                     "映射**——住宿表写「厅局级/其他人员」，这张表写「公司正职"
                     "领导…/其他人员」，两个「其他人员」是不是同一群人要客户确认。"))
        for tier, allowed in CLASS_TABLE.items():
            for veh, cls in zip(VEHICLES, allowed):
                cur.execute("""INSERT INTO lookup_row(instance_id, table_id, row_key,
                                   row_value, enabled, confidence, note)
                               VALUES (%s,%s,%s,%s,TRUE,'原生文本',%s)""",
                            (IID, LOOKUP_ID,
                             json.dumps({"tier": tier, "vehicle": veh},
                                        ensure_ascii=False),
                             json.dumps({"allowed": cls}, ensure_ascii=False),
                             "逐字抄自 %s 第二十三条的表格" % CUR_DOC[:24]))
    p("")
    p("**已入库**")
    for q, lab in (
            ("""SELECT count(*) FROM standard_limit WHERE instance_id=%s
                  AND std_type='伙食补助费标准'""", "伙食补助费标准"),
            ("""SELECT count(*) FROM standard_limit WHERE instance_id=%s
                  AND std_type='市内交通费标准'""", "市内交通费标准"),
            ("""SELECT count(*) FROM standard_limit WHERE instance_id=%s
                  AND std_type='住宿费限额标准'""", "住宿费限额标准（未动）"),
            ("""SELECT count(*) FROM lookup_row WHERE instance_id=%s
                  AND table_id='交通工具等级标准'""", "交通工具等级标准")):
        p("  %s: %s" % (lab, db.scalar(q, (IID,))))
    p("  fjhy 的标准表未被动：%s 行"
      % db.scalar("SELECT count(*) FROM standard_limit WHERE instance_id='fjhy'"))
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "std822.txt"), "w",
        encoding="utf-8").write("\n".join(out))
print("ok: 伙食 %d 行 / 市内交通 1 行 / 交通工具等级 %d 行"
      % (len(MEALS), len(CLASS_TABLE) * len(VEHICLES)))
