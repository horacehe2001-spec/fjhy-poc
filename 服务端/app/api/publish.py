# -*- coding: utf-8 -*-
"""栏目五 发布管理的**管理面**接口。

页面四段（2026-10-19 用户口径：文件 → 规则 → 三元本体 以版本的形式资产化，
并允许被其他业务消费）：
  ① 工作区  现在能发什么：八层现状、与当前生效版本的差异、能不能冻结
  ② 版本    冻结 → 送审 → 签字发布 → 切为当前 / 回滚；每一版的八层快照
  ③ 消费    对外接口清单 + 资产包 + 消费台账（见 app/api/pub.py）
  ④ 存储    哪张表是资产、哪张表是工作区

**管理面与消费面分两个 router**，不是为了好看：
消费面 /api/pub/v1 是给别的系统用的，它只读、只给已发布版本、路径带 v1 承诺兼容；
管理面 /api/publish 会改状态、能看草稿。混在一个前缀下，日后想给消费面
加鉴权或限流就得逐个接口挑，挑错一个就是漏。
"""
from fastapi import APIRouter, Body, HTTPException, Query

from .. import release_pack as rp

router = APIRouter(prefix="/api/publish", tags=["发布管理"])


def _err(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except rp.PackError as e:
        raise HTTPException(400, str(e))


# ── ① 工作区 ────────────────────────────────────────────────────────────────
@router.get("/workspace")
def workspace():
    """现在能发什么。**每条闸都带「怎么过」**，不然页面上只有一个红叉。"""
    return _err(rp.workspace)


# ── ② 版本 ──────────────────────────────────────────────────────────────────
@router.get("/releases")
def releases():
    return rp.list_releases()


@router.get("/releases/{release_id}")
def release(release_id: str):
    return _err(rp.get_release, release_id)


@router.get("/releases/{release_id}/asset/{layer}")
def asset(release_id: str, layer: str):
    """看某一版某一层的**快照原文**。管理面可以看草稿，消费面不行。"""
    return _err(rp.asset_payload, release_id, layer)


@router.post("/freeze")
def freeze(body: dict = Body(...)):
    """把工作区的八层**原样抄一份**存进库，建成草稿。

    抄完之后改 YAML、重装规则、文件库重扫，都动不了这一份。
    """
    return _err(rp.freeze, body.get("version"), body.get("by"), body.get("note"))


@router.post("/releases/{release_id}/review")
def review(release_id: str, body: dict = Body(...)):
    return _err(rp.review, release_id, body.get("by"))


@router.post("/releases/{release_id}/publish")
def publish(release_id: str, body: dict = Body(...)):
    """签字发布。**不自动切为当前**——发布和生效是两件事。"""
    return _err(rp.publish, release_id, body.get("by"))


@router.post("/releases/{release_id}/current")
def switch_current(release_id: str, body: dict = Body(...)):
    return _err(rp.switch_current, release_id, body.get("by"))


@router.post("/releases/{release_id}/rollback")
def rollback(release_id: str, body: dict = Body(...)):
    return _err(rp.rollback, release_id, body.get("to"), body.get("by"), body.get("reason"))


@router.post("/releases/{release_id}/discard")
def discard(release_id: str, body: dict = Body(...)):
    """删草稿。已发布的版本删不掉——那是资产。"""
    return _err(rp.discard, release_id, body.get("by"))


# ── ③ 消费台账 ──────────────────────────────────────────────────────────────
@router.get("/access-log")
def access_log(limit: int = Query(50, ge=1, le=300)):
    return {"items": rp.access_log(limit),
            "note": "「允许被其他业务消费」要在页面上看得见才算做到。"
                    "**只记元数据**：版本号、资源名、调用方自报的名字、字节数；"
                    "不记请求参数、不记响应内容、不记 IP。"}


# ── ④ 存储 ──────────────────────────────────────────────────────────────────
@router.get("/storage")
def storage():
    return rp.storage()
