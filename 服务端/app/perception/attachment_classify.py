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
import hashlib
import os
import re
import tempfile

from .. import db
from ..llm import client

# ── 哪些附件算「真实客户凭证」────────────────────────────────────────────────
# 2026-11-01 加。原来这一整个模块把 `source_kind='xlsx内嵌'` **写死在 9 处**，
# 那是福建海运集团当时唯一的来源（附件嵌在报账单 xlsx 导出里）。
# 822港航 的 147 个附件来源是 `zip附件包`（客户交付的嵌套 zip，见迁移 0054），
# 于是整条读图链路**看不见它们**：pending 查不到、status 不计数、
# classify_one 直接抛「不是从 xlsx 抽出来的真实附件」。
# 不报错、不漏日志，就是什么都不发生——这类「写死另一家的取值」本轮已经
# 遇到第五次了。
#
# 这里要的判据其实是「这是客户的真凭证、而且有原图可读」，不是「它从哪种
# 容器里抽出来的」。所以列成一个集合，**演示合成的仍然排除在外**——
# 那是生成器造的图，类型本来就是生成器直接给的，读它既花钱又没有意义。
# 「上传」也排除：那是制度文件库那条链路，不是单据附件。
REAL_SOURCES = ("xlsx内嵌", "zip附件包")

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
    # A16..A19 是 822港航 实际有、而上面这份（源自福建海运集团）没有的四类。
    # 2026-11-01 补，起因是试跑时一张「业务接待审批单」被判成了 A13 事权审批单、
    # 把握「高」、直接写进了字段——**模型不会主动判不出，它会挑最近邻**。
    # 查证过：必要附件决策表 29 行里一次都没提过「事权审批单」，
    # fjhy 的 219 个已定类附件里判成它的是 0 个，A13 在那边是个空桶。
    # 决策表那一列标 False：必要附件决策表里没有这四类的条目（客户还没给），
    # 所以它们**只能用来「认出来」，还不能用来判「该交没交」**。
    "A16": ("业务接待审批单", "业务接待审批单", False),
    "A17": ("业务接待结算单", "业务接待结算单", False),
    "A18": ("业务接待清单",   "业务接待清单",   False),
    "A19": ("酒水领用单",     "酒水领用单",     False),
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


def _as_image(path):
    """给读图模型准备一张**图**。PDF 要先渲成 PNG。

    2026-11-01 实测：把 PDF 原样发出去，厂商回的是
      400 InternalError.Algo.InvalidParameter:
      The image format is illegal and cannot be opened
    一轮批跑 105 次 PDF 调用**全军覆没**，而 JPG 50 次全成。
    原因是这条链路一直只喂过图片：福建海运集团那 219 个附件是从 xlsx 里
    抽出来的 PNG/JPG，**从来没有 PDF 进过这个队列**。
    822港航 的附件来自 zip 包，93 个是 PDF——放开来源取值之后它们进了队列，
    而管道读不了它们。

    渲页用 ocr.iter_rendered_pages()，和 app/vision.py 走同一套渲页实现
    （0042 把渲页从 OCR 里拆出来就是为了「我只想要页面图」这件事）。
    **只渲第一页**：判类型靠的是票面抬头，第一页就够；多渲几页是按 token
    计费的图，白花钱。
    """
    if os.path.splitext(path)[1].lower() != ".pdf":
        return _shrink(path)
    from .. import ocr
    for pg in ocr.iter_rendered_pages(path, keep_images=True, max_pages=1):
        img = pg.get("image_path")      # 键名是 image_path，不是 img_path
        if img and os.path.exists(img):
            return _shrink(img)
        break
    raise ValueError("这份 PDF 渲不出页面图：%s" % os.path.basename(path))


# 超过这个大小就先缩一版再发。
# **2 MB 这个数是量出来的，不是拍的**：2026-11-01 读图批里 42 个文件，
# 只有一个超过 2 MB（4419 KB 的手机拍照 JPG），而它**连续两次**都在
# 「连接已建立但中途被断开」上失败，写超时给到 663 秒也没传完；
# 其余 41 个（最大 1853 KB）一次都没失败过。
# base64 之后 4419 KB 变成约 5.9 MB，这条上行扛不住。
_MAX_KB = 2048
_MAX_SIDE = 2000      # 长边像素。图是按 token 计费的，缩图同时省钱。


def _shrink(path):
    """图太大就等比缩一版，缩不动就原样返回。

    **缩不动不许抛**：缩图是为了让大图也能发出去，不是新增一种失败方式。
    Pillow 读不了某张图（比如 CMYK 的怪 JPG）时，原样发出去至少还有机会成，
    而抛异常是必然失败。
    """
    try:
        if os.path.getsize(path) <= _MAX_KB * 1024:
            return path
    except OSError:
        return path
    out = os.path.join(tempfile.gettempdir(),
                       "shrink_%s_%d.jpg" % (
                           hashlib.sha1(path.encode("utf-8", "replace")
                                        ).hexdigest()[:16], _MAX_SIDE))
    if os.path.exists(out):
        return out
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((_MAX_SIDE, _MAX_SIDE))
            im.save(out, "JPEG", quality=85)
        return out
    except Exception:                                   # noqa: BLE001
        return path


def types_for(instance_id):
    """这个实例**能存得下**的类型。

    TYPES 是两家共用的一张表，而 attach_type 的外键指向各自本体的 EN17。
    不按实例裁剪就会出现：菜单上给模型列了 16 个选项，模型选中一个
    本实例 EN17 里没有的值，写回时被外键拒——**一次读图的钱白花，
    而且报出来的是「外键违反」，看不出是菜单给错了**。
    裁剪之后，模型只会在本实例装得下的类型里选。
    """
    have = {r["value"] for r in db.query(
        """SELECT value FROM enum_value_current
            WHERE instance_id = %s AND enum_id = 'EN17'""", (instance_id,))}
    return {c: v for c, v in TYPES.items() if v[1] in have}


def _type_menu(types=None):
    t = TYPES if types is None else types
    return "\n".join("%s %s" % (code, label) for code, (label, _, _) in t.items())


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
    row = db.query_one("""SELECT id, bill_no, file_name, stored_path, source_kind,
                                 instance_id
                            FROM attachment WHERE id=%s""", (attachment_id,))
    if not row:
        raise ValueError("没有这条附件：%s" % attachment_id)
    if row["source_kind"] not in REAL_SOURCES:
        raise ValueError("「%s」的来源是「%s」，不在真实客户凭证之列（%s），"
                         "不读图判类型（演示合成的附件类型是生成器直接给的）"
                         % (row["file_name"], row["source_kind"],
                            "、".join(REAL_SOURCES)))
    if not row["stored_path"]:
        raise ValueError("这条附件没有原图路径：%s" % row["file_name"])
    img_path = _as_image(row["stored_path"])

    cfg = _cfg(config_id)
    attempt = (db.scalar("""SELECT max(attempt) FROM attachment_vision
                             WHERE attachment_id=%s AND model_name=%s""",
                         (attachment_id, cfg["model_name"])) or 0) + 1

    allowed = types_for(row["instance_id"])
    res, content, use = client.chat_vision(cfg, SYSTEM, PROMPT % _type_menu(allowed),
                                           img_path, max_tokens=max_tokens)
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

    # **自相矛盾的回答不许回写。**
    # 2026-11-01 实测到一条：模型选了 A17 业务接待结算单、把握「高」，
    # 而它自己给的理由是「票面标题明确为『业务接待清单』」——
    # 那是 A18 的名字。理由和结论对不上，说明它读出来的字和它挑的类不是一回事，
    # 而把握「高」这一层拦不住这种错（它对自己挑的那个很有把握）。
    # 这道闸只用确定性判据：理由里**出现了别的类型的全名、而且没有出现
    # 所选类型的全名**，就不回写。
    # 误报风险压到最低——两个条件都要满足，而且比的是全名不是关键词。
    conflict = None
    if parsed["type_code"] and parsed["reason"]:
        mine = (allowed.get(parsed["type_code"]) or ("",))[0]
        others = [lab for c, (lab, _, _) in allowed.items()
                  if c != parsed["type_code"] and lab and lab in parsed["reason"]]
        if others and mine and mine not in parsed["reason"]:
            conflict = ("理由里提到的是「%s」，而选的是「%s」——自相矛盾，不回写"
                        % ("、".join(others), mine))

    # **两次「高」把握互相打架时，不许后写的那次说了算。**
    # 2026-11-01 实测：同一张图、同一个型号、同一个提示词读 5 次，
    # 给出 4 次 A17、1 次 A16，**每次都是「高」把握**。
    # 而原来每次「高」都覆盖字段，于是最终存的是哪个类型，
    # 取决于循环恰好读了它几次——**一个调度偶然决定了业务结论**。
    # 「把握高」只说明模型对自己这次的选择有信心，不保证两次之间稳定。
    # 这道闸把「已有一次高把握结论」当成第二个来源用：两边不一致就是
    # 两个互相矛盾的自信答案，没有第三方能裁决，所以**退回判不出**，
    # 并且把已经写进去的那个也清掉——留着它等于让人相信一个掷硬币的结果。
    unstable = None
    if res.ok and parsed["type_code"] and parsed["confidence"] == "高":
        prior = [r["type_code"] for r in db.query(
            """SELECT DISTINCT type_code FROM attachment_vision
                WHERE attachment_id = %s AND id <> %s AND ok
                  AND confidence = '高' AND type_code IS NOT NULL""",
            (attachment_id, vid))]
        diff = [c for c in prior if c != parsed["type_code"]]
        if diff:
            unstable = ("同一张图多次读出不同类型：本次 %s，此前 %s——"
                        "两次都是「高」把握，没有第三方能裁决，退回判不出"
                        % (parsed["type_code"], "、".join(sorted(diff))))
            db.execute("""UPDATE attachment SET attach_type=NULL, type_code=NULL,
                                 type_source=NULL, type_conf=NULL,
                                 typed_at=NULL, typed_by=NULL
                           WHERE id=%s AND type_source='读图判定'""",
                       (attachment_id,))

    applied = False
    # **只有「高」把握才回写字段。** 「中」留着给人看、不自动采信——
    # 决策表要求读图的那 7 种类型本身就不简单（往返行程、会议通知的真实性），
    # 「中」等于模型自己都说拿不准，不该由它替人做决定。
    if (res.ok and parsed["type_code"] and parsed["confidence"] == "高"
            and not conflict and not unstable):
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
        "note": parsed["note"], "applied": applied, "conflict": conflict,
        "unstable": unstable,
        "model": cfg["model_name"], "latency_ms": res.latency_ms, "tokens": use,
    }


def _iid(instance_id=None):
    """当前实例。**这个模块原来一个 instance_id 都没有。**

    后果不是查得多一点：`pending()` 不带实例，取到的是**全库**待判的附件，
    而 `run()` 会把它们一张张发到外部读图模型去——
    给 822港航 跑一轮，福建海运集团的客户凭证会跟着被发出去并计费。
    这条链路发出去的是真实发票和出差申请单（票面上有姓名、身份证号片段、
    金额、车次航班），所以这不是性能问题，是**范围失控**。
    项目里为同一类事故已经回滚过一次（读图判类型那次：mock 拦了函数，
    没拦数据范围）。
    """
    from .. import doclib                   # 复用「当前实例」的唯一入口
    return doclib.resolve_instance(instance_id)["instance_id"]


def pending(limit=500, instance_id=None, model_name=None, retry_read=False):
    """本实例里**还没读过**的真实客户附件。

    **「没判出类型」不等于「还没读过」。** 第一版只看 `attach_type IS NULL`，
    于是模型答「低把握」「判不出」的那些每一轮都被重新取出来再读一遍——
    同一张图、同一个模型、同一个提示词，重读不会得到新信息。
    2026-11-01 实测：一轮批跑下来有 8 个附件被读了 3 次、1 个读了 4 次、
    1 个读了 **6 次**，72 次调用里有一半是重复的。**纯烧钱，没有新信息。**

    所以默认排除「已经被当前型号成功读过一次」的。注意两点：
      · 按 **(附件, 型号)** 排除，不是按附件——换个型号对照读是这个模块
        一开始就设计好的用法（见文件头纪律 3：同一张图重读落新的 attempt）。
      · 只排除 **ok=True** 的。网络失败、超时那些该重试，排除了就永远不重试。
    真要拿同一个型号再读一遍（比如改了提示词），`retry_read=True`。
    """
    iid = _iid(instance_id)
    mdl = model_name or (_cfg() or {}).get("model_name")
    sql = """SELECT id, bill_no, file_name, seq, byte_size,
                    (SELECT count(*) FROM attachment_vision v
                      WHERE v.attachment_id = attachment.id) AS attempts
               FROM attachment
              WHERE instance_id = %s AND source_kind = ANY(%s)
                AND attach_type IS NULL"""
    params = [iid, list(REAL_SOURCES)]
    if not retry_read:
        sql += """ AND NOT EXISTS (SELECT 1 FROM attachment_vision v
                                    WHERE v.attachment_id = attachment.id
                                      AND v.ok AND v.model_name = %s)"""
        params.append(mdl)
        # **确定性的失败不要一轮一轮重试。**
        # 实测：93 个 PDF 原样发出去，厂商每次都回
        # 400 InvalidParameter「The image format is illegal」——
        # 同样的文件、同样的型号，重试 4 轮烧了 105 次调用，一次都不会成。
        # 超时和 TLS 握手失败是瞬时的，该重试；4xx 是我方请求本身不对，
        # 不改代码就永远是这个结果。
        # 改了代码（比如现在加了 PDF 渲页）之后要重跑这些，
        # 用 retry_read=True。
        sql += """ AND NOT EXISTS (SELECT 1 FROM attachment_vision v
                                    WHERE v.attachment_id = attachment.id
                                      AND NOT v.ok AND v.err_kind = 'http'
                                      AND v.model_name = %s)"""
        params.append(mdl)
    sql += " ORDER BY bill_no, seq LIMIT %s"
    params.append(max(1, min(2000, int(limit))))
    return db.query(sql, tuple(params))


def sample_by_type(instance_id=None, per_type=1):
    """按 content_type 各取几条，供**试跑**用。

    2026-11-01 的教训：试跑 3 条，我取的是队列头部，**全是 .jpg**，
    而队列里 93 个是 PDF——而 PDF 原样发出去会被厂商 400 拒掉。
    于是试跑全绿、批跑 101 次全败，耗掉约 63 分钟。
    附件「93 PDF / 54 JPG」这个数我早就量出来写在报告里了，试跑时没用上。

    试跑的全部意义是提前暴露问题；取一组恰好避开问题的样本，等于没试。
    所以把「按类型各取一条」做成函数，而不是每次靠自觉。
    """
    iid = _iid(instance_id)
    return db.query("""
        SELECT id, bill_no, file_name, content_type FROM (
            SELECT id, bill_no, file_name, content_type,
                   row_number() OVER (PARTITION BY content_type
                                      ORDER BY bill_no, seq) AS rn
              FROM attachment
             WHERE instance_id = %s AND source_kind = ANY(%s)) t
         WHERE rn <= %s ORDER BY content_type, bill_no""",
                    (iid, list(REAL_SOURCES), max(1, int(per_type))))


def run(attachment_ids=None, limit=20, config_id=None, instance_id=None):
    """批量判类型。**真出网、真计费**，一次最多 limit 条，由人决定要不要继续。

    `attachment_ids=[]`（空列表）表示「没有要处理的」，不是「全部」——
    和 doclib_extract 的同类参数保持一致的区分（is not None）。

    **显式给的 id 也要按实例过滤**：给错一个别家的 id，它就会被发出去。
    过滤掉比报错安全——报错会让人加个 try 绕过去。
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


def status(instance_id=None):
    """分类进度 + 费用留痕。**按实例**——不按实例，两家的进度会加在一起，
    页面上看不出哪一家读完了。费用那几项同理：这条链路是计费的，
    「谁花的」必须分得开。"""
    iid = _iid(instance_id)
    src = list(REAL_SOURCES)
    total = db.scalar("""SELECT count(*) FROM attachment
                          WHERE instance_id=%s AND source_kind = ANY(%s)""",
                      (iid, src)) or 0
    typed = db.scalar("""SELECT count(*) FROM attachment
                          WHERE instance_id=%s AND source_kind = ANY(%s)
                            AND attach_type IS NOT NULL""", (iid, src)) or 0
    by_type = db.query("""SELECT type_code, type_source, count(*) n FROM attachment
                          WHERE instance_id=%s AND source_kind = ANY(%s)
                            AND attach_type IS NOT NULL
                          GROUP BY 1,2 ORDER BY 3 DESC""", (iid, src))
    by_conf = db.query("""SELECT v.confidence, count(*) n FROM attachment_vision v
                          JOIN attachment a ON a.id = v.attachment_id
                          WHERE a.instance_id=%s AND v.confidence IS NOT NULL
                          GROUP BY 1 ORDER BY 2 DESC""", (iid,))
    calls = db.query_one("""SELECT count(*) AS n,
                                   count(*) FILTER (WHERE NOT v.ok) AS failed,
                                   sum(v.prompt_tokens) AS pt,
                                   sum(v.completion_tokens) AS ct
                              FROM attachment_vision v
                              JOIN attachment a ON a.id = v.attachment_id
                             WHERE a.instance_id = %s""", (iid,)) or {}
    unresolved = db.scalar("""SELECT count(*) FROM attachment a
                               WHERE a.instance_id=%s AND a.source_kind = ANY(%s)
                                 AND a.attach_type IS NULL
                                 AND EXISTS (SELECT 1 FROM attachment_vision v
                                             WHERE v.attachment_id=a.id)""",
                           (iid, src)) or 0
    return {
        "instance_id": iid,
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
