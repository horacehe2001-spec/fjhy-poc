# -*- coding: utf-8 -*-
"""制度抽取：从只读文件库的正文里切条款、识别规则特征、与现有规则比对覆盖。

两步，分得很开，**不要合成一个按钮**：

  第一步「条款切分」是确定性的，不调模型、不花钱、不出网。
  它回答的是规则治理最先要问的那个问题——
  **制度里还有哪些带阈值、带禁止性表述的条款，至今没有变成规则。**

  第二步「AI 草稿」才调模型，把候选条款写成八要素草稿。
  它一定产出**草稿**，不产出规则：采纳要人点，且记 reviewer。

两条口径必须跟着结果一起显示，否则一定被读错：
  1.「未覆盖」不等于「缺规则」。定义、名词解释、职责分工、流程叙述类条款
     本来就不该变成判定规则。这里只能指出「这条有阈值/禁止表述但没有规则
     引用它」，要不要补规则是业务专家的判断。
  2.「已覆盖」不等于「覆盖完整」。只表示有规则的来源条款引用了这个条款号，
     不代表该条款的每一款每一项都被规则表达了。

还有一条本项目特有的、必须主动说的：
  现有 82 条规则的 source_clause.file 多数写的是「规则表」而不是具体制度文件名。
  那是人工整理规则清单时的来源标记，不是制度文件。拿它去比对，结果必然
  大面积「未覆盖」——**那是比对的盲区，不是制度里真有那么多漏网条款**。
  所以 coverage() 把这类规则单独计数并显式回报，绝不让它混进「未覆盖」的数字里。
"""
import datetime
import hashlib
import json
import re
import uuid

from . import db, doclib, mask, ocr
from .llm import client as llmc

# ── 条款切分 ────────────────────────────────────────────────────────────────
# 「第X条」后面常跟（标题），正文到下一个「第Y条」为止。
CLAUSE_HEAD = re.compile(r"第[一二三四五六七八九十百零〇\d]{1,8}条")
TITLE_AFTER = re.compile(r"^\s*[（(]([^）)]{2,30})[）)]")

MIN_TEXT = 12          # 太短的（只有标题）不计入候选
SNIPPET = 200

# 下面三个是 config/agents/*.yaml 的 improve.params 里声明过的参数。
# **声明了就必须真的在代码里起作用**，否则那页参数台账是文档不是开关——
# 实测踩过：YAML 里写着 llm_timeout_seconds 可调 [15,300]，而真正的超时在
# llm_config.timeout_s=60，8 次调用 7 次被我们自己掐断。
# tests/test_extract_contract.py 钉住「YAML 的 default 必须等于这里的值」。
MAX_CLAUSES_PER_FILE = 500   # fj_l1_02_clause_split.improve.params
AI_BATCH_DEFAULT = 8         # fj_l1_03_rule_draft.improve.params.max_drafts_per_cycle
SEND_CHAR_LIMIT = 1500       # fj_l1_03_rule_draft.improve.params.send_char_limit

# 规则特征：带这些表述的条款才**可能**变成可判定的规则。
#
# 「以上/以下/以内」必须**紧跟在数字后面**才算阈值。单独匹配这三个词的话，
# 「分为以下两类」「以下统称差旅费」会把「第二条 名词解释」也标成阈值条款。
# 误报会让「制度里还有 N 条带阈值的条款没变成规则」这个数字整体失真，
# 而这个数字正是本功能的全部价值所在（纪律第 9 条：误报和漏报同样致命）。
UNIT = r"(?:%|％|元|天|日|个月|月|年|万元|亿元|级|倍|人|次|笔|公里|小时|工作日)"
FEATURES = [
    ("阈值", re.compile(r"(不得超过|不超过|不得低于|不低于|不少于|不得少于|最高|最长|最短|"
                        r"不得高于|不得大于|不得小于|上限|限额|标准为|≤|≥|"
                        r"\d+(?:\.\d+)?\s*" + UNIT + r"?\s*(?:以上|以下|以内))")),
    ("数值", re.compile(r"\d+(?:\.\d+)?\s*" + UNIT)),
    ("禁止", re.compile(r"(不得|禁止|严禁|不予|不能|一律不|不允许)")),
    ("强制", re.compile(r"(应当|必须|须经|应按|应在|需经)")),
    ("例外", re.compile(r"(例外|特殊情况|经[^，。；]{2,20}(?:批准|同意|审核|授权)|另有规定|"
                        r"确因工作需要)")),
    ("名单制", re.compile(r"(名单制|白名单|黑名单|准入名单|目录内)")),
]
# 「可能成为规则」的门槛：光有「数值」不够（页码、条款序号都是数字），
# 必须带阈值、禁止、强制、例外、名单制中的任意一项。
CANDIDATE_FEATURES = {"阈值", "禁止", "强制", "例外", "名单制"}

# 候选只排除**范本**，不是只收「制度」。
# 第一版定成「只有文件名像制度的才算候选」，实扫立刻露馅：
# 《差旅费解答六（国资委未转发）》第十三条带阈值+禁止+例外，是标准的待规则化条款，
# 却因为文件名里没有「办法/细则/制度」这类词被判成「其他」，一条候选都没进。
# 真正会污染数字的是合同、申请书这类**文书范本**——它们里面的「第X条」是文书条款。
# 所以门槛改成「不是范本」：宁可多看几条，不要漏掉名字不规整的制度文件。
CANDIDATE_KINDS = ("制度", "其他")   # 范本与单据都排除，见 doclib.classify

NUM = re.compile(r"\d+(?:\.\d+)?\s*" + UNIT + r"?")

# 文件类别。合同、申请书、通知书这类**文书范本**里也有「第X条」，但那是文书条款
# 不是制度条款。混进统计会让数字失真（参考工程实扫时一份《特别约定》一家
# 就贡献了 41 条）。单独分组，仍可查看。
TEMPLATE_WORDS = ("合同", "申请书", "通知书", "协议", "约定", "清单", "保函", "台账",
                  "承诺书", "确认书", "授权书", "声明", "模板", "范本", "表单", "附表")
POLICY_WORDS = ("管理办法", "审核要点", "规定", "细则", "指引", "制度", "标准",
                "办法", "通知", "规程", "须知", "解答", "问答", "口径", "规则", "要点")


def kind_of(name):
    """文件类别**以文件库那一栏为准**（doclib.classify）。

    这里不再自己判一套：同一个文件名在两处判出不同类别，
    清单上就会出现「文件库说它是单据、抽取说它是制度」，
    而没人知道该信哪个。分类是**文件的属性**，归文件库那一栏。
    """
    return doclib.classify(name)


def clause_ident(file_id, strategy, clause_no, nth):
    """条款的**内容定址** id：dlc- + md5(file_id|策略|条款号|同号第几次)[:12]。

    **与 0028 迁移里的 SQL 必须同算法**（那边用 md5() 拼同样的四段），
    否则迁移改过的 id 和这里重算的 id 不一样，重切一次就全部变成新条款，
    挂在上面的草稿又断一次。

    不用 uuid：uuid 让同一条「第十条」每次重切换一个身份，
    草稿（一条 150 秒的付费调用）和审定结论随 CASCADE 一起没——实测丢过 14 条。
    不用 seq：文件前面插一条，后面所有 seq 全体位移，等于全部换了身份。
    要带「同号第几次」：实扫有 3 处同份文件条款号重复（20号文第十六条、
    第二十一条各两次，解答六第三条两次），只用条款号会把两条不同的条款算成一条。
    """
    raw = "%s|%s|%s|%d" % (file_id, strategy or "", clause_no, nth)
    return "dlc-" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def split_clauses(body):
    """把正文切成 [{clause_no, title, body, pos}]。**切不出来就返回空，不硬凑。**

    硬凑的后果：把整份文件当成一条「条款」丢给模型，模型会在几千字里
    挑它觉得重要的编一条规则出来，而没人能对上是哪一条。
    """
    if not body:
        return []
    heads = list(CLAUSE_HEAD.finditer(body))
    if not heads:
        return []
    out = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        seg = body[start:end].strip()
        rest = seg[len(m.group(0)):]
        tm = TITLE_AFTER.match(rest)
        title = tm.group(1) if tm else None
        text = (rest[tm.end():] if tm else rest).strip("　：:。\n\t")
        if len(text) < MIN_TEXT:
            continue
        out.append({"clause_no": m.group(0), "title": title,
                    "body": text, "pos": start})
    return out


def features_of(text):
    return [name for name, rx in FEATURES if rx.search(text or "")]


def numbers_in(text):
    """条款里的数值表述，供人一眼看出阈值在哪，也供 AI 草稿的数值核对用。"""
    seen, out = set(), []
    for m in NUM.finditer(text or ""):
        t = m.group(0).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:20]


# ── 切分策略：按文件形态选，不是一套正则打天下 ──────────────────────────────
# 实扫客户资料逼出来的三种形态，少一种就有一整类文件抽不到：
#   条款体    「第十条 ...」          差旅管理实施细则、业务接待管理办法
#   层级编号  「1.3.9.1. ...」       差旅费共享审核要点（PDF，没有「第X条」）
#   表格行    「5 | 交通费标准控制 | ...」 差旅费审核规则.xlsx、系统审核算法.xlsx
#
# **表格行这一类最要紧**：差旅费审核规则.xlsx 里每一行就是一条规则，
# 现有 82 条规则的 source_clause.file 写的「规则表」指的就是它。
# 用条款体正则去切它，结果是 0 条——而它恰恰是规则密度最高的一份。

NUMBERED_HEAD = re.compile(r"^\s*(\d+(?:\.\d+){1,4})\.?\s*", re.M)
# 表格行：OCR/解析出来的 xlsx 正文形如「5 | 交通费标准控制 | 按照各单位制度要求…」
TABLE_ROW = re.compile(r"^\s*(\d{1,4})\s*\|\s*(.+)$")
TABLE_HEAD_HINT = ("序号", "校验类型", "校验规则", "算法编码", "算法名称", "规则", "控制类型")

# 一个工作簿里不是所有工作表都是规则来源。实扫《财务共享业务场景核算及审核规则.xlsx》
# 有 3 个 sheet（说明 / 业务域联系人 / 业务域），而**切出来的 9 条全部来自
# 「业务域联系人」**：9 个真实姓名 + 手机号，被当成「规则条款」入了库
# （出口脱敏把手机号打成 180****0859，闭集合外的姓名原样返回）。
# 它们没有规则特征所以没被送进模型——那是运气，不是设计。
NON_RULE_SHEETS = ("说明", "联系人", "目录", "封面", "修订", "变更记录", "填表")

# 认表头用的词。**和 TABLE_HEAD_HINT 分开**：那个用来判「这一行是表头所以不收」，
# 加词会连带改变「收不收某一行」的口径；这个只用来**认出表头是哪一行**，
# 加词只影响能不能按列映射。两件事混在一个表里，改一处会动到另一处。
HEADER_WORDS = TABLE_HEAD_HINT + (
    "校验方式", "是否已有", "评估共享是否可实现", "算法类型", "算法分类", "算法描述",
    "审核算法规则", "算法来源", "配置方式", "状态",
    "业务域", "系统单据", "业务大类", "业务小类", "业务场景释义", "报账附件",
    "专用审核要点", "备注")


def _looks_header(cols):
    """这一行是表头吗。两条判据，任一成立即可。

    一、第一列就是「序号」——两份规则表都是这样。
    二、至少两列是已知的表头词——场景表的表头以「业务域」开头，没有序号列。
    **要两列**：只要一列就会把正文里恰好写着「备注」的那一行当成表头。
    """
    if not cols:
        return False
    if cols[0] in ("序号", "序 号", "编号", "No", "NO"):
        return True
    return sum(1 for c in cols if c in HEADER_WORDS) >= 2
RE_MOBILE_ROW = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")


def _looks_contact_row(cols):
    """联系人行：列很少，且某一列整个就是一个手机号。

    **只在「列少 + 整列就是手机号」时判**，不是「行里出现 11 位数字就扔」：
    规则条款里完全可能出现金额、编码、日期这类长数字，
    宁可多留一行让人看见，不可把一条规则静默扔掉（漏报和误报同样致命）。
    """
    if len(cols) > 4:
        return False
    return any(RE_MOBILE_ROW.fullmatch(c.strip()) for c in cols)


def split_numbered(text):
    """层级编号体：1.3.9.1. 各企业特殊要求：…

    与条款体分开写，不是合并成一个大正则：两种编号的「下一条在哪」判法不同，
    合在一起的正则一旦改错，两类文件会一起坏掉，而且很难看出是哪一类坏的。
    """
    if not text:
        return []
    heads = list(NUMBERED_HEAD.finditer(text))
    if not heads:
        return []
    out = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end].strip().strip("　：:。")
        if len(body) < MIN_TEXT:
            continue
        out.append({"clause_no": m.group(1), "title": None, "body": body, "pos": start})
    return out


def split_table_rows(text):
    """表格行体：每一行就是一条规则。

    标题取第二列（校验类型 / 算法名称这类），正文保留整行——
    **不丢列**：「实现途径」「是否已有」「控制类型」这些列是规则八要素里
    的现成字段，丢了就得回头再读一次原表。
    表头行不收：它没有规则内容，收进来会变成一条「序号|校验类型|…」的假规则。
    """
    if not text:
        return []
    out = []
    pos = 0
    sheet = None
    header = None
    for raw in text.splitlines():
        line_start = pos
        pos += len(raw) + 1
        line = raw.strip()
        if line.startswith("#"):                      # 「# SheetName」是工作表名
            sheet = line.lstrip("#").strip()
            header = None                             # 换表就换表头，不许串用
            continue
        if not line:
            continue
        if sheet and any(w in sheet for w in NON_RULE_SHEETS):
            continue                                  # 说明页、联系人页不是规则来源
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 3 and _looks_header(cols):
            header = cols                             # 记下来，供按列映射用
            continue
        m = TABLE_ROW.match(line)
        if not m:
            continue
        if len(cols) < 3:
            continue
        rest = m.group(2).strip()
        if len(rest) < MIN_TEXT:
            continue
        # 表头行：第二列就是「校验类型」这种列名本身
        if any(h == cols[1] for h in TABLE_HEAD_HINT):
            continue
        if _looks_contact_row(cols):
            continue
        # sheet / header / cols 是给「规则明细」那一步按列映射用的。
        # run_split 只读 clause_no/title/body/pos，多带几个键不影响它。
        out.append({"clause_no": "第%s行" % m.group(1), "title": cols[1][:40] or None,
                    "body": rest, "pos": line_start,
                    "sheet": sheet, "header": header, "cols": cols})
    return out


# ── 场景表：按「专用审核要点」拆 ─────────────────────────────────────────────
# 《财务共享业务场景核算及审核规则.xlsx》的「业务域」工作表，1709 行业务场景。
# 这张表以前**一条都没进来**：切表格行要求行首是序号数字，而这些行行首是
# 「费用报账域」，匹配 0 行；而匹配上的 9 条全部来自「业务域联系人」工作表
# （9 个真实姓名加手机号，被当成规则条款入了库）。
#
# 要点正文高度重复：差旅+接待两类单据共 163 行，「专用审核要点」正文**只有 2 种**，
# 客户按业务小类逐行重复填。按行拆会得到 163 条几乎一样的「规则」。
# 正确的粒度是**按正文去重、再按条内序号拆**——人工抽过的那 9 条
# （rule 表里 source_clause.file='场景表'）clause 写的就是「专用审核要点 3」，
# 粒度一致，两边才对得上账。
SCENARIO_KEY_COL = "专用审核要点"
SCENARIO_DOC_COL = "系统单据"
SCENARIO_SUB_COL = "业务小类"
# 本次 POC 的范围：差旅 + 接待两类单据（用户 2026-10-19 指定）。
# 其余 15 类单据（收入报账单、员工借款单…）不在差旅 POC 范围内，
# **但跳过了多少行必须报出来**，不许静默——否则下一个人会以为这张表只有这些。
SCENARIO_DOC_TYPES = ("单人/多人差旅报销单", "招待费报账单")
# 「1.行程日期审核： …… 2.出行方式、住宿标准审核： ……」
SCENARIO_ITEM = re.compile(r"(?:^|\s)(\d{1,2})\s*[.、．]\s*")


def _dedup_key(s):
    """去重用的键：把标点和空白都去掉再比。

    实扫同一条要点有两个版本，差别只是一个多打的「！」
    （「核对发票税目与费用清单一致性！」）。按原文比会变成两条规则，
    而它们说的是同一件事。**只在去重时这么比，入库仍存原文。**
    """
    return re.sub(r"[\s　。，,；;：:！!（）()、．.]", "", s or "")


def split_scenario_rows(text):
    """场景表 → [{clause_no, title, body, pos, …}]。去重后按条内序号拆。

    返回的额外键供「规则明细」用：
      doc_type   哪类单据
      sub_classes 这条要点覆盖了哪些业务小类（证据：一条规则背后是多少行）
      row_count  覆盖多少行
      skipped_rows 不在本次范围内的单据行数（要在结论里报出来）
    """
    if not text:
        return []
    sheet = header = None
    groups = {}          # (单据, 去重键) → {text, subs, n, pos}
    skipped = 0
    pos = 0
    for raw in text.splitlines():
        line_start = pos
        pos += len(raw) + 1
        line = raw.strip()
        if line.startswith("#"):
            sheet, header = line.lstrip("#").strip(), None
            continue
        if not line or line.count("|") < 2:
            continue
        if sheet and any(w in sheet for w in NON_RULE_SHEETS):
            continue
        cols = [c.strip() for c in line.split("|")]
        if SCENARIO_KEY_COL in cols and SCENARIO_DOC_COL in cols:
            header = cols                      # 这就是场景表的表头
            continue
        if not header:
            continue

        def col(name):
            i = header.index(name) if name in header else -1
            return cols[i].strip() if 0 <= i < len(cols) else ""

        doc, key = col(SCENARIO_DOC_COL), col(SCENARIO_KEY_COL)
        if not key:
            continue
        if doc not in SCENARIO_DOC_TYPES:
            skipped += 1
            continue
        k = (doc, _dedup_key(key))
        g = groups.setdefault(k, {"text": key, "subs": [], "n": 0, "pos": line_start})
        g["n"] += 1
        if len(key) > len(g["text"]):           # 同一条要点留最长的那个原文
            g["text"] = key
        sub = col(SCENARIO_SUB_COL)
        if sub and sub not in g["subs"]:
            g["subs"].append(sub)

    # **去重落在「每一条要点」上，不是落在整格文本上。**
    # 不这么做实测会拆出 41 条，其中要点 1~5 在 6 个分组里逐字相同、各发了一次。
    # 而分组之间真正的差别只在最后一条（「金额核对」后面多接了
    # 「机票保险费用不允许报销」「主办方提供餐食…不报补贴」这类句子）——
    # 那是**真的不同规则**，只适用于部分业务小类，所以它们该各自成条，
    # 并把覆盖的业务小类记下来。人工抽过的 TR-D5-05 就是这么标的
    # （clause 写「专用审核要点（国内差旅费·市内交通费补贴 58 场景）」）。
    items = {}       # (单据, 去重键) → {body, subs, rows, pos, seq}
    for (doc, _k), g in groups.items():
        marks = list(SCENARIO_ITEM.finditer(g["text"]))
        pieces = []
        if marks:
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(g["text"])
                body = g["text"][m.end():end].strip(" 　：:；;")
                if len(body) >= MIN_TEXT:
                    pieces.append(body)
        if not pieces:                           # 没有序号就整条当一条，不硬拆
            pieces = [g["text"]]
        for body in pieces:
            k2 = (doc, _dedup_key(body))
            it = items.setdefault(k2, {"body": body, "subs": [], "rows": 0,
                                       "pos": g["pos"], "seq": len(items)})
            if len(body) > len(it["body"]):
                it["body"] = body
            it["rows"] += g["n"]
            for s in g["subs"]:
                if s not in it["subs"]:
                    it["subs"].append(s)

    out = []
    per_doc = {}
    for (doc, _k), it in sorted(items.items(), key=lambda kv: kv[1]["seq"]):
        per_doc[doc] = per_doc.get(doc, 0) + 1
        # 标题取冒号前那一小段（「行程日期审核」），没有冒号就不编
        head = re.match(r"^([^，。；:：]{2,20})[:：]", it["body"])
        out.append({
            "clause_no": "%s·专用审核要点%d" % (doc, per_doc[doc]),
            "title": head.group(1) if head else None,
            "body": it["body"], "pos": it["pos"],
            "doc_type": doc, "sub_classes": it["subs"], "row_count": it["rows"],
            "skipped_rows": skipped,
        })
    return out


def pick_strategy(text, ext=None):
    """选切分策略。**按内容实际切出多少条来选，不按扩展名猜。**

    按扩展名猜会错：差旅费共享审核要点是 PDF 但用层级编号，
    而有的 docx 里是表格。先各切一遍，谁切得出来用谁——
    成本只是多跑两个正则，换来的是不会整类文件静默地抽不到。
    """
    cands = [("条款体", split_clauses(text)),
             ("表格行", split_table_rows(text)),
             ("层级编号", split_numbered(text)),
             ("场景拆条", split_scenario_rows(text))]
    name, items = max(cands, key=lambda kv: len(kv[1]))
    if not items:
        return "未识别", []
    return name, items


# ── 覆盖比对 ────────────────────────────────────────────────────────────────
def _norm_name(s):
    """文件名归一：去扩展名、去空白、去书名号。**只做这些**，不做相似度。"""
    s = (s or "").rsplit(".", 1)[0]
    return re.sub(r"[\s　《》<>]", "", s)


def coverage_index():
    """(归一文件名, 条款号) → [规则号]，外加比对盲区的统计。

    **只认硬证据**：文件名归一后相等（或一方包含另一方）且条款号相同。
    相似度猜测一概不做——把规则挂到错的制度文件上，比标「未匹配」危险得多：
    前者给出一条看起来有据可查、实际指错地方的证据链。
    """
    idx = {}
    blind = []
    for r in db.query("SELECT rule_id, source_clause FROM rule"):
        sc = r["source_clause"] or {}
        f, c = sc.get("file"), sc.get("clause")
        if not f or not c:
            blind.append({"rule_id": r["rule_id"], "why": "来源条款没写文件或条款号"})
            continue
        idx.setdefault((_norm_name(f), c.strip()), []).append(r["rule_id"])
    return idx, blind


def _covered_by(idx, file_name, clause_no):
    key_name = _norm_name(file_name)
    hits = []
    for (fname, cno), rules in idx.items():
        if cno != clause_no:
            continue
        if fname == key_name or fname in key_name or key_name in fname:
            hits.extend(rules)
    return sorted(set(hits))


# ── 第一步：条款切分（确定性，不出网）────────────────────────────────────────
def run_split(file_ids=None, only_changed=False):
    """对文件库里已解析出正文的文件切条款。**不调模型。**

    only_changed=True 时只处理「待抽取」的文件（没抽过的，加上抽过之后又被改的）。
    这是和文件库那条变化链路接起来的地方：扫描发现文件变了 → 这里只重切那几份。

    **`file_ids=[]`（空列表）表示「没有要处理的文件」，不是「全部」。**
    这条必须显式写出来：第一版是 `if file_ids:`，于是增量集合为空时这个判断为假，
    过滤条件整个被跳过 → 一次「增量抽取 0 份」变成了全量重切 190 条条款。
    同类陷阱在 run_ai 和 rule_detail 里各有一处，一并修了（都有单测钉住）。
    """
    from . import doclib

    task_id = "dlx-" + uuid.uuid4().hex[:12]
    explicit = file_ids is not None
    # 口径标签：only_changed 优先。增量抽取是**传定好的 file_ids + only_changed=True**
    # （范围由 L0 一次算定，见 extract_orchestrator），这种情况台账上要记「仅变化」，
    # 记成「指定文件」就分不出「人手点了几份」和「系统算出来的增量」。
    scope = "仅变化" if only_changed else ("指定文件" if explicit else "全部")
    db.execute("INSERT INTO doclib_extract_task(task_id, mode, scope) VALUES (%s,'条款切分',%s)",
               (task_id, scope))

    # 两种文本来源都要收：原生文本，和 OCR 补出来的。
    # **OCR 过期的不收**（文件在 OCR 之后又被换过）——拿旧版文本去抽现行制度的规则，
    # 抽出来的东西看着有据可查，实际对的是一份已经不存在的文件。
    sql = ["""SELECT file_id, file_name, rel_path, body, fingerprint,
                     parse_state, ocr_state, ocr_mean_conf
                FROM doc_lib_file
               WHERE NOT missing
                 AND ( (parse_state='已解析' AND body IS NOT NULL)
                    OR (ocr_state='已OCR'
                        AND ocr_fingerprint IS NOT DISTINCT FROM fingerprint) )"""]
    args = []
    if explicit:
        # 空列表也照样拼进去（`= ANY('{}')` 恒假，一份也不选），**不许跳过这个条件**
        sql.append("AND file_id = ANY(%s)"); args.append(list(file_ids))
    if only_changed:
        sql.append("AND (extracted_at IS NULL "
                   "     OR extract_fingerprint IS DISTINCT FROM fingerprint)")
    sql.append("ORDER BY rel_path")
    files = db.query(" ".join(sql), tuple(args) if args else None)

    idx, blind = coverage_index()
    total_clauses = candidates = 0
    per_file = []
    try:
        for f in files:
            # 重切之前先清掉这份文件的旧条款：文件改过之后，旧条款号可能整体位移，
            # 留着会让同一份文件出现两套条款，清单上看着像制度写了两遍。
            kind = kind_of(f["file_name"])
            if kind == "单据":
                # **单据不切条款。** 报账单明细表、审批意见里是业务数据
                # （单据编号、金额、人名），切进规则资产表等于把业务数据复制一份
                # 进来，比噪声更糟：规则资产和业务数据混在一起之后，
                # 「这条规则出自哪份制度」就再也说不清了。
                db.execute("DELETE FROM doclib_clause WHERE file_id=%s", (f["file_id"],))
                doclib.mark_extracted([f["file_id"]])
                per_file.append({"file_id": f["file_id"], "file_name": f["file_name"],
                                 "kind": kind, "clauses": 0, "candidates": 0,
                                 "text_source": None, "table_clauses": 0,
                                 "strategy": "跳过",
                                 "note": "业务单据，不是制度来源，不切条款"})
                continue
            n_cand = 0
            # 原生文本优先；没有才用 OCR 文本，并把页边界一起拿回来，
            # 好把每一条条款映射回它所在的页。
            if f["parse_state"] == "已解析" and f["body"]:
                text, source, spans = f["body"], "原生文本", []
            else:
                text, spans = ocr.text_with_pages(f["file_id"])
                source = "OCR"
            strategy, clauses = pick_strategy(text)
            over = max(0, len(clauses) - MAX_CLAUSES_PER_FILE)
            if over:
                clauses = clauses[:MAX_CLAUSES_PER_FILE]
            n_table = 0
            seen_no = {}      # (策略, 条款号) → 已出现几次。重复条款号靠它区分身份
            kept = []
            for seq, c in enumerate(clauses, 1):
                feats = features_of(c["body"])
                covered = _covered_by(idx, f["file_name"], c["clause_no"])
                sp = ocr.page_at(spans, c["pos"]) if spans else None
                from_table = bool(sp and sp["has_table"])
                src_conf = sp["mean_conf"] if sp else (
                    float(f["ocr_mean_conf"]) if source == "OCR" and f["ocr_mean_conf"]
                    else None)
                if from_table:
                    n_table += 1
                if set(feats) & CANDIDATE_FEATURES and not covered and kind in CANDIDATE_KINDS:
                    n_cand += 1
                k = (strategy, c["clause_no"])
                seen_no[k] = seen_no.get(k, 0) + 1
                cid = clause_ident(f["file_id"], strategy, c["clause_no"], seen_no[k])
                kept.append(cid)
                # **UPSERT，不是先删后插。** 落点是主键 clause_id（内容定址），
                # 同一条条款重切还是同一行，挂在它上面的草稿与审定结论就留住了。
                db.execute("""
                    INSERT INTO doclib_clause(clause_id, file_id, seq, clause_no, title,
                        body, char_pos, features, numbers, doc_kind, covered_by,
                        file_fingerprint, text_source, src_conf, from_table_page,
                        split_strategy)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (clause_id) DO UPDATE SET
                        seq = EXCLUDED.seq, title = EXCLUDED.title,
                        body = EXCLUDED.body, char_pos = EXCLUDED.char_pos,
                        features = EXCLUDED.features, numbers = EXCLUDED.numbers,
                        doc_kind = EXCLUDED.doc_kind, covered_by = EXCLUDED.covered_by,
                        file_fingerprint = EXCLUDED.file_fingerprint,
                        text_source = EXCLUDED.text_source, src_conf = EXCLUDED.src_conf,
                        from_table_page = EXCLUDED.from_table_page,
                        split_strategy = EXCLUDED.split_strategy,
                        extracted_at = now()""",
                    (cid, f["file_id"], seq, c["clause_no"],
                     c["title"], c["body"], c["pos"], feats, numbers_in(c["body"]),
                     kind, covered, f["fingerprint"], source, src_conf, from_table,
                     strategy))
            # 只删**这次没切出来**的条款。整份清空会连带 CASCADE 掉草稿和审定结论，
            # 实测丢过 14 条付费草稿。作废几条草稿这件事要说出来，不许静默。
            lost = db.scalar("""SELECT count(*) FROM doclib_draft d
                                  JOIN doclib_clause c ON c.clause_id = d.clause_id
                                 WHERE c.file_id = %s AND NOT (c.clause_id = ANY(%s))""",
                             (f["file_id"], kept)) or 0
            gone = db.execute("DELETE FROM doclib_clause WHERE file_id=%s "
                              "AND NOT (clause_id = ANY(%s))", (f["file_id"], kept))
            total_clauses += len(clauses)
            candidates += n_cand
            bits = []
            if gone:
                bits.append("清掉 %d 条这次没再切出来的旧条款%s"
                            % (gone, ("，连带作废 %d 条草稿" % lost) if lost else ""))
            if over:
                bits.append("只收前 %d 条，另有 %d 条超出单文件上限"
                            "（max_clauses_per_file）" % (MAX_CLAUSES_PER_FILE, over))
            if not clauses:
                bits.append("三种切分策略（条款体 / 层级编号 / 表格行）都没切出东西，"
                            "没有硬凑成一条——排版太特殊，需要人看一眼")
            else:
                bits.append("切分策略：%s" % strategy)
            if source == "OCR":
                bits.append("文字来自 OCR（可信度比原生文本低一档，数字需人工复核）")
            if n_table:
                bits.append("其中 %d 条出自**表格页**：OCR 丢的不是字是结构，"
                            "这些条款的数字与行列对应关系都不可信" % n_table)
            per_file.append({"file_id": f["file_id"], "file_name": f["file_name"],
                             "kind": kind, "clauses": len(clauses), "candidates": n_cand,
                             "text_source": source, "table_clauses": n_table,
                             "strategy": strategy,
                             "note": "；".join(bits) or None})
            doclib.mark_extracted([f["file_id"]])

        note = ("「未覆盖」不等于「缺规则」：定义、职责、流程叙述类条款本来就不该成规则。"
                "候选统计「非范本」文件里带阈值/禁止/强制/例外/名单制、"
                "且没有规则引用的条款。")
        db.execute("""UPDATE doclib_extract_task SET status='done', finished_at=now(),
                             file_cnt=%s, clause_cnt=%s, candidate_cnt=%s, note=%s
                       WHERE task_id=%s""",
                   (len(files), total_clauses, candidates, note, task_id))
    except Exception as e:                                     # noqa: BLE001
        db.execute("UPDATE doclib_extract_task SET status='failed', finished_at=now(), "
                   "error=%s WHERE task_id=%s", ("%s: %s" % (type(e).__name__, e), task_id))
        raise

    return {"task_id": task_id, "scope": scope, "files": len(files),
            "clauses": total_clauses, "candidates": candidates,
            "per_file": per_file, "coverage_blind": blind[:20],
            "coverage_blind_total": len(blind),
            "note": "候选 = 非范本文件里带规则特征、且现有规则没有引用过的条款。"
                    "要不要补规则是业务专家的判断，这里只负责指出来。"}


# ── 清单与统计 ──────────────────────────────────────────────────────────────
def list_clauses(file_id=None, kind=None, feature=None, covered=None,
                 q=None, limit=500):
    sql = ["""SELECT c.*, f.file_name, f.rel_path, f.fingerprint AS now_fingerprint
                FROM doclib_clause c JOIN doc_lib_file f ON f.file_id=c.file_id
               WHERE TRUE"""]
    args = []
    if file_id:
        sql.append("AND c.file_id=%s"); args.append(file_id)
    if kind:
        sql.append("AND c.doc_kind=%s"); args.append(kind)
    if feature:
        sql.append("AND %s = ANY(c.features)"); args.append(feature)
    if covered is True:
        sql.append("AND cardinality(c.covered_by) > 0")
    elif covered is False:
        sql.append("AND cardinality(c.covered_by) = 0")
    if q:
        sql.append("AND c.body ILIKE %s"); args.append("%%%s%%" % q)
    sql.append("ORDER BY f.rel_path, c.seq LIMIT %s")
    args.append(max(1, min(2000, int(limit))))
    rows = db.query(" ".join(sql), tuple(args))
    for r in rows:
        # 条款是切的时候那一版文件的。文件后来又被改了就必须说出来——
        # 否则清单看着是现行制度，实际是旧版。
        r["stale"] = r["file_fingerprint"] != r["now_fingerprint"]
        r["snippet"] = (r["body"] or "")[:SNIPPET]
        r["is_candidate"] = (bool(set(r["features"]) & CANDIDATE_FEATURES)
                             and not r["covered_by"]
                             and r["doc_kind"] in CANDIDATE_KINDS)
        # 可信度警告跟着条款走。**不要只在文件级提示**：
        # 一份 27 页的制度里只有 2 页是表格，文件级标记会让另外 25 页
        # 正常条款陪着一起被怀疑，而真正该怀疑的那两页反倒混在里面看不见了。
        r["trust_warning"] = (
            "出自表格页且字是 OCR 来的：数字可能都在，但行列对应关系不可信，"
            "请对照页面图人工核对" if r["from_table_page"] and r["text_source"] == "OCR"
            else "字来自 OCR，可信度比原生文本低一档，数字需人工复核"
            if r["text_source"] == "OCR" else "")
    return rows


def stats():
    by_kind = db.query("SELECT doc_kind, count(*) AS n FROM doclib_clause "
                       "GROUP BY doc_kind ORDER BY n DESC")
    total = db.scalar("SELECT count(*) FROM doclib_clause") or 0
    covered = db.scalar("SELECT count(*) FROM doclib_clause "
                        "WHERE cardinality(covered_by) > 0") or 0
    cand = db.scalar("""SELECT count(*) FROM doclib_clause
                         WHERE doc_kind = ANY(%s) AND cardinality(covered_by)=0
                           AND features && %s""",
                     (list(CANDIDATE_KINDS), list(CANDIDATE_FEATURES))) or 0
    stale = db.scalar("""SELECT count(*) FROM doclib_clause c
                          JOIN doc_lib_file f ON f.file_id=c.file_id
                         WHERE c.file_fingerprint <> f.fingerprint""") or 0
    feats = db.query("""SELECT unnest(features) AS feature, count(*) AS n
                          FROM doclib_clause GROUP BY 1 ORDER BY n DESC""")
    _, blind = coverage_index()
    return {"clauses": total, "covered": covered, "candidates": cand,
            "stale": stale, "by_kind": by_kind, "by_feature": feats,
            "drafts": db.scalar("SELECT count(*) FROM doclib_draft") or 0,
            "coverage_blind": len(blind),
            "note": "coverage_blind 是来源条款没写清文件/条款号的规则条数。"
                    "本项目现有规则多数把 source_clause.file 记成「规则表」，"
                    "那是人工整理清单时的来源标记、不是制度文件名——"
                    "**拿它比对必然大面积「未覆盖」，那是比对的盲区，"
                    "不是制度里真有那么多漏网条款。**"}


def list_tasks(limit=20):
    return db.query("SELECT * FROM doclib_extract_task ORDER BY started_at DESC LIMIT %s",
                    (max(1, min(200, int(limit))),))


# ── 第二步：AI 草稿 ─────────────────────────────────────────────────────────
SYSTEM = """你是差旅费报销制度的规则抽取助手。把给定的制度条款原文抽成可被规则
引擎执行的「规则八要素」草稿。

严格要求：
1. 只依据提供的条款原文。**不得引入原文中没有的任何数字、金额、天数、比例、
   职级、标准**。原文没写的就是没写。
2. 条款若属于定义、名词解释、职责分工、流程叙述、文件归档等不可判定内容，
   返回 {"可规则化": false, "原因": "..."}，不要硬凑成规则。
3. 任何无法从原文确定的字段填 null，并在 "缺口" 里写明缺什么。
4. 只输出 JSON，不要任何解释文字。

JSON 结构：
{"可规则化": true/false, "原因": "", "规则名称": "", "审核维度": "",
 "规则类型": "硬|混合|软", "适用范围": "", "判定逻辑": "", "例外情形": "",
 "风险等级": "高|中|低", "处置动作": "禁止|警告", "缺口": ""}"""

DRAFT_TEXT_FIELDS = ("规则名称", "适用范围", "判定逻辑", "例外情形", "处置动作", "缺口")


def _norm_num(s):
    return re.sub(r"[\s　,，]", "", s or "")


def _num_in(digits, src):
    """这个数**作为一个完整的数**是否出现在原文里。

    **不能用子串比。** 实测漏报：草稿写「陪餐不得超过 5 人」，原文里根本没有 5，
    但原文有「150 元」——而 "5" 是 "150" 的子串，于是这条幻觉被放过了。
    前后不许再接数字或小数点，才算同一个数。
    """
    return re.search(r"(?<![\d.])" + re.escape(digits) + r"(?![\d.])", src) is not None


# 查幻觉只看**阈值**，不看所有数字。两种形态算阈值：
#   带单位      「150 元」「1 次」「10 人」「三分之一」里的 3 不带单位不算
#   三位以上    「500」这种金额即使模型没写单位也要查
# 列表序号（1. 2. （6） 第3项）一律不算——它们是模型给答案编的号，不是阈值。
NUM_UNIT = re.compile(r"\d+(?:\.\d+)?\s*" + UNIT)
NUM_BIG = re.compile(r"(?<![\d.])\d{3,}(?:\.\d+)?(?![\d.])")
# 列表序号的几种写法，先从文本里剔掉再查。
# 「点号后面不许再跟数字」这条必须有：没有它，`15.5 元` 里的 `15.` 会被当成序号剔掉，
# 剩下 `5 元` 去比对——于是一个 15.5 元的阈值被报成「5 元是编的」。
# **一个正则改两个方向都要测**，这就是那个被测出来的方向。
LIST_MARK = re.compile(
    r"(^|[\s；;。，,、（(【\[])\s*[（(]?\d{1,2}(?:[）)、]|[.．](?!\d))", re.M)


def check_numbers(draft, source):
    """草稿里出现、而条款原文里没有的**阈值** —— 幻觉嫌疑。

    **这是能确定性检出的一类幻觉，必须查。**
    抽规则时编一个阈值出来比不抽更糟：它会以「有制度依据」的面目进入规则清单，
    而审定的人未必逐字回查原文。实测教训就在 0005 的表注释里——
    市内交通费那条漏掉 40 元一档，就会把正常单判成超标准；
    反过来凭空多一档，就会把超标单放过去。

    **但误报和漏报同样致命**（纪律第 9 条）。第一版拿「所有数字」去比，
    实测 8 条草稿里报了 3 条存疑，逐条回查原文后**全部是误报**：
    报的是判定逻辑里的列表序号（「1.」「2.」「（6）」），
    而真正的阈值（150 元/人天、500/450/400 元、10 人）一个都没编。
    红标喊三次狼来了，第四次真有狼也没人看了——所以这里只查阈值：
    带单位的，或者三位以上的裸数字。
    """
    src = _norm_num(source)
    bad = []
    for f in DRAFT_TEXT_FIELDS:
        v = draft.get(f)
        if not isinstance(v, str):
            continue
        # 先把列表序号剔掉，再找阈值。不剔的话「2.」里的 2 会被当成阈值。
        text = LIST_MARK.sub(" ", v)
        seen = set()
        for rx in (NUM_UNIT, NUM_BIG):
            for m in rx.finditer(text):
                t = _norm_num(m.group(0))
                digits = re.sub(r"[^\d.]", "", t)
                if not digits or digits in seen:
                    continue
                if t not in src and not _num_in(digits, src):
                    seen.add(digits)
                    bad.append({"field": f, "value": m.group(0).strip()})
    return {"ok": not bad, "suspect": bad[:8],
            "note": "suspect 里的阈值（带单位的数，或三位以上的裸数）在条款原文中"
                    "找不到，疑似模型编的。只查阈值不查所有数字：列表序号那种"
                    "「1. 2. （6）」报出来全是误报，而误报会让红标失效。"
                    "草稿照样保留，但清单上会打红标——删掉它等于掩盖模型的问题。"}


def _extract_cfg():
    """取 extract 用途的默认模型。没有就**明确报错**，不悄悄退化成不调模型。"""
    cfg = db.query_one("""SELECT * FROM llm_config
                           WHERE purpose='extract' AND is_default AND enabled""")
    if not cfg:
        raise RuntimeError(
            "没有为「extract（从制度文件抽规则）」用途设默认模型。"
            "去「大模型配置」页，在该用途那行选一条配置点「设为默认」。"
            "**不自动改用别的用途的模型**：读图模型和抽规则模型的要求不一样，"
            "悄悄顶替会让结果质量无从解释。")
    return cfg


def ai_draft_one(clause_row, cfg, send_limit=SEND_CHAR_LIMIT):
    """对一条条款调一次模型。返回 (draft, result, sent_text)。

    送进模型前先脱敏（纪律：送进模型前就要脱敏，不能只脱回来的）。
    制度条款正文里一般没有个人信息，但「一般没有」不是「一定没有」，
    而请求体一旦发出就离开了本系统的控制范围。
    """
    src = clause_row["body"] or ""
    sent = mask.mask_free_text(src[:send_limit])
    # 文本来源要如实告诉模型，尤其是「表格页 OCR」这一类。
    # 不说的话，模型会照着已经乱掉的行列编出「福州 420 元」这种**看起来完全合理**
    # 的对应关系——实测住宿费标准表 OCR 后数字全在、城市↔职级↔金额全错位。
    # 这种错误没有任何技术信号，下游会当成有制度依据的阈值。
    caveat = ""
    if clause_row.get("from_table_page") and clause_row.get("text_source") == "OCR":
        caveat = ("\n\n【重要】以下文字是**从表格页 OCR 出来的**，"
                  "行列对应关系很可能已经错位：数字也许都在，但「哪一行对哪一列」不可信。"
                  "**不得据此推断任何「某某对应某数值」的映射**。"
                  "若判定逻辑依赖这种对应关系，请返回 可规则化=false，"
                  "并在 原因 里写明需人工对照原页核对。")
    elif clause_row.get("text_source") == "OCR":
        caveat = ("\n\n【提示】以下文字由 OCR 识别得到，可能有错字或错数字。"
                  "凡拿不准的数值，请在 缺口 里写明「该数值需回查原文」。")
    user = ("文件：%s\n条款：%s %s%s\n\n条款原文：\n%s"
            % (clause_row.get("file_name") or "", clause_row["clause_no"],
               clause_row.get("title") or "", caveat, sent))
    res, content = llmc.chat_json(cfg, SYSTEM, user)
    if not res.ok:
        return ({"错误": res.message, "err_kind": res.err_kind}, res, sent)
    raw = (content or "").strip()
    if raw.startswith("```"):                       # 有的模型照样套代码块
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        draft = json.loads(raw)
    except ValueError:
        return ({"错误": "模型返回的不是 JSON", "模型原文": raw[:300]}, res, sent)
    return draft, res, sent


def run_ai(clause_ids=None, limit=AI_BATCH_DEFAULT, only_candidates=True, file_ids=None):
    """对候选条款跑 AI 草稿。**真出网、真计费。**

    **不是把整个文件库丢给模型**：一来费用与耗时不可控，二来发出去的内容
    越多越难交代。默认只挑「候选」（制度类、带规则特征、现有规则没覆盖的），
    一次最多 limit 条，由人决定要不要继续。

    `file_ids` 把候选**限定在这几份文件里**，供「增量抽取」用：
    增量只动了 2 份文件，就不该顺手把另外 12 份里积压的候选也买一遍——
    那笔钱花出去了，而使用者以为自己只抽了那 2 份。

    **`clause_ids=[]` 或 `file_ids=[]`（空列表）表示「没有要处理的」，不是「全部」。**
    这一条是这个函数里最贵的一个陷阱：第一版是 `if clause_ids:`，
    空列表时判断为假 → 落进 else 分支 → 对全库候选调模型。
    「增量 0 条」变成「买 8 条草稿」，而且钱花了才看得出来。
    """
    cfg = _extract_cfg()
    task_id = "dlx-" + uuid.uuid4().hex[:12]
    pick_clauses = clause_ids is not None
    pick_files = file_ids is not None
    scope = ("指定文件" if pick_clauses else ("仅变化" if pick_files else "全部"))
    db.execute("""INSERT INTO doclib_extract_task(task_id, mode, scope, llm_config_id)
                  VALUES (%s,'AI草稿',%s,%s)""", (task_id, scope, cfg["config_id"]))

    sql = ["""SELECT c.*, f.file_name FROM doclib_clause c
                JOIN doc_lib_file f ON f.file_id=c.file_id
               WHERE TRUE"""]
    args = []
    if pick_files:
        # 空列表照样拼（`= ANY('{}')` 恒假），**不许跳过**
        sql.append("AND c.file_id = ANY(%s)"); args.append(list(file_ids))
    if pick_clauses:
        sql.append("AND c.clause_id = ANY(%s)"); args.append(list(clause_ids))
    else:
        if only_candidates:
            sql.append("AND c.doc_kind = ANY(%s) AND cardinality(c.covered_by)=0 "
                       "AND c.features && %s")
            args.append(list(CANDIDATE_KINDS))
            args.append(list(CANDIDATE_FEATURES))
        # **成功过的不再重抽，失败过的可以重试。**
        # 判据是「模型答没答上来」（rulable 非空），不是「抽没抽过」：
        #   按「抽过」判 → 上次超时的那 7 条被判死刑，再也不会被抽；
        #   不判        → 排序是确定性的，每点一次都把同样 8 条重新买一遍，
        #                  72 条候选里另外 64 条永远排不到。
        # 指定 clause_ids 时不加这一条：那是人明确要求重抽某几条。
        sql.append("""AND NOT EXISTS (SELECT 1 FROM doclib_draft d
                                       WHERE d.clause_id = c.clause_id
                                         AND d.rulable IS NOT NULL)""")
    # 按文件再按序号：兑现 YAML 的「一份文件的候选一起跑」。
    # 只按 c.seq 排会跨文件交错取，一批 8 条散在 5 份文件里，没法按文件交代。
    sql.append("ORDER BY f.file_name, c.seq LIMIT %s")
    args.append(max(1, min(50, int(limit))))
    rows = db.query(" ".join(sql), tuple(args))

    made, failed, suspect = 0, 0, 0
    items = []
    try:
        for c in rows:
            draft, res, sent = ai_draft_one(c, cfg)
            ok = "错误" not in draft
            chk = check_numbers(draft, sent) if ok else {"ok": None, "suspect": [],
                                                         "note": "调用失败，未做数值核对"}
            did = "dld-" + uuid.uuid4().hex[:12]
            attempt = (db.scalar("SELECT count(*) FROM doclib_draft WHERE clause_id=%s",
                                 (c["clause_id"],)) or 0) + 1
            db.execute("""
                INSERT INTO doclib_draft(draft_id, task_id, clause_id, rulable, draft,
                    number_check, sent_chars, truncated, model, latency_ms, attempt)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (did, task_id, c["clause_id"],
                 bool(draft.get("可规则化")) if ok else None,
                 json.dumps(draft, ensure_ascii=False),
                 json.dumps(chk, ensure_ascii=False),
                 len(sent), len(c["body"] or "") > len(sent),
                 res.model_echo or cfg.get("model_name"), res.latency_ms, attempt))
            if ok:
                made += 1
                if chk["suspect"]:
                    suspect += 1
            else:
                failed += 1
            items.append({"draft_id": did, "clause_id": c["clause_id"],
                          "clause_no": c["clause_no"], "file_name": c["file_name"],
                          "ok": ok, "rulable": draft.get("可规则化"),
                          "draft": draft, "number_check": chk})
        db.execute("""UPDATE doclib_extract_task SET status='done', finished_at=now(),
                             clause_cnt=%s, draft_cnt=%s, note=%s WHERE task_id=%s""",
                   (len(rows), made,
                    "草稿不是规则：采纳要人点，且记 reviewer。数值核对不通过的 %d 条已打标"
                    % suspect, task_id))
    except Exception as e:                                     # noqa: BLE001
        db.execute("UPDATE doclib_extract_task SET status='failed', finished_at=now(), "
                   "error=%s WHERE task_id=%s", ("%s: %s" % (type(e).__name__, e), task_id))
        raise

    return {"task_id": task_id, "model": cfg.get("model_name"),
            "config_id": cfg["config_id"], "tried": len(rows), "drafted": made,
            "failed": failed, "number_suspect": suspect, "items": items,
            "note": "产出的是**草稿**不是规则。数值核对不通过的那些，"
                    "是草稿里出现了条款原文中没有的数字——必须人工回查原文。"}


def list_drafts(task_id=None, state=None, limit=200):
    sql = ["""SELECT d.*, c.clause_no, c.title, c.body AS clause_body, f.file_name
                FROM doclib_draft d
                JOIN doclib_clause c ON c.clause_id=d.clause_id
                JOIN doc_lib_file f ON f.file_id=c.file_id
               WHERE TRUE"""]
    args = []
    if task_id:
        sql.append("AND d.task_id=%s"); args.append(task_id)
    if state:
        sql.append("AND d.review_state=%s"); args.append(state)
    sql.append("ORDER BY d.created_at DESC LIMIT %s")
    args.append(max(1, min(1000, int(limit))))
    return db.query(" ".join(sql), tuple(args))


def review_draft(draft_id, state, reviewer, reason=None):
    """采纳 / 驳回。**采纳只是标记草稿，不自动写 rule 表**——
    写规则要填八要素里模型给不全的那几项（维度编号、优先级、绑定节点），
    那是下一页「规则本体」的事。这里自动写进去，等于让没人填过的字段
    带着默认值进了规则清单。"""
    if state not in ("采纳", "驳回"):
        raise ValueError("只能是「采纳」或「驳回」")
    if state == "驳回" and not (reason or "").strip():
        raise ValueError("驳回必须写原因——不写原因的驳回，下次还会抽出同一条")
    n = db.execute("""UPDATE doclib_draft SET review_state=%s, reviewer=%s,
                             reviewed_at=%s, reject_reason=%s WHERE draft_id=%s""",
                   (state, reviewer or "未署名", datetime.datetime.now(), reason, draft_id))
    return n > 0


# ── L2 协同：多源归并与冲突识别 ─────────────────────────────────────────────
# 归并键按**可判定的硬特征**算，不按相似度猜：主题词 + 控制对象，两条都对得上
# 才算同一条规则。**宁可不归并，不可错归并**——把两条不同的规则归到一起，
# 后果是其中一条的例外情形被当成另一条的，而例外正是八要素里最容易丢、
# 丢了最伤的一项。归不到一起只是少一条证据链，代价小得多。

TOPICS = [
    ("住宿费", ("住宿费", "住宿标准", "房租", "宾馆", "酒店")),
    ("城市间交通费", ("城市间交通", "交通费", "车票", "机票", "飞机", "高铁", "动车", "坐席")),
    ("市内交通费", ("市内交通", "市内用车", "公务用车", "出租车")),
    ("伙食补助费", ("伙食补助", "伙食费", "餐费补助")),
    ("业务接待", ("业务接待", "公务接待", "接待费", "陪餐", "用餐标准")),
    ("出差审批", ("出差审批", "审批单", "事前审批", "出差申请")),
    ("发票与附件", ("发票", "附件", "行程单", "报销凭证", "清单")),
    ("付款与主体", ("付款主体", "核算主体", "收款", "资金支付")),
]
OBJECTS = [
    ("标准", ("标准", "限额", "上限", "不得超过", "元", "额度")),
    ("等级", ("等级", "职级", "级别", "坐席", "舱位")),
    ("审批", ("审批", "批准", "核准", "授权")),
    ("凭证", ("发票", "附件", "单据", "清单", "凭证")),
]
CONTROL_BLOCK = ("不得", "禁止", "严禁", "不予", "不允许", "一律不", "不能")
CONTROL_WARN = ("应当", "应按", "需", "须", "提醒", "警告")
SCOPE_WORDS = ("省级", "厅局级", "处级", "科级", "正高", "副高", "分公司", "权属单位",
               "机关", "船舶")


def _topic_of(text):
    for name, kws in TOPICS:
        if any(k in text for k in kws):
            return name
    return None


def _object_of(text):
    for name, kws in OBJECTS:
        if any(k in text for k in kws):
            return name
    return None


def group_and_detect(clauses):
    """→ (groups, conflicts)。groups: {group_key: [clause…]}

    只对**非范本非单据**的条款归并：文书模板里的「第X条」是文书条款。
    """
    groups = {}
    for c in clauses:
        if c["doc_kind"] in ("范本", "单据"):
            continue
        body = (c.get("body") or "") + " " + (c.get("title") or "")
        topic, obj = _topic_of(body), _object_of(body)
        if not topic or not obj:
            continue                      # 判不出就不归并，不猜
        groups.setdefault("%s·%s" % (topic, obj), []).append(c)

    conflicts = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        if len(items) > MAX_GROUP_FOR_CONFLICT:
            # **组太大说明归并键太粗**，19 条讲住宿费的条款不是同一条规则。
            # 这时两两比对会炸出几十条冲突（实测「住宿费·标准」一组 72 条），
            # 人看两眼就不看了。**承认判不了，比报一堆虚惊强。**
            conflicts.append({"group_key": key, "kind": "适用范围不一致",
                              "detail": "这一主题下有 %d 条来源，归并键过粗，"
                                        "无法判定它们是否同一条规则——"
                                        "需人工先分组再比对口径" % len(items),
                              "left": items[0], "right": items[-1]})
            continue
        # 同一个 (组, 类型) 只报一条，把分歧的出处汇总进 detail。
        # 两两各报一条的话，n 条来源会报 n(n-1)/2 条，而它们说的是同一件事。
        seen = {}
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a["file_id"] == b["file_id"]:
                    continue
                for kind, detail in _diff(a, b):
                    seen.setdefault(kind, {"detail": detail, "left": a, "right": b,
                                           "n": 0})
                    seen[kind]["n"] += 1
        for kind, v in seen.items():
            d = v["detail"]
            if v["n"] > 1:
                d += "（该主题下另有 %d 处同类分歧）" % (v["n"] - 1)
            conflicts.append({"group_key": key, "kind": kind, "detail": d,
                              "left": v["left"], "right": v["right"]})
    return groups, conflicts


UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(元|%|％|天|日|个月|年|级|倍|人|次)$")
RANK_WORDS = ("省级", "厅局级", "处级", "科级", "正高", "副高", "县处级", "乡科级")
ORG_WORDS = ("分公司", "权属单位", "机关", "船舶", "本部", "集团")
MAX_GROUP_FOR_CONFLICT = 8


def _nums_by_unit(c):
    """条款里的数值，**按单位分桶**。→ {单位: {值}}

    不分桶就会拿百分比去比金额：实测报出「给的是 150%」vs「给的是 100元、1天」
    这种冲突 26 条，没有一条是真的。**不同单位之间不可比，不可比就不报。**
    """
    out = {}
    for n in (c.get("numbers") or []):
        m = UNIT_RE.match(re.sub(r"\s", "", n))
        if m:
            out.setdefault(m.group(2), set()).add(m.group(1))
    return out


def _control_of(text):
    """控制类型。**两类词只能占一边**，沾到就算会把几乎所有条款判成「禁止」。

    中文公文里「不得」和「应当」常常同时出现在一条里
    （「应当……不得……」），一方沾到禁止词就判禁止的做法，
    实测报出 81 条控制类型冲突，基本全是虚惊。
    判不出就返回 None，**不猜**。
    """
    has_block = any(w in text for w in CONTROL_BLOCK)
    has_warn = any(w in text for w in CONTROL_WARN)
    if has_block and not has_warn:
        return "禁止"
    if has_warn and not has_block:
        return "警告"
    return None


def _scope_of(text):
    """适用范围，**按维度分开**：职级是一个维度，组织是另一个维度。

    「分公司」和「厅局级」不是矛盾，是两件事。混在一起比，
    实测报出 18 条范围冲突，没有一条是真的矛盾。
    """
    return {"职级": {w for w in RANK_WORDS if w in text},
            "组织": {w for w in ORG_WORDS if w in text}}


def _diff(a, b):
    """两条条款之间**可确定性检出**的口径差异。判不出就不报。"""
    out = []
    ua, ub = _nums_by_unit(a), _nums_by_unit(b)
    for unit in set(ua) & set(ub):                 # 只比同单位
        if ua[unit] and ub[unit] and not (ua[unit] & ub[unit]):
            out.append(("阈值不一致",
                        "同为「%s」口径但取值不同：%s %s 给 %s；%s %s 给 %s"
                        % (unit, a["file_name"], a["clause_no"],
                           "/".join(sorted(ua[unit])),
                           b["file_name"], b["clause_no"],
                           "/".join(sorted(ub[unit])))))
    ca, cb = _control_of(a["body"]), _control_of(b["body"])
    if ca and cb and ca != cb:
        out.append(("控制类型不一致",
                    "%s %s 是「%s」；%s %s 是「%s」——同一件事一个拦一个只提醒"
                    % (a["file_name"], a["clause_no"], ca,
                       b["file_name"], b["clause_no"], cb)))
    sa, sb = _scope_of(a["body"]), _scope_of(b["body"])
    for dim in ("职级", "组织"):
        if sa[dim] and sb[dim] and not (sa[dim] & sb[dim]):
            out.append(("适用范围不一致",
                        "同为「%s」维度但对象不同：%s %s 限定 %s；%s %s 限定 %s"
                        % (dim, a["file_name"], a["clause_no"], "、".join(sorted(sa[dim])),
                           b["file_name"], b["clause_no"], "、".join(sorted(sb[dim])))))
    return out


SOURCE_KIND = {"条款体": "制度条款", "层级编号": "制度条款", "表格行": "规则表"}


def save_groups(groups, conflicts):
    """落库。**已忽略的冲突不复活**——下次抽取还会发现同一个冲突，
    没有这一档就会反复提示同一件事，提示多了就没人看了。"""
    ignored = {r["group_key"] + "|" + r["kind"]
               for r in db.query("SELECT group_key, kind FROM doclib_conflict "
                                 "WHERE state='已忽略'")}
    db.execute("DELETE FROM doclib_rule_source")
    n_src = 0
    for key, items in groups.items():
        for c in items:
            kind = SOURCE_KIND.get(c.get("split_strategy") or "", "制度条款")
            if "算法" in (c.get("file_name") or ""):
                kind = "配置器算法"
            db.execute("""INSERT INTO doclib_rule_source(
                              group_key, clause_id, file_id, source_kind, excerpt, confidence)
                          VALUES (%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (group_key, clause_id) DO NOTHING""",
                       (key, c["clause_id"], c["file_id"], kind,
                        (c.get("body") or "")[:400], c.get("text_source") or "原生文本"))
            n_src += 1

    n_conf, fresh = 0, []
    for cf in conflicts:
        if cf["group_key"] + "|" + cf["kind"] in ignored:
            continue
        cid = "conf-" + hashlib.sha1(
            ("%s|%s|%s|%s" % (cf["group_key"], cf["kind"],
                              cf["left"]["clause_id"], cf["right"]["clause_id"]))
            .encode("utf-8")).hexdigest()[:12]
        fresh.append(cid)
        db.execute("""INSERT INTO doclib_conflict(
                          conflict_id, group_key, kind, detail, left_clause, right_clause)
                      VALUES (%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (conflict_id) DO NOTHING""",
                   (cid, cf["group_key"], cf["kind"], cf["detail"],
                    cf["left"]["clause_id"], cf["right"]["clause_id"]))
        n_conf += 1

    # **「待裁决」是算出来的，随重算清；「已裁决/已忽略」是人拍的，永不清。**
    # 不清的后果实测过：检测器从 125 条误报收紧到 9 条之后，
    # 老的那 125 条仍然躺在表里是「待裁决」，于是
    #   页面上显示 134 处待裁决冲突（9 条真的 + 125 条上一版的误报），
    #   而且**它们会挡住采纳**——采纳的硬闸看的就是待裁决条数。
    # 换句话说：检测器改好了，台账却还按坏的那版在拦人。
    # 只删本次没检出的那些，本次检出的用 ON CONFLICT DO NOTHING 保住 created_at。
    stale = db.execute(
        "DELETE FROM doclib_conflict WHERE state = '待裁决' AND NOT (conflict_id = ANY(%s))",
        (fresh,))
    return n_src, n_conf, stale


def list_groups(limit=200):
    rows = db.query("""
        SELECT s.group_key, count(*) AS n,
               count(DISTINCT s.file_id) AS files,
               array_agg(DISTINCT s.source_kind) AS kinds,
               bool_or(s.confidence = 'OCR') AS has_ocr
          FROM doclib_rule_source s GROUP BY s.group_key
         ORDER BY count(*) DESC LIMIT %s""", (max(1, min(500, int(limit))),))
    for r in rows:
        r["evidence"] = ("多源印证" if r["n"] > 1 else "单一来源")
    return rows


def list_conflicts(state=None, limit=200):
    sql = ["SELECT * FROM doclib_conflict WHERE TRUE"]
    args = []
    if state:
        sql.append("AND state=%s"); args.append(state)
    sql.append("ORDER BY created_at DESC LIMIT %s")
    args.append(max(1, min(500, int(limit))))
    return db.query(" ".join(sql), tuple(args))


def resolve_conflict(conflict_id, state, who, resolution=None):
    if state not in ("已裁决", "已忽略"):
        raise ValueError("只能是「已裁决」或「已忽略」")
    if state == "已裁决" and not (resolution or "").strip():
        raise ValueError("裁决必须写结论——「谁拍的板、拍成什么」在制度冲突上"
                         "和结论本身一样重要")
    n = db.execute("""UPDATE doclib_conflict SET state=%s, resolved_by=%s,
                             resolved_at=now(), resolution=%s WHERE conflict_id=%s""",
                   (state, who or "未署名", resolution, conflict_id))
    return n > 0


# ── 增量预览：点之前要能看见这一下会动什么、会花多少钱 ──────────────────────
def incremental_preview():
    """「增量抽取」会处理哪些文件、会对多少条候选调模型。**只读，不出网。**

    为什么必须有这个预览：增量抽取的最后一步是 AI 草稿，**真出网、真计费**。
    一个只写着「增量抽取」的按钮，点下去之前使用者不知道这一下是动 0 份还是 14 份、
    是买 0 条草稿还是 8 条。本项目的纪律是「点一下不会意外花钱」，
    那就得先把要花的钱摆出来。

    候选条款数**只能给下限**，这一点要说清楚：
    从没抽过的文件此刻还没有条款（条款是 run_split 切出来的），
    它们的候选数要等切完才知道。编出一个总数比给下限更糟——
    那会被当成预算用，而它是假的。
    """
    from . import doclib

    pend = doclib.pending_extract(limit=500)
    files = pend["items"]
    ids = [r["file_id"] for r in files]
    why = {}
    for r in files:
        why[r["why"]] = why.get(r["why"], 0) + 1

    # 这几份文件里**已有条款**的候选数。判据与 run_ai 的挑选条件逐条对齐
    # （制度/其他类、带规则特征、现有规则没覆盖、还没成功出过草稿）——
    # 两处写法不同就会出现「预览说 5 条，实际买了 8 条」，而那种错只有对账才看得出来。
    known = 0
    unsplit = []
    if ids:
        known = db.scalar("""
            SELECT count(*) FROM doclib_clause c
             WHERE c.file_id = ANY(%s)
               AND c.doc_kind = ANY(%s) AND cardinality(c.covered_by) = 0
               AND c.features && %s
               AND NOT EXISTS (SELECT 1 FROM doclib_draft d
                                WHERE d.clause_id = c.clause_id
                                  AND d.rulable IS NOT NULL)""",
            (ids, list(CANDIDATE_KINDS), list(CANDIDATE_FEATURES))) or 0
        # 还没切过条款的那几份：它们的候选数现在算不出来，如实列出来
        have = {r["file_id"] for r in db.query(
            "SELECT DISTINCT file_id FROM doclib_clause WHERE file_id = ANY(%s)", (ids,))}
        unsplit = [r["file_name"] for r in files if r["file_id"] not in have]

    will_call = min(int(known), AI_BATCH_DEFAULT) if ids else 0
    if not ids:
        cost_note = ("增量为 0 份：点下去四步都空跑并如实报 0，"
                     "**不会向模型发出任何调用，也不会动库里现有的条款**。")
    else:
        cost_note = ("最后一步是 AI 草稿，**真出网、真计费**："
                     "本次最多对 %d 条候选调模型（单批上限 %d）。"
                     % (will_call, AI_BATCH_DEFAULT))
        if unsplit:
            cost_note += ("另有 %d 份文件还没切过条款，它们的候选数要切完才知道——"
                          "所以上面这个数是**下限**，不是总数。" % len(unsplit))

    return {
        "files": files,
        "total": len(files),
        "file_ids": ids,
        "by_why": [{"why": k, "n": v}
                   for k, v in sorted(why.items(), key=lambda kv: -kv[1])],
        "blocked": pend["blocked"],
        "blocked_total": sum(b["n"] for b in pend["blocked"]),
        "ai_batch_limit": AI_BATCH_DEFAULT,
        "candidates_known": int(known),
        "candidates_unknown_files": unsplit,
        "will_call_model": will_call,
        "cost_note": cost_note,
        "note": "增量 = **从没抽过的 + 抽过之后又被改过的**（按文件指纹比对，"
                "不是只取最近一次扫描的变化台账）。两者在「重扫一次没变、"
                "但有文件一直没抽过」时结果相反，这里取前者："
                "它是累计的、自愈的，连点两次第二次就是 0 份，"
                "而且不会静默漏掉一份。",
    }
