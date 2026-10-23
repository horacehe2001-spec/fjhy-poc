# -*- coding: utf-8 -*-
"""发布管理：把 文件 → 规则 → 三元本体 冻成一个版本，留给别的业务消费。

用户 2026-10-19 的四条口径，这个模块就是照它写的：

  ① **三元本体 = 业务 / 规则 / 流程**（不是 RDF 三元组），
     对应 本体_v0.1.yaml 的 business / rule_schema / process 三段。
  ② **快照冻结**：发布时把八层内容原样抄进 release_asset 并算内容 sha256。
     之后改 YAML、重装规则、文件库重扫，都动不了已发布的那一份。
  ③ **允许被其他业务消费**：只读 API + 可下载资产包（见 app/api/pub.py）。
  ④ **装载只动工作区**：装载与采纳改的是工作区，已发布快照与当前指针不动。

哈希口径（整个模块只有这一条规矩，但它决定了所有「变没变」的结论）：
  · 内容哈希算在**规范化 JSON** 上（sort_keys、ensure_ascii=False、无空格）。
    不规范化的话，同一份内容两次 dumps 键序不同，哈希就不同，这个字段
    就证明不了任何事。
  · payload 里**不放易变的簿记字段**：rule.created_at、文件的 mtime 与
    first/last_seen 一律剔掉。理由是实测过的：重装一次规则，内容一个字没改，
    created_at 全变 now()，若进哈希，「这一版和上一版一样」就永远判不出来。
    文件身份用**内容 sha256** 认，不用修改时间认——拷一次盘 mtime 就变，内容没变。

出口安全（CLAUDE.md 第 8 条）：快照会经 /api/pub 原样送出去，所以它本身是个出口。
  · 规则名与本体元素名在 payload 里一律叫 rule_name / entity_name / concept_name /
    enum_name / node_name，**不叫 name**——叫 name 会被人名脱敏按字段名吃成
    「付**********」。上一轮刚踩过这个坑，这里从源头就不用那个键名。
  · 字符串里的卡号/手机号/密钥/闭集合人名由 app/api/pub.py 在出口处过
    mask.mask_text（按内容判定），不走按字段名判定的那套。
"""
import datetime
import hashlib
import io
import json
import os

import yaml

from . import config, db, onto_yaml
from .rules import engine as rule_engine

# 八层。ord 决定页面与资产包里的顺序：文件 → 规则 → 三元本体 → 派生。
LAYERS = [
    ("文件", 1, "库+文件", "doc_lib_file",
     "**冻的是清单不是文件本身**：资产包里不塞客户的原始文档，塞的是"
     "「用了哪些、内容指纹是什么」。要原件请走文件库那一栏。"),
    ("规则", 2, "库", "rule",
     "引擎执行的那 N 条。含 expr/控制类型/来源条款，不含 created_at（簿记字段）。"),
    ("规则参数", 3, "文件", "规则资产/*.yaml",
     "标准表 / 敏感词表 / 必要附件决策表 / 内部单位名单。**与规则同等重要**："
     "限额数字改了、规则一个字没动，判定结果照样变。"),
    ("本体源文件", 4, "文件", "本体_v0.1.yaml",
     "**带注释的源头原文。** 下面三层是它的库投影，投影只剩结构，"
     "而 925 行里的口径说明与踩坑记录才是本体资产最贵的部分。"
     "权威性也在这里：YAML 是源头，库是投影（单向，永不回写）。"),
    ("业务本体", 5, "库", "ontology_entity / concept / enum / relation",
     "三元本体之一。实体 - 概念 - 枚举 - 实体关系，取当前版本。"),
    ("规则本体", 6, "库+文件", "本体_v0.1.yaml#rule_schema + enum_value_current",
     "三元本体之一。规则的元模型（维度/风险等级/控制类型/规则类）+ 枚举当前取值。"),
    ("流程本体", 7, "库", "process_node / process_transition",
     "三元本体之一。节点 - 迁移 - 旁路，取当前版本。"),
    ("事实模型", 8, "文件", "事实模型_v0.1.json",
     "**派生**资产，由 规则资产/gen/gen_fact.py 从本体生成。派生物也冻："
     "消费方要的是这一版引擎吃的契约，不是让它自己重新生成一遍。"),
    ("规则绑定", 9, "文件", "规则绑定_v0.1.json",
     "**派生**资产，由 规则资产/gen/gen_bind.py 从本体 + 规则清单生成。"),
]
LAYER_ORD = {name: o for name, o, _k, _r, _n in LAYERS}
LAYER_META = {name: {"ord": o, "source_kind": k, "source_ref": r, "note": n}
              for name, o, k, r, n in LAYERS}
TRIPLE = ("业务本体", "规则本体", "流程本体")   # 用户说的「三元本体」


class PackError(ValueError):
    """发布流程被挡住了。消息直接给人看，所以必须写明挡在哪、怎么过。"""


# ─────────────────────────────────────────────────────────────────────────────
# 哈希
# ─────────────────────────────────────────────────────────────────────────────
def canon(obj):
    """规范化 JSON 文本。内容哈希与资产包里的字节都用它，两边必须是同一套。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def sha_obj(obj):
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def sha_file(path):
    """文件内容 sha256。取不到就回 None——**不许回空串**：
    空串会和「算出来是空文件」混在一起，而这两件事的处理完全不同。"""
    try:
        h = hashlib.sha256()
        with io.open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _onto_version():
    return db.scalar("SELECT version FROM ontology_version WHERE is_current")


def _yaml_section(key):
    """读 本体_v0.1.yaml 的一个顶层段。规则本体只在 YAML 里，库里没有投影。"""
    raw = yaml.safe_load(io.open(config.ONTOLOGY_YAML, encoding="utf-8").read())
    return raw.get(key)


# ─────────────────────────────────────────────────────────────────────────────
# 八层采集。每个 _snap_* 回 (item_count, payload)
# ─────────────────────────────────────────────────────────────────────────────
def _snap_files():
    rows = db.query("""SELECT rel_path, file_name, ext, file_size, parse_state, parse_reason,
                              text_chars, missing, doc_kind, doc_number, doc_title, issued_year,
                              abs_path, ocr_state, ocr_pages, ocr_table_pages
                         FROM doc_lib_file ORDER BY rel_path""")
    items = []
    unreadable = 0
    for r in rows:
        h = sha_file(r["abs_path"])
        if h is None:
            unreadable += 1
        items.append({
            "rel_path": r["rel_path"], "file_name": r["file_name"], "ext": r["ext"],
            "file_size": r["file_size"], "content_sha256": h,
            "parse_state": r["parse_state"], "parse_reason": r["parse_reason"],
            "text_chars": r["text_chars"], "missing": r["missing"],
            "doc_kind": r["doc_kind"], "doc_number": r["doc_number"],
            "doc_title": r["doc_title"], "issued_year": r["issued_year"],
            "ocr_state": r["ocr_state"], "ocr_pages": r["ocr_pages"],
            "ocr_table_pages": r["ocr_table_pages"],
        })
    return len(items), {"files": items, "unreadable": unreadable,
                        "note": "content_sha256 为 null = 发布时读不到这个文件"
                                "（已删/改名/权限），**不是空文件**"}


def _snap_rules():
    rows = db.query("SELECT * FROM rule ORDER BY rule_id, version")
    items = []
    for r in rows:
        d = dict(r)
        d.pop("created_at", None)        # 簿记字段，重装就变，进哈希会让「变没变」判不出来
        d["rule_name"] = d.pop("name", None)   # 不叫 name：叫 name 会被人名脱敏吃掉
        items.append(d)
    return len(items), {"rules": items}


def _snap_params():
    out = {}
    n = 0
    for key, path in (("标准表", config.STANDARDS_YAML),
                      ("必要附件决策表", config.ATTACHMENT_TABLE_YAML),
                      ("敏感词表", config.KEYWORDS_YAML),
                      ("内部单位名单", config.INTERNAL_UNITS_YAML)):
        try:
            text = io.open(path, encoding="utf-8").read()
        except OSError as e:
            raise PackError("规则参数表读不到：%s（%s）。"
                            "参数表缺一份，发布出去的口径就是不完整的，"
                            "所以这里直接挡住，不做「缺就跳过」。" % (os.path.basename(path), e))
        out[key] = {"file": os.path.basename(path), "sha256":
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "text": text, "parsed": yaml.safe_load(text)}
        n += 1
    return n, {"tables": out}


def _snap_biz():
    v = _onto_version()
    ents = db.query("""SELECT entity_id, name, source, note, alias FROM ontology_entity
                        WHERE version=%s ORDER BY entity_id""", (v,))
    cons = db.query("""SELECT concept_id, entity_id, name, data_type, enum_id, ref_entity,
                              source, availability, gap_id, sensitive, ocr_sourced, note, alias
                         FROM ontology_concept WHERE version=%s
                        ORDER BY concept_id""", (v,))
    enums = db.query("""SELECT enum_id, name, source, note FROM ontology_enum
                         WHERE version=%s ORDER BY enum_id""", (v,))
    vals = db.query("""SELECT enum_id, value, ord FROM ontology_enum_value
                        WHERE version=%s ORDER BY enum_id, ord""", (v,))
    rels = db.query("""SELECT from_entity, to_entity, rel_type, cardinality, source, note
                         FROM ontology_relation WHERE version=%s
                        ORDER BY from_entity, to_entity, rel_type""", (v,))
    payload = {
        "version": v,
        "entities": [{"entity_id": e["entity_id"], "entity_name": e["name"],
                      "source": e["source"], "note": e["note"], "alias": e["alias"]}
                     for e in ents],
        "concepts": [dict({k: c[k] for k in c if k != "name"},
                          concept_name=c["name"]) for c in cons],
        "enums": [{"enum_id": e["enum_id"], "enum_name": e["name"],
                   "source": e["source"], "note": e["note"]} for e in enums],
        "enum_values": [dict(x) for x in vals],
        "relations": [dict(x) for x in rels],
    }
    n = len(ents) + len(cons) + len(enums) + len(vals) + len(rels)
    return n, payload


def _snap_onto_source():
    """本体源文件原文。**冻文本，不冻解析结果**——注释只活在文本里。

    `loaded`（库里的投影是否同步）故意不冻：那是**当时的活状态**，
    不是内容。冻结时它必然是 True（gates 挡着），冻进去只会让人以为
    这一行记录的是「拿到包的时候同步不同步」。
    """
    si = onto_yaml.source_info()
    text = io.open(config.ONTOLOGY_YAML, encoding="utf-8").read()
    payload = {
        "file": si["path"], "lines": si["lines"], "bytes": si["bytes"],
        "sha256": si["sha256"], "version": si["version"], "maturity": si["maturity"],
        "date": si["date"], "counts": si["counts"], "sections": si["sections"],
        "written_by": si["written_by"], "written_how": si["written_how"],
        "derived": si["derived"], "text": text,
    }
    # 自检：抄下来的文本必须和 source_info 报的哈希一致。
    # 不一致说明读文件与算哈希之间文件被改了——此刻冻结就是冻了个半份。
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != si["sha256"]:
        raise PackError("本体源文件在冻结过程中被改动了（文本与哈希不一致）。"
                        "请重新冻结——半份快照比没有快照更糟。")
    return si["lines"], payload


def _snap_rule_schema():
    schema = _yaml_section("rule_schema")
    if not schema:
        raise PackError("本体_v0.1.yaml 里没有 rule_schema 段——规则本体这一层无从冻结。")
    cur = db.query("SELECT enum_id, value FROM enum_value_current ORDER BY enum_id, value")
    n = (len(schema) if isinstance(schema, dict) else 1) + len(cur)
    return n, {"rule_schema": schema,
               "enum_value_current": [dict(x) for x in cur],
               "note": "rule_schema 只在 YAML 里，库里没有投影；"
                       "enum_value_current 是库里的当前取值视图，规则的枚举列引用它"}


def _snap_process():
    v = _onto_version()
    nodes = db.query("""SELECT node_id, name, is_bypass, note, alias FROM process_node
                         WHERE version=%s ORDER BY node_id""", (v,))
    trans = db.query("""SELECT from_node, to_node, cond, blocking, note
                          FROM process_transition WHERE version=%s
                         ORDER BY from_node, to_node""", (v,))
    payload = {
        "version": v,
        "nodes": [{"node_id": n["node_id"], "node_name": n["name"],
                   "is_bypass": n["is_bypass"], "note": n["note"], "alias": n["alias"]}
                  for n in nodes],
        # occurrences **故意不冻**：YAML 里那组数来自「漏 24%」的旧解析器，
        # 载入一组已知是错的数比没有更糟。实测次数在流程本体那一栏看。
        "transitions": [dict(x) for x in trans],
    }
    return len(nodes) + len(trans), payload


def _snap_json_file(path, label):
    try:
        obj = json.load(io.open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PackError("%s 读不到或不是合法 JSON：%s（%s）"
                        % (label, os.path.basename(path), e))
    n = len(obj) if isinstance(obj, (list, dict)) else 1
    return n, {"file": os.path.basename(path), "content": obj}


def _snap_facts():
    return _snap_json_file(config.FACT_MODEL_JSON, "事实模型")


def _snap_bindings():
    return _snap_json_file(config.BINDING_JSON, "规则绑定")


COLLECTORS = {
    "文件": _snap_files, "规则": _snap_rules, "规则参数": _snap_params,
    "本体源文件": _snap_onto_source,
    "业务本体": _snap_biz, "规则本体": _snap_rule_schema, "流程本体": _snap_process,
    "事实模型": _snap_facts, "规则绑定": _snap_bindings,
}


def collect():
    """抄一遍八层。**整体成败**：缺一层就抛，不做部分冻结。"""
    out = {}
    for name, _o, kind, ref, note in LAYERS:
        n, payload = COLLECTORS[name]()
        out[name] = {"layer": name, "ord": LAYER_ORD[name], "source_kind": kind,
                     "source_ref": ref, "item_count": n, "payload": payload,
                     "content_sha256": sha_obj(payload), "note": note}
    return out


def bundle_sha(assets):
    """包级总校验：各层 content_sha256 按 layer 名排序后拼起来再算一次。

    **不是 zip 的哈希**：zip 里有时间戳，同样内容两次打包字节不同。
    """
    joined = "\n".join("%s:%s" % (k, assets[k]["content_sha256"]) for k in sorted(assets))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 工作区：现在能发什么、和当前生效版本差多少
# ─────────────────────────────────────────────────────────────────────────────
def current_release():
    """**只看指针**，没人点过「切为当前」就返回 None。

    和 `rules.engine.effective_release()`（带 fallback 阶梯）**故意不一样**，
    两个口径各有用处，不要顺手统一：
      · 对外消费面 `/api/pub/v1` 的 `current` 用这一个——外部系统拉到的
        版本必须是**有人明确指定**的，自动顺延等于让别人悄悄换了版本，
        而它们拉下去是要写进自己的审核链的。拉不到就报 404，并告诉
        对方去发布管理里指定一版。
      · 内部引擎与管理页用 effective_release()——本机演示不能因为
        少点一次按钮就整套不工作。
    """
    return db.query_one("""SELECT r.* FROM release_current c
                             JOIN release r ON r.release_id = c.release_id""")


def _rule_hash_map(rules):
    """rule_id → 内容哈希。比对两版规则用，createdat 这类簿记字段已经剔过了。"""
    return {r.get("rule_id"): sha_obj(r) for r in rules}


def diff_against(assets, base_release_id):
    """工作区快照 vs 某个已冻结版本。只做规则层逐条、其余层按哈希比。"""
    if not base_release_id:
        n = assets["规则"]["item_count"]
        return {"added": n, "modified": 0, "retired": 0, "unchanged": 0,
                "layers_changed": sorted(assets), "base": None,
                "note": "没有可比的已发布版本，整份都算新增"}, [
                    {"rule_id": r["rule_id"], "change": "added"}
                    for r in assets["规则"]["payload"]["rules"]]
    base = {r["layer"]: r for r in db.query(
        "SELECT layer, content_sha256, payload FROM release_asset WHERE release_id=%s",
        (base_release_id,))}
    now_rules = _rule_hash_map(assets["规则"]["payload"]["rules"])
    old_rules = _rule_hash_map((base.get("规则", {}).get("payload") or {}).get("rules") or [])
    items = []
    added = modified = retired = unchanged = 0
    for rid in sorted(set(now_rules) | set(old_rules)):
        if rid not in old_rules:
            items.append({"rule_id": rid, "change": "added"}); added += 1
        elif rid not in now_rules:
            items.append({"rule_id": rid, "change": "retired"}); retired += 1
        elif now_rules[rid] != old_rules[rid]:
            items.append({"rule_id": rid, "change": "modified"}); modified += 1
        else:
            items.append({"rule_id": rid, "change": "unchanged"}); unchanged += 1
    changed = [k for k in sorted(assets)
               if assets[k]["content_sha256"] != (base.get(k) or {}).get("content_sha256")]
    return {"added": added, "modified": modified, "retired": retired,
            "unchanged": unchanged, "layers_changed": changed,
            "base": base_release_id}, items


def gates(assets=None):
    """能不能冻结。**每条闸都给出「怎么过」**，不然页面上只有一个红叉。"""
    out = []
    si = onto_yaml.source_info()
    out.append({
        "gate": "本体源文件与库同步",
        "ok": bool(si["loaded"]["in_sync"]),
        "detail": "文件 sha %s… / 库 %s…" % (
            si["sha256"][:12], (si["loaded"]["source_hash"] or "—")[:12]),
        "how": "不同步就说不清冻的是哪一份：页面上改过 YAML 还没重装，"
               "库里是旧投影而文件是新的。去业务本体那一栏重装一次本体。",
    })
    n_rule = db.scalar("SELECT count(*) FROM rule WHERE version=1")
    out.append({"gate": "工作区有规则", "ok": n_rule > 0,
                "detail": "%d 条" % n_rule,
                "how": "先跑规则资产装载（python app/rules/loader.py）"})
    if assets is not None:
        out.append({"gate": "九层都抄全了", "ok": len(assets) == len(LAYERS),
                    "detail": "%d/%d 层" % (len(assets), len(LAYERS)),
                    "how": "缺层一律整体失败，不做部分冻结"})
    return out


def workspace():
    """「现在能发什么」。页面第一段就是它。"""
    assets = collect()
    # **比对基准和页面上的「当前生效」都取 effective_release()**，不取
    # current_release()（只看指针）。没设指针时后者返回 None，于是
    # 页头显示「现在没有生效版本」、工作区 diff 说「没有可比的已发布版本，
    # 整份都算新增」——而引擎其实正按最新已发布版在判。两句话都是错的。
    eff, eff_why = rule_engine.effective_release()
    cur = db.query_one("SELECT * FROM release WHERE release_id=%s",
                       (eff["release_id"],)) if eff else None
    is_pointer = bool(db.query_one("SELECT 1 FROM release_current"))
    summary, _items = diff_against(assets, cur["release_id"] if cur else None)
    return {
        "layers": [{"layer": k, "ord": assets[k]["ord"],
                    "source_kind": assets[k]["source_kind"],
                    "source_ref": assets[k]["source_ref"],
                    "item_count": assets[k]["item_count"],
                    "content_sha256": assets[k]["content_sha256"],
                    "is_triple": k in TRIPLE,
                    "changed": k in summary["layers_changed"],
                    "note": assets[k]["note"]}
                   for k in sorted(assets, key=lambda x: assets[x]["ord"])],
        "triple": list(TRIPLE),
        "bundle_sha256": bundle_sha(assets),
        "diff": summary,
        "gates": gates(assets),
        "current": {"release_id": cur["release_id"], "rule_set_version": cur["rule_set_version"],
                    "published_at": cur["published_at"],
                    "is_pointer": is_pointer, "why": eff_why} if cur else None,
        "suggest_version": _suggest_version(),
        "note": "冻结会把这九层**原样抄一份**存进库，之后改 YAML、重装规则、"
                "文件库重扫都动不了它。三元本体 = 业务 / 规则 / 流程，"
                "外加一层带注释的本体源文件原文。",
    }


def _suggest_version():
    d = datetime.date.today().strftime("%Y%m%d")
    n = db.scalar("SELECT count(*) FROM release WHERE rule_set_version LIKE %s",
                  ("rs-" + d + "-%",)) or 0
    return "rs-%s-%02d" % (d, n + 1)


def _next_release_id():
    rows = db.query("SELECT release_id FROM release WHERE release_id ~ '^rel-[0-9]+$'")
    nos = [int(r["release_id"].split("-")[1]) for r in rows]
    return "rel-%04d" % ((max(nos) if nos else 0) + 1)


# ─────────────────────────────────────────────────────────────────────────────
# 冻结 → 送审 → 签字发布 → 切当前 / 回滚
# ─────────────────────────────────────────────────────────────────────────────
def freeze(version, by, note=None):
    version = (version or "").strip()
    by = (by or "").strip()
    if not version:
        raise PackError("版本号必填。它是消费方引用这一版的唯一凭据。")
    if not by:
        raise PackError("冻结人必填。发布链上每一步都要有名字——出事要找得到人。")
    if db.query_one("SELECT 1 FROM release WHERE rule_set_version=%s", (version,)):
        raise PackError("版本号 %s 已经有了。版本号不许复用：复用等于"
                        "「同一个号两次拉到不同内容」，那是对外消费里最难查的事故。"
                        % version)
    assets = collect()
    bad = [g for g in gates(assets) if not g["ok"]]
    if bad:
        raise PackError("；".join("%s（%s）→ %s" % (g["gate"], g["detail"], g["how"])
                                 for g in bad))
    cur = current_release()
    summary, items = diff_against(assets, cur["release_id"] if cur else None)
    rid = _next_release_id()
    onto_v = _onto_version()
    n_rule = assets["规则"]["item_count"]
    compiled = db.scalar("SELECT count(*) FROM rule WHERE version=1 AND compiled")
    executable = db.scalar("SELECT count(*) FROM rule WHERE version=1 AND demo_executable")
    bsha = bundle_sha(assets)
    with db.tx() as conn:
        cur_ = conn.cursor()
        cur_.execute("""INSERT INTO release (release_id, rule_set_version, ontology_version,
                            status, rule_count, compiled_count, executable_count,
                            diff_summary, created_by, note, frozen_at, bundle_sha256)
                        VALUES (%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,now(),%s)""",
                     (rid, version, onto_v, n_rule, compiled, executable,
                      json.dumps(summary, ensure_ascii=False), by, note, bsha))
        for k in sorted(assets, key=lambda x: assets[x]["ord"]):
            a = assets[k]
            cur_.execute("""INSERT INTO release_asset (release_id, layer, ord, source_kind,
                                source_ref, item_count, content_sha256, payload, note)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                         (rid, k, a["ord"], a["source_kind"], a["source_ref"],
                          a["item_count"], a["content_sha256"],
                          json.dumps(a["payload"], ensure_ascii=False, default=str),
                          a["note"]))
        for it in items:
            cur_.execute("""INSERT INTO release_item (release_id, rule_id, version, change)
                            VALUES (%s,%s,1,%s)""", (rid, it["rule_id"], it["change"]))
    return {"release_id": rid, "rule_set_version": version, "bundle_sha256": bsha,
            "layers": len(assets), "diff": summary,
            "note": "已冻结为草稿。**草稿不能被消费**——/api/pub 只给已发布的版本。"
                    "下一步：送审 → 签字发布 → 切为当前。"}


def _must(release_id):
    r = db.query_one("SELECT * FROM release WHERE release_id=%s", (release_id,))
    if not r:
        raise PackError("没有这个发布：%s" % release_id)
    return r


def review(release_id, by):
    r = _must(release_id)
    if r["status"] != "draft":
        raise PackError("只有草稿能送审，当前是「%s」。" % r["status"])
    if not r["frozen_at"]:
        raise PackError("这一版还没冻结过内容（frozen_at 为空），送审没有意义。")
    if not (by or "").strip():
        raise PackError("送审人必填。")
    db.execute("UPDATE release SET status='reviewing' WHERE release_id=%s", (release_id,))
    return {"release_id": release_id, "status": "reviewing"}


def publish(release_id, by):
    """签字发布。**不自动切为当前**——发布和生效是两件事。"""
    r = _must(release_id)
    if r["status"] not in ("draft", "reviewing"):
        raise PackError("当前状态「%s」不能发布。" % r["status"])
    if not (by or "").strip():
        raise PackError("签字人必填。库里有 ck_release_signed 钉着："
                        "reviewed_by 为空的 release 不允许进 published。"
                        "规则集一变，所有在跑的单据的判定口径就跟着变——不许自动发布。")
    db.execute("""UPDATE release SET status='published', reviewed_by=%s, reviewed_at=now(),
                         published_at=now() WHERE release_id=%s""", (by.strip(), release_id))
    same = (r["created_by"] or "").strip() == by.strip()
    return {"release_id": release_id, "status": "published", "reviewed_by": by.strip(),
            "same_person": same,
            "note": ("**冻结人与签字人是同一个人**（%s）。POC 里不挡，"
                     "但真实上线前这应该是两个人。" % by.strip()) if same
                    else "已发布。还要再点一次「切为当前」才会生效。"}


def switch_current(release_id, by):
    r = _must(release_id)
    if r["status"] != "published":
        raise PackError("只有已发布的版本能切为当前，当前是「%s」。" % r["status"])
    with db.tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM release_current")
        c.execute("""INSERT INTO release_current (singleton, release_id, switched_by)
                     VALUES (TRUE,%s,%s)""", (release_id, (by or "").strip() or None))
    return {"release_id": release_id, "current": True}


def rollback(release_id, to_release_id, by, reason):
    """把某一版标回滚，并把当前指针指回另一个已发布版本。"""
    r = _must(release_id)
    if r["status"] != "published":
        raise PackError("只有已发布的版本需要回滚，当前是「%s」。" % r["status"])
    if not (reason or "").strip():
        raise PackError("回滚原因必填。没有原因的回滚，三个月后没人说得清为什么。")
    if not (by or "").strip():
        raise PackError("操作人必填。")
    to = _must(to_release_id) if to_release_id else None
    if to and to["status"] != "published":
        raise PackError("要指回的版本 %s 不是已发布状态。" % to_release_id)
    if to and to["release_id"] == release_id:
        raise PackError("不能指回自己。")
    with db.tx() as conn:
        c = conn.cursor()
        c.execute("""UPDATE release SET status='rolled_back', rolled_back_at=now(),
                            rollback_reason=%s WHERE release_id=%s""",
                  (reason.strip(), release_id))
        c.execute("DELETE FROM release_current")
        if to:
            c.execute("""INSERT INTO release_current (singleton, release_id, switched_by)
                         VALUES (TRUE,%s,%s)""", (to_release_id, by.strip()))
    return {"release_id": release_id, "status": "rolled_back",
            "current": to_release_id,
            "note": "当前指针%s。回滚不是「取 published 里最新的一条」能表达的——"
                    "旧版的 published_at 更早，所以当前版本单独用单行表记。"
                    % ("已指回 " + to_release_id if to else "已清空（现在没有生效版本）")}


def discard(release_id, by):
    """删掉一个草稿。**只能删草稿**：已发布的版本删不掉，那是资产。"""
    r = _must(release_id)
    if r["status"] != "draft":
        raise PackError("只有草稿能删，当前是「%s」。已发布的版本是资产，不许删。"
                        % r["status"])
    if not (by or "").strip():
        raise PackError("操作人必填。")
    db.execute("DELETE FROM release WHERE release_id=%s", (release_id,))   # 快照级联删
    return {"release_id": release_id, "deleted": True}


# ─────────────────────────────────────────────────────────────────────────────
# 查询
# ─────────────────────────────────────────────────────────────────────────────
def list_releases():
    rows = db.query("""SELECT r.release_id, r.rule_set_version, r.ontology_version, r.status,
                              r.rule_count, r.compiled_count, r.executable_count,
                              r.diff_summary, r.created_by, r.created_at, r.frozen_at,
                              r.reviewed_by, r.reviewed_at, r.published_at,
                              r.rolled_back_at, r.rollback_reason, r.note, r.bundle_sha256,
                              (c.release_id IS NOT NULL) AS is_current,
                              (SELECT count(*) FROM release_asset a
                                WHERE a.release_id = r.release_id) AS layers,
                              (SELECT count(*) FROM release_access_log l
                                WHERE l.release_id = r.release_id) AS accesses
                         FROM release r
                         LEFT JOIN release_current c ON c.release_id = r.release_id
                        ORDER BY r.created_at DESC""")
    # is_effective = 规则引擎实际在用的那一版。**直接问引擎，不在这里另算一遍。**
    # 原先这里自己写了一套 fallback（最新的 published），而引擎取的是
    # 「最新的任意版本」，两套在实测中给出了不同答案：引擎按草稿 rs-0.1 判，
    # 页面却把 rs-20261021-01 标成「生效中（自动）」。页面标错生效版，
    # 等于让人照着错的版本号去复现结论。
    eff, eff_why = rule_engine.effective_release()
    eff_id = eff["release_id"] if eff else None
    for r in rows:
        r["is_effective"] = (r["release_id"] == eff_id)
    return {"items": rows,
            "effective": {"release_id": eff_id, "why": eff_why},
            "policy": {"signed": "发布必须有人签字（库里 ck_release_signed 钉着）",
                       "frozen": "已发布的版本是快照，装载与改 YAML 都动不了它",
                       "consume": "只有 published 的版本能被 /api/pub/v1 消费",
                       "effective": eff_why}}


def get_release(release_id):
    r = _must(release_id)
    r["is_current"] = bool(db.query_one(
        "SELECT 1 FROM release_current WHERE release_id=%s", (release_id,)))
    r["assets"] = db.query("""SELECT layer, ord, source_kind, source_ref, item_count,
                                     content_sha256, note
                                FROM release_asset WHERE release_id=%s ORDER BY ord""",
                           (release_id,))
    for a in r["assets"]:
        a["is_triple"] = a["layer"] in TRIPLE
    r["items"] = db.query("""SELECT rule_id, change FROM release_item
                              WHERE release_id=%s ORDER BY change, rule_id""", (release_id,))
    return r


def asset_payload(release_id, layer):
    row = db.query_one("""SELECT payload, content_sha256, item_count, source_ref
                            FROM release_asset WHERE release_id=%s AND layer=%s""",
                       (release_id, layer))
    if not row:
        raise PackError("发布 %s 里没有「%s」这一层。" % (release_id, layer))
    return row


def access_log(limit=50):
    return db.query("""SELECT l.id, l.release_id, l.resource, l.at, l.client, l.bytes,
                              l.http_status, r.rule_set_version
                         FROM release_access_log l
                         LEFT JOIN release r ON r.release_id = l.release_id
                        ORDER BY l.at DESC LIMIT %s""", (limit,))


def log_access(release_id, resource, client, nbytes, status=200):
    db.execute("""INSERT INTO release_access_log (release_id, resource, client, bytes,
                        http_status) VALUES (%s,%s,%s,%s,%s)""",
               (release_id, resource, (client or "")[:200] or None, nbytes, status))


STORAGE = [
    ("release", "版本头：状态、签字人、计数、包级校验、与上一版的差异摘要",
     "冻结（页面）/ 装载脚本（只建自己那份草稿）", "**永不清**",
     "已发布的版本是资产，删不掉；只有草稿能删"),
    ("release_asset", "**九层快照本体**。对外消费的唯一数据源", "冻结时九行一起写",
     "**永不清**",
     "/api/pub 只读它，不读活表——读活表等于版本号是假的，"
     "因为活表随时在变，同一个号两次会拉到不同内容"),
    ("release_item", "逐条规则的 added/modified/retired/unchanged，给人看差异",
     "冻结时按差异写", "只清草稿的",
     "**它不是版本的内容**（内容在 release_asset）。0025 拆掉了它到 rule 的外键："
     "留着的话，装载时 DELETE FROM rule 会被已发布版本顶回来"),
    ("release_current", "单行表：当前生效的是哪一版", "切为当前 / 回滚", "**装载不碰**",
     "CHECK(singleton) 把主键钉死成 TRUE。用单行表而不是「取最新的 published」，"
     "因为回滚是指回旧版，而旧版的 published_at 更早"),
    ("release_access_log", "消费台账：谁在什么时候拉了哪一版的哪一块",
     "每次 /api/pub 请求追加", "不清",
     "只记元数据：版本号、资源名、调用方自报的名字、字节数；"
     "不记请求参数、不记响应内容、不记 IP"),
    ("rule", "**工作区**的规则，不是发布内容", "装载脚本 / 本体采纳",
     "装载时整批重建（只删 created_by='asset_loader' 的）",
     "采纳入本体写进来的规则（created_by=onto_adopt:谁）会保留——"
     "原来是无条件 DELETE，下次装载会把人采纳的规则静默删掉"),
    ("ontology_entity / concept / enum / relation", "**工作区**的业务本体投影",
     "本体装载脚本", "重装本体时整版重建", "YAML 是源、库是投影、单向永不回写"),
    ("process_node / process_transition", "**工作区**的流程本体投影",
     "本体装载脚本", "重装本体时整版重建", "冻结时按当前版本抄进快照"),
    ("doc_lib_file", "**工作区**的文件清单", "文件库扫描",
     "重扫时按指纹更新", "冻结时只抄清单与**内容 sha256**，不抄正文、不抄绝对路径"),
]


def storage():
    rows = []
    for name, what, who, on_recalc, why in STORAGE:
        first = name.split(" / ")[0].strip()
        try:
            n = db.scalar("SELECT count(*) FROM " + first)
        except Exception:                      # noqa: BLE001 表不存在要显式写出来，不静默
            n = None
        rows.append({"table_name": name, "what": what, "written_by": who,
                     "on_recalc": on_recalc, "why": why, "rows": n})
    return {"items": rows,
            "note": "上面五张是**发布资产**，下面四张是**工作区**。"
                    "「重算时」那一列回答的就是用户 2026-10-19 定的那条口径："
                    "装载只动工作区，已发布的快照与当前指针不受影响。"}
