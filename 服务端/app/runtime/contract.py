# -*- coding: utf-8 -*-
"""启动时契约校验。**Fail Fast：不过就不启动，不做部分启动。**

校验的 8 项来自 `事实模型_v0.1.json` 的 engine_contract.startup_checks。
它们不是「代码健康检查」，每一项都对应一种会让审核静默失效的具体失败：

  1 概念不存在        → 规则运行期取到 None，判出「字段为空所以合规」
  2 字段取不到        → 让 AI 声称复核了它读不到的数据，比少复核更危险
  3 外部表缺失        → 决策表加载不到，附件完整性这一类直接哑掉
  4 决策表对账不上     → 少几行就是几类场景永久无规则可判
  5 枚举多一个取值     → 规则去判一个永远为假的条件，等于悄悄失效
  6 敏感字段没覆盖脱敏  → 姓名账号原样流进审计接口、导出文件、模型请求体
  7 OCR 数字当阈值     → 阈值错一位，硬规则从保护变成伤害
  8 自由文本没过 M06   → 摘要里 35% 含真实姓名，而它不带 sensitive 标记

**被剔除的规则必须逐条记下来并返回给调用方**，由运行时写进 cycle_log.rule_excluded，
并在审核报告上单列一节。静默剔除会让一条本该拦截的闸悄悄消失，而且没人知道——
确定性的闸失效而没人发现，比没有这道闸更危险。
"""
import io
import json

from .. import config, db, mask
from ..rules import engine as rule_engine
from ..rules import impl as rule_impl  # noqa: F401  -- 触发 @rule 装饰器注册 _REGISTRY

# 可用的可得性档位。与 规则绑定_v0.1.json 的 availability_classes 一致。
# 「仅单人可得」「仅多人可得」也算可用，但要声明范围。
AVAIL_OK = {"样本可得", "样本可得(审批意见)", "已录入资产", "可推导"}
AVAIL_SCOPED = {"仅单人可得", "仅多人可得"}

EXTERNAL_TABLES = {
    "必要附件决策表": config.ATTACHMENT_TABLE_YAML,
    "敏感开票单位名单": config.KEYWORDS_YAML,
    "敏感消费项名单": config.KEYWORDS_YAML,
    "内部单位名单": config.INTERNAL_UNITS_YAML,
    "交通工具等级标准表": None,        # 已知无法录入（G-03），对应规则不编译
    "泉州港务补助标准": config.STANDARDS_YAML,
    "疗休养": None,                   # 行内列表，不是外部文件
}


class ContractResult:
    def __init__(self):
        self.errors = []        # 阻塞项
        self.warnings = []      # 非阻塞但要打日志
        self.excluded = []      # 被剔出执行集的规则：[(rule_id, reason)]
        self.checks = []        # [(名称, ok, 说明)]

    def check(self, name, ok, detail="", blocking=True):
        self.checks.append((name, bool(ok), detail))
        if not ok:
            (self.errors if blocking else self.warnings).append("%s：%s" % (name, detail))
        return ok

    @property
    def ok(self):
        return not self.errors

    def report(self):
        lines = []
        for name, ok, detail in self.checks:
            lines.append("  [%s] %s%s" % ("OK" if ok else "!!", name,
                                          ("  " + detail) if detail and not ok else ""))
        if self.excluded:
            lines.append("  被剔出执行集的规则 %d 条（**不是静默跳过，逐条在此**）：" % len(self.excluded))
            for rid, why in self.excluded[:200]:
                lines.append("     %-11s %s" % (rid, why))
        for w in self.warnings:
            lines.append("  警告 %s" % w)
        for e in self.errors:
            lines.append("  失败 %s" % e)
        return "\n".join(lines)


# 业务表里「枚举列」的列名清单。**只许有一份**：
# 本体构建那条链要拿同一份清单去比对「枚举取值 vs 真实数据里的取值」，
# 各留一份拷贝的下场是——这边加了列、那边没加，于是一个新枚举列既没有外键
# 护着、也不在候选比对范围内，两道闸同时漏掉它。
ENUM_COLUMNS = (
    'biz_type', 'doc_type', 'expense_item', 'attach_type', 'node',
    'inv_type', 'vehicle', 'matter_type', 'meal_provided',
    'meal_situation', 'car_situation', 'approve_form', 'meal_slot',
    'std_type', 'rank_level', 'tier', 'direct_managed', 'industry',
    'rule_category', 'void_status', 'bill_status',
)


def _fact():
    return json.load(io.open(config.FACT_MODEL_JSON, encoding="utf-8"))



def _delegate_live(rid, rules, rc_map, concepts):
    """delegate 在当前数据条件下能不能给出结论。只看一层，不递归。"""
    rule = next((x for x in rules if x["rule_id"] == rid), None)
    if rule is None or not rule.get("compiled") or rule.get("binding_node") == "OUT":
        return False
    cids = [c["concept_id"] for c in (rc_map.get(rid) or [])]
    if not cids:
        return False
    deg_missing = set((((rule["logic"] or {}).get("degraded") or {}).get("missing")) or [])
    for c in cids:
        info = concepts.get(c)
        if info is None:
            return False
        if info["availability"] not in AVAIL_OK | AVAIL_SCOPED and c not in deg_missing:
            return False
    return True

def verify(verbose=True):
    r = ContractResult()
    fact = _fact()

    # ── 0 本体是否已装载且未漂移 ─────────────────────────────────────────────
    onto = db.query_one("SELECT version, source_hash, stats FROM ontology_version WHERE is_current")
    if not r.check("本体已装载", onto is not None, "库里没有当前本体版本，先跑 app/ontology/loader.py"):
        return r
    import hashlib
    h = hashlib.sha256(io.open(config.ONTOLOGY_YAML, "rb").read()).hexdigest()
    r.check("本体未漂移", onto["source_hash"] == h,
            "库里的本体与 %s 不一致，重新装载" % config.ONTOLOGY_YAML)

    concepts = {c["concept_id"]: c for c in db.query(
        "SELECT concept_id, name, availability, gap_id, sensitive, ocr_sourced, enum_id "
        "FROM ontology_concept WHERE version = %s", (onto["version"],))}
    entities = {e["entity_id"] for e in db.query(
        "SELECT entity_id FROM ontology_entity WHERE version = %s", (onto["version"],))}

    # ── 1 规则引用的概念都存在 ──────────────────────────────────────────────
    bad_concept = db.query("""
        SELECT rc.rule_id, rc.concept_id FROM rule_concept rc
        LEFT JOIN ontology_concept oc
          ON oc.concept_id = rc.concept_id AND oc.version = %s
        WHERE oc.concept_id IS NULL
    """, (onto["version"],))
    r.check("规则引用的概念都存在于本体", not bad_concept,
            "；".join("%s→%s" % (b["rule_id"], b["concept_id"]) for b in bad_concept[:10]))

    bad_entity = [b for b in db.query("SELECT rule_id, entity_id FROM rule_entity")
                  if b["entity_id"] not in entities]
    r.check("规则引用的实体都存在于本体", not bad_entity,
            "；".join("%s→%s" % (b["rule_id"], b["entity_id"]) for b in bad_entity[:10]))

    # ── 2 执行集里每条规则的字段都取得到 ────────────────────────────────────
    rules = db.query("""
        SELECT r.rule_id, r.rule_class, r.control_type, r.compiled, r.demo_executable,
               r.demo_blocker, r.demo_scope, r.binding_node, r.external_table,
               r.logic, r.compiled_reason
        FROM rule r WHERE r.version = 1 ORDER BY r.rule_id
    """)
    rc_map = {}
    for row in db.query("SELECT rule_id, concept_id, role FROM rule_concept"):
        rc_map.setdefault(row["rule_id"], []).append(row)

    executable = []
    for rule in rules:
        rid = rule["rule_id"]
        if not rule["compiled"]:
            r.excluded.append((rid, "未编译：" + (rule["compiled_reason"] or "")[:70]))
            continue
        if rule["binding_node"] == "OUT":
            r.excluded.append((rid, "执行点在 OA，不进本系统执行集"))
            continue
        # **数据齐全不等于能跑。** 概念可得性只管字段有没有，不管有没有人写
        # 求值代码——2026-10-22 实测过一次：TR-D2-01/TR-D6-01/TR-D6-02 的概念
        # 都已可得，但 app/rules/impl.py 里压根没有对应的 @rule(...) 函数，
        # 跑起来会一直走 engine.py 的「没有实现」兜底，报告上却显示在工作。
        # 这类代码缺口只靠概念可得性查不出来，必须在这里显式核对实现是否存在。
        if rule_engine.registered().get(rid) is None:
            r.excluded.append((rid, "没有实现函数（代码缺口，概念可得性不能替代这一项核对）"))
            continue
        cids = rc_map.get(rid) or []
        if not cids:
            r.excluded.append((rid, "logic 未声明 concepts（软规则/提示词型），交 N04 AI 审核"))
            continue
        # 显式声明的降级：规则自己写明「缺这几个字段时按区间判定，不确定段转人工」。
        # 只有被 degraded.missing 完整覆盖、且不确定段确实转人工的，才允许留在执行集。
        deg = (rule["logic"] or {}).get("degraded") or {}
        deg_missing = set(deg.get("missing") or [])
        if deg and deg.get("on_indeterminate") != "待人工":
            r.check("声明 degraded 的规则必须把不确定区间转人工", False,
                    "%s 的 on_indeterminate=%s" % (rid, deg.get("on_indeterminate")))
            continue
        unavailable = [(c["concept_id"], concepts[c["concept_id"]]["availability"])
                       for c in cids
                       if concepts.get(c["concept_id"])
                       and concepts[c["concept_id"]]["availability"] not in AVAIL_OK | AVAIL_SCOPED
                       and c["concept_id"] not in deg_missing]
        if unavailable:
            r.excluded.append((rid, "依赖不可得字段 " + "、".join(
                "%s(%s)" % (a, b) for a, b in unavailable[:4])))
            continue
        # composite 规则：delegate 若本身不在执行集里，这条规则**永远**给不出结论，
        # 只会每张单产生一条 pending。那不是「判不了这一张」，而是
        # 「在当前数据条件下根本不可能判」——属于契约层事实。
        # 实测：TR-D4-03 与 TR-D8-05 合计贡献了 98 条待人工里的 78 条，
        # 把真正需要人看的 20 条淹掉了。
        delegates = (rule["logic"] or {}).get("delegates") or []
        dead = [d for d in delegates
                if not _delegate_live(d, rules, rc_map, concepts)]
        if dead:
            r.excluded.append((rid, "composite 的 delegate 不在执行集里：" + "、".join(dead)))
            continue

        scoped = [c["concept_id"] for c in cids
                  if concepts[c["concept_id"]]["availability"] in AVAIL_SCOPED]
        if scoped and not rule["demo_scope"]:
            r.check("范围受限的规则必须声明 demo_scope", False,
                    "%s 依赖 %s，但没写 demo_scope" % (rid, scoped))
            continue
        executable.append(rid)

    # 库里的 demo_executable 标记必须与这里算出来的执行集一致，否则两个口径会打架
    marked = {x["rule_id"] for x in db.query(
        "SELECT rule_id FROM rule WHERE demo_executable AND version = 1")}
    r.check("执行集与库里的 demo_executable 标记一致",
            set(executable) == marked,
            "算出来 %d 条、库里标了 %d 条，差集 %s" % (
                len(executable), len(marked),
                sorted(set(executable) ^ marked)[:8]))

    # ── 3 规则引用的外部表都在 ──────────────────────────────────────────────
    import os
    missing_tables = []
    for rule in rules:
        if not rule["external_table"] or not rule["compiled"]:
            continue
        for t in str(rule["external_table"]).split(","):
            t = t.strip()
            if t in ("", "疏休养"):
                continue
            if t not in EXTERNAL_TABLES:
                missing_tables.append((rule["rule_id"], t, "未登记"))
                continue
            path = EXTERNAL_TABLES[t]
            if path and not os.path.exists(path):
                missing_tables.append((rule["rule_id"], t, "文件不存在"))
    r.check("规则引用的外部表都在", not missing_tables, str(missing_tables[:6]))

    # 查找表都已装进库
    for tid in ("必要附件决策表", "敏感词表", "内部单位名单"):
        n = db.scalar("SELECT row_count FROM lookup_table WHERE table_id = %s", (tid,))
        r.check("查找表已装载：%s" % tid, bool(n), "库里没有这张表，先跑 app/rules/loader.py")

    # ── 4 决策表对账 ────────────────────────────────────────────────────────
    lt = db.query_one("SELECT row_count, source_rows FROM lookup_table "
                      "WHERE table_id = '必要附件决策表'")
    if lt:
        total = db.scalar("SELECT coalesce(sum(source_rows),0) FROM lookup_row "
                          "WHERE table_id = '必要附件决策表'")
        r.check("决策表 row_count 合计 == 源表行数",
                total == lt["source_rows"],
                "合计 %s ≠ 源表 %s" % (total, lt["source_rows"]))
        dup = db.query("""SELECT row_key, count(*) n FROM lookup_row
                          WHERE table_id='必要附件决策表' GROUP BY row_key HAVING count(*)>1""")
        r.check("决策表键唯一", not dup, "重复键 %d 个" % len(dup))
        r.check("决策表未命中行为是待人工",
                db.scalar("SELECT on_miss FROM lookup_table WHERE table_id='必要附件决策表'")
                == "待人工",
                "不得按最严或最宽兜底")

    # ── 5 枚举取值与事实模型一致 ────────────────────────────────────────────
    db_enums = {}
    for row in db.query("SELECT enum_id, value FROM enum_value_current ORDER BY enum_id, value"):
        db_enums.setdefault(row["enum_id"], set()).add(row["value"])
    diffs = []
    for eid, meta in fact["enums"].items():
        want = set(str(v) for v in meta["values"])
        got = db_enums.get(eid, set())
        if want != got:
            diffs.append((eid, sorted(want - got)[:3], sorted(got - want)[:3]))
    r.check("枚举取值与事实模型一致", not diffs,
            "；".join("%s 缺%s 多%s" % d for d in diffs[:5]))

    # 业务表的枚举列是否都有外键护着（防止新增列时忘记加约束）
    unguarded = db.query("""
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        WHERE c.table_schema='public'
          -- 只看基表。视图的列当然没有外键，把视图算进来会产生永远消不掉的告警，
          -- 而永远存在的告警等于没有告警——下次真有问题也不会有人看。
          AND c.table_name IN (SELECT table_name FROM information_schema.tables
                               WHERE table_schema='public' AND table_type='BASE TABLE')
          AND c.column_name = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM pg_constraint k
              JOIN pg_class t ON t.oid = k.conrelid
              JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(k.conkey)
              WHERE t.relname = c.table_name AND a.attname = c.column_name AND k.contype='f')
    """, (list(ENUM_COLUMNS),))
    r.check("业务表的枚举列都有外键约束", not unguarded,
            str([(u["table_name"], u["column_name"]) for u in unguarded[:8]]), blocking=False)

    # ── 6 敏感字段都被脱敏策略覆盖 ──────────────────────────────────────────
    covered = set()
    for p in fact["masking"]["policies"]:
        covered.update(p["applies_to"])
    sens_db = {c for c, v in concepts.items() if v["sensitive"]}
    r.check("敏感字段都被脱敏策略覆盖", not (sens_db - covered),
            "未覆盖 %s" % sorted(sens_db - covered))
    r.check("脱敏开关是开着的", config.MASK_MODE == "on",
            "FJHY_MASK_MODE=%s，生产不许关" % config.MASK_MODE,
            blocking=(config.CONTRACT_MODE == "fail_fast"))

    # ── 7 OCR 来源字段不得当硬阈值 ──────────────────────────────────────────
    ocr_concepts = {c for c, v in concepts.items() if v["ocr_sourced"]}
    asset_rules = json.load(io.open(config.RULES_JSON, encoding="utf-8"))["rules"]
    ts_map = {x["rule_id"]: x.get("threshold_source") for x in asset_rules}
    bad_threshold = []
    for rule in rules:
        if not rule["compiled"] or rule["rule_class"] not in ("hard", "mixed"):
            continue
        used = {c["concept_id"] for c in rc_map.get(rule["rule_id"]) or []}
        if not (used & ocr_concepts):
            continue
        ts = ts_map.get(rule["rule_id"])
        if not ts:
            bad_threshold.append((rule["rule_id"], "没声明 threshold_source"))
        elif ts["confidence"] not in ("人眼读图", "原生文本") and not ts.get("cross_checked"):
            bad_threshold.append((rule["rule_id"], "阈值可信度 %s 且未交叉印证" % ts["confidence"]))
    r.check("OCR 来源的数值没有被当成硬阈值", not bad_threshold, str(bad_threshold[:5]))

    # ── 8 自由文本泄露路径都被 M06 覆盖 ─────────────────────────────────────
    m06 = [p for p in fact["masking"]["policies"] if p["id"] == "M06"][0]
    leak_missing = [f["concept_id"] for f in fact["free_text_leak_paths"]["fields"]
                    if f["concept_id"] not in m06["applies_to"]]
    r.check("自由文本泄露路径都被 M06 覆盖", not leak_missing, str(leak_missing))

    # ── 额外：软规则不得硬拦 / 发布态必须签字 ───────────────────────────────
    soft_block = db.query("SELECT rule_id FROM rule WHERE rule_class='soft' AND control_type='禁止'")
    r.check("软规则没有标禁止", not soft_block, str([x["rule_id"] for x in soft_block]))
    unsigned = db.query("SELECT release_id FROM release WHERE status='published' "
                        "AND reviewed_by IS NULL")
    r.check("已发布的 release 都有签字", not unsigned, str([x["release_id"] for x in unsigned]))

    if verbose:
        print("启动时契约校验（%s）" % config.CONTRACT_MODE)
        print(r.report())
        print("  执行集 %d 条 / 候选 %d 条；剔除 %d 条"
              % (len(executable), len(rules), len(r.excluded)))
    r.executable = executable
    return r


def verify_or_die(verbose=True):
    """Fail Fast 入口。CONTRACT_MODE=warn 时只告警不退出，仅供排障。"""
    r = verify(verbose=verbose)
    if r.ok:
        return r
    if config.CONTRACT_MODE == "warn":
        print("** 契约校验有 %d 项失败，但 FJHY_CONTRACT_MODE=warn，继续启动。"
              "生产环境不许这么跑。" % len(r.errors))
        return r
    raise SystemExit("** 契约校验失败 %d 项，拒绝启动（Fail Fast）：\n%s"
                     % (len(r.errors), "\n".join("   - " + e for e in r.errors)))


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    res = verify()
    sys.exit(0 if res.ok else 1)
