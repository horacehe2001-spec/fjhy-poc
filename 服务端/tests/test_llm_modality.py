# -*- coding: utf-8 -*-
"""读图能力校验:前后端同一套判断,两个方向都测。

为什么后端也要有这条测试:只在前端校验,绕过界面直接调接口就没人管了。

为什么两个方向都测（CLAUDE.md 第 9 条）:
  漏报 → 纯文本模型配成读图,拿到图不报错,会按文字线索**编**一段看起来
         合理的内容,下游当成已核验的票面信息。
  误报 → 把自建网关或预置里没有的多模态型号判成「不行」,会把对的用法挡掉;
         更糟的是把「判不出」说成「有问题」——那和说成「没问题」一样错。

实测背景（2026-10-19）:库里躺着一个 vision_read 配置,预置选的是
aliyun_bailian_vision（标注 vision、建议 qwen-vl-max）,而 model_name 填的是
qwen3.5-122b-a10b——和两个文本用途的配置一模一样。
原来的检查只看预置,所以**一次警告都没报过**。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.api.sys_llm import _looks_vision, modality_warning  # noqa: E402

TEXT_PRESET = "aliyun_bailian_text"
VISION_PRESET = "aliyun_bailian_vision"


def _text_model_in_db():
    """库里某个被配成文本用途的型号名。没有就跳过——不造数据。"""
    r = db.query_one("""SELECT model_name FROM llm_config
                         WHERE purpose IN ('rule_judge','extract') LIMIT 1""")
    return r["model_name"] if r else None


# ─────────────────────────────────────────────────────────────────────────────
# 漏报方向
# ─────────────────────────────────────────────────────────────────────────────
def test_纯文本预置配读图是硬结论():
    w = modality_warning("vision_read", TEXT_PRESET, "qwen-vl-max")
    assert "不能用于" in w
    assert "编" in w, "文案没写明后果：模型会编一段看起来合理的内容"


def test_没选预置时不许静默放过():
    w = modality_warning("vision_read", None, "whatever")
    assert w and "判不出" in w


def test_型号同时被配成文本用途要指名道姓():
    """**这是个事实判断,不是猜。** 同一个型号既当文本用又当读图用。"""
    m = _text_model_in_db()
    if not m:
        pytest.skip("库里没有文本用途的配置")
    w = modality_warning("vision_read", VISION_PRESET, m)
    assert w, "预置选对了但型号是文本模型，这条没报出来"
    assert m in w, "警告里没写是哪个型号：%s" % w
    assert "编" in w and "票面信息" in w, "后果没写清"


def test_型号名没有多模态标记要提醒():
    w = modality_warning("vision_read", VISION_PRESET, "my-gateway-model-7b")
    assert w and "判不出" in w
    # **不许把「判不出」说成「不行」**
    assert "不能用于" not in w


# ─────────────────────────────────────────────────────────────────────────────
# 误报方向
# ─────────────────────────────────────────────────────────────────────────────
def test_正经读图型号不许误伤():
    for m in ("qwen-vl-max", "qwen2.5-vl-72b-instruct", "glm-4v-plus", "gpt-4o"):
        assert modality_warning("vision_read", VISION_PRESET, m) == "", \
            "%s 被误判了" % m


def test_其它用途完全不受影响():
    for p in ("rule_judge", "extract", None, ""):
        assert modality_warning(p, TEXT_PRESET, "qwen3.5-122b-a10b") == ""


def test_没给型号名时按旧行为走():
    """型号名是后加的参数。不传就只做预置级判断，不许因此多报或少报。"""
    assert modality_warning("vision_read", VISION_PRESET) == ""
    assert "不能用于" in modality_warning("vision_read", TEXT_PRESET)


def test_自己这条配置不和自己比():
    """同一个 config_id 不该因为「它自己也在库里」而报警。"""
    r = db.query_one("""SELECT config_id, model_name FROM llm_config
                         WHERE purpose='vision_read' LIMIT 1""")
    if not r:
        pytest.skip("库里没有读图配置")
    w = modality_warning("vision_read", VISION_PRESET, r["model_name"], r["config_id"])
    # 它自己被排除后，剩下的判断才是真的（可能仍有警告，但不能是「和自己重名」）
    if w:
        assert r["config_id"] not in w, "警告里把它自己算成了另一个配置"


def test_标记只用来判有不用来判没有():
    assert _looks_vision("qwen-vl-max")
    assert _looks_vision("gpt-4o")
    assert _looks_vision("GLM-4V-Plus")      # 大小写不敏感
    assert not _looks_vision("qwen3.5-122b-a10b")
    assert not _looks_vision(None)


# ─────────────────────────────────────────────────────────────────────────────
# 清单接口也要带警告
# ─────────────────────────────────────────────────────────────────────────────
def test_存量配置在清单里也要报():
    """**原来只在保存那一刻算一次**，于是已经存进库的错配置永远不报。

    实测就有一个 vision_read 配着文本型号躺在库里一天。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    d = c.get("/api/sys/llm-configs").json()
    assert d["items"], "库里没有配置"
    for r in d["items"]:
        assert "modality_warning" in r, "清单里没有这一列，存量错配置就永远不报"
        if r["purpose"] != "vision_read":
            assert not r["modality_warning"]
