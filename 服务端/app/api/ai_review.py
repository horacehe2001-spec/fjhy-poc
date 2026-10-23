# -*- coding: utf-8 -*-
"""AI 智能审核 API。三层架构：本体快照（S）→ 规则引擎（D）→ LLM 辅助（D-ext）。

三层各司其职：
  本体层   perception/reader.snapshot_fact()
           用本体定义的概念把单据事实捞成结构化对象，下面两层只看这个对象。
  规则引擎  rules/engine.evaluate()
           确定性判断，产出 hit / pass / pending。pending 表示规则引擎
           没有足够信息做决定（缺出差地、缺授权记录等），交 LLM 辅助。
  LLM 辅助  llm/client.chat_json()
           只对 pending findings 介入，给出辅助判断和说明，结果落在
           review_finding.llm_involved + llm_rationale，**不覆盖规则引擎的结论**。
           LLM 结论是参考，不是终局；工作台上会标明哪条来自 LLM。

非合格单（conclusion='hit' 或 'review'）会自动出现在审核岗工作台的
「不合格」筛选里——工作台那边已经按 conclusion 过滤，这里不需要额外推送。
"""
import json

from fastapi import APIRouter

from .. import db
from ..demo import generator
from ..rules import engine
from ..perception import reader
from ..runtime import yaml_loader, pipeline

router = APIRouter(prefix="/api/ai-review", tags=["AI智能审核"])

_cache: dict = {}


def _load_engine():
    """懒加载规则引擎。**生效版本一换就重载**，不然缓存会把旧版一直用下去。

    这个缓存原先只认「有没有初始化过」，没人清它，全仓也没有任何
    失效路径。后果：发布管理里点「切为当前」或「回滚」，库里的
    release_current 换了，而跑着的进程还拿着上一版的 RuleSet——
    页面显示切成功了，审核结果却仍按旧版判，一直到重启才生效。
    这正是「切为当前不好用」里最难发现的那一半：指针写对了，用的人没换。
    """
    eff, _why = engine.effective_release()
    eff_id = eff["release_id"] if eff else None
    if _cache.get("release_id") != eff_id:
        _cache.clear()
    if "ruleset" not in _cache:
        ac = yaml_loader.load(verbose=False)
        if not ac.ok or not ac.agents:
            raise RuntimeError("Agent 配置校验失败：" + "; ".join(ac.errors))
        _cache["agent_cfg"] = list(ac.agents.values())[0]
        _cache["ruleset"] = engine.load_ruleset()
        _cache["standards"] = reader.standards()
        _cache["tier_map"] = reader.tier_map()
        _cache["release_id"] = _cache["ruleset"].release_id
    return _cache


def audit_demo_bills(bill_nos=None, reason="AI智能审核手动触发"):
    """对演示单跑规则链路（S→D→A→F）。**不生成数据、不调模型。**

    抽出来给两个地方用：
      · /batch 的第 3 步；
      · 测试会话收尾的状态补偿（tests/conftest.py）——
        test_detection 的 fixture 会调 generator.generate()，它内部先 clear，
        把演示单的 review_run / review_finding 全删掉，然后只在内存里跑断言、
        不落库。于是**跑完一次全量测试，演示库变成「29 张单、0 条审核结论」**，
        AI 审核页上那几个 KPI 全归零——看页面的人会以为审核结果丢了。
        这跟 conftest 里已经记着的那条（测试重建 rule 表级联清空覆盖台账）
        是同一类问题：验证动作的副作用留在了演示环境里。

    reason 走参数：补偿跑出来的周期要能和人点的那次区分开（一眼可辨）。
    """
    c = _load_engine()
    if bill_nos is None:
        bill_nos = [r["bill_no"] for r in db.query(
            "SELECT bill_no FROM expense_bill WHERE is_demo ORDER BY bill_no")]
    out = []
    for bill_no in bill_nos:
        out.append(pipeline.run_cycle(
            c["agent_cfg"], c["ruleset"], bill_no, "manual", reason,
            c["standards"], c["tier_map"]))
    return out


@router.post("/batch")
def run_batch():
    """生成演示数据（幂等）并执行三层 AI 审核。

    幂等设计：同一张单 + 同一规则集版本 + 同一快照指纹，只产出一份结论
    （由 pipeline 的 A 阶段用 run_id 作幂等键保证）。
    已审过且快照未变的单会被 skip，不重复写库。
    """
    # 1. 生成演示数据（generator 内部先 clear 再 generate，每次调用都刷新）
    generator.generate(verbose=False)

    # 2. 取出所有演示单
    bill_nos = [r["bill_no"] for r in db.query(
        "SELECT bill_no FROM expense_bill WHERE is_demo ORDER BY bill_no")]
    if not bill_nos:
        return {"ok": False, "error": "演示数据生成失败，库里查不到演示单"}

    # 3. 本体快照 + 规则引擎（S→D→A→F 完整 SDAFI 周期）
    try:
        results = [r.as_dict() for r in audit_demo_bills(bill_nos)]
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    # 4. LLM 辅助：对规则引擎判不了的（pending）调 LLM 补充判断
    llm_stat = _llm_supplement()

    # 5. 返回摘要
    total = len(results)
    rejects = sum(1 for r in results if r["conclusion"] == "reject")
    reviews = sum(1 for r in results if r["conclusion"] == "review")
    passed = sum(1 for r in results if r["conclusion"] == "pass")
    failed = sum(1 for r in results if r["status"] != "ok")
    skipped = sum(1 for r in results if r.get("skipped"))
    return {
        "ok": True,
        "stats": {
            "total": total,
            "reject": rejects,
            "review": reviews,
            "pass": passed,
            "failed": failed,
            "skipped": skipped,
        },
        # LLM 这一段单列：**调了几次、成了几次、花了多少 token、慢不慢**，
        # 混在 stats 里会让人以为是规则引擎的数。调用失败不影响审核结论
        # （LLM 只补充，不改引擎判定），但失败必须看得见。
        "llm": llm_stat,
        "results": results,
    }


def _llm_supplement():
    """对演示单里所有 pending findings 调 LLM 给辅助判断。

    **不改规则引擎的结论**：只填 llm_involved=TRUE + llm_rationale。
    工作台展示时会标明「此条来自 LLM 辅助」，审核人自行判断参考价值。

    没有启用的 LLM 配置时静默跳过，不报错——LLM 层是辅助，不是必须项。
    """
    # **按用途取模型。** 页面上「按用途设默认」分 extract / rule_judge / vision_read
    # 三档，这里要的是规则研判那一档。
    # 原来写的是 `WHERE enabled AND is_default LIMIT 1`——不带 purpose、也不带
    # ORDER BY，实测选中的是**读图模型 qwen3-vl-plus**，拿视觉模型判纯文本规则，
    # 页面上设的「规则研判默认」从来没生效过。这种错不会报错：模型照样回 JSON，
    # 结论照样写进 llm_rationale，只有逐条对型号才看得出来。
    cfg = db.query_one("""SELECT * FROM llm_config
                          WHERE enabled AND is_default AND purpose = 'rule_judge'
                          LIMIT 1""")
    if not cfg:
        # 没设默认就退到任一启用的 rule_judge 配置；**绝不退到别的用途**，
        # 宁可不调模型，也不拿读图模型冒充规则研判。
        cfg = db.query_one("""SELECT * FROM llm_config
                              WHERE enabled AND purpose = 'rule_judge'
                              ORDER BY updated_at DESC LIMIT 1""")
    if not cfg:
        return {"called": 0, "ok": 0, "failed": 0,
                "note": "没有启用的「规则研判」用途配置，跳过 LLM 辅助"}

    from ..llm import client as llmc
    from .. import mask

    rows = db.query("""
        SELECT rf.id, rf.rule_id, rf.pending_reason, rf.evidence,
               rr.bill_no
        FROM review_finding rf
        JOIN review_run rr ON rr.run_id = rf.run_id
        JOIN expense_bill eb ON eb.bill_no = rr.bill_no
        WHERE eb.is_demo AND rf.outcome = 'pending' AND NOT rf.llm_involved
        ORDER BY rr.bill_no, rf.rule_id
    """)
    if not rows:
        return {"called": 0, "ok": 0, "failed": 0, "model": cfg.get("model_name"),
                "note": "没有待人工的结论需要模型补充判断"}

    stat = {"called": 0, "ok": 0, "failed": 0, "model": cfg.get("model_name"),
            "config_id": cfg.get("config_id"), "latency_ms": [],
            "prompt_tokens": 0, "completion_tokens": 0, "errors": []}
    for row in rows:
        bill = db.query_one("SELECT biz_type, amount, summary FROM expense_bill "
                            "WHERE bill_no = %s", (row["bill_no"],))
        if not bill:
            continue

        raw_evidence = row["evidence"]
        if isinstance(raw_evidence, str):
            try:
                raw_evidence = json.loads(raw_evidence)
            except Exception:
                raw_evidence = {}
        evidence_safe = mask.scrub(raw_evidence or {})

        user_content = json.dumps({
            "bill_no": row["bill_no"],
            "biz_type": bill["biz_type"],
            "amount": str(bill["amount"]) if bill["amount"] is not None else None,
            "summary": mask.mask_text(bill["summary"] or ""),
            "rule_id": row["rule_id"],
            "pending_reason": row["pending_reason"],
            "evidence": evidence_safe,
        }, ensure_ascii=False, indent=2)

        system = (
            "你是差旅费合规审核 AI。规则引擎对该单据某条规则判断为「待人工审核」，"
            "原因见 pending_reason。请基于证据给出辅助判断。"
            "只回复 JSON，格式：{\"outcome\": \"hit\"|\"pass\"|\"pending\","
            " \"rationale\": \"简要说明，不超过 80 字\"}"
        )
        res, content = llmc.chat_json(cfg, system, user_content, max_tokens=300)
        stat["called"] += 1
        # **只统计成功调用的时延。** 失败的那些是握手失败/超时，几百毫秒就返回，
        # 混进来会把 p50 拉到一个谁也没经历过的数——实测 25 次里混着算是
        # 1.9 秒，只算成功的是 11.8 秒，差了 6 倍。
        if res.ok and res.latency_ms:
            stat["latency_ms"].append(res.latency_ms)
        stat["prompt_tokens"] += res.prompt_tokens or 0
        stat["completion_tokens"] += res.completion_tokens or 0
        # **每一次调用都落 llm_call_log，成败都落。**
        # 这张表建好了一直没人写，于是「发了多少 token、慢在哪一次、哪次失败了」
        # 全部查不到——出了事只能靠回忆。
        # prompt_masked 是**布尔**，记的是「送出去之前脱过敏没有」，不是提示词原文：
        # 原文本身不入库，入库就等于把业务内容又抄了一份到日志里。
        # 这里恒为 TRUE 的依据是上面那两行：evidence 过 mask.scrub、摘要过 mask.mask_text。
        db.execute("""INSERT INTO llm_call_log
                      (config_id, purpose, prompt_tokens, completion_tokens,
                       latency_ms, status, error, prompt_masked)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)""",
                   (cfg.get("config_id"), "rule_judge",
                    res.prompt_tokens, res.completion_tokens, res.latency_ms,
                    "ok" if res.ok else (res.err_kind or "error"),
                    None if res.ok else (res.message or "")[:500]))
        if not res.ok:
            stat["failed"] += 1
            if len(stat["errors"]) < 5:
                stat["errors"].append("%s · %s" % (res.err_kind, (res.message or "")[:120]))
            continue
        try:
            obj = json.loads(content)
            rationale = (obj.get("rationale") or "")[:200]
        except Exception:
            stat["failed"] += 1
            if len(stat["errors"]) < 5:
                stat["errors"].append("回的不是 JSON：" + content[:100])
            continue

        stat["ok"] += 1
        db.execute(
            "UPDATE review_finding SET llm_involved = TRUE, llm_rationale = %s WHERE id = %s",
            (rationale, row["id"]))

    lat = sorted(stat.pop("latency_ms"))
    stat["latency_p50_ms"] = lat[len(lat) // 2] if lat else None
    stat["latency_max_ms"] = lat[-1] if lat else None
    stat["latency_note"] = "只含成功调用；失败的是握手失败/超时，不计入"
    return stat


@router.get("/status")
def get_status():
    """演示单审核状态汇总。用于页面初始加载，不触发新审核。"""
    rows = db.query("""
        SELECT eb.bill_no, eb.amount, eb.summary,
               rr.run_id, rr.conclusion, rr.hit_count, rr.pending_count,
               rr.rule_executed, rr.finished_at
        FROM expense_bill eb
        LEFT JOIN LATERAL (
            SELECT * FROM review_run WHERE bill_no = eb.bill_no
            ORDER BY started_at DESC LIMIT 1
        ) rr ON TRUE
        WHERE eb.is_demo
        ORDER BY eb.bill_no
    """)
    reviewed = sum(1 for r in rows if r["run_id"])
    reject = sum(1 for r in rows if r["conclusion"] == "reject")
    review = sum(1 for r in rows if r["conclusion"] == "review")
    passed = sum(1 for r in rows if r["conclusion"] == "pass")
    llm_count = db.scalar("""
        SELECT count(*) FROM review_finding rf
        JOIN review_run rr ON rr.run_id = rf.run_id
        JOIN expense_bill eb ON eb.bill_no = rr.bill_no
        WHERE eb.is_demo AND rf.llm_involved
    """) or 0

    # 批量加载命中规则（reject/review 单的违规制度），一次查询，不做 N+1
    # rule_name 不用 name 别名——避免脱敏按字段名把规则名打成星号
    run_ids_with_hits = [r["run_id"] for r in rows
                         if r.get("run_id") and (r.get("hit_count") or 0) > 0]
    hits_by_run: dict = {}
    if run_ids_with_hits:
        placeholders = ",".join(["%s"] * len(run_ids_with_hits))
        hit_rows = db.query(f"""
            SELECT rf.run_id, rf.rule_id, rl.name AS rule_name,
                   rf.control_type, rf.message, rf.needs_human
            FROM review_finding rf
            LEFT JOIN rule rl ON rl.rule_id = rf.rule_id AND rl.version = rf.rule_version
            WHERE rf.run_id IN ({placeholders}) AND rf.outcome = 'hit'
            ORDER BY rf.run_id, rf.rule_id
        """, tuple(run_ids_with_hits))
        for h in hit_rows:
            hits_by_run.setdefault(h["run_id"], []).append(h)

    for r in rows:
        r["hits"] = hits_by_run.get(r.get("run_id") or "", [])

    # 大模型辅助汇总。**从库里统计，不取本次调用的内存状态**：
    # 「本次审核摘要」那几个数原先只存在前端组件的 lastStats 里，
    # 页面一刷新、一换页回来就全没了——看页面的人会以为审核结果丢了，
    # 或者以为大模型没接上（实测连跑两轮 /batch 返回完全一致，
    # 25/25 全成功，丢的只是页面上那份内存状态）。能落库的数就从库里取。
    # 口径：llm_call_log 里 purpose='rule_judge' 的全部调用——这张表只由
    # AI 审核链路写，成败都写。
    llm_cfg = db.query_one(
        "SELECT config_id, model_name FROM llm_config "
        "WHERE enabled AND purpose = 'rule_judge' "
        "ORDER BY is_default DESC, updated_at DESC LIMIT 1")
    calls = db.query(
        "SELECT status, latency_ms, prompt_tokens, completion_tokens, called_at "
        "FROM llm_call_log WHERE purpose = 'rule_judge'")
    # 时延只统计成功调用：失败的是握手失败/超时，几百毫秒就返回，
    # 混进来会把 p50 拉到一个谁也没经历过的数。
    lat = sorted(c["latency_ms"] for c in calls
                 if c["status"] == "ok" and c["latency_ms"])
    llm_summary = {
        "configured": bool(llm_cfg),
        "model": llm_cfg["model_name"] if llm_cfg else None,
        "config_id": llm_cfg["config_id"] if llm_cfg else None,
        "called": len(calls),
        "ok": sum(1 for c in calls if c["status"] == "ok"),
        "failed": sum(1 for c in calls if c["status"] != "ok"),
        "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
        "completion_tokens": sum(c["completion_tokens"] or 0 for c in calls),
        "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        "latency_max_ms": lat[-1] if lat else None,
        "last_called_at": max((c["called_at"] for c in calls), default=None),
        "note": "累计口径（llm_call_log 全量，purpose=rule_judge）；时延只含成功调用",
    }

    return {
        "total": len(rows),
        "reviewed": reviewed,
        "reject": reject,
        "review": review,
        "pass": passed,
        "llm_assisted": llm_count,
        "llm_summary": llm_summary,
        "bills": rows,
    }
