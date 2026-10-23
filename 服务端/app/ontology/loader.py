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


def load_yaml():
    return yaml.safe_load(io.open(config.ONTOLOGY_YAML, encoding="utf-8"))


def fact_model():
    return json.load(io.open(config.FACT_MODEL_JSON, encoding="utf-8"))


def _availability_map():
    """概念 → (可得性, 缺口号)。来自 规则绑定_v0.1.json，它是实测结果。"""
    bind = json.load(io.open(config.BINDING_JSON, encoding="utf-8"))
    return {cid: (v.get("availability"), v.get("gap"))
            for cid, v in bind["concept_availability"].items()}


def load(version=None, make_current=True, verbose=True):
    onto = load_yaml()
    version = version or str(onto.get("meta", {}).get("version") or "0.1")
    h = file_hash(config.ONTOLOGY_YAML)
    avail = _availability_map()

    biz = onto["business"]
    entities = biz["entities"]
    enums = biz["enums"]
    relations = biz.get("relations") or []
    proc = onto.get("process") or {}
    nodes = (proc.get("nodes") or []) + (proc.get("bypasses") or [])
    transitions = proc.get("transitions") or []

    n_concepts = sum(len(e.get("concepts") or []) for e in entities)
    stats = {"entities": len(entities), "concepts": n_concepts,
             "enums": len(enums), "relations": len(relations),
             "process_nodes": len(nodes), "process_transitions": len(transitions),
             "rank_to_tier": len(biz.get("rank_to_tier") or [])}

    # **装载期间占住 ontology 这一组**（ontology_* / process_*）。
    # 装载是一个事务，但两个装载同时跑仍然是「后提交的赢」，而规则资产装载的
    # 闸（precheck）读的是本体投影——投影正被另一条链整组重建时，
    # precheck 读到的是半份本体，它会放行，然后留下一批静默的坏引用。
    with db.table_lock("ontology", who="本体装载 ontology.loader.load"), db.tx() as conn:
        cur = conn.cursor()
        # 同版本重装：先清掉旧投影，避免「YAML 删了一个概念但库里还留着」
        cur.execute("DELETE FROM ontology_version WHERE version = %s", (version,))
        cur.execute("INSERT INTO ontology_version (version, source_file, source_hash, "
                    "is_current, stats) VALUES (%s, %s, %s, FALSE, %s)",
                    (version, os.path.basename(config.ONTOLOGY_YAML), h, json.dumps(stats)))

        for e in entities:
            cur.execute("INSERT INTO ontology_entity (entity_id, version, name, source, "
                        "note, alias) VALUES (%s,%s,%s,%s,%s,%s)",
                        (e["id"], version, e["name"], e.get("source"),
                         _text(e.get("note")), list(e.get("alias") or [])))
            for c in e.get("concepts") or []:
                a, g = avail.get(c["id"], (None, None))
                cur.execute(
                    "INSERT INTO ontology_concept (concept_id, version, entity_id, name, "
                    "data_type, enum_id, ref_entity, source, availability, gap_id, "
                    # **alias 必须一起载入。** 别名是口径信息：客户材料里同一个字段
                    # 在不同表里叫不同名字（C0107 核算主体 ↔ 明细表里的「报账人公司」）。
                    # 丢了它，下游比对列头时会把别名报成「缺概念」，照着去新增
                    # 就在本体里长出一条重复概念——**重复概念比缺概念更糟**：
                    # 同一个字段从此有两套口径，而两边都「有出处」。
                    "sensitive, ocr_sourced, note, alias) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (c["id"], version, e["id"], c["name"], c.get("type") or "string",
                     c.get("enum"), c.get("ref"), c.get("source"),
                     a or "未分类", g, bool(c.get("sensitive")), bool(c.get("ocr")),
                     _text(c.get("note")), list(c.get("alias") or [])))

        for en in enums:
            cur.execute("INSERT INTO ontology_enum (enum_id, version, name, source, note) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (en["id"], version, en["name"], en.get("source"), _text(en.get("note"))))
            for i, v in enumerate(en.get("values") or []):
                cur.execute("INSERT INTO ontology_enum_value (version, enum_id, value, ord) "
                            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (version, en["id"], str(v), i))

        for r in relations:
            cur.execute("INSERT INTO ontology_relation (version, from_entity, to_entity, "
                        "rel_type, cardinality, source, note) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (version, r["from"], r["to"], r["type"],
                         r.get("card"), r.get("source"), _text(r.get("note"))))

        for n in nodes:
            # occurrences 故意留空：YAML 里的 observed 是「第一版解析器漏了 24%」
            # 那一版留下的数（N01=67 而实测「开始」103 次、N05=97 而实测 125 次）。
            # **载入一组已知是错的数字比没有这列更糟**——页面上会拿它当实测值显示。
            # 实测次数一律从 approval_record 现算。
            cur.execute("INSERT INTO process_node (node_id, version, name, is_bypass, "
                        "occurrences, note, alias) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (version, node_id) DO NOTHING",
                        (n.get("id") or n.get("node"), version, n.get("name") or "",
                         bool(n.get("bypass") or n.get("is_bypass")),
                         None, _text(n.get("note")), list(n.get("alias") or [])))

        for t in transitions:
            # blocking：只有 N04→N05 的「AI审核不通过」是非阻断。
            # 这不是建模偏好，是实测：FJPG-BX2610130821 在 2026-10-13 17:25:03
            # 得到「AI审核不通过」，17:35:41 仍进入共享审核并由人工给出退回理由。
            cond = t.get("cond") or ""
            blocking = not (t.get("from") == "N04" and "不通过" in cond)
            cur.execute("INSERT INTO process_transition (version, from_node, to_node, cond, "
                        "blocking, note) VALUES (%s,%s,%s,%s,%s,%s)",
                        (version, t["from"], t["to"], cond, blocking, _text(t.get("note"))))

        # 职级 → 住宿费标准档位。**映射带主体条件，不是恒等映射。**
        # 放在本体里而不是代码里，是因为它是制度条款的投影（细则第十二条），
        # 制度改版时跟着本体一起换版。
        cur.execute("DELETE FROM rank_tier_map")
        for m in biz.get("rank_to_tier") or []:
            cur.execute("INSERT INTO rank_tier_map (subject, rank_level, tier, clause, "
                        "confidence, on_unknown, note) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (m["subject"], m.get("rank"), m.get("tier"),
                         m.get("clause") or "细则", m.get("confidence") or "人眼读图",
                         m.get("on_unknown"), _text(m.get("note"))))

        if make_current:
            cur.execute("UPDATE ontology_version SET is_current = FALSE WHERE is_current")
            cur.execute("UPDATE ontology_version SET is_current = TRUE WHERE version = %s",
                        (version,))
            # enum_value_current 整表重建：业务表的枚举外键只该受当前版本约束，
            # 不该被历史版本的取值放行。
            cur.execute("DELETE FROM enum_value_current")
            cur.execute("INSERT INTO enum_value_current (enum_id, value) "
                        "SELECT enum_id, value FROM ontology_enum_value WHERE version = %s",
                        (version,))

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
