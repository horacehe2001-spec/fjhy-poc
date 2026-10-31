# -*- coding: utf-8 -*-
"""S 阶段：Sense。**只读纯 IO，一行都不写。**

职责只有一个：把一张单在库里的样子一次性捞成「事实对象」，
并算出快照指纹。之后 D 阶段只看这个对象，不再查库。

为什么 D 阶段不许再查库：
  规则实现里再查一次库，就可能在同一个周期内读到两份不同的数据
  （中间有人改了单据）。而 data_pool_snapshot 记的是 S 阶段这一份，
  事后复查时「审计记录里的输入」和「当时真正用的输入」对不上，
  整套审计就没有意义了。

事实对象的形状由 事实模型_v0.1.json 决定，键名用中文业务含义还是英文列名？
—— 用英文列名。理由：规则实现里要频繁取值，列名与库一致可以直接对照 schema；
而中文业务含义已经在 ontology_concept 里有权威记录，不必在代码里再抄一遍。
"""
import hashlib
import json

from .. import db, mask


def snapshot_fact(bill_no):
    """捞一张单的全部事实。返回 (fact, snapshot_meta)。

    **整个快照走同一条连接。** 第一版每个子查询都调 db.query，
    而 db.query 每次新开一条 psycopg2 连接——一张单开 10 次连接，
    实测 S 阶段 p50 = 733 ms，占整个周期 1164 ms 的 63%。

    还有一个正确性上的理由：同一条连接读到的是一致的一份数据。
    分散在 10 条连接上读，中间若有人改了单据，就会拼出一个
    **从未真实存在过**的事实对象，而审计表记的却是它。
    """
    with db.cursor() as cur:
        return _snapshot(cur, bill_no)


def _q(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _q1(cur, sql, params=()):
    rows = _q(cur, sql, params)
    return rows[0] if rows else None


def _snapshot(cur, bill_no):
    counts = {}

    bill = _q1(cur, """SELECT b.*, o.name AS org_name, o.city AS org_city,
                              o.direct_managed, o.is_internal
                       FROM expense_bill b
                       LEFT JOIN org o ON o.org_id = b.org_id
                       WHERE b.bill_no = %s""", (bill_no,))
    if not bill:
        return None, None
    counts["expense_bill"] = 1

    details = _q(cur, """SELECT id, expense_item, amount, alloc_dept, line_no
                         FROM expense_detail WHERE bill_no = %s ORDER BY line_no, id""",
                 (bill_no,))
    counts["expense_detail"] = len(details)

    approvals = _q(cur, """SELECT seq, node, approver, opinion, approved_at
                           FROM approval_record WHERE bill_no = %s ORDER BY seq, id""",
                   (bill_no,))
    counts["approval_record"] = len(approvals)

    attachments = _q(cur, """SELECT attach_type, file_name, is_required, pages
                             FROM attachment WHERE bill_no = %s ORDER BY id""", (bill_no,))
    counts["attachment"] = len(attachments)

    # 一张单可以挂**多份**出差审批单：实测 FJPG-BX2607280255 有两份
    # （07-23~24 与 07-25~26 两段行程），人工驳的就是「往返起止地点不一致」。
    # `trip` 保留单份口径不动（福建海运集团那 28 条规则都读它），
    # 另给 `trips` 全量——判「逐日表有没有覆盖出差区间」必须按份判，
    # 只看第一份会把另一份的缺漏整段放过去。
    trips = _q(cur, """SELECT * FROM trip_application WHERE bill_no = %s
                        ORDER BY start_date, app_no""", (bill_no,))
    trip = trips[0] if trips else None
    counts["trip_application"] = len(trips)

    invoices = _q(cur, """SELECT invoice_id, inv_type, inv_no, issue_date, issuer, title,
                                 total_amount, tax_amount, tax_rate, detail_text,
                                 remark_col, void_status, is_eticket, ocr_confidence
                          FROM invoice WHERE bill_no = %s ORDER BY invoice_id""", (bill_no,))
    counts["invoice"] = len(invoices)

    travelers = _q(cur, """SELECT emp_no, name, ord FROM bill_traveler
                           WHERE bill_no = %s ORDER BY ord""", (bill_no,))
    counts["bill_traveler"] = len(travelers)

    # 业务招待明细（E18）。迁移 0053 新建的表，**建表时这个装配函数还没改**——
    # 不装进 fact，接待类规则在 D 阶段就读不到它，而规则实现里不许查库。
    # 招待对象名单那一列**不取**：整列不出明文（外部人员姓名与职务黏连，
    # 脱敏闭集合切不出来，见 app/mask.py 的 roster 策略）。
    # 规则要的是「名单里有几个人」，所以这里只给算出来的人数。
    entertains = _q(cur, """SELECT line_no, amount, entertain_type, entertain_way,
                                   companion_level, place, happen_date,
                                   guest_count, companion_count, liquor_amount,
                                   CASE WHEN guest_list IS NULL THEN NULL
                                        WHEN guest_list ~ '[、,，;；]'
                                        THEN array_length(
                                             regexp_split_to_array(
                                               guest_list, '[、,，;；]+'), 1)
                                        ELSE NULL END AS guest_list_count
                              FROM entertain_detail WHERE bill_no = %s
                             ORDER BY line_no, id""", (bill_no,))
    counts["entertain_detail"] = len(entertains)

    # 出差日历（E04）。挂在 trip_application 上，没有 bill_no 列，要经审批单关联。
    # 伙食补助与市内交通费规则靠它判「接待单位有没有安排餐/提供车辆」。
    # app_no 必须带上：一张单可能挂两份审批单，不带就没法把某一天归到
    # 哪一段行程，判覆盖率时两段的日期会混成一堆，缺漏被填平。
    trip_days = _q(cur, """SELECT c.app_no, c.cal_date, c.meal_slot,
                                  c.has_meal, c.has_car, c.place
                             FROM trip_calendar c
                             JOIN trip_application t ON t.app_no = c.app_no
                            WHERE t.bill_no = %s
                            ORDER BY c.cal_date, c.meal_slot""", (bill_no,))
    counts["trip_calendar"] = len(trip_days)

    # 住宿发票的住宿明细（C0604 住宿晚数）。挂在 invoice 上，没有 bill_no 列，
    # 要经 invoice 关联。TR-D4-14 的「200 元/晚」按晚数算，拿天数代替会差一晚。
    hotel_invoices = _q(cur, """SELECT h.invoice_id, h.check_in, h.check_out,
                                       h.nights, h.rooms, h.is_daybreak
                                  FROM hotel_invoice h
                                  JOIN invoice i ON i.invoice_id = h.invoice_id
                                 WHERE i.bill_no = %s ORDER BY h.invoice_id""", (bill_no,))
    counts["hotel_invoice"] = len(hotel_invoices)

    # 交通凭证（C0706 往返行程）。TR-D6-05 要判去程/返程是否都有。
    transport_vouchers = _q(cur, """SELECT voucher_id, invoice_id, vehicle, seat_class,
                                           from_place, to_place, ride_date, has_round_trip,
                                           refund_fee, bus_has_identity, vehicle_enum
                                      FROM transport_voucher WHERE bill_no = %s
                                     ORDER BY ride_date, voucher_id""", (bill_no,))
    counts["transport_voucher"] = len(transport_vouchers)

    emp_sub = _q1(cur, "SELECT * FROM employee WHERE emp_no = %s",
                  (bill["submitter_no"],)) if bill.get("submitter_no") else None
    emp_cla = _q1(cur, "SELECT * FROM employee WHERE emp_no = %s",
                  (bill["claimant_no"],)) if bill.get("claimant_no") else None

    # TR-D10-06 拆单检测要看「1~2 日内同主体同收款方的其他单据」。
    # 收款方（C0123）当前取不到（来自审批意见 PDF，尚未解析），
    # 所以这里只按「同主体 + 1~2 日内」取，并在事实里标明口径打了折。
    # **不标的话，规则会以为自己真的按收款方比过了。**
    peers = _q(cur, """SELECT bill_no, amount, doc_date, payee FROM expense_bill
                       WHERE org_id = %s AND bill_no <> %s
                         AND doc_date BETWEEN %s::timestamptz - INTERVAL '2 days'
                                          AND %s::timestamptz + INTERVAL '2 days'""",
               (bill["org_id"], bill_no, bill["doc_date"], bill["doc_date"])) \
        if bill.get("doc_date") and bill.get("org_id") else []

    fact = {
        "bill": bill,
        "details": details,
        "approvals": approvals,
        "attachments": attachments,
        "trip": trip,
        "trips": trips,
        "invoices": invoices,
        "entertain_details": entertains,
        "trip_days": trip_days,
        "hotel_invoices": hotel_invoices,
        "transport_vouchers": transport_vouchers,
        "travelers": travelers,
        "org": {"name": bill.get("org_name"), "city": bill.get("org_city"),
                "direct_managed": bill.get("direct_managed"),
                "is_internal": bill.get("is_internal")},
        "employee_submitter": emp_sub,
        "employee_claimant": emp_cla,
        "peer_bills_2d": peers,
        "_degraded_inputs": {
            "peer_bills_2d": "只按「同主体 + 2 日内」筛，没按收款方——"
                             "收款方 C0123 来自审批意见 PDF，当前未解析入库。"
                             "口径比条文宽，可能多报，不会漏报。",
        },
        "delegation_authorized": None,      # 共享系统代提单授权，不在本次数据范围内
    }
    payload_hash = fact_hash(fact)
    meta = {
        "snapshot_id": "snap-" + payload_hash[:16],
        "subject_id": bill_no,
        "source_tables": counts,
        "payload_hash": payload_hash,
        "row_counts": counts,
    }
    return fact, meta


def fact_hash(fact):
    """输入指纹。

    **必须排除每次都变的字段**，否则指纹每次都不同，
    F 阶段就没法判「同样的输入是否得到同样的结论」、
    I 阶段也没法判「调参到底有没有效果」。
    这里排除 created_at / uploaded_at 一类时间戳，但保留业务日期。
    """
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in sorted(o.items())
                    if k not in ("created_at", "uploaded_at", "loaded_at", "id")}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o if isinstance(o, (int, float, bool)) or o is None else str(o)

    blob = json.dumps(clean(fact), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def masked_payload(fact):
    """给 data_pool_snapshot 入库用的脱敏副本。

    **入库前就要脱。** 快照会被审计接口、调试查询、导出文件读到，
    这些都是旁路出口；指望出口各自去脱，总有一条路会漏。
    返回 (payload, masked?)。masked 为 False 不允许出现在生产。
    """
    payload = {k: v for k, v in fact.items() if not k.startswith("_")}
    scrubbed = mask.scrub(payload)
    leaks = mask.find_leaks(scrubbed)
    return scrubbed, (not leaks), leaks


def all_bill_nos(include_demo=True):
    sql = "SELECT bill_no FROM expense_bill"
    if not include_demo:
        sql += " WHERE NOT is_demo"
    return [r["bill_no"] for r in db.query(sql + " ORDER BY doc_date, bill_no")]


def standards(instance_id=None):
    """限额标准。**按实例取**：住宿费标准这类数字是各单位自己的制度定的，
    拿另一个实例的限额判这个实例的单据，会直接判错超标与否。"""
    from .. import scope
    iid = scope.require(instance_id)
    return db.query("""SELECT std_type, tier, region, limit_amount, unit,
                              confidence, cross_checked, source_clause
                       FROM standard_limit WHERE instance_id = %s
                       ORDER BY std_type, region""", (iid,))


def tier_map(instance_id=None):
    """职级↔档位映射。按实例取，理由同 standards()——职级体系各单位不一样。"""
    from .. import scope
    iid = scope.require(instance_id)
    return db.query("""SELECT subject, rank_level, tier, on_unknown, clause, confidence
                       FROM rank_tier_map WHERE instance_id = %s ORDER BY id""", (iid,))
