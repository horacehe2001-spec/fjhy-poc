# -*- coding: utf-8 -*-
"""领域知识资产 5 个二级菜单的接口（大模型配置已移到「系统」组）。

栏目按**规则加工链**排序，不是按字母或重要性：
  制度文件库 → 规则抽取 → 规则本体 → 流程本体 → 发布管理
这个顺序本身就是信息：上游没有的东西，下游不可能凭空长出来。
每条规则都必须能回溯到制度文件库里的某一份文件、某一条条款。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from .. import db
from .common import demo_banner, must_exist, paginate

router = APIRouter(prefix="/api/asset", tags=["领域知识资产"])


# ─────────────────────────────────────────────────────────────────────────────
# 导航：规则资产 5 个二级菜单 + 系统组
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/nav")
def nav():
    """返回 6 个栏目及各自的实测条数。

    条数直接从库里数，不写死——写死的数字会和实际数据漂移，
    而前端上那个数字恰恰是用户判断「这块有没有东西」的依据。
    """
    counts = {
        "doc-lib": db.scalar("SELECT count(*) FROM policy_doc") or 0,
        "extract": db.scalar("SELECT count(*) FROM extract_candidate") or 0,
        "onto": db.scalar("SELECT count(*) FROM rule WHERE version=1") or 0,
        "proc": db.scalar("SELECT count(*) FROM process_node "
                          "WHERE version=(SELECT version FROM ontology_version WHERE is_current)") or 0,
        "publish": db.scalar("SELECT count(*) FROM release") or 0,
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
    return paginate(
        "SELECT count(*) FROM policy_doc",
        """SELECT doc_id, file_name, doc_number, title, issuer, issued_on,
                  page_count, parse_status, parse_reason, has_text_layer,
                  ocr_engine, ocr_pages, uploaded_at
           FROM policy_doc ORDER BY uploaded_at DESC""", (), page, size)


@router.get("/docs/{doc_id}")
def get_doc(doc_id: str):
    doc = must_exist(db.query_one("SELECT * FROM policy_doc WHERE doc_id=%s", (doc_id,)),
                     "制度文件 " + doc_id)
    doc["clauses"] = db.query(
        "SELECT clause_id, clause_no, page_no, confidence, text_content "
        "FROM policy_clause WHERE doc_id=%s ORDER BY clause_no", (doc_id,))
    doc["pages"] = db.query(
        "SELECT page_no, text_source, confidence, length(text_content) AS chars "
        "FROM policy_doc_page WHERE doc_id=%s ORDER BY page_no, text_source", (doc_id,))
    # 哪些规则引用了这份文件——反向可追溯性，栏目一存在的理由
    doc["referenced_by"] = db.query(
        """SELECT rule_id, name AS rule_name FROM rule
           WHERE version=1 AND source_clause->>'file' IS NOT NULL
             AND %s LIKE '%%' || (source_clause->>'file') || '%%'
           ORDER BY rule_id""", (doc["file_name"],))
    return doc


@router.get("/docs/{doc_id}/parse-status")
def parse_status(doc_id: str):
    """解析状态四态，**failed 必须配原因**。

    「解析失败」和「文件本来就是空的」是两件事，混了就再也查不出来。
    实测：6 份制度 PDF 里 2 份有文字层、4 份纯扫描（含最关键的 27 页那份）。
    """
    d = must_exist(db.query_one(
        "SELECT doc_id, parse_status, parse_reason, has_text_layer, ocr_engine, ocr_pages "
        "FROM policy_doc WHERE doc_id=%s", (doc_id,)), "制度文件 " + doc_id)
    if d["parse_status"] == "failed" and not d["parse_reason"]:
        d["_warning"] = "解析失败但没有记原因——这条数据本身就是个问题"
    return d


# ─────────────────────────────────────────────────────────────────────────────
# 栏目二 · 规则抽取
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/extract/tasks")
def list_extract_tasks(page: int = 1, size: int = 20):
    return paginate(
        "SELECT count(*) FROM extract_task",
        """SELECT t.task_id, t.doc_id, d.file_name, t.mode, t.status,
                  t.candidate_cnt, t.accepted_cnt, t.started_at, t.finished_at, t.error
           FROM extract_task t LEFT JOIN policy_doc d ON d.doc_id=t.doc_id
           ORDER BY t.created_at DESC""", (), page, size)


@router.get("/extract/candidates")
def list_candidates(task_id: str = None, state: str = None, page: int = 1, size: int = 20):
    where, params = [], []
    if task_id:
        where.append("c.task_id = %s")
        params.append(task_id)
    if state:
        where.append("c.review_state = %s")
        params.append(state)
    w = (" WHERE " + " AND ".join(where)) if where else ""
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
    where, params = ["version=1"], []
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
    row = db.query_one("""SELECT count(*) total,
                          sum(CASE WHEN compiled THEN 1 ELSE 0 END) compiled,
                          sum(CASE WHEN demo_executable THEN 1 ELSE 0 END) executable
                          FROM rule WHERE version=1""")
    by_dim = db.query("""SELECT dimension, count(*) n,
                         sum(CASE WHEN compiled THEN 1 ELSE 0 END) c,
                         sum(CASE WHEN demo_executable THEN 1 ELSE 0 END) e
                         FROM rule WHERE version=1 GROUP BY dimension ORDER BY dimension""")
    by_class = db.query("SELECT rule_class, count(*) n FROM rule WHERE version=1 "
                        "GROUP BY rule_class")
    by_control = db.query("SELECT control_type, count(*) n FROM rule WHERE version=1 "
                          "GROUP BY control_type")
    return {"total": row["total"], "compiled": row["compiled"],
            "executable": row["executable"],
            "note": "compiled = 规则资产层面已编译；executable = 用当前数据真能跑。"
                    "两者不同的那 %d 条，差在「字段取不到」而不是「规则没写好」"
                    % (row["compiled"] - row["executable"]),
            "by_dimension": by_dim, "by_class": by_class, "by_control": by_control}


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str):
    r = must_exist(db.query_one("SELECT * FROM rule WHERE rule_id=%s AND version=1",
                                (rule_id,)), "规则 " + rule_id)
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
                                 AND oc.version = (SELECT version FROM ontology_version
                                                   WHERE is_current)
                                WHERE rc.rule_id=%s AND rc.version=1""", (rule_id,))
    r["entities"] = db.query("SELECT entity_id, semantics FROM rule_entity "
                             "WHERE rule_id=%s AND version=1", (rule_id,))
    r["relations"] = db.query("""SELECT rel_type, other_rule, note, needs_confirm, gap_id
                                 FROM rule_relation WHERE rule_id=%s AND version=1""",
                              (rule_id,))
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
           FROM lookup_table ORDER BY table_id""")}


@router.get("/lookup-tables/{table_id}")
def get_lookup_table(table_id: str):
    t = must_exist(db.query_one("SELECT * FROM lookup_table WHERE table_id=%s", (table_id,)),
                   "查找表 " + table_id)
    t["rows"] = db.query("""SELECT id, row_key, row_value, enabled, confidence,
                            source_rows, must_hit, must_not_hit, conflict, gap_id, note
                            FROM lookup_row WHERE table_id=%s ORDER BY id""", (table_id,))
    # 对账：row_count 合计是否等于源表行数
    if t["source_rows"]:
        s = db.scalar("SELECT coalesce(sum(source_rows),0) FROM lookup_row WHERE table_id=%s",
                      (table_id,))
        t["reconciliation"] = {"sum_source_rows": s, "declared": t["source_rows"],
                               "ok": s == t["source_rows"]}
    return t


@router.get("/ontology")
def ontology():
    v = db.query_one("SELECT version, source_hash, loaded_at, stats FROM ontology_version "
                     "WHERE is_current")
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
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
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
    rows = db.query("""SELECT r.*, (c.release_id IS NOT NULL) AS is_current
                       FROM release r
                       LEFT JOIN release_current c ON c.release_id=r.release_id
                       ORDER BY r.created_at DESC""")
    return {"items": rows,
            "policy": {"requires_signature": True,
                       "why": "规则集一变，所有在跑的单据的判定口径就跟着变。"
                              "让抽取结果自动发布，等于让模型直接改生产口径。"
                              "reviewed_by 为空的 release 不允许进 published"
                              "（库里有 CHECK 约束 ck_release_signed 钉着）。"}}


@router.get("/releases/{release_id}")
def get_release(release_id: str):
    r = must_exist(db.query_one("SELECT * FROM release WHERE release_id=%s", (release_id,)),
                   "发布 " + release_id)
    r["items"] = db.query("""SELECT i.rule_id, i.version, i.change,
                                    ru.name AS rule_name, ru.control_type
                             FROM release_item i
                             LEFT JOIN rule ru ON ru.rule_id=i.rule_id AND ru.version=i.version
                             WHERE i.release_id=%s ORDER BY i.change, i.rule_id""",
                          (release_id,))
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
