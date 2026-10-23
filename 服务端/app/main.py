# -*- coding: utf-8 -*-
"""FastAPI 主程序。

启动顺序是有讲究的：
  1. 先做两道契约校验（规则资产 + Agent 配置），不过就不启动
  2. 再挂脱敏中间件
  3. 最后才注册路由

中间件必须在路由之前挂，否则后注册的路由不经过它——
而「后来加的那个接口忘了脱敏」正是最典型的泄露方式。

运行：
    cd 服务端
    python -I -m uvicorn app.main:app --host 127.0.0.1 --port 8000
或：
    python -I app/main.py
"""
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager                # noqa: E402
from fastapi import FastAPI, Request                      # noqa: E402
from fastapi.responses import JSONResponse                # noqa: E402

from app import config, db, mask                          # noqa: E402
from app.api import (agents, ai_review, asset, doclib, domain, onto, pub,  # noqa: E402
                     publish, review, sys_llm)  # noqa: E402
from app.audit import recorder as rec                     # noqa: E402
from app.rules import engine, impl                        # noqa: E402,F401
from app.runtime import contract, yaml_loader             # noqa: E402

@asynccontextmanager
async def lifespan(_app):
    """Fail Fast：两道校验不过就不启动。

    不是「启动后报警」——启动后报警的系统会在报警被忽略的情况下
    继续提供错误的审核结论，那比不启动坏得多。
    """
    print("=" * 72)
    cv = contract.verify(verbose=False)
    print("规则资产契约：%d 项，失败 %d，警告 %d；执行集 %d / 剔除 %d"
          % (len(cv.checks), len(cv.errors), len(cv.warnings),
             len(cv.executable), len(cv.excluded)))
    if not cv.ok and config.CONTRACT_MODE == "fail_fast":
        raise RuntimeError("规则资产契约校验失败，拒绝启动：" + chr(10) +
                           chr(10).join("  - " + e for e in cv.errors))
    ac = yaml_loader.load(verbose=False)
    print("Agent 配置：%d 个，检查项 %d，失败 %d" % (len(ac.agents), len(ac.checks), len(ac.errors)))
    if not ac.ok:
        raise RuntimeError("Agent 配置校验失败，拒绝启动：" + chr(10) +
                           chr(10).join("  - " + e for e in ac.errors))
    # 第三道：执行集里的规则必须都有实现。
    # 这道闸原先只写在 orchestrator.boot() 里，而 boot() 从来没有被调用过——
    # 结果 9 条没有实现的规则在执行集里待了很久，每张单都产出一条
    # 「代码缺口」待人工，而报告上看着它们在正常工作。
    # 契约校验（第一道）只比对「该不该跑」，比不出「有没有实现」，两道都要有。
    rs = engine.load_ruleset()
    missing = rs.missing_impl
    print("规则集 %s / 本体 %s：执行集 %d 条，缺实现 %d"
          % (rs.rule_set_version, rs.ontology_version, len(rs), len(missing)))
    if missing:
        raise RuntimeError(
            "执行集里有 %d 条规则没有实现，拒绝启动：%s%s%s"
            % (len(missing), ", ".join(missing), chr(10),
               "   契约校验说它该跑，而代码里没有对应实现——它既不在剔除名单里、"
               "也不会产生任何结论，报告上看不出少了一条。"))
    if config.MASK_MODE != "on":
        print("** 警告：FJHY_MASK_MODE=%s。脱敏已关闭，生产环境绝不允许。"
              % config.MASK_MODE)
    print("脱敏：%d 条策略，覆盖 %d 个出口" % (len(mask.policies()), len(mask.outlets())))
    print("=" * 72)
    yield


app = FastAPI(title="福建海运集团POC · 差旅费 AI 智能审核",
              version=config.APP_VERSION,
              lifespan=lifespan,
              description="领域知识资产 + 报账单 AI 审核端。"
                          "AI 结论为参考，不阻断流程；闸门在人工复核。")

# ── 脱敏中间件：**必须在路由之前挂** ─────────────────────────────────────────
# 挂在这里，所有 JSON 响应（含异常处理器返回的 500）都会过一遍 scrub。
# 在每个 handler 里各自调 scrub 的做法行不通：handler 会越写越多，总有人忘；
# 而且异常处理器返回的响应根本不经过 handler，错误信息里带着参数值，
# 是最容易漏的出口之一。
app.add_middleware(mask.MaskMiddleware)

app.include_router(asset.router)
app.include_router(domain.router)
app.include_router(review.router)
app.include_router(review.audit_router)
app.include_router(review.debug_router)
app.include_router(ai_review.router)
app.include_router(sys_llm.router)
app.include_router(doclib.router)
app.include_router(doclib.ex_router)
app.include_router(onto.router)
app.include_router(publish.router)
# 对外消费面单独一个 router：只读、只给已发布版本、路径带 v1 承诺兼容。
# 和管理面分开挂，日后要给消费面加鉴权或限流才不用逐个接口挑（挑错一个就是漏）。
app.include_router(pub.router)
# 系统 · 智能体工作台。全站 12 个 Agent 的契约/状态/痕迹散在三处，这是唯一能
# 一次看全的地方。它**只读**：不改参数、不投信号、不停 Agent——
# 改参数要走 improve 的 bounds 校验与 improve_log，不能从页面绕过去。
app.include_router(agents.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """异常响应也要脱敏。

    错误信息里经常带着查询参数、SQL 片段、甚至整行数据。
    中间件会处理这个 JSONResponse，所以这里只负责不把堆栈直接吐出去。
    """
    return JSONResponse(status_code=500,
                        content={"error": type(exc).__name__,
                                 "detail": str(exc)[:500],
                                 "note": "详细堆栈只记在服务端日志里，不通过接口返回"})


@app.get("/health")
def health():
    ok_db, info = db.healthcheck()
    ok_audit, audit = rec.audit_consistency()
    return {
        "ok": ok_db and ok_audit,
        "version": config.APP_VERSION,
        "db": info,
        "audit": audit,
        "mask_mode": config.MASK_MODE,
        "contract_mode": config.CONTRACT_MODE,
    }


@app.get("/api/startup-report")
def startup_report():
    """启动自检的完整结果。前端「系统状态」页用它。

    **这个接口要把失败项原样列出来**，不能只回一个 ok/false。
    「哪 3 项没过、为什么」才是能让人去修的信息。
    """
    cv = contract.verify(verbose=False)
    ac = yaml_loader.load(verbose=False)
    return {
        "contract": {
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in cv.checks],
            "errors": cv.errors, "warnings": cv.warnings,
            "executable": cv.executable,
            "excluded": [{"rule_id": r, "why": w} for r, w in cv.excluded],
        },
        "agents": {
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in ac.checks],
            "errors": ac.errors,
            "items": [{"agent_id": a.agent_id, "version": a.agent_version,
                       "layer": a.layer, "name": a.name,
                       "triggers": a.enabled_triggers,
                       "reads": sorted(a.readable_tables),
                       "writes": sorted(a.writable_tables),
                       "params": a.param_bounds,
                       "forbidden": a.improve.get("forbidden")}
                      for a in ac.agents.values()],
        },
        "mask": {
            "mode": config.MASK_MODE,
            "outlets": mask.outlets(),
            "policies": [p["id"] + " " + p["name"] for p in mask.policies().values()],
            "sensitive_fields": mask.sensitive_concepts(),
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
