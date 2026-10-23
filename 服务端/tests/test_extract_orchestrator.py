# -*- coding: utf-8 -*-
"""抽取编排器：进度条的三条硬规则 + 一条脱敏误报回归。

这几条看着琐碎，但每一条都对应一种**界面会撒谎**的方式：
  进度回退      → 人会以为任务重来了
  按个数算进度  → 耗时最长那一步会让条卡住六分钟，比没有进度条更像死机
  失败被跳过    → 得到一份「看起来跑完了」而中间是空的结果
  名字被脱敏    → 页面显示「文******」，看着像系统坏了
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import extract_orchestrator as orch  # noqa: E402


def _job(statuses, timings=None):
    """造一个假作业快照，只为了算进度，不碰库。"""
    job = {"agents": [], "_pct_seen": 0}
    for meta, st in zip(orch.AGENTS, statuses):
        a = orch._blank_agent(meta)
        a["status"] = st
        a["timings"] = dict(timings or {}) if st == "running" else (
            {c: 0.1 for c, _, _ in orch.PHASES} if st in ("done", "skipped") else {})
        job["agents"].append(a)
    return job


# ─────────────────────────────────────────────────────────────────────────────
# 进度条
# ─────────────────────────────────────────────────────────────────────────────
def test_进度单调不回退():
    """回退会让人以为任务重来了。只取历史最大值。"""
    job = _job(["done", "running", "waiting"], {"S": 0.1, "D": 0.1})
    p1 = orch._pct(job)
    # 模拟一次「上游状态刷新慢」导致的倒退：第二个 Agent 退回 waiting
    job["agents"][1]["status"] = "waiting"
    job["agents"][1]["timings"] = {}
    p2 = orch._pct(job)
    assert p2 >= p1, "进度回退了：%d → %d" % (p1, p2)


def test_没跑过也能给出编排图():
    """演示现场最怕白屏。没有作业时也要能画出智能体和总线。"""
    s = orch.snapshot()
    assert s["status"] == "idle"
    assert len(s["agents"]) == len(orch.AGENTS)
    assert all(a["status"] == "waiting" for a in s["agents"])
    assert s["pct"] == 0


def test_全部完成才是100():
    job = _job(["done"] * len(orch.AGENTS))
    assert orch._pct(job) == 100


def test_失败之后的智能体标未执行而不是跳过不提():
    """失败被静默跳过，会得到一份「看起来跑完了」而中间是空的结果。"""
    job = _job(["done", "failed", "skipped"])
    assert orch._pct(job) < 100
    assert job["agents"][2]["status"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# 脱敏误报回归
# ─────────────────────────────────────────────────────────────────────────────
def test_智能体名字不许用name字段():
    """脱敏中间件按**字段名**选策略：叫 `name` 的一律走人名脱敏。

    那条策略本身是对的——本系统里 `name` 绝大多数真是人名。
    但智能体名字不是人名，复用这个键的后果是页面上显示「文******」「条***」。
    这条测试钉住字段名，免得哪天有人「顺手」改回 name。
    """
    a = orch._blank_agent(orch.AGENTS[0])
    assert "agent_name" in a, "智能体名字字段被改回 name 了，页面上会被脱敏成「文******」"
    assert "name" not in a
    assert a["agent_name"]


def test_编排元数据里的阶段名也不许用name():
    m = orch.meta()
    for ph in m["phases"]:
        assert "phase_name" in ph and "name" not in ph, ph


def test_脱敏后智能体名字还认得出来():
    """正向验一遍：过一次 scrub，名字必须原样。"""
    from app import mask
    out = mask.scrub(orch.snapshot())
    names = [a["agent_name"] for a in out["agents"]]
    assert "文件库变更感知" in names, names
    assert all("*" not in n for n in names), names


# ─────────────────────────────────────────────────────────────────────────────
# 编排图与 YAML 契约一致
# ─────────────────────────────────────────────────────────────────────────────
def test_编排图里的agent_id都有对应的yaml配置():
    """界面画的编排图和启动时校验的契约必须是同一套 Agent。
    对不上的话，页面上画着的东西运行时根本不存在。"""
    from app.runtime import yaml_loader
    r = yaml_loader.load(verbose=False)
    for a in orch.AGENTS:
        assert a["id"] in r.agents, "%s 在编排图里但没有 YAML 契约" % a["id"]


def test_每个agent都有执行体():
    """有配置没实现 = 点了开始抽取之后那一格永远转圈。"""
    for a in orch.AGENTS:
        assert a["id"] in orch.RUNNERS, "%s 没有执行体" % a["id"]


def test_统一口径的那个agent不在抽取链里():
    """多源归并与冲突裁决是「统一口径」，属于规则本体那一栏，不属于抽取。"""
    ids = [a["id"] for a in orch.AGENTS]
    assert "fj_l2_01_extract_coord" not in ids
    # 4 = 感知 / 条款切分 / 规则明细成形 / 规则草稿
    # （2026-10-19 加了「规则明细成形」，这一栏的产物从条款变成规则明细）
    assert len(ids) == 4
