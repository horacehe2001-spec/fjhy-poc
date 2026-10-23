# -*- coding: utf-8 -*-
"""规则引擎。硬规则 / 混合规则 / 软规则三分类，版本化加载。

三分类的边界是**谁下结论**，不是难易：
  hard   引擎直接判，确定性，可拦（禁止级）
  mixed  硬条件先过滤，再交模型判那一部分；模型只对「说明是否成立」这类
         语义问题表态，阈值比较仍由引擎做
  soft   模型辅助，**必须人工复核**，永远不拦

四态结论，None 一路传到底：
  hit      命中（不符合）
  pass     通过
  pending  判不了 → 待人工。**不是「通过」**
  excluded 契约校验把它剔出了执行集，根本没跑

【为什么 pending 必须和 pass 分开】
  把 pending 当 pass，报告上就会出现「本单 82 条规则全部通过」，
  而实际上 66 条根本没跑。审核岗据此放行，等于 AI 声称复核了它读不到的数据。
  报告里必须三个数并列：执行多少、命中多少、判不了多少。

【版本化】
  RuleSet 载入时钉住 rule_set_version 与 ontology_version，
  写进 cycle_log。没有这两个版本号，一条历史结论就无法复现——
  规则改过、本体改过，同样的输入会得到不同的输出。
"""
import json
import time

from .. import config, db
from . import predicates as P

# 规则实现注册表：rule_id → 函数
_REGISTRY = {}


def rule(rule_id):
    """注册一条规则的实现。"""
    def deco(fn):
        if rule_id in _REGISTRY:
            raise RuntimeError("规则 %s 注册了两次" % rule_id)
        _REGISTRY[rule_id] = fn
        return fn
    return deco


def registered():
    return dict(_REGISTRY)


# ─────────────────────────────────────────────────────────────────────────────
# 判定结果
# ─────────────────────────────────────────────────────────────────────────────
class Finding:
    __slots__ = ("rule_id", "outcome", "message", "evidence", "clause_ref",
                 "matched_text", "pending_reason", "needs_human", "llm_involved",
                 "llm_rationale")

    def __init__(self, rule_id, outcome, message, evidence=None, clause_ref=None,
                 matched_text=None, pending_reason=None, needs_human=False,
                 llm_involved=False, llm_rationale=None):
        self.rule_id = rule_id
        self.outcome = outcome
        self.message = message
        self.evidence = evidence or {}
        self.clause_ref = clause_ref
        self.matched_text = matched_text
        self.pending_reason = pending_reason
        self.needs_human = needs_human
        self.llm_involved = llm_involved
        self.llm_rationale = llm_rationale

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def hit(rule_id, message, evidence=None, **kw):
    return Finding(rule_id, "hit", message, evidence, **kw)


def ok(rule_id, message="符合", evidence=None, **kw):
    return Finding(rule_id, "pass", message, evidence, **kw)


def pending(rule_id, reason, evidence=None, **kw):
    """判不了。message 和 pending_reason 都写，报告上两处都要显示。"""
    return Finding(rule_id, "pending", "待人工：" + reason, evidence,
                   pending_reason=reason, needs_human=True, **kw)


def not_applicable(rule_id, why, evidence=None):
    """scope 不覆盖这张单。算 pass，但 message 要说清是「不适用」而不是「合规」。

    这两件事在报告上必须分开写：「不适用」说明这条规则跟本单无关；
    「合规」说明查过了而且没问题。混在一起会让覆盖面看起来比实际大。
    """
    f = ok(rule_id, "不适用：" + why, evidence)
    f.evidence = dict(f.evidence or {}, not_applicable=True, why=why)
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 规则集
# ─────────────────────────────────────────────────────────────────────────────
class RuleSet:
    def __init__(self, rows, rule_set_version, ontology_version, lookups, excluded,
                 release_id=None, effective_why=None):
        self.rules = rows
        self.by_id = {r["rule_id"]: r for r in rows}
        self.rule_set_version = rule_set_version
        self.ontology_version = ontology_version
        self.lookups = lookups
        self.excluded = excluded
        # 这一版是从哪个 release 认出来的。调用方（如 /api/ai-review 的缓存）
        # 拿它判断「库里的生效版本换了没有」，不必自己再去解析一遍阶梯。
        self.release_id = release_id
        self.effective_why = effective_why

    def __len__(self):
        return len(self.rules)

    @property
    def missing_impl(self):
        """执行集里没有实现的规则。

        **这不能静默**：契约校验说它该跑，而代码里没有对应实现，
        于是它既不在剔除名单里、也不会产生任何结论——
        报告上看不出少了一条。启动时必须报错。
        """
        return [r["rule_id"] for r in self.rules if r["rule_id"] not in _REGISTRY]


_REL_COLS = "r.release_id, r.rule_set_version, r.ontology_version, r.status"


def effective_release():
    """哪一版正在生效。**全站只认这一个定义**，返回 (row, why)。

    三级阶梯：
      ① `release_current` 指针——发布管理里点「切为当前」写的那条
      ② 没设指针 → `created_at` 最新的 **published** 版本
      ③ 一版都没发布过 → 最新的任意版本（通常是装载器刚建的草稿）

    **为什么要单独一个函数**：这个判断原先有两份，而且两份不一样——
    这里取「最新一版」（不分状态），而发布管理页的 `is_effective`
    取「最新的**已发布**版」。实测后果：库里 rel-0014 是装载器建的草稿、
    rel-0008 是最后发布的版本，于是引擎按草稿的 `rs-0.1` 判、页面却把
    `rs-20261021-01` 标成「生效中（自动）」——**页面说的不是引擎在用的那一版**。
    版本号要写进 cycle_log 用来复现历史结论，标错等于复现不了。
    同一个判断有两份实现迟早分叉，所以只留这一份，页面也来调它。

    第③级是给空库留的后路：`loader.py` 装载完只建草稿（装载不等于发布），
    新库里一版都没 published，不给这一级就连启动自检都过不去。
    但它会把草稿版本号写进结论，所以 why 要带回去，让调用方能说清楚。
    """
    row = db.query_one("""SELECT %s FROM release_current c
                          JOIN release r ON r.release_id = c.release_id""" % _REL_COLS)
    if row:
        return row, "发布管理里「切为当前」指定的版本"
    row = db.query_one("""SELECT %s FROM release r WHERE r.status = 'published'
                          ORDER BY r.created_at DESC LIMIT 1""" % _REL_COLS)
    if row:
        return row, "没设当前指针，自动用最新的已发布版本"
    row = db.query_one("""SELECT %s FROM release r
                          ORDER BY r.created_at DESC LIMIT 1""" % _REL_COLS)
    if row:
        return row, "库里一版都没发布过，暂用最新的草稿版本（结论上的版本号是草稿号）"
    return None, "库里没有任何 release"


def load_ruleset(only_executable=True):
    """从库里载入规则集。生效版本由 effective_release() 定，不在这里另立一套。"""
    rel, why = effective_release()
    if not rel:
        raise RuntimeError("库里没有 release，先跑 app/rules/loader.py")
    where = "WHERE version = 1" + (" AND demo_executable" if only_executable else "")
    rows = db.query("""SELECT rule_id, name, dimension, risk_class, rule_class, control_type,
                       priority, scope, logic, exception, source_clause, external_table,
                       demo_scope, compiled, compiled_reason, demo_blocker, binding_node
                       FROM rule %s ORDER BY priority DESC, rule_id""" % where)
    excluded = db.query("""SELECT rule_id, name, control_type, compiled, compiled_reason,
                           demo_blocker, binding_node FROM rule
                           WHERE version = 1 AND NOT demo_executable
                           ORDER BY rule_id""")
    lookups = load_lookups()
    return RuleSet(rows, rel["rule_set_version"], rel["ontology_version"], lookups, excluded,
                   release_id=rel["release_id"], effective_why=why)


def load_lookups():
    """把查找表读成内存结构。规则实现通过 ctx.lookup(...) 取。"""
    out = {}
    for t in db.query("SELECT table_id, kind, on_miss, key_fields, row_count FROM lookup_table"):
        rows = db.query("""SELECT row_key, row_value, enabled, confidence, source_rows,
                           must_hit, must_not_hit, conflict, gap_id, note
                           FROM lookup_row WHERE table_id = %s ORDER BY id""", (t["table_id"],))
        out[t["table_id"]] = {"meta": t, "rows": rows}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 判定上下文
# ─────────────────────────────────────────────────────────────────────────────
class Context:
    """规则实现能看到的全部东西。**只读。**

    D 阶段不碰库，所以这里的数据都是 S 阶段一次性捞好的。
    规则实现里不许再查库——那样会出现「同一个周期内两次读到不同的数据」，
    而审计表记的是 S 阶段那一份快照，对不上就说不清。
    """

    def __init__(self, ruleset, fact, standards=None, tier_map=None):
        self.rs = ruleset
        self.fact = fact
        self.standards = standards or []
        self.tier_map = tier_map or []
        self._notes = []

    def lookup(self, table_id):
        return (self.rs.lookups.get(table_id) or {}).get("rows") or []

    def lookup_meta(self, table_id):
        return (self.rs.lookups.get(table_id) or {}).get("meta") or {}

    def enabled_rows(self, table_id):
        return [r for r in self.lookup(table_id) if r.get("enabled")]

    def limit(self, std_type, tier=None, region_like=None):
        """查限额。返回匹配的行列表，调用方自己决定怎么用。"""
        out = []
        for s in self.standards:
            if s["std_type"] != std_type:
                continue
            if tier is not None and s.get("tier") != tier:
                continue
            if region_like and region_like not in (s.get("region") or ""):
                continue
            out.append(s)
        return out

    def tier_for(self, subject, rank_level):
        """主体 × 职级 → 住宿费标准档位。**映射不是恒等，带主体条件。**

        职级取不到时返回 (None, '待人工')：不许兜底。
        兜到低档会把合规的领导住宿判成超标（误报），
        兜到高档会放过超标的普通员工（漏报），两个方向都错。
        """
        subj = P.norm_strip_space(subject)
        # 先找主体精确匹配，再找通用 '*'
        for want_specific in (True, False):
            for m in self.tier_map:
                if (m["subject"] != "*") != want_specific:
                    continue
                if m["subject"] != "*" and P.norm_strip_space(m["subject"]) not in subj:
                    continue
                if m["rank_level"] != rank_level:
                    continue
                return m["tier"], None
        unknown = [m for m in self.tier_map if m["rank_level"] is None and m["on_unknown"]]
        if unknown:
            return None, unknown[0]["on_unknown"]
        return None, "待人工"


# ─────────────────────────────────────────────────────────────────────────────
# 执行
# ─────────────────────────────────────────────────────────────────────────────
def order_rules(rules):
    """按依赖排序，同层按优先级倒序。

    **不能只按优先级排。** TR-D4-06「超标准住宿需说明」优先级 35，
    而它依赖的 TR-D4-02「住宿费标准控制」优先级 30——
    按优先级倒序跑，D4-06 会先跑，永远取不到前置结论，
    于是对没超标的单子也去要「超标说明」，是纯误报。
    所以依赖优先于优先级。

    依赖来自两处：
      logic.depends_on   显式声明，`["*"]` 表示排到最后（TR-D9-01 要看
                         「哪些例外被触发了」，必须在所有规则之后）
      logic.delegates    composite 规则汇总的那几条
    环依赖直接抛异常——有环说明规则之间的前后关系本身矛盾，
    这时候随便挑一个顺序跑，结论就取决于实现细节而不是制度，必须让人来解。
    """
    by_id = {r["rule_id"]: r for r in rules}
    deps = {}
    last = []
    for r in rules:
        lg = r.get("logic") or {}
        d = list(lg.get("depends_on") or []) + list(lg.get("delegates") or [])
        if "*" in d:
            last.append(r["rule_id"])
            d = [x for x in d if x != "*"]
        # 只保留本次执行集里真有的依赖；不在执行集的由 composite 自己报 pending
        deps[r["rule_id"]] = [x for x in d if x in by_id]

    ordered, visiting, done = [], set(), set()

    def visit(rid, path):
        if rid in done:
            return
        if rid in visiting:
            raise RuntimeError("规则依赖成环：%s" % " -> ".join(path + [rid]))
        visiting.add(rid)
        for d in deps.get(rid, []):
            visit(d, path + [rid])
        visiting.discard(rid)
        done.add(rid)
        ordered.append(by_id[rid])

    normal = [r for r in rules if r["rule_id"] not in last]
    for r in sorted(normal, key=lambda x: (-(x.get("priority") or 0), x["rule_id"])):
        visit(r["rule_id"], [])
    for rid in sorted(last):
        visit(rid, [])
    assert len(ordered) == len(rules), "排序丢了规则：%d vs %d" % (len(ordered), len(rules))
    return ordered


def evaluate(ruleset, fact, standards=None, tier_map=None):
    """跑一遍规则集，返回 (findings, 统计)。**不写库。**

    D 阶段不碰库是 SDAFI 的硬纪律：写库只在 A 阶段，
    这样「模型看到什么」和「系统写了什么」才能分开追。
    """
    ctx = Context(ruleset, fact, standards, tier_map)
    findings = []
    by_rule = {}
    fact["_findings_by_rule"] = by_rule          # 规则实现靠它取前置结论
    t0 = time.time()
    for r in order_rules(ruleset.rules):
        rid = r["rule_id"]
        fn = _REGISTRY.get(rid)
        if fn is None:
            # 不静默。没实现就明确记一条，报告上能看见
            f = pending(rid, "规则在执行集里但没有实现（代码缺口，不是数据缺口）")
            findings.append(f); by_rule[rid] = f
            continue
        matched, why = P.scope_matches(r["scope"] or {}, fact)
        if not matched:
            f = not_applicable(rid, why)
            findings.append(f); by_rule[rid] = f
            continue
        try:
            f = fn(ctx, r)
        except Exception as exc:          # 单条规则炸掉不能带崩整次审核
            f = pending(rid, "规则执行异常：%s: %s" % (type(exc).__name__, str(exc)[:120]))
            findings.append(f); by_rule[rid] = f
            continue
        if f is None:
            f = pending(rid, "规则实现没有给出结论")
            findings.append(f); by_rule[rid] = f
            continue
        f.clause_ref = f.clause_ref or r["source_clause"]
        findings.append(f)
        by_rule[rid] = f
    stats = _stats(findings, ruleset)
    stats["duration_ms"] = int((time.time() - t0) * 1000)
    return findings, stats


def _stats(findings, ruleset):
    by = {"hit": 0, "pass": 0, "pending": 0, "not_applicable": 0}
    blocked = warned = 0
    for f in findings:
        if f.outcome == "pass" and (f.evidence or {}).get("not_applicable"):
            by["not_applicable"] += 1
            continue
        by[f.outcome] = by.get(f.outcome, 0) + 1
        if f.outcome == "hit":
            ct = ruleset.by_id.get(f.rule_id, {}).get("control_type")
            if ct == "禁止":
                blocked += 1
            else:
                warned += 1
    return {
        "candidates": len(ruleset) + len(ruleset.excluded),
        "executed": len(ruleset),
        "excluded": len(ruleset.excluded),
        "hit": by["hit"], "passed": by["pass"], "pending": by["pending"],
        "not_applicable": by["not_applicable"],
        "blocked": blocked, "warned": warned,
    }


def conclude(findings, ruleset):
    """由 findings 得出整单结论。

    **结论只有三档，而且 pending 不能被吞掉：**
      reject  有禁止级命中
      review  有警告级命中，或有判不了的
      pass    全部通过（含不适用）

    注意 pending 落到 review 而不是 pass。「判不了」就是要人看，
    把它算进 pass 等于声称复核了没复核的东西。
    并且：**这个结论不阻断流程。** 实测 FJPG-BX2610130821 在
    2026-10-13 17:25:03 得到「AI审核不通过」，17:35:41 仍进入共享审核
    并由人工给出退回理由。闸门在 manual_review，不在这里。
    """
    has_block = any(f.outcome == "hit" and
                    ruleset.by_id.get(f.rule_id, {}).get("control_type") == "禁止"
                    for f in findings)
    if has_block:
        return "reject"
    if any(f.outcome == "hit" for f in findings) or any(f.outcome == "pending" for f in findings):
        return "review"
    return "pass"
