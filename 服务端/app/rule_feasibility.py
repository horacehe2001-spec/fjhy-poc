# -*- coding: utf-8 -*-
"""一条规则在某个实例里**能不能真的判**——拿数据实测，不看它自报什么。

── 为什么要有这一层 ───────────────────────────────────────────────────
2026-10-31：822港航 的 14 条反向抽取规则里，`TR-通用-02 审批流程时效性`
自报 auto_judgeable=yes，于是被当成「最便宜的第一条、可以先编译」。
拿数据一核：这个实例 **approval_record 0 行、doc_date 0/29**，
「审批完成时间 vs 单据提交时间」两个输入一个都没有。
自报的可行性是模型对规则文字的判断，**不是对这个实例数据的判断**。

所以这里把两件事分开，各由擅长的一方做：
  模型说  这条规则需要哪些业务数据项（它读得懂自然语言判定逻辑）
  代码判  这些数据项在这个实例里有没有（这件事必须精确，不能猜）

── 三种结局，不许合并 ────────────────────────────────────────────────
    判不了      需要的数据项，库里有这个字段但本实例是空的 → 等客户补数据
    缺字段      库里**根本没有这个字段/表** → 要改模型（schema），不是补数据
    口径未定    数据项名字映射不到任何字段 → 要人先定这个名字指什么
「缺字段」和「判不了」给人的动作完全不同：前者找我们改表，后者找客户要数。
合成一句「数据不足」，客户补完数据回来发现还是判不了。

**映射不上的一律进「口径未定」，绝不当成可得。** 静默丢掉一个认不出的
数据项，会让一条判不了的规则显示成「可判」——那比没有这个判定更坏。
"""
from . import data_availability as da, db

# 业务数据项名 → 它落在哪。
#   (表, 列)    这一列必须在本实例有值
#   (表, None)  这张表必须在本实例有行
#   NO_FIELD    库里根本没有这个东西，要改 schema
# 同义词全部列出来，**不做模糊匹配**：模糊匹配会把「审批单目的地」匹到
# 「审批单」，于是「缺的是目的地字段」变成「缺的是审批单」，方向就错了。
NO_FIELD = "__no_field__"

NEED_MAP = {
    # ── 附件类（822港航 attachment 0 行）──
    "附件清单": ("attachment", None),
    "票据附件": ("attachment", None),
    "附件内容": ("attachment", None),
    "审批单图片": ("attachment", None),
    "发票影像": ("attachment", None),
    "授权书": ("attachment", None),
    # ── 发票类 ──
    "发票日期": ("invoice", "issue_date"),
    "发票明细": ("invoice", "detail_text"),
    "发票明细日期": ("invoice", "issue_date"),
    "发票开具地点": ("invoice", "issuer"),
    # ── 出差申请/日历 ──
    "出差审批单": ("trip_application", None),
    "审批单": ("trip_application", None),
    "出差审批单日期": ("trip_application", "start_date"),
    "审批单目的地": ("trip_application", "place"),
    "出差天数": ("trip_application", "days"),
    "出差日历": ("trip_calendar", None),
    # ── 审批流 ──
    "审批完成时间": ("approval_record", "approved_at"),
    "审批时间": ("approval_record", "approved_at"),
    "审批日期": ("approval_record", "approved_at"),
    # ── 单据头 ──
    "入账期间": ("expense_bill", "booking_period"),
    "业务发生日期": ("expense_bill", "doc_date"),
    "付款用途": ("expense_bill", "pay_purpose"),
    "业务类型": ("expense_bill", "biz_type"),
    "报销人": ("expense_bill", "claimant_no"),
    "收款人": ("expense_bill", "payee"),
    "费用承担人": ("expense_bill", "bearing_dept"),
    "补充说明": ("expense_bill", "remark"),
    "补充说明区": ("expense_bill", "remark"),
    # ── 明细 ──
    "报销明细": ("expense_detail", None),
    "系统明细": ("expense_detail", None),
    "费用类型": ("expense_detail", "expense_item"),
    # ── 库里真的没有的 ──
    # 单据提交时间：expense_bill 没有「提单时间」这一列。created_at 是写库
    #   那一刻，**不是业务上的提单时间**，不许顶替（见 data_availability
    #   的 _BOOKKEEPING）。要判审批时效，得先加一列并由客户提供。
    "单据提交时间": NO_FIELD,
    # 业务招待清单：接待明细目前只落在 expense_detail 的费用项里，没有
    #   「接待清单」这个结构（陪餐人、职务、场所、酒水规格都没有字段）。
    #   驳回理由里有 4 条就是在查这些，所以这是个真缺口，不是命名问题。
    "业务招待清单": NO_FIELD,
    "招待明细日期": NO_FIELD,

    # ── 2026-10-31 补：那 74 条制度抽取规则要的数据项 ──────────────────
    # 模型按要求把词表外的写成「其他：<名字>」，回来 23 个。
    # 其中只有下面 4 个在库里有字段，**另外 19 个连表都没有**。
    #
    # 这不是「缺数据」，是**缺数据模型**，必须分清：
    # 那 74 条覆盖 8 个制度域——会议 11 / 采购 7 / 决策 3 / 租房 2 /
    # 备用金 2 / 日常 2，而本 POC 的库是按**差旅报销单**建的。
    # 采购、招标、供应商、合同、租房、备用金**没有任何表**；
    # meeting_notice 只有 unified_board/meal_provided/shuttle/self_funded
    # 四个布尔列，连参会人数都没有。
    # 所以这批规则等客户补数据也解锁不了——要先建表。
    # 客户数据需求清单里这两类要分开提，否则客户把数据给过来，
    # 回头发现还是判不了。
    "人员职级": ("employee", "rank_level"),
    "员工常驻地址": ("employee", "resident_city"),
    "支付方式": ("expense_bill", "pay_method"),
    "职级标准": ("rank_tier_map", None),
    # —— 以下 19 个：库里没有对应表/字段 ——
    "外派状态": NO_FIELD,
    "参会人员数量": NO_FIELD,
    "工作人员数量": NO_FIELD,
    "借用金额": NO_FIELD,
    "预计归还日期": NO_FIELD,
    "月度租房合同金额": NO_FIELD,
    "公示状态": NO_FIELD,
    "违规事实记录": NO_FIELD,
    "年度收入水平": NO_FIELD,
    "供应商数量": NO_FIELD,
    "招标方式": NO_FIELD,
    "采购方式": NO_FIELD,
    "采购金额": NO_FIELD,
    "采购预算金额": NO_FIELD,
    "采购合同": NO_FIELD,
    "采购人员信息": NO_FIELD,
    "招标代理机构信息": NO_FIELD,
    "投标邀请书发送对象": NO_FIELD,
    "自行招标能力": NO_FIELD,
}

# 模型把词表外的数据项写成「其他：<名字>」。**这个前缀要剥掉再查映射**，
# 否则一个已经补进 NEED_MAP 的名字仍然会被判成「口径未定」。
# ─────────────────────────────────────────────────────────────────────────────
# 原始材料在不在手 —— **这一层是 2026-10-31 补的，补的原因是一次真实的错判**
# ─────────────────────────────────────────────────────────────────────────────
# 第一版只判「库里有没有这个数据」，然后把所有「没有」一律当成「找客户要」。
# 据此排出来的客户需求清单，涉及规则数前三名是出差审批单(26)/附件(24)/
# 发票明细(16)——而这三样的原始材料**早就在 材料/822港航、单据、数据/ 下面
# 躺着**，是我方没解析：
#   · 三个单据 zip 里共 570 个附件文件（388 个 PDF + 182 张 JPG）一个没入库
#   · 报账单 xls 有 5 个分区（主表/归属/分摊/支付/出差地点），只读了主表
#   · 招待费 xls 有「招待明细区」，而库里根本没建这个结构
#   · 员工信息导出里有「差旅级别」，没映射成职级
# 差一点就让客户去补他们已经给过的东西。
#
# 所以「库里有没有」和「该谁动」是两个维度，必须分开：
#   库里有没有  → verdict（判不了 / 缺字段 / …），描述系统现状
#   该谁动      → owner（我方 / 客户 / 待定），决定这条进谁的清单
# 合成一个维度，就会把「我方的活」写进客户需求清单——那是最贵的一类错误：
# 客户照着做完回来，发现问题还在。
#
# 每条都要带**证据**（材料里的哪个文件/哪个分区），不是「我觉得在手」。
IN_HAND = "我方"          # 材料已收到，我方未解析/未建表
NEED_CLIENT = "客户"      # 客户确实没给
UNDECIDED = "待定"        # 还没核过材料，不许猜

# key 是 NEED_MAP 的目标：优先 "表.列"，取不到再按 "表"。
SOURCE_STATUS = {
    "attachment": (IN_HAND,
                   "三个单据 zip 里共 570 个附件文件（388 PDF + 182 JPG），未入库"),
    "invoice": (IN_HAND,
                "附件里有 388 个「发票附件-*.pdf」，未解析"),
    "trip_application": (IN_HAND,
                         "附件里有「港航本部出差审批流程*.pdf」；"
                         "差旅报账单 xls 还有「出差地点」分区，均未读"),
    "expense_bill.payee": (IN_HAND, "差旅/招待报账单 xls 的「支付区」有收款客商、收款账户名称"),
    "expense_bill.pay_method": (IN_HAND, "同上，「支付区」有支付方式"),
    "expense_bill.pay_purpose": (IN_HAND, "同上，「支付区」有付款用途"),
    "expense_bill.claimant_no": (IN_HAND, "报账单 xls「主表区」有姓名、经办部门"),
    "expense_bill.bearing_dept": (IN_HAND, "报账单 xls「归属区」有费用归属部门"),
    "employee.rank_level": (IN_HAND, "员工信息导出有「差旅级别」（一级/二级…），未映射"),
    "expense_detail": (IN_HAND, "报账单 xls「分摊区」已读，招待的「招待明细区」未读"),
    # —— 真缺 ——
    "expense_bill.doc_date": (NEED_CLIENT, "导出里只有「入账期间」（到月），没有单据日期"),
    "approval_record": (NEED_CLIENT, "导出里只有最终「驳回原因」，没有审批流水"),
    "rank_tier_map": (NEED_CLIENT,
                      "12 份制度里没有职级→住宿档位的对应；住宿标准 317 条里"
                      "105 条的档位列在原文件里没有表头；员工写「一级/二级」而"
                      "标准表写「厅局级/其他人员」，两套对不上"),
    "trip_calendar": (NEED_CLIENT,
                      "报账单只有「出差地点」一列（如「宁德、福州」），"
                      "没有逐日的出差日历"),
    "employee.resident_city": (NEED_CLIENT, "员工信息导出里没有常驻地址"),
}

# 库里没有字段的那些（NEED_MAP 里标 NO_FIELD），同样要分「材料在不在手」。
SOURCE_STATUS_NO_FIELD = {
    "业务招待清单": (IN_HAND,
                 "招待费 xls 有「招待明细区」：招待地点、发生日期、招待人数、"
                 "陪同人数、领用酒水金额、招待对象名单。**库里要先建表**"),
    "招待明细日期": (IN_HAND, "同上，「招待明细区」有发生日期"),
    "单据提交时间": (NEED_CLIENT, "导出里没有提单时间，要客户增列"),
}
# 采购/招标/租房/备用金/会议 这些：822港航 给了制度但没给对应的单据样本，
# 所以既缺表也缺数据，一律算真缺。列在这里是为了别让它们落进「待定」。
for _k in ("供应商数量", "招标方式", "采购方式", "采购金额", "采购预算金额",
           "采购合同", "采购人员信息", "招标代理机构信息", "投标邀请书发送对象",
           "自行招标能力", "参会人员数量", "工作人员数量", "借用金额",
           "预计归还日期", "月度租房合同金额", "公示状态", "违规事实记录",
           "年度收入水平", "外派状态"):
    SOURCE_STATUS_NO_FIELD[_k] = (
        NEED_CLIENT, "822港航 提供了对应制度，但没有提供这一类业务的单据样本")


def source_of(need_name):
    """这个数据项的原始材料在不在手、该谁动。返回 (归属, 证据)。

    **查不到一律返回「待定」，不许默认成任何一方。** 默认成「我方」会让
    客户清单漏项；默认成「客户」会让客户去补已经给过的东西——后者正是
    这一层要防的那个错。
    """
    n = normalize_need(need_name)
    tgt = NEED_MAP.get(n)
    if tgt == NO_FIELD:
        return SOURCE_STATUS_NO_FIELD.get(n, (UNDECIDED, "还没核过材料"))
    if not tgt:
        return UNDECIDED, "数据项名还没定指什么"
    t, c = tgt
    if c and ("%s.%s" % (t, c)) in SOURCE_STATUS:
        return SOURCE_STATUS["%s.%s" % (t, c)]
    return SOURCE_STATUS.get(t, (UNDECIDED, "还没核过材料"))


_OTHER_PREFIX = "其他："


def normalize_need(name):
    n = (name or "").strip()
    return n[len(_OTHER_PREFIX):].strip() if n.startswith(_OTHER_PREFIX) else n


def need_items(rule_rows=None):
    """从规则的 note 里取回当初记下的「判定需要的数据项」。

    为什么从 note 取：这批规则是 2026-10-31 装进来的，need_fields 当时
    写在 note 里（见 app/demo/load_822gh_rejection_rules.py）。
    **不重新调模型去问一遍**——同一个问题问两次拿到两个答案，
    就说不清台账里记的是哪一次的结论了。
    """
    out = {}
    for r in (rule_rows or []):
        note = r.get("note") or ""
        for line in note.splitlines():
            if line.startswith("判定需要的数据项："):
                out[r["rule_id"]] = [normalize_need(x) for x in
                                     line.split("：", 1)[1].split("、")
                                     if x.strip()]
    return out


def judge(needs, snap):
    """给一条规则的数据项清单定可行性。返回 (结论, 判不了的, 缺字段的, 口径未定的)。"""
    blocked, no_field, unknown, ok, partial = [], [], [], [], []
    for raw in needs:
        n = normalize_need(raw)
        m = NEED_MAP.get(n)
        if m is None:
            unknown.append(n)
            continue
        if m == NO_FIELD:
            no_field.append(n)
            continue
        t, c = m
        rows = snap["tables"].get(t)
        if not isinstance(rows, int) or rows == 0:
            blocked.append("%s（%s 表 0 行）" % (n, t))
            continue
        if c is None:
            ok.append(n)
            continue
        g = (snap["columns"].get(t) or {}).get(c)
        if not g:
            no_field.append("%s（%s.%s 这一列不存在）" % (n, t, c))
        elif g[2] == "无":
            blocked.append("%s（%s.%s 本实例全空）" % (n, t, c))
        elif g[2] == "部分":
            partial.append("%s（%s.%s 只有 %d/%d 有值）" % (n, t, c, g[0], g[1]))
        else:
            ok.append(n)
    if unknown:
        verdict = "口径未定"
    elif no_field:
        verdict = "缺字段"
    elif blocked:
        verdict = "判不了"
    elif partial:
        verdict = "部分字段不全"
    elif ok:
        # **不叫「可判」。** 这一档只说明「它自己列出来的数据项都在库里」，
        # 而 need_fields 本身也是模型的自报，**它会列漏**。2026-10-31 实测
        # 6 条落到这一档的规则，逐条复核后一条都站不住：
        #   TR-会议-04/11  漏列「会议费综合定额标准」——822港航 的
        #                  standard_limit 只有住宿费 317 条，没有会议费定额
        #   TR-差旅-03     漏列交通工具信息，transport_voucher 0 行
        #   TR-接待-19     「附件信息显示实质为接待」，漏列附件内容，0 行
        #   TR-差旅-11     「无实质内容」要人判断，根本不是数据问题
        #   TR-日常-02     数据项真齐备，但 136 条明细里没有一条车辆使用费
        #                  （只有 7 种费用项）→ 适用 0 张单
        # 叫「可判」会让人直接去编译它，编出来的就是「能跑但判不准」。
        # 这一档必须落到人手里复核，所以名字就写成待复核。
        verdict = "未发现数据缺口·待复核"
    else:
        verdict = "口径未定"
    return verdict, blocked, no_field, unknown, partial, ok


def report(instance_id, created_by=None):
    """算出每条规则的可行性，**只算不写**。写库的是 apply()。"""
    snap = da.snapshot(instance_id)
    sql = """SELECT rule_id, name, note FROM rule
              WHERE instance_id = %s AND version = 1"""
    params = [instance_id]
    if created_by:
        sql += " AND created_by = %s"
        params.append(created_by)
    rows = db.query(sql + " ORDER BY rule_id", tuple(params))
    needs = need_items(rows)
    out = []
    for r in rows:
        nf = needs.get(r["rule_id"])
        if nf is None:
            # **没记数据项的不猜**：标出来，让人知道这条还没过这一关，
            # 而不是给它一个看起来已经评估过的结论。
            out.append({"rule_id": r["rule_id"], "name": r["name"],
                        "verdict": "未登记数据项", "needs": [],
                        "blocked": [], "no_field": [], "unknown": [],
                        "partial": [], "ok": [],
                        "owners": [], "owner_main": "", "sources": []})
            continue
        v, bl, nfd, un, pa, okl = judge(nf, snap)
        # **谁该动**，和「库里有没有」分开算。一条规则可能两边都占
        # （有的数据我方没解析、有的客户真没给），所以 owner 是集合不是单值；
        # 对外给一个 owner_main 方便排序，但**不要只看它**——
        # 只看主归属会把「还差客户一样」的规则排进我方清单然后以为做完就能跑。
        names = [x.split("（")[0] for x in bl + nfd]
        srcs = [(n,) + source_of(n) for n in names]
        owners = sorted({o for _, o, _ in srcs})
        main = (NEED_CLIENT if NEED_CLIENT in owners
                else (UNDECIDED if UNDECIDED in owners
                      else (IN_HAND if IN_HAND in owners else "")))
        out.append({"rule_id": r["rule_id"], "name": r["name"], "verdict": v,
                    "needs": nf, "blocked": bl, "no_field": nfd,
                    "unknown": un, "partial": pa, "ok": okl,
                    "owners": owners, "owner_main": main, "sources": srcs})
    return {"instance_id": instance_id, "snapshot": snap, "rules": out}


def _blocker_text(it):
    bits = []
    # **归属放在最前面。** 这一行决定这条规则进谁的清单；放在一长串
    # 字段名后面，读的人翻到那儿之前就已经按「缺数据=找客户」处理掉了。
    if it.get("owners"):
        tag = {IN_HAND: "我方（材料已在手，未解析/未建表）",
               NEED_CLIENT: "客户（确实没给）",
               UNDECIDED: "待定（还没核过材料，不许当成客户的）"}
        bits.append("该谁动：" + "＋".join(tag.get(o, o) for o in it["owners"]))
        for n, o, ev in it.get("sources", []):
            if o == IN_HAND:
                bits.append("「%s」在手：%s" % (n, ev))
    
    if it["blocked"]:
        bits.append("本实例没有这些数据：%s" % "；".join(it["blocked"]))
    if it["no_field"]:
        bits.append("**库里没有这些字段，要先改表**：%s" % "；".join(it["no_field"]))
    if it["unknown"]:
        bits.append("**这些数据项还没定指什么**，要人先定口径：%s"
                    % "、".join(it["unknown"]))
    if it["partial"]:
        bits.append("只有部分单有值（拿它判会静默跳过其余的单）：%s"
                    % "；".join(it["partial"]))
    if it["ok"]:
        bits.append("已具备：%s" % "、".join(it["ok"]))
    return "｜".join(bits) or None


# 落到「未发现数据缺口·待复核」那一档的规则，**必须有一条人工复核结论**。
# 没有就说明这条还没人看过，而 _scope_text 会如实写「待人工复核」而不是
# 「全部 29 张」。test_rule_feasibility 里有一条用例盯着这件事：
# 新规则落到这一档而没有复核结论时它会红，而不是悄悄发一张绿灯。
MANUAL_REVIEW = {
    "TR-会议-04": {"by": "2026-10-31 实测复核",
                  "scope": "0/29 张（缺 会议费综合定额标准）",
                  "why": "need_fields 漏列了综合定额标准本身。822港航 的 "
                         "standard_limit 只有住宿费限额 317 条，没有会议费定额，"
                         "「不得超过综合定额标准」无从比较。"},
    "TR-会议-11": {"by": "2026-10-31 实测复核",
                  "scope": "0/29 张（缺 会议费综合定额标准）",
                  "why": "同 TR-会议-04 缺标准表。而且它的判定写的是「若被认定为"
                         "超范围或超标准」——结论依赖另一条规则的结果，"
                         "本身不是一条可独立判定的规则。"},
    "TR-差旅-03": {"by": "2026-10-31 实测复核",
                  "scope": "0/29 张（缺 交通工具信息）",
                  "why": "要判「是否选乘经济便捷的交通工具」得知道实际乘了什么、"
                         "有哪些可选。transport_voucher 0 行，need_fields 漏列。"},
    "TR-差旅-11": {"by": "2026-10-31 实测复核",
                  "scope": "0/29 张（要人工判断实质）",
                  "why": "「无实质内容」「变相旅游」「接受宴请」靠的是人的判断，"
                         "不是缺哪个字段。被标成 hard/禁止 也不对——"
                         "这是 soft 规则，列入 17 条处置动作待复核那一批。"},
    "TR-接待-19": {"by": "2026-10-31 实测复核",
                  "scope": "0/29 张（缺 附件内容）",
                  "why": "判定原文写的是「单据或附件信息显示费用实质为业务接待」，"
                         "要读附件内容。attachment 0 行，need_fields 漏列。"},
    "TR-日常-02": {"by": "2026-10-31 实测复核",
                  "scope": "0/29 张（本批单据无车辆使用费）",
                  "why": "**这条的数据项是真齐备的**，但 136 条明细只有 7 种费用项"
                         "（伙食补助费/交通费/住宿费/市内交通费补贴/商务接待/"
                         "其他招待/市内交通补贴），没有一条车辆使用费，适用 0 张单。"
                         "规则本身没问题，是这批样本里没有它管的业务。"},
}


def unlock_plan(instance_id, created_by=None):
    """**补哪一样能解锁最多规则**——客户数据需求按这个排序提，不按表名字母序。

    一条规则要它的数据项**全部**到位才解锁，所以「解锁数」这一栏是
    「补了这一样之后、这条规则还差几样」的分母，不是承诺。
    所以同时给 `仅差这一样` ——补完它就真的能判的那些规则。
    只报前者会让人以为补一张表就能上线六条规则。
    """
    rep = report(instance_id, created_by)
    need_of = {}
    for it in rep["rules"]:
        if it["verdict"] == "未登记数据项":
            continue
        miss = [x.split("（")[0] for x in it["blocked"] + it["no_field"]]
        if miss:
            need_of[it["rule_id"]] = miss
    buckets = {}
    for rid, miss in need_of.items():
        for m in miss:
            tgt = NEED_MAP.get(normalize_need(m))
            key = ("库里没这个字段：" + m) if tgt == NO_FIELD else (
                "%s 表" % tgt[0] if tgt and tgt[1] is None
                else "%s.%s" % tgt if tgt else "口径未定：" + m)
            # **用 set**：同义数据项会映到同一列（「审批日期」和「审批时间」
            # 都是 approval_record.approved_at），用 list 会把同一条规则
            # 数两次，于是「涉及 3 条规则」里其实只有 2 条。
            o, ev = source_of(m)
            b = buckets.setdefault(key, {"涉及规则": set(), "仅差这一样": set(),
                                         "该谁动": o, "证据": ev})
            b["涉及规则"].add(rid)
            if len(set(miss)) == 1:
                b["仅差这一样"].add(rid)
    out = sorted(({"要补的": k,
                   "该谁动": buckets[k]["该谁动"],
                   "证据": buckets[k]["证据"],
                   "涉及规则数": len(v["涉及规则"]),
                   "补完即可判": len(v["仅差这一样"]),
                   "规则": sorted(v["涉及规则"])}
                  for k, v in buckets.items()),
                 key=lambda x: (-x["补完即可判"], -x["涉及规则数"]))
    # **按归属分成两张清单。** 合在一张里排序，我方的活和客户的活会交替
    # 出现，而这份东西是要发给客户的——客户看到自己清单里有「我们已经给过
    # 的附件」，下一句就是「这不是给过了吗」。
    return {"instance_id": instance_id, "items": out,
            "rules_blocked": len(need_of),
            "ours": [x for x in out if x["该谁动"] == IN_HAND],
            "theirs": [x for x in out if x["该谁动"] == NEED_CLIENT],
            "undecided": [x for x in out if x["该谁动"] == UNDECIDED]}


def _scope_text(it, n_bills):
    """本演示这条规则实际能判几张单。沿用福建海运集团那一列的写法
    （「仅单人 33 张（多人导出缺 报账人）」「区间判定（缺 住宿天数…）」）。"""
    # **人工复核结论盖过机械推断。** TR-差旅-03/11 机械上是「部分字段不全」，
    # 而人看过之后结论是 0/29（一个缺交通工具信息、一个要人判断实质）。
    # 两个数并排写在页面上互相打架，而对的是人那一个。
    rv0 = MANUAL_REVIEW.get(it["rule_id"])
    if rv0:
        return rv0["scope"]
    miss = [x.split("（")[0] for x in it["blocked"] + it["no_field"]]
    if it["verdict"] in ("判不了", "缺字段", "口径未定"):
        return "0/%d 张（缺 %s）" % (n_bills, "、".join(miss[:4]) or "必要数据")
    if it["verdict"] == "部分字段不全":
        return "部分单可判（%s）" % "；".join(it["partial"][:2])
    # **不写「全部 29 张」**：那是承诺，而这一档还没人复核过。
    rv = MANUAL_REVIEW.get(it["rule_id"])
    return rv["scope"] if rv else "待人工复核（未发现数据缺口，但 need_fields 可能列漏）"


def apply(instance_id, created_by=None):
    """把结论写进 rule.demo_blocker / rule.demo_scope。可重入。

    **不写 feasibility。** 那一列是客户规则清单里的「是否已有」
    （见 app/rule_detail.py 的 FIELD_MAP：取值 是/否/已有/无/部分/部分已有），
    记的是**客户自己的系统里有没有这条规则**，和「本 POC 判不判得了」是
    两件完全不同的事。第一版我把结论写进了 feasibility，等于把两种语义
    塞进同一列——822港航 会显示 feasibility='判不了'，而福建海运集团同一列
    是 '已有/可以实现'，两个实例的同一列不是一个意思，下游没法一起读。
    822港航 没有客户评估表，这一列就该是空的，空着才是实话。
    """
    rep = report(instance_id, created_by)
    n_bills = db.scalar("SELECT count(*) FROM expense_bill WHERE instance_id=%s",
                        (instance_id,)) or 0
    n = 0
    for it in rep["rules"]:
        if it["verdict"] == "未登记数据项":
            continue
        blocker = "数据可行性（%s）：%s" % (it["verdict"], _blocker_text(it))
        rv = MANUAL_REVIEW.get(it["rule_id"])
        if rv:
            blocker += "｜%s：%s" % (rv["by"], rv["why"])
        db.execute("""UPDATE rule SET demo_blocker = %s, demo_scope = %s
                       WHERE instance_id = %s AND rule_id = %s AND version = 1""",
                   (blocker, _scope_text(it, n_bills), instance_id, it["rule_id"]))
        n += 1
    return {"written": n, "n_bills": n_bills, "report": rep}
