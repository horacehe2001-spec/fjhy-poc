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
from . import db


def _version():
    return db.scalar("SELECT version FROM ontology_version WHERE is_current")


def build():
    """本体建设：节点与迁移是从哪儿来的、和真实记录差多少。"""
    v = _version()
    nodes = db.query("""SELECT node_id, name, is_bypass, occurrences, note, alias
                          FROM process_node WHERE version = %s ORDER BY node_id""", (v,))
    measured = db.query("""SELECT node, count(*) n, count(DISTINCT bill_no) bills
                             FROM approval_record GROUP BY 1 ORDER BY 2 DESC""")
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
        "source": {"审批记录": db.scalar("SELECT count(*) FROM approval_record"),
                   "覆盖单据": db.scalar("SELECT count(DISTINCT bill_no) FROM approval_record"),
                   "实测节点名": len(measured),
                   "本体节点": len(nodes),
                   "本体迁移": db.scalar("SELECT count(*) FROM process_transition "
                                     "WHERE version = %s", (v,))},
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


def graph():
    """流程图谱：节点 + 迁移，带实测次数与分层。

    **分层在后端算**（从提单 N01 出发按迁移做 BFS），不在前端估：
    前端估出来的层次会随数据变，而两次打开页面图形不一样，
    人就无法把「图变了」当成「本体变了」的信号。
    """
    v = _version()
    nodes = db.query("""SELECT node_id, name AS node_name, is_bypass, note
                          FROM process_node WHERE version = %s ORDER BY node_id""", (v,))
    edges = db.query("""SELECT from_node, to_node, cond, blocking, note
                          FROM process_transition WHERE version = %s
                         ORDER BY from_node, to_node""", (v,))
    cnt = {}
    for d in build()["drift"]:
        if d["node_id"]:
            cnt[d["node_id"]] = cnt.get(d["node_id"], 0) + d["n"]

    adj = {}
    for e in edges:
        adj.setdefault(e["from_node"], []).append(e["to_node"])
    rank, frontier, seen = {}, ["N01"], {"N01"}
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
    return {"version": v, "nodes": nodes, "edges": edges,
            "note": "N04 AI审核 → N05 共享审核 的 blocking=false 是实测结论："
                    "有单在 17:25:03 得到「AI审核不通过」，17:35:41 仍进入共享审核"
                    "并由人工给出退回理由。**AI 结论是参考，闸门在人工手里**——"
                    "这条边的 blocking 标错，整个 POC 的定位就错了。"}


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


def storage():
    rows = []
    for t, what, who, on_recalc, why in STORAGE:
        try:
            n = db.scalar("SELECT count(*) FROM " + t)
        except Exception:                                      # noqa: BLE001
            n = None
        rows.append({"table_name": t, "what": what, "written_by": who,
                     "on_recalc": on_recalc, "why": why, "rows": n})
    return {"items": rows,
            "note": "流程本体这边**没有「算出来的」表**：节点与迁移都是装载进来的，"
                    "实测次数每次现算不落库。所以这一栏不存在「重算会清掉什么」的问题——"
                    "和规则本体那边不一样，那边的对账结论是整表重算的。"}
