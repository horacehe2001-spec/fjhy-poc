# -*- coding: utf-8 -*-
"""单据附件字段抽取。**真出网、真计费**，是 0037 判类型之后的第二步。

0037 解决「这张图是什么类型」；本模块解决「这张图上写了什么字段值」。
两者刻意分两次调用、两套提示词——判类型的提示词只问类型，字段抽取的
提示词只问字段，混在一次问里模型会顾此失彼。

**2026-10-22 用户决策：这一轮只抽文字/日期/布尔字段，金额税额先不落明文。**
价税合计/税额/税率这一次提示词里都不问，模型答了也不解析、不落库——
不是「解析了但不回写」，是**压根不进解析器**，杜绝它以后被哪个新代码当字段用。

**2026-11-01 第三轮（822港航 读图批）加了 A16 业务接待审批单，
并给 A06/A07 补上活动起止与地点。** 这一轮踩到并修掉的三处：
  · PDF 原样发出去会被厂商 400 拒（A06 那 7 份全是扫描 PDF）。
    渲页复用 attachment_classify._as_image，**不另写一份**。
  · meeting_notice 的回写一直没给 instance_id，而那列 NOT NULL——
    这条分支从来没跑通过，全库只有 1 行且是别的程序写的，所以没人撞见。
  · A16 这一桶里混着「简餐审批单」和「纪念品、酒水领用申请单」。
    回写闸收成**必须有接待时间**：混进去不会造成误报，但会把
    「审批单份数」这个对外报的数字做大，让「已核查」看起来成立。
依然**不问金额**：业务接待审批单票面上的「标准（元/人）」「预算总额」
看得见也不问，放开与否是口径决策，决策权在用户。

和 attachment_classify 同样的三条纪律：
  1. 判不出就是 NULL，不猜
  2. 把握不到「高」不回写到 invoice / hotel_invoice / transport_voucher /
     trip_application 这些业务表（但调用记录照落 attachment_extract）
  3. 成败都落台账，永不覆盖（同一张图重读是新 attempt）
"""
import json
import re

from .. import db
# _as_image 是 attachment_classify 里的「PDF 先渲成 PNG」。借用私有名是故意的：
# 这两个模块共用**同一条读图链路**，渲页实现只许有一份。各写一份的后果
# 2026-11-01 已经量到过——那边加了渲页，这边没加，于是 A06 通知那 7 份
# 扫描 PDF 原样发出去，厂商回 400「The image format is illegal」，一次不中。
from .attachment_classify import REAL_SOURCES, _as_image
from ..llm import client

# ── 每种细类问哪些字段：唯一真相来源 ────────────────────────────────────────
# 字段名 = reader.py 读业务表时用的英文列名，保持对照方便。
# **这里面不许出现任何金额/税额键**——total_amount/tax_amount/tax_rate
# 一个都不问。
#   2026-10-22 第二轮（用户决策）：开票日期/备注栏/凭证形式/发票类型这几个
#   "文字/日期/布尔"字段补进来——**依然不问金额、税额、价税合计**。
#   发票类型只认票面写的字样（增值税专用发票/普通发票/电子发票/数电专票等），
#   不是让模型去猜税率档位。
INVOICE_COMMON_FIELDS = ["issue_date", "remark_col", "is_eticket", "inv_type"]
# inv_type 落库过一次外键校验（enum EN09），只认这 9 个值——模型自己编的
# 措辞（如「电子发票（铁路电子客票）」）写不进去，必须给菜单让它选，
# 和 attachment_classify 判类型用的是同一套「给菜单，不许自己编」纪律。
INV_TYPE_MENU = ["住宿清单", "增值税专用发票", "增值税普通发票", "收据", "数电专票",
                 "数电普票", "航空运输电子客票行程单", "铁路电子客票", "非增值税类发票"]
INVOICE_COMMON_PROMPT = (
    "开票日期: <YYYY-MM-DD，看不清写 不确定>\n"
    "发票备注栏: <备注栏/票面边角的文字原样抄，没有写 无，看不清写 不确定>\n"
    "凭证形式: <纸质/电子，看不清写 不确定>\n"
    "发票类型: <严格从这几个里选一个，不要自己编：%s；一个都不像就写 不确定>"
    % "/".join(INV_TYPE_MENU))

# ── 会议/培训通知（A06/A07）─────────────────────────────────────────────────
NOTICE_FIELDS = ["meal_provided", "shuttle",
                 "notice_start", "notice_end", "notice_place"]
NOTICE_PROMPT = (
    "是否提供用餐: <未提供/部分提供/全部提供，看不清写 不确定>\n"
    "是否提供接送车: <是/否，看不清写 不确定>\n"
    "活动起始日期: <通知正文写的活动/培训开始日期，YYYY-MM-DD；"
    "年份没写全就看文号里的年份；看不清写 不确定>\n"
    "活动结束日期: <YYYY-MM-DD，看不清写 不确定>\n"
    "活动地点: <通知正文写的举办地点，看不清写 不确定>")

# ── 业务接待审批单（A16）───────────────────────────────────────────────────
# 审批节点标签 → 存进 approval_record.node 的值。
# **右边四个值必须正好是本实例 EN16 的取值**（approval_record 有三列组合外键
# fk_appr_node → enum_value_current(instance_id, enum_id, value)）。
# 2026-11-01 实测：822gh 的 EN16 就是这四个，而且是从客户票面上读下来的。
# 模型如果读出别的节点名（比如「总经理」），**对的做法是往本体加枚举值，
# 不是放宽外键**——这里只认这四格，读出别的就丢掉并记在台账里。
A16_NODE_KEYS = [("appr_initiate", "发起节点"),
                 ("appr_dept", "部门负责人"),
                 ("appr_vice", "分管领导"),
                 ("appr_chief", "主管领导")]
# 票面字段。**故意不问「标准」「预算总额」**——那两格是金额，
# 而本模块的 SYSTEM 提示词按 2026-10-22 用户决策写死了「不要回答任何金额」。
# 要不要把审批单上的预算金额纳进来，是个**口径决策**（决策权在用户），
# 不在这一批里自己放开。两列留 NULL，TR-接待-04 也还没实现。
A16_FIELDS = ["dept", "entertain_type", "entertain_at", "meal_at",
              "meal_place", "meal_persons", "work_meal", "car_arrange",
              "lodging_arrange", "gift_arrange"] + [k for k, _ in A16_NODE_KEYS]
A16_PROMPT = (
    "接待部门: <表头「接待部门」那一格，看不清写 不确定>\n"
    "接待类型: <表头「接待类型」那一格的原文，如 商务接待/公务接待，"
    "看不清写 不确定>\n"
    "接待时间: <「接待时间」那一格的日期，YYYY-MM-DD，看不清写 不确定>\n"
    "用餐时间: <「用餐安排」那一栏里的日期，YYYY-MM-DD，"
    "没有写 无，看不清写 不确定>\n"
    "用餐场所: <「用餐安排」那一栏的「场所」，没有写 无，看不清写 不确定>\n"
    "陪餐人数: <「用餐安排」那一栏的「人数」，整数，"
    "没有写 无，看不清写 不确定>\n"
    "工作餐安排: <「工作餐/简餐安排」那一格的原文，没有写 无，看不清写 不确定>\n"
    "用车安排: <「用车安排」那一格的原文，没有写 无，看不清写 不确定>\n"
    "住宿安排: <「住宿安排」那一格的原文，没有写 无，看不清写 不确定>\n"
    "赠送纪念品: <「赠送纪念品」那一格的原文（酒水要把规格原样抄下来），"
    "没有写 无，看不清写 不确定>\n"
    "发起节点审批日期: <表单下方审批流转里「发起节点」的日期，YYYY-MM-DD，"
    "这一格没有写 无，看不清写 不确定>\n"
    "部门负责人审批日期: <YYYY-MM-DD，这一格没有写 无，看不清写 不确定>\n"
    "分管领导审批日期: <YYYY-MM-DD，这一格没有写 无，看不清写 不确定>\n"
    "主管领导审批日期: <YYYY-MM-DD，这一格没有写 无，看不清写 不确定>")

FIELD_SPECS = {
    "A01": {  # 出差审批单 → trip_application
        "table": "trip_application",
        "fields": ["start_date", "end_date", "place"],
        "prompt_fields": (
            "出差起始日期: <YYYY-MM-DD，看不清写 不确定>\n"
            "出差结束日期: <YYYY-MM-DD，看不清写 不确定>\n"
            "出差地点: <目的地城市/地区，看不清写 不确定>"),
        "keys": ["start_date", "end_date", "place"],
    },
    "A03": {  # 交通费报销凭证 → invoice + transport_voucher
        "table": "invoice+transport_voucher",
        "fields": ["title", "issuer", "detail_text", "vehicle", "seat_class",
                   "from_place", "to_place", "ride_date", "has_round_trip"]
                  + INVOICE_COMMON_FIELDS,
        "prompt_fields": (
            "发票抬头: <抬头上的单位/个人名称，看不清写 不确定>\n"
            "开票单位: <开票方名称，和铁路/航空客票可能是同一个，看不清写 不确定>\n"
            "发票明细: <一句话描述票面内容，比如车次座位信息，看不清写 不确定>\n"
            "交通工具: <火车/飞机/汽车/轮船等，看不清写 不确定>\n"
            "座位等级: <如二等座/经济舱，没有写 无，看不清写 不确定>\n"
            "出发地: <看不清写 不确定>\n"
            "到达地: <看不清写 不确定>\n"
            "乘车日期: <YYYY-MM-DD，看不清写 不确定>\n"
            "是否含往返: <是/否，只看这一张票判断不了往返就写 不确定>\n"
            + INVOICE_COMMON_PROMPT),
        "keys": ["title", "issuer", "detail_text", "vehicle", "seat_class",
                 "from_place", "to_place", "ride_date", "has_round_trip"]
                + INVOICE_COMMON_FIELDS,
    },
    "A04": {  # 住宿费发票 → invoice + hotel_invoice
        "table": "invoice+hotel_invoice",
        "fields": ["title", "issuer", "check_in", "check_out", "nights"]
                  + INVOICE_COMMON_FIELDS,
        "prompt_fields": (
            "发票抬头: <抬头上的单位/个人名称，看不清写 不确定>\n"
            "开票单位: <酒店/开票方名称，看不清写 不确定>\n"
            "入住日期: <YYYY-MM-DD，票面没有写 无，看不清写 不确定>\n"
            "离店日期: <YYYY-MM-DD，票面没有写 无，看不清写 不确定>\n"
            "住宿晚数: <整数，票面有住宿明细才填，没有写 无，看不清写 不确定>\n"
            + INVOICE_COMMON_PROMPT),
        "keys": ["title", "issuer", "check_in", "check_out", "nights"]
                + INVOICE_COMMON_FIELDS,
    },
    "A05": {  # 其他差旅费发票 → invoice
        "table": "invoice",
        "fields": ["title", "issuer", "detail_text"] + INVOICE_COMMON_FIELDS,
        "prompt_fields": (
            "发票抬头: <抬头上的单位/个人名称，看不清写 不确定>\n"
            "开票单位: <开票方名称，看不清写 不确定>\n"
            "发票明细: <一句话描述票面内容，看不清写 不确定>\n"
            + INVOICE_COMMON_PROMPT),
        "keys": ["title", "issuer", "detail_text"] + INVOICE_COMMON_FIELDS,
    },
    # A06/A07 的三个 notice_* 字段是 2026-11-01 补的（migration 0057 建的列）。
    # 补它们的理由很具体：TR-差旅-30「申报出差日期与活动日期一致性」要比
    # 「出差审批单的日期范围 vs 通知上的活动起止」，第三方一直缺。
    # 8 份通知里只有 1 份有文字层（已免费解出，正是人工驳
    # FJPG-BX2608311795「按照培训通知体现出差时间应为 8.22-8.28」的判据），
    # **其余 7 份是扫描 PDF，只能读图**。
    "A06": {  # 会议或培训通知 → meeting_notice
        "table": "meeting_notice",
        "fields": NOTICE_FIELDS,
        "prompt_fields": NOTICE_PROMPT,
        "keys": NOTICE_FIELDS,
    },
    "A07": {  # 报名回执 → meeting_notice（和 A06 同一张业务表）
        "table": "meeting_notice",
        "fields": NOTICE_FIELDS,
        "prompt_fields": NOTICE_PROMPT,
        "keys": NOTICE_FIELDS,
    },
    "A16": {  # 业务接待审批单 → entertain_approval + approval_record
        "table": "entertain_approval",
        "fields": A16_FIELDS,
        "prompt_fields": A16_PROMPT,
        "keys": A16_FIELDS,
    },
}

SYSTEM = ("你在协助财务共享中心做差旅费报销单的字段录入。只回答票面/表单上能直接看到的"
          "文字、日期、是否类信息。**不要回答任何金额、税额、价税合计类数字**——"
          "就算看得清也不要写，这次只要文字字段。看不清或拿不准的字段就如实写"
          "「不确定」，不许编。")

PROMPT_HEAD = "这是一份差旅费报销单的附件扫描图（%s）。请把下面这些字段按票面/表单上写的原样抄出来：\n\n"
PROMPT_TAIL = ("\n\n请按上面的字段逐行回答，字段名照抄，不要加 Markdown、不要多写别的内容。"
               "最后再加一行：\n把握: <高/中/低，只要有一个字段写了「不确定」或你自己吃不准，"
               "把握就不能是「高」>")


def _prompt(type_code):
    spec = FIELD_SPECS[type_code]
    return PROMPT_HEAD % spec["table"].split("+")[0] + spec["prompt_fields"] + PROMPT_TAIL


def _cfg(config_id=None):
    """复用 vision_read 这条读图配置——和 attachment_classify 共用一份模型选择。"""
    if config_id:
        row = db.query_one("SELECT * FROM llm_config WHERE config_id=%s", (config_id,))
    else:
        row = db.query_one("""SELECT * FROM llm_config
                               WHERE purpose='vision_read' AND enabled AND is_default
                               LIMIT 1""")
        if not row:
            row = db.query_one("""SELECT * FROM llm_config
                                   WHERE purpose='vision_read' AND enabled
                                   ORDER BY config_id LIMIT 1""")
    if not row:
        raise ValueError("没有可用的读图配置。去「大模型配置」配一条 purpose=vision_read 的。")
    return row


_FIELD_LABELS = {
    "出差起始日期": "start_date", "出差结束日期": "end_date", "出差地点": "place",
    "发票抬头": "title", "开票单位": "issuer", "发票明细": "detail_text",
    "交通工具": "vehicle", "座位等级": "seat_class", "出发地": "from_place",
    "到达地": "to_place", "乘车日期": "ride_date", "是否含往返": "has_round_trip",
    "入住日期": "check_in", "离店日期": "check_out", "住宿晚数": "nights",
    "开票日期": "issue_date", "发票备注栏": "remark_col",
    "凭证形式": "is_eticket", "发票类型": "inv_type",
    "是否提供用餐": "meal_provided", "是否提供接送车": "shuttle",
    # A06/A07 活动起止与地点（migration 0057 的三列）
    "活动起始日期": "notice_start", "活动结束日期": "notice_end",
    "活动地点": "notice_place",
    # A16 业务接待审批单票面字段 + 四个审批节点日期
    "接待部门": "dept", "接待类型": "entertain_type", "接待时间": "entertain_at",
    "用餐时间": "meal_at", "用餐场所": "meal_place", "陪餐人数": "meal_persons",
    "工作餐安排": "work_meal", "用车安排": "car_arrange",
    "住宿安排": "lodging_arrange", "赠送纪念品": "gift_arrange",
    "发起节点审批日期": "appr_initiate", "部门负责人审批日期": "appr_dept",
    "分管领导审批日期": "appr_vice", "主管领导审批日期": "appr_chief",
}
# **标签必须是纯汉字**：_LINE_RE 是 `([一-龥]+)[:：]`，带斜杠或括号的标签
# （比如票面原文「工作餐/简餐安排」）整行匹配不上，字段会静默全空。
# 所以 A16_PROMPT 里问的是「工作餐安排」，而提示词的说明里写明它对应票面哪一格。
assert all(re.fullmatch(r"[一-龥]+", k) for k in _FIELD_LABELS), \
    "字段标签只许用汉字，否则 _LINE_RE 匹配不上"
# 日期字段：只认 YYYY-MM-DD 这一种落库形态（_clean 会把 2026年4月22日 之类
# 也规整过来）。A16 的四个节点日期和接待/用餐时间都在这里。
_DATE_KEYS = ("start_date", "end_date", "ride_date", "check_in", "check_out",
              "issue_date", "notice_start", "notice_end",
              "entertain_at", "meal_at") + tuple(k for k, _ in A16_NODE_KEYS)
_INT_KEYS = ("nights", "meal_persons")
_LINE_RE = re.compile(r"([一-龥]+)[:：]\s*(.+)")
_CONF_RE = re.compile(r"把握[:：]\s*([高中低])")

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def _clean(label_cn, raw):
    """把一行原始回答转成可以入库的值。**「不确定」「无」一律变成 None，不猜。**"""
    raw = (raw or "").strip()
    if not raw or raw in ("不确定", "无", "无法判断", "看不清"):
        return None
    key = _FIELD_LABELS.get(label_cn)
    if key in _DATE_KEYS:
        m = _DATE_RE.search(raw)
        if not m:
            return None
        y, mo, d = m.groups()
        # 月/日越界一律判不出。读图把「13 月」这种读出来过，而
        # entertain_at 是 TIMESTAMPTZ，插进去直接 500——
        # **拿不到比拿错好**（TR-接待-01 对日期缺失的反应是 pending，不是命中）。
        if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
            return None
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))
    if key in _INT_KEYS:
        m = re.search(r"\d+", raw)
        return int(m.group()) if m else None
    if key in ("has_round_trip", "shuttle"):
        if raw.startswith("是"):
            return True
        if raw.startswith("否"):
            return False
        return None
    if key == "is_eticket":
        if "电子" in raw:
            return True
        if "纸质" in raw or "纸" in raw:
            return False
        return None
    if key == "entertain_type":
        # 票面是「公务接待（ ）　商务接待（√）」这种勾选格，模型会把勾一起
        # 抄回来（实测「商务接待（√）」）。勾是**标记**不是值，去掉它——
        # 留着会让「商务接待」和「商务接待（√）」变成两个不同的取值。
        return re.sub(r"[（(]\s*[√✓✔xX×对勾]?\s*[)）]", "", raw).strip() or None
    if key == "inv_type":
        # 模型没按菜单回答（编了个不在 EN09 里的说法），当判不出，不强行纠正——
        # 猜它想选哪个，比直接判不出更危险（而且这列有外键，编错了直接 500）。
        return raw if raw in INV_TYPE_MENU else None
    return raw


def _parse(content, type_code):
    spec = FIELD_SPECS[type_code]
    fields = {}
    for line in (content or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        label_cn, raw = m.group(1), m.group(2)
        key = _FIELD_LABELS.get(label_cn)
        if key and key in spec["keys"]:
            fields[key] = _clean(label_cn, raw)
    conf_m = _CONF_RE.search(content or "")
    confidence = conf_m.group(1) if conf_m else None
    return fields, confidence


def extract_one(attachment_id, config_id=None, max_tokens=600):
    """抽一张附件的字段。**一次调用，一行台账。**

    只有 type_code 在 FIELD_SPECS 里（已判过类型、且是决策表要求读图的发票/
    审批单类）才会被抽；类型判不出或不在这四种里的，直接拒绝——
    不读图去猜它该问哪些字段。
    """
    row = db.query_one("""SELECT id, bill_no, file_name, stored_path, source_kind,
                                 type_code, instance_id
                            FROM attachment WHERE id=%s""", (attachment_id,))
    if not row:
        raise ValueError("没有这条附件：%s" % attachment_id)
    if row["source_kind"] not in REAL_SOURCES:
        raise ValueError("「%s」的来源是「%s」，不在真实客户凭证之列（%s），"
                         "不做字段抽取"
                         % (row["file_name"], row["source_kind"],
                            "、".join(REAL_SOURCES)))
    type_code = row["type_code"]
    if type_code not in FIELD_SPECS:
        raise ValueError("「%s」的类型是「%s」，不在字段抽取范围内（只抽 %s）"
                         % (row["file_name"], type_code or "未判定",
                            "/".join(FIELD_SPECS)))
    if not row["stored_path"]:
        raise ValueError("这条附件没有原图路径：%s" % row["file_name"])

    # PDF 要先渲成 PNG。**渲页放在拿 cfg 之前**：渲不出来就抛，一次调用都不花。
    img_path = _as_image(row["stored_path"])

    cfg = _cfg(config_id)
    attempt = (db.scalar("""SELECT max(attempt) FROM attachment_extract
                             WHERE attachment_id=%s AND model_name=%s""",
                         (attachment_id, cfg["model_name"])) or 0) + 1

    res, content, use = client.chat_vision(cfg, SYSTEM, _prompt(type_code),
                                           img_path, max_tokens=max_tokens)
    fields, confidence = _parse(content, type_code) if res.ok else ({}, None)

    eid = db.scalar("""
        INSERT INTO attachment_extract(
            attachment_id, attempt, config_id, model_name, model_echo,
            ok, err_kind, message, content, type_code, fields, confidence,
            latency_ms, prompt_tokens, completion_tokens)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        (attachment_id, attempt, cfg["config_id"], cfg["model_name"], res.model_echo,
         res.ok, res.err_kind, res.message, content or None,
         type_code, json.dumps(fields, ensure_ascii=False) if fields else None, confidence,
         res.latency_ms, use.get("prompt_tokens"), use.get("completion_tokens")))

    applied, applied_to = False, None
    # **「高」或「中」才回写，「低」不回写。** 和判类型（只认「高」）不同的地方：
    # 判类型是单一判断，混一点不确定就该整条不信；字段抽取一次问 3~9 个字段，
    # 任何一个字段答「不确定」（已经在 _clean 里变成 None 了）都会把模型自报的
    # 整体把握拉到「中」——但那不代表**其它已经读清楚的字段**也不可信。
    # 2026-10-22 实测：A03 车票整单「中」，是因为「是否含往返」答不出来，
    # 车次/出发到达站/日期全部读对，不能因为一个字段不确定就把其余字段也扔掉。
    # 「低」才是模型自己说连大部分都吃不准，那时整条不回写。
    if res.ok and confidence in ("高", "中") and fields:
        applied_to = _apply(type_code, row["bill_no"], attachment_id, fields,
                           cfg["model_name"], confidence, row["instance_id"])
        applied = bool(applied_to)

    if applied_to:
        db.execute("UPDATE attachment_extract SET applied=%s, applied_to=%s WHERE id=%s",
                   (applied, applied_to, eid))

    return {
        "attachment_id": attachment_id, "bill_no": row["bill_no"],
        "file_name": row["file_name"], "extract_id": eid,
        "ok": res.ok, "err_kind": res.err_kind, "message": res.message,
        "type_code": type_code, "fields": fields, "confidence": confidence,
        "applied": applied, "applied_to": applied_to,
        "model": cfg["model_name"], "latency_ms": res.latency_ms, "tokens": use,
    }


def _apply(type_code, bill_no, attachment_id, f, model_name, confidence,
           instance_id):
    """把「高/中」把握的字段回写进业务表。**没有任何金额/税额键会被写进这里**——

    f 的键只可能来自 FIELD_SPECS[type_code]['keys']，那张表里压根没有
    total_amount/tax_amount/tax_rate。这不是运行时过滤，是编译期就不存在。

    `instance_id` 是 2026-11-01 补的形参。补它的起因是个实测缺陷：
    A06/A07 这条分支往 meeting_notice 插行时**没给 instance_id**，
    而那一列是 NOT NULL——也就是说会议通知的字段回写**从来没跑通过**，
    一跑就是 NOT NULL 违反。全库 meeting_notice 当时只有 1 行，
    是文字层程序写的，所以没人撞见。
    """
    source = "vision:%s:attachment=%s" % (model_name, attachment_id)
    conf_score = {"高": 0.90, "中": 0.60}.get(confidence, 0.60)
    inv_id = "ATT-%s" % attachment_id
    if type_code == "A01":
        # ── 三道闸，都是 2026-11-01 读图批实测到的脏值 ──────────────────
        # 这些值不会报错，它们会**安静地让规则误报**，所以必须在入库前拦。
        #
        # 1) 年份读错十年。实测 image_t0000000896_n1.pdf 读回 2016-08-13，
        #    而报账单是 2026-08。TR-通用-02 判「逐日表有没有覆盖出差区间」，
        #    一个 2016 年的起始日期会让区间变成十年，整张单必然命中。
        #    基准取 expense_bill.doc_date（提单日期）。822港航 29/29 有值
        #    （migration 0058 从单据号前六位回填的）。**取不到就不设这道闸**——
        #    没有基准还硬判，等于拿一个猜来的年份去否决一个读出来的年份。
        ref = db.query_one("SELECT doc_date FROM expense_bill WHERE bill_no=%s",
                           (bill_no,))
        ref_y = ref and ref["doc_date"] and ref["doc_date"].year
        for k in ("start_date", "end_date"):
            v = f.get(k)
            if v and ref_y and abs(int(v[:4]) - ref_y) > 1:
                f = dict(f)
                f[k] = None
        # 2) 起止反了就两个都丢（同 A06 的处理，理由也一样：宁可取不到）。
        if f.get("start_date") and f.get("end_date") \
                and f["start_date"] > f["end_date"]:
            f = dict(f, start_date=None, end_date=None)
        # 3) 「出差地点」读成了出差事由。实测 雒朔出差审批单.jpg 读回
        #    「前往罗屿上石方项目管促，参加湄江公司开港签约仪式」——
        #    TR-差旅-28 拿 place 切二字片段去比住宿发票的开票方，
        #    一句话能切出几十个片段，**几乎必然蒙中一个**，
        #    于是本该报「地点不一致」的单被判成一致：这是漏报。
        place = f.get("place")
        if place and (len(place) > 20 or re.search(r"[，,。；;]", place)):
            f = dict(f, place=None)
        if not any(f.get(k) for k in ("start_date", "end_date", "place")):
            return None
        days = None
        if f.get("start_date") and f.get("end_date"):
            from datetime import date
            y1, m1, d1 = (int(x) for x in f["start_date"].split("-"))
            y2, m2, d2 = (int(x) for x in f["end_date"].split("-"))
            days = (date(y2, m2, d2) - date(y1, m1, d1)).days + 1
        db.execute("""
            INSERT INTO trip_application(app_no, bill_no, start_date, end_date,
                place, days, synced_from_oa, extracted_from)
            VALUES (%s,%s,%s,%s,%s,%s,FALSE,'审批单读图')
            ON CONFLICT (app_no) DO UPDATE SET
                start_date=EXCLUDED.start_date, end_date=EXCLUDED.end_date,
                place=EXCLUDED.place, days=EXCLUDED.days,
                extracted_from=EXCLUDED.extracted_from""",
            (inv_id, bill_no, f.get("start_date"), f.get("end_date"),
             f.get("place"), days))
        return "trip_application:%s" % inv_id
    if type_code in ("A03", "A04", "A05"):
        core_keys = ("title", "issuer", "detail_text",
                    "issue_date", "remark_col", "is_eticket", "inv_type")
        if not any(f.get(k) is not None for k in core_keys):
            invoice_written = False
        else:
            db.execute("""
                INSERT INTO invoice(invoice_id, bill_no, title, issuer, detail_text,
                    issue_date, remark_col, is_eticket, inv_type,
                    ocr_source, ocr_confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (invoice_id) DO UPDATE SET
                    title=COALESCE(EXCLUDED.title, invoice.title),
                    issuer=COALESCE(EXCLUDED.issuer, invoice.issuer),
                    detail_text=COALESCE(EXCLUDED.detail_text, invoice.detail_text),
                    issue_date=COALESCE(EXCLUDED.issue_date, invoice.issue_date),
                    remark_col=COALESCE(EXCLUDED.remark_col, invoice.remark_col),
                    is_eticket=COALESCE(EXCLUDED.is_eticket, invoice.is_eticket),
                    inv_type=COALESCE(EXCLUDED.inv_type, invoice.inv_type),
                    ocr_source=EXCLUDED.ocr_source, ocr_confidence=EXCLUDED.ocr_confidence""",
                (inv_id, bill_no, f.get("title"), f.get("issuer"), f.get("detail_text"),
                 f.get("issue_date"), f.get("remark_col"), f.get("is_eticket"), f.get("inv_type"),
                 source, conf_score))
            invoice_written = True
        if type_code == "A04" and (f.get("check_in") or f.get("check_out") or f.get("nights")):
            if not invoice_written:
                db.execute("""INSERT INTO invoice(invoice_id, bill_no)
                               VALUES (%s,%s) ON CONFLICT (invoice_id) DO NOTHING""",
                           (inv_id, bill_no))
            nights = f.get("nights")
            if nights is None and f.get("check_in") and f.get("check_out"):
                from datetime import date
                y1, m1, d1 = (int(x) for x in f["check_in"].split("-"))
                y2, m2, d2 = (int(x) for x in f["check_out"].split("-"))
                nights = (date(y2, m2, d2) - date(y1, m1, d1)).days
            db.execute("""
                INSERT INTO hotel_invoice(invoice_id, check_in, check_out, nights,
                    ocr_source, ocr_confidence)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (invoice_id) DO UPDATE SET
                    check_in=EXCLUDED.check_in, check_out=EXCLUDED.check_out,
                    nights=EXCLUDED.nights, ocr_source=EXCLUDED.ocr_source,
                    ocr_confidence=EXCLUDED.ocr_confidence""",
                (inv_id, f.get("check_in"), f.get("check_out"), nights, source, confidence))
            return "invoice+hotel_invoice:%s" % inv_id
        if type_code == "A03" and any(f.get(k) for k in
                ("from_place", "to_place", "ride_date", "has_round_trip", "vehicle")):
            if not invoice_written:
                db.execute("""INSERT INTO invoice(invoice_id, bill_no)
                               VALUES (%s,%s) ON CONFLICT (invoice_id) DO NOTHING""",
                           (inv_id, bill_no))
            voucher_id = "VCH-%s" % attachment_id
            db.execute("""
                INSERT INTO transport_voucher(voucher_id, bill_no, invoice_id, vehicle,
                    seat_class, from_place, to_place, ride_date, has_round_trip,
                    ocr_source, ocr_confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (voucher_id) DO UPDATE SET
                    vehicle=EXCLUDED.vehicle, seat_class=EXCLUDED.seat_class,
                    from_place=EXCLUDED.from_place, to_place=EXCLUDED.to_place,
                    ride_date=EXCLUDED.ride_date, has_round_trip=EXCLUDED.has_round_trip,
                    ocr_source=EXCLUDED.ocr_source, ocr_confidence=EXCLUDED.ocr_confidence""",
                (voucher_id, bill_no, inv_id, f.get("vehicle"), f.get("seat_class"),
                 f.get("from_place"), f.get("to_place"), f.get("ride_date"),
                 f.get("has_round_trip"), source, confidence))
            return "invoice+transport_voucher:%s" % inv_id
        return "invoice:%s" % inv_id if invoice_written else None
    if type_code in ("A06", "A07"):
        if not any(f.get(k) is not None for k in NOTICE_FIELDS):
            return None
        # 起止反了就两个都丢掉。**宁可取不到，不许取错**：notice_start/end 是
        # TR-差旅-30 的第三方判据，反过来的区间会让正常单据误报。
        ns, ne = f.get("notice_start"), f.get("notice_end")
        if ns and ne and ns > ne:
            ns = ne = None
        # meeting_notice 是 UNIQUE(bill_no)，一张报账单只存一行。
        # 实测 FJPG-BX2607280255 挂了两份通知（观摩学习通知 + 邀请函），
        # 第二份按字段 COALESCE 并进第一份——**先到先得，不覆盖已有值**。
        # 这是现状口径（原来就是 COALESCE），不在这一批里改成一对多。
        db.execute("""
            INSERT INTO meeting_notice(bill_no, meal_provided, shuttle,
                notice_start, notice_end, notice_place, instance_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (bill_no) DO UPDATE SET
                meal_provided=COALESCE(meeting_notice.meal_provided, EXCLUDED.meal_provided),
                shuttle=COALESCE(meeting_notice.shuttle, EXCLUDED.shuttle),
                notice_start=COALESCE(meeting_notice.notice_start, EXCLUDED.notice_start),
                notice_end=COALESCE(meeting_notice.notice_end, EXCLUDED.notice_end),
                notice_place=COALESCE(meeting_notice.notice_place, EXCLUDED.notice_place)""",
            (bill_no, f.get("meal_provided"), f.get("shuttle"),
             ns, ne, f.get("notice_place"), instance_id))
        return "meeting_notice:%s" % bill_no
    if type_code == "A16":
        # 一张报账单可以挂多份业务接待审批单（实测最多 9 份），所以
        # app_no 按**附件**取，不按报账单取——否则 9 份互相覆盖成 1 份。
        app_no = "ATT-%s" % attachment_id
        node_dates = [(node, f.get(key)) for key, node in A16_NODE_KEYS
                      if f.get(key)]
        # **「接待时间」是这张表的身份证：没有它就不建行。**
        #
        # 这道闸收紧过一次，是实测逼出来的。A16 这一桶里混着别的单子：
        #   · `简餐审批单`（FJPG-BX2603230510 一张单挂了 9 份）
        #   · `纪念品、酒水领用申请单`（附件6，被定类成了 A16）
        # 第一版的闸是「有接待时间**或**有任一审批日期」，结果 3 份简餐审批单
        # 和 1 份酒水领用单靠一个审批日期就建了行——票面上根本没有「接待时间」
        # 那一格，建出来的是只有 work_meal='简餐审批单' 的空壳。
        #
        # 为什么宁可漏不可错：两种错的代价不对称。
        #   漏一份真审批单 → TR-接待-01 对那份 pending
        #   混进一份假审批单 → 同样 pending，**但还会把「审批单份数」这个
        #   对外报的数字做大**，并且让「这张单的接待审批已核查」看起来成立。
        # 后者是假结论，前者只是没结论。
        if not f.get("entertain_at"):
            return None
        db.execute("""
            INSERT INTO entertain_approval(app_no, bill_no, dept, entertain_type,
                entertain_at, meal_at, meal_place, meal_persons, work_meal,
                car_arrange, lodging_arrange, gift_arrange,
                extracted_from, instance_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'审批单读图',%s)
            ON CONFLICT (app_no) DO UPDATE SET
                dept=EXCLUDED.dept, entertain_type=EXCLUDED.entertain_type,
                entertain_at=EXCLUDED.entertain_at, meal_at=EXCLUDED.meal_at,
                meal_place=EXCLUDED.meal_place, meal_persons=EXCLUDED.meal_persons,
                work_meal=EXCLUDED.work_meal, car_arrange=EXCLUDED.car_arrange,
                lodging_arrange=EXCLUDED.lodging_arrange,
                gift_arrange=EXCLUDED.gift_arrange,
                extracted_from=EXCLUDED.extracted_from""",
            (app_no, bill_no, f.get("dept"), f.get("entertain_type"),
             f.get("entertain_at"), f.get("meal_at"), f.get("meal_place"),
             f.get("meal_persons"), f.get("work_meal"), f.get("car_arrange"),
             f.get("lodging_arrange"), f.get("gift_arrange"), instance_id))
        # 审批流水：先按**这份审批单**删、再插（migration 0060 的 app_no 就是
        # 为这一句加的）。按 bill_no 删会连带删掉同一张单上另外几份审批单的流水。
        # 读图只拿到**日期**，没有时分秒：TR-接待-01 的口径本来就是按日期比
        # （见 migration 0057 的列注释：审批单上的接待时间是计划时间，
        # 实测有 3 分钟的填报噪声），所以不去问时分秒、也不编一个。
        # 存进 TIMESTAMPTZ 是当天零点，**approver 一律留 NULL**——
        # 签批人姓名不问（规则不用，而让模型抄外部人名没有必要）。
        # node 落的是 EN16 的取值（node_enum 是 GENERATED ALWAYS AS 'EN16'，
        # 外键 fk_appr_node 按 (instance_id, 'EN16', node) 校验）——所以这里
        # 只插 A16_NODE_KEYS 右边那四个值，模型读出别的节点名在上面就被丢掉了。
        db.execute("DELETE FROM approval_record WHERE app_no=%s AND instance_id=%s",
                   (app_no, instance_id))
        for seq, (node, d) in enumerate(node_dates, start=1):
            db.execute("""
                INSERT INTO approval_record(bill_no, seq, node, approver, opinion,
                    approved_at, flow, app_no, extracted_from, instance_id)
                VALUES (%s,%s,%s,NULL,NULL,%s,'业务接待审批',%s,'审批单读图',%s)""",
                (bill_no, seq, node, d, app_no, instance_id))
        return "entertain_approval:%s（审批流水 %d 条）" % (app_no, len(node_dates))
    return None


def _iid(instance_id=None):
    """当前实例。理由同 attachment_classify._iid：**这条链路是计费的、
    而且发出去的是客户真凭证**，不按实例过滤就是范围失控。"""
    from .. import doclib
    return doclib.resolve_instance(instance_id)["instance_id"]


def pending(limit=500, instance_id=None):
    """本实例里已判过类型、类型在抽取范围内、但还没抽过字段的真实附件。"""
    return db.query("""SELECT id, bill_no, file_name, type_code
                         FROM attachment
                        WHERE instance_id = %s AND source_kind = ANY(%s)
                          AND type_code = ANY(%s)
                          AND NOT EXISTS (SELECT 1 FROM attachment_extract e
                                          WHERE e.attachment_id = attachment.id)
                        ORDER BY bill_no LIMIT %s""",
                    (_iid(instance_id), list(REAL_SOURCES), list(FIELD_SPECS),
                     max(1, min(2000, int(limit)))))


def run(attachment_ids=None, limit=20, config_id=None, instance_id=None):
    """批量抽字段。**真出网、真计费**，一次最多 limit 条。

    显式给的 id 同样按实例过滤：给错一个别家的 id，它就会被发出去。
    """
    iid = _iid(instance_id)
    if attachment_ids is not None:
        rows = db.query("""SELECT id, bill_no, file_name FROM attachment
                            WHERE id = ANY(%s) AND instance_id = %s
                              AND source_kind = ANY(%s)
                            ORDER BY bill_no""",
                        (list(attachment_ids), iid, list(REAL_SOURCES)))
    else:
        rows = pending(limit, instance_id=iid)
    rows = rows[:max(1, min(100, int(limit)))]

    items, applied, failed = [], 0, 0
    for r in rows:
        try:
            res = extract_one(r["id"], config_id=config_id)
        except ValueError as e:
            items.append({"attachment_id": r["id"], "bill_no": r["bill_no"],
                         "file_name": r["file_name"], "ok": False,
                         "message": str(e)})
            failed += 1
            continue
        items.append(res)
        if res["applied"]:
            applied += 1
        if not res["ok"]:
            failed += 1
    return {"tried": len(rows), "applied": applied, "failed": failed, "items": items}


def status(instance_id=None):
    """**按实例**：两家的进度和费用加在一起就分不清谁花的。"""
    iid = _iid(instance_id)
    total_typed = db.scalar("""SELECT count(*) FROM attachment
                                WHERE instance_id=%s AND source_kind = ANY(%s)
                                  AND type_code = ANY(%s)""",
                            (iid, list(REAL_SOURCES), list(FIELD_SPECS))) or 0
    extracted = db.scalar("""SELECT count(DISTINCT e.attachment_id)
                               FROM attachment_extract e
                               JOIN attachment a ON a.id = e.attachment_id
                              WHERE a.instance_id = %s""", (iid,)) or 0
    applied = db.scalar("""SELECT count(DISTINCT e.attachment_id)
                             FROM attachment_extract e
                             JOIN attachment a ON a.id = e.attachment_id
                            WHERE a.instance_id = %s AND e.applied""", (iid,)) or 0
    calls = db.query_one("""SELECT count(*) AS n,
                                   count(*) FILTER (WHERE NOT e.ok) AS failed,
                                   sum(e.prompt_tokens) AS pt,
                                   sum(e.completion_tokens) AS ct
                              FROM attachment_extract e
                              JOIN attachment a ON a.id = e.attachment_id
                             WHERE a.instance_id = %s""", (iid,)) or {}
    # **按实例**。这一句原来没有实例条件，而它上面四句都有——
    # 两家的把握分布加在一起，这份「本实例进度」报表里就夹着别家的数字。
    by_conf = db.query("""SELECT e.confidence, count(*) n
                            FROM attachment_extract e
                            JOIN attachment a ON a.id = e.attachment_id
                           WHERE a.instance_id = %s AND e.confidence IS NOT NULL
                           GROUP BY 1 ORDER BY 2 DESC""", (iid,))
    return {
        "eligible": total_typed, "extracted": extracted, "applied": applied,
        "pending": total_typed - extracted,
        "calls": int(calls.get("n") or 0), "calls_failed": int(calls.get("failed") or 0),
        "prompt_tokens": int(calls.get("pt") or 0),
        "completion_tokens": int(calls.get("ct") or 0),
        "by_confidence": by_conf,
        "note": "只抽文字/日期/布尔字段，不问金额税额（2026-10-22 用户决策）。"
                "把握到「高」才回写 invoice/hotel_invoice/transport_voucher/"
                "trip_application，其余原样留空，记录都在本表。",
    }
