# -*- coding: utf-8 -*-
"""16 条执行集规则的实现。一条规则一个函数，用 @rule 注册。

为什么是手写实现、不是通用表达式求值器：
  规则清单里的 `logic.expr` 是给人读的中文表达式
  （如「费用明细.住宿费金额 <= 标准表.accommodation.rates[出差地].other × 实际住宿天数」）。
  做一个能吃这种表达式的求值器，就等于自己发明一套 DSL，
  而 DSL 的坑在于：写得出来的表达式总比求值器支持的多，
  求值器不支持的部分会被静默忽略——那正是最危险的失败方式。
  手写实现换来的是：每条规则的判定逻辑、证据构造、例外出口都摆在明处，
  可以逐条 review，而且判不了的时候能说出判不了的具体原因。

每个实现都遵守三条：
  1. 判不了就返回 pending，**绝不返回 pass**
  2. 命中时 evidence 要让人能自己复核一遍（给出实际值、标准值、出处条款）
  3. 例外出口（补充说明、审批记录）要显式检查，不能漏——
     漏掉例外就会对正常单据大面积误报
"""
from . import predicates as P
from .engine import hit, not_applicable, ok, pending, rule

MEAL_DAILY = 100.0          # 省内与省外其他省份都是 100；西藏青海新疆 120
MEAL_HIGH = 120.0
LOCAL_TRANSPORT_DAILY = 80.0


def _travel_day_count(ctx, fact):
    """**累计出差日数**，不是单次连续出差天数。两者不能混。

    伙食补助按自然天数包干计发，所以 总额 ÷ 日标准 = 累计出差日数。
    但「累计 3 个出差日」可能是**一次 3 天的出差**，也可能是**3 次当天往返**。

    实测这个区别造成过 9 条误报：
      FJPG-BX2607260052 摘要「6月17日莆田港务业财培训、6月24日闽运福清驻点辅导、
      6月30日莆田港务共享培训」—— 三次不同日期的当天往返。
      伙食补助 300 = 3 天 × 100、市内交通 240 = 3 天 × 80，完全吻合。
      没有住宿费是完全正常的，而 TR-D6-03「出差 2 天以上需有住宿费发票」
      按累计日数判就报了违规。人工最终也是「同意」。

    所以返回值是 (累计日数, 原因, 来源, 是否为单次连续)。
    最后那一项只有 OA 出差审批单给出起止日期时才为 True——
    靠累计日数永远推不出它。
    """
    trip = fact.get("trip") or {}
    if trip.get("synced_from_oa") and trip.get("days"):
        return int(trip["days"]), None, "OA 出差审批单（含起止日期）", True
    meal = P.sum_of(fact, ["伙食补助费"])
    d, why = P.days_from_meal_allowance(meal, MEAL_DAILY)
    if d is None:
        return None, why, None, False
    return d, None, "由伙食补助费 %s ÷ %s 倒算（**累计出差日数**）" % (meal, int(MEAL_DAILY)), False


def _days(ctx, fact):
    """兼容旧签名：只返回累计日数。用它的规则必须自己确认「累计」够用。

    伙食补助、市内交通补贴这类**按自然天数计发**的，累计日数正是它们的基数，
    用这个没问题。而「出差 2 天以上需有住宿费」要的是单次连续天数，不能用。
    """
    d, why, how, _ = _travel_day_count(ctx, fact)
    return d, why, how


# ─────────────────────────────────────────────────────────────────────────────
# D1 主体
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D1-02")
def tr_d1_02(ctx, r):
    """住宿费发票抬头须为本单的核算主体。

    **抬头与核算主体都做规范化再比**：发票 OCR 出来的抬头常带空格、
    全角括号、「有限公司」前后的空白，直接字符串相等会把合规单判成违规。
    但规范化只去空白，不做模糊匹配——「福建海运集团」与
    「福建海运集团物流分公司」是两个主体，混判就等于放过了抬头开错的单。
    """
    hotel_items = ["住宿费", "住宿费（参加会议产生）"]
    rows = P.details_of(ctx.fact, hotel_items)
    if not rows:
        return not_applicable(r["rule_id"], "本单没有住宿费明细")
    org_name = (ctx.fact.get("org") or {}).get("name")
    if not P.nonempty(org_name):
        return pending(r["rule_id"], "本单取不到核算主体名称，无法核对发票抬头")
    # 住宿类发票：有 hotel_invoice 明细的优先，否则按发票种类兜
    hotel_ids = {h.get("invoice_id") for h in ctx.fact.get("hotel_invoices") or []}
    invs = [i for i in ctx.fact.get("invoices") or []
            if i.get("invoice_id") in hotel_ids
            or "住宿" in str(i.get("inv_type") or "")]
    if not invs:
        return pending(r["rule_id"],
                       "本单有住宿费 %s 元，但没有可核对的住宿发票记录（票据未 OCR 入库）"
                       % P.sum_of(ctx.fact, hotel_items),
                       {"核算主体": org_name})
    want = P.norm_strip_space(org_name)
    bad, missing = [], []
    for i in invs:
        title = i.get("title")
        if not P.nonempty(title):
            missing.append(i.get("inv_no") or i.get("invoice_id"))
        elif P.norm_strip_space(title) != want:
            bad.append({"发票号": i.get("inv_no") or i.get("invoice_id"), "抬头": title})
    ev = {"核算主体": org_name, "住宿发票张数": len(invs),
          "抬头不符": bad, "抬头为空": missing,
          "出处": "规则表「住宿费发票抬头为核算主体」"}
    if bad:
        return hit(r["rule_id"],
                   "住宿发票抬头与核算主体「%s」不一致：%s"
                   % (org_name, "、".join("%s（%s）" % (b["发票号"], b["抬头"]) for b in bad)),
                   ev)
    if missing:
        return pending(r["rule_id"],
                       "有 %d 张住宿发票取不到抬头，无法核对" % len(missing), ev)
    return ok(r["rule_id"], "%d 张住宿发票抬头均为核算主体「%s」" % (len(invs), org_name), ev)


@rule("TR-D1-03")
def tr_d1_03(ctx, r):
    """本人自行提单；代提单需共享系统授权。"""
    bill = ctx.fact["bill"]
    submitter, claimant = bill.get("submitter_no"), bill.get("claimant_no")
    if claimant is None:
        return pending(r["rule_id"],
                       "本单没有「报账人」字段（多人导出缺这一列，缺口 G-32），"
                       "无法判断是否本人提单",
                       {"提单人": submitter})
    if submitter == claimant:
        return ok(r["rule_id"], "本人自行提单", {"提单人": submitter, "报账人": claimant})
    # 代提单：授权记录不在样本里，不能据此判违规
    auth = ctx.fact.get("delegation_authorized")
    if auth is True:
        return ok(r["rule_id"], "代提单且已在共享系统授权",
                  {"提单人": submitter, "报账人": claimant})
    # 单位特殊授权（闽运集团郯筠和、八方物流）由 TR-D10-01/02 单独处理，这里只记事实
    return pending(r["rule_id"],
                   "提单人与报账人不一致，而共享系统的代提单授权记录不在本次数据范围内，"
                   "无法确认是否已授权",
                   {"提单人": submitter, "报账人": claimant,
                    "说明": "样本 40 张里提单人≠报账人的有 5 张，全部是「公司领导」被代提单"})


# ─────────────────────────────────────────────────────────────────────────────
# D3 发票敏感内容
# ─────────────────────────────────────────────────────────────────────────────
def _sensitive_scan(ctx, r, group, field, field_label, subject, advice):
    """在发票的某个字段上扫敏感词表的某一组。

    **停用的词条不扫**（enabled=False 是「推断出来的外延，未经业务确认」），
    所以证据里要把本次实际启用的词条列出来——
    否则「没命中」看不出是真没有，还是那条词根本没启用。
    """
    rows = [x for x in ctx.enabled_rows("敏感词表")
            if (x["row_key"] or {}).get("group") == group]
    if not rows:
        return pending(r["rule_id"], "敏感词表里 %s 这一组没有启用的词条" % group)
    invs = subject(ctx.fact)
    terms = [(x["row_key"] or {}).get("term") for x in rows]
    if not invs:
        return not_applicable(r["rule_id"], "本单没有可扫描的发票记录",
                              {"启用词条": terms})
    scanned, blank = 0, 0
    for i in invs:
        text = i.get(field)
        if not P.nonempty(text):
            blank += 1
            continue
        scanned += 1
        for row in rows:
            entry = dict(row["row_value"] or {})
            entry["term"] = (row["row_key"] or {}).get("term")
            h, frag = P.keyword_hit(entry, text)
            if h:
                return hit(r["rule_id"],
                           "%s「%s」命中敏感词「%s」（%s）——%s"
                           % (field_label, text, entry["term"],
                              entry.get("category") or "", advice),
                           {"发票号": i.get("inv_no") or i.get("invoice_id"),
                            field_label: text, "命中词": entry["term"],
                            "词条类别": entry.get("category"),
                            "启用词条": terms,
                            "出处": (r.get("source_clause") or {}).get("clause")},
                           matched_text=frag, needs_human=True)
    ev = {"扫描发票张数": scanned, "%s为空" % field_label: blank, "启用词条": terms}
    if scanned == 0:
        return pending(r["rule_id"],
                       "%d 张发票的%s都为空，扫不出结果（票据未 OCR 入库）"
                       % (blank, field_label), ev)
    if blank:
        return pending(r["rule_id"],
                       "已扫 %d 张发票未命中敏感词，但还有 %d 张的%s为空，不能确认整单干净"
                       % (scanned, blank, field_label), ev)
    return ok(r["rule_id"], "%d 张发票的%s均未命中敏感词" % (scanned, field_label), ev)


@rule("TR-D3-01")
def tr_d3_01(ctx, r):
    """住宿发票的开票单位不得是娱乐场所、旅游景点。"""
    return _sensitive_scan(
        ctx, r, "sensitive_issuer_keywords", "issuer", "开票单位",
        lambda f: f.get("invoices") or [],
        "非增值税类发票的开票单位不能包含娱乐活动场所、旅游景点")


@rule("TR-D3-02")
def tr_d3_02(ctx, r):
    """发票明细不得包含洗浴等敏感消费项。"""
    return _sensitive_scan(
        ctx, r, "sensitive_item_keywords", "detail_text", "发票明细",
        lambda f: f.get("invoices") or [],
        "发票明细不能包含敏感消费内容")


@rule("TR-D3-08")
def tr_d3_08(ctx, r):
    """2025-09-30 后乘坐的纸质铁路车票不可报销入账和抵扣进项税额。

    凭证形式（纸质/电子）落在 invoice.is_eticket 上，按 voucher.invoice_id
    关联过去取——**取不到就转人工，不当「电子」放过，也不当「纸质」硬判**。
    """
    import datetime
    vouchers = ctx.fact.get("transport_vouchers") or []
    if not vouchers:
        return not_applicable(r["rule_id"], "本单没有交通费凭证记录")
    inv_by_id = {i.get("invoice_id"): i for i in ctx.fact.get("invoices") or []}
    cutoff = datetime.date(2025, 9, 30)
    train_kw = ("火车", "动车", "高铁")
    bad, unknown = [], []
    for v in vouchers:
        vehicle = v.get("vehicle")
        ride_date = v.get("ride_date")
        # 交通工具取不到时**不能当「反正不是火车」跳过**——除非乘车日期
        # 明确在截止日之前，那不管是不是火车都不犯规。
        if vehicle is None:
            if ride_date and ride_date <= cutoff:
                continue
            unknown.append((v, "交通工具取不到"))
            continue
        if not any(k in vehicle for k in train_kw):
            continue
        if ride_date is None:
            unknown.append((v, "是火车/动车/高铁，但乘车日期取不到"))
            continue
        if ride_date <= cutoff:
            continue
        inv = inv_by_id.get(v.get("invoice_id")) or {}
        is_e = inv.get("is_eticket")
        if is_e is False:
            bad.append(v)
        elif is_e is None:
            unknown.append((v, "凭证形式（纸质/电子）取不到"))
    ev = {"交通凭证张数": len(vouchers),
          "2025-09-30后的纸质铁路票": [v.get("voucher_id") for v in bad],
          "判不出能否排除": [(v.get("voucher_id"), why) for v, why in unknown],
          "出处": (r.get("source_clause") or {}).get("clause")}
    if bad:
        return hit(r["rule_id"],
                   "存在 2025-09-30 后乘坐的纸质铁路车票 %d 张，不可报销入账和抵扣进项税额"
                   % len(bad), ev)
    if unknown:
        return pending(r["rule_id"],
                       "有 %d 张交通凭证无法排除是 2025-09-30 后的纸质铁路车票：%s"
                       % (len(unknown), "；".join(why for _, why in unknown)), ev)
    return ok(r["rule_id"], "没有 2025-09-30 后的纸质铁路车票", ev)


# ─────────────────────────────────────────────────────────────────────────────
# D4 标准限额
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D4-02")
def tr_d4_02(ctx, r):
    """住宿费标准控制。**区间判定**：缺出差地点与住宿天数，见 logic.degraded。"""
    total = P.sum_of(ctx.fact, ["住宿费", "住宿费（参加会议产生）"])
    if total is None:
        return not_applicable(r["rule_id"], "本单没有住宿费明细")
    days, why, how = _days(ctx, ctx.fact)
    if days is None:
        return pending(r["rule_id"], "住宿费 %s 元，但出差天数算不出来：%s" % (total, why),
                       {"住宿费合计": total})
    deg = (r["logic"] or {}).get("degraded") or {}
    lower, upper = deg.get("lower"), deg.get("upper")
    per_day = total / days
    # 档位链：主体 × 职级 → 档位。职级取不到时 tier 为 None，此时只能走区间判定。
    org_name = (ctx.fact.get("org") or {}).get("name") or ""
    emp = ctx.fact.get("employee_claimant") or ctx.fact.get("employee_submitter") or {}
    tier, unk = ctx.tier_for(org_name, emp.get("rank_level"))
    ev = {"住宿费合计": round(total, 2), "出差天数": days, "天数来源": how,
          "日均": round(per_day, 2), "档位": tier or "未知（职级取不到）",
          "区间下界": lower, "区间上界": upper,
          "区间依据": "已录入 15 行费率里最低 %s、最高 %s" % (lower, upper)}
    v = P.interval_verdict(per_day, lower, upper)
    if v is True:
        return hit(r["rule_id"],
                   "住宿费日均 %.2f 元超过已录入费率的最高档 %s 元——"
                   "不论出差地点与职级，均已超标准" % (per_day, upper), ev)
    if v is False:
        return ok(r["rule_id"],
                  "住宿费日均 %.2f 元不超过最低档 %s 元，任何地区任何档位都不超标"
                  % (per_day, lower), ev)
    return pending(r["rule_id"],
                   "住宿费日均 %.2f 元落在 %s~%s 元之间，取哪一档取决于出差地点"
                   "（需 OA 同步，缺口 G-04）%s" % (
                       per_day, lower, upper,
                       "与职级（%s）" % unk if unk else ""), ev)


@rule("TR-D4-03")
def tr_d4_03(ctx, r):
    """出差补贴标准控制。composite：汇总 TR-D4-04 / TR-D4-05 的结论。"""
    return _composite(ctx, r, "补贴标准")


@rule("TR-D4-04")
def tr_d4_04(ctx, r):
    """伙食补助费标准。**区间判定**：100 与 120 两档，取哪档要看出差地点。"""
    total = P.sum_of(ctx.fact, ["伙食补助费"])
    if total is None:
        return not_applicable(r["rule_id"], "本单没有伙食补助费明细")
    days, why, how = _days(ctx, ctx.fact)
    if days is None:
        return pending(r["rule_id"], "伙食补助费 %s 元，但天数算不出来：%s" % (total, why),
                       {"伙食补助费": total})
    persons = _person_count(ctx.fact)
    deg = (r["logic"] or {}).get("degraded") or {}
    lower, upper = deg.get("lower", MEAL_DAILY), deg.get("upper", MEAL_HIGH)
    per = total / (days * max(persons, 1))
    ev = {"伙食补助费": round(total, 2), "出差天数": days, "天数来源": how,
          "人数": persons, "人日均": round(per, 2),
          "省内/省外其他": lower, "西藏青海新疆": upper,
          "出处": "细则第十七条（已与样本交叉印证：100/200/300/400/600/900 全为 100 的整数倍）"}
    v = P.interval_verdict(per, lower, upper)
    if v is True:
        return hit(r["rule_id"],
                   "伙食补助人日均 %.2f 元超过最高档 %s 元（西藏青海新疆档），已超标准"
                   % (per, upper), ev)
    if v is False:
        return ok(r["rule_id"], "伙食补助人日均 %.2f 元不超过 %s 元标准" % (per, lower), ev)
    return pending(r["rule_id"],
                   "伙食补助人日均 %.2f 元落在 %s~%s 之间，要看出差地是否属"
                   "西藏/青海/新疆（出差地点需 OA 同步，缺口 G-04）" % (per, lower, upper), ev)


@rule("TR-D4-06")
def tr_d4_06(ctx, r):
    """超标准住宿需书面说明并经分管领导和总经理审批。

    **前置是 TR-D4-02 命中。** TR-D4-02 没命中（或判不了）时本条不适用——
    对没超标的单子要求「超标说明」是纯误报。
    """
    nodes = {a.get("node") for a in ctx.fact.get("approvals") or []}
    prev = ctx.fact.get("_findings_by_rule", {}).get("TR-D4-02")
    if prev is None:
        return pending(r["rule_id"], "本条依赖 TR-D4-02 的结论，而它还没跑")
    if prev.outcome == "pending":
        return pending(r["rule_id"], "TR-D4-02 判不了是否超标，本条无从判起")
    if prev.outcome != "hit":
        return not_applicable(r["rule_id"], "住宿费未超标准，无需超标说明")
    remark = ctx.fact["bill"].get("remark")
    has_note = P.nonempty(remark)
    has_vp = any("分管领导" in (n or "") for n in nodes)
    has_gm = any(("总经理" in (n or "")) for n in nodes)
    ev = {"补充说明非空": has_note, "审批节点": sorted(n for n in nodes if n),
          "含分管领导": has_vp, "含总经理": has_gm,
          "出处": "细则第十五条 + 附件2 六种特殊情形"}
    if not has_note:
        return hit(r["rule_id"], "住宿费超标准但补充说明为空", ev)
    if not (has_vp and has_gm):
        return hit(r["rule_id"],
                   "住宿费超标准且有书面说明，但审批链里缺%s"
                   % ("分管领导" if not has_vp else "总经理"), ev)
    return ok(r["rule_id"], "超标准住宿已有书面说明并经分管领导与总经理审批", ev)


@rule("TR-D4-13")
def tr_d4_13(ctx, r):
    """闽货、饮食服务公司无交通补贴，按凭据实报实销。"""
    subsidy = P.sum_of(ctx.fact, ["市内交通费补贴", "市内交通补贴"])
    transport = P.sum_of(ctx.fact, ["交通费"])
    days, why, how = _days(ctx, ctx.fact)
    dept = ctx.fact["bill"].get("claim_dept") or ""
    cap_per_day = 80.0 if "流通事业部" in dept else 50.0
    ev = {"市内交通补贴": subsidy, "交通费": transport, "报账部门": dept,
          "日上限": cap_per_day, "出差天数": days, "天数来源": how,
          "出处": "审核要点 2.3.2.6"}
    if subsidy:
        return hit(r["rule_id"],
                   "本单位不应有市内交通补贴，实际有 %s 元（应从交通费科目凭票列支）"
                   % subsidy, ev)
    if transport is None:
        return ok(r["rule_id"], "无市内交通补贴，且本单无交通费明细", ev)
    if days is None:
        return pending(r["rule_id"], "交通费 %s 元，但天数算不出来无法算上限：%s"
                       % (transport, why), ev)
    cap = cap_per_day * days
    ev["上限合计"] = cap
    if transport > cap:
        return hit(r["rule_id"], "交通费 %s 元超过上限 %s 元（%s 元/天 × %s 天）"
                   % (transport, cap, cap_per_day, days), ev)
    return ok(r["rule_id"], "无市内交通补贴，交通费 %s 元在上限 %s 元内" % (transport, cap), ev)


def _trip_place(fact):
    """出差地点。OA 出差审批单给得出就用它，否则为 None（缺口 G-04）。

    **不拿摘要去猜地名。** 摘要是自由文本，「福州港务培训」里的「福州」
    可能是主办方名字的一部分而不是出差地，猜错会直接改掉适用档位。
    """
    trip = fact.get("trip") or {}
    place = trip.get("place")
    return place if P.nonempty(place) else None


@rule("TR-D4-09")
def tr_d4_09(ctx, r):
    """闽运集团市内交通费包干：地级市外 80、跨县 50，在榕当天往返特定地点为 0。

    **三档都取决于出差地点**，而出差地点要 OA 同步（缺口 G-04）。
    取不到时只能按上界 80 判「必定超标」的那一侧，其余转人工——
    按 80 判会放过本该 50 或 0 的（漏报），按 0 判会把正常的全报成违规（误报）。
    """
    total = P.sum_of(ctx.fact, ["市内交通费补贴", "市内交通补贴"])
    if total is None:
        return not_applicable(r["rule_id"], "本单没有市内交通费补贴明细")
    days, why, how = _days(ctx, ctx.fact)
    if days is None:
        return pending(r["rule_id"],
                       "市内交通费补贴 %s 元，但天数算不出来：%s" % (total, why),
                       {"市内交通费补贴": total})
    persons = _person_count(ctx.fact)
    per = total / (days * max(persons, 1))
    place = _trip_place(ctx.fact)
    zero_places = ["闽侯", "连江贵安", "长乐机场"]
    ev = {"市内交通费补贴": round(total, 2), "出差天数": days, "天数来源": how,
          "人数": persons, "人日均": round(per, 2),
          "地级市外标准": 80, "跨县标准": 50,
          "不发放的情形": "在榕人员当天往返 " + "、".join(zero_places),
          "出差地点": place or "取不到（需 OA 同步，缺口 G-04）",
          "出处": "审核要点 2.3.1 表第 3 行"}
    if place and any(z in place for z in zero_places):
        if total > 0:
            return hit(r["rule_id"],
                       "出差地「%s」属在榕人员当天往返不发放市内交通费的情形，"
                       "本单却有市内交通费补贴 %s 元" % (place, total), ev)
        return ok(r["rule_id"], "属不发放情形且本单市内交通费为 0", ev)
    v = P.interval_verdict(per, 50.0, 80.0)
    if v is True:
        return hit(r["rule_id"],
                   "市内交通费人日均 %.2f 元超过最高档 80 元/人·天（地级市外），"
                   "不论出差地点都已超包干标准" % per, ev)
    if v is False:
        return ok(r["rule_id"],
                  "市内交通费人日均 %.2f 元不超过最低档 50 元/人·天，任何情形都不超标" % per, ev)
    return pending(r["rule_id"],
                   "市内交通费人日均 %.2f 元落在 50~80 元之间，取哪一档要看出差地"
                   "是否在常驻地地级市外（需 OA 同步，缺口 G-04）" % per, ev)


@rule("TR-D4-10")
def tr_d4_10(ctx, r):
    """漳州港务：古雷港经济开发区内公务不属出差；当天往返周边不报三项费用。"""
    place = _trip_place(ctx.fact)
    hotel = P.sum_of(ctx.fact, ["住宿费", "住宿费（参加会议产生）"])
    meal = P.sum_of(ctx.fact, ["伙食补助费"])
    local = P.sum_of(ctx.fact, ["市内交通费补贴", "市内交通补贴"])
    near = ["漳浦", "云霄", "东山", "厦门机场", "厦门动车站"]
    charged = {k: v for k, v in
               (("住宿费", hotel), ("伙食补助费", meal), ("市内交通费", local)) if v}
    ev = {"出差地点": place or "取不到（需 OA 同步，缺口 G-04）",
          "常驻地": "漳州市古雷港经济开发区",
          "当天往返不报销的周边": "、".join(near),
          "本单三项费用": charged,
          "出处": "审核要点 2.3.1 表第 4 行"}
    if place is None:
        if not charged:
            return ok(r["rule_id"], "本单没有住宿费、伙食补助、市内交通费，无论出差地为何都不违规", ev)
        return pending(r["rule_id"],
                       "本单有 %s，但出差地点取不到——"
                       "若在古雷港经济开发区内则整单不成立，若为当天往返周边则这三项不予报销"
                       % "、".join("%s %s 元" % (k, v) for k, v in charged.items()), ev)
    if "古雷" in place:
        return hit(r["rule_id"],
                   "出差地「%s」在常驻地古雷港经济开发区内，该区域内公务不属于出差，"
                   "整单不成立" % place, ev)
    if any(n in place for n in near):
        if charged:
            return hit(r["rule_id"],
                       "出差地「%s」属当天往返周边，不报销住宿费、伙食补助、市内交通费，"
                       "本单却有 %s" % (place, "、".join(
                           "%s %s 元" % (k, v) for k, v in charged.items())), ev)
        return ok(r["rule_id"], "出差地属当天往返周边，且本单未报销这三项费用", ev)
    return ok(r["rule_id"], "出差地「%s」不在古雷港区内，也不属当天往返周边" % place, ev)


@rule("TR-D4-11")
def tr_d4_11(ctx, r):
    """泉州港务补助特殊标准。**五档都取决于出差地点**，缺地点只能判上界那一侧。

    中心城区 0、市域外当日往返 50、省内其他设区市 100、省外一般 100、
    西藏青海新疆 120。取不到出差地时按 120 为上界：
    超过 120 的必定超标（任何一档都超），其余转人工。
    """
    meal = P.sum_of(ctx.fact, ["伙食补助费"])
    if meal is None:
        return not_applicable(r["rule_id"], "本单没有伙食补助费明细")
    days, why, how = _days(ctx, ctx.fact)
    if days is None:
        return pending(r["rule_id"], "伙食补助费 %s 元，但天数算不出来：%s" % (meal, why),
                       {"伙食补助费": meal})
    persons = _person_count(ctx.fact)
    per = meal / (days * max(persons, 1))
    place = _trip_place(ctx.fact)
    ev = {"伙食补助费": round(meal, 2), "出差天数": days, "天数来源": how,
          "人数": persons, "人日均": round(per, 2),
          "档位": {"泉州中心城区": 0, "市域中心城区外": 50,
                   "省内其他设区市": 100, "省外一般": 100, "西藏青海新疆": 120},
          "出差地点": place or "取不到（需 OA 同步，缺口 G-04）",
          "例外": "接受接待的不发伙食补助",
          "出处": "审核要点 2.3.1 表（泉州港务）"}
    v = P.interval_verdict(per, 0.0, 120.0)
    if v is True:
        return hit(r["rule_id"],
                   "伙食补助人日均 %.2f 元超过泉州港务最高档 120 元/人·天"
                   "（西藏青海新疆档），不论出差地都已超标" % per, ev)
    return pending(r["rule_id"],
                   "伙食补助人日均 %.2f 元，而泉州港务的档位从 0（中心城区）到 120 不等，"
                   "取哪一档完全取决于出差地点（需 OA 同步，缺口 G-04）" % per, ev)


@rule("TR-D4-12")
def tr_d4_12(ctx, r):
    """闽深公司无交通补贴，按凭据实报实销（日上限 80）；出租车发票不抵税。

    **出租车发票的判定靠 vehicle（交通工具，读图可得）或 inv_type 两路认**——
    2026-10-22 起读图抽取只填了 vehicle，没填 inv_type，只认 inv_type 会让这一支
    永远认不出出租车发票。税额（tax_amount）本轮不抽取，**取不到不能当成「没抵税」
    直接放过**：有出租车发票但税额未知时转人工，不能把「不知道」当「合规」。
    """
    subsidy = P.sum_of(ctx.fact, ["市内交通费补贴", "市内交通补贴"])
    transport = P.sum_of(ctx.fact, ["交通费"])
    days, why, how = _days(ctx, ctx.fact)
    taxi_invoices = [i for i in ctx.fact.get("invoices") or []
                     if "出租" in str(i.get("inv_type") or "")
                     or "出租" in str(i.get("vehicle") or "")]
    taxi_taxed = [i for i in taxi_invoices if (P.as_num(i.get("tax_amount")) or 0) > 0]
    taxi_unknown_tax = [i for i in taxi_invoices
                        if i not in taxi_taxed and i.get("tax_amount") is None]
    ev = {"市内交通补贴": subsidy, "交通费": transport,
          "日上限": LOCAL_TRANSPORT_DAILY, "出差天数": days, "天数来源": how,
          "有税额的出租车发票": [i.get("inv_no") or i.get("invoice_id") for i in taxi_taxed],
          "税额未知的出租车发票": [i.get("inv_no") or i.get("invoice_id")
                            for i in taxi_unknown_tax],
          "出处": "审核要点 2.3.2.5"}
    if subsidy:
        return hit(r["rule_id"],
                   "闽深公司差旅费无交通补贴，本单却有市内交通补贴 %s 元"
                   "（应从交通费科目凭票实报实销）" % subsidy, ev)
    if taxi_taxed:
        return hit(r["rule_id"],
                   "出租车发票不抵税，但有 %d 张出租车发票带了税额" % len(taxi_taxed), ev)
    if taxi_unknown_tax:
        return pending(r["rule_id"],
                       "本单有 %d 张出租车发票，但税额未读入（本轮不抽取金额字段），"
                       "无法确认是否违规抵税" % len(taxi_unknown_tax), ev)
    if transport is None:
        return ok(r["rule_id"], "无市内交通补贴，且本单无交通费明细", ev)
    if days is None:
        return pending(r["rule_id"],
                       "交通费 %s 元，但天数算不出来无法算上限：%s" % (transport, why), ev)
    cap = LOCAL_TRANSPORT_DAILY * days
    ev["上限合计"] = cap
    if transport > cap:
        return hit(r["rule_id"],
                   "交通费 %s 元超过上限 %s 元（%s 元/天 × %s 天）"
                   % (transport, cap, int(LOCAL_TRANSPORT_DAILY), days), ev)
    return ok(r["rule_id"], "无市内交通补贴，交通费 %s 元在上限 %s 元内" % (transport, cap), ev)


@rule("TR-D4-14")
def tr_d4_14(ctx, r):
    """拖轮公司增员（外派）差旅费：住宿 200 元/人·晚，伙食 50 元/人·天。

    **住宿按「晚」算，不是按「天」。** 住 3 晚的行程是 4 天，
    拿天数当晚数会把 600 元的合规住宿算成 800 元上限而放过超标的。
    晚数取自住宿发票的住宿明细（C0604）；取不到时不拿天数顶替。
    """
    hotel = P.sum_of(ctx.fact, ["住宿费", "住宿费（参加会议产生）"])
    meal = P.sum_of(ctx.fact, ["伙食补助费"])
    if hotel is None and meal is None:
        return not_applicable(r["rule_id"], "本单既无住宿费也无伙食补助费明细")
    persons = _person_count(ctx.fact)
    nights = sum(int(h["nights"]) for h in ctx.fact.get("hotel_invoices") or []
                 if h.get("nights"))
    days, why, how = _days(ctx, ctx.fact)
    ev = {"住宿费": hotel, "伙食补助费": meal, "人数": persons,
          "住宿晚数": nights or "取不到（住宿发票未录住宿明细）",
          "出差天数": days, "天数来源": how,
          "住宿标准": "200 元/人·晚", "伙食标准": "50 元/人·天",
          "出处": "审核要点 2.2.2（临时调派、增员工作期间费用报销管理办法）"}
    if hotel:
        if not nights:
            return pending(r["rule_id"],
                           "住宿费 %s 元，但住宿晚数取不到（住宿发票没有住宿明细），"
                           "不能拿出差天数顶替——住 N 晚对应 N+1 天，顶替会把上限算高一档"
                           % hotel, ev)
        cap = 200.0 * nights * max(persons, 1)
        ev["住宿上限"] = cap
        if hotel > cap:
            return hit(r["rule_id"],
                       "住宿费 %s 元超过上限 %s 元（200 元/人·晚 × %s 晚 × %s 人）"
                       % (hotel, cap, nights, persons), ev)
    if meal:
        if days is None:
            return pending(r["rule_id"],
                           "伙食补助费 %s 元，但天数算不出来：%s" % (meal, why), ev)
        mcap = 50.0 * days * max(persons, 1)
        ev["伙食上限"] = mcap
        if meal > mcap:
            return hit(r["rule_id"],
                       "伙食补贴 %s 元超过上限 %s 元（50 元/人·天 × %s 天 × %s 人）"
                       % (meal, mcap, days, persons), ev)
    return ok(r["rule_id"], "住宿费与伙食补贴均在增员差旅费标准内", ev)


# ─────────────────────────────────────────────────────────────────────────────
# D5 补贴口径
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D5-10")
def tr_d5_10(ctx, r):
    """工会疗休养不从差旅费列支。关键词型，作用在摘要与付款用途上。"""
    bill = ctx.fact["bill"]
    rows = [x for x in ctx.enabled_rows("敏感词表")
            if (x["row_key"] or {}).get("group") == "excluded_purpose_keywords"]
    if not rows:
        return pending(r["rule_id"], "敏感词表里没有启用的「不得列支」词条")
    for field, label in (("summary", "摘要"), ("pay_purpose", "付款用途")):
        text = bill.get(field)
        for row in rows:
            entry = dict(row["row_value"] or {})
            entry["term"] = (row["row_key"] or {}).get("term")
            h, frag = P.keyword_hit(entry, text)
            if h:
                return hit(r["rule_id"],
                           "%s 命中「%s」——疗休养费用应在行政福利费列支或由工会账户支付，"
                           "不从差旅费科目列支" % (label, entry["term"]),
                           {"字段": label, "命中词": entry["term"],
                            "出处": "审核要点 2.1.19"},
                           matched_text=frag)
    return ok(r["rule_id"], "摘要与付款用途均未出现「疗休养」",
              {"检查字段": ["摘要", "付款用途"],
               "启用词条": [(x["row_key"] or {}).get("term") for x in rows]})


# ─────────────────────────────────────────────────────────────────────────────
# D6 附件
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D6-03")
def tr_d6_03(ctx, r):
    """出差 2 天以上需有住宿费发票，无则需说明。

    **这条规则要的是「单次连续出差 2 天以上」，不是「累计 2 个出差日」。**
    单次起止日期在 OA 出差审批单里（C0302/C0303，需 OA 同步，缺口 G-04）。
    拿累计日数代替会产生误报，实测过：3 次当天往返累计 3 日，
    按累计判就报「缺住宿费发票」，而人工放行。
    """
    days, why, how, is_single = _travel_day_count(ctx, ctx.fact)
    if days is None:
        return pending(r["rule_id"], "出差天数算不出来：%s" % why)
    if not is_single:
        return pending(r["rule_id"],
                       "只能算出累计 %s 个出差日（%s），但本条要的是**单次连续**出差天数。"
                       "累计 %s 日既可能是一次 %s 天的出差，也可能是 %s 次当天往返——"
                       "后者本来就不该有住宿费。单次起止日期在 OA 出差审批单里，"
                       "尚未同步（缺口 G-04）。"
                       % (days, how, days, days, days),
                       {"累计出差日数": days, "天数来源": how,
                        "住宿费明细": P.sum_of(ctx.fact, ["住宿费", "住宿费（参加会议产生）"]),
                        "摘要": ctx.fact["bill"].get("summary")})
    if days < 2:
        return not_applicable(r["rule_id"], "单次连续出差 %s 天，未达 2 天" % days)
    hotel = P.sum_of(ctx.fact, ["住宿费", "住宿费（参加会议产生）"])
    remark = ctx.fact["bill"].get("remark")
    ev = {"出差天数": days, "天数来源": how, "住宿费明细": hotel,
          "补充说明非空": P.nonempty(remark),
          "出处": "29 条规则表第 14 条 + 制度第二十三条（三）"}
    if hotel:
        return ok(r["rule_id"], "出差 %s 天且有住宿费 %s 元" % (days, hotel), ev)
    if P.nonempty(remark):
        # 说明「是否成立」是语义判断，交 TR-D9-01 软规则；本条只确认有说明
        return ok(r["rule_id"],
                  "出差 %s 天无住宿费明细，但已有补充说明（说明是否成立交 TR-D9-01 研判）"
                  % days, ev)
    return hit(r["rule_id"],
               "出差 %s 天（%s）却没有住宿费明细，且补充说明为空" % (days, how), ev)


@rule("TR-D6-05")
def tr_d6_05(ctx, r):
    """交通费报销凭证需包含往返行程；仅单程的需有情况说明。

    **外派差旅费已由 scope 的 excluded_biz_type 排除**（审核要点 2.2.1
    白纸黑字取消了这项要求），不在这里再判一次。

    判「往返」的三条线索，按可靠度排：
      ① 凭证自带 has_round_trip 标记
      ② 存在两张方向相反的凭证（A→B 与 B→A）
      ③ 只有单程 → 看补充说明是否给了情况说明
    """
    transport = P.sum_of(ctx.fact, ["交通费"])
    if transport is None:
        return not_applicable(r["rule_id"], "本单没有交通费明细")
    vouchers = ctx.fact.get("transport_vouchers") or []
    remark = ctx.fact["bill"].get("remark")
    has_note = P.nonempty(remark)
    ev = {"交通费": transport, "交通凭证张数": len(vouchers),
          "补充说明非空": has_note,
          "例外": "外派差旅费取消本要求（2.2.1，已由 scope 排除）；"
                  "仅上传单程的需提供情况说明",
          "出处": "审核要点 2.1.4"}
    if not vouchers:
        if has_note:
            return ok(r["rule_id"],
                      "交通费 %s 元但没有交通凭证记录，已有补充说明" % transport, ev)
        return pending(r["rule_id"],
                       "交通费 %s 元，但本单没有交通凭证记录（票据未 OCR 入库），"
                       "无法判断是否含往返行程" % transport, ev)
    legs = [(P.norm_strip_space(v.get("from_place")),
             P.norm_strip_space(v.get("to_place"))) for v in vouchers]
    ev["行程"] = ["%s→%s" % (a or "?", b or "?") for a, b in legs]
    if any(v.get("has_round_trip") for v in vouchers):
        return ok(r["rule_id"], "交通凭证已标明含往返行程", ev)
    paired = any(a and b and (b, a) in legs for a, b in legs)
    if paired:
        return ok(r["rule_id"], "交通凭证中存在方向相反的两段，构成往返行程", ev)
    if any(not a or not b for a, b in legs):
        return pending(r["rule_id"],
                       "有凭证的起止地点为空，判不出是否构成往返行程", ev)
    # **站名不等不等于不是往返。** 2026-10-22 实测：读图把同一程的出发/到达站
    # 读成「福州南站」与「福州站」、「平潭」与「平潭站」这类同城不同写法，
    # 字符串不相等但很可能是同一个往返——直接判 hit 会把合规单报成违规。
    # 核心地名（去掉 站/南站/北站/东站/西站/机场 等后缀）能配对上的，
    # 只能说「像是往返但站名对不上」，不能升级成「确定往返」，也不能当「确定违规」，
    # 按项目纪律转人工，不在两个猜测里选一个当结论。
    core_legs = [(P.strip_station_suffix(a), P.strip_station_suffix(b)) for a, b in legs]
    core_paired = any(a and b and (b, a) in core_legs for a, b in core_legs)
    if core_paired:
        return pending(r["rule_id"],
                       "交通凭证里有两段起止地点互换但站名写法不完全一致（%s），"
                       "像是往返但判不准，需人工核对是否为同一往返"
                       % "、".join(ev["行程"]), ev)
    if has_note:
        return ok(r["rule_id"],
                  "交通凭证只有单程（%s），但已按要求提供情况说明"
                  % "、".join(ev["行程"]), ev)
    return hit(r["rule_id"],
               "交通费报销凭证只有单程（%s），未包含往返行程，且补充说明为空"
               % "、".join(ev["行程"]), ev)


@rule("TR-D6-06")
def tr_d6_06(ctx, r):
    """应有住宿而无住宿发票的连带限制：城市间交通费、伙食补助费、市内交通费均不予报销。

    **比 TR-D6-03 严厉得多**：不只是缺附件警告，而是连带否掉其余三项费用。
    两条都保留：D6-03 是「提醒补件」，本条是「说明缺失则整单费用不予报销」。
    合并成一条会丢掉严厉度的差别。

    与 TR-D6-03 同一个前提：**要的是单次连续出差天数**，不是累计出差日数。
    本条比 D6-03 更严厉（连带否掉三项费用），所以拿不准时更不能硬判。
    """
    days, why, how, is_single = _travel_day_count(ctx, ctx.fact)
    if days is None:
        return pending(r["rule_id"], "出差天数算不出来：%s" % why)
    if not is_single:
        return pending(r["rule_id"],
                       "只能算出累计 %s 个出差日，而本条要单次连续天数（缺口 G-04）。"
                       "本条的后果是连带否掉城市间交通费、伙食补助费、市内交通费三项，"
                       "比 TR-D6-03 严厉得多，拿不准时更不能硬判。" % days,
                       {"累计出差日数": days, "天数来源": how})
    if days < 2:
        return not_applicable(r["rule_id"], "单次连续出差 %s 天，未达 2 天" % days)
    hotel = P.sum_of(ctx.fact, ["住宿费", "住宿费（参加会议产生）"])
    if hotel:
        return not_applicable(r["rule_id"], "本单有住宿费明细 %s 元" % hotel)
    remark = ctx.fact["bill"].get("remark")
    affected = {k: P.sum_of(ctx.fact, [k]) for k in
                ("交通费", "伙食补助费", "市内交通费补贴", "市内交通补贴")}
    affected = {k: v for k, v in affected.items() if v}
    ev = {"出差天数": days, "天数来源": how, "无住宿费明细": True,
          "补充说明非空": P.nonempty(remark), "连带不予报销的费用": affected,
          "出处": "制度第二十三条（三）（OCR）+ 省财政厅解答1（原生文本）",
          "例外": "住在自己家里 / 港口集团权属企业内部招待所 / 边远地区无法取票，"
                  "需说明情况并经领导批准"}
    if P.nonempty(remark):
        return ok(r["rule_id"],
                  "无住宿费但已有补充说明，连带限制不触发（说明是否成立交 TR-D9-01）", ev)
    return hit(r["rule_id"],
               "出差 %s 天无住宿费发票且无补充说明，按制度第二十三条（三），"
               "城市间交通费、伙食补助费、市内交通费合计 %s 元均不予报销"
               % (days, round(sum(affected.values()), 2) if affected else 0), ev)


# ─────────────────────────────────────────────────────────────────────────────
# D7 发票时间校验
# ─────────────────────────────────────────────────────────────────────────────
def _trip_range(fact):
    """出差首末日。不管 synced_from_oa 是不是 TRUE——这三条要的是「有没有
    一个可信的出差区间」，不是「单次连续出差」那个更严的判定（那个在
    _travel_day_count 里，只认 OA 同步）。读图抽取的 trip_application
    行（extracted_from='审批单读图'）起止日期本来就是审批单上写的原文，
    拿来核对发票时间是不是在区间内，可信度足够。"""
    trip = fact.get("trip") or {}
    return trip.get("start_date"), trip.get("end_date")


def _hotel_invoices_with_dates(fact):
    """住宿发票记录，按 hotel_invoice.invoice_id 关联回 invoice 取 issue_date/
    remark_col。只要有住宿费发票就都看，不管住宿晚数取不取得到——
    D7-03/04/05 要的是开票日期，跟晚数无关。"""
    hotel_ids = {h.get("invoice_id") for h in fact.get("hotel_invoices") or []}
    return [i for i in fact.get("invoices") or []
            if i.get("invoice_id") in hotel_ids
            or "住宿" in str(i.get("inv_type") or "")]


_REMARK_DATE_RE = None


def _remark_date(remark):
    """从发票备注栏文字里找一个 YYYY-MM-DD 或中文日期格式的日期。
    找不到返回 None——**不把「备注栏有字」当成「备注栏有日期」**。"""
    global _REMARK_DATE_RE
    if _REMARK_DATE_RE is None:
        import re
        _REMARK_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
    if not remark:
        return None
    import datetime
    m = _REMARK_DATE_RE.search(remark)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return datetime.date(int(y), int(mo), int(d))
    except ValueError:
        return None


@rule("TR-D7-03")
def tr_d7_03(ctx, r):
    """住宿费发票开票时间不早于出差首日。"""
    invs = _hotel_invoices_with_dates(ctx.fact)
    if not invs:
        return not_applicable(r["rule_id"], "本单没有住宿费发票记录")
    start, _end = _trip_range(ctx.fact)
    if not start:
        return pending(r["rule_id"], "出差首日取不到（需 OA 同步或审批单读图），无法核对开票时间")
    bad, unknown = [], []
    for i in invs:
        d = i.get("issue_date")
        if d is None:
            unknown.append(i)
        elif d < start:
            bad.append(i)
    ev = {"出差首日": start, "住宿发票张数": len(invs),
          "开票早于出差首日": [i.get("invoice_id") for i in bad],
          "开票日期取不到": [i.get("invoice_id") for i in unknown],
          "出处": (r.get("source_clause") or {}).get("clause")}
    if bad:
        return hit(r["rule_id"], "存在开票时间早于出差首日 %s 的住宿发票" % start, ev)
    if unknown:
        return pending(r["rule_id"], "有住宿发票取不到开票日期，无法核对", ev)
    return ok(r["rule_id"], "住宿发票开票时间均不早于出差首日", ev)


@rule("TR-D7-04")
def tr_d7_04(ctx, r):
    """住宿费发票开票时间晚于出差末日的，要看发票备注栏有没有时间说明。"""
    invs = _hotel_invoices_with_dates(ctx.fact)
    if not invs:
        return not_applicable(r["rule_id"], "本单没有住宿费发票记录")
    _start, end = _trip_range(ctx.fact)
    if not end:
        return pending(r["rule_id"], "出差末日取不到（需 OA 同步或审批单读图），无法核对开票时间")
    late, unknown = [], []
    for i in invs:
        d = i.get("issue_date")
        if d is None:
            unknown.append(i)
        elif d > end:
            late.append(i)
    ev = {"出差末日": end, "住宿发票张数": len(invs),
          "开票晚于出差末日": [i.get("invoice_id") for i in late],
          "开票日期取不到": [i.get("invoice_id") for i in unknown],
          "出处": (r.get("source_clause") or {}).get("clause")}
    if late:
        missing_remark = [i.get("invoice_id") for i in late if not P.nonempty(i.get("remark_col"))]
        if missing_remark:
            return hit(r["rule_id"],
                       "住宿发票开票时间晚于出差末日 %s，且备注栏为空，无法说明原因" % end, ev)
        return pending(r["rule_id"],
                       "住宿发票开票时间晚于出差末日，备注栏有文字但是否说明了时间需人工核对", ev)
    if unknown:
        return pending(r["rule_id"], "有住宿发票取不到开票日期，无法核对", ev)
    return ok(r["rule_id"], "住宿发票开票时间均不晚于出差末日", ev)


@rule("TR-D7-05")
def tr_d7_05(ctx, r):
    """发票备注栏里写的时间应在出差日期内；没有备注时间或不在区间内的，
    要看提单界面有没有补充说明。"""
    invs = _hotel_invoices_with_dates(ctx.fact)
    if not invs:
        return not_applicable(r["rule_id"], "本单没有住宿费发票记录")
    start, end = _trip_range(ctx.fact)
    remark = ctx.fact["bill"].get("remark")
    has_note = P.nonempty(remark)
    ev = {"出差区间": "%s ~ %s" % (start or "?", end or "?"),
          "补充说明非空": has_note,
          "出处": (r.get("source_clause") or {}).get("clause")}
    if not start or not end:
        if has_note:
            return ok(r["rule_id"], "出差区间取不到，但已有补充说明", ev)
        return pending(r["rule_id"], "出差首末日取不到，且无补充说明，无法核对发票备注时间", ev)
    out_of_range = []
    for i in invs:
        rd = _remark_date(i.get("remark_col"))
        if rd is None:
            continue
        if rd < start or rd > end:
            out_of_range.append((i.get("invoice_id"), rd))
    ev["备注时间不在出差区间内"] = ["%s(%s)" % (iid, d) for iid, d in out_of_range]
    if out_of_range and not has_note:
        return hit(r["rule_id"],
                   "发票备注时间不在出差日期内，且提单界面无补充说明", ev)
    return ok(r["rule_id"],
              "发票备注时间在出差日期内，或已有补充说明" if (out_of_range or has_note)
              else "发票备注栏无时间信息，不适用本条核对", ev)


# ─────────────────────────────────────────────────────────────────────────────
# D8 预算分摊
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D8-05")
def tr_d8_05(ctx, r):
    """餐补金额计算正确。composite：汇总 TR-D4-04 等的结论。"""
    return _composite(ctx, r, "餐补计算")


# ─────────────────────────────────────────────────────────────────────────────
# D9 说明充分性（软规则）
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D9-01")
def tr_d9_01(ctx, r):
    """例外情形的补充说明是否充分。**软规则，命中必须人工复核。**

    **规则引擎本身不调模型**，这是有意的：引擎的判定要可复现，
    同样的输入必须得到同样的结论，而模型不保证这一点。
    大模型的辅助判断放在引擎之后单独跑（app/api/ai_review.py 的 _llm_supplement），
    它只往这条 pending 上附一段参考意见，**不改引擎的结论**。
    所以这里产出的仍然是「判不了」——
    「判不了」和「判过了没问题」是两件事，混了就等于声称复核了没复核的东西。
    """
    triggered = [rid for rid, f in (ctx.fact.get("_findings_by_rule") or {}).items()
                 if f.outcome == "hit" and rid != r["rule_id"]]
    remark = ctx.fact["bill"].get("remark")
    ev = {"触发例外的规则": triggered, "补充说明非空": P.nonempty(remark),
          "补充说明长度": len(str(remark or "")),
          "判定方式": "软规则，需大模型研判 + 人工复核"}
    if not triggered:
        return not_applicable(r["rule_id"], "本单没有触发任何需要补充说明的例外情形")
    if not P.nonempty(remark):
        return hit(r["rule_id"],
                   "有 %d 条例外情形被触发（%s），但补充说明为空"
                   % (len(triggered), "、".join(triggered)), ev, needs_human=True)
    return pending(r["rule_id"],
                   "有补充说明，但「说明是否针对该例外给出了具体、可核查的理由」"
                   "需要大模型研判 + 人工复核；引擎不下这个结论（判定要可复现）", ev)


# ─────────────────────────────────────────────────────────────────────────────
# D10 单位特殊规则
# ─────────────────────────────────────────────────────────────────────────────
@rule("TR-D10-01")
def tr_d10_01(ctx, r):
    """闽运集团本部由郯筠和代报司机及领导差旅费 → TR-D1-03 豁免。"""
    bill = ctx.fact["bill"]
    sub = (ctx.fact.get("employee_submitter") or {}).get("name")
    ev = {"提单人姓名": sub, "出处": "审核要点 2.3.2.1",
          "作用": "命中则豁免 TR-D1-03「本人自行提单」"}
    if sub == "郯筠和":
        return ok(r["rule_id"], "闽运集团本部授权代报，TR-D1-03 豁免", ev)
    return not_applicable(r["rule_id"], "提单人不是被授权的代报人", ev)


@rule("TR-D10-02")
def tr_d10_02(ctx, r):
    """八方物流驾驶员汇总报销，同行人免签。"""
    remark = str(ctx.fact["bill"].get("remark") or "")
    phrase = "驾驶员报销，同行人已自行报销"
    ev = {"要求话术": phrase, "补充说明含该话术": phrase in remark,
          "出处": "审核要点 2.3.2.2"}
    if phrase in remark:
        return ok(r["rule_id"], "已按要求在补充说明区备注，同行人签名要求豁免", ev)
    return not_applicable(r["rule_id"], "补充说明未出现驾驶员汇总报销的约定话术", ev)


@rule("TR-D10-03")
def tr_d10_03(ctx, r):
    """八方物流综合办授权审批驾驶员出差，有效期 2026-01-04 ~ 2026-12-31。"""
    import datetime
    bill = ctx.fact["bill"]
    d = bill.get("doc_date")
    if d is None:
        return pending(r["rule_id"], "单据日期为空，无法判断是否在授权有效期内")
    day = d.date() if hasattr(d, "date") else d
    lo, hi = datetime.date(2026, 1, 4), datetime.date(2026, 12, 31)
    approvers = [a.get("approver") for a in ctx.fact.get("approvals") or []]
    ev = {"单据日期": str(day), "授权有效期": "%s ~ %s" % (lo, hi),
          "在有效期内": lo <= day <= hi, "审批人": approvers,
          "出处": "审核要点 2.3.2.3"}
    if not (lo <= day <= hi):
        return hit(r["rule_id"], "单据日期 %s 不在综合办授权有效期内" % day, ev)
    return ok(r["rule_id"], "单据日期在授权有效期内", ev)


@rule("TR-D10-04")
def tr_d10_04(ctx, r):
    """中平船舶管理：住宿费/机票暂无法取得发票时，可凭住宿清单先行报销，
    暂免 TR-D1-02/TR-D3-04/TR-D6-01 的发票要求。

    scope 已按核算主体=中平船舶管理过滤，这里只管「有没有凭住宿清单替代」。
    """
    items = {d.get("expense_item") for d in ctx.fact.get("details") or []}
    relevant = {"住宿费", "住宿费（参加会议产生）", "交通费"} & items
    if not relevant:
        return not_applicable(r["rule_id"], "本单没有住宿费或交通费（机票）明细")
    invs = ctx.fact.get("invoices") or []
    has_list = any(i.get("inv_type") == "住宿清单" for i in invs)
    ev = {"相关费用项": sorted(relevant),
          "发票类型": [i.get("inv_type") for i in invs if i.get("inv_type")],
          "是否凭住宿清单先行报销": has_list,
          "出处": (r.get("source_clause") or {}).get("clause")}
    if has_list:
        return ok(r["rule_id"],
                  "已凭住宿清单先行报销，暂免 TR-D1-02/D3-04/D6-01 的发票要求", ev)
    return not_applicable(r["rule_id"], "未使用住宿清单替代发票，走正常发票校验路径")


@rule("TR-D10-05")
def tr_d10_05(ctx, r):
    """涉密项目：已说明不便上传附件，审核意见需含固定话术。"""
    remark = str(ctx.fact["bill"].get("remark") or "")
    opinions = " ".join(str(a.get("opinion") or "") for a in ctx.fact.get("approvals") or [])
    phrase = "涉密项目，审核以业财审核为准"
    is_secret = "涉密" in remark
    ev = {"补充说明含涉密": is_secret, "审核意见含固定话术": phrase in opinions,
          "出处": "审核要点 1.3.9.3",
          "作用": "命中则豁免 TR-D6-01 附件完整性"}
    if not is_secret:
        return not_applicable(r["rule_id"], "本单补充说明未提涉密", ev)
    if phrase not in opinions:
        return hit(r["rule_id"],
                   "补充说明提及涉密，但审核意见里没有「%s」这句固定话术" % phrase, ev)
    return ok(r["rule_id"], "涉密项目且审核意见已含固定话术，附件完整性豁免", ev)


@rule("TR-D10-06")
def tr_d10_06(ctx, r):
    """大额支付与拆单检测：单笔 ≥200 万，或 1~2 日内同主体同收款方合计 ≥200 万。"""
    bill = ctx.fact["bill"]
    amt = P.as_num(bill.get("amount"))
    thr = 2000000.0
    peers = ctx.fact.get("peer_bills_2d") or []
    peer_sum = sum(P.as_num(p.get("amount")) or 0 for p in peers)
    total = (amt or 0) + peer_sum
    ev = {"本单金额": amt, "阈值": thr,
          "1~2 日内同主体同收款方其他单据": len(peers), "其他单据合计": peer_sum,
          "合计": total, "出处": "审核要点 1.3.9.2"}
    if amt is None:
        return pending(r["rule_id"], "报账金额为空，无法判断", ev)
    if amt >= thr:
        return hit(r["rule_id"], "单笔报账金额 %s 元达到 200 万元，需福港本部审批" % amt, ev)
    if total >= thr:
        return hit(r["rule_id"],
                   "本单 %s 元，加上 1~2 日内同主体同收款方的 %d 笔共 %s 元，"
                   "合计 %s 元达到 200 万元，疑似拆单支付" % (amt, len(peers), peer_sum, total), ev)
    return ok(r["rule_id"], "单笔及短期合计均未达 200 万元", ev)


# ─────────────────────────────────────────────────────────────────────────────
# composite 汇总
# ─────────────────────────────────────────────────────────────────────────────
def _composite(ctx, r, label):
    """把 delegates 的结论汇总上来。

    **有一个 delegate 判不了，整条就判不了。** 不能「其余都过所以算过」——
    那等于声称复核了没复核的部分。
    delegate 本身不在执行集里（未编译）的，也算判不了，并说明是哪一条。
    """
    delegates = (r["logic"] or {}).get("delegates") or []
    found = ctx.fact.get("_findings_by_rule") or {}
    results, missing = {}, []
    for d in delegates:
        f = found.get(d)
        if f is None:
            missing.append(d)
        else:
            results[d] = f.outcome
    ev = {"汇总自": delegates, "各自结论": results, "未参与判定的": missing,
          "出处": r["source_clause"].get("clause") if r.get("source_clause") else None}
    if missing:
        return pending(r["rule_id"],
                       "%s 需汇总 %s，其中 %s 不在执行集里（未编译或缺字段），无法汇总"
                       % (label, "、".join(delegates), "、".join(missing)), ev)
    if any(v == "hit" for v in results.values()):
        bad = [k for k, v in results.items() if v == "hit"]
        return hit(r["rule_id"], "%s 不正确：%s 命中" % (label, "、".join(bad)), ev)
    if any(v == "pending" for v in results.values()):
        und = [k for k, v in results.items() if v == "pending"]
        return pending(r["rule_id"], "%s 无法确认：%s 判不了" % (label, "、".join(und)), ev)
    return ok(r["rule_id"], "%s 正确" % label, ev)


def _person_count(fact):
    """出差人数。多人单用分摊部门去重数兜，单人单为 1。

    这是偏宽的估计：分摊部门去重数 <= 实际人数（同部门两人会被算成一个），
    人数估小则人日均算大，只会多报不会漏报——**方向是安全的那一侧**。
    实测：7 张多人单的费用明细只有「部门」列，没有人员列（缺口 G-14）。
    """
    bill = fact.get("bill") or {}
    if bill.get("doc_type") == "差旅费报账单":
        return 1
    depts = {d.get("alloc_dept") for d in fact.get("details") or [] if d.get("alloc_dept")}
    return max(len(depts), 1)
