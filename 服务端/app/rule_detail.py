# -*- coding: utf-8 -*-
"""规则明细：把抽出来的东西变成**一行一条规则、要素成列**。

这一栏叫「规则抽取」，但在这个模块之前，它的产物是 185 条**条款**——
文本切片，不是规则。实测这条链对规则库的净贡献是 0 条
（82 条规则全是 created_by='asset_loader'，onto_adoption 一条没有）。

两路来源，进**同一张**明细表（页面上必须是一张清单，两张会让人问
「哪张才是抽出来的规则」），但可信度不是一档：

  列映射   表格行文件按**表头名**映射。确定性、不出网、不计费。
  AI草稿   模型从条款原文抽的八要素。要过数值幻觉核对，逐条人核。

为什么列映射要优先：《差旅费审核规则.xlsx》的表头就是
  序号 | 校验类型 | 校验规则 | 实现途径 | 校验方式 | 是否已有 | 评估共享是否可实现
那条唯一成功的付费草稿（58 秒）给出的「规则名称=交通费标准控制」和
「处置动作=禁止」，**原本就躺在第 2 列和第 5 列**；而模型给不出的
「实现途径=系统规则」「是否已有=是」反倒被丢掉了。
把结构化数据降维成一行文本、再花钱让模型升维回来，是这一栏最贵的一处浪费。

**映射最危险的不是读错字，是列错位。** 实扫两处：
  《系统审核算法.xlsx》   表头 15 列、数据行 14 列（「审核算法规则」是空列）
                          按位置硬映射 → 「控制类型」读到「自定义」、「状态」读到时间
  《差旅费审核规则.xlsx》 第 2 行「是否已有」空着，评估意见整体左移一列
所以这里不按位置硬映射，而是**用闭集合列当锚**（控制类型只能是禁止/警告、
状态只能是有效/无效、实现途径只能是那几种），锚对不上就说对不上。
能唯一确定对齐方式的才映射，确定不了的留原文交人看——
**静默丢掉是漏报，硬映射是误报，两者同样致命。**
"""
import datetime
import hashlib
import itertools
import json

from . import db, doclib_extract as dx

# ── 表头名 → 明细字段 ───────────────────────────────────────────────────────
# 一个字段可以有多个表头名（两份表的叫法不同），**先到的非空值赢**。
# 没列进来的表头（序号、创建人、创建时间…）不是漏了，是故意不要：
# 它们是台账簿记，不是规则要素。
HEADER_ALIAS = {
    "校验类型": "rule_name",
    "算法名称": "rule_name",
    "规则名称": "rule_name",
    "校验规则": "logic_text",
    "校验规则（需要进一步细化）": "logic_text",
    "算法描述": "logic_text",
    "审核算法规则": "logic_text",
    "判定逻辑": "logic_text",
    "实现途径": "impl_path",
    "配置方式": "impl_path",
    "校验方式": "control_type",
    "控制类型": "control_type",
    "是否已有": "feasibility",
    "算法编码": "algo_code",
    "算法类型": "algo_type",
    "状态": "configurator_status",
    "适用范围": "scope_text",
    "例外情形": "exception_text",
    "风险等级": "risk_text",
    # 这几列收进 note：它们是判断用得上的旁证，但不是八要素里的任何一项。
    # 丢掉就得回头再读一次原表（抽取的第一条纪律：不丢列）。
    "评估共享是否可实现": "note",
    "算法分类": "note",
    "算法来源": "note",
    "备注": "note",
}

# 闭集合锚。**这是防错位的全部依据**：这些列的取值范围是已知的，
# 一旦读到范围外的值，说明对齐错了，而不是「这个单元格的内容比较特别」。
ANCHORS = {
    "control_type": ("禁止", "警告"),
    "impl_path": ("系统规则", "配置器", "人工", "手工", "系统规则+人工", "系统"),
    "configurator_status": ("有效", "无效"),
    "feasibility": ("是", "否", "已有", "无", "部分", "部分已有"),
}

MAX_DROP = 2       # 最多允许表头比数据行多几列（再多就不猜了，见 _align）

# 模型给的中文取值 → 本体的闭集合。对不上的**不硬塞**，进 note 并标出来。
CLASS_MAP = {"硬": "hard", "混合": "mixed", "软": "soft",
             "hard": "hard", "mixed": "mixed", "soft": "soft"}
DIMS = tuple("D%d" % i for i in range(1, 11))


def _kind_of_file(file_name):
    """这份文件是哪一类规则来源。**按文件名判，并且只认这四类。**

    和 rule.source_clause.file 里那几个类别代号对得上（规则表/配置器/场景表/制度），
    将来做「代号→文件」映射时两边说的是同一件事。
    """
    n = file_name or ""
    if "算法" in n:
        return "配置器算法"
    if "场景" in n:
        return "场景表"
    if "审核规则" in n or "规则" in n:
        return "规则表"
    return "制度条款"


def _put(fields, col_map, head, val):
    """把一列的值放进字段。**先到的非空值赢**，并逐列留痕到 col_map。"""
    col_map[head] = val
    tgt = HEADER_ALIAS.get(head)
    if not tgt or not (val or "").strip():
        return
    if tgt == "note":
        fields["note"] = ((fields.get("note") or "")
                          + ("；" if fields.get("note") else "")
                          + "%s=%s" % (head, val.strip()))
        return
    if not (fields.get(tgt) or "").strip():
        fields[tgt] = val.strip()


def _project(header, cols, keep):
    """按一种对齐方式把一行投影成字段。keep 是「用上的表头下标」。"""
    fields, col_map = {}, {}
    for i, h in zip(keep, cols):
        _put(fields, col_map, header[i], h)
    return fields, col_map


def _violations(fields):
    """闭集合列里，取值落在范围外的那些。→ [(字段, 原值, 允许值)]

    **空值不算违反**——空只是没填。

    这个函数有两个用处，分清很重要（第一版混成了一个，判错了 7 行）：
      一、**定对齐**：列数和表头不一致时，哪种对齐方式违反得最少，就是它。
      二、**说取值**：对齐没问题、但某一格的值不在闭集合里，
          那是**源表这一格本身的事**，不是结构错。
    实扫《差旅费审核规则.xlsx》的「实现途径」列，客户自己填的就是
    「？」「通过OA控制？」「AI智能审核？」——待定，不是错位。
    把它判成「列错位」会把 7 条真规则挡在外面，而真正该做的是
    **把这一列留空、把原值写在旁边，让人来定**。
    """
    out = []
    for k, allowed in ANCHORS.items():
        v = (fields.get(k) or "").strip()
        if v and v not in allowed:
            out.append((k, v, allowed))
    return out


def _align(header, cols):
    """→ (fields, col_map, ok, warning, value_notes)

    先把列对齐（结构），再说取值（内容）。两件事分开报：
      ok=False      列对不齐，没映射，留原文交人看
      value_notes   列对齐了，但某几格的值不在闭集合里——该列留空，原值记下来

    对齐的挑法：把「尾部截断」和「中间空几列」都算一遍，
    取**闭集合违反最少**的那种；并列且结果不同就不替人选。
    唯一性判在**投影结果**上，不在对齐方式上：实扫《系统审核算法.xlsx》
    「算法描述」和「审核算法规则」都映到 logic_text，
    两种对齐给出的明细一模一样——那不算歧义。
    """
    nh, nc = len(header), len(cols)
    if nc > nh:
        return None, {str(i): c for i, c in enumerate(cols)}, False, (
            "本行 %d 列、表头 %d 列：列比表头还多，单元格里可能本来就有竖线" % (nc, nh)
        ), []

    cands = []          # (违反数, fields, col_map, 说明)
    trailing = None     # 尾部截断那一种，单独留着：并列时它优先，见下
    if nc == nh:
        f, cm = _project(header, cols, range(nh))
        cands.append((len(_violations(f)), f, cm, None))
    else:
        gap = nh - nc
        if gap > MAX_DROP:
            return None, {str(i): c for i, c in enumerate(cols)}, False, (
                "本行 %d 列、表头 %d 列，差 %d 列：**不猜**。"
                "差一两列还能靠闭集合定出唯一对齐，差更多只能靠猜，"
                "而猜错一列会让控制类型或阈值读到隔壁去" % (nc, nh, gap)), []
        # 尾部截断（Excel 导出最常见的情形）
        f, cm = _project(header, cols, range(nc))
        trailing = (len(_violations(f)), f, cm,
                    "本行 %d 列、表头 %d 列，缺的是末尾「%s」——按尾部空列处理"
                    % (nc, nh, "、".join(header[nc:])))
        cands.append(trailing)
        # 中间空了几列
        for drop in itertools.combinations(range(nh), gap):
            if drop == tuple(range(nc, nh)):
                continue                       # 就是上面那种，不重复
            keep = [i for i in range(nh) if i not in drop]
            f2, cm2 = _project(header, cols, keep)
            cands.append((len(_violations(f2)), f2, cm2,
                          "本行 %d 列、表头 %d 列；按闭集合校验，「%s」是空列，已按此映射"
                          % (nc, nh, "、".join(header[i] for i in drop))))

    best = min(c[0] for c in cands)
    # **并列时尾部截断优先。** 这不是口味问题：Excel 导出会裁掉尾部空列，
    # 这个机制是已知的；而「中间某一列恰好空着」是没有证据的假设。
    # 不给它优先权的代价实测过：7 条干干净净的 6 列行（尾部「评估共享」空着）
    # 和「是否已有 空着」那种假设打成平手，于是被判成「两种对齐都说得通」挡下来——
    # 一条真规则因为一个假设被挡住，就是漏报。
    if trailing is not None and trailing[0] == best:
        top = [trailing]
    else:
        top = [c for c in cands if c[0] == best]
    distinct = []
    for c in top:
        if not any(c[1] == d[1] for d in distinct):
            distinct.append(c)
    if len(distinct) > 1:
        return None, {str(i): c for i, c in enumerate(cols)}, False, (
            "本行 %d 列、表头 %d 列，有 %d 种对齐都说得通但结果不一致，**不替人选**"
            % (nc, nh, len(distinct))), []

    _n, fields, col_map, warn = distinct[0]
    notes = []
    for k, v, allowed in _violations(fields):
        # **这一列留空**，原值记进 note。硬塞一个最接近的值进去，
        # 后面会被当成事实——而它只是「看起来最像」。
        fields[k] = None
        notes.append("%s 原文是「%s」，不在 %s 里，已留空待人定"
                     % (k, v[:40], "/".join(allowed)))
    return fields, col_map, True, warn, notes


def _detail_id(clause_id, origin):
    return "drd-" + hashlib.md5(("%s|%s" % (clause_id, origin))
                                .encode("utf-8")).hexdigest()[:12]


def _save(d):
    """UPSERT。detail_id = md5(clause_id|origin)，重跑还是同一行，

    所以人工的采纳/驳回结论留得住（和条款那边同一条口径）。
    """
    db.execute("""
        INSERT INTO doclib_rule_detail(detail_id, clause_id, file_id, origin, source_kind,
            rule_name, dimension, rule_class, control_type, scope_text, logic_text,
            exception_text, risk_text, impl_path, feasibility, algo_code, algo_type,
            configurator_status, numbers, note, col_map, mapped_ok, map_warning,
            confidence, model, draft_id)
        VALUES (%(detail_id)s,%(clause_id)s,%(file_id)s,%(origin)s,%(source_kind)s,
            %(rule_name)s,%(dimension)s,%(rule_class)s,%(control_type)s,%(scope_text)s,
            %(logic_text)s,%(exception_text)s,%(risk_text)s,%(impl_path)s,%(feasibility)s,
            %(algo_code)s,%(algo_type)s,%(configurator_status)s,%(numbers)s,%(note)s,
            %(col_map)s,%(mapped_ok)s,%(map_warning)s,%(confidence)s,%(model)s,
            %(draft_id)s)
        ON CONFLICT (detail_id) DO UPDATE SET
            rule_name=EXCLUDED.rule_name, dimension=EXCLUDED.dimension,
            rule_class=EXCLUDED.rule_class, control_type=EXCLUDED.control_type,
            scope_text=EXCLUDED.scope_text, logic_text=EXCLUDED.logic_text,
            exception_text=EXCLUDED.exception_text, risk_text=EXCLUDED.risk_text,
            impl_path=EXCLUDED.impl_path, feasibility=EXCLUDED.feasibility,
            algo_code=EXCLUDED.algo_code, algo_type=EXCLUDED.algo_type,
            configurator_status=EXCLUDED.configurator_status,
            numbers=EXCLUDED.numbers, note=EXCLUDED.note, col_map=EXCLUDED.col_map,
            mapped_ok=EXCLUDED.mapped_ok, map_warning=EXCLUDED.map_warning,
            confidence=EXCLUDED.confidence, model=EXCLUDED.model,
            draft_id=EXCLUDED.draft_id""", d)


def _blank(clause, origin, source_kind):
    return {"detail_id": _detail_id(clause["clause_id"], origin),
            "clause_id": clause["clause_id"], "file_id": clause["file_id"],
            "origin": origin, "source_kind": source_kind,
            "rule_name": None, "dimension": None, "rule_class": None,
            "control_type": None, "scope_text": None, "logic_text": None,
            "exception_text": None, "risk_text": None, "impl_path": None,
            "feasibility": None, "algo_code": None, "algo_type": None,
            "configurator_status": None, "numbers": list(clause["numbers"] or []),
            "note": None, "col_map": None, "mapped_ok": True, "map_warning": None,
            "confidence": clause.get("text_source") or "原生文本",
            "model": None, "draft_id": None}


# ── 第一路：表格行按列映射（确定性，不出网、不计费）─────────────────────────
def from_table_rows(file_ids=None):
    """把「表格行」策略切出来的条款按表头映射成规则明细。

    重新解析一次文件正文，是为了拿到**表头和逐列原值**——
    doclib_clause 里只存了整行文本。解析用的是 doclib_extract.split_table_rows
    同一段代码，不是另写一个：两份实现会各自走偏，
    到那时「明细第 5 行」和「条款第 5 行」就不是同一行了，
    而证据链全靠这个对应关系。
    """
    sql = ["""SELECT file_id, file_name, body FROM doc_lib_file
               WHERE NOT missing AND body IS NOT NULL"""]
    args = []
    # **file_ids=[]（空列表）表示「没有要处理的文件」，不是「全部」。**
    # 第一版是 `if file_ids:`，于是增量集合为空时过滤条件整个被跳过，
    # 一次「增量 0 份」变成全量重映射。同类陷阱在 doclib_extract 的
    # run_split 与 run_ai 里各有一处，一并修了（都有单测钉住）。
    if file_ids is not None:
        sql.append("AND file_id = ANY(%s)"); args.append(list(file_ids))
    files = db.query(" ".join(sql), tuple(args) if args else None)

    made = flagged = 0
    per_file = []
    for f in files:
        rows = dx.split_table_rows(f["body"])
        if not rows:
            continue
        kind = _kind_of_file(f["file_name"])
        seen_no, n_ok, n_bad, notes = {}, 0, 0, []
        for r in rows:
            k = ("表格行", r["clause_no"])
            seen_no[k] = seen_no.get(k, 0) + 1
            cid = dx.clause_ident(f["file_id"], "表格行", r["clause_no"], seen_no[k])
            clause = db.query_one("SELECT * FROM doclib_clause WHERE clause_id=%s", (cid,))
            if not clause:
                # 这一行在 doclib_clause 里没有对应条款：说明这份文件当初
                # 选的不是「表格行」策略（pick_strategy 按条数挑）。
                # **不在这里补条款**——那会绕过切分那一步的全部口径。
                continue
            d = _blank(clause, "列映射", kind)
            if not r.get("header"):
                d["mapped_ok"] = False
                d["map_warning"] = ("这张工作表没认出表头行，无法按列映射。"
                                    "没有表头就只能按位置猜，而猜错一列就是"
                                    "把控制类型读到隔壁去")
                d["rule_name"] = (r.get("title") or r["body"])[:60]
                d["col_map"] = json.dumps({"原行": r["body"]}, ensure_ascii=False)
                _save(d); n_bad += 1; continue
            fields, col_map, ok, warn, notes = _align(r["header"], r["cols"])
            d["col_map"] = json.dumps(col_map, ensure_ascii=False)
            d["mapped_ok"] = bool(ok)
            d["map_warning"] = warn
            if ok:
                for key, val in fields.items():
                    d[key] = val
                # 控制类型 → 规则类型只在「禁止」这一侧能确定：
                # 禁止一定是硬规则（它要拦单），警告可能是硬也可能是软，判不出就留空。
                if d["control_type"] == "禁止":
                    d["rule_class"] = "hard"
                # 取值不在闭集合的那几格：列是对的，值待定。进 note，不进字段。
                if notes:
                    d["note"] = ((d.get("note") or "")
                                 + ("；" if d.get("note") else "") + "；".join(notes))
                n_ok += 1
            else:
                d["rule_name"] = (r.get("title") or r["body"])[:60]
                n_bad += 1
            if not (d["rule_name"] or "").strip():
                # 「校验类型」那一格空着的行（实扫第 13 行就是）：拿判定逻辑当名字，
                # 并说明是补的。body 以「 | 」开头（空格子），要剥掉，
                # 否则清单上会出现一条叫「| 单人出差须本人自行提单…」的规则。
                alt = (d.get("logic_text") or r.get("title") or r["body"] or "")
                d["rule_name"] = "（未命名）" + alt.strip(" |　")[:50]
                d["note"] = ((d.get("note") or "")
                             + ("；" if d.get("note") else "")
                             + "源表「校验类型」这一格是空的，名称按判定逻辑临时补的，需人工定名")
            _save(d)
        if n_ok or n_bad:
            per_file.append({"file_id": f["file_id"], "file_name": f["file_name"],
                             "source_kind": kind, "mapped": n_ok, "flagged": n_bad,
                             "note": "；".join(notes) or None})
            made += n_ok; flagged += n_bad
    return {"mapped": made, "flagged": flagged, "per_file": per_file,
            "note": "列映射不出网、不计费、不经过模型。"
                    "标红的那些是**列对不齐**（表头 N 列、数据行 M 列，或闭集合列"
                    "读到了范围外的值），留了原文和逐列留痕，等人看一眼——"
                    "硬映射会让控制类型或阈值读到隔壁列去。"}


# ── 第二路：场景表的「专用审核要点」拆条 ────────────────────────────────────
def from_scenario(file_ids=None):
    """场景表 → 规则明细。确定性，不出网、不计费。

    和列映射分开成两路（origin='场景拆条'），因为证据链长相不同：
    列映射是**一行一条规则**，场景拆条是**多行去重后按条内序号拆**——
    一条规则背后可能是 142 行、11 个业务小类，这个覆盖范围必须记在明细上。
    混成一档，页面上就看不出哪条规则背后是 1 行、哪条背后是 142 行。

    源表**没有规则名这一列**，所以名称按要点首句生成，并在 note 里说明需人工定名。
    控制类型、规则类型、风险类别源表也没有——**留空，不猜**，
    清单上的「要素完整度」标签会直接显示还差哪几项。
    """
    sql = ["""SELECT file_id, file_name, body FROM doc_lib_file
               WHERE NOT missing AND body IS NOT NULL"""]
    args = []
    # **file_ids=[]（空列表）表示「没有要处理的文件」，不是「全部」。**
    # 第一版是 `if file_ids:`，于是增量集合为空时过滤条件整个被跳过，
    # 一次「增量 0 份」变成全量重映射。同类陷阱在 doclib_extract 的
    # run_split 与 run_ai 里各有一处，一并修了（都有单测钉住）。
    if file_ids is not None:
        sql.append("AND file_id = ANY(%s)"); args.append(list(file_ids))
    files = db.query(" ".join(sql), tuple(args) if args else None)

    made = 0
    per_file = []
    for f in files:
        rows = dx.split_scenario_rows(f["body"])
        if not rows:
            continue
        seen_no, n = {}, 0
        for r in rows:
            k = ("场景拆条", r["clause_no"])
            seen_no[k] = seen_no.get(k, 0) + 1
            cid = dx.clause_ident(f["file_id"], "场景拆条", r["clause_no"], seen_no[k])
            clause = db.query_one("SELECT * FROM doclib_clause WHERE clause_id=%s", (cid,))
            if not clause:
                continue
            d = _blank(clause, "场景拆条", "场景表")
            d["rule_name"] = (r.get("title") or r["body"][:24]).strip()
            d["logic_text"] = r["body"]
            d["scope_text"] = "%s｜业务小类：%s" % (
                r["doc_type"], "、".join(r["sub_classes"][:8])
                + ("…" if len(r["sub_classes"]) > 8 else ""))
            d["note"] = ("覆盖 %d 行场景、%d 个业务小类；"
                         "名称按要点首句生成（源表没有规则名列），需人工定名"
                         % (r["row_count"], len(r["sub_classes"])))
            d["col_map"] = json.dumps({"系统单据": r["doc_type"],
                                       "业务小类": "、".join(r["sub_classes"]),
                                       "专用审核要点": r["body"],
                                       "覆盖行数": str(r["row_count"])},
                                      ensure_ascii=False)
            _save(d)
            n += 1
        if n:
            made += n
            per_file.append({"file_id": f["file_id"], "file_name": f["file_name"],
                             "source_kind": "场景表", "mapped": n, "flagged": 0,
                             "note": "另有 %d 行是其他单据类型（不在差旅+接待范围内），"
                                     "本次没取——**说出来而不是静默跳过**，"
                                     "否则下一个人会以为这张表只有这些"
                                     % rows[0]["skipped_rows"]})
    return {"mapped": made, "flagged": 0, "per_file": per_file,
            "note": "场景表的「专用审核要点」按正文去重、再按条内序号拆。"
                    "一条规则背后往往是几十到上百行场景，覆盖范围记在每条的 note 里。"}


# ── 第三路：把 AI 草稿投影进同一张明细表 ────────────────────────────────────
def from_drafts():
    """已成稿的 AI 草稿 → 规则明细。**只投影，不重新调模型。**

    模型给的取值不一定落在本体的闭集合里：实测两次调用给出的「审核维度」
    是「差旅费报销」和「人员身份，审批手续，交通住宿补贴，报销频次」，
    而本体的维度是 D1..D10。**对不上的不硬塞**，原值进 note 并标出来——
    硬塞会让一条维度错的规则看起来填得整整齐齐。
    """
    rows = db.query("""SELECT d.*, c.file_id, c.numbers, c.text_source, c.clause_id
                         FROM doclib_draft d JOIN doclib_clause c ON c.clause_id=d.clause_id
                        WHERE d.rulable IS TRUE
                        ORDER BY d.created_at""")
    n = 0
    for r in rows:
        draft = r["draft"] or {}
        clause = {"clause_id": r["clause_id"], "file_id": r["file_id"],
                  "numbers": r["numbers"], "text_source": r["text_source"]}
        d = _blank(clause, "AI草稿", "制度条款")
        d["rule_name"] = (draft.get("规则名称") or "")[:200] or "（模型未给名称）"
        d["logic_text"] = draft.get("判定逻辑")
        d["scope_text"] = draft.get("适用范围")
        d["exception_text"] = draft.get("例外情形")
        d["risk_text"] = draft.get("风险等级")
        d["model"] = r["model"]
        d["draft_id"] = r["draft_id"]
        ctrl = (draft.get("处置动作") or "").strip()
        d["control_type"] = ctrl if ctrl in ("禁止", "警告") else None
        d["rule_class"] = CLASS_MAP.get((draft.get("规则类型") or "").strip())
        dim = (draft.get("审核维度") or "").strip()
        d["dimension"] = dim if dim in DIMS else None
        bits = []
        if draft.get("缺口"):
            bits.append("缺口=%s" % draft["缺口"])
        if dim and dim not in DIMS:
            bits.append("模型给的审核维度「%s」不在 D1-D10 里，没有采用" % dim[:40])
        if ctrl and ctrl not in ("禁止", "警告"):
            bits.append("模型给的处置动作「%s」不是禁止/警告，没有采用" % ctrl[:20])
        chk = r["number_check"] or {}
        if chk.get("suspect"):
            bits.append("数值存疑：%s（这些数字在条款原文里找不到，必须回查原文）"
                        % "、".join("%s=%s" % (s.get("field"), s.get("value"))
                                   for s in chk["suspect"][:6]))
        d["note"] = "；".join(bits) or None
        # 模型这一路没有「列」，col_map 记它实际拿到的八要素原样，同样是留痕。
        d["col_map"] = json.dumps(draft, ensure_ascii=False)
        _save(d)
        n += 1
    return {"projected": n,
            "note": "AI 草稿投影进同一张明细表，但**可信度不是一档**："
                    "列映射是确定性的，模型这一路要逐条核数值、核闭集合。"}


def run(file_ids=None):
    """三路都跑一遍。**不出网**（from_drafts 只读已有草稿，不调模型）。

    `file_ids` 限定前两路（确定性列映射与场景拆条）。
    **第三路 from_drafts 刻意不限定**：它把「已采纳的草稿」投影进明细表，
    而采纳是人在别的时候点的，和这次增量是哪几份文件没有关系。
    限定了的后果是：上一轮采纳的草稿要等到它那份文件再次变动才会进明细表——
    而那可能永远不会发生，表现是「我明明采纳了，明细里没有」。
    它是幂等的 UPSERT，重复跑不会多产出。

    `file_ids=[]`（空列表）表示「没有要处理的文件」，不是「全部」，见下面两路的注释。
    """
    a = from_table_rows(file_ids)
    sc = from_scenario(file_ids)
    b = from_drafts()
    # 两路确定性映射合并上报：页面上「列映射」这一栏要能对上明细条数
    table = {"mapped": a["mapped"] + sc["mapped"],
             "flagged": a["flagged"] + sc["flagged"],
             "per_file": a["per_file"] + sc["per_file"],
             "note": a["note"] + " " + sc["note"]}
    return {"table": table, "scenario": sc, "draft": b,
            "total": db.scalar("SELECT count(*) FROM doclib_rule_detail") or 0}


# ── 出处与标签 ──────────────────────────────────────────────────────────────
# 列表上一行三块：**规则 / 出处 / 分类（标签）**。
#
# 出处不是「文件名」三个字就够。要能回答「凭什么说制度这么规定」，
# 必须同时给出：哪份文件 → 哪一条（行）→ 原文怎么写的 → 这段文字多可信。
# 少了最后一项就会出事：OCR 来的数字和原生文本的数字在页面上长得一模一样，
# 而项目纪律是只有原生文本与人眼读图能进硬阈值。
def source_of(r):
    return {"file_name": r["file_name"], "clause_no": r["clause_no"],
            "excerpt": (r["clause_body"] or "")[:300],
            "confidence": r["confidence"], "strategy": r["split_strategy"],
            "source_kind": r["source_kind"],
            "origin": r["origin"],
            "model": r["model"]}


# 闭集合的七个标签维度。**全部由明细的列派生，不另存**——
# 同一个事实存两处必然对不上（文件类别就这么错过一次：
# 文件库判一套、抽取判一套，清单上出现「文件库说单据、抽取说制度」）。
TAG_DIMS = (
    ("费用维度", "dimension", "本体的 10 个维度（D1..D10）"),
    ("控制类型", "control_type", "规则表「校验方式」列 / 算法表「控制类型」列"),
    ("规则类型", "rule_class", "禁止⇒硬（这一侧能确定）；其余由人填"),
    ("实现途径", "impl_path", "「实现途径」/「配置方式」列"),
    ("落地状态", "configurator_status", "算法表「状态」列（有效/无效）"),
)

# 进本体必须有人填的四项（和 onto_coord.adopt 的硬闸一致）。
# **在清单上就说清还差什么**，不要等人点了采纳才报错。
MUST_FOR_ADOPT = (("审核维度", "dimension"), ("规则类型", "rule_class"),
                  ("控制类型", "control_type"), ("风险类别", "risk_text"))


def tags_of(r, free=None):
    """派生标签。每个标签都带 src（凭什么）和 sure（确定性还是待人填）。

    **没有依据的标签等于没有**：标签是用来筛选的，筛错了就抽错样本，
    而抽错的样本会被当成结论。
    """
    out = [{"dim": "来源类别", "value": r["source_kind"],
            "src": "按文件名归类，对齐 rule.source_clause.file 那几个代号",
            "sure": True},
           {"dim": "可信度", "value": r["confidence"],
            "src": "跟着条款的文本来源走（原生文本 / OCR）",
            "sure": True}]
    for name, col, src in TAG_DIMS:
        v = (r.get(col) or "").strip()
        if not v:
            continue
        out.append({"dim": name, "value": v, "src": (
            "模型给的，已校过闭集合" if r["origin"] == "AI草稿" else "列映射：" + src),
            "sure": r["origin"] == "列映射"})
    miss = [n for n, c in MUST_FOR_ADOPT if not (r.get(c) or "").strip()]
    out.append({"dim": "要素完整度",
                "value": "可进本体" if not miss else "缺 %d 项：%s" % (
                    len(miss), "、".join(miss)),
                "src": "按「规则本体」采纳的硬闸反推，不是人填的",
                "sure": True})
    for t in (free or []):
        out.append({"dim": "人工标签", "value": t["tag"],
                    "src": "%s：%s" % (t["tagged_by"], t["why"]), "sure": True})
    return out, miss


def add_tag(detail_id, tag, by, why):
    """人工标签。**署名和理由都不许空**（库里也有 CHECK）。"""
    if not (tag or "").strip():
        raise ValueError("标签不能为空")
    if not (by or "").strip():
        raise ValueError("打标签必须署名")
    if not (why or "").strip():
        raise ValueError("必须写为什么打这个标签——没有依据的标签等于没有")
    db.execute("""INSERT INTO doclib_detail_tag(detail_id, tag, tagged_by, why)
                  VALUES (%s,%s,%s,%s)
                  ON CONFLICT (detail_id, tag) DO UPDATE SET
                      tagged_by=EXCLUDED.tagged_by, why=EXCLUDED.why,
                      tagged_at=now()""",
               (detail_id, tag.strip(), by.strip(), why.strip()))
    return True


def del_tag(detail_id, tag):
    return db.execute("DELETE FROM doclib_detail_tag WHERE detail_id=%s AND tag=%s",
                      (detail_id, tag)) > 0


def tag_cloud():
    """人工标签都有哪些、各打了几条。给筛选器用。"""
    return db.query("""SELECT tag, count(*) AS n FROM doclib_detail_tag
                        GROUP BY tag ORDER BY n DESC, tag""")


# ── 清单 / 统计 / 审定 ──────────────────────────────────────────────────────
def list_details(origin=None, state=None, mapped=None, file_id=None, q=None,
                 tag=None, dim=None, limit=500):
    sql = ["""SELECT d.*, f.file_name, c.clause_no, c.body AS clause_body,
                     c.split_strategy
                FROM doclib_rule_detail d
                JOIN doc_lib_file f ON f.file_id = d.file_id
                JOIN doclib_clause c ON c.clause_id = d.clause_id
               WHERE TRUE"""]
    args = []
    if origin:
        sql.append("AND d.origin=%s"); args.append(origin)
    if state:
        sql.append("AND d.review_state=%s"); args.append(state)
    if mapped is True:
        sql.append("AND d.mapped_ok")
    elif mapped is False:
        sql.append("AND NOT d.mapped_ok")
    if file_id:
        sql.append("AND d.file_id=%s"); args.append(file_id)
    if q:
        sql.append("AND (d.rule_name ILIKE %s OR d.logic_text ILIKE %s)")
        args += ["%%%s%%" % q, "%%%s%%" % q]
    if tag:
        sql.append("""AND EXISTS (SELECT 1 FROM doclib_detail_tag t
                                   WHERE t.detail_id = d.detail_id AND t.tag = %s)""")
        args.append(tag)
    if dim:
        sql.append("AND d.dimension=%s"); args.append(dim)
    sql.append("ORDER BY f.file_name, c.seq LIMIT %s")
    args.append(max(1, min(2000, int(limit))))
    rows = db.query(" ".join(sql), tuple(args))
    free = {}
    for t in db.query("SELECT * FROM doclib_detail_tag"):
        free.setdefault(t["detail_id"], []).append(t)
    for r in rows:
        r["source"] = source_of(r)
        r["tags"], r["missing_for_adopt"] = tags_of(r, free.get(r["detail_id"]))
    return rows


def stats():
    by = db.query("""SELECT origin, source_kind, count(*) AS n,
                            sum(CASE WHEN mapped_ok THEN 1 ELSE 0 END) AS mapped,
                            sum(CASE WHEN review_state='采纳' THEN 1 ELSE 0 END) AS adopted
                       FROM doclib_rule_detail GROUP BY 1,2 ORDER BY n DESC""")
    total = db.scalar("SELECT count(*) FROM doclib_rule_detail") or 0
    # **mapped 只数确定性那两路（列映射 + 场景拆条）。**
    # 把 AI 草稿也算进去，页面上就会出现「确定性映射 57」而实际只有 49——
    # 模型那一路根本没有「列」，mapped_ok 对它永远是 true。
    return {"total": total,
            "mapped": db.scalar("SELECT count(*) FROM doclib_rule_detail "
                                "WHERE mapped_ok AND origin IN ('列映射','场景拆条')") or 0,
            "flagged": db.scalar("SELECT count(*) FROM doclib_rule_detail "
                                 "WHERE NOT mapped_ok") or 0,
            "adopted": db.scalar("SELECT count(*) FROM doclib_rule_detail "
                                 "WHERE review_state='采纳'") or 0,
            "into_onto": db.scalar("SELECT count(*) FROM doclib_rule_detail "
                                   "WHERE adopted_rule_id IS NOT NULL") or 0,
            "by": by,
            "note": "「采纳」只是标记这条明细可用，**不等于进了规则库**。"
                    "写进本体要填维度、优先级、绑定节点这些明细里没有的东西，"
                    "那是「规则本体」那一栏的事——这里自动写进去，"
                    "等于让没人填过的字段带着默认值进了规则清单。"}


def review(detail_id, state, reviewer, reason=None):
    if state not in ("采纳", "驳回"):
        raise ValueError("只能是「采纳」或「驳回」")
    if not (reviewer or "").strip():
        raise ValueError("采纳/驳回必须署名：没有署名的审定在审计上等于没发生")
    if state == "驳回" and not (reason or "").strip():
        raise ValueError("驳回必须写原因——不写原因的驳回，下次抽取还会抽出同一条")
    d = db.query_one("SELECT mapped_ok, map_warning FROM doclib_rule_detail "
                     "WHERE detail_id=%s", (detail_id,))
    if not d:
        raise ValueError("没有这条明细：%s" % detail_id)
    if state == "采纳" and not d["mapped_ok"]:
        # 列没对齐的那些，采纳之前必须先有人把列的事弄清楚。
        # 直接允许采纳，等于把一条「控制类型可能读到隔壁列」的规则放进清单。
        raise ValueError("这条明细的列没对齐，不能直接采纳：%s。"
                         "先按原文把列的事定下来（改源表或人工填），再采纳"
                         % (d["map_warning"] or ""))
    n = db.execute("""UPDATE doclib_rule_detail SET review_state=%s, reviewer=%s,
                             reviewed_at=%s, reject_reason=%s WHERE detail_id=%s""",
                   (state, reviewer, datetime.datetime.now(), reason, detail_id))
    return n > 0


def storage():
    """这一栏把东西存在哪、重算时会不会被冲掉。

    rows 是实测条数，不是估的：这张表要能回答「页面上说的 35 条存在哪」。
    """
    def n(t):
        return db.scalar("SELECT count(*) FROM " + t) or 0

    return {"items": [
        {"table_name": "doclib_clause", "what": "切出来的条款原文、特征、覆盖情况",
         "rows": n("doclib_clause"),
         "written_by": "条款切分（确定性）", "on_recalc": "按内容定址 UPSERT",
         "why": "clause_id = md5(file_id|策略|条款号|同号第几次)，重切还是同一行——"
                "挂在上面的草稿和审定结论因此留得住。以前是 uuid + 整份删，"
                "实测丢过 14 条付费草稿。"},
        {"table_name": "doclib_rule_detail", "what": "一行一条规则明细，要素成列",
         "rows": n("doclib_rule_detail"),
         "written_by": "规则明细（列映射）+ AI 草稿投影", "on_recalc": "按 detail_id UPSERT",
         "why": "两路来源进同一张表（origin 区分），页面上只有一张产物清单；"
                "重跑不覆盖人工的采纳/驳回。"},
        {"table_name": "doclib_draft", "what": "模型抽的八要素原样 + 数值核对结果",
         "rows": n("doclib_draft"),
         "written_by": "规则草稿（出网、计费）", "on_recalc": "不覆盖，attempt 加一行",
         "why": "「上次抽出来的是什么、第几次抽的」要永远查得到。"},
        {"table_name": "rule", "what": "本体里真正生效的规则",
         "rows": n("rule"),
         "written_by": "**不由这一栏写**", "on_recalc": "—",
         "why": "明细采纳只是标记。写规则要填维度、优先级、绑定节点，"
                "那是「规则本体」那一栏的事——这里自动写进去，等于让没人填过的"
                "字段带着默认值进了规则清单。"},
    ], "note": "列映射 → 人核列 → 采纳 → **到「规则本体」那一栏才写 rule 表**。"
               "这一栏的 act.writes 里根本没有 rule 表，不是约定不写，是能力上不给。"}
