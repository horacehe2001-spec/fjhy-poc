# -*- coding: utf-8 -*-
"""领域与实例：「领域知识资产」这一组的归属层。

口径（2026-10-21 用户决策）：

1. **本期只做「归属 + 展示」，不做按实例过滤。**
   现有全部资产归到「审核 / 福建海运集团」，各栏目页头显示当前实例，
   但接口不加 `WHERE instance_id`。只有一个实例时，过滤与否结果完全相同；
   等真有第二个实例，再逐条接口加过滤。
   **所以本模块返回的资产数字是全库数字**，接口里把这件事明写在 note 里——
   让人以为「这是当前实例筛出来的数」，等于埋了一个将来说不清的坑。

2. **当前选择是全局单例。** 存在 `app_setting` 里，所有人共用一份，
   不存浏览器。本 POC 没有登录体系，存在浏览器会出现
   「你看到的和我看到的不同」，排障时多一个变量。

3. **当前实例允许为空。** 切到「报表」领域时它下面没有实例，
   就如实返回 null，页面显示「该领域下还没有实例」——不假装有。
"""
import uuid

from fastapi import APIRouter, Body, HTTPException

from .. import db

router = APIRouter(prefix="/api/domain", tags=["领域知识资产"])

KEY_DOMAIN = "current_domain"
KEY_INSTANCE = "current_instance"


def _get(key):
    return db.scalar("SELECT value FROM app_setting WHERE setting_key = %s", (key,))


def _set(key, value, who):
    db.execute("""INSERT INTO app_setting (setting_key, value, updated_by)
                  VALUES (%s, %s, %s)
                  ON CONFLICT (setting_key) DO UPDATE
                  SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                      updated_at = now()""", (key, value, who))


def current():
    """当前领域与实例。**给别处用的唯一入口**——各处自己去读 app_setting
    迟早会读出不一致的口径（有人读 current_domain 有人读 current_instance，
    而两者可能指向不同领域）。这里一次读完并做一致性校验。"""
    did = _get(KEY_DOMAIN)
    iid = _get(KEY_INSTANCE)
    d = db.query_one("SELECT domain_id, label FROM domain WHERE domain_id = %s", (did,)) if did else None
    i = db.query_one("""SELECT instance_id, domain_id, label FROM domain_instance
                        WHERE instance_id = %s""", (iid,)) if iid else None
    # 实例不属于当前领域 = 数据不一致。**不静默纠正**：静默纠正会让
    # 「我明明选的是 A，怎么显示 B」变成查不出来的现象。如实返回并标出来。
    mismatch = bool(i and d and i["domain_id"] != d["domain_id"])
    return {
        "domain_id": d["domain_id"] if d else None,
        "domain_label": d["label"] if d else None,
        "instance_id": None if mismatch else (i["instance_id"] if i else None),
        "instance_label": None if mismatch else (i["label"] if i else None),
        "mismatch": mismatch,
    }


@router.get("/state")
def state():
    """当前选择 + 全部可选项。页面一次拿齐，不用来回查。"""
    domains = db.query("""SELECT domain_id, label, sort_no, enabled, note
                          FROM domain ORDER BY sort_no, domain_id""")
    insts = db.query("""SELECT instance_id, domain_id, label, is_default, sort_no, note
                        FROM domain_instance ORDER BY sort_no, instance_id""")
    by_domain = {}
    for it in insts:
        by_domain.setdefault(it["domain_id"], []).append(it)
    for d in domains:
        d["instances"] = by_domain.get(d["domain_id"], [])
    return {
        "current": current(),
        "domains": domains,
        "note": "当前领域/实例是**全局单例**：库里一行，所有人共用，换浏览器也一样。",
    }


@router.post("/current")
def set_current(body: dict = Body(...)):
    """切换当前领域/实例。

    不传 instance_id 时取该领域的默认实例；该领域没有实例就置空，
    **不退到别的领域的实例**——跨领域串实例比没有实例更难发现。
    """
    did = (body.get("domain_id") or "").strip()
    d = db.query_one("SELECT domain_id, enabled FROM domain WHERE domain_id = %s", (did,))
    if not d:
        raise HTTPException(400, "领域不存在：%s" % did)
    if not d["enabled"]:
        raise HTTPException(400, "领域已停用：%s" % did)

    iid = (body.get("instance_id") or "").strip() or None
    if iid:
        i = db.query_one("""SELECT instance_id, domain_id FROM domain_instance
                            WHERE instance_id = %s""", (iid,))
        if not i:
            raise HTTPException(400, "实例不存在：%s" % iid)
        if i["domain_id"] != did:
            raise HTTPException(400, "实例 %s 不属于领域 %s，不能跨领域选" % (iid, did))
    else:
        iid = db.scalar("""SELECT instance_id FROM domain_instance
                           WHERE domain_id = %s AND is_default""", (did,))

    who = (body.get("who") or "ui").strip()[:64]
    _set(KEY_DOMAIN, did, who)
    _set(KEY_INSTANCE, iid, who)
    cur = current()
    return {
        "ok": True,
        "current": cur,
        "note": ("该领域下还没有实例，当前实例置空"
                 if not cur["instance_id"] else "已切换"),
    }


@router.post("/domains")
def add_domain(body: dict = Body(...)):
    """新增领域。

    **只建领域，不自动建实例**：一个凭空冒出来的实例会让人以为那里已经有资产。
    新领域建出来就是空的，页面如实显示「该领域下还没有实例」。

    **不自动切过去**：点「新增」是建一个东西，不是「我要开始用它」。
    当前领域是全局单例，切过去会影响正在看页面的所有人。要用就再点一下它。

    domain_id 用生成的短 id，不从中文名推：拼音转换不在依赖里，
    而让用户自己填英文 id 是在问一个他答不上来的问题。
    """
    label = (body.get("label") or "").strip()
    if not label:
        raise HTTPException(400, "领域名称不能为空")
    if len(label) > 40:
        raise HTTPException(400, "领域名称最长 40 字")
    dup = db.query_one("SELECT domain_id FROM domain WHERE label = %s", (label,))
    if dup:
        raise HTTPException(400, "已有同名领域：%s（%s）" % (label, dup["domain_id"]))

    did = "dom-" + uuid.uuid4().hex[:8]
    nxt = (db.scalar("SELECT max(sort_no) FROM domain") or 0) + 10
    db.execute("""INSERT INTO domain (domain_id, label, sort_no, note)
                  VALUES (%s, %s, %s, %s)""",
               (did, label, nxt, (body.get("note") or "").strip()[:500] or None))
    return {"ok": True, "domain_id": did, "label": label,
            "note": "领域已建好，下面还没有实例。要用它先给它加一个实例。"}


@router.get("/overview")
def overview():
    """当前实例的资产概览。

    **这些是全库数字。** 本期不按实例过滤（只有一个实例，过滤与否相同），
    口径写在返回里，不让人误读成「筛出来的」。
    """
    cur = current()
    _onto_cur = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    counts = {
        "doc_files": db.scalar("SELECT count(*) FROM doc_lib_file") or 0,
        "clauses": db.scalar("SELECT count(*) FROM doclib_clause") or 0,
        "rules_total": db.scalar("SELECT count(*) FROM rule WHERE version = 1") or 0,
        "rules_compiled": db.scalar(
            "SELECT count(*) FROM rule WHERE version = 1 AND compiled") or 0,
        "rules_executable": db.scalar(
            "SELECT count(*) FROM rule WHERE version = 1 AND demo_executable") or 0,
        # 本体实体/概念只数**当前版本**，当前版本取自 ontology_version.is_current。
        # 两个坑都在这一句里：
        #   · 直接 count(*) 会把旧版 v0.1-draft 的行算进去（34 实体 / 240 概念），
        #     而页面上那个数会被当成「本体有多大」来读；
        #   · version 是 TEXT，用 max(version) 选「最新版」会按文本比大小，
        #     选中的是 'v0.1-draft'（v > 0）——**正好是草稿那版**。
        "onto_entities": db.scalar(
            "SELECT count(*) FROM ontology_entity WHERE version = %s", (_onto_cur,)) or 0,
        "onto_concepts": db.scalar(
            "SELECT count(*) FROM ontology_concept WHERE version = %s", (_onto_cur,)) or 0,
        "bills_real": db.scalar("SELECT count(*) FROM expense_bill WHERE NOT is_demo") or 0,
        "bills_demo": db.scalar("SELECT count(*) FROM expense_bill WHERE is_demo") or 0,
    }
    # 生效版本**必须走 engine.effective_release()**。自己写一句
    # `WHERE status='current'` 会得到 None（库里根本没有 current 这个状态值），
    # 页面就会显示「没有发布版本」，而引擎其实正按 rs-20261021-01 在判。
    # 这个判断全站只留一份，engine 里那份注释写明了分叉过一次的后果。
    from ..rules import engine
    rel, rel_why = engine.effective_release()
    return {
        "current": cur,
        "counts": counts,
        "current_release": rel["release_id"] if rel else None,
        "current_release_why": rel_why,
        "rule_set_version": rel.get("rule_set_version") if rel else None,
        "onto_version": _onto_cur,
        "scope_note": ("本期按「归属 + 展示」做：资产已归到当前实例，"
                       "但接口**尚未按实例过滤**，所以这里是全库数字。"
                       "有第二个实例时才需要逐条接口加过滤。"),
    }
