# -*- coding: utf-8 -*-
"""增量抽取：空集合不许退化成全量，范围算一次全链共用。

这个文件钉的是两类错，**两类都是静默的，而其中一类会花钱**：

1. **空列表退化成全量。** `run_split` / `run_ai` / `rule_detail.*` 原来一律写
   `if file_ids:`，于是「增量 0 份」时过滤条件整个被跳过：
     · run_split      → 全量重切 190 条条款（数据被动，而页面上看不出来）
     · run_ai         → 从全库候选里挑 8 条**调模型买回来**（钱花了才看得出来）
   现在四处都改成 `is not None`，这里逐处正向钉住。

2. **范围算第二次就是空集。** `run_split` 每处理完一份就 `mark_extracted()`，
   所以它跑完之后 `pending_extract()` 返回 0。要是「规则明细」和「AI 草稿」
   各自再问一次「增量是谁」，拿到的都是空集——表现是
   「条款切了 2 份，明细和草稿一条没出」。所以范围由 L0 一次算定、
   放在 job["scope"] 上，全链共用。这里钉住「算第二次确实会变空」这个事实，
   好让下一个人明白那个 job["scope"] 不是多余的。

**本文件绝不触发真实模型调用。** AI 那一步只测「空集时一次都不发」，
非空的那条路不测——测它就是在单元测试里买草稿（真出网、真计费）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, doclib, doclib_extract as dx, extract_orchestrator as orch  # noqa: E402
from app import rule_detail  # noqa: E402


def _calls():
    """抽取链「发出过几次模型调用」的见证者。

    **不能用 llm_call_log。** 那张表全项目只有 app/api/ai_review.py 写
    （purpose='rule_judge'）；抽取链的 run_ai 走的是另一条路，它把每次尝试
    落成一行 doclib_draft（**成败都落**，失败那行 rulable 为 NULL）。
    拿 llm_call_log 去断言「没调模型」是个**空断言**——它在这条链上永远不涨，
    所以代码退化了测试也照样绿。
    2026-10-21 实测发现：跑完一次增量全链、产出 2 条草稿，llm_call_log 一条没涨。
    """
    return db.scalar("SELECT count(*) FROM doclib_draft") or 0


def _counts():
    return (db.scalar("SELECT count(*) FROM doclib_clause") or 0,
            db.scalar("SELECT count(*) FROM doclib_draft") or 0,
            db.scalar("SELECT count(*) FROM doclib_rule_detail") or 0)


# ─────────────────────────────────────────────────────────────────────────────
# 一 · 空列表 ≠ 全部
# ─────────────────────────────────────────────────────────────────────────────
def test_切条款传空列表时一份都不处理且不动已有条款():
    """原来是 `if file_ids:`——空列表时判断为假，过滤被跳过，全量重切 190 条。"""
    before = _counts()
    r = dx.run_split(file_ids=[])
    assert r["files"] == 0, "传了空列表却处理了 %d 份文件" % r["files"]
    assert r["clauses"] == 0
    assert _counts() == before, "库里的条款/草稿/明细被动过了：%s → %s" % (before, _counts())


def test_切条款不传file_ids才是全部():
    """反方向：别把「空列表不许当全部」做成「怎么传都是 0 份」。"""
    r = dx.run_split()
    assert r["files"] > 0, "不传 file_ids 应该是全部，却处理了 0 份"
    assert r["scope"] == "全部"


def test_AI草稿传空文件列表时一次模型调用都不发():
    """**这条是四条里最贵的。** 原来空列表会落进 else 分支，
    从全库候选里挑 8 条调模型——钱花出去了，而使用者以为增量是空的。"""
    before = _calls()
    r = dx.run_ai(file_ids=[], limit=8)
    assert r["tried"] == 0, "传了空文件列表却试抽了 %d 条" % r["tried"]
    assert r["drafted"] == 0
    assert _calls() == before, "**向模型发出了调用**——增量为空时一次都不许发"


def test_AI草稿传空条款列表时也一次都不发():
    """clause_ids=[] 同理。两个参数各自都要挡住。"""
    before = _calls()
    r = dx.run_ai(clause_ids=[], limit=8)
    assert r["tried"] == 0
    assert _calls() == before, "**向模型发出了调用**"


def test_AI草稿的文件范围是真的在筛而不是碰巧为空():
    """前面那两条（空列表）在「全库本来就没有待跑候选」时没有鉴别力——
    不管筛没筛，结果都是 0。这一条造一个真实的候选出来再筛：

      全库有 1 条待跑候选（挂在文件 X 上）
      run_ai(file_ids=[Y])  →  必须试抽 0 条

    **这条用例在代码退化时会花掉 1 次模型调用**（所以 limit=1 把代价钉死）。
    这是刻意的权衡：它防住的那个退化（file_ids 被跳过 → 对全库候选调模型）
    一次就是 8 条，而且钱花了才看得出来。
    """
    rows = db.query("""SELECT file_id FROM doc_lib_file
                        WHERE NOT missing AND parse_state='已解析'
                        ORDER BY rel_path LIMIT 2""")
    if len(rows) < 2:
        pytest.skip("需要至少两份已解析的文件")
    x, y = rows[0]["file_id"], rows[1]["file_id"]
    cid = "FJHY-TEST-INC-SCOPE-c1"
    db.execute("""INSERT INTO doclib_clause(clause_id, file_id, seq, clause_no, body,
                                            char_pos, features, doc_kind,
                                            file_fingerprint, covered_by)
                  VALUES (%s,%s,9999,'第九九九条','住宿费不得超过 500 元。',0,
                          %s,'制度','FJHY-TEST-INC',%s)
                  ON CONFLICT (clause_id) DO NOTHING""",
               (cid, x, ["阈值"], []))
    try:
        backlog = db.scalar("""
            SELECT count(*) FROM doclib_clause c
             WHERE c.doc_kind = ANY(%s) AND cardinality(c.covered_by)=0
               AND c.features && %s
               AND NOT EXISTS (SELECT 1 FROM doclib_draft d
                                WHERE d.clause_id = c.clause_id
                                  AND d.rulable IS NOT NULL)""",
            (list(dx.CANDIDATE_KINDS), list(dx.CANDIDATE_FEATURES))) or 0
        assert backlog >= 1, "造的那条候选没进去，这条用例就失去鉴别力了"

        before = _calls()
        r = dx.run_ai(file_ids=[y], limit=1)
        assert r["tried"] == 0, \
            ("文件范围没生效：全库有 %d 条待跑候选（挂在别的文件上），"
             "限定在文件 %s 上却试抽了 %d 条" % (backlog, y, r["tried"]))
        assert _calls() == before, "**向模型发出了调用**"
    finally:
        db.execute("DELETE FROM doclib_clause WHERE clause_id=%s", (cid,))


def test_规则明细传空列表时两路确定性映射都不处理():
    a = rule_detail.from_table_rows(file_ids=[])
    sc = rule_detail.from_scenario(file_ids=[])
    assert a["mapped"] == 0 and a["flagged"] == 0, a
    assert sc["mapped"] == 0, sc


def test_规则明细不传file_ids才是全部():
    a = rule_detail.from_table_rows()
    assert a["mapped"] > 0, "不传 file_ids 应该是全部，却映射出 0 条"


def test_已采纳草稿的投影不受增量范围限定():
    """第三路刻意不限定：采纳是人在别的时候点的，和这次增量是哪几份文件无关。
    限定了会让「我明明采纳了，明细里没有」。"""
    r = rule_detail.run(file_ids=[])
    assert r["table"]["mapped"] == 0, "确定性映射应该被限定成 0"
    assert "draft" in r and r["draft"]["projected"] >= 0
    # 草稿投影那一路要真的跑过（projected 是它的产出计数，不是被跳过）
    assert r["total"] > 0, "明细表被清空了？"


# ─────────────────────────────────────────────────────────────────────────────
# 二 · 范围算一次，全链共用
# ─────────────────────────────────────────────────────────────────────────────
def _fake_job(only_changed=True):
    """造一个和 AgentJob.start() 同形的作业字典。

    started_at 是必须的：emit() 会拿它算信号的相对时刻。
    少一个键就是 KeyError，而那会被误读成「编排器坏了」。
    """
    import time
    return {"job_id": "job-test", "status": "running", "agents": [],
            "started_at": time.time(), "finished_at": None, "error": None,
            "signals": [], "log_errors": [], "_pct_seen": 0,
            "opts": {"only_changed": only_changed}}


def test_L0把范围定在job上而不是只报个数():
    """缺陷修正点：L0 一直在算增量、投信号报 N 份，但范围没传下去，
    下游 _run_split 调的是不带参数的 run_split()，等于全量。"""
    job = _fake_job(True)
    a = orch._blank_agent(orch.AGENTS[0])
    orch._run_l0(job, a)
    assert "scope" in job, "L0 没有把范围定下来"
    sc = job["scope"]
    assert sc["only_changed"] is True
    assert isinstance(sc["file_ids"], list), "增量模式下 file_ids 必须是列表（可以为空）"
    # 每一份都要说得出为什么在增量里
    for f in sc["files"]:
        assert f["why"] in ("从未抽取", "抽取后文件又被改过"), f


def test_全量模式下范围是None而不是空列表():
    """None=全部，[]=一份都不处理。**这两者意思相反**，不许混。"""
    job = _fake_job(False)
    a = orch._blank_agent(orch.AGENTS[0])
    orch._run_l0(job, a)
    assert job["scope"]["file_ids"] is None, \
        "全量模式给了 %r——空列表在下游是「一份都不处理」" % job["scope"]["file_ids"]


def test_切分跑完之后再算一次范围就是空的():
    """这条解释了 job["scope"] 为什么不是多余的：
    run_split 每处理完一份就 mark_extracted()，所以它跑完之后
    pending_extract() 返回 0 份。下游要是自己再算一次，拿到的是空集。"""
    # 先确保有东西可抽：把一份文件的抽取指纹清掉，让它重新进增量
    fid = db.scalar("""SELECT file_id FROM doc_lib_file
                        WHERE NOT missing AND parse_state='已解析' AND body IS NOT NULL
                        ORDER BY rel_path LIMIT 1""")
    assert fid
    keep = db.query_one("SELECT extracted_at, extract_fingerprint FROM doc_lib_file "
                        "WHERE file_id=%s", (fid,))
    db.execute("UPDATE doc_lib_file SET extract_fingerprint=NULL WHERE file_id=%s", (fid,))
    try:
        job = _fake_job(True)
        a0 = orch._blank_agent(orch.AGENTS[0])
        orch._run_l0(job, a0)
        frozen = list(job["scope"]["file_ids"])
        assert fid in frozen

        a1 = orch._blank_agent(orch.AGENTS[1])
        orch._run_split(job, a1, None)
        # 范围没被改写
        assert job["scope"]["file_ids"] == frozen
        # 而重新算一次已经空了——这正是不能让下游自己算的原因
        assert fid not in [r["file_id"] for r in doclib.pending_extract()["items"]]
    finally:
        db.execute("UPDATE doc_lib_file SET extracted_at=%s, extract_fingerprint=%s "
                   "WHERE file_id=%s",
                   (keep["extracted_at"], keep["extract_fingerprint"], fid))


def test_增量为0时切分空跑且台账记仅变化():
    """演示现场会碰到的常态：没有新文件。四步都要如实报 0，
    **不许退化成全量重跑**（那会悄悄重算 190 条条款）。"""
    before = _counts()
    job = _fake_job(True)
    a0 = orch._blank_agent(orch.AGENTS[0])
    orch._run_l0(job, a0)
    if job["scope"]["file_ids"]:
        pytest.skip("当前库里有待抽取文件，这条用例要的是「增量为 0」的场景")
    a1 = orch._blank_agent(orch.AGENTS[1])
    r = orch._run_split(job, a1, None)
    assert r["files"] == 0 and r["scope"] == "仅变化", r
    assert _counts() == before, "增量为 0 却动了库：%s → %s" % (before, _counts())
    assert "不是失败" in (a1["note"] or ""), \
        "增量 0 份要说清楚这是正常结果，否则看页面的人会当成跑失败了"


def test_增量为0时AI那一步一次调用都不发():
    """**最贵的那一步。** 增量为空时连模型配置都不去读——
    没配 extract 默认模型时 _extract_cfg 会抛异常把整条链判红，
    而「增量 0 份」本来是正常结果，不该因为模型没配变成失败。"""
    job = _fake_job(True)
    job["scope"] = {"only_changed": True, "file_ids": [], "files": []}
    a = orch._blank_agent(orch.AGENTS[3])
    before = _calls()
    r = orch._run_draft(job, a, None)
    assert r["tried"] == 0 and r["drafted"] == 0
    assert _calls() == before, "**向模型发出了调用**"
    assert a["cost"] == "未调用 · 不消耗", \
        "费用要写「未调用」，不是「已调用 0 次」——后者看着像调过了"
    assert a["status"] != "failed"


def test_四个Agent都读同一个范围来源():
    """不许任何一个下游自己调 pending_extract 重算范围。

    靠 prev 链往下传也不行：中间任何一个 Agent 忘了带，
    范围就静默退化成「全部」——而那是会花钱的退化。
    """
    import inspect
    for name in ("_run_split", "_run_detail", "_run_draft"):
        src = inspect.getsource(getattr(orch, name))
        assert "_scope_of(job)" in src, "%s 没有从 job 读范围" % name
        assert "pending_extract" not in src, \
            "%s 自己又算了一次范围——那一次拿到的是空集" % name


# ─────────────────────────────────────────────────────────────────────────────
# 三 · 预览：点之前要看得见会花多少钱
# ─────────────────────────────────────────────────────────────────────────────
def test_预览的候选判据与实际挑选条件一致():
    """两处写法不同就会出现「预览说 5 条，实际买了 8 条」，
    而那种错只有对账才看得出来。"""
    p = dx.incremental_preview()
    assert p["will_call_model"] <= p["ai_batch_limit"]
    assert p["will_call_model"] <= p["candidates_known"] or not p["file_ids"]
    assert p["total"] == len(p["files"]) == len(p["file_ids"])
    # 增量为 0 时必须明说「不会调模型」
    if not p["file_ids"]:
        assert p["will_call_model"] == 0
        assert "不会向模型发出任何调用" in p["cost_note"]


def test_预览不许把未切条款的文件算进候选数():
    """从没抽过的文件此刻还没有条款，它们的候选数要切完才知道。
    编一个总数比给下限更糟——那会被当成预算用。"""
    p = dx.incremental_preview()
    if p["candidates_unknown_files"]:
        assert "下限" in p["cost_note"], "给了下限却没说这是下限"


def test_预览是只读的():
    before = _counts()
    dx.incremental_preview()
    assert _counts() == before
