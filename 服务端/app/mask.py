# -*- coding: utf-8 -*-
"""脱敏。覆盖**全部出口**，不是只守主输出路径。

出口清单来自 `事实模型_v0.1.json` 的 masking.outlets，当前 9 个：
  AI 审核报告正文 / 审计接口 /audit/* / 调试查询 /debug/* / 配置接口 /config/*
  错误响应与异常堆栈 / 导出文件 xlsx·pdf / Prometheus 指标标签
  应用日志与 SQL 日志 / 大模型请求体

做安全的那个模块往往只守住一条路（比如只扫最终报告正文），而审计接口、
调试查询窗口、配置文件接口、错误信息这些旁路根本不经过它。
所以这里提供的不是「一个给报告用的函数」，而是三件东西：

  1. `scrub(obj)`        —— 任意 JSON 结构的递归脱敏，给所有接口响应用
  2. `MaskMiddleware`    —— 挂在 FastAPI 上，把**每一个** JSON 响应都过一遍
  3. `mask_text(s)`      —— 单串文本，给日志、报告正文、大模型请求体用

判断「有没有泄露」要把每个对外接口的响应都过一遍同一套规则，
而不是看这个模块写没写。`tests/test_mask_outlets.py` 就是这么断言的：
它遍历所有路由，对每条响应做一次「埋雷—检查」。

── 策略从哪来 ────────────────────────────────────────────────────────────────
6 条策略（M01~M06）定义在 `事实模型_v0.1.json` 里，不在本文件里重写一份。
理由：策略的 must_hit / must_not_hit 是测试数据，代码和测试必须读同一份，
否则改了一处漏了另一处，测试就会给出虚假的通过。

── 误报与漏报同样致命 ────────────────────────────────────────────────────────
漏报泄露隐私；误报把正常业务数字打成乱码——一个正常的浮点数被当成银行卡号，
整份报告就被打上不可信警告。所以：
  · 卡号用「前后不得紧邻数字」的边界断言 + 长度下限 16
  · 手机号同时卡「第二位 3~9」与前后边界
  · 姓名**不用通用模式**，用出差人姓名闭集合
实测反例（取自真实样本，不是编的）：金额 1374.80 / 34.11 / 3264 / 7209、
单据编号 FJPG-BX2610132121、业务小类编码 CL006004、时间 2026-10-13 17:20:52
—— 一个都不许被动。
"""
import io
import json
import re
import threading
import time

from . import config

# ─────────────────────────────────────────────────────────────────────────────
# 策略装载
# ─────────────────────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_POLICIES = None
_SENSITIVE_CONCEPTS = None
_FREETEXT_CONCEPTS = None
_OUTLETS = None


def _load():
    global _POLICIES, _SENSITIVE_CONCEPTS, _FREETEXT_CONCEPTS, _OUTLETS
    if _POLICIES is not None:
        return
    with _LOCK:
        if _POLICIES is not None:
            return
        fact = json.load(io.open(config.FACT_MODEL_JSON, encoding="utf-8"))
        _POLICIES = {p["id"]: p for p in fact["masking"]["policies"]}
        _SENSITIVE_CONCEPTS = {x["concept_id"]: x["field"] for x in fact["sensitive_fields"]}
        _FREETEXT_CONCEPTS = {f["concept_id"]: f["field"]
                              for f in fact["free_text_leak_paths"]["fields"]}
        _OUTLETS = list(fact["masking"]["outlets"])


def policies():
    _load()
    return _POLICIES


def outlets():
    _load()
    return _OUTLETS


def sensitive_concepts():
    _load()
    return dict(_SENSITIVE_CONCEPTS)


# ─────────────────────────────────────────────────────────────────────────────
# M02 银行账号 / M03 手机号：模式型
# ─────────────────────────────────────────────────────────────────────────────
# 不用 \d 而用 [0-9]：\d 在 Unicode 模式下会匹配全角数字和其他语言的数字，
# 范围比我们测过的要大。只认 ASCII 数字，范围与测试用例一致。
RE_CARD = re.compile(r"(?<![0-9])[0-9]{16,19}(?![0-9])")
RE_MOBILE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")


def _mask_card_one(m):
    v = m.group(0)
    return v[:6] + "*" * 12 + v[-4:]


def _mask_mobile_one(m):
    v = m.group(0)
    return v[:3] + "*" * 4 + v[-4:]


def mask_card(s):
    return RE_CARD.sub(_mask_card_one, s)


def mask_mobile(s):
    return RE_MOBILE.sub(_mask_mobile_one, s)


# ─────────────────────────────────────────────────────────────────────────────
# M01 姓名（字段级）/ M06 自由文本里的姓名（闭集合）
# ─────────────────────────────────────────────────────────────────────────────
# **不做通用姓名模式匹配**：单位名「闽运长乐公交公司」「厦门海隆」都是中文短词，
# 任何「2~3 个汉字的人名模式」都会把它们打掉。实测闭集合对 19 个单位名/部门名
# 误伤 0 个。代价是闭集合要随数据更新——这个代价必须付。
#
# 闭集合怎么来，这里有两处口径是 2026-10-31 改掉的，改的原因写在下面，别改回去：
#
# 【改动一】原来写的是「优先从库里取，取不到用种子名单」，但**第 1 条从来没有
#   实现**：set_name_set() 全项目只被 mask.py 自己用 _SEED_NAMES 调过一次，
#   库里的真实姓名从未进过闭集合。后果是第二实例「822港航」14 个员工姓名在
#   所有出口原样输出；而 find_leaks() 用的是同一个闭集合，所以自检也看不见
#   ——闸和自检一起瞎，测试全绿。现在改成**惰性从库里取**（见 _ensure），
#   不挂在任何启动钩子上：挂钩子的闸迟早会碰上没走那条路的入口（CLI、
#   pytest、后台任务），这个项目已经为此栽过一次。
#
# 【改动二】种子名单和库里的名单是**并集**，不是二选一。原口径「取到库就不用
#   种子」会在取到库的那一刻把种子里的 15 个真人丢掉——他们是 fjhy 的真人，
#   丢了就是新开一个洞。宁可多脱，不可少脱。
_SEED_NAMES = [
    "甘梓宁", "宓淑仪", "幸婉如", "蓝维安", "岑柏", "乐知行", "宿白", "逯承宇",
    "冼芷清", "宦允文", "笪澜生", "沃明远", "璩砺川", "郗岱", "鄢岚",
]

# 只出现在自由文本里、**任何结构化姓名列都查不到**的真人。
# 已知的一类是**代报单的被代报人**：FJPG-BX2607022137 由阙玥宁代报，
# 被代报人「靳纭芷」只写在 expense_bill.summary 里（「靳纭芷莆田差旅费」），
# submitter_no/payee/bill_traveler 全都没有她——所以 names_from_db() 按构造
# 取不到，verify_name_coverage() 也查不到（它只扫姓名列）。
#
# 为什么不做自动扫描：试过「把摘要脱敏后剩下的汉字串当候选」，29+69 张单的
# 摘要里出来 108 种候选，真名只有 1 个，其余全是单位名和业务词
# （厦门轮船公司、闽运长乐公交公司、参加集团审计……）。107:1 的噪音比，
# 这种报告没人会看第二次，等于没有。宽规则只配用来报告，而这个宽度连报告
# 都不配——所以这里用显式名单，宁可要「要手工维护」这个代价。
#
# **根因在装载侧，而且在我方这一侧**（2026-10-31 更正）。
#   原来这里写的是「822港航 的导出里没给她的工号，这是客户数据的缺口」。
#   **这句话是错的。** 员工信息导出(2026-10-27).xlsx 里一直有她：
#   工号 2005235、人力资源部、部门负责人。缺口是我方只读了报账单导出、
#   没读员工导出，装了 14 个报账人就收工。把自己没做的事记成客户的缺口，
#   清单发出去就是让客户去补他们已经给过的东西。
#   现在 49 人全量已入库，她从 employee.name 进闭集合（已实测在内）。
# 这个名字**仍然留着**：内置名单是库取不到时（纯正则单测、库重建中途）
#   的下限，口径是「宁可多脱，不可少脱」，留一个重复的名字不花钱。
# 新装载的数据如果又出现只在自由文本里露面的人，要往这里加——
# verify_name_coverage() 帮不了你（它只扫姓名列）。
_FREETEXT_ONLY_NAMES = [
    "靳纭芷",
]

# 姓名来源列分两类，**解析方式不一样，别合并成一张表**：
#   纯姓名列：整个值就是一个人名，原样收（不限字数，4 字姓名也要收得到）
#   复合列  ：值是「工号/姓名」这类拼接，要切开再挑（见 _name_tokens）
_PURE_NAME_COLS = (
    ("employee", "name"),
    ("bill_traveler", "name"),
    ("hotel_invoice", "guest_name"),
    ("expense_bill", "standard_basis_person"),
)
_COMPOSITE_NAME_COLS = (
    ("expense_bill", "payee"),            # 姓名/工号
    ("approval_record", "approver"),      # 工号/姓名，也有「姓名/工号 撤回单据」
)

# 内置名单 = 种子（40 张样本里的人）+ 自由文本专有（代报单的被代报人）。
# 两份分开定义、合起来用：来源和维护方式不一样，合并成一个列表就看不出
# 谁该在什么条件下删掉。
_BUILTIN_NAMES = _SEED_NAMES + _FREETEXT_ONLY_NAMES

_RE_CN_ONLY = re.compile(r"^[一-龥]{2,}$")
# 复合列里只收 2~3 字：实测复合值里混着 4 字动作词「撤回单据」和 4 字演示占位
# 「演示审核」，收进闭集合就会把界面上所有「撤回单据」打成星号——这是误报，
# 和漏报一样致命。纯姓名列不受这条限制，因为那一列按定义不会混进动作词。
_RE_CN_SHORT = re.compile(r"^[一-龥]{2,3}$")

_NAMES = None
_NAMES_SORTED = None
_NAMES_AT = 0.0
_NAMES_SRC = "未初始化"
_PINNED = False
# 库里新增了人（导入新实例的单据）之后，闭集合必须跟上。做法是定时过期而不是
# 「写入时失效」：失效钩子要挂在每一条写路径上，漏一条就是一个静默的漏报窗口。
_NAMES_TTL_S = 60.0


def _name_tokens(raw):
    """从复合列的值里取出姓名段。

    实测这一列有四种写法，顺序不固定、还可能黏着动作词：
        1000536/黄露琳          工号/姓名
        岑柏/1000507 撤回单据    姓名/工号 + 空格 + 动作词
        林若琪撤回              姓名直接黏着动作词，**没有分隔符**
        aiAudit/null           账号/空
    只收「切开后本身就是 2~3 个汉字」的 token。黏着动作词的那种收不到
    （「林若琪撤回」是 5 个字），靠的是同一个人在别处有干净写法
    （1000520/林若琪）兜住——这不是碰巧，装载程序两处写的是同一份名单。
    真有谁只以黏着形式出现，verify_name_coverage() 会把它报出来，
    而不是静悄悄放过。
    """
    for part in re.split(r"[/、,，;；\s]+", raw or ""):
        part = part.strip()
        if part and _RE_CN_SHORT.match(part):
            yield part


def names_from_db():
    """从库里取真实姓名闭集合。

    **不按实例过滤**：一条真名在哪个实例都是真名，脱敏是安全网，
    按实例切只会切出漏洞——切错一次，另一个实例的人就裸着。
    """
    from . import db  # 延迟导入：mask 要能在没有库的环境里被引入（纯正则单测）

    out = set()
    for t, c in _PURE_NAME_COLS:
        sql = "SELECT DISTINCT %s AS v FROM %s WHERE %s IS NOT NULL" % (c, t, c)
        for r in db.query(sql):
            v = (r["v"] or "").strip()
            if _RE_CN_ONLY.match(v):
                out.add(v)
    for t, c in _COMPOSITE_NAME_COLS:
        sql = "SELECT DISTINCT %s AS v FROM %s WHERE %s IS NOT NULL" % (c, t, c)
        for r in db.query(sql):
            out.update(_name_tokens(r["v"]))
    return out


def set_name_set(names):
    """手工钉住闭集合，**从此 TTL 不再自动刷新**，直到有人调 refresh_names()。

    为什么要钉住：不钉的话，手工注入的小名单会在 TTL 到期时被库里的名单
    悄悄盖掉，用例就变成「有时过有时不过」的随机失败。
    用完必须调 refresh_names() 还原——**不要用 set_name_set(_BUILTIN_NAMES)
    还原**，那会把后面所有用例钉在 16 个人的内置名单上，库里的人全不在了，
    于是后面任何「真人名该脱的照样脱」的用例都在一个假的小集合上跑。
    这个项目在「当前实例」那个全局单例上已经栽过同样的跟头。
    """
    global _NAMES, _NAMES_SORTED, _PINNED
    _PINNED = True
    with _LOCK:
        _NAMES = sorted({n.strip() for n in names if n and len(n.strip()) >= 2})
        # 长名优先：否则「岑柏」会先把「岑柏X」的前两个字替换掉，剩一个字裸露
        _NAMES_SORTED = sorted(_NAMES, key=len, reverse=True)


def _ensure(force=False):
    """惰性保证闭集合是新的。**不要把这个函数挪到启动钩子里**（见上方改动一）。"""
    global _NAMES_AT, _NAMES_SRC
    if _PINNED and not force:
        return
    now = time.monotonic()
    if not force and _NAMES is not None and (now - _NAMES_AT) < _NAMES_TTL_S:
        return
    try:
        got = names_from_db()
    except Exception as e:
        if _NAMES is not None:
            # 已经有一份（含库里的人），取不到新的就**保留旧的**，
            # 绝不退化成更小的种子名单——那是把已经堵上的洞重新打开。
            _NAMES_AT = now
            _NAMES_SRC += "；刷新失败(%s)，仍用上一份" % type(e).__name__
            return
        got, src = set(), "退回内置名单 %d 人（取库失败：%s）" % (
            len(_BUILTIN_NAMES), type(e).__name__)
    else:
        src = "库 %d 人 ∪ 内置 %d 人" % (len(got), len(_BUILTIN_NAMES))
    set_name_set(set(_BUILTIN_NAMES) | got)
    _NAMES_AT = now
    _NAMES_SRC = src


def refresh_names():
    """解除钉住并立刻重取闭集合。
    装载完新实例的人员数据后调它，不用等 TTL；测试里用它还原。"""
    global _PINNED
    _PINNED = False
    _ensure(force=True)
    return list(_NAMES)


def name_set_source():
    """闭集合是从哪来的、几个人。给运行期自检和排障用：
    「退回内置名单」出现在日志里就等于有人在裸奔，要当故障看，不是提示。"""
    _ensure()
    return _NAMES_SRC


def name_set():
    _ensure()
    return list(_NAMES)


def _names_sorted():
    _ensure()
    return _NAMES_SORTED


_DIGITS = re.compile(r"^[0-9]+$")


def mask_name(s):
    """M01：作用在已知的姓名字段上，保留姓氏。

    **带斜杠的复合格式里，姓名和工号的顺序不固定**，不能按位置猜：
        approval_record.approver = 工号/姓名   如 1000504/甘梓宁
        expense_bill.payee       = 姓名/工号   如 岑柏/1000507
    第一版固定把斜杠后半当姓名，于是 payee 被脱成「岑柏/1******」——
    工号脱了、姓名裸着，正好反了。自检（find_leaks）把它抓了出来。
    现在按「哪一侧是纯数字」判断，判不出来时两侧都当姓名脱（偏保守）。
    """
    if not isinstance(s, str):
        return s
    t = s.strip()
    if len(t) < 2:
        return s
    if "/" in t:
        head, _, tail = t.rpartition("/")
        if _DIGITS.match(head.strip()):               # 工号/姓名
            left, right = _mask_emp_no(head), _mask_one_name(tail)
        elif _DIGITS.match(tail.strip()):             # 姓名/工号
            left, right = _mask_one_name(head), _mask_emp_no(tail)
        else:                                          # 判不出来：两侧都按姓名脱
            left, right = _mask_one_name(head), _mask_one_name(tail)
        return left + "/" + right
    return _mask_one_name(t)


def _mask_one_name(t):
    t = (t or "").strip()
    if not t or t == "null":
        return t
    if len(t) < 2:
        return t
    return t[0] + "*" * (len(t) - 1)


def _mask_emp_no(s):
    """M04 工号：保留后 2 位，**再过一遍闭集合姓名**。

    为什么要再过一遍：种子数据里有形如 `SMP-岑柏` 的占位工号，
    「保留后 2 位」保留的恰好是那两个字的姓名。
    单靠「保留后 N 位」这种位置规则，碰上「标识里嵌了姓名」就会漏。
    这一层是兜底，真正的修法是别把姓名嵌进标识（见 app/demo/seed.py 的 _emp）。
    单据编号不走这里——它是逐张单对账的唯一抓手，不脱。
    """
    if not s:
        return s
    t = s if len(s) <= 2 else "*" * (len(s) - 2) + s[-2:]
    return mask_free_text(t)


def mask_free_text(s):
    """M06：自由文本里的姓名，按闭集合逐个替换。"""
    if not isinstance(s, str) or not s:
        return s
    for n in _names_sorted():
        if n in s:
            s = s.replace(n, n[0] + "*" * (len(n) - 1))
    return s


# ─────────────────────────────────────────────────────────────────────────────
# M05 大模型 API Key
# ─────────────────────────────────────────────────────────────────────────────
RE_APIKEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")


def mask_api_key(s):
    if not isinstance(s, str):
        return s
    return RE_APIKEY.sub(lambda m: "sk-****" + m.group(0)[-4:], s)


def key_mask(plain):
    """给 llm_config.api_key_mask 用：由明文算出展示用掩码。"""
    if not plain:
        return None
    tail = plain[-4:] if len(plain) >= 4 else ""
    return "sk-****" + tail


# ─────────────────────────────────────────────────────────────────────────────
# 文本总入口
# ─────────────────────────────────────────────────────────────────────────────
def mask_text(s):
    """任意文本的全量脱敏：卡号 → 手机号 → API Key → 闭集合姓名。

    顺序有讲究：先做模式型（卡号、手机号），再做闭集合姓名。
    反过来做的话，姓名替换产生的「*」不会影响数字模式，其实两种顺序都对；
    但卡号与手机号的正则有重叠风险（19 位数字里能切出 11 位手机号段），
    所以卡号必须先走——它的长度下限更高，先匹配掉长的，短的就不会误切。
    """
    if not isinstance(s, str) or not s:
        return s
    if config.MASK_MODE == "off":
        return s
    s = mask_card(s)
    s = mask_mobile(s)
    s = mask_api_key(s)
    s = mask_free_text(s)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# 结构化出口：按字段名 + 按概念 ID
# ─────────────────────────────────────────────────────────────────────────────
# 字段名 → 策略。键用小写，匹配时也小写，兼容驼峰与下划线两种命名。
_FIELD_POLICY = {
    # M01 姓名类
    "payee": "name", "approver": "name", "guest_name": "name", "reviewer": "name",
    "name": "name", "emp_name": "name", "traveler": "name", "submitter": "name",
    "claimant": "name",
    # M02 账号
    "payee_account": "card", "account": "card", "bank_account": "card",
    # M03 手机
    "mobile": "mobile", "phone": "mobile", "tel": "mobile",
    # M04 工号
    "emp_no": "emp_no", "employee_no": "emp_no", "work_no": "emp_no",
    # M05 Key
    "api_key": "key", "api_key_cipher": "key", "apikey": "key", "secret": "key",
    "password": "key", "api_key_plain": "key",
    # M06 自由文本
    "summary": "free", "remark": "free", "opinion": "free", "pay_purpose": "free",
    "return_reason": "free", "reason": "free", "message": "free",
    "llm_rationale": "free", "note": "free", "detail_text": "free",
    "prompt": "free", "error": "free", "traceback": "free",
    # 2026-10-22 新增：attachment_extract 开始往 invoice.remark_col 写真实
    # 发票备注栏原文（已实测里面带过携程订单号+同行人姓名）。落在未命中名单
    # 的字段本来就会走 scrub() 的兜底（同样过 mask_text），这里显式列出
    # 不是补洞，是让「为什么没漏」查得到依据，不靠兜底碰巧接住。
    "remark_col": "free",
}

# 这些字段**永远不脱**。它们是对账抓手，脱了演示就没法逐张核对。
# 列出来是为了让「为什么这个字段没被脱」有据可查，而不是靠正则碰巧没命中。
_NEVER_MASK = {
    "bill_no", "voucher_no", "run_id", "cycle_id", "snapshot_id", "signal_id",
    "rule_id", "clause_id", "doc_id", "task_id", "release_id", "config_id",
    "concept_id", "entity_id", "enum_id", "org_id", "app_no", "invoice_id",
    "amount", "pay_amount", "tax", "total_amount", "tax_amount", "tax_rate",
    "limit_amount", "alloc_amount", "budget_amount", "used_amount", "deviation",
}


def _apply(policy, value):
    """字段级策略。**每条策略的结果都再过一遍闭集合姓名**。

    位置型规则（保留前 N 后 M）遇到「标识里嵌了姓名」就会漏，
    实测 `SMP-岑柏` 就是这么漏出去的。多走一层闭集合是便宜的兜底。
    """
    out = _apply_raw(policy, value)
    return mask_free_text(out) if isinstance(out, str) else out


def _apply_raw(policy, value):
    if policy == "name":
        return mask_name(value)
    if policy == "card":
        return mask_card(value) if isinstance(value, str) else value
    if policy == "mobile":
        return mask_mobile(value) if isinstance(value, str) else value
    if policy == "emp_no":
        return _mask_emp_no(value) if isinstance(value, str) else value
    if policy == "key":
        # Key 一律不回明文，连掩码都由上层显式提供
        return "***"
    if policy == "free":
        return mask_text(value)
    return mask_text(value)


def mask_concept(concept_id, value):
    """按本体概念 ID 脱敏。给 S 阶段的快照入库用。"""
    if config.MASK_MODE == "off":
        return value
    sens = sensitive_concepts()
    if concept_id in ("C0124",):
        return mask_card(value) if isinstance(value, str) else value
    if concept_id in ("C0125",):
        return mask_text(value)
    if concept_id in ("C0123", "C0603", "C1203", "C0802"):
        return mask_name(value)
    if concept_id == "C0801":
        return _mask_emp_no(value) if isinstance(value, str) else value
    if concept_id in _FREETEXT_CONCEPTS:
        return mask_text(value)
    if concept_id in sens:
        return mask_text(value)
    return value


def scrub(obj, _depth=0):
    """递归脱敏任意 JSON 结构。**这是所有接口响应的统一出口。**

    两层防护：
      1) 按字段名命中策略的，走对应策略
      2) 其余所有字符串，一律过 mask_text（卡号/手机号/Key/闭集合姓名）
    第 2 层是关键：新加一个字段、换一个命名风格，都不会漏——
    漏的那条路正是「做安全的模块只守主输出路径」的典型长相。
    """
    if config.MASK_MODE == "off":
        return obj
    if _depth > 64:          # 防御畸形输入导致的递归爆栈
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _NEVER_MASK:
                out[k] = v
                continue
            pol = _FIELD_POLICY.get(lk)
            if pol is not None and isinstance(v, str):
                out[k] = _apply(pol, v)
            elif pol == "key":
                out[k] = "***"
            else:
                out[k] = scrub(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [scrub(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub(v, _depth + 1) for v in obj)
    if isinstance(obj, str):
        return mask_text(obj)
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# 自检：泄露探测
# ─────────────────────────────────────────────────────────────────────────────
def find_leaks(obj):
    """在已脱敏的结构里反查还有没有漏网的。给测试和运行期自检用。

    返回 [(路径, 类型, 片段)]。空列表表示没查出泄露。
    **「没查出」不等于「没有」**——这个函数只认它认识的三种模式加闭集合姓名。
    所以测试里既要用它做断言，也要有「埋雷—检查」的正向用例。
    """
    hits = []

    def walk(node, path, key=None):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + "." + str(k), str(k).lower())
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, path + "[%d]" % i, key)
        elif isinstance(node, str):
            # **白名单字段不做「数字模式」检查。** 它们是对账抓手（单据号、
            # 周期号、信号号、金额），按设计就不脱敏；拿卡号/手机号正则去扫它们
            # 只会产生假泄露。姓名检查仍然做——标识里不该出现姓名。
            if key in _NEVER_MASK:
                for n in _names_sorted():
                    if n in node:
                        hits.append((path, "name", n))
                        break
                return
            if RE_CARD.search(node):
                hits.append((path, "card", RE_CARD.search(node).group(0)))
            if RE_MOBILE.search(node):
                hits.append((path, "mobile", RE_MOBILE.search(node).group(0)))
            if RE_APIKEY.search(node):
                hits.append((path, "api_key", "sk-***"))
            for n in _names_sorted():
                if n in node:
                    hits.append((path, "name", n))
                    break

    walk(obj, "$")
    return hits


# 这些词出现在姓名列里但**不是人名**，脱敏后它们理应原样留着。
# 列出来是为了让 verify_name_coverage 能把「真的漏了一个人」和
# 「这本来就不是人名」分开——否则自检会报一堆噪音，然后没人再看它。
_NON_NAME_IN_NAME_COLS = ("撤回单据", "撤回", "演示审核", "null")

# 收款人可以是**单位**：2026-10-31 装 822港航 支付区时，payee 里出现了
# 「福州市八方物业管理有限公司」。它不是人名，脱敏后理应原样留着，
# 但自检只按「还剩 2 个以上连着的汉字」判，就把它报成了欠脱。
# 不往黑名单里加这一个供应商名——下一张单会换一个，黑名单追不上。
# 按**单位名后缀**判：中文人名是 2~4 字，带这些后缀又超过 3 字的，
# 不可能是人名。
#
# 代价说在这里：一个以人名起头的单位名（「麴渺物业有限公司」），
# 如果那个人恰好**只**以这种形式出现过、别处没有干净写法，
# 这条规则会让自检放过它。换来的是自检不报供应商噪音——
# 按 9 条纪律，误报会让人把断言删掉，那时漏报就一个都拦不住了。
# 这种情况靠 names_from_db 的纯姓名列兜（收款人是个人时另有一行）。
_ORG_SUFFIX = (
    "公司", "集团", "中心", "银行", "医院", "酒店", "宾馆", "饭店", "学校",
    "学院", "大学", "事务所", "工作室", "合作社", "协会", "基金会",
    "研究院", "研究所", "管理局", "管理处", "服务部", "经营部", "商行",
    "超市", "门市", "厂", "店", "社",
)


def looks_like_org(s):
    """像不像单位名。只在自检里用来排噪音，**不参与脱敏决策**。

    脱敏本身不看这个函数：该脱的照脱。这里只回答
    「残留的这串汉字值不值得报成『漏了一个人』」。
    """
    s = (s or "").strip()
    return len(s) > 3 and s.endswith(_ORG_SUFFIX)


def verify_name_coverage(with_ignored=False):
    """**反向**自检：直接去姓名来源列里查，有没有哪个真名没进闭集合。

    为什么必须有这个函数：find_leaks() 查泄露用的是**同一个闭集合**，
    闭集合漏了谁，find_leaks 就看不见谁。2026-10-31 之前整个项目就栽在
    这上面——822港航 14 个人全在裸奔，而脱敏测试一条不红。
    用闸自己的规则去验闸，验的是「规则有没有被执行」，不是「规则对不对」。

    返回 [(表.列, 原值, 脱敏后仍剩下的汉字片段)]，空列表表示源列已全覆盖。
    `with_ignored=True` 时返回 (漏的, 按单位名跳过的)——**跳过的要能列出来**，
    「查过是阴性」和「碰巧没看见」是两回事，不许只在代码里默默 continue。
    """
    from . import db

    _ensure()
    out, skipped = [], []
    for t, c in _PURE_NAME_COLS + _COMPOSITE_NAME_COLS:
        sql = "SELECT DISTINCT %s AS v FROM %s WHERE %s IS NOT NULL" % (c, t, c)
        for r in db.query(sql):
            raw = (r["v"] or "").strip()
            if not raw:
                continue
            left = mask_free_text(raw)
            for w in _NON_NAME_IN_NAME_COLS:
                left = left.replace(w, "")
            # **残留 1 个汉字是对的，不是漏**：脱敏按设计保留姓氏
            # （「张灵」→「张*」）。只有连着 2 个及以上汉字还站着，
            # 才说明这个名字整体没进闭集合。第一版没区分，报了 116 处
            # 全是姓氏，噪音盖住信号——自检报假阳性，下次就没人看它了。
            rest = [s for s in re.findall(r"[一-龥]+", left) if len(s) >= 2]
            if rest and all(looks_like_org(s) for s in rest):
                skipped.append(("%s.%s" % (t, c), raw, "".join(rest)))
                continue
            if rest:
                out.append(("%s.%s" % (t, c), raw, "".join(rest)))
    return (out, skipped) if with_ignored else out


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 中间件：把每一个 JSON 响应都过一遍
# ─────────────────────────────────────────────────────────────────────────────
class MaskMiddleware:
    """ASGI 中间件。覆盖**所有**返回 application/json 的响应，包括错误响应。

    为什么做成中间件而不是在每个 handler 里调 scrub：
      handler 会越写越多，总有人忘。而且异常处理器返回的 500 响应
      根本不经过 handler——错误信息里带堆栈和参数值，是最容易漏的出口之一。
      挂在中间件上，新增路由自动被覆盖。

    非 JSON 响应（文件下载、SSE）不在这里处理：
      导出文件的脱敏在生成时做（见 app/api/export.py），
      因为 xlsx/pdf 是二进制，拿到字节流已经晚了。
      这一点必须在测试里单独覆盖，不能假定中间件管了全部。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        chunks = []
        status_holder = {}
        headers_holder = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers_holder["headers"] = message.get("headers") or []
                return                                  # 先不发，等拿到 body 再决定
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                await self._flush(send, status_holder, headers_holder, chunks)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _flush(self, send, status_holder, headers_holder, chunks):
        raw = b"".join(chunks)
        headers = headers_holder.get("headers") or []
        ctype = b""
        for k, v in headers:
            if k.lower() == b"content-type":
                ctype = v
                break
        body = raw
        if b"application/json" in ctype and raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = None
            if data is not None:
                body = json.dumps(scrub(data), ensure_ascii=False).encode("utf-8")
        new_headers = []
        for k, v in headers:
            if k.lower() == b"content-length":
                continue                                 # 长度变了，重算
            new_headers.append((k, v))
        new_headers.append((b"content-length", str(len(body)).encode()))
        # 让出口可观测：响应头上标一下脱敏状态，便于联调时发现中间件没生效
        new_headers.append((b"x-fjhy-masked", b"1" if config.MASK_MODE == "on" else b"0"))
        await send({"type": "http.response.start",
                    "status": status_holder.get("status", 200),
                    "headers": new_headers})
        await send({"type": "http.response.body", "body": body})
