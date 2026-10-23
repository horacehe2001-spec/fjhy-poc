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
# 闭集合的来源优先级：
#   1) 运行期从库里取（employee.name + expense_bill 的提单人/报账人 + 审批人）
#   2) 取不到时用种子名单（来自 40 张样本，仅用于离线测试与演示）
# **不做通用姓名模式匹配**：单位名「闽运长乐公交公司」「厦门海隆」都是中文短词，
# 任何「2~3 个汉字的人名模式」都会把它们打掉。实测闭集合对 19 个单位名/部门名
# 误伤 0 个。代价是闭集合要随数据更新——这个代价必须付。
_SEED_NAMES = [
    "甘梓宁", "宓淑仪", "幸婉如", "蓝维安", "岑柏", "乐知行", "宿白", "逯承宇",
    "冼芷清", "宦允文", "笪澜生", "沃明远", "璩砺川", "郗岱", "鄢岚",
]
_NAMES = None
_NAMES_SORTED = None


def set_name_set(names):
    """由运行时在启动后注入真实姓名闭集合。"""
    global _NAMES, _NAMES_SORTED
    with _LOCK:
        _NAMES = sorted({n.strip() for n in names if n and len(n.strip()) >= 2})
        # 长名优先：否则「岑柏」会先把「岑柏X」的前两个字替换掉，剩一个字裸露
        _NAMES_SORTED = sorted(_NAMES, key=len, reverse=True)


def name_set():
    if _NAMES is None:
        set_name_set(_SEED_NAMES)
    return list(_NAMES)


def _names_sorted():
    if _NAMES_SORTED is None:
        set_name_set(_SEED_NAMES)
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
