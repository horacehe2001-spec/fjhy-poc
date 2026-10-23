# -*- coding: utf-8 -*-
"""感知层共用的小工具。

目前只有一支：两张读图台账的失败视图。
    attachment_vision   判类型（attachment_classify 写）
    attachment_extract  抽字段（attachment_extract_fields 写）

两张表的失败相关列结构完全相同（ok / err_kind / message / attempt /
latency_ms / created_at），所以共用这一支——各写一遍，改了一处忘另一处
是迟早的事。
"""
from .. import db

# 只允许这两张表。**表名要拼进 SQL**（标识符不能用参数占位符），
# 所以必须白名单——哪怕现在的调用方全是写死的字面量，
# 「调用方都是可信的」正是最容易出事的那种假设。
_LEDGERS = {
    "attachment_vision": "判类型",
    "attachment_extract": "抽字段",
}

# 网络层的失败类型。用来判断「这些失败是不是都怪网络」——
# 是，才能说「不是模型拒答」；不是，就不许这么说。
_NET_KINDS = {"timeout", "http", "tls", "conn", "network", "proxy"}


def summarize_failures(by_kind, failed_imgs, recovered_imgs):
    """由三份聚合数据算出 (失败次数, 是否全网络层, 说明文案)。

    **抽成纯函数是为了能测空表那一支。** 台账里永远有真实数据的失败留痕，
    清不空，于是「by_kind 为空时不许冒充『失败全怪网络』」这条判断在带库的
    测试里永远走不到——写成 `if not by_kind` 才断言的测试是真空通过的，
    用探针验过：把空表判断删掉，那条测试照样绿。
    这正是「确定性的闸失效而没人发现」，所以判定逻辑搬到这里来，直接喂参数测。
    """
    calls_failed = sum(int(r["n"] or 0) for r in by_kind)
    lost = failed_imgs - recovered_imgs
    # 「没有失败」和「失败全怪网络」是两句不同的话。
    # all([]) 返回 True，所以必须先判空，否则空表会冒充后者。
    all_net = bool(by_kind) and all(
        (r["err_kind"] or "") in _NET_KINDS for r in by_kind)

    if not by_kind:
        note = "没有失败记录。"
    elif lost == 0:
        note = ("%d 次失败涉及 %d 张图，**这 %d 张后来全部重试成功，一张都没丢**。"
                % (calls_failed, failed_imgs, failed_imgs))
        if all_net:
            note += "失败全部在网络层（超时 / 连接中断 / TLS），**没有一次是模型拒答或解析失败**。"
        note += "所以这里的失败数是成本留痕，**不是待办**——不需要再重跑一轮读图。"
    else:
        note = ("%d 次失败涉及 %d 张图，其中 %d 张后来重试成功，**还有 %d 张至今没有一次"
                "成功的读图记录**。这 %d 张是待办，要去查。"
                % (calls_failed, failed_imgs, recovered_imgs, lost, lost))
    return calls_failed, all_net, note


def ledger_failures(table, limit=50):
    """一张读图台账的失败视图。

    **「失败了几次」和「丢了几张图」是两个数，必须并列给。**
    实测（2026-10-24）判类型 25 次失败、抽字段 24 次失败，加起来 49 次，
    听起来像有 49 张图没读出来；实际曾失败的 24 + 20 张**全部在后续
    attempt 里成功了，一张都没丢**。只报失败次数，看页面的人会得出
    完全相反的结论——而那个结论会让人去重跑一轮已经跑完的读图（真计费）。
    """
    if table not in _LEDGERS:
        raise ValueError("不认识的台账表：%s" % table)

    by_kind = db.query(
        "SELECT err_kind, count(*) AS n, min(left(message, 160)) AS sample "
        "FROM %s WHERE NOT ok GROUP BY 1 ORDER BY 2 DESC" % table)

    # 曾失败的图、以及其中后来成功的图。两个数在同一条 SQL 里算，
    # 分两次查会在中间有新调用时对不上。
    img = db.query_one(
        "SELECT count(DISTINCT attachment_id) AS failed_imgs, "
        "       count(DISTINCT attachment_id) FILTER (WHERE attachment_id IN "
        "           (SELECT attachment_id FROM %s WHERE ok)) AS recovered_imgs "
        "FROM %s WHERE NOT ok" % (table, table)) or {}
    failed_imgs = int(img.get("failed_imgs") or 0)
    recovered = int(img.get("recovered_imgs") or 0)
    lost = failed_imgs - recovered

    # later_ok 一行一行算：页面上每条失败记录都要能看出「这张图后来成了没」。
    # 不算这一列，使用者只能看到一串红色的失败，不知道哪些已经不用管了。
    recent = db.query(
        "SELECT v.id, v.attachment_id, a.bill_no, a.file_name, v.attempt, "
        "       v.err_kind, left(v.message, 300) AS message, v.latency_ms, "
        "       v.model_name, v.created_at, "
        "       EXISTS (SELECT 1 FROM %s w WHERE w.attachment_id = v.attachment_id "
        "               AND w.ok) AS later_ok "
        "FROM %s v LEFT JOIN attachment a ON a.id = v.attachment_id "
        "WHERE NOT v.ok ORDER BY v.created_at DESC LIMIT %%s" % (table, table),
        (limit,))

    calls_failed, all_net, note = summarize_failures(by_kind, failed_imgs, recovered)

    return {
        "stage": _LEDGERS[table],
        "table": table,
        "calls_failed": calls_failed,
        "by_kind": by_kind,
        "failed_imgs": failed_imgs,
        "recovered_imgs": recovered,
        "lost_imgs": lost,
        "all_network": all_net,
        "recent": recent,
        "note": note,
    }
