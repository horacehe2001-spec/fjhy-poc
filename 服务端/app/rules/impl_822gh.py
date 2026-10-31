# -*- coding: utf-8 -*-
"""822港航 的规则实现。**和福建海运集团的 impl.py 分开放。**

为什么分文件：两家的规则编号体系不同（这边是 TR-差旅-NN / TR-接待-NN，
那边是 TR-DN-NN），制度也不是同一套。混进一个文件之后，
「这条规则是哪家的」只能靠编号前缀猜，而改一家的实现时很容易碰到另一家。
注册表 _REGISTRY 是全局的、按 rule_id 索引，两边编号不撞，所以分文件不影响装载。

──────────────────────────────────────────────────────────────────────────
【这里只实现 7 条，不是 88 条】
──────────────────────────────────────────────────────────────────────────
88 条规则逐条做过数据可行性复核（见 app/rule_feasibility.py 的 MANUAL_REVIEW），
其中有可判范围的 12 条。这 12 条里：
  · TR-差旅-17 与 TR-差旅-02 是**同一条制度条款抽了两次**，
    两条都实现等于同一个违规报两遍。只实现 TR-差旅-02，
    TR-差旅-17 留给人工裁决后再决定合并还是分开。
  · TR-差旅-09 / TR-差旅-22 是把七种费用类型合在一起的大规则，
    每种的适用集合不一样，合判只能取最窄的那个。
    复核结论建议**拆成按费用类型的子规则**，拆之前不实现——
    现在实现出来的是一条「按最窄集合判」的规则，
    它会让另外几种费用类型看起来已经被覆盖了。
  · TR-接待-11 / TR-接待-14 与已实现的 TR-接待-10 / TR-接待-13 判据重叠
    （都靠发票项目明细查类目），先实现代表性的两条，看准确率再铺开。

没有可判范围的 76 条**一条都不实现**：它们现在只会输出 pending，
而 pending 不是产出，是噪音。报告里 76 条 pending 会把 7 条真结论淹掉。

──────────────────────────────────────────────────────────────────────────
【三条纪律，照抄 impl.py】
──────────────────────────────────────────────────────────────────────────
  1. 判不了就返回 pending，**绝不返回 pass**
  2. 命中时 evidence 要让人能自己复核一遍（实际值、标准值、出处条款）
  3. 例外出口要显式检查——漏掉例外就会对正常单据大面积误报

【还有一条是这个实例特有的】
  4. **招待明细的三个码列不许当业务值用**（entertain_type / entertain_way /
     companion_level 是 OTHER_ENTERTAIN、EAT_WORK、LEVEL_05 这种系统码，
     客户没给码表）。用它们分流程就是在猜，猜错了整类判定全错而且错得
     看起来很合理。所以凡是要靠「公务接待还是商务接待」分支的规则，
     这里一律返回 pending 并说明缺码表——**不按码的字面意思猜**。
"""
from . import predicates as P
from .engine import hit, not_applicable, ok, pending, rule

# 制度里的数，逐字抄自条款，改制度时改这里。
LOCAL_TRANSPORT_DAILY = 80.0        # 第十七条：市内交通费每人每天 80 元包干
MEAL_DAILY = 100.0                  # 第十四条：省内/其他省份每人每天 100 元
MEAL_DAILY_HIGH = 120.0             # 第十四条：西藏、青海、新疆 120 元
COMPANION_CAP = 3                   # 第十条：接待对象 ≤10 人时陪餐 ≤3 人
GUEST_THRESHOLD = 10                # 第十条：接待对象超过 10 人换另一套算法

# 市内交通费在客户系统里有两种写法，都要认。**不做合并**——
# 合并是口径决策不是清洗，两种写法在客户那边就是并存的。
LOCAL_TRANSPORT_ITEMS = ["市内交通费补贴", "市内交通补贴"]

CLAUSE = {
    "接待": "闽港发办〔2023〕6号《业务接待管理办法》",
    "差旅": "闽港发资财〔2026〕3号《关于修订〈差旅费管理办法〉的通知》",
}


def _entertains(fact):
    return fact.get("entertain_details") or []


def _trip_days(fact):
    return fact.get("trip_days") or []


# ═════════════════════════════════════════════════════════════════════════
# 接待
# ═════════════════════════════════════════════════════════════════════════
@rule("TR-接待-04")
def tr_jd_04(ctx, r):
    """陪餐人数：接待对象 ≤10 人时陪餐 ≤3 人；>10 人时陪餐 ≤ 对象数的 1/3。

    条款第十条里还有「用餐标准按省直机关二类会议标准」「不得提供香烟、
    一律不饮酒」两项，**这个实现只判陪餐人数那一半**：
      · 用餐标准要财政部门的二类会议标准，库里没有这张表
      · 禁烟禁酒由 TR-接待-10 判（靠发票项目明细）
    所以上线时不能把这条规则标成「第十条已覆盖」——它只覆盖了第十条的一项。

    **阈值不分公务/商务**，所以不受招待类型码表缺失的影响（见文件头纪律 4）。
    """
    rows = _entertains(ctx.fact)
    if not rows:
        return not_applicable(r["rule_id"], "本单没有招待明细（非招待类单据）")
    bad, unknown = [], []
    for e in rows:
        g, c = e.get("guest_count"), e.get("companion_count")
        if g is None or c is None:
            unknown.append({"行号": e.get("line_no"), "招待人数": g, "陪同人数": c})
            continue
        g, c = int(g), int(c)
        cap = COMPANION_CAP if g <= GUEST_THRESHOLD else g // 3
        if c > cap:
            bad.append({"行号": e.get("line_no"), "发生日期": str(e.get("happen_date")),
                        "接待对象人数": g, "陪同人数": c, "上限": cap,
                        "依据": ("≤%d 人时陪餐不超过 %d 人" % (GUEST_THRESHOLD, COMPANION_CAP))
                        if g <= GUEST_THRESHOLD else "超过 %d 人时陪餐不超过对象数的 1/3" % GUEST_THRESHOLD})
    ev = {"明细行数": len(rows), "超限行": bad, "取不到人数的行": unknown,
          "出处": CLAUSE["接待"] + " 第十条"}
    if bad:
        return hit(r["rule_id"],
                   "陪餐人数超限 %d 行：%s" % (
                       len(bad), "；".join("第%s行 对象%d人/陪同%d人（上限%d）"
                                           % (b["行号"], b["接待对象人数"],
                                              b["陪同人数"], b["上限"]) for b in bad)),
                   ev)
    if unknown:
        return pending(r["rule_id"],
                       "有 %d 行取不到招待人数或陪同人数，无法判定" % len(unknown), ev)
    return ok(r["rule_id"], "%d 行招待明细的陪餐人数均未超限" % len(rows), ev)


@rule("TR-接待-22")
def tr_jd_22(ctx, r):
    """招待明细须按接待日期分开填列，不得把多次合并成一行。

    判据：同一张单里出现**同一个发生日期的多行**，就是没有分开填列吗？
    **不是。** 同一天确实可能有两次不同的接待（中午一次、晚上一次）。
    所以这条判的是反面：**一行都没有发生日期**，或者整单只有一行却
    对应多笔费用明细——那才是「合并填列」的痕迹。

    客户自己的驳回意见 #28 原话是「简餐请按照接待日期，在招待明细区
    分开进行填列」，说明判的是「该分开而没分开」。
    能确定性查出来的只有「发生日期为空」这一种；
    「两次接待写成了一行」需要看附件才知道，所以那种情形返回 pending，
    **不假装判过**。
    """
    rows = _entertains(ctx.fact)
    if not rows:
        return not_applicable(r["rule_id"], "本单没有招待明细（非招待类单据）")
    bill = ctx.fact["bill"]
    reason_txt = "%s %s" % (bill.get("summary") or "", bill.get("pay_purpose") or "")
    period = P.month_phrase(reason_txt)
    no_date = [e for e in rows if not e.get("happen_date")]
    ev = {"明细行数": len(rows),
          "缺发生日期的行": [e.get("line_no") for e in no_date],
          "发生日期": sorted({str(e.get("happen_date")) for e in rows
                              if e.get("happen_date")}),
          "事由/付款用途里的期间表述": period,
          "出处": "客户人工驳回意见 #28「简餐请按照接待日期，在招待明细区分开进行填列」"}
    if no_date:
        return hit(r["rule_id"],
                   "有 %d 行招待明细没有填发生日期，无法按日期分开核对" % len(no_date),
                   ev)
    # ── 整月合并：有标注样本支撑的判据 ──────────────────────────────────
    # 2026-11-01 拿客户 29 条人工驳回意见对了一遍，这条规则**漏报了**
    # FJPG-BX2609201921——人工驳的原话正是「简餐请按照接待日期，在招待
    # 明细区分开进行填列」，而第一版实现只查「发生日期为空」，放过了它。
    #
    # 三张「X月份工作餐」的单据构成了一组标注样本：
    #   2609201921  8月份工作餐   招待明细 1 行  → 人工驳「要分开填列」
    #   2606221801  5月份工作餐   招待明细 1 行  → 人工驳的是别的原因
    #   2603230510  二月份工作餐  招待明细 9 行  → **人工没驳这一条**
    # 判别特征因此很清楚：**事由写的是一个「月份」，而明细只有 1 行**，
    # 就是把整月合并成了一笔。已经按日期拆开的那张（9 行）不命中。
    #
    # 为什么是 hit 不是 pending：判据完全来自库里的字段（事由 + 明细行数），
    # 不需要看附件。第一版那个「1 行而费用明细多笔」的 pending 判据
    # 在这批数据上一次都没命中过——费用明细那边早就合成一笔了。
    if period and len(rows) == 1:
        return hit(r["rule_id"],
                   "事由写的是「%s」（一个月的工作餐），而招待明细只有 1 行"
                   "（%s，%s 元，%s 人）——整月合并成了一笔，"
                   "应按接待日期分开填列"
                   % (period, rows[0].get("happen_date"), rows[0].get("amount"),
                      rows[0].get("guest_count")), ev)
    if period:
        return ok(r["rule_id"],
                  "事由是「%s」，招待明细已按 %d 个日期分开填列（%s）"
                  % (period, len(ev["发生日期"]), "、".join(ev["发生日期"])), ev)
    return ok(r["rule_id"],
              "%d 行招待明细都填了发生日期（%s）"
              % (len(rows), "、".join(ev["发生日期"])), ev)


def _scan_invoice_items(fact, terms):
    """在发票的**项目明细**里找词。返回 [(发票号, 项目明细, 命中词)]。

    **只扫 detail_text，不扫销售方名称。** 销售方里带「酒」的太多了
    （XX大酒店、XX酒业），扫销售方会把住在酒店的正常住宿费判成「报销酒类」
    ——误报和漏报同样致命，而这类误报会让审核岗很快不再信任这套系统。
    """
    out = []
    for i in fact.get("invoices") or []:
        txt = str(i.get("detail_text") or "")
        hits = [t for t in terms if t in txt]
        if hits:
            out.append({"发票号": i.get("inv_no") or i.get("invoice_id"),
                        "项目明细": txt[:60], "命中": hits})
    return out


# 烟酒类词表。**不用单字「酒」**：会把「酒店」「酒水服务费」之外的
# 正常项目也扫进来。用具体品类词，宁可漏一点也不误伤。
TOBACCO_ALCOHOL = ["香烟", "卷烟", "烟草", "白酒", "红酒", "洋酒", "啤酒",
                   "黄酒", "酒水"]
# 礼品类词表。同理用具体词。
GIFT_TERMS = ["礼金", "有价证券", "购物卡", "预付卡", "代金券", "纪念品",
              "土特产", "礼品"]


@rule("TR-接待-10")
def tr_jd_10(ctx, r):
    """与港口集团权属单位之间的接待：不得提供或报销香烟、酒类。

    **只实现禁烟禁酒那两项。** 条款里还有「同城接待不安排用餐」和
    「按公务接待要求执行」两项：
      · 「是否同城」要比接待地点与本单位所在地，而招待明细的地点只到城市
        （福州/厦门），本单位也在福州——判出来会把所有福州的接待都算同城，
        而条款说的同城是指「接待对象也在同城」，库里没有接待对象的属地。
      · 「是否与港口集团权属单位接待」要靠招待类型码表区分，**码表客户没给**。
    所以这条规则**不限定在权属单位接待**，而是对所有招待单查烟酒——
    范围比条款宽。**宽在哪里必须说出来**：宽了只会多报不会漏报，
    但多报的那部分不是违规，审核岗要知道这一点。
    """
    rows = _entertains(ctx.fact)
    if not rows:
        return not_applicable(r["rule_id"], "本单没有招待明细（非招待类单据）")
    invs = ctx.fact.get("invoices") or []
    found = _scan_invoice_items(ctx.fact, TOBACCO_ALCOHOL)
    liquor = [e for e in rows
              if e.get("liquor_amount") is not None and float(e["liquor_amount"]) > 0]
    ev = {"发票张数": len(invs), "命中的发票": found,
          "领用酒水金额大于 0 的明细行": [e.get("line_no") for e in liquor],
          "口径": "**范围比条款宽**：条款限定在与港口集团权属单位之间的接待，"
                  "而「是否权属单位接待」要靠招待类型码表区分，码表客户未提供。"
                  "这里对所有招待单查烟酒，只会多报不会漏报。",
          "出处": CLAUSE["接待"] + " 第十九条"}
    if found or liquor:
        return hit(r["rule_id"],
                   "发现烟酒类支出：发票 %d 张命中（%s）；领用酒水金额大于 0 的明细 %d 行"
                   % (len(found), "、".join(f["发票号"] for f in found) or "无",
                      len(liquor)), ev)
    if not invs:
        return pending(r["rule_id"],
                       "本单没有已解析的发票（票据多为 JPG，尚未读图），"
                       "无法核查项目明细里有没有烟酒", ev)
    return ok(r["rule_id"],
              "%d 张发票的项目明细里没有烟酒类，领用酒水金额均为 0 或未填" % len(invs),
              ev)


@rule("TR-接待-13")
def tr_jd_13(ctx, r):
    """公务接待不得以任何名义赠送礼金、有价证券、纪念品、土特产品。

    判据同 TR-接待-10：扫发票项目明细。
    **同样不限定在「公务接待」**——公务/商务要靠招待类型码表区分，
    码表客户没给。范围比条款宽，宽在哪里写在证据里。
    """
    rows = _entertains(ctx.fact)
    if not rows:
        return not_applicable(r["rule_id"], "本单没有招待明细（非招待类单据）")
    invs = ctx.fact.get("invoices") or []
    found = _scan_invoice_items(ctx.fact, GIFT_TERMS)
    ev = {"发票张数": len(invs), "命中的发票": found,
          "口径": "**范围比条款宽**：条款限定在公务接待，而公务/商务要靠"
                  "招待类型码表区分，码表客户未提供。这里对所有招待单查。",
          "出处": CLAUSE["接待"] + " 第二十二条"}
    if found:
        return hit(r["rule_id"],
                   "发票项目明细里出现礼品类：%s"
                   % "；".join("%s（%s）" % (f["发票号"], "、".join(f["命中"]))
                               for f in found), ev)
    if not invs:
        return pending(r["rule_id"],
                       "本单没有已解析的发票（票据多为 JPG，尚未读图），"
                       "无法核查项目明细里有没有礼品", ev)
    return ok(r["rule_id"], "%d 张发票的项目明细里没有礼品类" % len(invs), ev)


# ═════════════════════════════════════════════════════════════════════════
# 差旅
# ═════════════════════════════════════════════════════════════════════════
# 交通工具等级标准在参照表里（不是 standard_limit——那张表的值是金额，
# 而等级不是钱）。表里一行是 (级别, 交通工具) → 允许等级。
LOOKUP_VEHICLE = "交通工具等级标准"
# 主岗里**明确属于「其他人员」档**的三个。董事长/副总经理落在哪一档
# 取决于「公司正职领导」怎么界定，客户还没确认，所以不猜。
PLAIN_RANKS = {"部门负责人", "员工", "其他员工"}


@rule("TR-差旅-02")
def tr_cl_02(ctx, r):
    """未按规定等级乘坐交通工具，超支部分个人自理。

    三件东西齐了才判得了：
      ① 实际乘坐等级 —— 铁路电子客票的项目明细里有席别（二等座/一等座）
      ② 规定等级     —— 参照表「交通工具等级标准」，2 个级别 × 4 种交通工具
      ③ 这个人属于哪一级别 —— **断在这里**

    ③ 的缺口是真的：等级表写「公司正职领导／正高级职称／随同省级人员」
    和「其他人员」，而员工表写「董事长／副总经理／部门负责人／员工／其他员工」，
    两套档位名不是一回事。所以：
      · 报账人主岗是 部门负责人/员工/其他员工 的 → **无论「公司正职领导」
        怎么界定，他们都落在「其他人员」档**，可以判
      · 是 董事长/副总经理 或主岗取不到的 → pending，等客户定映射

    还有一种数据表达不了的情形：高档里有一条是「因工作需要随同省级及
    相当职务人员的出差人员」——那是**随行情形不是职级**，库里没有任何
    字段能表达它。所以即使判出超标，也只能报「疑似」，不能直接拦。
    例外条款（第六章特殊情形）同样不在库里，一并写进证据。
    """
    invs = [i for i in (ctx.fact.get("invoices") or [])
            if "铁路" in str(i.get("inv_type") or "")]
    if not invs:
        return not_applicable(r["rule_id"], "本单没有已解析的铁路电子客票")
    emp = ctx.fact.get("employee_submitter") or {}
    rank = emp.get("rank_level")
    table = ctx.enabled_rows(LOOKUP_VEHICLE) if hasattr(ctx, "enabled_rows") else []
    if not table:
        return pending(r["rule_id"],
                       "参照表「%s」没装进来，取不到规定等级" % LOOKUP_VEHICLE)
    low_tier = None
    for row in table:
        tier = (row.get("row_key") or {}).get("tier") or ""
        veh = (row.get("row_key") or {}).get("vehicle") or ""
        if "其他人员" in tier and "火车" in veh:
            low_tier = (row.get("row_value") or {}).get("allowed")
            break
    ev = {"报账人职级（主岗）": rank, "火车票张数": len(invs),
          "「其他人员」档允许的火车等级": low_tier,
          "口径缺口": "等级表的级别名（公司正职领导／其他人员）与员工主岗"
                      "（董事长／副总经理／部门负责人／员工／其他员工）不是一套，"
                      "对应关系客户未确认；高档里「随同省级人员出差」是随行情形"
                      "不是职级，库里没有字段能表达。",
          "出处": CLAUSE["差旅"] + " 第二十三条附表"}
    if rank not in PLAIN_RANKS:
        return pending(r["rule_id"],
                       "报账人主岗是「%s」，落在哪一档取决于「公司正职领导」"
                       "怎么界定——客户未确认，不猜" % (rank or "取不到"), ev)
    if not low_tier:
        return pending(r["rule_id"], "参照表里取不到「其他人员」档的火车等级", ev)
    bad = []
    for i in invs:
        txt = str(i.get("detail_text") or "")
        seat = next((s for s in ("一等座", "二等座", "软卧", "硬卧", "软座", "硬座")
                     if s in txt), None)
        if seat is None:
            continue
        if seat not in low_tier:
            bad.append({"发票号": i.get("inv_no"), "实际席别": seat,
                        "允许": low_tier})
    ev["逐票席别"] = [{"发票号": i.get("inv_no"),
                       "明细": str(i.get("detail_text") or "")[:40]} for i in invs]
    if bad:
        return hit(r["rule_id"],
                   "报账人主岗「%s」属「其他人员」档，但有 %d 张票超等级：%s"
                   % (rank, len(bad),
                      "、".join("%s（%s）" % (b["发票号"], b["实际席别"]) for b in bad)),
                   ev)
    unknown = [i.get("inv_no") for i in invs
               if not any(s in str(i.get("detail_text") or "")
                          for s in ("一等座", "二等座", "软卧", "硬卧", "软座", "硬座"))]
    if unknown:
        return pending(r["rule_id"],
                       "有 %d 张火车票的项目明细里取不到席别" % len(unknown), ev)
    return ok(r["rule_id"],
              "报账人主岗「%s」属「其他人员」档，%d 张火车票席别均合规"
              % (rank, len(invs)), ev)


@rule("TR-差旅-20")
def tr_cl_20(ctx, r):
    """市内交通费按出差自然天数、每人每天 80 元包干；公司提供车辆的不再报销。

    例外出口（条款明写的）：**由公司提供交通工具的，不再报销市内交通费**。
    库里能表达的只有「接待单位提供车辆」（出差日历的 has_car），
    「公司提供车辆」没有字段——所以这一项只能在证据里标明没查，
    **不能假装查过**。
    """
    amt = P.sum_of(ctx.fact, LOCAL_TRANSPORT_ITEMS)
    if not amt:
        return not_applicable(r["rule_id"], "本单没有市内交通费明细")
    trip = ctx.fact.get("trip") or {}
    days = trip.get("days")
    if not days:
        return pending(r["rule_id"],
                       "取不到出差天数（本单没有已解析的出差审批单），"
                       "无法按天数算包干上限",
                       {"市内交通费": float(amt),
                        "日标准": LOCAL_TRANSPORT_DAILY})
    people = trip.get("person_count") or 1
    cap = LOCAL_TRANSPORT_DAILY * int(days) * int(people)
    tdays = _trip_days(ctx.fact)
    host_car_days = sorted({str(d["cal_date"]) for d in tdays if d.get("has_car")})
    ev = {"市内交通费": float(amt), "出差天数": int(days), "出差人数": int(people),
          "日标准": LOCAL_TRANSPORT_DAILY, "包干上限": cap,
          "接待单位提供车辆的日子": host_car_days,
          "没查的例外": "「由公司提供交通工具的不再报销」——库里没有"
                        "「公司是否提供车辆」这个字段，这一项**没有核查**。",
          "出处": CLAUSE["差旅"] + " 第十七条"}
    if float(amt) > cap:
        return hit(r["rule_id"],
                   "市内交通费 %.2f 元超过包干上限 %.2f 元（%d 天 × %d 人 × %.0f 元）"
                   % (float(amt), cap, int(days), int(people), LOCAL_TRANSPORT_DAILY),
                   ev)
    if host_car_days:
        return pending(r["rule_id"],
                       "有 %d 天由接待单位提供车辆（%s），按条款应向接待单位交费"
                       "且最高不超过日标准——交没交费库里没有记录，无法判定"
                       % (len(host_car_days), "、".join(host_car_days)), ev)
    return ok(r["rule_id"],
              "市内交通费 %.2f 元未超包干上限 %.2f 元" % (float(amt), cap), ev)


@rule("TR-差旅-15")
def tr_cl_15(ctx, r):
    """接待单位协助安排用餐的，应交纳伙食费；未交纳的不再领取伙食补助费。

    条款第 2 条里有好几件事，**这个实现只判其中一件**：
      已判：接待单位安排了餐的那几天，是否仍然领了伙食补助费
      未判：午休房（限定在闽侯县/连江县贵安/长乐机场当天往返，
            本批 15 份审批单的目的地里一个都没有，适用集合为空）
      未判：统一安排伙食时「仅按往返各 1 天计发」（库里没有「是否统一安排」）
      未判：交没交伙食费、交了多少（库里没有交费记录）

    所以命中时给的是**疑似**：条款说的是「未交纳伙食费的不再领取」，
    而「交没交」库里没有。能确定的只是「接待单位安排了餐而且领了补助」，
    这两件事同时成立时值得人看一眼。**不能直接判违规。**
    """
    tdays = _trip_days(ctx.fact)
    if not tdays:
        return not_applicable(r["rule_id"],
                              "本单没有已解析的出差日历（无出差审批单或审批单是扫描件）")
    meal = P.sum_of(ctx.fact, ["伙食补助费"])
    host_meals = [d for d in tdays if d.get("has_meal")]
    host_days = sorted({str(d["cal_date"]) for d in host_meals})
    ev = {"伙食补助费": float(meal or 0),
          "逐日逐餐次行数": len(tdays),
          "接待单位安排用餐的餐次数": len(host_meals),
          "涉及日期": host_days,
          "没查的部分": "「交没交伙食费」库里没有记录；"
                        "「是否由所在单位统一安排伙食」也没有字段。"
                        "午休房那一项适用集合为空（本批目的地里没有闽侯县、"
                        "连江县贵安、长乐机场）。",
          "出处": CLAUSE["差旅"] + " 第2条 / 2023年附件3 第十五条"}
    if not host_meals:
        return ok(r["rule_id"],
                  "%d 个餐次全部是自行用餐，不涉及向接待单位交纳伙食费" % len(tdays),
                  ev)
    if not meal:
        return ok(r["rule_id"],
                  "有 %d 个餐次由接待单位安排（%s），而本单未领取伙食补助费"
                  % (len(host_meals), "、".join(host_days)), ev)
    return pending(r["rule_id"],
                   "有 %d 个餐次由接待单位安排（%s），同时领取了伙食补助费 %.2f 元"
                   "——条款规定未交纳伙食费的不再领取，而交没交库里没有记录，"
                   "需人工核对交款凭证"
                   % (len(host_meals), "、".join(host_days), float(meal)), ev)
