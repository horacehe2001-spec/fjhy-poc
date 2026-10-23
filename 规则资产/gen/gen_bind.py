# -*- coding: utf-8 -*-
import yaml, io, json, collections
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def rel(*p): return os.path.join(ROOT, *p)

NL = chr(10)
onto = yaml.safe_load(io.open(rel("本体_v0.1.yaml"), encoding="utf-8"))
rules = json.load(io.open(rel("规则清单_v0.1.json"), encoding="utf-8"))["rules"]

# ── 概念字典 ────────────────────────────────────────────────────────────────
C = {}
for e in onto["business"]["entities"]:
    for c in e.get("concepts", []):
        C[c["id"]] = {"entity": e["id"], "entity_name": e["name"], "name": c["name"],
                      "type": c.get("type"), "source": c.get("source"),
                      "sensitive": bool(c.get("sensitive")), "ocr": bool(c.get("ocr"))}

# ── 可得性模型（全部来自 2026-10-18 对 40 张样本的实测列填充率）────────────
# 单人导出 16 列 33/33 非空；发票文件 / 事前附件 / 报销附件 三列 0/33 全空。
# 多人导出缺 报账人 / 报账人公司(核算主体) / 单据状态 / 费用承担部门 四列。
SAMPLE_OK = {  # 单人 33/33 且多人 7/7 都有
 "C0101":"单据编号","C0102":"单据类型","C0103":"单据日期","C0104":"提单人",
 "C0106":"报账部门","C0110":"业务类型","C0111":"报账金额","C0112":"付款金额",
 "C0113":"币种","C0114":"税金","C0115":"摘要",
 "C0201":"单据号","C0202":"费用明细","C0203":"费用明细金额",
}
SINGLE_ONLY = {"C0105":"报账人","C0107":"报账人公司","C0108":"单据状态","C0109":"费用承担部门"}
MULTI_ONLY  = {"C0204":"部门（多人费用明细行）"}
EMPTY_COL   = {"C1302":"附件.文件名"}
# C1301（附件.附件类型）2026-10-22 实测：219 张内嵌附件读图判类型已 100% 完成
# （attachment_classify，untyped=0），不再是"样本全空"，走下面 RESOLVED 分支而非
# EMPTY_COL/实体兜底（E13 的 ENT_RULE 仍会把它判成样本全空，必须在 avail() 里
# 早于实体兜底短路掉）。
APPROVAL_PDF= {"C1201","C1202","C1203","C1204","C1205","C1304"}
DERIVED = {"C0305": "出差天数 = 伙食补助费 ÷ 日标准（伙食补助按自然天数包干计发）；见 TR-D6-03 的 note，这是少数几条绕开 G-04 的路"}
ENT_RULE = {
 "E03":("需OA同步","G-04"), "E04":("需OA同步","G-04"),
 "E05":("需票据OCR","G-29"), "E06":("需票据OCR","G-29"), "E07":("需票据OCR","G-29"),
 "E10":("已录入资产",None),  "E11":("需外部数据源",None),
 "E13":("样本全空","G-29"),  "E14":("样本缺列","G-12"),
 "E15":("需票据OCR","G-29"), "E16":("样本缺列",None), "E17":("待业务侧明确","G-01"),
}
# C0804 职级已从 PENDING 移出：它就在审批意见页的「申请人」栏里（source=审批意见），
# 由下面 `source == "审批意见"` 那一支判成「样本可得(审批意见)」。
# 初稿把它当「待业务侧明确」，于是住宿费档位链被当成断的。
PENDING = {"C0805":("待业务侧明确","G-31"),
           "C0902":("待业务侧明确","G-01"), "C0904":("样本缺列",None),
           "C1704":("待业务侧明确","G-01")}
# 2026-10-22 实测后解除的字段：曾经「样本全空」，现在 attachment_classify
# 把 219 张附件的 attach_type 全判完了（untyped=0），C1301 真实可得。
# C1303（是否必要）被 E13 的实体级兜底误判成「样本全空」——它根本不是单据
# 样本字段，是必要附件决策表本身的结构化数据（哪个大类×小类必要哪种附件），
# 落在 规则资产/必要附件决策表_v0.1.yaml 里，和附件抽取/分类进度无关，一直可得。
RESOLVED = {"C1301": ("样本可得", None), "C1303": ("已录入资产", None)}
AVAIL_OK = {"样本可得","样本可得(审批意见)","已录入资产","可推导"}
AVAIL_SINGLE = {"仅单人可得"}

def _is_live(rid, rules, C, AVAIL_OK, AVAIL_SINGLE):
    """这条规则在当前数据条件下能不能给出结论。给 composite 判 delegate 用。

    只看一层，不递归：delegate 本身也是 composite 的情形在本规则集里不存在，
    真出现了会在校验里暴露成「执行集与 demo_executable 标记不一致」。
    """
    r = next((x for x in rules if x["rule_id"] == rid), None)
    if r is None or not r.get("compiled"):
        return False
    if (r.get("binding") or {}).get("node") == "OUT":
        return False
    lg = r.get("logic") or {}
    cids = lg.get("concepts") or []
    if not cids:
        return False
    deg_missing = set(((lg.get("degraded") or {}).get("missing")) or [])
    for c in cids:
        if c not in C:
            return False
        if C[c]["availability"] not in AVAIL_OK | AVAIL_SINGLE and c not in deg_missing:
            return False
    return True


def avail(cid):
    if cid in RESOLVED: return RESOLVED[cid]
    if cid in PENDING: return PENDING[cid]
    if cid in SAMPLE_OK: return ("样本可得", None)
    if cid in SINGLE_ONLY: return ("仅单人可得", "G-32")
    if cid in MULTI_ONLY: return ("仅多人可得", None)
    if cid in EMPTY_COL: return ("样本全空", "G-29")
    if cid in APPROVAL_PDF: return ("样本可得(审批意见)", None)
    if cid in DERIVED: return ("可推导", None)
    if C.get(cid, {}).get("source") == "审批意见": return ("样本可得(审批意见)", None)
    ent = C.get(cid, {}).get("entity")
    if ent in ENT_RULE: return ENT_RULE[ent]
    if ent in ("E01","E02","E08","E09"): return ("样本缺列", None)
    return ("未分类", None)

for cid, v in C.items():
    a, g = avail(cid)
    v["availability"] = a
    if g: v["gap"] = g

# ── 逐规则绑定 ──────────────────────────────────────────────────────────────
out = []
for r in rules:
    lg = r.get("logic", {}) or {}
    cids = list(lg.get("concepts", []))
    unknown = [c for c in cids if c not in C]
    bad = [c for c in cids if c in C and C[c]["availability"] not in AVAIL_OK | AVAIL_SINGLE]
    only_single = [c for c in cids if c in C and C[c]["availability"] in AVAIL_SINGLE]
    # 显式声明的降级：规则自己说清楚了「这几个字段取不到时我怎么判」，
    # 且不确定区间转人工。只有被 degraded.missing 完整覆盖的不可得字段才放行。
    # **不是网开一面**：没声明 degraded 的照旧剔除；声明了也必须把不确定段交人工，
    # 否则就是拿一个猜出来的标准去判别人的单子。
    # composite 规则的 delegate 若本身不在执行集里，这条规则**永远**给不出结论，
    # 只会每张单产生一条 pending。那不是「判不了这一张」，而是
    # 「在当前数据条件下这条规则根本不可能判」——属于契约层事实，不是运行期发现。
    # 实测：TR-D4-03（delegate TR-D4-05 未编译）与 TR-D8-05
    # （delegate TR-D5-01/02/06 未编译）合计贡献了 98 条待人工里的 78 条，
    # 把真正需要人看的 20 条淹掉了。
    delegates = list(lg.get("delegates") or [])
    deg = (lg.get("degraded") or {})
    deg_missing = set(deg.get("missing") or [])
    degraded_ok = bool(deg) and set(bad) <= deg_missing and deg.get("on_indeterminate") == "待人工"
    if degraded_ok:
        bad = []
    node = r.get("binding", {}).get("node")
    demo = bool(r.get("compiled")) and not bad and not unknown and node != "OUT" and bool(cids)
    dead_delegates = []
    if demo and delegates:
        dead_delegates = [d for d in delegates if not _is_live(d, rules, C, AVAIL_OK, AVAIL_SINGLE)]
        if dead_delegates:
            demo = False
    scope = "全部 40 张"
    if demo and deg:
        scope = "区间判定（缺 %s，不确定区间转人工）" % "/".join(
            C[c]["name"] for c in deg_missing if c in C)
    if demo and only_single:
        scope = "仅单人 33 张（多人导出缺 %s，见 G-32）" % "/".join(C[c]["name"] for c in only_single)
    if "demo_executable" in r:
        demo = r["demo_executable"]
    reason = None
    if not demo:
        if not r.get("compiled"): reason = "规则未编译：" + (r.get("compiled_reason") or "")[:80]
        elif node == "OUT": reason = "执行点在 OA，不进本系统执行集"
        elif not cids: reason = "logic 未声明 concepts（软规则/提示词型），由 AI 审核节点处理，不进硬执行集"
        elif dead_delegates:
            reason = ("composite 规则的 delegate 不在执行集里：" + "、".join(dead_delegates) +
                      "。这些 delegate 未编译或缺字段，本条永远给不出结论，"
                      "每张单只会产生一条 pending，把真正需要人看的淹掉。")
        elif unknown: reason = "引用了本体里不存在的概念 " + ",".join(unknown)
        else:
            reason = "依赖不可得字段：" + "、".join(
                "%s(%s·%s)" % (c, C[c]["name"], C[c]["availability"]) for c in bad)
    out.append({
        "rule_id": r["rule_id"], "name": r["name"], "dimension": r["dimension"],
        "rule_class": r["rule_class"], "control_type": r["configurator"]["control_type"],
        "node": node, "compiled": r.get("compiled"),
        "demo_executable": demo, "demo_scope": (scope if demo else None),
        "degraded": (deg or None),
        "demo_blocker": r.get("demo_blocker") or reason,
        "concepts": [{"id": c, "entity": C[c]["entity"], "name": C[c]["name"],
                      "availability": C[c]["availability"], "gap": C[c].get("gap")}
                     if c in C else {"id": c, "availability": "未定义"} for c in cids],
        "external_table": lg.get("table") or lg.get("list") or lg.get("alias_table"),
    })

# ── 反向索引：概念 → 用到它的规则 ───────────────────────────────────────────
rev = collections.OrderedDict()
for b in out:
    for c in b["concepts"]:
        rev.setdefault(c["id"], []).append(b["rule_id"])

doc = collections.OrderedDict()
doc["meta"] = {
 "version": "0.1", "generated": "2026-10-18",
 "purpose": "把 82 条规则绑到本体概念上，并标出每个概念在 POC 样本里到底取不取得到",
 "note": ("**compiled 与 demo_executable 是两件事，不要合并。** "
          "compiled = 规则资产层面是否已编译成可执行形式（口径清楚、阈值可信、决策表齐备）；"
          "demo_executable = 用这 40 张样本能不能真的跑。"
          "TR-D1-01「付款主体与核算主体一致」客户自己标「是否已有=是」，"
          "说明真实共享系统里有这个闸；但导出样本里没有「付款主体」列，所以演示跑不了。"
          "把这两件事混成一个字段，会让「客户系统做不到」和「我手上没数据」分不清——"
          "前者是产品缺口，后者只是取数没到位。"),
 "availability_classes": {
   "样本可得": "单人 33/33 与多人 7/7 都有且非空（实测列填充率）",
   "样本可得(审批意见)": "来自 50 页审批意见 PDF，可解析",
   "仅单人可得": "单人导出有、多人导出缺列。实测缺的是 报账人/报账人公司(核算主体)/单据状态/费用承担部门 四列，见 G-32",
   "仅多人可得": "多人费用明细行有「部门」列，单人没有",
   "样本全空": "列存在但 100% 为空（发票文件/事前附件/报销附件），见 G-29",
   "样本缺列": "导出里没有这一列",
   "需OA同步": "出差审批单/出差日历字段，OA 尚未同步到共享，见 G-04",
   "需票据OCR": "需要读发票/车票/住宿单原件，而样本一个票据文件都没给",
   "待业务侧明确": "口径本身还没定（职级、常驻地、直管企业范围）",
   "已录入资产": "已落在 规则资产/标准表_v0.1.yaml 里",
   "需外部数据源": "预算系统等本系统之外的数据",
 },
 "measured": {
   "sample_bills": 40, "single": 33, "multi": 7,
   "single_columns_full": 16, "single_columns_empty": 3,
   "multi_missing_columns": ["报账人", "报账人公司（核算主体）", "单据状态", "费用承担部门"],
   "orphan_detail_bill": "FJPG-BX2609041860（8 行明细无主表行，见 G-30）",
 },
}
doc["meta"]["availability_classes"]["可推导"] = "本体里没有直接字段，但可由样本已有字段算出"
doc["derivations"] = DERIVED
doc["concept_availability"] = C
doc["bindings"] = out
doc["reverse_index"] = rev
doc["gaps"] = {
 "G-31": {"title": "常驻地字段在共享系统里是否存在、取值口径为何，未定",
          "detail": ("本体 glossary 原先把常驻地挂在 G-03 上，但 G-03 已重定义为「交通工具等级标准表无法录入」，"
                     "属悬空引用，现独立为 G-31。制度《差旅管理实施细则》对常驻地有明确界定（标准表 RES-01~08，"
                     "含漳州港务古雷港、泉州港务中心城区等特殊口径），但 40 张样本里没有任何一列是常驻地。"
                     "TR-D9-05「出差地点须写到市级且非常驻地」靠它，TR-D4-02 住宿费标准匹配又靠 TR-D9-05。"),
          "impact": "TR-D9-05 及其下游 TR-D4-02", "owner": "共享中心（数据接口方）", "blocking": True},
 "G-32": {"title": "多人差旅报账单导出缺 4 列，其中包含核算主体",
          "detail": ("实测：「2多人差旅报账单」只有 12 列，比单人少 报账人 / 报账人公司（核算主体） / "
                     "单据状态 / 费用承担部门。核算主体是 D1 维度 3 条规则的比较基准，缺了它，"
                     "TR-D1-01 / TR-D1-02 / TR-D6-04 对 7 张多人单全部无法判。"
                     "**注意这不是「多人单没有核算主体」，而是「导出时没带上」**——两种结论的处置完全不同。"),
          "impact": "7 张多人单 × D1 维度 3 条规则", "owner": "共享中心（数据接口方）", "blocking": False},
}
io.open(rel("规则绑定_v0.1.json"), "w", encoding="utf-8", newline="").write(
    json.dumps(doc, ensure_ascii=False, indent=1))

# ── 统计 ────────────────────────────────────────────────────────────────────
print("概念总数 %d" % len(C))
ca = collections.Counter(v["availability"] for v in C.values())
for k, v in ca.most_common(): print("   %-18s %d" % (k, v))
print()
de = sum(1 for b in out if b["demo_executable"])
print("可编译 %d / 82 ；**演示可执行 %d / 82**" % (sum(1 for b in out if b["compiled"]), de))
print("演示不可执行的原因分布:")
rc = collections.Counter()
for b in out:
    if b["demo_executable"]: continue
    t = b["demo_blocker"] or ""
    k = ("规则未编译" if t.startswith("规则未编译") else
         "执行点在 OA" if t.startswith("执行点") else
         "软规则/无 concepts" if t.startswith("logic 未声明") else
         "依赖不可得字段" if t.startswith("依赖") else "其他(规则清单显式声明)")
    rc[k] += 1
for k, v in rc.most_common(): print("   %-20s %d" % (k, v))
print()
print("演示可执行的 %d 条，按维度：%s" % (de, dict(collections.Counter(b["dimension"] for b in out if b["demo_executable"]))))
print("演示可执行的，按控制类型：%s" % dict(collections.Counter(b["control_type"] for b in out if b["demo_executable"])))
et = set()
for b in out:
    t = b["external_table"]
    if isinstance(t, list): et.update(t)
    elif t: et.add(t)
print("引用到的外部表/名单：%s" % sorted(et))
print("按演示范围：%s" % dict(collections.Counter(b["demo_scope"] for b in out if b["demo_executable"])))
print("未被任何规则引用的概念：%d / %d" % (len(C) - len(rev), len(C)))
