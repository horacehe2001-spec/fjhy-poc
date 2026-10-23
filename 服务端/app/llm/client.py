# -*- coding: utf-8 -*-
"""对厂商的实际调用。**这是整个系统唯一出网的地方。**

两个动作：
  list_models(cfg)  GET  {base_url}/models          列出可用型号
  chat_ping(cfg)    POST {base_url}/chat/completions 真发一句话

【为什么"测试"不能只看 HTTP 200】
网关在 Key 无效或额度耗尽时也可能回 200，body 里放一个错误对象。
只看状态码的测试会显示「连通正常」，然后跑批时全部失败。
所以 chat_ping 要求**看到模型真的说了话**（choices[0].message.content 非空），
并且把回复前 200 字带回去给人看——那段文字就是证据。

【为什么要比对 model 回显】
厂商/网关静默降级到别的型号是常见事：配的 glm-5.3，实际服务 glm-4-flash。
不比对就会拿一个降级后的结果去评估配置型号的能力。
响应里的 model 字段和配置不一致时，这里标出来，不替用户决定它要不要紧。

【为什么错误要分类】
分类的唯一标准是「下一步该干什么」：
  auth / quota        → 去厂商控制台，不是改代码
  dns / timeout / tls → 网络或代理，不是 Key
  model_not_found     → 型号填错，用「拉取型号」重选
  no_key              → 还没填 Key
混成一句「调用失败」等于什么都没说，用户只能瞎试。

【Key 绝不出现在返回值里】
厂商的 4xx 报文**经常把 Authorization 头原样回显**。错误原文要显示给用户
（不显示就没法排错），于是它成了又一个出口。这里每条返回的文本都过一遍
mask.mask_api_key + 显式剔除明文 Key 本身；库里还有 ck_llm_test_no_key
作为最后一道防线。
"""
import base64
import io
import time

import httpx

from app import mask
from app.llm import crypto

PING_PROMPT = "请回复两个字：收到"
EXCERPT_LIMIT = 200


class Result(object):
    """一次测试的结果。字段名和 llm_test_log 的列一一对应，便于直接落库。"""

    def __init__(self, kind, ok, http_status=None, latency_ms=None,
                 model_echo=None, reply_excerpt=None, err_kind=None,
                 message=None, models=None,
                 prompt_tokens=None, completion_tokens=None):
        self.kind = kind
        self.ok = ok
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.model_echo = model_echo
        self.reply_excerpt = reply_excerpt
        self.err_kind = err_kind
        self.message = message
        self.models = models or []
        # 厂商回的 usage。**没有它就算不出成本、也比不了不同型号的性价比**；
        # llm_test_log 没有这两列（它只记连通性），落库的是 llm_call_log。
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def as_dict(self):
        return {"kind": self.kind, "ok": self.ok, "http_status": self.http_status,
                "latency_ms": self.latency_ms, "model_echo": self.model_echo,
                "reply_excerpt": self.reply_excerpt, "err_kind": self.err_kind,
                "message": self.message, "models": self.models,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens}


def _scrub(text, plain_key=None):
    """返回给前端、写进库之前，把可能的 Key 抹掉。"""
    if not text:
        return text
    s = str(text)
    if plain_key and len(plain_key) >= 6:
        s = s.replace(plain_key, "***")
    s = mask.mask_api_key(s)
    return s[:1000]


def _base(url):
    return (url or "").strip().rstrip("/")


def _headers(plain_key):
    h = {"Content-Type": "application/json",
         "User-Agent": "fjhy-poc-llm-test/0.1"}
    if plain_key:
        h["Authorization"] = "Bearer " + plain_key
    return h


def _classify_transport(exc):
    """把 httpx 的异常翻成「下一步该干什么」。"""
    s = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "连接或读取超时。检查网络/代理，或把超时设长一点"
    if "certificate" in s or "ssl" in s or "tls" in s:
        return "tls", "TLS 握手失败。常见于公司代理做了中间人，或地址的证书不匹配"
    if "getaddrinfo" in s or "name or service not known" in s or "nodename" in s:
        return "dns", "域名解析不了。地址可能拼错，或这台机器上不了外网"
    # **「断开」要排在「连不上」前面判。** `disconnected` 里含 `connect`，
    # 落到下面那条就会被说成「连不上这个地址，端口/路径可能不对」——
    # 而实际是连上了、请求也发出去了，对端中途把连接掐了。
    # 这两种的下一步完全不同：一个去查地址配置，一个去查网络/代理/限流。
    # 实测把人带偏过：rule_judge 的失败全被报成「服务没起（私有化部署常见）」，
    # 而那是公有云 dashscope。
    if "disconnect" in s or "connection reset" in s or "aborted" in s \
            or "10053" in s or "10054" in s:
        return "http", ("连接已建立但中途被断开（请求可能已经发出去了）。"
                        "多见于网络抖动、代理超时或对端限流——不是地址配错")
    if "refused" in s or "connect" in s:
        return "http", "连不上这个地址。端口/路径可能不对，或服务没起（私有化部署常见）"
    return "http", "网络层失败"


def _classify_http(status, body_text):
    b = (body_text or "").lower()
    if status in (401, 403):
        return "auth", "鉴权被拒。Key 不对、或这个 Key 不属于这个地址（国内站/国际站不通用）"
    if status in (402, 429) or "insufficient" in b or "quota" in b or "余额" in b or "欠费" in b:
        return "quota", "额度或频率问题。去厂商控制台看余额与限流，不是代码问题"
    if status == 404 or "model not found" in b or "模型不存在" in b or "does not exist" in b:
        return "model_not_found", "型号或路径不存在。用「拉取型号」重新选一个"
    if status == 400:
        return "http", "请求被拒（400）。常见于型号名写错、或这家要求的字段不一样"
    return "http", "HTTP %s" % status


def _client(cfg, plain_key, write_timeout=10.0):
    # connect 单独给 6 秒：地址写错时应该很快失败，
    # 不要让用户为了看到「地址错了」等满 120 秒。
    #
    # write 默认 10 秒是按**文本请求**定的（几 KB，10 秒绰绰有余）。
    # 读图不一样：一页 170dpi 的扫描件 PNG 约 450 KB，base64 之后 600 KB，
    # 上行慢一点 10 秒根本传不完——实测第一次读图就是这么超时的，
    # 而且超时发生在**写**阶段：图没传完，厂商那边什么都没收到，钱也没花。
    # 所以读图走 chat_vision 时把这个值放大，不要动 connect（地址错了还是要快失败）。
    to = httpx.Timeout(connect=6.0, read=float(cfg.get("timeout_s") or 60),
                       write=float(write_timeout), pool=6.0)
    return httpx.Client(timeout=to, follow_redirects=False,
                        headers=_headers(plain_key))


def _plain_key(cfg):
    """取明文 Key。cfg 里可以直接带 api_key（界面上刚填还没存的那次测试）。"""
    if cfg.get("api_key"):
        return cfg["api_key"], None
    return crypto.decrypt(cfg.get("api_key_cipher"))


def _needs_key(cfg):
    """本地私有化服务多数不校验鉴权，不该因为没填 Key 就拒绝测试。"""
    u = _base(cfg.get("base_url")).lower()
    return not (u.startswith("http://127.0.0.1") or u.startswith("http://localhost")
                or u.startswith("http://[::1]"))


def list_models(cfg):
    """GET {base_url}/models。

    这个请求不消耗 token，是最便宜的连通性检查；顺带解决型号漂移——
    型号名由厂商现给，不靠我写死在代码里。
    """
    base = _base(cfg.get("base_url"))
    if not base:
        return Result("models", False, err_kind="http", message="没填 base_url")
    key, why = _plain_key(cfg)
    if not key and _needs_key(cfg):
        return Result("models", False, err_kind="no_key", message=why or "没填 API Key")

    t0 = time.time()
    try:
        with _client(cfg, key) as c:
            r = c.get(base + "/models")
    except Exception as exc:                                  # noqa: BLE001
        kind, msg = _classify_transport(exc)
        return Result("models", False, latency_ms=int((time.time() - t0) * 1000),
                      err_kind=kind, message=_scrub(msg + "：" + str(exc), key))
    ms = int((time.time() - t0) * 1000)

    if r.status_code // 100 != 2:
        kind, msg = _classify_http(r.status_code, r.text)
        return Result("models", False, http_status=r.status_code, latency_ms=ms,
                      err_kind=kind, message=_scrub(msg + " · 厂商原文：" + r.text[:400], key))
    try:
        data = r.json()
    except ValueError:
        return Result("models", False, http_status=r.status_code, latency_ms=ms,
                      err_kind="parse",
                      message=_scrub("返回的不是 JSON。前 200 字：" + r.text[:200], key))

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return Result("models", False, http_status=r.status_code, latency_ms=ms,
                      err_kind="parse",
                      message=_scrub("返回里没有 data 数组，这家可能不支持 /models。"
                                     "前 200 字：" + r.text[:200], key))
    ids = []
    for it in items:
        mid = it.get("id") if isinstance(it, dict) else None
        if mid:
            ids.append(str(mid))
    return Result("models", True, http_status=r.status_code, latency_ms=ms,
                  models=sorted(ids),
                  message="取到 %d 个型号" % len(ids))


def chat_ping(cfg):
    """POST {base_url}/chat/completions，发一句最短的话。

    **只在用户点「测试」时才发**，不自动跑。理由：这是真实计费调用，
    而且会把请求发到第三方。自动跑的连通性检查会在用户不知情时产生费用和出网记录。
    请求体里不含任何业务数据——只有一句固定的问候语，
    不拿真实报账单去试，避免把差旅数据发给一个还没验证过的地址。
    """
    base = _base(cfg.get("base_url"))
    model = (cfg.get("model_name") or "").strip()
    if not base:
        return Result("chat", False, err_kind="http", message="没填 base_url")
    if not model:
        return Result("chat", False, err_kind="model_not_found",
                      message="没填型号。火山方舟这类要填的是推理接入点 ID（ep- 开头）")
    key, why = _plain_key(cfg)
    if not key and _needs_key(cfg):
        return Result("chat", False, err_kind="no_key", message=why or "没填 API Key")

    body = {"model": model,
            "messages": [{"role": "user", "content": PING_PROMPT}],
            "max_tokens": 16,
            "temperature": 0,
            "stream": False}
    t0 = time.time()
    try:
        with _client(cfg, key) as c:
            r = c.post(base + "/chat/completions", json=body)
    except Exception as exc:                                  # noqa: BLE001
        kind, msg = _classify_transport(exc)
        return Result("chat", False, latency_ms=int((time.time() - t0) * 1000),
                      err_kind=kind, message=_scrub(msg + "：" + str(exc), key))
    ms = int((time.time() - t0) * 1000)

    if r.status_code // 100 != 2:
        kind, msg = _classify_http(r.status_code, r.text)
        return Result("chat", False, http_status=r.status_code, latency_ms=ms,
                      err_kind=kind, message=_scrub(msg + " · 厂商原文：" + r.text[:400], key))
    try:
        data = r.json()
    except ValueError:
        return Result("chat", False, http_status=r.status_code, latency_ms=ms,
                      err_kind="parse",
                      message=_scrub("返回的不是 JSON。前 200 字：" + r.text[:200], key))

    echo = str(data.get("model") or "") or None
    choices = data.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        msgobj = choices[0].get("message") or {}
        content = (msgobj.get("content") or "")
        if isinstance(content, list):      # 某些家回的是分段数组
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content).strip()

    if not content:
        # HTTP 200 但模型没说话。这是「只看状态码」会放过的那种失败。
        return Result("chat", False, http_status=r.status_code, latency_ms=ms,
                      model_echo=echo, err_kind="parse",
                      message=_scrub("HTTP 200 但回复是空的——"
                                     "多半是网关把错误包在 200 里了。原文前 300 字："
                                     + r.text[:300], key))

    note = "通了"
    if echo and model and echo != model:
        note = ("通了，但**厂商回的型号是 %s，和配置的 %s 不一致**——"
                "可能被网关降级了，拿它评估能力会偏" % (echo, model))
    return Result("chat", True, http_status=r.status_code, latency_ms=ms,
                  model_echo=echo,
                  reply_excerpt=_scrub(content[:EXCERPT_LIMIT], key),
                  message=note)


def chat_vision(cfg, system, user, image_path, max_tokens=4000):
    """把**一页图**发给多模态模型。供「制度表格读图」用。

    和 chat_json 的区别只有一个，但它是整个系统里最该被看见的一个：
    **这是唯一会把客户制度文件发到外部的动作**（OCR 是本机 tesseract 跑的）。
    所以调用方必须把「哪一页、发给谁、多少 token」落进 doc_page_vision，
    事后要能回答「哪一页的图离开过本系统」。

    不发 response_format：多模态端点对它的支持比文本端点差得多，
    400 之后再退一次会白烧一次图的钱（图是按 token 计费的，一页几千）。
    结构化靠提示词要 markdown 表，解析放在 app/vision.py 里做。

    返回 (Result, content, usage)。usage 是 {prompt_tokens, completion_tokens}——
    图的钱必须记下来，不记就没人知道重读一次要花多少。
    """
    base = _base(cfg.get("base_url"))
    model = (cfg.get("model_name") or "").strip()
    empty = {"prompt_tokens": None, "completion_tokens": None}
    if not base:
        return Result("vision", False, err_kind="http", message="没填 base_url"), "", empty
    if not model:
        return Result("vision", False, err_kind="model_not_found", message="没填型号"), "", empty
    key, why = _plain_key(cfg)
    if not key and _needs_key(cfg):
        return (Result("vision", False, err_kind="no_key",
                       message=why or "没填 API Key"), "", empty)
    try:
        raw = io.open(image_path, "rb").read()
    except OSError as e:
        return (Result("vision", False, err_kind="http",
                       message="读不到这张图：%s" % e), "", empty)
    b64 = base64.b64encode(raw).decode("ascii")

    body = {"model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}},
                    {"type": "text", "text": user}]}],
            "max_tokens": int(max_tokens),
            "temperature": float(cfg.get("temperature") or 0),
            "stream": False}
    t0 = time.time()
    # 写超时按图的大小给：600 KB 的 base64 在慢上行里要几十秒。
    # 下限 60 秒，再按每 100 KB 加 10 秒——宁可等，也不要在传了一半时放弃，
    # 那样既看不到结果，也说不清图到底传出去没有。
    wt = max(60.0, 60.0 + len(b64) / 100000.0 * 10.0)
    try:
        with _client(cfg, key, write_timeout=wt) as c:
            r = c.post(base + "/chat/completions", json=body)
    except Exception as exc:                                  # noqa: BLE001
        kind, msg = _classify_transport(exc)
        return (Result("vision", False, latency_ms=int((time.time() - t0) * 1000),
                       err_kind=kind,
                       message=_scrub("%s：%s（图 %d KB，写超时 %.0f 秒）"
                                      % (msg, exc, len(raw) // 1024, wt), key)),
                "", empty)
    ms = int((time.time() - t0) * 1000)
    if r.status_code // 100 != 2:
        kind, msg = _classify_http(r.status_code, r.text)
        return (Result("vision", False, http_status=r.status_code, latency_ms=ms,
                       err_kind=kind,
                       message=_scrub(msg + " · 厂商原文：" + r.text[:400], key)),
                "", empty)
    try:
        data = r.json()
    except ValueError:
        return (Result("vision", False, http_status=r.status_code, latency_ms=ms,
                       err_kind="parse",
                       message=_scrub("返回的不是 JSON。前 200 字：" + r.text[:200], key)),
                "", empty)
    usage = data.get("usage") or {}
    use = {"prompt_tokens": usage.get("prompt_tokens"),
           "completion_tokens": usage.get("completion_tokens")}
    echo = str(data.get("model") or "") or None
    choices = data.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        msgobj = choices[0].get("message") or {}
        content = msgobj.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content).strip()
    if not content:
        return (Result("vision", False, http_status=r.status_code, latency_ms=ms,
                       model_echo=echo, err_kind="parse",
                       message=_scrub("HTTP 200 但回复是空的——多半是网关把错误"
                                      "包在 200 里了。原文前 300 字：" + r.text[:300], key)),
                "", use)
    note = "已返回"
    if echo and model and echo != model:
        note += "；**厂商回的型号是 %s，与配置的 %s 不一致**，可能被网关降级" % (echo, model)
    return (Result("vision", True, http_status=r.status_code, latency_ms=ms,
                   model_echo=echo,
                   reply_excerpt=_scrub(content[:EXCERPT_LIMIT], key),
                   message=note), content, use)


# 传输层重试次数（含首次）。只用于 chat_json 这条业务链路，见函数内注释。
TRANSPORT_TRIES = 4

# 可以重试的异常：**全都发生在请求发出之前**，所以不消耗 token、不计费。
#   ConnectError    连不上（TLS 握手失败也归这一类，httpx 把 ssl 错误包成它）
#   ConnectTimeout  连接阶段超时（受 Timeout(connect=6.0) 约束，几秒就抛）
#   PoolTimeout     等连接池，根本没出网
# **刻意不含 ReadTimeout / WriteTimeout / ReadError / RemoteProtocolError**：
# 那些发生在请求发出之后，模型可能已经在生成（计费），而且 ReadTimeout 要
# 等满 timeout_s 才抛——重试一次就是再等一个 timeout_s。
RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def chat_json(cfg, system, user, max_tokens=1500):
    """通用对话，要求模型只回 JSON。供「制度抽取」用。

    与 chat_ping 的唯一区别，也是最要紧的区别：**它会把业务内容发出去**。
    所以两件事必须由调用方保证，本函数不替它做：
      1. 送进来的 user 内容**已经脱敏**。脱敏要按业务语义做，
         在这里统一抹会把该发的制度原文也抹掉。
      2. 实际发送的字数要记下来——哪一段原文离开了本系统、有没有被截断，
         事后必须查得到。本函数把 sent_chars 一并回报。

    返回 (Result, content)。content 是模型回复全文，不截断：
    抽取要拿它解析 JSON，截断了就解析不出来。
    Result.reply_excerpt 仍是截断过的摘要，供落库与展示。
    """
    base = _base(cfg.get("base_url"))
    model = (cfg.get("model_name") or "").strip()
    if not base:
        return Result("chat", False, err_kind="http", message="没填 base_url"), ""
    if not model:
        return Result("chat", False, err_kind="model_not_found", message="没填型号"), ""
    key, why = _plain_key(cfg)
    if not key and _needs_key(cfg):
        return Result("chat", False, err_kind="no_key", message=why or "没填 API Key"), ""

    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens),
            "temperature": float(cfg.get("temperature") or 0),
            "stream": False}
    # json_object 不是所有厂商都支持，支持的那些能显著降低「回了一段解释文字」的概率。
    # 不支持的会 400，所以失败后要退一次普通模式——但**只退一次**，
    # 并且在 message 里说明，免得「悄悄降级」变成看不见的行为差异。
    body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    r = None
    fallback_note = ""
    retry_note = ""

    # 实际试了几次。**不能拿 TRANSPORT_TRIES 当「试了几次」用**——
    # 收口重试范围之后，不可重试的失败只试 1 次，而文案照着常量写就是
    # 「连试 4 次都没通」，那是假话，还会让人以为网络烂到重试四次都不行。
    tried_box = [0]

    def _post(payload, c):
        """**只对「请求发出之前」的失败重试**：连不上、TLS 握手失败、连接池超时。

        实测依据（2026-10-21，本机→dashscope）：curl 裸连 6 次成 3 次；
        连发 25 次业务调用成 8 次，TCP 10ms 就连上了，卡在 TLS 握手。
        这类失败发生在请求发出之前，**不消耗 token、不花钱**，重试是纯赚。

        ── 2026-10-24 收口：原来这里 `except Exception` 全retry，而上面那条
        「不消耗 token」的理由**只对连接阶段成立**。请求发出之后的失败
        （ReadTimeout、已建立的连接被中止）模型很可能已经在生成了，
        **那是计费的**，重试等于重复付钱；更要命的是 ReadTimeout 要等满
        `timeout_s` 才抛，而「规则研判」那条配置的 timeout_s = 240 秒——
        4 次重试就是 16 分钟卡在一条 finding 上。
        实测 AI 审核一轮 25 条 pending，跑了 25 分钟没跑完，根因就在这里。

        所以重试集合收成显式三种，**其余一律不重试，照实返回失败**。
        宁可报一次失败让人看见，也不要把钱和时间悄悄翻四倍。

        HTTP 4xx/5xx 一律**不重试**——那是厂商给的真实回答（鉴权错、型号不存在、
        限流），重试只会把一个明确的错误拖成一串，还可能重复计费。
        连通性测试（chat_ping）也不走这里：测试要报的是真实状况，
        重试会把「时好时坏」粉饰成「好的」。
        """
        last = None
        for i in range(TRANSPORT_TRIES):
            tried_box[0] = i + 1      # 实际试到第几次，给失败文案用
            try:
                return c.post(base + "/chat/completions", json=payload), i
            except RETRYABLE as exc:
                # 请求还没发出去：重试是纯赚
                last = exc
                if i + 1 < TRANSPORT_TRIES:
                    time.sleep(0.4 * (i + 1))
            except Exception:                                 # noqa: BLE001
                # 请求已经发出去了（或无法判断）：**原样抛出，不重试**
                raise
        raise last

    try:
        with _client(cfg, key) as c:
            r, tried = _post(body, c)
            if tried:
                retry_note = "传输层重试 %d 次后才通" % tried
            if r.status_code == 400 and "response_format" in (r.text or ""):
                body.pop("response_format", None)
                fallback_note = "该厂商不支持 json_object，已退回普通模式重发一次"
                r, _ = _post(body, c)
    except Exception as exc:                                  # noqa: BLE001
        kind, msg = _classify_transport(exc)
        return (Result("chat", False, latency_ms=int((time.time() - t0) * 1000),
                       err_kind=kind,
                       message=_scrub(
                           ("连试 %d 次都没通。%s：%s" % (tried_box[0], msg, exc))
                           if tried_box[0] > 1 else
                           ("没通（这类失败发生在请求发出之后，**不重试**——"
                            "模型可能已经在生成，重试等于重复计费）。%s：%s"
                            % (msg, exc)), key)), "")
    ms = int((time.time() - t0) * 1000)

    if r.status_code // 100 != 2:
        kind, msg = _classify_http(r.status_code, r.text)
        return (Result("chat", False, http_status=r.status_code, latency_ms=ms,
                       err_kind=kind,
                       message=_scrub(msg + " · 厂商原文：" + r.text[:400], key)), "")
    try:
        data = r.json()
    except ValueError:
        return (Result("chat", False, http_status=r.status_code, latency_ms=ms,
                       err_kind="parse",
                       message=_scrub("返回的不是 JSON。前 200 字：" + r.text[:200], key)), "")

    echo = str(data.get("model") or "") or None
    choices = data.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        msgobj = choices[0].get("message") or {}
        content = msgobj.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content).strip()

    if not content:
        return (Result("chat", False, http_status=r.status_code, latency_ms=ms,
                       model_echo=echo, err_kind="parse",
                       message=_scrub("HTTP 200 但回复是空的——多半是网关把错误"
                                      "包在 200 里了。原文前 300 字：" + r.text[:300], key)), "")

    note = fallback_note or "已返回"
    if retry_note:
        note = retry_note + ("；" + note if note else "")
    if echo and model and echo != model:
        note += "；**厂商回的型号是 %s，与配置的 %s 不一致**，可能被网关降级" % (echo, model)
    usage = data.get("usage") or {}
    return (Result("chat", True, http_status=r.status_code, latency_ms=ms,
                   model_echo=echo,
                   reply_excerpt=_scrub(content[:EXCERPT_LIMIT], key),
                   message=note,
                   prompt_tokens=usage.get("prompt_tokens"),
                   completion_tokens=usage.get("completion_tokens")), content)
