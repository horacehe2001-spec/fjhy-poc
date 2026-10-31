# -*- coding: utf-8 -*-
"""流程本体 · 建设 / 图谱 / 存储。

和规则本体一样分三段，但**建设方式不同，页面上必须说清楚**：
规则本体的统一口径是智能体协同跑出来的；流程本体的节点与迁移是
**确定性解析器**从真实审批记录里数出来的，没有 Agent，也没有模型。
不说清楚，看页面的人会以为这里也有一条智能体链在跑。

这个模块里最要紧的一件事是**漂移核对**，而且核对必须是「不猜」的：
审批记录里的节点名（开始 / 共享审核 / 部门负责人…）和本体里的节点名
（提单 / 共享审核·初审 / 部门负责人审批…）**不是同一套写法**。
两套名字之间的映射是**口径决策，不是技术判断**——所以这里只做三件事：
  精确相等        → 对上了
  本体名以实测名开头 → 对上了，但标明是前缀匹配（共享审核 → 共享审核·初审）
  都不满足        → **标「本体里找不到对应节点」，不猜**
实测结果：综合部分管领导 5 次、总经理 3 次、分管领导 2 次，共 10 次出现
在本体里没有节点。这 10 次就是漂移的全部证据，
而它们正是「本体是设计出来的还是数出来的」这个问题的答案。
"""
from . import db, onto_current


def _iid(instance_id=None):
    """当前实例。本模块每一句查询都要用它——原因见下。"""
    from . import scope
    return scope.require(instance_id)


def _version(instance_id=None):
    return onto_current.version(instance_id)


# ── 为什么这个模块每一句都必须带 instance_id（2026-10-31 补）──────────────
# 原来整个模块只按 version 过滤。**而两个实例的当前本体版本号都叫「0.1」**，
# 所以「按版本过滤」看着像过滤了，其实一点没过滤掉另一家。
# 实测后果：822港航 的 process_node 是 0 行、福建海运集团有 10 行，
# 于是 822港航 的流程本体页**显示的是福建海运集团的 10 个节点和 12 条迁移**
# ——另一家客户的流程出现在这家客户的页面上，而且页面上没有任何异常迹象，
# 看起来就像「822港航 的流程本体已经建好了」。
# approval_record 同理：漂移核对拿的是全库 388 条审批记录去对，
# 「实测节点名」里混着另一家的节点名，核对结论整段不可信。
# 按版本过滤挡不住跨实例——**实例和版本是两个维度，每一句都要带两个**。


def build(instance_id=None):
    """本体建设：节点与迁移是从哪儿来的、和真实记录差多少。"""
    iid = _iid(instance_id)
    v = _version(iid)
    nodes = db.query("""SELECT node_id, name, is_bypass, occurrences, note, alias
                          FROM process_node
                         WHERE version = %s AND instance_id = %s
                         ORDER BY node_id""", (v, iid))
    measured = db.query("""SELECT node, count(*) n, count(DISTINCT bill_no) bills
                             FROM approval_record WHERE instance_id = %s
                            GROUP BY 1 ORDER BY 2 DESC""", (iid,))
    names = {n["name"]: n for n in nodes}
    # **别名也算对上。** 这一条是实测补的：审批记录里的「开始」出现 103 次，
    # 本体里 N01 提单 带 alias: [开始]，但装载脚本一直没把 alias 写进库，
    # 于是这 103 次被报成「本体里找不到对应节点」——**假红**。
    # 假红比漏报更隐蔽：它看起来像一个待办，人去查一圈发现本体里本来就有，
    # 下次就不再认真看这张表了。
    aliases = {a: n for n in nodes for a in (n["alias"] or [])}

    drift = []
    for m in measured:
        hit, how = None, "未匹配"
        if m["node"] in names:
            hit, how = names[m["node"]], "精确"
        elif m["node"] in aliases:
            hit, how = aliases[m["node"]], "别名"
        else:
            pre = [n for n in nodes if n["name"].startswith(m["node"])]
            if len(pre) == 1:
                hit, how = pre[0], "前缀"
            elif len(pre) > 1:
                how = "多个候选"
        drift.append({"measured_node": m["node"], "n": m["n"], "bills": m["bills"],
                      "match": how, "node_id": hit["node_id"] if hit else None,
                      "onto_name": hit["name"] if hit else None})
    matched_ids = {d["node_id"] for d in drift if d["node_id"]}
    unseen = [{"node_id": n["node_id"], "node_name": n["name"]} for n in nodes
              if n["node_id"] not in matched_ids]

    return {
        "version": v,
        "source": {"审批记录": db.scalar("SELECT count(*) FROM approval_record "
                                     "WHERE instance_id = %s", (iid,)),
                   "覆盖单据": db.scalar("SELECT count(DISTINCT bill_no) FROM approval_record "
                                     "WHERE instance_id = %s", (iid,)),
                   "实测节点名": len(measured),
                   "本体节点": len(nodes),
                   "本体迁移": db.scalar("SELECT count(*) FROM process_transition "
                                     "WHERE version = %s AND instance_id = %s",
                                     (v, iid))},
        "built_by": "确定性解析器（app/perception），不是智能体协同，也不调模型",
        "drift": drift,
        "unseen_nodes": unseen,
        # 四个数必须加得上实测节点名总数（实测 9 = 4 + 1 + 4），
        # 少列一档就会出现「4+4=8 而总数是 9」这种悄悄对不上的账。
        # 「多个候选」单独一档：共享审核 125 次同时像 N05 初审和 N06 复审，
        # **选哪个是口径决策**，不是技术判断。
        "drift_summary": {
            "对上的": sum(1 for d in drift if d["match"] in ("精确", "别名", "前缀")),
            "多个候选": sum(1 for d in drift if d["match"] == "多个候选"),
            "未匹配": sum(1 for d in drift if d["match"] == "未匹配"),
            "未匹配出现次数": sum(d["n"] for d in drift if d["match"] == "未匹配"),
            "多个候选出现次数": sum(d["n"] for d in drift if d["match"] == "多个候选"),
            "本体里有而记录里没见过": len(unseen),
        },
        "note": "两套名字之间的映射是**口径决策，不是技术判断**，所以这里只认"
                "「精确相等」和「本体名以实测名开头」两种，都不满足就标未匹配，不猜。"
                "**本体里 occurrences 列是空的**，页面上的实测次数每次从审批记录现算——"
                "现算是对的：存一份下来，它就会和真实记录各自漂移，"
                "而漂移的那一刻没有任何提示。",
    }


def _start_nodes(nodes, edges, adj):
    """推分层 BFS 的起点，返回 (起点列表, 用了哪条规则)。

    原来这里写死的是 `["N01"]`——那是**福建海运集团**的提单节点号。
    822港航 的提单是 P01，于是它 10 个节点全都 rank=None、全被当成旁路
    画在单独一行；而图还是画得出来的，页面上看不出哪里错了。
    这是「写死另一家的编号」这一类 bug 的第 N 次，所以这里不再写死。

    为什么不直接用「没有入边的节点」：**福建海运集团的 N01 有入边**
    （实测：退回之后回到提单，是个环），用这条规则它会退化成 0 个起点，
    整张图的层次一起没了——比写死更糟。所以按三级阶梯：

      ① 非旁路节点里名字叫「提单」或别名含「开始」的，恰好一个 → 用它
      ② 否则取「没有入边但有出边」的节点（有向无环图的正常情形）
      ③ 都不成立 → 退化成最小的非旁路节点号，并在 how 里说明是退化的

    推不出来就返回空列表：**猜错的层次比没有层次更坏**，
    页面按「排不进层次」画，起码是实话。
    """
    named = [n["node_id"] for n in nodes
             if not n["is_bypass"] and (n["node_name"] == "提单"
                                        or "开始" in (n.get("alias") or []))]
    if len(named) == 1:
        return named, "起点按节点名「提单」/别名「开始」认定：%s" % named[0]
    has_in = {e["to_node"] for e in edges}
    roots = [n["node_id"] for n in nodes
             if n["node_id"] not in has_in and n["node_id"] in adj]
    if roots:
        return roots, "起点按「没有入边但有出边」认定：%s" % "、".join(roots)
    plain = sorted(n["node_id"] for n in nodes if not n["is_bypass"])
    if plain:
        return plain[:1], ("**起点是退化推定的**（既没有名叫「提单」的节点，"
                           "也没有无入边的节点，图里有环）：取最小节点号 %s"
                           % plain[0])
    return [], "**推不出起点**：这个实例没有非旁路节点，层次一律留空"


def graph(instance_id=None):
    """流程图谱：节点 + 迁移，带实测次数与分层。

    **分层在后端算**（从提单 N01 出发按迁移做 BFS），不在前端估：
    前端估出来的层次会随数据变，而两次打开页面图形不一样，
    人就无法把「图变了」当成「本体变了」的信号。
    """
    iid = _iid(instance_id)
    v = _version(iid)
    nodes = db.query("""SELECT node_id, name AS node_name, is_bypass, note, alias
                          FROM process_node
                         WHERE version = %s AND instance_id = %s
                         ORDER BY node_id""", (v, iid))
    edges = db.query("""SELECT from_node, to_node, cond, blocking, note
                          FROM process_transition
                         WHERE version = %s AND instance_id = %s
                         ORDER BY from_node, to_node""", (v, iid))
    cnt = {}
    # **把 iid 传下去**：不传就是再取一次「当前实例」，显式传了别的实例的
    # 调用方会拿到「本实例的节点 + 当前实例的实测次数」，两边不是一家。
    for d in build(iid)["drift"]:
        if d["node_id"]:
            cnt[d["node_id"]] = cnt.get(d["node_id"], 0) + d["n"]

    adj = {}
    for e in edges:
        adj.setdefault(e["from_node"], []).append(e["to_node"])
    roots, how = _start_nodes(nodes, edges, adj)
    rank, frontier, seen = {}, list(roots), set(roots)
    depth = 0
    while frontier:
        nxt = []
        for n in frontier:
            rank[n] = depth
            for m in adj.get(n, []):
                if m not in seen:
                    seen.add(m)
                    nxt.append(m)
        frontier, depth = nxt, depth + 1
    for n in nodes:
        n["measured"] = cnt.get(n["node_id"])
        n["rank"] = rank.get(n["node_id"])
        # 旁路节点（撤回、审批人=报账人跳过）没有入边，排不进层次——
        # **这不是数据缺陷，是旁路的本来面目**，页面上单独一行画
        n["off_path"] = n["rank"] is None
    # 说明从**本实例的数据**生成。原来这里是一段写死的文字，讲的是
    # 福建海运集团那条 N04→N05 的非阻断边；822港航 没有 N04 也没有 N05，
    # 可它的流程页照样显示这段话——又一处「另一家的结论画在这一家页面上」。
    name_of = {n["node_id"]: n["node_name"] for n in nodes}
    bits = [how]
    soft = [e for e in edges if not e["blocking"]]
    if soft:
        bits.append(
            "非阻断的边 %d 条：%s。**阻断与否标错，整个定位就错了**——"
            "AI 结论是参考，闸门在人工手里。"
            % (len(soft), "；".join(
                "%s %s → %s %s（%s）" % (
                    e["from_node"], name_of.get(e["from_node"], ""),
                    e["to_node"], name_of.get(e["to_node"], ""),
                    e["cond"] or "无条件") for e in soft[:3])))
    else:
        bits.append("本实例没有非阻断的边：每一条迁移都是必过的。")
    return {"version": v, "nodes": nodes, "edges": edges,
            "note": "｜".join(bits)}


STORAGE = [
    ("process_node", "流程节点（含旁路）", "本体装载", "不清",
     "版本钉在 ontology_version 上；occurrences 列留空，实测次数现算"),
    ("process_transition", "节点之间的迁移（带条件与是否阻断）", "本体装载", "不清",
     "blocking=false 的那条边是实测结论，不是设计值"),
    ("approval_record", "真实审批记录（节点与迁移的唯一事实来源）", "数据导入", "不清",
     "368 条、覆盖真实单据。**流程本体的每个节点都必须能数回到这张表**"),
    ("ontology_version", "本体版本与源文件哈希", "本体装载", "不清",
     "启动自检比对 source_hash，本体漂移就拒绝启动"),
]


def storage(instance_id=None):
    iid = _iid(instance_id)
    rows = []
    for t, what, who, on_recalc, why in STORAGE:
        try:
            # 四张表都有 instance_id，**存储页报的行数必须是本实例的**：
            # 不带实例，822港航 的存储页会报 process_node 10 行，
            # 而它自己一行都没有——看起来像建好了。
            n = db.scalar("SELECT count(*) FROM " + t
                          + " WHERE instance_id = %s", (iid,))
        except Exception:                                      # noqa: BLE001
            n = None
        rows.append({"table_name": t, "what": what, "written_by": who,
                     "on_recalc": on_recalc, "why": why, "rows": n})
    return {"items": rows,
            "note": "流程本体这边**没有「算出来的」表**：节点与迁移都是装载进来的，"
                    "实测次数每次现算不落库。所以这一栏不存在「重算会清掉什么」的问题——"
                    "和规则本体那边不一样，那边的对账结论是整表重算的。"}
