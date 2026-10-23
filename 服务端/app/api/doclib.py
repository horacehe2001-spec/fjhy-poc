# -*- coding: utf-8 -*-
"""栏目一 · 制度文件库。

接口分四组，对应页面的四件事：
  1. 文件来源：上传到托管目录；或纳入服务器上已有目录（原地只读扫描）
  2. 扫描：形成快照，并回答「相对上次变了什么」
  3. 清单：按类型/解析状态筛，看单份文件的正文预览，删托管文件
  4. 台账：谁什么时候传了什么、哪些被拒了、托管区占了多少盘

**两种根，两套权限**（迁移 0034）：
  托管 = var/doclib/{领域id}/{实例id}/，上传落在这里，可写可删
  引用 = 客户的资料目录，原地扫描，0015 的只读纪律原样生效

**路径与文件名都是外来输入。** browse/roots 把本机目录结构暴露给了前端，
上传的文件名与相对路径更是直接参与拼路径。两者分别过 doclib.safe_dir 与
doclib.safe_rel；它们抛 DocLibError 时回 400 而不是 500——
「你给的路径不对」是使用者的问题，混成 500 会让人去翻后端日志找原因。
ConflictError 单独回 409：那不是「你输错了」，而是「这件事会破坏已有的证据链」。

**实例过滤的边界**（0034 的「过滤边界」）：
本文件里 /api/doclib/* 的查询**真的按当前实例过滤**；
而 /api/extract/*（本文件下半部分的 ex_router）仍是**全库**数字。
这不是漏做，是刻意的——抽取那一栏的页面上还没有「当前实例」这个概念，
现在加 WHERE 等于在没有第二份资产的情况下改掉它的口径。
代价写在每个接口的 scope_note 里：让人以为全站都过滤过，等于埋一个说不清的坑。

正文预览的脱敏由全局 MaskMiddleware 覆盖（所有 JSON 出口），这里不重复做。
"""
from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile

from .. import doclib
from .common import must_exist

router = APIRouter(prefix="/api/doclib", tags=["规则资产 · 制度文件库"])


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except doclib.ConflictError as e:
        # 409 而不是 400：动作本身合法，是**后果**不可接受（会断掉证据链）。
        # 混成 400 的话界面只能说「请求有误」，而使用者真正需要知道的是
        # 「有 7 条规则明细挂在这份文件上」。
        raise HTTPException(status_code=409, detail=str(e))
    except doclib.DocLibError as e:
        # PathError / ScopeError 都在这里。两者的处置动作不同，
        # 但都属于「使用者能自己改」的那一类，统一 400 + 把原因原样带出去。
        raise HTTPException(status_code=400, detail=str(e))


def _scope(instance_id=None):
    """本次请求按哪个实例过滤。没选实例时抛 400，**不退化成全库**。

    退化成全库的后果：页头写着「报表 / 华电集团」，清单里列的是福建海运集团
    的 14 份文件，而接口返回里没有任何字段说明这是全库数字——
    演示现场被问住的就是这种。
    """
    return _guard(doclib.resolve_instance, instance_id)


SCOPE_NOTE = ("本栏（制度文件库）的数字**按当前实例过滤**。"
              "规则抽取、本体工场、发布管理那三栏仍是全库数字——"
              "现有资产全部属于「审核 / 福建海运集团」，那三栏的页面上"
              "还没有「当前实例」这个概念，见迁移 0034 的「过滤边界」。")


# ── 文件夹 ──────────────────────────────────────────────────────────────────
@router.get("/browse")
def browse(path: str = Query(None, description="不传则列出盘符")):
    """逐级浏览本机目录。只读目录结构，不读文件内容。"""
    return _guard(doclib.browse, path)


@router.get("/roots")
def roots(instance_id: str = Query(None, description="不传用当前实例")):
    """本实例的文件来源。**托管根排在最前**，它是默认入口。"""
    inst = _scope(instance_id)
    items = doclib.list_roots(inst["instance_id"])
    return {"items": items,
            "instance_id": inst["instance_id"],
            "instance_label": inst["instance_label"],
            "domain_label": inst["domain_label"],
            "managed": [r for r in items if r["kind"] == "托管"],
            "scope_note": SCOPE_NOTE,
            "note": "托管根=客户端上传落盘的目录，可写可删，由系统自动创建；"
                    "引用根=服务器上已有的目录，连同其所有子文件夹原地只读扫描，"
                    "**一个字节都不写**。两种登记都存在数据库里，"
                    "重启服务、换浏览器都还在。"}


@router.post("/roots")
def add_roots(body: dict = Body(...)):
    """纳入服务器上已有的目录（引用根，原地只读）。逐个独立判定，一个失败不影响其余。

    **这不是上传。** 它要求路径在**服务器**上存在；客户端本机的文件走 /upload。
    """
    paths = body.get("paths") or ([body["path"]] if body.get("path") else [])
    if not paths:
        raise HTTPException(400, "没有给出任何路径")
    res = _guard(doclib.add_roots, paths, body.get("label"), body.get("instance_id"))
    return {"items": res,
            "ok_count": sum(1 for r in res if r["ok"]),
            "failed": [r for r in res if not r["ok"]],
            "note": "加入只登记路径，不复制、不移动、不修改磁盘上的任何文件。"
                    "**路径是服务器上的路径**——如果浏览器和服务不在同一台机器上，"
                    "这里选不到你本机的文件，请用上传。"}


# ── 上传（托管目录）─────────────────────────────────────────────────────────
@router.post("/upload")
def upload(files: list[UploadFile] = File(..., description="一次可多份，也可整个文件夹"),
           rel_paths: list[str] = Form(None,
               description="与 files 一一对应的相对路径（整文件夹上传时传 "
                           "webkitRelativePath）；不传就只用文件名"),
           subdir: str = Form(None, description="统一放进托管根下的某个子目录"),
           by: str = Form(None, description="上传人署名"),
           instance_id: str = Form(None, description="不传用当前实例")):
    """把客户端本机的制度文件传到服务器的托管目录。

    落在 var/doclib/{领域id}/{实例id}/ 下，**按内容 sha256 去重**，
    同名不同内容存为新版本（不覆盖）。上传完会自动跑一次扫描做登记与轻解析。

    **逐份独立判定**：一份被拒不影响其余的；被拒的在 /uploads 台账里留行。
    """
    # rel_paths 与 files 的对应关系由前端保证。**长度不等时不猜**：
    # 错位的后果是文件被放进别人的子目录，而落盘之后很难看出来错在哪一份。
    rels = rel_paths or []
    if rels and len(rels) != len(files):
        raise HTTPException(400,
            "rel_paths 给了 %d 个，files 有 %d 个，对不上。"
            "一一对应才敢用——错位会把文件放进别的子目录，落盘之后很难查"
            % (len(rels), len(files)))
    items = [((rels[i] if rels else None) or f.filename, f.file)
             for i, f in enumerate(files)]
    return _guard(doclib.save_uploads, items, instance_id, by, subdir)


@router.get("/uploads")
def uploads(instance_id: str = Query(None), batch_id: str = Query(None),
            only_rejected: bool = Query(False),
            limit: int = Query(200, ge=1, le=2000)):
    """上传台账。**被拒的也在里面**，这是它存在的主要理由。"""
    inst = _scope(instance_id)
    r = doclib.list_uploads(limit, inst["instance_id"], batch_id, only_rejected)
    r["scope_note"] = SCOPE_NOTE
    return r


@router.get("/storage")
def storage(instance_id: str = Query(None),
            all_instances: bool = Query(False, description="看全部实例的占用")):
    """托管区占用、上传上限、允许的扩展名。

    上传是**会改变服务器磁盘状态**的动作，占用必须在界面上看得见——
    不然「盘满了」这件事只会在下一次上传失败时才被发现。
    """
    if all_instances:
        return doclib.storage(None)
    return doclib.storage(_scope(instance_id)["instance_id"])


@router.patch("/roots/{root_id}")
def patch_root(root_id: str, body: dict = Body(...)):
    ok = doclib.set_root(root_id, body.get("enabled"), body.get("label"))
    must_exist(ok or None, "文件夹 " + root_id)
    return {"root_id": root_id, "ok": True}


@router.delete("/roots/{root_id}")
def remove_root(root_id: str, confirm: str = Query("", description="必须传 confirm=yes")):
    if confirm != "yes":
        raise HTTPException(400, "移出文件库需要二次确认，请传 confirm=yes")
    r = _guard(doclib.remove_root, root_id)
    must_exist(r["removed"] or None, "文件夹 " + root_id)
    return r


# ── 扫描 ────────────────────────────────────────────────────────────────────
@router.post("/scan")
def scan(body: dict = Body(None)):
    """扫描并形成快照。第二次及以后会给出变化清单（新增/修改/消失）。

    不传 root_ids 时只扫**当前实例**的根。
    """
    b = body or {}
    return _guard(doclib.scan,
                  root_ids=b.get("root_ids"),
                  parse=b.get("parse", True),
                  force=bool(b.get("force")),
                  instance_id=b.get("instance_id"))


@router.get("/scans")
def scans(limit: int = Query(20, ge=1, le=200),
          instance_id: str = Query(None)):
    inst = _scope(instance_id)
    return {"items": doclib.list_scans(limit, inst["instance_id"]),
            "scope_note": SCOPE_NOTE,
            "note": "第一次扫描也记一行——没有第一次的基线，第二次无从说「变了」。"
                    "跨实例批次（显式指定根扫出来的）也列在这里，"
                    "它确实影响了本实例的文件，藏起来等于台账缺一次真实扫描。"}


@router.get("/changes")
def changes(scan_id: str = Query(None, description="不传给最近一次扫描的"),
            change: str = Query(None, pattern="^(新增|修改|消失)$"),
            limit: int = Query(500, ge=1, le=2000),
            instance_id: str = Query(None)):
    inst = _scope(instance_id)
    r = doclib.list_changes(scan_id, change, limit, inst["instance_id"])
    r["scope_note"] = SCOPE_NOTE
    return r


@router.get("/pending-extract")
def pending(limit: int = Query(500, ge=1, le=2000),
            instance_id: str = Query(None)):
    """待抽取的文件：从没抽过的，加上抽过之后又被改过的。"""
    inst = _scope(instance_id)
    r = doclib.pending_extract(limit, inst["instance_id"])
    r["scope_note"] = SCOPE_NOTE
    return r


# ── 清单 ────────────────────────────────────────────────────────────────────
@router.get("/stats")
def stats(instance_id: str = Query(None)):
    inst = _scope(instance_id)
    r = doclib.stats(inst["instance_id"])
    r["instance_label"] = inst["instance_label"]
    r["domain_label"] = inst["domain_label"]
    r["scope_note"] = SCOPE_NOTE
    return r


@router.get("/files")
def files(q: str = Query(None), ext: str = Query(None),
          root_id: str = Query(None), state: str = Query(None),
          recognize: str = Query(None, description="按「能否识别」筛，如 可识别/待识别"),
          kind: str = Query(None, description="按文件类别筛：制度/范本/单据/其他"),
          origin: str = Query(None, pattern="^(上传|扫描)$", description="按来源筛"),
          instance_id: str = Query(None),
          limit: int = Query(500, ge=1, le=2000)):
    inst = _scope(instance_id)
    rows = doclib.list_files(q, ext, root_id, state, limit, recognize, kind,
                             inst["instance_id"], origin)
    return {"items": rows, "total": len(rows),
            "truncated": len(rows) >= limit,
            "instance_id": inst["instance_id"],
            "instance_label": inst["instance_label"],
            "scope_note": SCOPE_NOTE,
            "note": "只列在库文件；标记为消失的不在这里，去 /changes 看。"
                    "deletable=true 的是托管目录里的文件（可删）；"
                    "false 的在引用目录里，那是客户的资料，本系统对它只读。"
                    "name_copies>1 表示同一实例里有同名的另一个版本"
                    "（同名不同内容时上传不覆盖，另存为「xxx(2).pdf」）。"}


@router.get("/files/{file_id}")
def file_detail(file_id: str, chars: int = Query(4000, ge=200, le=50000)):
    return must_exist(doclib.get_file(file_id, chars), "文件 " + file_id)


@router.delete("/files/{file_id}")
def delete_file(file_id: str,
                confirm: str = Query("", description="必须传 confirm=yes"),
                by: str = Query(None, description="操作人署名")):
    """删掉**托管目录**里的一份文件。

    三道闸，缺一道就会出事：
      · 引用目录里的文件一律拒删（那是客户的资料，本系统只读）→ 400
      · 已产出条款 / 规则明细 / 已签字读图的拒删（会断证据链）→ 409
      · 磁盘文件删掉，**登记行按「消失」保留**（删行会级联清空四张派生表）
    """
    if confirm != "yes":
        raise HTTPException(400, "删除文件需要二次确认，请传 confirm=yes")
    r = _guard(doclib.delete_managed_file, file_id, by)
    return must_exist(r, "文件 " + file_id)


# ── 本机 OCR ────────────────────────────────────────────────────────────────
# 单独一个动作、单独一个按钮，**不并进扫描**：OCR 一页几百毫秒到几秒，
# 27 页就是三分钟起步。扫描要快到可以随时重跑，重扫一慢就没人跑，
# 快照也就不再反映现状。
from .. import ocr as ocr_mod   # noqa: E402


@router.get("/ocr/engine")
def ocr_engine():
    """引擎与语言包的实际情况。装没装、用的哪份语言包，必须能在界面上看到——
    同一份扫描件用 fast 包和 best 包结果不一样，事后回看「这段文字当初怎么来的」
    没有这个信息就说不清。"""
    return ocr_mod.engine_info()


@router.post("/ocr")
def run_ocr(body: dict = Body(None)):
    """对「需OCR」的文件跑本机 OCR。不出网、不计费。"""
    b = body or {}
    try:
        return ocr_mod.run(file_ids=b.get("file_ids"),
                           dpi=int(b.get("dpi") or ocr_mod.DEFAULT_DPI),
                           limit=int(b.get("limit") or 5))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/ocr/stats")
def ocr_stats():
    return ocr_mod.stats()


@router.get("/ocr/{file_id}")
def ocr_pages(file_id: str):
    rows = ocr_mod.pages_of(file_id)
    must_exist(rows or None, "文件 %s 的 OCR 结果" % file_id)
    return {"file_id": file_id, "pages": rows, "total": len(rows),
            "note": "表格页的 OCR 文本只能当线索：OCR 丢的不是字是结构。"
                    "实测住宿费标准表数字全在，但城市↔职级↔金额的对应关系全乱了。"}

# ─────────────────────────────────────────────────────────────────────────────
# 多模态读图：OCR 丢结构的那几页
# ─────────────────────────────────────────────────────────────────────────────
# 和 /ocr 刻意分成两组接口、两个按钮，理由和下面「切条款 vs 调模型」完全一样：
#   /ocr          本机 tesseract，**不出网、不计费**
#   /vision/read  **把那一页的 PNG 发到厂商**，按图计费
# 合成一个按钮的话，用户点「识别」时不知道这一下会不会把客户的制度文件发出去。
from .. import db, vision as vis   # noqa: E402


@router.get("/vision/pages")
def vision_pages(limit: int = Query(40, ge=1, le=200)):
    """该读哪些页：OCR 标了表格的，或者一个字都没认出来的。

    **不是「所有页都读一遍」**：27 页的文件全读是几万 token，
    而其中 23 页的原生文字层本来就是好的——拿钱换一份更差的副本。
    """
    rows = vis.pages_to_read(limit)
    return {"items": rows, "total": len(rows),
            "note": "读图是**唯一会把客户制度文件发到外部的动作**（OCR 是本机跑的）。"
                    "所以这里只列真正需要的页，并且逐页留痕。"}


@router.post("/vision/read")
def vision_read(body: dict = Body(...)):
    """读一页。**这一下会出网、会计费。**"""
    b = body or {}
    if not b.get("file_id") or b.get("page_no") is None:
        raise HTTPException(400, "file_id 与 page_no 必填")
    try:
        return vis.read_page(b["file_id"], int(b["page_no"]),
                             config_id=b.get("config_id"),
                             kind=b.get("kind") or "table")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/vision/page")
def vision_page(file_id: str = Query(...), page_no: int = Query(...)):
    """一页的全部读图结果 + 两个模型的互相印证。"""
    return {"file_id": file_id, "page_no": page_no,
            "reads": vis.latest_by_model(file_id, page_no),
            "cross_check": vis.cross_check(file_id, page_no)}


@router.get("/vision/summary")
def vision_summary():
    return vis.summary()


@router.get("/vision/storage")
def vision_storage():
    return vis.storage()


@router.post("/vision/verify")
def vision_verify(body: dict = Body(...)):
    """人工签字。**签字之前，读图出来的数字不许进任何规则阈值。**

    模型读图不等于人眼读图：它同样会把看不清的格子编出来，而且编得通顺。
    """
    b = body or {}
    if not b.get("id") or not (b.get("by") or "").strip():
        raise HTTPException(400, "id 与签字人必填——没有签字人的签字等于没签")
    n = db.execute("""UPDATE doc_page_vision SET verified_by=%s, verified_at=now(),
                             verify_note=%s WHERE id=%s AND ok""",
                   (b["by"].strip(), b.get("note"), int(b["id"])))
    if not n:
        raise HTTPException(404, "没有这条读图记录，或者它是失败的那次")
    return {"id": int(b["id"]), "verified_by": b["by"].strip(),
            "note": "签字只说明**这一页的誊抄是对的**，不等于可以自动录入："
                    "录哪几行进 standard_limit 仍然是人的决定。"}


# ═════════════════════════════════════════════════════════════════════════════
# 栏目二 · 制度抽取
# ─────────────────────────────────────────────────────────────────────────────
# 两个动作刻意分成两个接口、两个按钮：
#   /extract/split  确定性切条款，不调模型、不出网、不花钱
#   /extract/ai     真调模型产八要素草稿，必须人点，一次只跑有限条
# 合成一个按钮的话，用户点「抽取」时不知道这一下会不会出网、会不会计费。
# ═════════════════════════════════════════════════════════════════════════════
from .. import doclib_extract as dx   # noqa: E402

ex_router = APIRouter(prefix="/api/extract", tags=["规则资产 · 制度抽取"])


@ex_router.post("/split")
def split(body: dict = Body(None)):
    """条款切分。不调模型、不出网。

    `only_changed=true` 只切增量（从没抽过的 + 抽过之后又被改过的）。
    **注意 `file_ids: []`（空列表）表示「一份都不处理」，不是「全部」**——
    这一条在 dx.run_split 里是显式区分的，不传这个键才是全部。
    """
    b = body or {}
    return dx.run_split(file_ids=b.get("file_ids"),
                        only_changed=bool(b.get("only_changed")))


@ex_router.get("/clauses")
def clauses(file_id: str = Query(None), kind: str = Query(None),
            feature: str = Query(None), covered: bool = Query(None),
            q: str = Query(None), limit: int = Query(500, ge=1, le=2000)):
    rows = dx.list_clauses(file_id, kind, feature, covered, q, limit)
    return {"items": rows, "total": len(rows),
            "note": "「未覆盖」不等于「缺规则」：定义、职责、流程叙述类条款本来"
                    "就不该变成判定规则。这里只指出「有阈值/禁止表述但没有规则"
                    "引用它」，要不要补规则是业务专家的判断。"}


@ex_router.get("/stats")
def ex_stats():
    return dx.stats()


@ex_router.get("/tasks")
def ex_tasks(limit: int = Query(20, ge=1, le=200)):
    return {"items": dx.list_tasks(limit)}


@ex_router.post("/ai")
def ai(body: dict = Body(None)):
    """AI 草稿。**真调模型、真出网、真计费**，所以只在点击时跑，一次最多 limit 条。"""
    b = body or {}
    try:
        return dx.run_ai(clause_ids=b.get("clause_ids"),
                         limit=int(b.get("limit") or 8),
                         only_candidates=b.get("only_candidates", True))
    except RuntimeError as e:
        # 没配 extract 用途的默认模型。这是配置问题，回 400 让界面直接说清楚，
        # 不要变成 500 让人去翻后端日志。
        raise HTTPException(400, str(e))


@ex_router.get("/drafts")
def drafts(task_id: str = Query(None), state: str = Query(None),
           limit: int = Query(200, ge=1, le=1000)):
    rows = dx.list_drafts(task_id, state, limit)
    return {"items": rows, "total": len(rows),
            "note": "草稿不是规则。采纳只标记草稿，不自动写规则表——"
                    "八要素里模型给不全的那几项（维度编号、优先级、绑定节点）"
                    "要在「规则本体」页补齐。"}


@ex_router.post("/drafts/{draft_id}/review")
def review(draft_id: str, body: dict = Body(...)):
    try:
        ok = dx.review_draft(draft_id, body.get("state"), body.get("reviewer"),
                             body.get("reason"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    must_exist(ok or None, "草稿 " + draft_id)
    return {"draft_id": draft_id, "state": body.get("state"), "ok": True}


# ── 规则明细（这一栏真正的产物）──────────────────────────────────────────────
from .. import rule_detail   # noqa: E402

# 清单一行三块：**规则 / 出处 / 分类（标签）**。
# 出处不是「文件名」三个字就够：要给出 文件 → 条款号或行号 → 原文片段 → 可信度。
# 少了可信度就会出事——OCR 来的数字和原生文本的数字在页面上长得一模一样，
# 而项目纪律是只有原生文本与人眼读图能进硬阈值。
@ex_router.post("/details")
def details_run(body: dict = Body(None)):
    """按表头名把表格行映射成规则明细 + 把已成稿的草稿投影进同一张表。

    **不出网、不计费、不调模型。**
    """
    b = body or {}
    return rule_detail.run(file_ids=b.get("file_ids"))


@ex_router.get("/details")
def details(origin: str = Query(None), state: str = Query(None),
            mapped: bool = Query(None), file_id: str = Query(None),
            q: str = Query(None), tag: str = Query(None), dim: str = Query(None),
            limit: int = Query(500, ge=1, le=2000)):
    rows = rule_detail.list_details(origin, state, mapped, file_id, q, tag, dim, limit)
    return {"items": rows, "total": len(rows),
            "note": "一行一条规则明细。origin=列映射 的那些是确定性映射出来的"
                    "（不出网、不计费、不经过模型）；origin=AI草稿 的要逐条核数值。"
                    "**采纳只是标记，不等于进了规则库**——写进本体要填维度、"
                    "优先级、绑定节点，那是「规则本体」那一栏的事。"}


@ex_router.get("/details/stats")
def details_stats():
    return rule_detail.stats()


@ex_router.get("/details/tags")
def details_tags():
    return {"items": rule_detail.tag_cloud(),
            "note": "人工标签。闭集合的七个维度（费用维度/控制类型/规则类型/"
                    "来源类别/实现途径/落地状态/可信度）不在这里——那些由明细的"
                    "列派生，存两处必然对不上。"}


@ex_router.post("/details/{detail_id}/review")
def details_review(detail_id: str, body: dict = Body(...)):
    try:
        ok = rule_detail.review(detail_id, body.get("state"), body.get("reviewer"),
                                body.get("reason"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    must_exist(ok or None, "规则明细 " + detail_id)
    return {"detail_id": detail_id, "state": body.get("state"), "ok": True}


@ex_router.post("/details/{detail_id}/tags")
def details_tag_add(detail_id: str, body: dict = Body(...)):
    """人工标签。**署名和理由都不许空**——没有依据的标签等于没有。"""
    try:
        rule_detail.add_tag(detail_id, body.get("tag"), body.get("by"), body.get("why"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"detail_id": detail_id, "tag": body.get("tag"), "ok": True}


@ex_router.delete("/details/{detail_id}/tags/{tag}")
def details_tag_del(detail_id: str, tag: str):
    return {"detail_id": detail_id, "tag": tag, "ok": rule_detail.del_tag(detail_id, tag)}


@ex_router.get("/details/storage")
def details_storage():
    return rule_detail.storage()


# ── 智能体协同编排 ──────────────────────────────────────────────────────────
# 页面要的是**协同的进展**，不是一排按钮。所以这里给三个接口：
#   run       手工启动一次协同（4 个 Agent 一路跑到底）
#   progress  轮询快照：谁在跑、跑到哪个阶段、写了什么、投了什么信号
#   agents    编排元数据，没开跑时也能画图——演示现场最怕白屏
from .. import extract_orchestrator as orch   # noqa: E402


@ex_router.get("/incremental")
def incremental():
    """「增量抽取」点下去会动什么、会花多少钱。**只读，不出网。**

    增量 = **从没抽过的 + 抽过之后又被改过的**（按文件指纹比对）。
    刻意不用「最近一次扫描的变化台账」：那个在「重扫一次没变、但有文件一直
    没抽过」时会变成 0 份，而那种漏是静默的。指纹比对是累计的、自愈的。
    """
    return dx.incremental_preview()


@ex_router.post("/run")
def run_job(body: dict = Body(None)):
    """启动一次抽取协同。**包含 AI 草稿那一步，会真出网、真计费。**

    `only_changed` 默认 **true**（增量）：四个 Agent 全链只处理
    「待抽取」的那几份文件，范围由 L0 一次算定、全链共用。
    增量为 0 份时四步都空跑并如实报 0，**不会退化成全量重跑，也不会调模型**。

    传 `only_changed=false` 是**全量重跑**：改过切分策略或正则之后必须这样跑一次，
    否则文件没动、指纹没变，一份也跑不到——那会得到「改了但看不出改了」的假象。
    """
    b = body or {}
    return orch.start(only_changed=bool(b.get("only_changed", True)))


@ex_router.get("/progress")
def progress():
    return orch.snapshot()


@ex_router.get("/agents")
def agents_meta():
    return orch.meta()


# 归并组与口径冲突的接口**挪到了 /api/onto**（app/api/onto.py）。
# 「统一口径是本体要做的事，规则抽取只负责抽取」（2026-10-19 用户口径），
# 接口路径跟着归属走——留在这里会让下一个人在抽取那一栏找这些数，
# 而两处都挂着同一组接口更糟：改了一处忘另一处，两个页面显示的冲突数会不一样。
