# -*- coding: utf-8 -*-
"""统一口径的编排器 + 两条链共用的公共编排机。

公共编排机（app/agent_job.py）是这次抽出来的：抽取链和本体链各留一份
进度逻辑，迟早会出现「两个页面上跑到一半失败长得不一样」。
所以这里的测试要同时压住**两条链**——任一条跑偏都要红。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_job  # noqa: E402
from app import extract_orchestrator as ex  # noqa: E402
from app import onto_orchestrator as oo  # noqa: E402

CHAINS = (("规则抽取", ex), ("统一口径", oo))


def _job(mod, statuses, timings=None):
    job = {"agents": [], "_pct_seen": 0}
    for meta, st in zip(mod.AGENTS, statuses):
        a = mod._blank_agent(meta)
        a["status"] = st
        a["timings"] = dict(timings or {}) if st == "running" else (
            {c: 0.1 for c, _, _ in agent_job.PHASES} if st in ("done", "skipped") else {})
        job["agents"].append(a)
    return job


# ─────────────────────────────────────────────────────────────────────────────
# 两条链都要满足的进度语义
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,mod", CHAINS)
def test_进度单调不回退(name, mod):
    job = _job(mod, ["done", "running", "waiting"], {"S": 0.1, "D": 0.1})
    p1 = mod._pct(job)
    job["agents"][1]["status"] = "waiting"
    job["agents"][1]["timings"] = {}
    p2 = mod._pct(job)
    assert p2 >= p1, "%s 的进度回退了：%d → %d" % (name, p1, p2)


@pytest.mark.parametrize("name,mod", CHAINS)
def test_没跑过也能给出编排图(name, mod):
    s = mod.snapshot()
    assert len(s["agents"]) == len(mod.AGENTS)
    assert s["pct"] >= 0
    if s["status"] == "idle":
        assert all(a["status"] == "waiting" for a in s["agents"])
        assert s["note"], "空态必须有话说，演示现场最怕白屏"


@pytest.mark.parametrize("name,mod", CHAINS)
def test_全部完成才是100(name, mod):
    assert mod._pct(_job(mod, ["done"] * len(mod.AGENTS))) == 100


@pytest.mark.parametrize("name,mod", CHAINS)
def test_失败之后的标未执行(name, mod):
    st = ["done"] + ["failed"] + ["skipped"] * (len(mod.AGENTS) - 2)
    job = _job(mod, st)
    assert mod._pct(job) < 100


@pytest.mark.parametrize("name,mod", CHAINS)
def test_智能体名字不许用name字段(name, mod):
    """脱敏按**字段名**选策略：叫 name 的一律走人名脱敏，页面上会显示「文******」。"""
    a = mod._blank_agent(mod.AGENTS[0])
    assert "agent_name" in a and "name" not in a
    assert a["agent_name"]


@pytest.mark.parametrize("name,mod", CHAINS)
def test_阶段名也不许用name(name, mod):
    for ph in mod.meta()["phases"]:
        assert "phase_name" in ph and "name" not in ph, ph


@pytest.mark.parametrize("name,mod", CHAINS)
def test_脱敏后智能体名字还认得出来(name, mod):
    from app import mask
    out = mask.scrub(mod.snapshot())
    names = [a["agent_name"] for a in out["agents"]]
    assert all("*" not in n for n in names), names


@pytest.mark.parametrize("name,mod", CHAINS)
def test_编排图与yaml契约一致(name, mod):
    from app.runtime import yaml_loader
    r = yaml_loader.load(verbose=False)
    for a in mod.AGENTS:
        assert a["id"] in r.agents, "%s 在 %s 的编排图里但没有 YAML 契约" % (a["id"], name)
        assert a["id"] in mod.RUNNERS, "%s 没有执行体，点了开始那一格永远转圈" % a["id"]


@pytest.mark.parametrize("name,mod", CHAINS)
def test_两条链各自有独立基线文件(name, mod):
    """共用一个基线文件会让两条链的权重互相污染——
    抽取链 400 秒、本体链 13 秒，混在一起进度条会彻底失准。"""
    assert mod._JOB.baseline_path.endswith(".json")
    other = [m for n, m in CHAINS if m is not mod][0]
    assert mod._JOB.baseline_path != other._JOB.baseline_path


# ─────────────────────────────────────────────────────────────────────────────
# 统一口径这条链自己的事
# ─────────────────────────────────────────────────────────────────────────────
def test_统一口径的三个agent是谁():
    ids = [a["id"] for a in oo.AGENTS]
    assert ids == ["fj_l0_03_onto_perception", "fj_l2_01_extract_coord",
                   "fj_l2_02_onto_align"]


def test_统一口径全是确定性不计费():
    """这条链一步都不调模型。**口径对不对是能复核的硬判断**——
    让模型来判，只会给出一条「看起来有据可查」的证据链。"""
    assert all(a["kind"] == "确定性" for a in oo.AGENTS)


def test_归并agent同时挂在两条链上但抽取链不跑它():
    """fj_l2_01 的 YAML 在抽取链里也订阅得到，但抽取链的作业里**没有它**：
    统一口径是本体要做的事，规则抽取只负责抽取。"""
    assert "fj_l2_01_extract_coord" in [a["id"] for a in oo.AGENTS]
    assert "fj_l2_01_extract_coord" not in [a["id"] for a in ex.AGENTS]
    assert len(ex.AGENTS) == 4        # 加了「规则明细成形」之后是 4 个


def test_信号通道首尾相接():
    """上游 emits 必须是下游 listens，否则图上画着一条不存在的链。"""
    for prev, nxt in zip(oo.AGENTS, oo.AGENTS[1:]):
        assert prev["emits"] == nxt["listens"], \
            "%s 发 %s，而 %s 听 %s" % (prev["id"], prev["emits"], nxt["id"], nxt["listens"])
