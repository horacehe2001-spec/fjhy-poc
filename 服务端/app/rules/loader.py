# -*- coding: utf-8 -*-
"""把规则资产装载进库，并生成一个 release（草稿态）。

装载范围：
  规则清单_v0.1.json          → rule / rule_concept / rule_entity / rule_relation
  必要附件决策表_v0.1.yaml     → lookup_table / lookup_row（decision_table）
  敏感词表_v0.1.yaml          → lookup_table / lookup_row（keyword_list）
  内部单位名单_v0.1.yaml       → lookup_table / lookup_row（allow_list）
  标准表_v0.1.yaml            → standard_limit + lookup_table（limit_table）

**装载不等于发布。** release 建出来是 draft 态，reviewed_by 为空，
而 0005 里有一条约束 ck_release_signed：status='published' 必须有 reviewed_by。
所以这个脚本无论跑多少遍都不可能让规则自动上生产。
理由很直接：规则集一变，所有在跑的单据的判定口径就跟着变。

**装载只动工作区**（用户 2026-10-19 定的）：
已发布的版本是快照（release_asset 里自成一份），当前生效指针 release_current
也一律不碰。这个脚本只重建自己那一份草稿。
原来它会 `DELETE FROM release_item`（整表）、`DELETE FROM release_current`，
并且不分状态地删 rs-0.1 那一版——于是「装载一次，发布状态被复位」。

用法（在 服务端/ 下）：
    python -I app/rules/loader.py
    python -I app/rules/loader.py --status
"""
import argparse
import hashlib
import io
import json
import os
import sys

import yaml

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config, db  # noqa: E402

RULE_VERSION = 1


def _j(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _hash(path):
    return hashlib.sha256(io.open(path, "rb").read()).hexdigest()


def _binding_map():
    """规则 → 演示可执行性。**源在 规则绑定_v0.1.json，不在规则清单。**

    规则清单里只有 TR-D6-01 显式写了 demo_executable；其余 81 条的
    演示可执行性是 gen_bind.py 按「概念可得性」算出来的。
    从规则清单里读会得到 0 条可执行——第一次装载就踩了这个坑：
    打印出「演示可执行 0」，而实测应该是 17。
    """
    bind = json.load(io.open(config.BINDING_JSON, encoding="utf-8"))
    return {b["rule_id"]: b for b in bind["bindings"]}


def db_scalar_in_tx(cur, sql, params=()):
    """事务里取一个标量。装载是在一个 tx 里跑的，不能再开一条连接去读——
    那样读到的是事务外的旧值。"""
    cur.execute(sql, params)
    row = cur.fetchone()
    return (row[0] if not isinstance(row, dict) else list(row.values())[0]) if row else 0


def load_rules(conn, rules):
    cur = conn.cursor()
    bmap = _binding_map()
    # **只清装载脚本自己写的行。** 原来这五句是无条件 DELETE，
    # 其中 DELETE FROM rule 会把「统一口径采纳进本体」的规则一起删掉——
    # 那些规则的 created_by 是 onto_adopt:<谁>，是人拍板写进去的，
    # 重跑一次装载就静默消失，而装载脚本的输出里一个字都看不出来。
    # 这类「我的流程把别人的数据清了」的损失在 code review 里极难发现，
    # 所以口径写在这里：**装载脚本的地盘是 created_by='asset_loader'，
    # 别人的行一律不碰。**
    kept = db_scalar_in_tx(cur, "SELECT count(*) FROM rule WHERE created_by <> %s",
                           ('asset_loader',))
    cur.execute("DELETE FROM rule_relation WHERE (rule_id, version) IN "
                "(SELECT rule_id, version FROM rule WHERE created_by = %s)",
                ('asset_loader',))
    cur.execute("DELETE FROM rule_entity WHERE (rule_id, version) IN "
                "(SELECT rule_id, version FROM rule WHERE created_by = %s)",
                ('asset_loader',))
    cur.execute("DELETE FROM rule_concept WHERE (rule_id, version) IN "
                "(SELECT rule_id, version FROM rule WHERE created_by = %s)",
                ('asset_loader',))
    # **只清草稿的差异清单。** 已发布版本的 release_item 是历史，不许碰。
    # 0025 已经拆掉了 release_item → rule 的外键，所以留着它们也不会
    # 挡住下面那条 DELETE FROM rule（发布是快照，不该被工作区的删除拖着走）。
    cur.execute("DELETE FROM release_item WHERE release_id IN "
                "(SELECT release_id FROM release WHERE status = 'draft')")
    cur.execute("DELETE FROM rule WHERE created_by = %s", ('asset_loader',))
    if kept:
        print("  保留 %d 条非装载脚本写入的规则（采纳入本体的）" % kept)
    for r in rules:
        lg = r.get("logic") or {}
        ext = lg.get("table") or lg.get("list") or lg.get("alias_table")
        if isinstance(ext, list):
            ext = ",".join(str(x) for x in ext)
        cfg = r.get("configurator") or {}
        bind = r.get("binding") or {}
        eff = r.get("effective") or {}
        b = bmap.get(r["rule_id"]) or {}
        # 规则清单里显式写了的以它为准，否则取绑定文件算出来的
        demo_exec = r["demo_executable"] if "demo_executable" in r else b.get("demo_executable")
        demo_blocker = r.get("demo_blocker") or b.get("demo_blocker")
        demo_scope = r.get("demo_scope") or b.get("demo_scope")
        cur.execute("""
            INSERT INTO rule (rule_id, version, name, dimension, risk_class, rule_class,
                source_clause, also_source, scope, logic, exception, priority, owner,
                effective_from, effective_to, algo_code, algo_type, control_type,
                configurator_status, compiled, compiled_reason, demo_executable,
                demo_blocker, demo_scope, binding_node, impl_path, feasibility,
                external_table, note, status, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,'draft','asset_loader')
        """, (
            r["rule_id"], RULE_VERSION, r["name"], r["dimension"], r["risk_class"],
            r["rule_class"], _j(r.get("source_clause")), _j(r.get("also_source")),
            _j(r.get("scope")), _j(lg), r.get("exception"), r.get("priority") or 100,
            r.get("owner"), eff.get("from"), eff.get("to"),
            cfg.get("algo_code"), cfg.get("algo_type"),
            cfg.get("control_type") or "警告", cfg.get("status"),
            bool(r.get("compiled")), r.get("compiled_reason"),
            bool(demo_exec), demo_blocker, demo_scope,
            bind.get("node"), bind.get("impl_path"), bind.get("feasibility"),
            ext, r.get("note"),
        ))
        for cid in lg.get("concepts") or []:
            # role：阈值位的概念单独标出来，契约校验对它更严
            role = "threshold" if r.get("threshold_source") else "read"
            cur.execute("INSERT INTO rule_concept (rule_id, version, concept_id, role) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (r["rule_id"], RULE_VERSION, cid, role))
        for eid in lg.get("entities") or []:
            cur.execute("INSERT INTO rule_entity (rule_id, version, entity_id, semantics) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (r["rule_id"], RULE_VERSION, eid,
                         lg.get("entities_semantics") or "存在性判断"))
        for key, rel in (("overrides", "overrides"), ("priority_over", "priority_over")):
            for other in (r.get(key) or []) if isinstance(r.get(key), list) else \
                         ([r[key]] if r.get(key) else []):
                cur.execute("INSERT INTO rule_relation (rule_id, version, rel_type, "
                            "other_rule, note) VALUES (%s,%s,%s,%s,%s)",
                            (r["rule_id"], RULE_VERSION, rel, str(other), None))
        cf = r.get("conflict")
        if cf:
            cur.execute("INSERT INTO rule_relation (rule_id, version, rel_type, other_rule, "
                        "note, needs_confirm, gap_id) VALUES (%s,%s,'conflicts_with',%s,%s,%s,%s)",
                        (r["rule_id"], RULE_VERSION, str(cf.get("with") or "?"),
                         _j(cf), bool(cf.get("needs_confirm")), cf.get("gap")))
    return len(rules)


def _lookup(conn, table_id, version, kind, source_file, source_rows, key_fields,
            fallback_key, on_miss, note, rows):
    cur = conn.cursor()
    cur.execute("DELETE FROM lookup_table WHERE table_id = %s", (table_id,))
    cur.execute("""INSERT INTO lookup_table (table_id, version, kind, source_file, source_rows,
                   row_count, key_fields, fallback_key, on_miss, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (table_id, version, kind, source_file, source_rows, len(rows),
                 _j(key_fields), _j(fallback_key), on_miss or "待人工", note))
    for row in rows:
        cur.execute("""INSERT INTO lookup_row (table_id, row_key, row_value, enabled,
                       confidence, source_rows, must_hit, must_not_hit, conflict, gap_id, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (table_id, _j(row["key"]), _j(row["value"]), row.get("enabled", True),
                     row.get("confidence"), row.get("source_rows"),
                     _j(row.get("must_hit")), _j(row.get("must_not_hit")),
                     _j(row.get("conflict")), row.get("gap"), row.get("note")))
    return len(rows)


def load_attachment_table(conn):
    d = yaml.safe_load(io.open(config.ATTACHMENT_TABLE_YAML, encoding="utf-8"))
    rows = []
    for r in d["rows"]:
        rows.append({
            "key": r["key"],
            "value": {"must": r["must"], "optional": r["optional"],
                      "variants": r.get("variants"),
                      "requires_trip_approval": r.get("requires_trip_approval", True)},
            "source_rows": r["row_count"],
            "conflict": r.get("conflict"),
            "gap": r.get("gap"),
            "note": r.get("note"),
        })
    # 别名**不混进决策表**：决策表的 row_count 要能和源表 153 行 / 29 键对账，
    # 混进 3 条别名就变成 32，对账口径立刻就不干净了。单独一张查找表。
    alias_rows = [{"key": {"alias": a["from"]},
                   "value": {"to_code": a.get("to_code"), "to": a.get("to"),
                             "scope": a.get("scope")},
                   "source_rows": a.get("hits"), "note": a.get("note")}
                  for a in d.get("expense_item_aliases") or []]
    _lookup(conn, "费用明细别名表", str(d["meta"]["version"]), "allow_list",
            d["meta"]["source_file"], None, ["alias"], None, "待人工",
            "费用项目名称由各单位自行维护，一定会继续长出新写法。不能靠改代码跟。"
            "实测：182 条费用明细行按名称直撞决策表命中 165 行（90.7%），"
            "加这 3 条别名后 174 行（95.6%）。", alias_rows)
    return _lookup(conn, "必要附件决策表", str(d["meta"]["version"]), "decision_table",
                   d["meta"]["source_file"], d["meta"]["source_rows"],
                   d["meta"]["key"], d["meta"].get("fallback_key"),
                   d["on_key_not_found"]["action"],
                   "29 行与源表 153 行逐行对账通过；必要缺失可拦，个性化缺失绝不拦截", rows)


def load_keywords(conn):
    d = yaml.safe_load(io.open(config.KEYWORDS_YAML, encoding="utf-8"))
    rows = []
    for group in ("sensitive_issuer_keywords", "sensitive_item_keywords",
                  "excluded_purpose_keywords"):
        for e in d[group]:
            rows.append({
                "key": {"group": group, "term": e["term"]},
                "value": {"category": e.get("category"), "match": e.get("match", "substring"),
                          "aliases": e.get("aliases") or [],
                          "exclusions": e.get("exclusions") or [],
                          "action": e.get("action"), "source": e.get("source")},
                # 外延推断的 10 个词默认不启用：每一个都会直接产生警告，
                # 不能默认开着上线。见 G-33。
                "enabled": e.get("confidence") != "外延推断",
                "confidence": e.get("confidence") or "原文明示",
                "must_hit": e.get("must_hit"),
                "must_not_hit": e.get("must_not_hit"),
                "gap": "G-33" if e.get("confidence") == "外延推断" else None,
                "note": e.get("note"),
            })
    return _lookup(conn, "敏感词表", str(d["meta"]["version"]), "keyword_list",
                   d["meta"]["source_file"], None,
                   ["group", "term"], None, "待人工",
                   "控制类型只能是警告；没有 must_not_hit 的词不许进表", rows)


def load_internal_units(conn):
    d = yaml.safe_load(io.open(config.INTERNAL_UNITS_YAML, encoding="utf-8"))
    rows = []
    for seg in ("verbatim", "directly_managed", "observed_with_place", "unconfirmed"):
        for e in d[seg]:
            rows.append({
                "key": {"segment": seg, "name": e["name"]},
                "value": {"aliases": e.get("aliases") or [], "city": e.get("city"),
                          "city_status": e.get("city_status"),
                          "occurrences": e.get("occurrences")},
                "enabled": bool(e.get("enabled")),
                "confidence": e.get("confidence") or "样本观测",
                "gap": e.get("gap"),
                "note": e.get("note"),
            })
    for e in d["external_denylist"]:
        rows.append({"key": {"segment": "external_denylist", "name": e["name"]},
                     "value": {"why": e["why"], "seen_in": e["seen_in"]},
                     "enabled": True, "confidence": "负面清单",
                     "note": "命中则绝不豁免"})
    return _lookup(conn, "内部单位名单", str(d["meta"]["version"]), "allow_list",
                   d["meta"]["source"]["file"], None, ["segment", "name"], None, "待人工",
                   "豁免名单：多收一个比少收一个贵（会连带 TR-D4-02 住宿费标准失效）", rows)


def load_standards(conn):
    """装载标准表。**装完必须自检**，这是踩出来的。

    第一版把字段名写错了（以为是 `region`、`base`，实际是 `province`+`area`、
    `base_rate.amount`），结果：
      · 住宿费 30 行全都插进去了，但 region 列**全是 NULL**
      · 伙食补助、市内交通**一行都没插**
    而装载器照样打印「标准表限额 30 行」，看上去一切正常。
    一个静默写 NULL 的装载器比一个报错的装载器坏得多——
    下游按地区查限额会查不到任何行，于是住宿费标准静默失效。

    所以这个函数末尾有一组断言：行数对不对、关键列是不是真的有值。
    对不上就抛异常，让整个事务回滚。
    """
    d = yaml.safe_load(io.open(config.STANDARDS_YAML, encoding="utf-8"))
    cur = conn.cursor()
    cur.execute("DELETE FROM standard_limit")

    acc = d["accommodation"]
    acc_rates = acc["rates"]
    n_acc = 0
    for r in acc_rates:
        # region 由 province + area 拼成。两段都要：「福建·福州市、泉州市、平潭综合实验区」
        # 与「福建·福建其他」是两档不同限额（380 / 350），只写 province 会把它们合并。
        region = "%s·%s" % (r.get("province") or "?", r.get("area") or "?")
        for key, tier in (("leader", "公司领导"), ("other", "其他人员")):
            amt = r.get(key)
            if not isinstance(amt, (int, float)):
                continue
            cur.execute("""INSERT INTO standard_limit (std_type, tier, region, limit_amount,
                           unit, source_clause, confidence, cross_checked)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE)""",
                        ("住宿费限额标准", tier, region, amt, "元/人·天",
                         "细则附件1（第 %s 页）" % r.get("page", "?"),
                         r.get("confidence") or "人眼读图"))
            n_acc += 1

    meal = d["meal_allowance"]
    n_meal = 0
    for r in meal["rates"]:
        cur.execute("""INSERT INTO standard_limit (std_type, tier, region, limit_amount,
                       unit, source_clause, confidence, cross_checked)
                       VALUES (%s,NULL,%s,%s,%s,%s,%s,%s)""",
                    ("伙食补助费标准", r["area"], r["amount"], meal["unit"],
                     meal["clause"], meal["confidence"], bool(meal.get("cross_check"))))
        n_meal += 1

    lt = d["local_transport"]
    base = lt["base_rate"]
    cur.execute("""INSERT INTO standard_limit (std_type, tier, region, limit_amount,
                   unit, source_clause, confidence, cross_checked)
                   VALUES (%s,NULL,NULL,%s,%s,%s,%s,%s)""",
                ("市内交通费标准", base["amount"], base["unit"], lt["clause"],
                 lt["confidence"], bool(lt.get("cross_check"))))
    n_lt = 1

    # 扣减档位不是限额，不塞进 standard_limit —— 硬塞会丢掉触发条件，
    # 而触发条件（「仅往返中转站」「两头均提供」）恰恰是判定的依据。
    # 40 元那一档漏了就会把样本里正常的 120 元判成异常值。
    tiers = [{"key": {"std": "市内交通费", "ord": i},
              "value": {"condition": t.get("condition"), "amount": t.get("amount"),
                        "unit": t.get("unit"), "rule": t.get("rule"), "note": t.get("note")},
              "confidence": lt["confidence"],
              "note": t.get("note")}
             for i, t in enumerate(lt["deduction_tiers"])]
    _lookup(conn, "市内交通费扣减档位", str(d["meta"]["version"]), "limit_table",
            lt["clause"], None, ["std", "ord"], None, "待人工",
            "80 元是基准档，还有 40 元（仅往返中转站）与 0 元（全程提供交通工具）等档。"
            "实测：样本「市内交通费补贴」金额为 80/120/160/240/320/720，"
            "120 = 80 + 40 正好对应 40 元档。漏掉这一档，120 会被当成异常值误报。", tiers)

    thr = [{"key": {"id": t.get("id") or ("TH-%02d" % i)},
            "value": {k: v for k, v in t.items() if k not in ("confidence",)},
            "confidence": t.get("confidence")}
           for i, t in enumerate(d["thresholds"], 1)]
    _lookup(conn, "金额阈值表", str(d["meta"]["version"]), "limit_table",
            "审核要点 1.3.9.1/1.3.9.2", None, ["id"], None, "待人工",
            "海运集团事权审批单 船舶3000/机关1000、5% 差额、福港 200 万大额支付", thr)

    # ── 自检：行数与关键列 ──────────────────────────────────────────────────
    expect_acc = sum(1 for r in acc_rates for k in ("leader", "other")
                     if isinstance(r.get(k), (int, float)))
    assert n_acc == expect_acc, "住宿费限额装载 %d 行，资产里应有 %d 行" % (n_acc, expect_acc)
    assert n_meal == len(meal["rates"]), "伙食补助装载 %d 行，应有 %d 行" % (n_meal, len(meal["rates"]))
    cur.execute("SELECT count(*) FROM standard_limit WHERE std_type='住宿费限额标准' AND region IS NULL")
    null_region = cur.fetchone()[0]
    assert null_region == 0, (
        "住宿费限额有 %d 行的 region 是 NULL。按地区查限额会查不到任何行，"
        "住宿费标准于是静默失效——这正是第一版踩的坑。" % null_region)
    cur.execute("SELECT count(*) FROM standard_limit WHERE std_type='住宿费限额标准' AND tier IS NULL")
    null_tier = cur.fetchone()[0]
    assert null_tier == 0, "住宿费限额有 %d 行的 tier 是 NULL" % null_tier
    cur.execute("SELECT count(DISTINCT region) FROM standard_limit WHERE std_type='住宿费限额标准'")
    n_region = cur.fetchone()[0]
    assert n_region == len(acc_rates), (
        "住宿费限额只有 %d 个不同地区，资产里有 %d 行费率——"
        "province+area 拼接若只用 province，福建的两档（380/350）会被合并" % (n_region, len(acc_rates)))
    return n_acc + n_meal + n_lt


def make_release(conn, rules):
    cur = conn.cursor()

    cur.execute("SELECT version FROM ontology_version WHERE is_current")
    row = cur.fetchone()
    onto_version = row[0] if row else "0.1"
    rs_version = "rs-0.1"
    compiled = sum(1 for r in rules if r.get("compiled"))
    # **必须用同一条连接的游标读。** db.scalar() 会另开一条连接，
    # 而此刻 rule 的插入还在本事务里没提交，另一条连接只能看到 0 行。
    # 第一次就是这么写的，结果 release.executable_count 写成了 0，
    # 而逐维度统计（事务提交后另开连接查的）是对的 17 ——两个数自相矛盾。
    # **带 version=1**：这个数要写进 release.executable_count，
    # 多一个版本就会把两版加起来，而发布资产上的数字是对外的。
    cur.execute("SELECT count(*) FROM rule WHERE version=1 AND demo_executable")
    executable = cur.fetchone()[0]

    # ── 装载只动工作区 ────────────────────────────────────────────────────
    # 这个脚本的地盘是**它自己建的那份草稿**：created_by='asset_loader'
    # 且 status='draft'。已发布 / 审阅中 / 已回滚的版本一律不碰，
    # release_current 也不清——那是发布管理（栏目五）的事。
    # 原来这里是 `DELETE FROM release_current` + 不分状态地删 rs-0.1，
    # 结果是「装载一次，当前生效版本就没了」。
    cur.execute("DELETE FROM release WHERE created_by = 'asset_loader' "
                "AND status = 'draft'")
    # rs_version 有唯一约束。若 rs-0.1 已经被签字发布过，就不能再占这个号——
    # 换一个带日期的号，并在输出里说清楚，不静默改名。
    cur.execute("SELECT count(*) FROM release WHERE rule_set_version = %s", (rs_version,))
    taken = cur.fetchone()[0] > 0
    if taken:
        import datetime
        cur.execute("SELECT count(*) FROM release WHERE rule_set_version LIKE %s",
                    ("rs-ws-%",))
        rs_version = "rs-ws-%s-%02d" % (datetime.date.today().strftime("%Y%m%d"),
                                        cur.fetchone()[0] + 1)
    # release_id 同样不能硬编码 rel-0001：那一版可能已经发布了。
    cur.execute("SELECT release_id FROM release WHERE release_id ~ '^rel-[0-9]+$'")
    nos = [int(r[0].split("-")[1]) for r in cur.fetchall()]
    rel_id = "rel-%04d" % ((max(nos) if nos else 0) + 1)

    cur.execute("""INSERT INTO release (release_id, rule_set_version, ontology_version, status,
                   rule_count, compiled_count, executable_count, diff_summary, created_by, note)
                   VALUES (%s,%s,%s,'draft',%s,%s,%s,%s,'asset_loader',%s)""",
                (rel_id, rs_version, onto_version, len(rules), compiled, executable,
                 _j({"added": len(rules), "modified": 0, "retired": 0,
                     "note": "装载脚本建的工作区草稿，内容未冻结"}),
                 "装载脚本自动建的**工作区草稿**：frozen_at 为空，八层快照没抄，"
                 "所以它不能送审也不能发布。要发版请到发布管理页点「冻结为新版本」。"))
    for r in rules:
        cur.execute("INSERT INTO release_item (release_id, rule_id, version, change) "
                    "VALUES (%s,%s,%s,'added')", (rel_id, r["rule_id"], RULE_VERSION))
    return rs_version, compiled, executable


# ── 装载前的闸 ──────────────────────────────────────────────────────────────
def _wanted():
    """规则清单里引用了哪些概念/实体，绑定文件里绑了哪些节点。"""
    rules = json.load(io.open(config.RULES_JSON, encoding="utf-8"))["rules"]
    c, e = set(), set()
    for r in rules:
        lg = r.get("logic") or {}
        c.update(lg.get("concepts") or [])
        e.update(lg.get("entities") or [])
    bind = json.load(io.open(config.BINDING_JSON, encoding="utf-8"))
    n = {b.get("node") for b in bind["bindings"] if b.get("node")}
    return c, e, n


def precheck():
    """**装载规则资产之前必须先装本体。** 这道闸回答的是「点反了顺序会怎样」。

    实测依据：`rule_concept` 上**没有**指向 `ontology_concept` 的外键
    （pg_constraint 里只有指向 rule 的那一条）。所以本体没装、或者库里装的
    还是旧版本时，规则的概念引用会**静默**落进库里指向不存在的概念——
    不报错、不回滚，只是从此每条引用它的规则都判不出，而页面上一切正常。
    这种「不报错的错」比报错的错贵得多，所以闸放在装载器自己身上：
    命令行和页面按钮共用这一道，谁都绕不过去（要绕得显式 --force）。

    两档，和产品其它地方的口径一致：
      blocker   装下去会产生错数据      → 拦住
      warning   库里已经有的老问题       → 只报不拦（拦了今天就没法装）

    返回 {ok, checks:[{check, level, ok, detail}], reason}
    """
    checks = []
    cur = db.query_one("SELECT version, source_hash FROM ontology_version "
                       "WHERE is_current")
    checks.append({
        "check": "本体已装载并切为当前版本", "level": "blocker", "ok": bool(cur),
        "detail": ("当前版本 %s" % cur["version"]) if cur else
                  "库里没有 is_current 的本体版本。先跑第②步「装载本体源文件」："
                  "规则要引用本体里的概念，本体不在，引用就是空的。"})

    h = hashlib.sha256(io.open(config.ONTOLOGY_YAML, "rb").read()).hexdigest()
    in_sync = bool(cur and cur["source_hash"] == h)
    checks.append({
        "check": "本体源文件与库同步", "level": "blocker", "ok": in_sync,
        "detail": "源文件 sha256 与库里一致" if in_sync else
                  "本体源文件改过但没重装（库里的 hash 对不上）。"
                  "现在装规则，规则引用的是**旧本体**里的概念。先重跑第②步。"})

    ver = cur["version"] if cur else None
    want_c, want_e, want_n = _wanted()
    have_c = {r["concept_id"] for r in db.query(
        "SELECT concept_id FROM ontology_concept WHERE version = %s", (ver,))}
    have_e = {r["entity_id"] for r in db.query(
        "SELECT entity_id FROM ontology_entity WHERE version = %s", (ver,))}
    have_n = {r["node_id"] for r in db.query(
        "SELECT node_id FROM process_node WHERE version = %s", (ver,))}
    miss_c, miss_e, miss_n = sorted(want_c - have_c), sorted(want_e - have_e), \
        sorted(want_n - have_n)
    checks.append({
        "check": "规则引用的概念都在本体里", "level": "blocker", "ok": not miss_c,
        "detail": "引用 %d 个，全部命中" % len(want_c) if not miss_c else
                  "引用 %d 个，缺 %d 个：%s。装下去这些引用会指向不存在的概念，"
                  "而且不会报错。" % (len(want_c), len(miss_c), "、".join(miss_c[:8]))})
    checks.append({
        "check": "规则引用的实体都在本体里", "level": "blocker", "ok": not miss_e,
        "detail": "引用 %d 个，全部命中" % len(want_e) if not miss_e else
                  "缺 %d 个：%s" % (len(miss_e), "、".join(miss_e[:8]))})
    # 节点只警告不拦：实测现在就有一个 OUT 绑不上（process_node 里查无此节点），
    # 拦住等于今天谁也装不了。它是 P4「可得性按节点」那一包要清的账，
    # 在那之前先让它在每次装载时露一次脸，而不是躺在库里没人知道。
    checks.append({
        "check": "规则绑定的节点都在流程本体里", "level": "warning", "ok": not miss_n,
        "detail": "绑定 %d 个节点，全部命中" % len(want_n) if not miss_n else
                  "绑定 %s，其中 %s 在 process_node 里查无此节点。"
                  "rule.binding_node 没有外键，所以不会报错——这条是**已知欠账**，"
                  "归 P4 清理。" % ("、".join(sorted(want_n)), "、".join(miss_n))})

    bad = [c for c in checks if c["level"] == "blocker" and not c["ok"]]
    return {"ok": not bad, "checks": checks,
            "reason": bad[0]["detail"] if bad else None}


def load_all(verbose=True, force=False):
    gate = precheck()
    if not gate["ok"] and not force:
        # **拒绝装载，不是警告后继续。** 继续的后果是一批静默的坏引用，
        # 而调用方（按钮或命令行）从返回值里看不出来。
        raise RuntimeError("规则资产装载被拦下：" + gate["reason"])
    if not gate["ok"] and force:
        print("**--force：明知有问题仍然装载** —— " + gate["reason"])
    rules = json.load(io.open(config.RULES_JSON, encoding="utf-8"))["rules"]
    # **装载期间占住 rule 这一组。** 整个装载已经是一个事务（原子的），
    # 但原子不等于互斥：两个装载同时跑，PG 层面是「后提交的赢」，
    # 而两边的 release 草稿都已经建出来了，库里会多一份没人认领的 draft。
    # 占住之后第二个调用方当场收到「正在装载」，而不是等锁等到超时。
    with db.table_lock("rule", who="规则资产装载 rules.loader.load_all"), db.tx() as conn:
        n_rules = load_rules(conn, rules)
        n_att = load_attachment_table(conn)
        n_kw = load_keywords(conn)
        n_unit = load_internal_units(conn)
        n_std = load_standards(conn)
        rs_version, compiled, executable = make_release(conn, rules)
    if verbose:
        print("规则资产已装载")
        print("  规则            %d 条（可编译 %d / 演示可执行 %d）" % (n_rules, compiled, executable))
        print("  必要附件决策表   %d 行" % n_att)
        print("  敏感词表        %d 词（启用 %s）" % (
            n_kw, db.scalar("SELECT count(*) FROM lookup_row WHERE table_id='敏感词表' AND enabled")))
        print("  内部单位名单     %d 条（启用 %s）" % (
            n_unit, db.scalar("SELECT count(*) FROM lookup_row WHERE table_id='内部单位名单' AND enabled")))
        print("  标准表限额       %d 行" % n_std)
        print("  release         %s（draft，未签字，不可用于生产）" % rs_version)
        print("  规则概念绑定     %s 条" % db.scalar("SELECT count(*) FROM rule_concept"))
        print("  规则实体绑定     %s 条" % db.scalar("SELECT count(*) FROM rule_entity"))
        print("  规则关系        %s 条（其中需裁决 %s 条）" % (
            db.scalar("SELECT count(*) FROM rule_relation"),
            db.scalar("SELECT count(*) FROM rule_relation WHERE needs_confirm")))
    return {"rules": n_rules, "compiled": compiled, "executable": executable,
            "attachment_rows": n_att, "keywords": n_kw, "units": n_unit,
            "standards": n_std, "release": rs_version,
            # 警告跟着结果一起回去：装成了不等于没问题
            "warnings": [c for c in gate["checks"]
                         if c["level"] == "warning" and not c["ok"]]}


def status():
    # 一律按 version=1 报。命令行的数和页面上的数对不上，排错时最费时间。
    print("规则：%s 条" % db.scalar("SELECT count(*) FROM rule WHERE version=1"))
    for row in db.query("""SELECT dimension, count(*) n,
                           sum(CASE WHEN compiled THEN 1 ELSE 0 END) c,
                           sum(CASE WHEN demo_executable THEN 1 ELSE 0 END) d
                           FROM rule WHERE version=1
                           GROUP BY dimension ORDER BY dimension"""):
        print("  %-4s 共 %2d  可编译 %2d  演示可执行 %2d" % (
            row["dimension"], row["n"], row["c"], row["d"]))
    print("查找表：")
    for row in db.query("SELECT table_id, kind, row_count, source_rows, on_miss "
                        "FROM lookup_table ORDER BY table_id"):
        print("  %-14s %-15s %3d 行  源表 %-5s 未命中→%s" % (
            row["table_id"], row["kind"], row["row_count"],
            row["source_rows"] if row["source_rows"] is not None else "-", row["on_miss"]))
    r = db.query_one("SELECT * FROM release ORDER BY created_at DESC LIMIT 1")
    if r:
        print("release %s  %s  规则 %d / 可编译 %d / 演示可执行 %d  签字人=%s" % (
            r["release_id"], r["status"], r["rule_count"], r["compiled_count"],
            r["executable_count"], r["reviewed_by"] or "（未签字）"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="只跑装载前的闸，不写库")
    ap.add_argument("--force", action="store_true",
                    help="闸没过也装（会打印原因）。**正常不要用。**")
    args = ap.parse_args()
    if args.status:
        return status()
    if args.check:
        g = precheck()
        for c in g["checks"]:
            print("  [%s] %-24s %s" % (
                "过" if c["ok"] else ("拦" if c["level"] == "blocker" else "警"),
                c["check"], c["detail"]))
        print("闸：%s" % ("过" if g["ok"] else "没过 —— " + g["reason"]))
        return 0 if g["ok"] else 1
    load_all(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
