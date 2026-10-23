# -*- coding: utf-8 -*-
"""规则本体 · 统一口径：双向对账 + 采纳入本体 + 关系图谱。

「统一口径是本体要做的事，规则抽取只负责抽取」（2026-10-19 用户口径）。
抽取那一栏交过来的是散的：条款、草稿、归并组、口径冲突。
这里回答的是另一个问题——**这些东西和现有的规则清单对得上吗**。

对账必须双向，单向各漏一半：
  制度 → 落地   哪些制度要求在规则清单里找不到（可能要新增规则）
  规则 → 依据   哪些规则在制度里找不到出处（可能是业务惯例而非制度要求）

这个模块里最重要的不是算法，是**三条「不许下断言」的边界**。
三条都是实测逼出来的，去掉任何一条，页面上就会出现一句假话：

一、**「判不出」必须是一个独立档位，不许二分成落地/未落地。**
   规则清单的 source_clause.file 存的是**类别代号**（规则表/要点/制度/
   场景表/配置器/解答），不是文件名。实测这些代号在 14 份文件里：
     要点→1 份、制度→1 份、解答→1 份、规则表/场景表/配置器→0 份
   也就是说，一条写着「制度 第七条」的规则，指的可能是《国资委差旅制度》
   也可能是《差旅管理实施细则》——**对不到唯一文件就不能下断言**。
   把这类算成「未落地」，人会去新增一条其实已经存在的规则，
   而重复规则对同一张单会给出两条结论，比漏一条更难查。

二、**条款号对不上 ≠ 查无依据。**
   实测 10 条规则的来源是款级（第二十三条（三））、表内行（2.3.1 表第1行）、
   问答序号（解答3）或「多处」。它们都有依据，只是两套编号不同。
   标成「查无依据」会让人去改规则——而该改的是切分粒度。

三、**有未裁决冲突的归并组不许采纳。**
   口径还没统一就采纳，等于替客户把一方口径当成了结论。
   这条做成硬闸（adopt 直接拒绝），不是提示。
"""
import json
import re
import uuid

from . import db, doclib_extract as dx

# ── 来源代号能不能钉到唯一一份文件 ──────────────────────────────────────────
SRC_CODES = ("规则表", "要点", "制度", "场景表", "配置器", "解答")


def code_targets():
    """类别代号 → 文件库里名字含该代号的文件。**唯一命中才算可比对。**

    这不是相似度匹配：代号是规则清单里原样写着的字符串，
    「名字里含这个代号」是能复核的硬条件。但**命中 0 份或多份都不算钉上**——
    多份命中时选一份就是猜，而猜错会把一条规则挂到错的制度文件上，
    给出一条看起来有据可查、实际指错地方的证据链。
    """
    files = [r["file_name"] for r in
             db.query("SELECT file_name FROM doc_lib_file WHERE NOT missing")]
    return {code: [f for f in files if code in f] for code in SRC_CODES}


def comparable_files():
    """可比对文件集：至少被一个代号唯一命中的文件。

    不在这个集合里的文件（实测：《差旅管理实施细则》《业务接待管理办法》
    和三份 xlsx），规则清单里压根没有能钉到它的来源写法——
    所以这些文件里的条款**既不能判已落地，也不能判未落地**。
    """
    t = code_targets()
    return {hit[0] for hit in t.values() if len(hit) == 1}, t


# ── 制度 → 落地 ─────────────────────────────────────────────────────────────
def _group_rows():
    """归并组的成员条款，带文件名、条款号、覆盖规则、切分策略。"""
    return db.query("""
        SELECT s.group_key, s.clause_id, s.source_kind, s.confidence,
               c.clause_no, c.covered_by, c.split_strategy, c.title, c.numbers,
               f.file_name
          FROM doclib_rule_source s
          JOIN doclib_clause c ON c.clause_id = s.clause_id
          JOIN doc_lib_file  f ON f.file_id   = s.file_id
         ORDER BY s.group_key, f.file_name, c.seq""")


def align_forward():
    """→ [onto_align 行]。每个归并组对上了哪几条规则。"""
    comparable, _ = comparable_files()
    conflict_groups = {r["group_key"] for r in db.query(
        "SELECT DISTINCT group_key FROM doclib_conflict WHERE state = '待裁决'")}
    by = {}
    for r in _group_rows():
        by.setdefault(r["group_key"], []).append(r)

    out = []
    for key, items in by.items():
        pairs = [(rid, it) for it in items for rid in (it["covered_by"] or [])]
        rules = sorted({rid for rid, _ in pairs})
        files = sorted({it["file_name"] for it in items})
        in_scope = [it for it in items if it["file_name"] in comparable]
        has_ocr = any(it["confidence"] == "OCR" for it in items)
        if rules:
            verdict = "已落地"
            basis = "条款号与规则来源精确命中：" + "、".join(
                "%s←%s %s" % (rid, it["file_name"][:14], it["clause_no"])
                for rid, it in pairs[:8])
        elif not in_scope:
            verdict = "判不出"
            basis = ("组内 %d 条来源出自 %s；规则清单用类别代号记来源，"
                     "代号对不到这些文件中的任何一份，既不能判已落地也不能判未落地"
                     % (len(items), "、".join(f[:20] for f in files)))
        else:
            verdict = "未落地"
            basis = ("组内有 %d 条来源出自可比对文件（%s），条款号都没有被任何规则引用"
                     % (len(in_scope),
                        "、".join(sorted({it["file_name"][:18] for it in in_scope}))))
        out.append({"group_key": key, "verdict": verdict, "rule_ids": rules,
                    "match_basis": basis[:900], "n_sources": len(items),
                    "evidence": "多源印证" if len(items) > 1 else "单一来源",
                    "has_conflict": key in conflict_groups, "has_ocr": has_ocr})
    return out


# ── 规则 → 依据 ─────────────────────────────────────────────────────────────
def align_reverse():
    """→ [onto_rule_cover 行]。每条规则找不找得到制度出处。"""
    _, codes = comparable_files()

    cited, by_file = {}, {}
    for c in db.query("""SELECT c.clause_no, c.covered_by, c.split_strategy, f.file_name
                           FROM doclib_clause c
                           JOIN doc_lib_file f ON f.file_id = c.file_id"""):
        by_file.setdefault(c["file_name"], set()).add(c["clause_no"])
        for rid in (c["covered_by"] or []):
            cited.setdefault(rid, []).append(c)
    groups_of = {}
    for r in _group_rows():
        for rid in (r["covered_by"] or []):
            groups_of.setdefault(rid, set()).add(r["group_key"])

    out = []
    for r in db.query("""SELECT rule_id, version,
                                source_clause->>'file'   AS src_file,
                                source_clause->>'clause' AS src_clause
                           FROM rule WHERE version = 1 ORDER BY rule_id"""):
        rid, code, cno = r["rule_id"], r["src_file"], r["src_clause"]
        hits = cited.get(rid) or []
        if hits:
            only_table = all(h["split_strategy"] == "表格行" for h in hits)
            state = "仅规则表" if only_table else "有制度依据"
            basis = "%d 条条款引用了它：%s" % (
                len(hits), "、".join("%s %s" % (h["file_name"][:16], h["clause_no"])
                                     for h in hits[:4]))
            if only_table:
                basis += "。**只有规则表（xlsx）的行引用它，制度文件里没找到**——" \
                         "规则表是人工整理的二手产物，这条可能是业务惯例而不是制度要求"
        elif not code or not cno:
            state = "来源未写明"
            basis = "来源条款没写文件或条款号，无法比对"
        elif len(codes.get(code) or []) != 1:
            state = "来源未写明"
            basis = ("来源写的是类别代号「%s」而不是文件名，文件库里名字含该代号的文件有 %d 份，"
                     "对不到唯一一份，**无法比对**（这是台账口径问题，不是规则质量问题）"
                     % (code, len(codes.get(code) or [])))
        else:
            fname = codes[code][0]
            if (cno or "").strip() not in (by_file.get(fname) or set()):
                state = "编号对不上"
                basis = ("钉到《%s》，但条款号「%s」不在该文件切出的条款清单里——"
                         "来源写的是款级/表内行/问答序号或多处，和条款级编号对不上。"
                         "**这不是没有依据，是两套编号对不上**，"
                         "该做的是把切分做到款级/行级" % (fname[:24], cno))
            else:
                state = "查无依据"
                basis = ("钉到《%s》且条款号「%s」确实存在，但那一条没有引用这条规则——"
                         "这是真的找不到依据" % (fname[:24], cno))
        out.append({"rule_id": rid, "version": r["version"], "state": state,
                    "group_keys": sorted(groups_of.get(rid) or []), "basis": basis[:900]})
    return out


# ── 落库 ────────────────────────────────────────────────────────────────────
def save_align(fwd, rev):
    """**算出来的两张表整表重算**；人拍的 onto_adoption 一个字不动。

    **两张表一起占住。** 这个函数被「工场一键搭建」和「统一口径运行」
    共用（同一函数对象），两条链的作业锁互不阻塞。一起锁而不是分两次锁，
    是因为这两张表是一次对账的正反两面：只锁住一张，另一张仍会被
    另一条链中途重算，结果是正向反向对不上——而这种不一致不报错。
    """
    with db.table_lock("onto_align", "onto_rule_cover", who="口径对账 onto_coord.save_align"):
        return _save_align_locked(fwd, rev)


def _save_align_locked(fwd, rev):
    db.execute("DELETE FROM onto_align")
    for a in fwd:
        db.execute("""INSERT INTO onto_align(group_key, verdict, rule_ids, match_basis,
                          n_sources, evidence, has_conflict, has_ocr)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (a["group_key"], a["verdict"], a["rule_ids"], a["match_basis"],
                    a["n_sources"], a["evidence"], a["has_conflict"], a["has_ocr"]))
    db.execute("DELETE FROM onto_rule_cover")
    for c in rev:
        db.execute("""INSERT INTO onto_rule_cover(rule_id, version, state, group_keys, basis)
                      VALUES (%s,%s,%s,%s,%s)""",
                   (c["rule_id"], c["version"], c["state"], c["group_keys"], c["basis"]))
    return len(fwd), len(rev)


# ── 清单与统计 ──────────────────────────────────────────────────────────────
def list_align(verdict=None, limit=300):
    sql = ["""SELECT a.*,
                     (SELECT action FROM onto_adoption d WHERE d.group_key = a.group_key
                       ORDER BY d.adopted_at DESC LIMIT 1) AS last_action,
                     (SELECT d.rule_id FROM onto_adoption d WHERE d.group_key = a.group_key
                       AND d.action = '采纳' LIMIT 1) AS adopted_rule
                FROM onto_align a WHERE TRUE"""]
    args = []
    if verdict:
        sql.append("AND a.verdict = %s")
        args.append(verdict)
    sql.append("ORDER BY a.n_sources DESC, a.group_key LIMIT %s")
    args.append(max(1, min(500, int(limit))))
    return db.query(" ".join(sql), tuple(args))


def list_cover(state=None, limit=300):
    sql = ["""SELECT c.*, r.name AS rule_name, r.dimension, r.control_type, r.rule_class,
                     r.source_clause->>'file'   AS src_file,
                     r.source_clause->>'clause' AS src_clause
                FROM onto_rule_cover c
                JOIN rule r ON r.rule_id = c.rule_id AND r.version = c.version
               WHERE TRUE"""]
    args = []
    if state:
        sql.append("AND c.state = %s")
        args.append(state)
    sql.append("ORDER BY c.rule_id LIMIT %s")
    args.append(max(1, min(500, int(limit))))
    return db.query(" ".join(sql), tuple(args))


def cover_check(n_rules, n_cover):
    """反向覆盖台账是不是这一轮算出来的。**0 不等于「一条都没覆盖」。**

    实测（2026-10-20）外键：
        onto_rule_cover.(rule_id, version) → rule   ON DELETE CASCADE
    所以第③步重建 rule 表会把这张表**整表级联清空**，而全项目只有
    第⑦步的 onto_coord._save_align_locked() 会重建它（唯一写入者）。
    链在 ③ 和 ⑦ 之间任何一次失败（闸没过、被互斥闸拒绝、人中途停），
    这张表就静默留空——页面上显示「规则覆盖 0」。

    **这两件事的处置完全相反**：
      · 真的 0 条覆盖 → 要去查规则为什么都找不到依据
      · 还没算        → 只要补跑第⑦步
    显示成同一个 0，客户只会往第一条去想。实测踩到：双击测试之后
    这张表从 82 掉到 0，而页面上看不出任何异常。

    **判据是确定性的，不是启发式。** onto_coord.align_reverse() 的每个
    分支都 out.append()——每条 version=1 的规则必然产出一行。所以
    「规则表有行而覆盖 0 行」不可能是正常跑完的结果。
    误报方向也钉住：规则表本身是空的（还没跑第③步）时，覆盖为 0 是
    正常的，这里必须报 ok——把「没开」说成「坏了」，下一次真坏了就没人看了。
    """
    if not n_rules:
        return {"check": "反向覆盖台账与规则表同步", "level": "warning", "ok": True,
                "detail": "规则表还是空的（第③步没跑），覆盖为 0 是正常的"}
    if not n_cover:
        return {"check": "反向覆盖台账与规则表同步", "level": "warning", "ok": False,
                "detail": "规则 %d 条，反向覆盖 0 条——这不是「一条都没覆盖」，是这一轮没算。"
                          "第③步重建 rule 表会级联清空这张表（ON DELETE CASCADE），"
                          "只有第⑦步会重建它——补跑第⑦步「统一口径」即可。" % n_rules}
    if n_cover != n_rules:
        return {"check": "反向覆盖台账与规则表同步", "level": "warning", "ok": False,
                "detail": "规则 %d 条但反向覆盖 %d 条。每条规则必然产出一行，"
                          "数不相等说明规则表在上一次对账之后又变过（采纳进来的规则、"
                          "重装过规则资产），覆盖台账是旧的——补跑第⑦步。"
                          % (n_rules, n_cover)}
    return {"check": "反向覆盖台账与规则表同步", "level": "warning", "ok": True,
            "detail": "规则 %d 条，反向覆盖 %d 条，一一对应" % (n_rules, n_cover)}


def summary():
    """两个方向的对账看板。**两向并列，不合成一个「覆盖率」。**

    合成一个数就看不出缺口在哪一侧了：一侧是「制度要求没变成规则」
    （要新增规则），另一侧是「规则找不到制度出处」（要补台账或改规则），
    责任方和下一步动作完全不同。
    """
    fwd = {r["verdict"]: r["n"] for r in db.query(
        "SELECT verdict, count(*) n FROM onto_align GROUP BY 1")}
    rev = {r["state"]: r["n"] for r in db.query(
        "SELECT state, count(*) n FROM onto_rule_cover GROUP BY 1")}
    conf = {r["state"]: r["n"] for r in db.query(
        "SELECT state, count(*) n FROM doclib_conflict GROUP BY 1")}
    ado = {r["action"]: r["n"] for r in db.query(
        "SELECT action, count(*) n FROM onto_adoption GROUP BY 1")}
    return {
        "forward": {"已落地": fwd.get("已落地", 0), "未落地": fwd.get("未落地", 0),
                    "判不出": fwd.get("判不出", 0), "总计": sum(fwd.values())},
        "reverse": {k: rev.get(k, 0) for k in
                    ("有制度依据", "仅规则表", "编号对不上", "查无依据", "来源未写明")},
        "reverse_total": sum(rev.values()),
        "conflicts": {"待裁决": conf.get("待裁决", 0), "已裁决": conf.get("已裁决", 0),
                      "已忽略": conf.get("已忽略", 0)},
        "adoptions": {"采纳": ado.get("采纳", 0), "驳回": ado.get("驳回", 0),
                      "暂缓": ado.get("暂缓", 0)},
        "rules": db.scalar("SELECT count(*) FROM rule WHERE version = 1"),
        "adopted_rules": db.scalar(
            "SELECT count(*) FROM rule WHERE version = 1 AND created_by LIKE 'onto_adopt%%'"),
        "note": "两个方向并列，**不合成一个覆盖率**：左边缺口是「制度要求没变成规则」，"
                "右边缺口是「规则找不到制度出处」，责任方和下一步动作完全不同。",
        # **这一页也要报。** 反向那一列全是 0 的时候，这一页看起来是
        # 「82 条规则一条依据都没有」——而真相多半是第③步刚重建过 rule 表、
        # 把这张表级联清空了，第⑦步还没补跑。本体工场那一页已经报了，
        # 但客户是在这一页看覆盖数的，只报一处等于没报。
        "freshness": [cover_check(
            db.scalar("SELECT count(*) FROM rule WHERE version = 1"), sum(rev.values()))],
    }


# ── 本体关系 ────────────────────────────────────────────────────────────────
def relations():
    """本体里的关系。**本体没有关系就只是一张清单。**

    四类关系分开给，因为它们的性质不同：
      实体↔实体   结构（包含/关联/流转/归属），带基数和来源
      规则↔规则   覆盖/优先/冲突。**冲突那两条挂着 needs_confirm 与缺口号**，
                  是「不替客户拍板」的落地形态
      规则↔概念   引用关系，其中阈值位（role=threshold）契约校验更严
      归并组↔规则 统一口径算出来的新关系：同一条规则的多个出处
    """
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    ent = db.query("""SELECT r.from_entity, r.to_entity, r.rel_type, r.cardinality,
                             r.source, r.note, ef.name AS from_name, et.name AS to_name
                        FROM ontology_relation r
                        LEFT JOIN ontology_entity ef
                          ON ef.entity_id = r.from_entity AND ef.version = r.version
                        LEFT JOIN ontology_entity et
                          ON et.entity_id = r.to_entity AND et.version = r.version
                       WHERE r.version = %s ORDER BY r.from_entity, r.to_entity""", (v,))
    rr = db.query("""SELECT rl.rule_id, rl.rel_type, rl.other_rule, rl.needs_confirm,
                            rl.gap_id, rl.note, r.name AS rule_name
                       FROM rule_relation rl
                       LEFT JOIN rule r ON r.rule_id = rl.rule_id AND r.version = rl.version
                      ORDER BY rl.needs_confirm DESC, rl.rule_id""")
    rc = db.query("""SELECT rc.concept_id, oc.name AS concept_name, rc.role, count(*) n,
                            array_agg(rc.rule_id ORDER BY rc.rule_id) AS rules
                       FROM rule_concept rc
                       LEFT JOIN ontology_concept oc
                         ON oc.concept_id = rc.concept_id AND oc.version = %s
                      GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 40""", (v,))
    multi = db.query("""SELECT a.group_key, a.rule_ids, a.n_sources, a.evidence, a.verdict,
                               array_agg(DISTINCT s.source_kind) AS kinds
                          FROM onto_align a
                          JOIN doclib_rule_source s ON s.group_key = a.group_key
                         WHERE a.n_sources > 1
                         GROUP BY 1,2,3,4,5 ORDER BY a.n_sources DESC""")
    return {"ontology_version": v, "entity_relations": ent, "rule_relations": rr,
            "concept_refs": rc, "multi_source": multi,
            "counts": {"实体关系": len(ent), "规则关系": len(rr),
                       "概念引用": db.scalar("SELECT count(*) FROM rule_concept"),
                       "实体引用": db.scalar("SELECT count(*) FROM rule_entity"),
                       "待确认的冲突关系": sum(1 for x in rr if x["needs_confirm"])},
            "note": "规则↔规则的「冲突」挂着 needs_confirm 与缺口号——"
                    "**系统只发现冲突，不替客户拍板**，这就是那条口径在本体里的样子。"}


# ── 本体图谱 ────────────────────────────────────────────────────────────────
# 图谱不是关系表的美化版。**它要回答表格回答不了的一个问题：
# 哪个节点是枢纽、哪条边是悬着的。**
# 所以节点上带「概念数 / 可得性」，边上带基数与来源，
# 并且把「取不到字段的实体」显式标出来——
# 一个连不上数据的实体在表格里和别的实体长得一样，在图上一眼就看得出是孤点。
def graph():
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    ents = db.query("""SELECT e.entity_id, e.name AS entity_name, e.source,
                              count(c.concept_id) AS concepts,
                              sum(CASE WHEN c.availability = '可得' THEN 1 ELSE 0 END) AS avail,
                              sum(CASE WHEN c.availability = '不可得' THEN 1 ELSE 0 END) AS gap
                         FROM ontology_entity e
                         LEFT JOIN ontology_concept c
                           ON c.entity_id = e.entity_id AND c.version = e.version
                        WHERE e.version = %s
                        GROUP BY 1,2,3 ORDER BY 1""", (v,))
    rels = db.query("""SELECT from_entity, to_entity, rel_type, cardinality, source, note
                         FROM ontology_relation WHERE version = %s
                        ORDER BY from_entity, to_entity""", (v,))
    deg = {}
    for r in rels:
        deg[r["from_entity"]] = deg.get(r["from_entity"], 0) + 1
        deg[r["to_entity"]] = deg.get(r["to_entity"], 0) + 1
    for e in ents:
        e["degree"] = deg.get(e["entity_id"], 0)
        e["isolated"] = e["degree"] == 0

    # 规则关系子图：只画有关系的规则，孤立的 60 多条不画——
    # 全画出来是一片点云，看不出覆盖链
    rr = db.query("""SELECT rule_id, rel_type, other_rule, needs_confirm, gap_id
                       FROM rule_relation ORDER BY rule_id""")
    ids = {x["rule_id"] for x in rr} | {x["other_rule"] for x in rr
                                        if re.match(r"^TR-D\d+-\d+$", x["other_rule"] or "")}
    rnodes = db.query("""SELECT rule_id, name, dimension, control_type, rule_class,
                                compiled, demo_executable
                           FROM rule WHERE version = 1 AND rule_id = ANY(%s)
                          ORDER BY rule_id""", (sorted(ids),)) if ids else []
    return {"ontology_version": v, "entities": ents, "entity_edges": rels,
            "rule_nodes": rnodes, "rule_edges": rr,
            "note": "实体图谱的孤点（没有任何关系的实体）和规则图谱里指向"
                    "「制度 第十条」这类**非规则节点**的冲突边，是两种需要人看的东西："
                    "前者说明本体关系没建全，后者说明冲突的另一方还没成为规则。"}


# ── 存储 ────────────────────────────────────────────────────────────────────
# 每一行都要说清三件事：谁写的、重算会不会被清、为什么这么定。
# 页面上只给表名和行数没有意义——**人要知道的是「我点了重算，什么会消失」**。
STORAGE = [
    ("rule", "规则本体主表（八要素 + 配置器 4 字段）", "人 / 装载脚本", "不清",
     "装载脚本只删 created_by='asset_loader' 的行，采纳进来的规则保留。"
     "原来是无条件 DELETE FROM rule，下次装载会把人采纳的规则静默删掉"),
    ("rule_concept", "规则引用的本体概念（阈值位单独标 role）", "装载脚本", "随规则",
     "契约校验靠它连接规则与本体；阈值位的概念校验更严"),
    ("rule_entity", "规则引用的实体（存在性判断）", "装载脚本", "随规则",
     "与概念分两张表：「这类附件在不在」不是「某字段值是多少」"),
    ("rule_relation", "规则之间的覆盖 / 优先 / 冲突", "装载脚本 / 采纳", "随规则",
     "冲突行带 needs_confirm 与缺口号，不替客户拍板"),
    ("ontology_entity", "本体实体", "本体装载", "不清", "版本钉在 ontology_version 上"),
    ("ontology_concept", "本体概念（带可得性）", "本体装载", "不清",
     "可得性决定规则能不能进执行集"),
    ("ontology_relation", "实体之间的结构关系（含基数与来源）", "本体装载", "不清",
     "本体没有关系就只是一张清单"),
    ("doclib_rule_source", "归并组成员：同一条规则的多个出处", "L2 归并 Agent", "整表重算",
     "多源不是冗余，是证据强度"),
    ("doclib_conflict", "口径冲突", "L2 归并 Agent", "保留已忽略",
     "「已忽略」也要留行，否则反复提示同一件事，提示多了就没人看了"),
    ("onto_align", "制度 → 落地 对账结论", "L2 对齐 Agent", "整表重算",
     "算出来的，每次统一口径重算"),
    ("onto_rule_cover", "规则 → 依据 对账结论", "L2 对齐 Agent", "整表重算",
     "带外键：规则被删，结论就该跟着消失"),
    ("onto_adoption", "采纳 / 驳回 / 暂缓（人的动作）", "只有人", "永不清",
     "Agent 写不进这张表，也写不进 rule 表——WRITABLE_TABLES 白名单里没有它们，"
     "不是「约定不写」，是能力上就不给"),
]


def storage():
    rows = []
    for t, what, who, on_recalc, why in STORAGE:
        try:
            n = db.scalar("SELECT count(*) FROM " + t)
        except Exception:                                      # noqa: BLE001
            n = None
        rows.append({"table_name": t, "what": what, "written_by": who,
                     "on_recalc": on_recalc, "why": why, "rows": n})
    return {"items": rows,
            "note": "「重算时」这一列是这张表唯一必须看的东西：**点一次统一口径，"
                    "哪些行会消失**。算出来的整表重算，人拍的永不清——"
                    "混在一张表里，重算一次谁采纳过就全没了。"}


# ── 采纳入本体 ──────────────────────────────────────────────────────────────
DIMS = ["D%d" % i for i in range(1, 11)]
CLASSES = ("hard", "mixed", "soft")
CONTROLS = ("禁止", "警告")


def risk_classes():
    """风险类别取现有值，不新造。新造一个会让按风险类别统计的报表多出一档。"""
    return [r["risk_class"] for r in db.query(
        "SELECT risk_class, count(*) n FROM rule WHERE version = 1 "
        "GROUP BY 1 ORDER BY 2 DESC, 1")]


def pending_adoption(limit=100):
    """待采纳：没落地或判不出、且还没处置过的归并组，带建议。

    **建议不等于结论**：控制类型、规则类型、维度都要人选。
    自动给一个默认值的后果是人会直接点确认——而控制类型选错，
    就是把「提醒」变成「拦单」，或者反过来让一道闸静默失效。
    """
    rows = db.query("""
        SELECT a.*, (SELECT d.action FROM onto_adoption d
                      WHERE d.group_key = a.group_key
                      ORDER BY d.adopted_at DESC LIMIT 1) AS last_action
          FROM onto_align a
         WHERE a.verdict <> '已落地'
         ORDER BY a.has_conflict, a.n_sources DESC LIMIT %s""",
                    (max(1, min(300, int(limit))),))
    out = []
    for a in rows:
        if a["last_action"] in ("采纳", "驳回"):
            continue
        srcs = db.query("""SELECT s.source_kind, s.confidence, s.excerpt, c.clause_no,
                                  c.title, c.numbers, f.file_name
                             FROM doclib_rule_source s
                             JOIN doclib_clause c ON c.clause_id = s.clause_id
                             JOIN doc_lib_file  f ON f.file_id   = s.file_id
                            WHERE s.group_key = %s ORDER BY f.file_name, c.seq""",
                        (a["group_key"],))
        ctrls = {dx._control_of(s["excerpt"] or "") for s in srcs} - {None}
        a["sources"] = srcs
        a["suggest"] = {
            "name": a["group_key"].replace("·", ""),
            "control_type": list(ctrls)[0] if len(ctrls) == 1 else None,
            "control_why": ("组内 %d 条来源的控制类型判不出或不一致，必须人选" % len(srcs))
                           if len(ctrls) != 1 else "组内来源的控制类型一致",
            "numbers": sorted({n for s in srcs for n in (s["numbers"] or [])})[:12],
            "blocked_by_conflict": a["has_conflict"],
        }
        out.append(a)
    return out


def _next_rule_id(dim):
    """沿用现有编号口径 TR-D{n}-{nn}，取该维度现有最大号 +1。

    **不另起一套编号**：规则号是和客户配置器对账的锚点，
    新造一套前缀会让「这条规则在配置器里对应哪个算法」多出一层翻译。
    """
    mx = 0
    for r in db.query("SELECT rule_id FROM rule WHERE dimension = %s", (dim,)):
        m = re.match(r"^TR-%s-(\d+)$" % dim, r["rule_id"])
        if m:
            mx = max(mx, int(m.group(1)))
    return "TR-%s-%02d" % (dim, mx + 1)


class AdoptError(ValueError):
    pass


def adopt(group_key, by, form):
    """把一个归并组采纳成一条规则（status=draft、未编译）。

    **三条硬闸**，都返回明确的错误而不是落一条半成品：
      1. 有未裁决冲突的组不许采纳——口径没统一就采纳等于替客户拍板
      2. 控制类型 / 规则类型 / 维度 / 风险类别必须人填，不给默认值
      3. 软规则不许标禁止（库里也有 CHECK，但这里先拦，给人看得懂的话）
    """
    a = db.query_one("SELECT * FROM onto_align WHERE group_key = %s", (group_key,))
    if not a:
        raise AdoptError("没有这个归并组：%s。先跑一次统一口径" % group_key)
    if db.query_one("SELECT 1 FROM onto_adoption WHERE group_key = %s AND action = '采纳'",
                    (group_key,)):
        raise AdoptError("这个组已经采纳过了。重复采纳会在本体里长出两条同源规则，"
                         "对同一张单给出两条结论，而两条都「有制度依据」")
    n_conf = db.scalar("SELECT count(*) FROM doclib_conflict "
                       "WHERE group_key = %s AND state = '待裁决'", (group_key,))
    if n_conf:
        raise AdoptError("这个组有 %d 处口径冲突还没裁决。**口径没统一就采纳，"
                         "等于替客户把一方口径当成了结论**——先去冲突裁决，"
                         "或者标「暂缓」把理由记下来" % n_conf)
    if not (by or "").strip():
        raise AdoptError("采纳必须署名：没有署名的采纳在审计上等于没发生")

    name = (form.get("name") or "").strip()
    dim = (form.get("dimension") or "").strip()
    cls = (form.get("rule_class") or "").strip()
    ctrl = (form.get("control_type") or "").strip()
    risk = (form.get("risk_class") or "").strip()
    miss = [k for k, v in (("规则名称", name), ("审核维度", dim), ("规则类型", cls),
                           ("控制类型", ctrl), ("风险类别", risk)) if not v]
    if miss:
        raise AdoptError("这几项必须人填，系统不给默认值：%s。"
                         "控制类型选错就是把提醒变成拦单，或者让一道闸静默失效"
                         % "、".join(miss))
    if dim not in DIMS:
        raise AdoptError("审核维度只能是 %s" % "/".join(DIMS))
    if cls not in CLASSES:
        raise AdoptError("规则类型只能是 hard / mixed / soft")
    if ctrl not in CONTROLS:
        raise AdoptError("控制类型只能是 禁止 / 警告")
    if cls == "soft" and ctrl == "禁止":
        raise AdoptError("软规则不许标禁止：模型给的是提示，不是判决；"
                         "让模型硬拦单据等于把审批权责交给它")

    srcs = db.query("""SELECT s.source_kind, s.confidence, s.excerpt, c.clause_no, f.file_name
                         FROM doclib_rule_source s
                         JOIN doclib_clause c ON c.clause_id = s.clause_id
                         JOIN doc_lib_file  f ON f.file_id   = s.file_id
                        WHERE s.group_key = %s ORDER BY f.file_name""", (group_key,))
    if not srcs:
        raise AdoptError("这个组没有来源条款，采纳出来的规则会没有证据链")
    # 来源条款取**可信度最高**的那条（原生文本优于 OCR），其余进 also_source。
    # 这不是随便挑一条：source_clause 是八要素里的④，它决定这条规则
    # 将来能不能当硬阈值用——OCR 来源的数值不许当硬阈值（契约校验卡着）。
    srcs.sort(key=lambda s: 0 if s["confidence"] == "原生文本" else 1)
    first, rest = srcs[0], srcs[1:]
    sc = {"file": first["file_name"], "clause": first["clause_no"],
          "text": (first["excerpt"] or "")[:500], "confidence": first["confidence"]}
    also = [{"file": s["file_name"], "clause": s["clause_no"],
             "text": (s["excerpt"] or "")[:500], "confidence": s["confidence"]}
            for s in rest]

    rid = _next_rule_id(dim)
    logic = {"kind": "待编译", "group_key": group_key,
             "note": "采纳入本体时未编译：判定逻辑与概念绑定待规则管理员补。"
                     "未声明 concepts，契约校验会把它列进剔除名单并写明原因"}
    scope = {"待细化": "采纳时未细化适用范围",
             "来源": [s["file_name"] + " " + s["clause_no"] for s in srcs[:6]]}
    reason = ("采纳入本体时未编译：判定逻辑与概念绑定待规则管理员补"
              + ("；来源可信度为 OCR，数值不得直接当硬阈值"
                 if first["confidence"] == "OCR" else ""))
    db.execute("""INSERT INTO rule(rule_id, version, name, dimension, risk_class, rule_class,
                      source_clause, also_source, scope, logic, exception, priority, owner,
                      control_type, configurator_status, compiled, compiled_reason,
                      demo_executable, demo_blocker, note, status, created_by)
                  VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'建议新增',FALSE,%s,
                          FALSE,'未编译',%s,'draft',%s)""",
               (rid, name, dim, risk, cls,
                json.dumps(sc, ensure_ascii=False), json.dumps(also, ensure_ascii=False),
                json.dumps(scope, ensure_ascii=False), json.dumps(logic, ensure_ascii=False),
                (form.get("exception") or None), int(form.get("priority") or 50),
                (form.get("owner") or None), ctrl, reason,
                "由统一口径采纳：归并组 %s，%d 个出处" % (group_key, len(srcs)),
                "onto_adopt:" + by.strip()))
    # 已裁决的冲突落成规则关系：**裁决结论要留在本体里**，
    # 不然三个月后看这条规则，只看到一个口径，看不到当时还有另一个。
    for cf in db.query("""SELECT kind, detail, resolution, resolved_by FROM doclib_conflict
                           WHERE group_key = %s AND state = '已裁决'""", (group_key,)):
        db.execute("""INSERT INTO rule_relation(rule_id, version, rel_type, other_rule,
                          note, needs_confirm, gap_id)
                      VALUES (%s,1,'conflicts_with',%s,%s,FALSE,NULL)""",
                   (rid, (cf["kind"] or "")[:60],
                    json.dumps({"detail": cf["detail"], "resolution": cf["resolution"],
                                "resolved_by": cf["resolved_by"]}, ensure_ascii=False)))
    aid = "ado-" + uuid.uuid4().hex[:10]
    db.execute("""INSERT INTO onto_adoption(adoption_id, group_key, action, rule_id,
                      rule_version, adopted_by) VALUES (%s,%s,'采纳',%s,1,%s)""",
               (aid, group_key, rid, by.strip()))
    return {"adoption_id": aid, "rule_id": rid, "group_key": group_key,
            "sources": len(srcs), "status": "draft",
            "note": "已写入本体：status=draft、未编译、不在执行集。"
                    "**采纳入本体 ≠ 发布生效**，发布是栏目五的事，且已发布的 release"
                    "必须有签字人（0005 的 ck_release_signed 卡着）"}


def dispose(group_key, action, by, reason):
    """驳回 / 暂缓。两者都必须写理由（库里也有 CHECK 兜着）。"""
    if action not in ("驳回", "暂缓"):
        raise AdoptError("只能是「驳回」或「暂缓」；采纳走 adopt()")
    if not (by or "").strip():
        raise AdoptError("必须署名")
    if not (reason or "").strip():
        raise AdoptError("必须写理由：「为什么不采纳」比「采纳了什么」更容易被时间冲掉，"
                         "三个月后再看这个组，没有理由就只剩一个标签")
    if not db.query_one("SELECT 1 FROM onto_align WHERE group_key = %s", (group_key,)):
        raise AdoptError("没有这个归并组：%s" % group_key)
    aid = "ado-" + uuid.uuid4().hex[:10]
    db.execute("""INSERT INTO onto_adoption(adoption_id, group_key, action, adopted_by, reason)
                  VALUES (%s,%s,%s,%s,%s)""", (aid, group_key, action, by.strip(), reason))
    return {"adoption_id": aid, "group_key": group_key, "action": action}


def list_adoptions(limit=200):
    return db.query("""SELECT d.*, r.name AS rule_name, r.status AS rule_status
                         FROM onto_adoption d
                         LEFT JOIN rule r ON r.rule_id = d.rule_id AND r.version = 1
                        ORDER BY d.adopted_at DESC LIMIT %s""",
                    (max(1, min(500, int(limit))),))


# ── L0 感知用的素材 ─────────────────────────────────────────────────────────
def perceive():
    """抽取那一栏交过来多少、本体这边统过多少。**差额就是这一栏要干的活。**"""
    return {
        "条款": db.scalar("SELECT count(*) FROM doclib_clause"),
        "AI草稿": db.scalar("SELECT count(*) FROM doclib_draft"),
        "已归并来源": db.scalar("SELECT count(*) FROM doclib_rule_source"),
        "已对齐组": db.scalar("SELECT count(*) FROM onto_align"),
        "待裁决冲突": db.scalar("SELECT count(*) FROM doclib_conflict WHERE state = '待裁决'"),
        "本体规则": db.scalar("SELECT count(*) FROM rule WHERE version = 1"),
    }
