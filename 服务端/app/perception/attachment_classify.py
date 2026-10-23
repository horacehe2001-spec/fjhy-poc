# -*- coding: utf-8 -*-
"""单据附件读图判类型。**真出网、真计费**，而且是本系统第二敏感的出口。

0035 把 219 张内嵌附件抽进来了，但 attach_type 全部留空——TR-D6-01（必要附件
完整性）要的是类型，从一张 PNG 判它是出差审批单还是住宿费发票必须读图。

**为什么比 app/vision.py（制度表格读图）更敏感**：
那边发出去的是客户的制度文件页；这里发出去的是**真实发票与出差申请单**，
票面上有姓名、身份证号片段、金额、车次航班。所以每次调用都要落一行
attachment_vision（成败都落），事后必须答得出「哪些客户凭证离开过本系统」。

──────────────────────────────────────────────────────────────────────────
【最要紧的一条：细类编码 ≠ 字段值，两者不是 1:1】
──────────────────────────────────────────────────────────────────────────
必要附件决策表用的是 A01..A13 细类编码；attachment.attach_type 的外键是
enum EN17，只有 12 个值，把其中三组合并了（A01+A02、A06+A07、A10+A11）。
模型答「出差审批单」四个字写不进 attach_type（外键会拒绝那个值）。

所以 TYPES 表是本模块唯一的真相来源：一次性定义
    细类编码 → (中文名, 能不能直接判定, EN17 值, 决策表是否要求读图)
别的地方不许再抄一份这个映射——抄两份，口径一变就会漏改一处。

──────────────────────────────────────────────────────────────────────────
【三条纪律】
──────────────────────────────────────────────────────────────────────────
1. **判不出就是 NULL，不猜。** 模型答「看不清」「像是发票但型号不确定」这类，
   一律落 type_code=NULL。TR-D6-01 看到 NULL 必须报「判不出」，
   不许当「缺件」去拦单——那会把这套系统的误报率从 0% 做到 100%。

2. **把握「低」的判定不写进 attach_type。** 本项目在制度表格读图上已经实测过：
   模型会把看不清的格子编出来，而且编得通顺。一个错的类型比空值危险——
   空值会让规则报「判不出」，错类型会让规则给出一个看着自信、实则错误的结论。

3. **成败都落台账，永不覆盖。** 同一张图重读（失败重试、换模型对照）落新的
   attempt，不改旧行。这不是为了好看，是因为**这条链路发出去的是客户凭证**，
   少一行就少一条「发给谁了」的证据。
"""
import re

from .. import db
from ..llm import client

# ── 类型字典：唯一真相来源 ───────────────────────────────────────────────────
# code: (中文名, EN17 值, 决策表要求读图)
# A14/A15 是 EN17 里有、但必要附件决策表没有对应条目的两种
# （「情况说明」「预算调整审批材料」）——票面上确实可能是这两种，
# 不给它们编号的话，模型遇到这类票只能瞎选一个硬塞，那比判不出更坏。
TYPES = {
    "A01": ("出差审批单",       "出差审批单/差旅费明细表", False),
    "A02": ("差旅费明细表",     "出差审批单/差旅费明细表", False),
    "A03": ("交通费报销凭证",   "交通费报销凭证",          True),
    "A04": ("住宿费发票",       "住宿费发票",               True),
    "A05": ("其他差旅费发票",   "其他差旅费发票",           True),
    "A06": ("会议或培训通知",   "会议或培训通知/报名回执", True),
    "A07": ("报名回执",         "会议或培训通知/报名回执", True),
    "A08": ("租车合同或协议",   "租车合同或协议",          False),
    "A09": ("租房合同",         "租房合同",                False),
    "A10": ("安全资金确认表",   "安全资金确认表/使用表",   False),
    "A11": ("安全资金使用表",   "安全资金确认表/使用表",   False),
    "A12": ("部门审核的补贴清单", "部门审核的补贴清单",    False),
    "A13": ("事权审批单",       "事权审批单",              False),
    "A14": ("情况说明",         "情况说明",                True),
    "A15": ("预算调整审批材料", "预算调整审批材料",        True),
}

CONF_LEVELS = ("高", "中", "低")

SYSTEM = ("你在协助财务共享中心做差旅费报销单的附件归档。只回答票面上能直接看到的"
          "信息，看不清或拿不准就如实说看不清——**不许因为常见就猜一个类型**。")

PROMPT = """这是一份差旅费报销单的附件扫描图。请判断它属于下面哪一种类型，
严格按给出的编码回答，不要自己编编码或名称：

%s

请**只**按这个格式回答，四行，不要多写也不要用 Markdown：
类型编码: <上面列表里的编码，或 无法判断>
把握: <高/中/低>
依据: <你在票面上具体看到了什么，比如标题文字、票据种类、印章字样——一句话>
备注: <有疑虑的地方，没有就写 无>

"无法判断"的情况包括但不限于：图片模糊看不清标题、像两种类型但拿不准选哪个、
票面内容和列表里任何一种都对不上。**宁可答"无法判断"，不要猜一个接近的。**"""


def _type_menu():
    return "\n".join("%s %s" % (code, label) for code, (label, _, _) in TYPES.items())


def _cfg(config_id=None):
    """取一条读图配置。**复用 vision_read 这个用途**——这是本系统里唯一一类
    「发图给多模态模型」的调用，不该为附件分类另开一个 purpose：
    两类调用共用一份模型选择、一份计费口径，否则「vision_read 配的是哪个型号」
    这个问题会有两个不同的答案。"""
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


_CODE_RE = re.compile(r"类型编码[:：]\s*([A-Z0-9]+|无法判断)")
_CONF_RE = re.compile(r"把握[:：]\s*([高中低])")
_REASON_RE = re.compile(r"依据[:：]\s*(.+)")
_NOTE_RE = re.compile(r"备注[:：]\s*(.+)")


def _parse(content):
    """解析模型的四行回答。**解析不出来就全部留空，不报错**——

    解析失败不等于读图失败：模型可能换了措辞、加了多余的话。原始 content
    已经原样存进 attachment_vision.content 了，解析不出结构化字段只影响
    「能不能自动判定」，不影响「这次调用发生过」这件事被记下来。
    """
    code_m = _CODE_RE.search(content or "")
    code = code_m.group(1) if code_m else None
    if code == "无法判断":
        code = None
    elif code and code not in TYPES:
        # 模型没按列表回答（编了个不存在的编码），当作判不出，**不强行纠正**：
        # 猜它想说哪个编码，比直接判不出更危险。
        code = None
    conf_m = _CONF_RE.search(content or "")
    reason_m = _REASON_RE.search(content or "")
    note_m = _NOTE_RE.search(content or "")
    return {
        "type_code": code,
        "type_label": TYPES[code][0] if code else None,
        "en17_value": TYPES[code][1] if code else None,
        "confidence": conf_m.group(1) if conf_m else None,
        "reason": (reason_m.group(1).strip() if reason_m else None),
        "note": (note_m.group(1).strip() if note_m else None),
    }


def classify_one(attachment_id, config_id=None, max_tokens=500):
    """读一张附件的图，判类型。**一次调用，一行台账。**

    把握为「低」或判不出时，**不回写 attachment.attach_type**——
    台账里仍然留着这次调用的完整记录，只是不拿它去更新字段。
    """
    row = db.query_one("""SELECT id, bill_no, file_name, stored_path, source_kind
                            FROM attachment WHERE id=%s""", (attachment_id,))
    if not row:
        raise ValueError("没有这条附件：%s" % attachment_id)
    if row["source_kind"] != "xlsx内嵌":
        raise ValueError("「%s」的来源是「%s」，不是从 xlsx 抽出来的真实附件，"
                         "不读图判类型（演示合成的附件类型是生成器直接给的）"
                         % (row["file_name"], row["source_kind"]))
    if not row["stored_path"]:
        raise ValueError("这条附件没有原图路径：%s" % row["file_name"])

    cfg = _cfg(config_id)
    attempt = (db.scalar("""SELECT max(attempt) FROM attachment_vision
                             WHERE attachment_id=%s AND model_name=%s""",
                         (attachment_id, cfg["model_name"])) or 0) + 1

    res, content, use = client.chat_vision(cfg, SYSTEM, PROMPT % _type_menu(),
                                           row["stored_path"], max_tokens=max_tokens)
    parsed = _parse(content) if res.ok else {
        "type_code": None, "type_label": None, "en17_value": None,
        "confidence": None, "reason": None, "note": None}

    vid = db.scalar("""
        INSERT INTO attachment_vision(
            attachment_id, attempt, config_id, model_name, model_echo,
            ok, err_kind, message, content, type_code, type_label, en17_value,
            confidence, reason, latency_ms, prompt_tokens, completion_tokens)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        (attachment_id, attempt, cfg["config_id"], cfg["model_name"], res.model_echo,
         res.ok, res.err_kind, res.message, content or None,
         parsed["type_code"], parsed["type_label"], parsed["en17_value"],
         parsed["confidence"], parsed["reason"],
         res.latency_ms, use.get("prompt_tokens"), use.get("completion_tokens")))

    applied = False
    # **只有「高」把握才回写字段。** 「中」留着给人看、不自动采信——
    # 决策表要求读图的那 7 种类型本身就不简单（往返行程、会议通知的真实性），
    # 「中」等于模型自己都说拿不准，不该由它替人做决定。
    if res.ok and parsed["type_code"] and parsed["confidence"] == "高":
        db.execute("""UPDATE attachment SET attach_type=%s, type_code=%s,
                             type_source='读图判定', type_conf=%s,
                             typed_at=now(), typed_by=%s
                       WHERE id=%s""",
                   (parsed["en17_value"], parsed["type_code"], parsed["confidence"],
                    vid, attachment_id))
        applied = True

    return {
        "attachment_id": attachment_id, "bill_no": row["bill_no"],
        "file_name": row["file_name"], "vision_id": vid,
        "ok": res.ok, "err_kind": res.err_kind, "message": res.message,
        "type_code": parsed["type_code"], "type_label": parsed["type_label"],
        "confidence": parsed["confidence"], "reason": parsed["reason"],
        "note": parsed["note"], "applied": applied,
        "model": cfg["model_name"], "latency_ms": res.latency_ms, "tokens": use,
    }


def pending(limit=500):
    """还没判类型的真实附件（source_kind='xlsx内嵌' 且 attach_type IS NULL）。"""
    return db.query("""SELECT id, bill_no, file_name, seq, byte_size,
                              (SELECT count(*) FROM attachment_vision v
                                WHERE v.attachment_id = attachment.id) AS attempts
                         FROM attachment
                        WHERE source_kind='xlsx内嵌' AND attach_type IS NULL
                        ORDER BY bill_no, seq LIMIT %s""",
                    (max(1, min(2000, int(limit))),))


def run(attachment_ids=None, limit=20, config_id=None):
    """批量判类型。**真出网、真计费**，一次最多 limit 条，由人决定要不要继续。

    `attachment_ids=[]`（空列表）表示「没有要处理的」，不是「全部」——
    和 doclib_extract 的同类参数保持一致的区分（is not None）。
    """
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
            res = classify_one(r["id"], config_id=config_id)
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
    return {"tried": len(rows), "applied": applied, "failed": failed,
            "items": items,
            "note": "applied=True 的那些已经写进 attachment.attach_type；"
                    "其余的（判不出 / 把握不到「高」/ 调用失败）**原样留空**，"
                    "完整记录都在 attachment_vision 里，不丢。"}


def status():
    """分类进度 + 费用留痕。"""
    total = db.scalar("SELECT count(*) FROM attachment WHERE source_kind='xlsx内嵌'") or 0
    typed = db.scalar("""SELECT count(*) FROM attachment
                          WHERE source_kind='xlsx内嵌' AND attach_type IS NOT NULL""") or 0
    by_type = db.query("""SELECT type_code, type_source, count(*) n FROM attachment
                          WHERE source_kind='xlsx内嵌' AND attach_type IS NOT NULL
                          GROUP BY 1,2 ORDER BY 3 DESC""")
    by_conf = db.query("""SELECT confidence, count(*) n FROM attachment_vision
                          WHERE confidence IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
    calls = db.query_one("""SELECT count(*) AS n,
                                   count(*) FILTER (WHERE NOT ok) AS failed,
                                   sum(prompt_tokens) AS pt, sum(completion_tokens) AS ct
                              FROM attachment_vision""") or {}
    unresolved = db.scalar("""SELECT count(*) FROM attachment a
                               WHERE a.source_kind='xlsx内嵌' AND a.attach_type IS NULL
                                 AND EXISTS (SELECT 1 FROM attachment_vision v
                                             WHERE v.attachment_id=a.id)""") or 0
    return {
        "total": total, "typed": typed, "untyped": total - typed,
        "by_type": by_type, "by_confidence": by_conf,
        "calls": int(calls.get("n") or 0), "calls_failed": int(calls.get("failed") or 0),
        "prompt_tokens": int(calls.get("pt") or 0),
        "completion_tokens": int(calls.get("ct") or 0),
        # 读过但没判定成的：模型判不出、或把握不到「高」。**不是没读过**，
        # 这个数字和 untyped 不一样——untyped 包含「一次都没读过」的。
        "read_but_unresolved": unresolved,
        "type_menu": [{"code": c, "label": l, "en17": e, "ocr_needed": o}
                      for c, (l, e, o) in TYPES.items()],
        "note": "**把握「高」才写进 attach_type。** 「中」「低」以及判不出的，"
                "完整记录都在 attachment_vision 里，但不回写字段——"
                "TR-D6-01 看到 attach_type 为空仍然会报「判不出」，不会误判成缺件。",
    }
