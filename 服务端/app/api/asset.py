# -*- coding: utf-8 -*-
"""领域知识资产 5 个二级菜单的接口（大模型配置已移到「系统」组）。

栏目按**规则加工链**排序，不是按字母或重要性：
  制度文件库 → 规则抽取 → 规则本体 → 流程本体 → 发布管理
这个顺序本身就是信息：上游没有的东西，下游不可能凭空长出来。
每条规则都必须能回溯到制度文件库里的某一份文件、某一条条款。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from .. import db, scope, onto_current
from .common import demo_banner, must_exist, paginate

router = APIRouter(prefix="/api/asset", tags=["领域知识资产"])


def _iid():
    """本栏所有查询按哪个实例过滤。没选实例时回 400，**不退化成全库**。

    退化成全库的后果：页头写着「审核 / 822港航」，规则本体那一栏列的是福建海运集团
    的 82 条规则，而返回里没有一个字段说明这是全库数字——
    使用者会把它当成 822港航 已经有了 82 条规则，照着这个数去对账。
    400 而不是 500：这是「请先去选实例」，不是程序出错；
    混成 500 会让人去翻后端日志找原因（api/doclib.py 的 _guard 同款处置）。
    """
    try:
        return scope.require()
    except scope.ScopeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# **本体链的表还没有 instance_id**（ontology_version / ontology_entity /
# ontology_concept / ontology_enum / ontology_enum_value / ontology_relation /
# process_node / process_transition / enum_value_current，迁移未做）。
# 本文件里涉及它们的查询一律保持原样：给没有这一列的表加 WHERE instance_id
# 是直接 ProgrammingError，整页打不开，比"数字是全库的"糟得多。
# review_run / review_finding / approval_record 也不过滤——它们靠 bill_no 关联
# expense_bill，而 bill_no 全局唯一，实例隔离在上游 bill 那一层就已经成立。


# ─────────────────────────────────────────────────────────────────────────────
# 导航：规则资产 5 个二级菜单 + 系统组
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/nav")
def nav():
    """返回 6 个栏目及各自的实测条数。

    条数直接从库里数，不写死——写死的数字会和实际数据漂移，
    而前端上那个数字恰恰是用户判断「这块有没有东西」的依据。
    """
    iid = _iid()
    # 侧栏这几个数字是用户判断「这块有没有东西」的依据，所以必须是**当前实例的**数。
    # 新建的实例这里应当全是 0——那正是它真实的样子，不是接口坏了。
    # **前两项原来数的是死表。**
    #   doc-lib 数 policy_doc、extract 数 extract_candidate——这两张表
    #   （连同 policy_clause / extract_task）全库 0 行、全项目零写入，
    #   是早期设计留下的空壳。真正在用的是 doc_lib_file 与 doclib_clause。
    #   表现是左侧导航「制度文件库 0 / 规则抽取 0」而库里明明有 12 份文件、
    #   343 条条款——**两个实例都一直是 0**，所以没人把它当成 bug，
    #   只当成「这个实例还没开始建」。
    #
    # llm（大模型配置）不按实例过滤：系统级配置（2026-10-18 已定），不跟业务域走。
    # proc 按实例过滤（本体链 0044 之后 process_node 有 instance_id 了）。
    counts = {
        "doc-lib": db.scalar("SELECT count(*) FROM doc_lib_file "
                             "WHERE instance_id=%s AND NOT missing", (iid,)) or 0,
        "extract": db.scalar("""SELECT count(*) FROM doclib_clause c
                                  JOIN doc_lib_file f ON f.file_id = c.file_id
                                 WHERE f.instance_id=%s""", (iid,)) or 0,
        "onto": db.scalar("SELECT count(*) FROM rule WHERE instance_id=%s AND version=1",
                          (iid,)) or 0,
        "proc": db.scalar("SELECT count(*) FROM process_node "
                          "WHERE instance_id=%s AND version=%s",
                          (iid, onto_current.version(iid))) or 0,
        "publish": db.scalar("SELECT count(*) FROM release WHERE instance_id=%s", (iid,)) or 0,
        "llm": db.scalar("SELECT count(*) FROM llm_config") or 0,
    }
    items = [
        {"key": "doc-lib", "label": "制度文件库", "order": 1,
         "desc": "规则的唯一上游。每条规则都要能回溯到这里的某份文件、某条条款"},
        {"key": "extract", "label": "规则抽取", "order": 2,
         "desc": "两档：自动抽取给草稿，人工确认才落规则本体。没有自动发布这条路"},
        {"key": "onto", "label": "规则本体", "order": 3,
         "desc": "八要素 + 对齐客户配置器的 4 个字段；查找表在这里维护"},
        {"key": "proc", "label": "流程本体", "order": 4,
         "desc": "节点与迁移全部数自 50 页真实审批记录，不是设计出来的"},
        {"key": "publish", "label": "发布管理", "order": 5,
         "desc": "版本化 + 人工签字。reviewed_by 为空不允许进 published"},
        # 2026-10-18 移到「系统」组。条数接口仍然回它——
        # 前端是按 key 取 count 的，少回一项侧栏那个数字就空着，
        # 而空着看起来像「库里没配置」，不是「没在这组里」。
        {"key": "llm", "label": "大模型配置", "order": 1, "group": "sys",
         "desc": "系统级配置。Key 只回掩码，明文永不出库"},
    ]
    for it in items:
        it["count"] = counts[it["key"]]
    return {"items": items, "banner": demo_banner()}


# ─────────────────────────────────────────────────────────────────────────────
# 栏目一 · 制度文件库
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/docs")
def list_docs(page: int = 1, size: int = 20):
    iid = _iid()
    return paginate(
        "SELECT count(*) FROM policy_doc WHERE instance_id=%s",
        """SELECT doc_id, file_name, doc_number, title, issuer, issued_on,
                  page_count, parse_status, parse_reason, has_text_layer,
                  ocr_engine, ocr_pages, uploaded_at
           FROM policy_doc WHERE instance_id=%s ORDER BY uploaded_at DESC""",
        (iid,), page, size)


@router.get("/docs/{doc_id}")
def get_doc(doc_id: str):
    iid = _iid()
    # 按 (实例, doc_id) 取。只按 doc_id 取的话，切到 822港航 之后用一个
    # 福建海运集团的 doc_id 仍然能把那份文件的全文调出来——那是越过实例边界读数据。
    doc = must_exist(db.query_one("SELECT * FROM policy_doc WHERE instance_id=%s AND doc_id=%s",
                                  (iid, doc_id)), "制度文件 " + doc_id)
    doc["clauses"] = db.query(
        "SELECT clause_id, clause_no, page_no, confidence, text_content "
        "FROM policy_clause WHERE instance_id=%s AND doc_id=%s ORDER BY clause_no",
        (iid, doc_id))
    doc["pages"] = db.query(
        "SELECT page_no, text_source, confidence, length(text_content) AS chars "
        "FROM policy_doc_page WHERE instance_id=%s AND doc_id=%s "
        "ORDER BY page_no, text_source", (iid, doc_id))
    # 哪些规则引用了这份文件——反向可追溯性，栏目一存在的理由。
    # **必须同实例**：两个实例可能各有一份同名制度文件（"差旅管理办法.pdf"），
    # 不限定实例，这里会把对面那些规则列成"引用了本文件"，而证据链其实不通。
    doc["referenced_by"] = db.query(
        """SELECT rule_id, name AS rule_name FROM rule
           WHERE instance_id=%s AND version=1 AND source_clause->>'file' IS NOT NULL
             AND %s LIKE '%%' || (source_clause->>'file') || '%%'
           ORDER BY rule_id""", (iid, doc["file_name"]))
    return doc


@router.get("/docs/{doc_id}/parse-status")
def parse_status(doc_id: str):
    """解析状态四态，**failed 必须配原因**。

    「解析失败」和「文件本来就是空的」是两件事，混了就再也查不出来。
    实测：6 份制度 PDF 里 2 份有文字层、4 份纯扫描（含最关键的 27 页那份）。
    """
    d = must_exist(db.query_one(
        "SELECT doc_id, parse_status, parse_reason, has_text_layer, ocr_engine, ocr_pages "
        "FROM policy_doc WHERE instance_id=%s AND doc_id=%s", (_iid(), doc_id)),
        "制度文件 " + doc_id)
    if d["parse_status"] == "failed" and not d["parse_reason"]:
        d["_warning"] = "解析失败但没有记原因——这条数据本身就是个问题"
    return d


# ─────────────────────────────────────────────────────────────────────────────
# 栏目二 · 规则抽取
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/extract/tasks")
def list_extract_tasks(page: int = 1, size: int = 20):
    iid = _iid()
    return paginate(
        "SELECT count(*) FROM extract_task WHERE instance_id=%s",
        """SELECT t.task_id, t.doc_id, d.file_name, t.mode, t.status,
                  t.candidate_cnt, t.accepted_cnt, t.started_at, t.finished_at, t.error
           FROM extract_task t
           LEFT JOIN policy_doc d ON d.doc_id=t.doc_id AND d.instance_id=t.instance_id
           WHERE t.instance_id=%s
           ORDER BY t.created_at DESC""", (iid,), page, size)


@router.get("/extract/candidates")
def list_candidates(task_id: str = None, state: str = None, page: int = 1, size: int = 20):
    where, params = ["c.instance_id = %s"], [_iid()]
    if task_id:
        where.append("c.task_id = %s")
        params.append(task_id)
    if state:
        where.append("c.review_state = %s")
        params.append(state)
    w = " WHERE " + " AND ".join(where)
    return paginate(
        "SELECT count(*) FROM extract_candidate c" + w,
        """SELECT c.id, c.task_id, c.clause_id, c.draft, c.confidence,
                  c.review_state, c.reviewer, c.reviewed_at, c.reject_reason
           FROM extract_candidate c""" + w + " ORDER BY c.id", tuple(params), page, size)


@router.get("/extract/policy")
def extract_policy():
    """抽取的处置口径。前端要把这段显示出来，不能只给个「确认」按钮。"""
    return {
        "two_tier": "自动抽取给草稿，人工确认才落规则本体",
        "no_auto_publish": True,
        "why": "规则是制度的投影，判错一条就是对一批单据误报或漏报。"
               "模型抽出来的八要素里，「判定逻辑」和「例外」两项最容易丢细节——"
               "实测市内交通费那条，制度原文有 40 元这一档（仅往返中转站），"
               "漏掉就会把样本里 120 元的正常单判成超标准。",
        "states": ["pending", "accepted", "rejected", "merged"],
        "requires_reviewer": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 栏目三 · 规则本体
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/rules")
def list_rules(dimension: str = None, rule_class: str = None,
               compiled: bool = None, executable: bool = None,
               q: str = None, page: int = 1, size: int = 50):
    # 实例条件放在第一个，和 version=1 一样当成"这张表的基本约束"看待：
    # 漏掉它不会报错，只会让 822港航 的规则清单里列出福建海运集团的 82 条——
    # 而页头写着的是 822港航，使用者会照着这个数去对账。
    where, params = ["instance_id=%s", "version=1"], [_iid()]
    if dimension:
        where.append("dimension=%s")
        params.append(dimension)
    if rule_class:
        where.append("rule_class=%s")
        params.append(rule_class)
    if compiled is not None:
        where.append("compiled=%s")
        params.append(compiled)
    if executable is not None:
        where.append("demo_executable=%s")
        params.append(executable)
    if q:
        where.append("(rule_id ILIKE %s OR name ILIKE %s)")
        params += ["%%%s%%" % q, "%%%s%%" % q]
    w = " WHERE " + " AND ".join(where)
    out = paginate(
        "SELECT count(*) FROM rule" + w,
        """SELECT rule_id, name AS rule_name, dimension, risk_class, rule_class,
                  control_type,
                  priority, algo_code, algo_type, configurator_status,
                  compiled, compiled_reason, demo_executable, demo_blocker, demo_scope,
                  binding_node, impl_path, feasibility, external_table, owner
           FROM rule""" + w + " ORDER BY dimension, rule_id", tuple(params), page, size)
    out["summary"] = rules_summary()
    return out


@router.get("/rules/summary")
def rules_summary():
    """三个数并列，**不能只报可编译数**。

    「可编译 37」和「演示可执行 12」是两件事：前者是规则资产层面，
    后者是手上有没有数据。只报一个，产品缺口和取数缺口就分不清了。
    """
    iid = _iid()
    row = db.query_one("""SELECT count(*) total,
                          sum(CASE WHEN compiled THEN 1 ELSE 0 END) compiled,
                          sum(CASE WHEN demo_executable THEN 1 ELSE 0 END) executable
                          FROM rule WHERE instance_id=%s AND version=1""", (iid,))
    by_dim = db.query("""SELECT dimension, count(*) n,
                         sum(CASE WHEN compiled THEN 1 ELSE 0 END) c,
                         sum(CASE WHEN demo_executable THEN 1 ELSE 0 END) e
                         FROM rule WHERE instance_id=%s AND version=1
                         GROUP BY dimension ORDER BY dimension""", (iid,))
    by_class = db.query("SELECT rule_class, count(*) n FROM rule "
                        "WHERE instance_id=%s AND version=1 GROUP BY rule_class", (iid,))
    by_control = db.query("SELECT control_type, count(*) n FROM rule "
                          "WHERE instance_id=%s AND version=1 GROUP BY control_type", (iid,))
    # 空实例（新建的实例一条规则都没有）时 sum(...) 回的是 NULL 而不是 0，
    # 直接拿去做减法是 TypeError —— 整页 500。**新实例是常态，不是异常**，
    # 所以在这里落成 0，而不是让调用方各自防一次。
    total = row["total"] or 0
    n_compiled = row["compiled"] or 0
    n_exec = row["executable"] or 0
    return {"total": total, "compiled": n_compiled,
            "executable": n_exec,
            "note": "compiled = 规则资产层面已编译；executable = 用当前数据真能跑。"
                    "两者不同的那 %d 条，差在「字段取不到」而不是「规则没写好」"
                    % (n_compiled - n_exec),
            "by_dimension": by_dim, "by_class": by_class, "by_control": by_control}


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str):
    iid = _iid()
    # rule 的主键在 0041 之后是 (instance_id, rule_id, version)：**两个实例可以有
    # 同号规则**（TR-D4-05 在两边各一条），不带实例条件 query_one 会随机拿到一条。
    r = must_exist(db.query_one("SELECT * FROM rule WHERE instance_id=%s AND rule_id=%s "
                                "AND version=1", (iid, rule_id)), "规则 " + rule_id)
    # SELECT * 起不了别名，在出口处改键：叫 name 的字段会被人名脱敏吃成「付**********」。
    # 规则名不是人名——但字段名一样，策略就一样。
    r["rule_name"] = r.pop("name", None)
    # oc.name 必须起别名：叫 name 的字段会被人名脱敏吃成「核****」
    r["concepts"] = db.query("""SELECT rc.concept_id, rc.role,
                                       oc.name AS concept_name, oc.availability,
                                       oc.gap_id, oc.entity_id
                                FROM rule_concept rc
                                LEFT JOIN ontology_concept oc
                                  ON oc.concept_id = rc.concept_id
                                 AND oc.instance_id = rc.instance_id
                                 AND oc.version = (SELECT version FROM ontology_version
                                                    WHERE instance_id = rc.instance_id
                                                      AND is_current)
                                WHERE rc.instance_id=%s AND rc.rule_id=%s
                                  AND rc.version=1""", (iid, rule_id))
    r["entities"] = db.query("SELECT entity_id, semantics FROM rule_entity "
                             "WHERE instance_id=%s AND rule_id=%s AND version=1",
                             (iid, rule_id))
    r["relations"] = db.query("""SELECT rel_type, other_rule, note, needs_confirm, gap_id
                                 FROM rule_relation
                                 WHERE instance_id=%s AND rule_id=%s AND version=1""",
                              (iid, rule_id))
    r["recent_findings"] = db.query("""SELECT f.run_id, f.outcome, f.message, r.bill_no
                                       FROM review_finding f
                                       JOIN review_run r ON r.run_id=f.run_id
                                       WHERE f.rule_id=%s ORDER BY f.id DESC LIMIT 10""",
                                    (rule_id,))
    return r


@router.get("/lookup-tables")
def list_lookup_tables():
    return {"items": db.query(
        """SELECT table_id, version, kind, source_file, source_rows, row_count,
                  key_fields, fallback_key, on_miss, note, loaded_at
           FROM lookup_table WHERE instance_id=%s ORDER BY table_id""", (_iid(),))}


@router.get("/lookup-tables/{table_id}")
def get_lookup_table(table_id: str):
    iid = _iid()
    # table_id 是固定中文名（"必要附件决策表"），两个实例必然同名，
    # 所以这里不带实例条件会直接串表——而串掉的是决策表，一行之差就是
    # 对一批单据误报或漏报。
    t = must_exist(db.query_one("SELECT * FROM lookup_table WHERE instance_id=%s "
                                "AND table_id=%s", (iid, table_id)),
                   "查找表 " + table_id)
    t["rows"] = db.query("""SELECT id, row_key, row_value, enabled, confidence,
                            source_rows, must_hit, must_not_hit, conflict, gap_id, note
                            FROM lookup_row WHERE instance_id=%s AND table_id=%s
                            ORDER BY id""", (iid, table_id))
    # 对账：row_count 合计是否等于源表行数
    if t["source_rows"]:
        s = db.scalar("SELECT coalesce(sum(source_rows),0) FROM lookup_row "
                      "WHERE instance_id=%s AND table_id=%s", (iid, table_id))
        t["reconciliation"] = {"sum_source_rows": s, "declared": t["source_rows"],
                               "ok": s == t["source_rows"]}
    return t


@router.get("/ontology")
def ontology():
    v = onto_current.row()
    if not v:
        raise HTTPException(404, "库里没有当前本体版本")
    # e.name AS entity_name：实测 HTTP 出来是「报**」「费***」——
    # 本体元素名不是人名，但字段名一样就走同一条策略
    v["entities"] = db.query("""SELECT e.entity_id, e.name AS entity_name, e.source,
                                 count(c.concept_id) AS concepts
                                FROM ontology_entity e
                                LEFT JOIN ontology_concept c
                                  ON c.entity_id=e.entity_id AND c.version=e.version
                                WHERE e.version=%s GROUP BY 1,2,3 ORDER BY 1""",
                             (v["version"],))
    v["availability"] = db.query("""SELECT availability, count(*) n FROM ontology_concept
                                    WHERE version=%s GROUP BY 1 ORDER BY 2 DESC""",
                                 (v["version"],))
    v["enums"] = db.query("""SELECT e.enum_id, e.name AS enum_name,
                                    count(ev.value) AS values
                             FROM ontology_enum e
                             LEFT JOIN ontology_enum_value ev
                               ON ev.enum_id=e.enum_id AND ev.version=e.version
                             WHERE e.version=%s GROUP BY 1,2 ORDER BY 1""", (v["version"],))
    return v


# ─────────────────────────────────────────────────────────────────────────────
# 栏目四 · 流程本体
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/process")
def process():
    v = onto_current.version()
    # node_name：这一页的节点链一直显示的是「提*」「部******」
    nodes = db.query("""SELECT node_id, name AS node_name, is_bypass, occurrences, note
                        FROM process_node WHERE version=%s ORDER BY node_id""", (v,))
    trans = db.query("""SELECT from_node, to_node, cond, blocking, note
                        FROM process_transition WHERE version=%s ORDER BY from_node, to_node""",
                     (v,))
    # 实测的节点出现次数——流程图上要显示，让人看出哪条路是主路
    measured = db.query("""SELECT node, count(*) n FROM approval_record
                           GROUP BY node ORDER BY n DESC""")
    return {
        "version": v, "nodes": nodes, "transitions": trans,
        "measured_from_real_approvals": measured,
        "note": "节点与迁移全部数自 50 页真实审批记录，不是设计出来的。"
                "N04 AI审核 → N05 共享审核 的 blocking=false 是实测结论："
                "FJPG-BX2610130821 在 2026-10-13 17:25:03 得到「AI审核不通过」，"
                "17:35:41 仍进入共享审核并由人工给出退回理由。AI 结论是参考，闸门在人工手里。",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 栏目五 · 发布管理
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/releases")
def list_releases():
    iid = _iid()
    rows = db.query("""SELECT r.*, (c.release_id IS NOT NULL) AS is_current
                       FROM release r
                       LEFT JOIN release_current c ON c.release_id=r.release_id
                                                  AND c.instance_id=r.instance_id
                       WHERE r.instance_id=%s
                       ORDER BY r.created_at DESC""", (iid,))
    return {"items": rows,
            "policy": {"requires_signature": True,
                       "why": "规则集一变，所有在跑的单据的判定口径就跟着变。"
                              "让抽取结果自动发布，等于让模型直接改生产口径。"
                              "reviewed_by 为空的 release 不允许进 published"
                              "（库里有 CHECK 约束 ck_release_signed 钉着）。"}}


@router.get("/releases/{release_id}")
def get_release(release_id: str):
    iid = _iid()
    r = must_exist(db.query_one("SELECT * FROM release WHERE instance_id=%s AND release_id=%s",
                                (iid, release_id)), "发布 " + release_id)
    # LEFT JOIN rule 也要带实例：release_item.rule_id 是**弱引用**（0025 拆掉了外键），
    # 不带实例条件会把另一个实例的同号规则的名字贴到这一版的差异清单上。
    r["items"] = db.query("""SELECT i.rule_id, i.version, i.change,
                                    ru.name AS rule_name, ru.control_type
                             FROM release_item i
                             LEFT JOIN rule ru ON ru.rule_id=i.rule_id
                                              AND ru.version=i.version
                                              AND ru.instance_id=i.instance_id
                             WHERE i.instance_id=%s AND i.release_id=%s
                             ORDER BY i.change, i.rule_id""", (iid, release_id))
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 大模型配置：已移到「系统」组
# ---------------------------------------------------------------------------
# 实现在 app/api/sys_llm.py，路径是 /api/sys/llm-*。
# 这里留两条 307 跳转，不是为了好看——
# 已经发出去的链接、写进文档的 curl、别人存的书签，不该因为菜单换了归类就 404。
# 307 而不是 301/302：307 保留请求方法，POST 跳过去还是 POST。
#
# **逻辑一行都不留在这里。** 留一份「简化版」实现是最坏的做法：
# 两边口径会漂移，而漂移的那一天没人知道前端调的是哪一份。
# 原先这里存 Key 用的是 sha256（不可逆），新实现改成了 Fernet 可逆加密——
# 两份并存就等于两种存储格式并存，测试连通时取不回明文的那条永远测不通。
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/llm-configs", include_in_schema=False)
def list_llm_moved():
    return RedirectResponse("/api/sys/llm-configs", status_code=307)


@router.post("/llm-configs", include_in_schema=False)
def upsert_llm_moved():
    return RedirectResponse("/api/sys/llm-configs", status_code=307)
