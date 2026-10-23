# -*- coding: utf-8 -*-
"""单据附件字段抽取。**真出网、真计费**，是 0037 判类型之后的第二步。

0037 解决「这张图是什么类型」；本模块解决「这张图上写了什么字段值」。
两者刻意分两次调用、两套提示词——判类型的提示词只问类型，字段抽取的
提示词只问字段，混在一次问里模型会顾此失彼。

**2026-10-22 用户决策：这一轮只抽文字/日期/布尔字段，金额税额先不落明文。**
价税合计/税额/税率这一次提示词里都不问，模型答了也不解析、不落库——
不是「解析了但不回写」，是**压根不进解析器**，杜绝它以后被哪个新代码当字段用。

和 attachment_classify 同样的三条纪律：
  1. 判不出就是 NULL，不猜
  2. 把握不到「高」不回写到 invoice / hotel_invoice / transport_voucher /
     trip_application 这些业务表（但调用记录照落 attachment_extract）
  3. 成败都落台账，永不覆盖（同一张图重读是新 attempt）
"""
import json
import re

from .. import db
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
    "A06": {  # 会议或培训通知 → meeting_notice
        "table": "meeting_notice",
        "fields": ["meal_provided", "shuttle"],
        "prompt_fields": (
            "是否提供用餐: <未提供/部分提供/全部提供，看不清写 不确定>\n"
            "是否提供接送车: <是/否，看不清写 不确定>"),
        "keys": ["meal_provided", "shuttle"],
    },
    "A07": {  # 报名回执 → meeting_notice（和 A06 同一张业务表）
        "table": "meeting_notice",
        "fields": ["meal_provided", "shuttle"],
        "prompt_fields": (
            "是否提供用餐: <未提供/部分提供/全部提供，看不清写 不确定>\n"
            "是否提供接送车: <是/否，看不清写 不确定>"),
        "keys": ["meal_provided", "shuttle"],
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
}
_LINE_RE = re.compile(r"([一-龥]+)[:：]\s*(.+)")
_CONF_RE = re.compile(r"把握[:：]\s*([高中低])")

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def _clean(label_cn, raw):
    """把一行原始回答转成可以入库的值。**「不确定」「无」一律变成 None，不猜。**"""
    raw = (raw or "").strip()
    if not raw or raw in ("不确定", "无", "无法判断", "看不清"):
        return None
    key = _FIELD_LABELS.get(label_cn)
    if key in ("start_date", "end_date", "ride_date", "check_in", "check_out", "issue_date"):
        m = _DATE_RE.search(raw)
        if not m:
            return None
        y, mo, d = m.groups()
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))
    if key == "nights":
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
    row = db.query_one("""SELECT id, bill_no, file_name, stored_path, source_kind, type_code
                            FROM attachment WHERE id=%s""", (attachment_id,))
    if not row:
        raise ValueError("没有这条附件：%s" % attachment_id)
    if row["source_kind"] != "xlsx内嵌":
        raise ValueError("「%s」不是从 xlsx 抽出来的真实附件，不做字段抽取" % row["file_name"])
    type_code = row["type_code"]
    if type_code not in FIELD_SPECS:
        raise ValueError("「%s」的类型是「%s」，不在字段抽取范围内（只抽 %s）"
                         % (row["file_name"], type_code or "未判定",
                            "/".join(FIELD_SPECS)))
    if not row["stored_path"]:
        raise ValueError("这条附件没有原图路径：%s" % row["file_name"])

    cfg = _cfg(config_id)
    attempt = (db.scalar("""SELECT max(attempt) FROM attachment_extract
                             WHERE attachment_id=%s AND model_name=%s""",
                         (attachment_id, cfg["model_name"])) or 0) + 1

    res, content, use = client.chat_vision(cfg, SYSTEM, _prompt(type_code),
                                           row["stored_path"], max_tokens=max_tokens)
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
                           cfg["model_name"], confidence)
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


def _apply(type_code, bill_no, attachment_id, f, model_name, confidence):
    """把「高/中」把握的字段回写进业务表。**没有任何金额/税额键会被写进这里**——

    f 的键只可能来自 FIELD_SPECS[type_code]['keys']，那张表里压根没有
    total_amount/tax_amount/tax_rate。这不是运行时过滤，是编译期就不存在。
    """
    source = "vision:%s:attachment=%s" % (model_name, attachment_id)
    conf_score = {"高": 0.90, "中": 0.60}.get(confidence, 0.60)
    inv_id = "ATT-%s" % attachment_id
    if type_code == "A01":
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
        if not any(f.get(k) is not None for k in ("meal_provided", "shuttle")):
            return None
        db.execute("""
            INSERT INTO meeting_notice(bill_no, meal_provided, shuttle)
            VALUES (%s,%s,%s)
            ON CONFLICT (bill_no) DO UPDATE SET
                meal_provided=COALESCE(EXCLUDED.meal_provided, meeting_notice.meal_provided),
                shuttle=COALESCE(EXCLUDED.shuttle, meeting_notice.shuttle)""",
            (bill_no, f.get("meal_provided"), f.get("shuttle")))
        return "meeting_notice:%s" % bill_no
    return None


def pending(limit=500):
    """已判过类型、类型在抽取范围内、但还没抽过字段的真实附件。"""
    codes = list(FIELD_SPECS)
    return db.query("""SELECT id, bill_no, file_name, type_code
                         FROM attachment
                        WHERE source_kind='xlsx内嵌' AND type_code = ANY(%s)
                          AND NOT EXISTS (SELECT 1 FROM attachment_extract e
                                          WHERE e.attachment_id = attachment.id)
                        ORDER BY bill_no LIMIT %s""",
                    (codes, max(1, min(2000, int(limit))),))


def run(attachment_ids=None, limit=20, config_id=None):
    """批量抽字段。**真出网、真计费**，一次最多 limit 条。"""
    if attachment_ids is not None:
        rows = db.query("""SELECT id, bill_no, file_name FROM attachment
                            WHERE id = ANY(%s) AND source_kind='xlsx内嵌'
                            ORDER BY bill_no""", (list(attachment_ids),))
    else:
        rows = pending(limit)
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


def status():
    total_typed = db.scalar("""SELECT count(*) FROM attachment
                                WHERE source_kind='xlsx内嵌'
                                  AND type_code = ANY(%s)""",
                            (list(FIELD_SPECS),)) or 0
    extracted = db.scalar("""SELECT count(DISTINCT attachment_id) FROM attachment_extract""") or 0
    applied = db.scalar("""SELECT count(DISTINCT attachment_id) FROM attachment_extract
                            WHERE applied""") or 0
    calls = db.query_one("""SELECT count(*) AS n, count(*) FILTER (WHERE NOT ok) AS failed,
                                   sum(prompt_tokens) AS pt, sum(completion_tokens) AS ct
                              FROM attachment_extract""") or {}
    by_conf = db.query("""SELECT confidence, count(*) n FROM attachment_extract
                          WHERE confidence IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
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
