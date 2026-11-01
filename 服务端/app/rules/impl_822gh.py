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
import datetime

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


@rule("TR-接待-01")
def tr_jd_01(ctx, r):
    """业务接待先审批后接待：审批应早于接待发生。

    【口径是按日期比，不按时间戳比——这是量出来的，不是省事】
      审批单上的「接待时间」是**计划**时间（票面写的是「预计在XX酒店用餐」
      那一栏旁边的时间），不是实际发生时间。实测
        FJPG-BX2604260325  接待 2026-04-22 15:00，最后审批 04-20 16:48:30
        FJPG-BX2606240151  接待 2026-06-23 17:00，最后审批 06-23 17:03:45
      第二张按时间戳比**晚了 3 分钟**，会命中；而人工对这张单驳的是
      「入账期间」和「清单场所与发票不一致」，**没有驳审批滞后**。
      按日期比则两张都通过，和人工 2/2 一致。
      3 分钟的差是表单填报的噪声，不是「先接待后审批」。

    【人工驳过审批滞后的那两张，现在一张判出来了、另一张本来就不归这条管】
      FJPG-BX2607162084「业务审批清单，审批流程滞后」
        2026-11-01 读图批把它的审批单读出来了（那张 JPG 是 OA 打印件，
        流转记录完整）：接待时间 07-15 18:00，分管领导 07-16 08:18、
        主管领导 07-16 10:44 → **按日期命中**，和人工原话对上。
      FJPG-BX2604160034「审批滞后，请在补充说明栏备注原因」
        这是**差旅费报账单**，人工说的是出差审批滞后，不是接待审批。
        本规则对它返回「不适用」是对的，不要为了「让它也被判到」
        去放宽适用范围。

    【剩下 5 份 pending 是什么，要说清楚——不是「还没读图」】
      26 份 JPG 已经全部读过了（44 次调用、0 份漏读）。读完之后才看清
      这一桶里装的是三种不同的表：
        · **OA 打印件**（3 份）：流转记录带时间戳，能判——阳性样本就在这里
        · **手写纸质业务接待审批单**：经办人/承办部门/分管领导/主管领导
          **只有签名、没有日期**。票面上就没有审批时间这个信息，
          读图读不出来是因为**它不存在**，不是因为读不清。
        · **简餐审批单（附件3）/ 纪念品、酒水领用申请单（附件6）**：
          定类时被归成了业务接待审批单，但它们连「接待时间」那一格都没有，
          回写闸按「必须有接待时间」把它们挡在表外了。
      所以这 5 份 pending 的真实含义是
      **「这张单的接待审批时间在纸面上就不存在」**，
      要判它得客户从 OA 导审批流水，读图再读一百遍也没有。

    【「通过 3 张」仍然不等于「接待审批都合规」】

    【条款里另外两项不判】
      「接待对象应为有实质性业务往来人员」要人判断实质；
      「公务接待应提供公函」要看附件里有没有公函，而公函这个附件类型
      本批一个都没有（已定类的 134 个附件里 0 个公函）。
      两项都写在证据里，不计入结论。
    """
    apprs = ctx.fact.get("entertain_approvals") or []
    ents = _entertains(ctx.fact)
    doc_type = str(ctx.fact["bill"].get("doc_type") or "")
    base = {"没判的部分": "条款还要求「接待对象有实质性业务往来」（要人判断实质）"
                          "和「公务接待提供公函」（公函这个附件类型本批 0 个）。"
                          "两项**没有核查**，不计入结论。",
            "口径": "按**日期**比，不按时间戳比。审批单上的接待时间是计划时间，"
                    "实测 FJPG-BX2606240151 审批比接待时间晚 3 分钟，"
                    "按时间戳会命中而人工没驳它——3 分钟是填报噪声。",
            "出处": CLAUSE["接待"] + " 第六条"}
    if not ents and not apprs:
        return not_applicable(r["rule_id"], "本单不是接待类单据", base)
    if not apprs:
        return pending(
            r["rule_id"],
            "本单是%s，但没有已解析的业务接待审批单。"
            "26 份 JPG 已于 2026-11-01 全部读过——读完才看清这一桶里混着"
            "简餐审批单和酒水领用单（连「接待时间」那一格都没有），"
            "以及**只有签名、没有日期**的手写纸质审批单。"
            "要判这一条，得客户从 OA 导出接待审批流水"
            % (doc_type or "接待类单据"), base)
    # **只取业务接待审批那条流程的流水。** approval_record 装两条流程
    # （NULL = 报账流程，见 migration 0059），混起来取 max 会把报账流程的
    # 审批时间当成接待审批的最后一步——报账总是发生在接待之后，
    # 于是每一张单都会被判成「先接待后审批」。这是误报，不是漏报。
    rows = [x for x in (ctx.fact.get("approvals") or [])
            if x.get("flow") == "业务接待审批"]
    # 一张报账单挂多份审批单（实测最多 9 份）。能按份对上就按份对
    # （app_no 是 migration 0060 加的），对不上的退回「本单该流程的最后一步」
    # 并在证据里说明用的是哪一种——**不许悄悄退化**。
    by_form = {}
    for x in rows:
        if x.get("app_no") and x.get("approved_at"):
            by_form.setdefault(x["app_no"], []).append(x["approved_at"])
    bill_last = max((x.get("approved_at") for x in rows
                     if x.get("approved_at")), default=None)
    bad, unknown, detail = [], [], []
    for a in apprs:
        ent_at = a.get("entertain_at")
        mine = by_form.get(a.get("app_no"))
        last = max(mine) if mine else bill_last
        d_ent = str(ent_at)[:10] if ent_at else None
        d_app = str(last)[:10] if last else None
        detail.append({"审批单": a.get("app_no"), "接待时间": str(ent_at),
                       "最后审批时间": str(last),
                       "接待日期": d_ent, "审批日期": d_app,
                       "审批时间取自": ("这份审批单自己的流水" if mine
                                   else "本单业务接待审批流程的最后一步"),
                       "来源": a.get("extracted_from")})
        if not d_ent or not d_app:
            unknown.append(a.get("app_no"))
        elif d_app > d_ent:
            bad.append({"审批单": a.get("app_no"), "接待日期": d_ent,
                        "审批日期": d_app})
    ev = dict(base)
    ev.update({"审批单份数": len(apprs),
               "业务接待审批流水行数": len(rows),
               "另一条流程的流水行数": len(ctx.fact.get("approvals") or []) - len(rows),
               "逐份明细": detail})
    if bad:
        return hit(r["rule_id"],
                   "先接待后审批 %d 份：%s" % (
                       len(bad), "；".join(
                           "审批单 %s 接待日期 %s、审批日期 %s"
                           % (b["审批单"], b["接待日期"], b["审批日期"])
                           for b in bad)), ev)
    if unknown:
        return pending(r["rule_id"],
                       "有 %d 份审批单取不到接待时间或审批时间" % len(unknown), ev)
    return ok(r["rule_id"],
              "%d 份业务接待审批单的审批日期都不晚于接待日期" % len(apprs), ev)


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


def _han(s):
    return "".join(ch for ch in str(s or "") if "一" <= ch <= "龥")


def _grams(s):
    """地名的 2 字片段集合。**不用城市白名单。**

    第一版用的是一张城市正则（北京|上海|…），实测它漏了「湛江」——
    而漏掉的表现不是报错，是**静默失配**：湛江市希朋酒店对不上
    申报地「海南湛江」，于是报一条「地点不符」的误报。
    闭集合的地名表永远补不全（县、区、开发区、口岸），
    所以改成比 2 字片段：「海南湛江」的片段里有「湛江」，
    命中「湛江市希朋酒店管理有限公司」；「莆田市秀屿区」有「莆田」，
    命中「莆田市必盛酒店管理有限公司秀屿分公司」。
    """
    h = _han(s)
    return {h[i:i + 2] for i in range(len(h) - 1)}


def _trips(fact):
    ts = list(fact.get("trips") or [])
    if not ts and fact.get("trip"):
        ts = [fact["trip"]]
    return ts


# 住宿类项目明细的特征词。**只拿住宿票比地点**：
# 酒店在哪儿，人就住在哪儿；而代订机票/代订附加那类票的销售方是旅行社，
# 名字前缀是**旅行社的注册地**不是出差地。实测拿全部发票比，
# 「上海华程西南国际旅行社」对申报地遵义、「天津航路无界国际旅行社」对珠海、
# 「成都携程旅行社」对海南，3 张单全是误报；窄到住宿类之后 0 条。
LODGING_HINTS = ("住宿",)


def _lodging_invoices(fact):
    out = []
    for i in fact.get("invoices") or []:
        dt = str(i.get("detail_text") or "")
        if any(h in dt for h in LODGING_HINTS):
            out.append(i)
    return out


@rule("TR-差旅-28")
def tr_cl_28(ctx, r):
    """差旅行程地点一致性：比对出差日历、审批单回填内容与发票明细里的地点。

    【只比了两方，不是三方——这件事必须说在前面】
      规则原文要比三方。**出差日历的逐日地点这一方不在库里**：
      trip_calendar.place 本实例 90 行全空，因为出差审批单 PDF 的逐日表
      只有「日期 × 早/中/晚」和「日期 × 上午/下午」两张小表，
      **没有地点列**，整单只有表头一个「出差地点」。
      人工为逐日地点驳过 5 次（「日历区7月23日地点改为厦门」这类），
      驳的是客户 ERP 里的日历区，那个数据我们根本没拿到。
      所以这条规则**判不了人工驳的那 5 条**，它判的是另一件事：
      审批单申报的出差地 vs 住宿发票的开票方所在地。

    【为什么只拿住宿票比】
      见 LODGING_HINTS 上面那段：拿全部发票比会把旅行社注册地当出差地，
      实测 3 张单全误报。

    【为什么用 2 字片段而不是城市表】
      见 _grams。城市白名单漏「湛江」，漏了不报错、只误报。
    """
    ts = _trips(ctx.fact)
    if not ts:
        return not_applicable(
            r["rule_id"],
            "本单没有已解析的出差审批单，取不到申报的出差地点")
    places = [t.get("place") for t in ts if t.get("place")]
    g = set().union(*[_grams(p) for p in places]) if places else set()
    lodging = _lodging_invoices(ctx.fact)
    usable = [i for i in lodging if _han(i.get("issuer")) and
              len(_han(i.get("issuer"))) >= 4]
    ev = {"申报的出差地点": places,
          "住宿类发票张数": len(lodging),
          "开票方可用的": [str(i.get("issuer")) for i in usable],
          "只比了两方": "规则原文要比三方。出差日历的逐日地点本实例取不到"
                        "（trip_calendar.place 90 行全空，出差审批单 PDF 的"
                        "逐日表没有地点列）。人工为逐日地点驳过 5 次，驳的是"
                        "客户 ERP 的日历区，**这条规则判不了那 5 条**。",
          "不比的发票": "代订机票/代订附加类不参与比对——销售方是旅行社，"
                        "名字前缀是旅行社注册地不是出差地。实测拿全部发票比"
                        "会误报 3 张单。",
          "出处": CLAUSE["差旅"]}
    if not places:
        return pending(r["rule_id"], "审批单上取不到出差地点", ev)
    if not usable:
        return pending(
            r["rule_id"],
            "本单 %d 张住宿类发票的开票方名称取不到（票面是扫描件或开票方那一格"
            "没解析出来），无法比对地点" % len(lodging)
            if lodging else "本单没有住宿类发票，没有可比对地点的票据", ev)
    bad = [i for i in usable if not (_grams(i.get("issuer")) & g)]
    ev["逐票比对"] = [{"开票方": str(i.get("issuer")),
                       "与申报地有共同地名片段": bool(_grams(i.get("issuer")) & g)}
                      for i in usable]
    if bad:
        return hit(r["rule_id"],
                   "住宿发票的开票方所在地与申报出差地点「%s」对不上：%s"
                   % ("、".join(places),
                      "、".join(str(i.get("issuer")) for i in bad)), ev)
    return ok(r["rule_id"],
              "%d 张住宿发票的开票方所在地都落在申报出差地点「%s」内"
              % (len(usable), "、".join(places)), ev)


@rule("TR-差旅-30")
def tr_cl_30(ctx, r):
    """差旅审批与票据时间匹配：审批单的出差日期范围 vs 票据上的日期。

    【用乘车日期，不用开票日期】
      开票日期**本来就在行程之后**（实测住宿发票 2026-05-24~26 的行程
      开票日期是 06-08）。拿开票日期比区间会把正常单据大面积判成
      「票据日期不在出差期间」。
      车票的项目明细里有真正的乘车日期（「2026年07月22日 13:35开 06车09F号
      二等座」），比的是它。

    【只比了两方，不是三方】
      规则原文还要比「附件通知」的日期（培训通知/会议通知上写的活动起止）。
      那类附件的字段抽取还没做，所以这条判不了人工驳 FJPG-BX2608311795
      时说的「按照培训通知体现出差时间应为8.22-8.28」——
      通知上的日期不在库里。
      住宿发票票面也没有入住日期（hotel_invoice 本实例 0 行），不参与。

    【本批 0 命中，这是实测结果不是没判】
      10 份审批单取到了乘车日期，全部落在申报的出差区间内。
    """
    ts = _trips(ctx.fact)
    if not ts:
        return not_applicable(
            r["rule_id"], "本单没有已解析的出差审批单，取不到出差日期范围")
    spans = [(t.get("start_date"), t.get("end_date")) for t in ts
             if t.get("start_date") and t.get("end_date")]
    rides = []
    for i in ctx.fact.get("invoices") or []:
        for y, m, d in P.cn_dates(str(i.get("detail_text") or "")):
            rides.append((datetime.date(y, m, d), i.get("inv_no")))
    # 第三方：通知上写的活动起止（migration 0057 补的 meeting_notice 三列）。
    # 8 份会议/培训通知里只有 1 份有文字层，其余 7 份是扫描件——
    # **有就比，没有就在证据里说没比**，不拿「两方都对得上」充当三方核过了。
    notices = [(n.get("notice_start"), n.get("notice_end"))
               for n in (ctx.fact.get("meeting_notices") or [])
               if n.get("notice_start") and n.get("notice_end")]
    ev = {"出差日期范围": ["%s~%s" % (a, b) for a, b in spans],
          "票面乘车日期": sorted({str(d) for d, _ in rides}),
          "通知上的活动起止": ["%s~%s" % (a, b) for a, b in notices] or "取不到",
          "第三方比没比": ("比了（通知有文字层）" if notices else
                           "**没比**：本单的会议/培训通知是扫描件，"
                           "活动起止取不到（8 份通知里只有 1 份有文字层）"),
          "不用开票日期": "开票日期本来就在行程之后（实测 05-24~26 的行程"
                          "住宿发票开票日期是 06-08），拿它比区间会大面积误报。",
          "出处": CLAUSE["差旅"]}
    if not spans:
        return pending(r["rule_id"], "审批单上取不到出差起止日期", ev)
    # 申报区间与通知区间的差异：**算出来、报出来，但不下结论。**
    #
    # 批次 C 里我把口径定成「申报必须覆盖通知」，当时只有 1 份通知，
    # 而那一份正好覆盖住了，所以 0 命中、看不出问题。
    # 2026-11-01 读图补到 6 份通知之后，这个口径当场产出一条命中：
    #   FJPG-BX2606011481 申报 05-23~05-28  覆盖通知 05-24~05-28  通过
    #   FJPG-BX2606050568 申报 05-24~05-28  覆盖              通过
    #   FJPG-BX2606050495 申报 05-24~05-26  **没覆盖** → 命中
    # 三个人参加同一期培训班，其中一个少报两天。
    # **人工对 2606050495 驳的是「补充5.25日用餐与用车情况」，没驳这件事。**
    # 提前回程是正常的；反过来申报比通知长（2606011481 早一天出发赶路）
    # 也是正常的。两个方向都有正常解释，**而这条规则到底算哪个方向违规，
    # 客户没说过**——人工驳 2608311795 的那句「按照培训通知体现出差时间应为
    # 8.22-8.28」说的是逐日表只填了两天（TR-通用-02 判的），不是申报区间。
    #
    # 口径未定就不许命中：在 6 个样本、0 个人工标注的基础上自己定一个方向，
    # 产出的就是误报。差异照算照报，结论留给客户定口径。
    notice_diff = []
    for na, nb in notices:
        if not any(a <= na and nb <= b for a, b in spans):
            notice_diff.append("通知 %s~%s" % (na, nb))
    if notice_diff:
        ev["申报区间与通知区间不一致"] = notice_diff
        ev["为什么不据此命中"] = (
            "口径未定：申报比通知短（提前回程）和比通知长（提前一天赶路）"
            "都有正常解释，客户没说过哪个方向算违规，"
            "而人工对唯一一张不一致的单（FJPG-BX2606050495）驳的是别的事。"
            "**这一项算了但不计入结论**，等客户定口径。")
    if not rides and not notices:
        return pending(
            r["rule_id"],
            "本单票据的项目明细里取不到乘车日期（车票是扫描件，或本单没有车票），"
            "通知也是扫描件——两方都取不到", ev)
    out = [(d, no) for d, no in rides
           if not any(a <= d <= b for a, b in spans)]
    if out:
        return hit(r["rule_id"],
                   "有 %d 个乘车日期落在申报的出差区间（%s）之外：%s"
                   % (len(out), "、".join(ev["出差日期范围"]),
                      "、".join("%s（票 %s）" % (d, no) for d, no in out)), ev)
    if notice_diff:
        return pending(
            r["rule_id"],
            "乘车日期都在申报区间内；但申报区间（%s）与%s不一致，"
            "**这一项的口径客户还没定**（申报比通知短/长都有正常解释），"
            "所以不下结论"
            % ("、".join(ev["出差日期范围"]), "、".join(notice_diff)), ev)
    if not rides:
        return pending(
            r["rule_id"],
            "通知上的活动起止（%s）被申报区间覆盖，但本单取不到乘车日期"
            "（车票是扫描件，或本单没有车票）" % "、".join(ev["通知上的活动起止"]),
            ev)
    return ok(r["rule_id"],
              "%d 个乘车日期都落在申报的出差区间（%s）内%s"
              % (len(rides), "、".join(ev["出差日期范围"]),
                 ("；通知上的活动起止 %s 也被覆盖" % "、".join(ev["通知上的活动起止"]))
                 if notices else "（通知是扫描件，第三方没比）"), ev)


# ═════════════════════════════════════════════════════════════════════════
# 通用
# ═════════════════════════════════════════════════════════════════════════
@rule("TR-通用-02")
def tr_ty_02(ctx, r):
    """审批单关键要素完整性：逐日用餐/用车情况是否逐天填满、出差人是否签名回填。

    【这条规则原来被标成「判不了」，理由写的是「判的就是图片内容」——那是错的】
    15 份出差审批单是 **PDF 且有文字层**，表尾两张小表（差旅用餐情况：
    日期 × 早/中/晚；差旅用车情况：日期 × 上午/下午）和「出差人签名 <姓名>
    <日期>」都能确定性解析出来，不需要读图。只有 3 份是扫描件/JPG，
    那 3 份返回 pending。

    【判两件事，都有人工驳回原话兜底】
      ① 逐日表的日期必须覆盖出差起止区间的每一天
         FJPG-BX2606050495  出差 05-24~05-26（3 天），表里只有 05-24、05-26
           → 人工原话「出差审批单需补充**5.25日**用餐与用车情况」
         FJPG-BX2608311795  出差 08-22~08-28（7 天），表里只有 08-22、08-28
           → 人工原话「出差时间应为8.22-8.28，出差审批单**仅体现8.22、8.28**」
      ② 出差人签名日期必须有值（backfill_date）
         FJPG-BX2603270004 → 人工原话「出差审批未体现出差人回填情况（签字及日期）」
         FJPG-BX2606101698 → 人工原话「补充审批单下面回填日期」

    【② 现在一条都判不出来，不是因为数据好】
      实测 15 份全部有签名日期。而人工驳过这件事的两张单，
      审批单一张是 JPG、一张不是标准格式，**都在 pending 那 3 份里**。
      所以 ② 这一半的真实状态是「有能力判、本批没有阳性样本」，
      不是「本批全部合规」——这两件事在报告上不能写成一句话。

    【按份判，不按单判】
      FJPG-BX2607280255 挂了两份审批单（07-23~24 与 07-25~26）。
      只看第一份会把第二份的缺漏整段放过去，所以逐份算、逐份报。

    【没判的部分，写在证据里】
      规则原文还要求查「姓名、职务、酒水规格」：
        · 姓名在审批单上有（填写人/出差人），但**职务不在这张表上**
        · 酒水规格是业务接待审批单（A16）上的字段，不在出差审批单上
      这两项这里一律不判，不计入结论。
    """
    bill = ctx.fact["bill"]
    doc_type = str(bill.get("doc_type") or "")
    trips = list(ctx.fact.get("trips") or [])
    if not trips and ctx.fact.get("trip"):
        trips = [ctx.fact["trip"]]

    base_ev = {
        "没判的部分": "规则原文还要求查「职务」与「酒水规格」。职务不在出差"
                      "审批单这张表上；酒水规格是业务接待审批单（A16）的字段。"
                      "这两项**没有核查**，不计入结论。",
        "出处": "客户人工驳回意见：「出差审批单需补充5.25日用餐与用车情况」"
                "（FJPG-BX2606050495）、「出差审批单仅体现8.22、8.28」"
                "（FJPG-BX2608311795）、「出差审批未体现出差人回填情况"
                "（签字及日期）」（FJPG-BX2603270004）",
    }
    if not trips:
        if "招待" in doc_type:
            return not_applicable(
                r["rule_id"],
                "本单是%s，关键要素要查的是业务接待审批单（A16），"
                "不是出差审批单——A16 的字段抽取还没做，这条规则在接待类单据上"
                "本实例暂不适用" % (doc_type or "招待类单据"), base_ev)
        return pending(
            r["rule_id"],
            "本单没有已解析的出差审批单（附件里的审批单是扫描件或 JPG，"
            "文字层取不到，尚未读图）", base_ev)

    days_by_app = {}
    for d in _trip_days(ctx.fact):
        days_by_app.setdefault(d.get("app_no"), set()).add(str(d.get("cal_date")))

    gaps, no_sign, unjudgeable, per_app = [], [], [], []
    for t in trips:
        app = t.get("app_no")
        sd, ed = t.get("start_date"), t.get("end_date")
        have = days_by_app.get(app, set())
        # **读图抽取的审批单只读了表头，判不了这条规则。**
        # 2026-11-01 实测到的误报，是我自己造出来的：读图批给 8 份扫描/JPG
        # 审批单写了 trip_application 行，而那次的提示词只问了
        # 出差起止日期和出差地点三个字段——**逐日用餐/用车表没问、
        # 出差人签名日期也没问**。规则看到「逐日表一行都没有」「签名日期为空」，
        # 于是报「逐日表缺 N 天」「没有签名回填日期」：
        # 一次就把 6 张单从 pending 变成了命中，而票面上那些格子可能是填了的。
        # **「没读」不是「没填」。** 这两件事在报告上长得一模一样，
        # 而把前者说成后者，是审核系统最该避免的那种错。
        if t.get("extracted_from") == "审批单读图":
            unjudgeable.append(
                {"审批单": app,
                 "原因": "这份审批单是读图抽取的，提示词只问了出差起止与地点"
                         "三个字段；逐日用餐/用车表和出差人签名日期**没有读**，"
                         "不是票面上没有"})
            per_app.append({"审批单": app, "出差起止": "%s~%s" % (sd, ed),
                            "来源": "审批单读图（只读了表头）",
                            "逐日表里的日期": sorted(have),
                            "缺的日期": None, "出差人签名日期": None})
            continue
        miss = []
        if isinstance(sd, datetime.date) and isinstance(ed, datetime.date) \
                and sd <= ed:
            d = sd
            while d <= ed:
                if d.isoformat() not in have:
                    miss.append(d.isoformat())
                d += datetime.timedelta(days=1)
        else:
            unjudgeable.append({"审批单": app, "原因": "取不到出差起止日期"})
        per_app.append({"审批单": app,
                        "出差起止": "%s~%s" % (sd, ed),
                        "逐日表里的日期": sorted(have),
                        "缺的日期": miss,
                        "出差人签名日期": str(t.get("backfill_date") or "") or None,
                        "填报日期": str(t.get("report_date") or "") or None})
        if miss:
            gaps.append({"审批单": app, "出差起止": "%s~%s" % (sd, ed),
                         "缺的日期": miss})
        if not t.get("backfill_date"):
            no_sign.append(app)

    ev = dict(base_ev)
    ev.update({"审批单份数": len(trips), "逐份明细": per_app})

    if gaps or no_sign:
        parts = []
        for g in gaps:
            parts.append("审批单 %s（出差 %s）的用餐/用车逐日表缺 %d 天：%s"
                         % (g["审批单"], g["出差起止"], len(g["缺的日期"]),
                            "、".join(g["缺的日期"])))
        if no_sign:
            parts.append("审批单 %s 没有出差人签名回填日期"
                         % "、".join(str(a) for a in no_sign))
        return hit(r["rule_id"], "；".join(parts), ev)
    if unjudgeable:
        return pending(r["rule_id"],
                       "有 %d 份审批单判不了：%s"
                       % (len(unjudgeable),
                          "；".join("%s（%s）" % (u["审批单"], u["原因"])
                                   for u in unjudgeable)), ev)
    return ok(r["rule_id"],
              "%d 份审批单的用餐/用车逐日表都填满了出差区间的每一天，"
              "且都有出差人签名回填日期" % len(trips), ev)


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
