# -*- coding: utf-8 -*-
"""传输层重试的范围。**全程不出网**：只检查哪些异常在重试集合里。

钉一条纪律：

    **只重试「请求发出之前」的失败。**

原来 `_post` 是 `except Exception` 全重试，而它给出的理由是
「这类失败发生在请求发出之前，不消耗 token、不花钱，重试是纯赚」——
那句话**只对连接阶段成立**。请求发出之后的失败（ReadTimeout、
已建立的连接被中止）模型很可能已经在生成了，那是计费的，重试等于重复付钱。

更要命的是时间：ReadTimeout 要等满 `timeout_s` 才抛，而「规则研判」那条
配置的 timeout_s = 240 秒，TRANSPORT_TRIES = 4 —— 一条 finding 最坏卡 16 分钟。
实测 2026-10-24 点一次「智能审核」跑了 25 分钟没返回，根因就在这里。

**这条闸失效不会报错**，只会让一次点击悄悄变慢四倍、账单悄悄变大四倍，
所以逐个异常类型钉死，不是只测一个代表。
"""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import client as llmc  # noqa: E402


# 请求还没发出去 → 不计费 → 重试是纯赚
@pytest.mark.parametrize("exc_cls", [
    httpx.ConnectError,      # 连不上；TLS 握手失败也被 httpx 包成它
    httpx.ConnectTimeout,    # 连接阶段超时，受 connect=6.0 约束，几秒就抛
    httpx.PoolTimeout,       # 等连接池，根本没出网
])
def test_发出之前的失败要重试(exc_cls):
    assert issubclass(exc_cls, llmc.RETRYABLE), \
        "%s 发生在请求发出之前，不重试等于白白放弃一次免费的机会" % exc_cls.__name__


# 请求已经发出去了 → 可能已计费，且要等满 timeout → 一律不重试
@pytest.mark.parametrize("exc_cls", [
    httpx.ReadTimeout,          # 等模型回话超时：**模型很可能已经在生成，计费的**
    httpx.WriteTimeout,         # 发请求体时超时：可能已经发出去一部分
    httpx.ReadError,            # 读的时候连接断了（WinError 10053 这类）
    httpx.WriteError,
    httpx.RemoteProtocolError,  # 对端把连接掐了
])
def test_发出之后的失败不许重试(exc_cls):
    assert not issubclass(exc_cls, llmc.RETRYABLE), \
        ("%s 发生在请求发出之后，模型可能已经在生成（计费）；"
         "而且这类超时要等满 timeout_s 才抛，重试一次就是再等一个 timeout_s"
         % exc_cls.__name__)


def test_超时家族没被整个放进重试集合():
    """ConnectTimeout 和 ReadTimeout 都是 TimeoutException 的子类。
    **按父类判就会把 ReadTimeout 一起放进来**——那正是要防的那个 bug。"""
    assert issubclass(httpx.ConnectTimeout, httpx.TimeoutException)
    assert issubclass(httpx.ReadTimeout, httpx.TimeoutException)
    assert httpx.TimeoutException not in llmc.RETRYABLE, \
        "把 TimeoutException 整个放进来，等于把 ReadTimeout 也放进来了"


def test_重试次数没被悄悄调大():
    """4 次是针对「几秒就抛」的连接失败定的（退避 0.4/0.8/1.2 秒）。
    这个数只在重试集合全是快失败时才合理，所以和上面几条绑在一起看。"""
    assert llmc.TRANSPORT_TRIES == 4


def test_重试集合是元组且不为空():
    """写成单个类（不是元组）时 `except RETRYABLE` 仍然能跑，
    但往里加第二项时会静默变成语法上合法、语义上错的东西。"""
    assert isinstance(llmc.RETRYABLE, tuple)
    assert len(llmc.RETRYABLE) >= 3
    assert all(isinstance(x, type) and issubclass(x, Exception)
               for x in llmc.RETRYABLE)


# ── 失败归因 ─────────────────────────────────────────────────────────────────
# 归错类不会报错，只会让人去查错的东西。实测 rule_judge 的失败
# （"Server disconnected without sending a response."）全被报成
# 「连不上这个地址，端口/路径可能不对，或服务没起（私有化部署常见）」——
# 而那是公有云 dashscope，地址一个字都没错。
# 根因：`disconnected` 里含 `connect`，被 `"connect" in s` 那条先捞走了。

@pytest.mark.parametrize("text", [
    "Server disconnected without sending a response.",
    "[WinError 10053] 你的主机中的软件中止了一个已建立的连接。",
    "[WinError 10054] Connection reset by peer",
    "The connection was aborted",
])
def test_中途断开不许报成连不上(text):
    kind, msg = llmc._classify_transport(Exception(text))
    assert kind == "http"
    assert "中途被断开" in msg, "「断开」被归成了别的类：" + msg
    assert "端口/路径可能不对" not in msg, \
        "连接已建立过，不是地址配错——这么说会让人去改一个没错的配置"


def test_真的连不上还是要报连不上():
    """收口不能误伤：真正的 connection refused 仍然要给地址排查的方向。"""
    kind, msg = llmc._classify_transport(Exception("Connection refused"))
    assert kind == "http"
    assert "端口/路径可能不对" in msg


def test_其余几类归因没被带歪():
    import httpx as _h
    assert llmc._classify_transport(_h.ConnectTimeout("x"))[0] == "timeout"
    assert llmc._classify_transport(Exception("SSL: CERTIFICATE_VERIFY_FAILED"))[0] == "tls"
    assert llmc._classify_transport(Exception("getaddrinfo failed"))[0] == "dns"
