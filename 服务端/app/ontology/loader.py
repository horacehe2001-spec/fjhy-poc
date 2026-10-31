# -*- coding: utf-8 -*-
"""把 `本体_v0.1.yaml` 装载进库。

YAML 是源，库里是投影。**单向，永不回写**——
回写会走「解析 → 序列化」的路子，yaml.dump 把全文注释一次性抹光，
而注释里是口径说明和踩坑记录，这种损失在 code review 里极难发现。
要改本体就改 YAML，然后重装。

用法：
    python -I -m app.ontology.loader              # 装载并切成当前版本
    python -I -m app.ontology.loader --check      # 只比对指纹，不写库
"""
import argparse
import hashlib
import io
import json
import os
import sys

import yaml

if __name__ == "__main__" and __package__ is None:      # 允许直接 python 路径执行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config, db  # noqa: E402


def file_hash(path):
    return hashlib.sha256(io.open(path, "rb").read()).hexdigest()


def yaml_for(instance_id):
    """这个实例的本体源文件路径。

    **每实例一份（0044 之后）。** 本体_v0.1.yaml 是福建海运集团的
    （meta.entity 写着财智分公司，rank_to_tier 的 subject 是它的组织名）；
    别的实例要有自己那一份，命名 本体_<实例id>_v0.1.yaml，和主文件并排放。

    找不到就报错，**绝不回退到主文件**——回退的后果是
    「822港航 装载成功，装进去的是海运集团的本体」，而两边页面都不显示这件事。
    """
    if instance_id == YAML_OWNER_INSTANCE:
        return config.ONTOLOGY_YAML
    path = os.path.join(os.path.dirname(config.ONTOLOGY_YAML),
                        "本体_%s_v0.1.yaml" % instance_id)
    if not os.path.exists(path):
        raise FileNotFoundError(
            "实例「%s」还没有自己的本体源文件。期望路径：%s｜"
            "本体已按实例隔离（迁移 0044），每个实例一份 YAML。"
            "**不会退回用 %s** —— 那是「%s」的本体，装进来会以另一家的口径"
            "定义实体、概念和枚举，而页面上看不出来。"
            % (instance_id, path, os.path.basename(config.ONTOLOGY_YAML),
               YAML_OWNER_INSTANCE))
    return path


def load_yaml(path=None):
    return yaml.safe_load(io.open(path or config.ONTOLOGY_YAML, encoding="utf-8"))


def fact_model():
    return json.load(io.open(config.FACT_MODEL_JSON, encoding="utf-8"))


def _availability_map():
    """概念 → (可得性, 缺口号)。来自 规则绑定_v0.1.json，它是实测结果。"""
    bind = json.load(io.open(config.BINDING_JSON, encoding="utf-8"))
    return {cid: (v.get("availability"), v.get("gap"))
            for cid, v in bind["concept_availability"].items()}


# ── 本体与实例的关系（2026-10-30 定的口径，可以推翻，但要连这段注释一起改）──
#
# 本体投影分两类，**隔离粒度不同**：
#
#   A. ontology_* / process_*（实体、概念、枚举、关系、流程节点与迁移）
#      = **领域模型**，描述的是「差旅费报销」这件事的结构：报账单有哪些字段、
#        发票有哪些类型、审批有哪几步。它不是某一家公司的数值。
#      → **两个实例共用一份**。这些表上也确实没有 instance_id 列，
#        要隔离是一场涉及 8 张表 + enum_value_current + 20 多处外键的迁移。
#
#   B. rank_tier_map（职级 → 住宿费档位）
#      = **实例数据**：它是某一家公司某一条制度的投影
#        （本 YAML 里是 福建海运集团 的「细则第十二条」，subject 写着「财智分公司」）。
#      → 它有 instance_id（迁移 0040），**必须按实例收口**。
#
# **最要紧的一条：rank_to_tier 只写给这份 YAML 所属的实例。**
# 822港航 装载时写 0 行，并在 stats 里如实报出来。
# 拿 福建海运集团 的职级档位去套 822港航，表现是「住宿费超标判定照常出结论」，
# 而那个结论依据的是另一家公司的制度——**比判不出来危险得多**：
# 判不出来会转人工，判错了没有任何人会发现。
#
# 而且原来这里是 `DELETE FROM rank_tier_map`（整表）+ 不带 instance_id 的 INSERT。
# 后者撞上 0040 的非空约束直接报错（2026-10-30 实测，822港航 点「一键搭建」时），
# 那个报错阴差阳错挡住了前者——否则 822港航 装一次本体就会把
# 福建海运集团 的 9 行职级档位全删掉。
YAML_OWNER_INSTANCE = "fjhy"


def load(version=None, make_current=True, verbose=True, instance_id=None):
    from app import scope
    iid = scope.require(instance_id)
    yaml_path = yaml_for(iid)
    onto = load_yaml(yaml_path)
    version = version or str(onto.get("meta", {}).get("version") or "0.1")
    h = file_hash(yaml_path)
    avail = _availability_map()

    biz = onto["business"]
    entities = biz["entities"]
    enums = biz["enums"]
    relations = biz.get("relations") or []
    proc = onto.get("process") or {}
    # `bypasses:` 下面的节点**就是旁路**，这里给它打上标记。
    # 原来两个列表直接相加，而 is_bypass 只认节点自己写的 `bypass: true`——
    # 福建海运集团的 B01 撤回 / B02 审批人和报账人相同跳过都写在 bypasses: 下面、
    # 都没写 bypass 字段，于是入库时 is_bypass=false，前端那条「旁路画虚线」
    # 的样式一直没生效。**分组本身就是声明**，不该再要求写一遍。
    nodes = list(proc.get("nodes") or [])
    for n in (proc.get("bypasses") or []):
        nodes.append(dict(n, bypass=True))
    transitions = proc.get("transitions") or []

    n_concepts = sum(len(e.get("concepts") or []) for e in entities)
    # 职级档位只写给 YAML 所属的实例（见文件顶上那段口径注释）。
    # **这个判断要在这里做**：stats 下面就被 json.dumps 进 ontology_version 了，
    # 之后再改这个 dict，库里记的数和返回值就对不上——而那种对不上
    # 只有在有人同时看页面和查库时才会发现。
    rank_rows = (biz.get("rank_to_tier") or []) if iid == YAML_OWNER_INSTANCE else []
    stats = {"entities": len(entities), "concepts": n_concepts,
             "enums": len(enums), "relations": len(relations),
             "process_nodes": len(nodes), "process_transitions": len(transitions),
             "rank_to_tier": len(rank_rows), "instance_id": iid}
    if iid != YAML_OWNER_INSTANCE:
        stats["rank_to_tier_skipped"] = len(biz.get("rank_to_tier") or [])
        stats["rank_to_tier_note"] = (
            "本 YAML 的职级档位属于实例「%s」（细则第十二条，subject 是它的组织名），"
            "**没有写给当前实例「%s」**。要让它判得出住宿费档位，"
            "得从这个实例自己的《住宿标准》表另建一份映射。"
            % (YAML_OWNER_INSTANCE, iid))

    # **装载期间占住 ontology 这一组**（ontology_* / process_*）。
    # 装载是一个事务，但两个装载同时跑仍然是「后提交的赢」，而规则资产装载的
    # 闸（precheck）读的是本体投影——投影正被另一条链整组重建时，
    # precheck 读到的是半份本体，它会放行，然后留下一批静默的坏引用。
    with db.table_lock("ontology", who="本体装载 ontology.loader.load"), db.tx() as conn:
        cur = conn.cursor()
        # 同版本重装：先清掉旧投影，避免「YAML 删了一个概念但库里还留着」
        #
        # **这一整段是跨实例共用的，不是漏加实例条件**（见文件顶上 A 类的说明）。
        # 两个实例装的是同一份 YAML，所以重建出来的内容逐字相同，
        # 谁先谁后都一样。哪天真要做「每实例一份本体」，改的是这里和
        # ontology_* 那八张表的结构，不是在这句上补一个 WHERE。
        cur.execute("DELETE FROM ontology_version WHERE instance_id=%s AND version=%s",
                    (iid, version))
        cur.execute("INSERT INTO ontology_version (instance_id, version, source_file, "
                    "source_hash, is_current, stats) VALUES (%s, %s, %s, %s, FALSE, %s)",
                    (iid, version, os.path.basename(yaml_path), h, json.dumps(stats)))

        for e in entities:
            cur.execute("INSERT INTO ontology_entity (instance_id, entity_id, version, "
                        "name, source, note, alias) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (iid, e["id"], version, e["name"], e.get("source"),
                         _text(e.get("note")), list(e.get("alias") or [])))
            for c in e.get("concepts") or []:
                a, g = avail.get(c["id"], (None, None))
                cur.execute(
                    "INSERT INTO ontology_concept (instance_id, concept_id, version, "
                    "entity_id, name, "
                    "data_type, enum_id, ref_entity, source, availability, gap_id, "
                    # **alias 必须一起载入。** 别名是口径信息：客户材料里同一个字段
                    # 在不同表里叫不同名字（C0107 核算主体 ↔ 明细表里的「报账人公司」）。
                    # 丢了它，下游比对列头时会把别名报成「缺概念」，照着去新增
                    # 就在本体里长出一条重复概念——**重复概念比缺概念更糟**：
                    # 同一个字段从此有两套口径，而两边都「有出处」。
                    "sensitive, ocr_sourced, note, alias) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (iid, c["id"], version, e["id"], c["name"], c.get("type") or "string",
                     c.get("enum"), c.get("ref"), c.get("source"),
                     a or "未分类", g, bool(c.get("sensitive")), bool(c.get("ocr")),
                     _text(c.get("note")), list(c.get("alias") or [])))

        for en in enums:
            cur.execute("INSERT INTO ontology_enum (instance_id, enum_id, version, name, "
                        "source, note) VALUES (%s,%s,%s,%s,%s,%s)",
                        (iid, en["id"], version, en["name"], en.get("source"),
                         _text(en.get("note"))))
            for i, v in enumerate(en.get("values") or []):
                cur.execute("INSERT INTO ontology_enum_value (instance_id, version, "
                            "enum_id, value, ord) VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT DO NOTHING",
                            (iid, version, en["id"], str(v), i))

        for r in relations:
            cur.execute("INSERT INTO ontology_relation (instance_id, version, from_entity, "
                        "to_entity, rel_type, cardinality, source, note) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (iid, version, r["from"], r["to"], r["type"],
                         r.get("card"), r.get("source"), _text(r.get("note"))))

        for n in nodes:
            # occurrences 故意留空：YAML 里的 observed 是「第一版解析器漏了 24%」
            # 那一版留下的数（N01=67 而实测「开始」103 次、N05=97 而实测 125 次）。
            # **载入一组已知是错的数字比没有这列更糟**——页面上会拿它当实测值显示。
            # 实测次数一律从 approval_record 现算。
            cur.execute("INSERT INTO process_node (instance_id, node_id, version, name, "
                        "is_bypass, occurrences, note, alias) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (instance_id, version, node_id) DO NOTHING",
                        (iid, n.get("id") or n.get("node"), version, n.get("name") or "",
                         bool(n.get("bypass") or n.get("is_bypass")),
                         None, _text(n.get("note")), list(n.get("alias") or [])))

        for t in transitions:
            # blocking：只有 N04→N05 的「AI审核不通过」是非阻断。
            # 这不是建模偏好，是实测：FJPG-BX2610130821 在 2026-10-13 17:25:03
            # 得到「AI审核不通过」，17:35:41 仍进入共享审核并由人工给出退回理由。
            cond = t.get("cond") or ""
            blocking = not (t.get("from") == "N04" and "不通过" in cond)
            cur.execute("INSERT INTO process_transition (instance_id, version, from_node, "
                        "to_node, cond, blocking, note) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (iid, version, t["from"], t["to"], cond, blocking,
                         _text(t.get("note"))))

        # 职级 → 住宿费标准档位。**映射带主体条件，不是恒等映射。**
        # 放在本体里而不是代码里，是因为它是制度条款的投影（细则第十二条），
        # 制度改版时跟着本体一起换版。
        # **只删本实例那一片。** 整表删会把另一个实例的职级档位一起带走，
        # 而那边页面上只会显示「住宿费判不出档位」，看不出是被谁清的。
        cur.execute("DELETE FROM rank_tier_map WHERE instance_id = %s", (iid,))
        for m in rank_rows:
            cur.execute("INSERT INTO rank_tier_map (instance_id, subject, rank_level, "
                        "tier, clause, confidence, on_unknown, note) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (iid, m["subject"], m.get("rank"), m.get("tier"),
                         m.get("clause") or "细则", m.get("confidence") or "人眼读图",
                         m.get("on_unknown"), _text(m.get("note"))))

        if make_current:
            # **只动本实例那一行。** 不带实例条件会把对面的「当前版本」也摘掉，
            # 而那边页面只显示「没有当前本体版本」，看不出是被谁摘的。
            cur.execute("UPDATE ontology_version SET is_current = FALSE "
                        "WHERE instance_id=%s AND is_current", (iid,))
            cur.execute("UPDATE ontology_version SET is_current = TRUE "
                        "WHERE instance_id=%s AND version=%s", (iid, version))
            # enum_value_current 整表重建：业务表的枚举外键只该受当前版本约束，
            # 不该被历史版本的取值放行。
            # **同样是跨实例共用的**：枚举取值属于领域模型（发票类型有哪几种），
            # 不是某一家的数值。两个实例装同一份 YAML，重建结果相同。
            cur.execute("DELETE FROM enum_value_current WHERE instance_id=%s", (iid,))
            cur.execute("INSERT INTO enum_value_current (instance_id, enum_id, value) "
                        "SELECT instance_id, enum_id, value FROM ontology_enum_value "
                        " WHERE instance_id=%s AND version=%s", (iid, version))

    if verbose:
        print("本体 %s 已装载（源文件指纹 %s…）" % (version, h[:12]))
        for k, v in stats.items():
            print("  %-20s %d" % (k, v))
        n_enum_values = db.scalar("SELECT count(*) FROM enum_value_current")
        print("  %-20s %d" % ("enum_value_current", n_enum_values))
    return stats


def _text(v):
    """note 可能是 str / list / dict，统一成文本。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def check():
    """比对库里的指纹与当前 YAML。不一致就是漂移。"""
    row = db.query_one("SELECT version, source_hash, loaded_at, stats FROM ontology_version "
                       "WHERE is_current")
    if not row:
        print("库里没有当前本体版本，需要先装载。")
        return 1
    h = file_hash(config.ONTOLOGY_YAML)
    same = row["source_hash"] == h
    print("当前版本 v%s，装载于 %s" % (row["version"], row["loaded_at"]))
    print("  库里指纹 %s…" % row["source_hash"][:12])
    print("  文件指纹 %s…  %s" % (h[:12], "一致" if same else "**不一致，本体已漂移**"))
    print("  stats: %s" % json.dumps(row["stats"], ensure_ascii=False))
    return 0 if same else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--version")
    args = ap.parse_args()
    if args.check:
        return check()
    load(version=args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
