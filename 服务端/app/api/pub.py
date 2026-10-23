# -*- coding: utf-8 -*-
"""对外消费面：别的业务怎么用这一版的 文件 / 规则 / 三元本体。

用户 2026-10-19：「以版本的形式资产化，**并允许被其他业务消费**。例如 API、本体等」。
这个模块就是那个「允许」的落点，四条硬规矩：

  ① **只读快照，不读活表。**
     一律 SELECT ... FROM release_asset。读 rule / ontology_* / doc_lib_file
     等于版本号是假的——活表随时在变，同一个号两次拉到的东西会不一样。
     tests/test_release_pack.py 里有一条测试扫本文件源码钉这件事。
  ② **草稿不出门。** 只有 published / rolled_back 的版本能被消费。
     rolled_back 仍然给，但响应里带 withdrawn=true 与原因——
     已经集成了那一版的系统需要知道它被撤了，404 掉只会让对方以为是网络问题。
  ③ **路径带 v1。** 层名、字段名、版本号都是对外契约，改名等于改别人的代码。
     要改先加新层/新版本并存，不原地重命名。
  ④ **旁路出口也过脱敏**（CLAUDE.md 第 8 条）。
     JSON 响应由全局 ASGI 中间件兜住；而**资产包是 application/zip，
     中间件按 content-type 放行**，所以包内容在这里自己过一遍
     mask.mask_text（按内容判定：卡号/手机号/密钥/闭集合人名）。
     **不走按字段名判定的那套**——上一轮刚踩过：那套会把本体元素名
     「报销附件」打成「报**」。

没有鉴权：POC 里不做授权体系，这件事写在自描述接口里，不藏着。
消费台账只记元数据（版本号、资源名、调用方自报的名字、字节数），不记 IP、
不记请求参数、不记响应内容。
"""
import datetime
import hashlib
import io
import json
import re
import urllib.parse
import zipfile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .. import db, mask, release_pack as rp

router = APIRouter(prefix="/api/pub/v1", tags=["对外消费"])


def _header_safe(v, fallback):
    """HTTP 头里只放 ASCII。**非 ASCII 会让响应 500，不是乱码。**

    Starlette 按 latin-1 编码响应头，中文字符直接抛
    UnicodeEncodeError —— 整个下载 500，而报出来的是服务器内部错误，
    看不出是文件名的事。实测踩到（2026-10-20，用中文前缀的 release 名
    拉 /bundle）。
    """
    out = re.sub(r"[^ -~]", "", str(v or "")).strip()
    out = out.replace('"', "").replace("\\", "")
    return out or fallback


def _attachment(fname):
    """content-disposition。**两个文件名都给**（RFC 6266 / RFC 5987）。

    `filename=` 只能是 ASCII，给的是去掉非 ASCII 之后的退化名；
    `filename*=UTF-8''…` 给的是百分号编码的真名，认这个的浏览器
    （Chrome / Edge / Firefox / Safari 都认）会用真名存盘。
    只给前者会丢中文名，只给后者老客户端会存成 `bundle`。
    """
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(fname or "")).strip("_")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name or "bundle.zip", urllib.parse.quote(str(fname or ""), safe=""))

# 便捷别名 → 层名。层名带中文，别的系统拼 URL 不方便，所以给一组 ASCII 别名。
ALIAS = {
    "files": "文件", "rules": "规则", "params": "规则参数",
    "onto-source": "本体源文件",
    "ontology-business": "业务本体", "ontology-rule": "规则本体",
    "ontology-process": "流程本体",
    "facts": "事实模型", "bindings": "规则绑定",
}
BUNDLE_FILE = {                      # 层名 → 资产包里的文件名
    "文件": "01_文件清单.json", "规则": "02_规则.json", "规则参数": "03_规则参数.json",
    "本体源文件": "04_本体源文件.json", "业务本体": "05_业务本体.json",
    "规则本体": "06_规则本体.json", "流程本体": "07_流程本体.json",
    "事实模型": "08_事实模型.json", "规则绑定": "09_规则绑定.json",
}


def _client(req):
    return req.headers.get("x-client") or req.headers.get("user-agent")


def _resolve(version):
    """版本号 / 发布号 / 字面量 current → release 行。**只认已发布的。**"""
    if version in ("current", "@current"):
        r = rp.current_release()
        if not r:
            raise HTTPException(404, "现在没有生效版本。发布管理里把某一版「切为当前」。")
        return r
    r = db.query_one("""SELECT * FROM release
                         WHERE rule_set_version=%s OR release_id=%s""", (version, version))
    if not r:
        raise HTTPException(404, "没有这个版本：%s" % version)
    if r["status"] not in ("published", "rolled_back"):
        raise HTTPException(404, "版本 %s 的状态是「%s」，不对外提供。"
                                 "**草稿不出门**：没签字的口径流出去，"
                                 "别的系统就按一份没人认过的规则在判单。" % (version, r["status"]))
    return r


def _meta(r):
    return {
        "release_id": r["release_id"], "version": r["rule_set_version"],
        "ontology_version": r["ontology_version"], "status": r["status"],
        "withdrawn": r["status"] == "rolled_back",
        "withdrawn_reason": r["rollback_reason"],
        "published_at": r["published_at"], "reviewed_by": r["reviewed_by"],
        "bundle_sha256": r["bundle_sha256"],
        "rule_count": r["rule_count"], "compiled_count": r["compiled_count"],
        "executable_count": r["executable_count"], "note": r["note"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 自描述
# ─────────────────────────────────────────────────────────────────────────────
@router.get("")
def index():
    """这组接口自己说明自己。集成方不该靠猜，也不该等人发文档。"""
    return {
        # service_name 不叫 name：叫 name 的字段会被人名脱敏按字段名吃掉
        # （实测变成「福*********************」）。这是同一个坑的第三次，
        # 处方不变：改键名，不动策略——本系统里 name 绝大多数真是人名。
        "service_name": "福建海运集团 · 差旅费审核口径资产（只读）",
        "triple_ontology": list(rp.TRIPLE),
        "layers": [{"layer": n, "ord": o, "alias": next(
            (a for a, v in ALIAS.items() if v == n), None), "note": note}
            for n, o, _k, _r, note in rp.LAYERS],
        "endpoints": [
            {"path": "/api/pub/v1/releases", "what": "可消费的版本清单（已发布 / 已撤回）"},
            {"path": "/api/pub/v1/current", "what": "当前生效版本的元数据"},
            {"path": "/api/pub/v1/releases/{版本}", "what": "一版的清单：九层各自的内容校验值"},
            {"path": "/api/pub/v1/releases/{版本}/layers/{层名或别名}", "what": "某一层的快照内容"},
            {"path": "/api/pub/v1/releases/{版本}/bundle", "what": "整版资产包（zip）"},
        ],
        "rules_of_the_road": {
            "版本": "{版本} 可以是版本号、发布号，或字面量 current",
            "快照": "一律读冻结快照，**不读活表**。同一个版本号永远拉到同一份内容",
            "草稿": "只有已发布的版本能消费；已撤回的仍给，但带 withdrawn=true",
            "鉴权": "**POC 里没有鉴权**。上生产前要加，这件事摆在这里不藏着",
            "调用方自报": "请在 HTTP 头 X-Client 里写**ASCII**的系统名（头按 ISO-8859-1 传，"
                     "中文到不了对端）。只用于台账，不当凭据",
            "脱敏": "响应经全局脱敏；资产包（zip）在打包时按内容过一遍"
                    "（卡号/手机号/密钥/闭集合人名），不按字段名过——"
                    "按字段名会把本体元素名打成星号",
            "校验": "每层给 content_sha256，整版给 bundle_sha256（算在内容上，不是 zip 字节上）",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 版本
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/releases")
def releases(request: Request):
    rows = db.query("""SELECT * FROM release WHERE status IN ('published','rolled_back')
                        ORDER BY published_at DESC NULLS LAST""")
    cur = rp.current_release()
    out = []
    for r in rows:
        m = _meta(r)
        m["is_current"] = bool(cur and cur["release_id"] == r["release_id"])
        out.append(m)
    rp.log_access(None, "releases", _client(request), None)
    return {"items": out, "count": len(out),
            "note": "空清单不等于没做发布——可能是还没有任何一版签字发布。"}


@router.get("/current")
def current(request: Request):
    r = rp.current_release()
    if not r:
        raise HTTPException(404, "现在没有生效版本。")
    rp.log_access(r["release_id"], "current", _client(request), None)
    return _meta(r)


@router.get("/releases/{version}")
def manifest(version: str, request: Request):
    r = _resolve(version)
    assets = db.query("""SELECT layer, ord, source_kind, source_ref, item_count,
                                content_sha256, note
                           FROM release_asset WHERE release_id=%s ORDER BY ord""",
                      (r["release_id"],))
    for a in assets:
        a["is_triple"] = a["layer"] in rp.TRIPLE
        a["alias"] = next((k for k, v in ALIAS.items() if v == a["layer"]), None)
    rp.log_access(r["release_id"], "manifest", _client(request), None)
    return dict(_meta(r), layers=assets, triple_ontology=list(rp.TRIPLE))


@router.get("/releases/{version}/layers/{layer}")
def layer(version: str, layer: str, request: Request):
    """某一层的**快照原文**。层名可用中文或 ASCII 别名。"""
    r = _resolve(version)
    name = ALIAS.get(layer, layer)
    row = db.query_one("""SELECT layer, item_count, content_sha256, source_kind, source_ref,
                                 note, payload FROM release_asset
                           WHERE release_id=%s AND layer=%s""", (r["release_id"], name))
    if not row:
        raise HTTPException(404, "版本 %s 里没有「%s」这一层。可用的层见 /api/pub/v1"
                                 % (version, layer))
    rp.log_access(r["release_id"], "layer:" + name, _client(request), None)
    return {"release": _meta(r), "layer": row["layer"], "alias": layer,
            "item_count": row["item_count"], "content_sha256": row["content_sha256"],
            "source_kind": row["source_kind"], "source_ref": row["source_ref"],
            "note": row["note"], "payload": row["payload"]}


# ─────────────────────────────────────────────────────────────────────────────
# 资产包
# ─────────────────────────────────────────────────────────────────────────────
def _scrub_for_bundle(obj, _depth=0):
    """按**内容**过一遍脱敏，递归。

    这里刻意不用 mask.scrub（按字段名选策略）：那套在这里是错的工具——
    快照里 rule_name / entity_name 这些字段装的是规则名与本体元素名，
    按字段名走人名策略会把「报销附件」打成「报**」。
    按内容判定的 mask_text 只动真卡号、真手机号、真密钥，
    以及闭集合里的真实人名，不会误伤本体。
    """
    if _depth > 40:                      # 快照是自己造的，正常不会这么深；兜底防环
        return obj
    if isinstance(obj, str):
        return mask.mask_text(obj)
    if isinstance(obj, dict):
        return {k: _scrub_for_bundle(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_for_bundle(v, _depth + 1) for v in obj]
    return obj


README = """# 差旅费审核口径资产包

版本 {version}（发布号 {release_id}，本体 {onto}）
签字人 {by} · 发布时间 {at}
整版校验 bundle_sha256 = {bsha}

## 这个包里有什么

{layers}

**三元本体 = 业务本体 / 规则本体 / 流程本体**（不是 RDF 三元组）。
另有一层 `04_本体源文件`，装的是 本体_v0.1.yaml 的**原文含注释**——
下面三层是它的库投影，投影只剩结构，而注释里是口径说明与踩坑记录。

## 这个包里**没有**什么

* **客户的原始文档**。`01_文件清单` 只给「用了哪些、内容 sha256 是多少」，
  不含文件正文、不含绝对路径。要原件请走文件库。
* **业务数据**。一张单据都没有。发布的是口径资产，不是客户的业务数据。
* **鉴权**。这一版接口没有授权体系，上生产前要加。

## 怎么验这个包没被动过

1. 逐个文件算 sha256，和 `MANIFEST.json` 里的 `files[].sha256` 比
   （文件名那一列叫 `file_name`，不叫 `name`——`name` 会被出口的人名脱敏吃掉）。
2. 把各层的 `content_sha256` 按层名排序拼成 `层名:哈希\\n`，再算一次 sha256，
   和上面的 `bundle_sha256` 比。
   **它不是 zip 文件的哈希**——zip 里有时间戳，同样内容两次打包字节不同。

## 脱敏说明

包里的文本按**内容**过了一遍脱敏（银行卡号、手机号、API Key、闭集合人名）。
**没有**按字段名过：那样会把本体元素名也打成星号。
所以 `rule_name`、`entity_name`、`concept_name` 这些字段是完整的规则名与本体名。
"""


def _build_bundle(r):
    """打包。返回 (zip 字节, manifest 字典)。"""
    assets = db.query("""SELECT layer, ord, source_kind, source_ref, item_count,
                                content_sha256, note, payload FROM release_asset
                          WHERE release_id=%s ORDER BY ord""", (r["release_id"],))
    if not assets:
        raise HTTPException(409, "版本 %s 没有快照内容（frozen_at 为空的空壳）。"
                                 % r["rule_set_version"])
    members = []
    for a in assets:
        body = rp.canon(_scrub_for_bundle(a["payload"])).encode("utf-8")
        members.append({"name": BUNDLE_FILE.get(a["layer"], a["layer"] + ".json"),
                        "layer": a["layer"], "bytes": body,
                        "item_count": a["item_count"],
                        "content_sha256": a["content_sha256"]})
    readme = README.format(
        version=r["rule_set_version"], release_id=r["release_id"],
        onto=r["ontology_version"], by=r["reviewed_by"] or "—",
        at=(r["published_at"] or "").isoformat() if r["published_at"] else "—",
        bsha=r["bundle_sha256"] or "—",
        layers="\n".join("* `%s` —— %s（%d 项）"
                         % (m["name"], m["layer"], m["item_count"]) for m in members),
    ).encode("utf-8")
    manifest = {
        "release": _meta(r),
        "triple_ontology": list(rp.TRIPLE),
        # **用 published_at 而不是 now()**：now() 每次打包都变，导致同内容两次打包
        # 的 MANIFEST 字节不同，ZIP_DEFLATED 压缩后字节数可能相差 1（实测复现）。
        # published_at 对已发布版本是固定的，同一个版本下载多少次字节都一样。
        "packed_at": (r["published_at"] or datetime.datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"),
        # file_name 不叫 name：/bundle-manifest 是 JSON 出口，
        # 叫 name 这一列会被人名脱敏吃成「0***」。**zip 里和 JSON 里用同一个键名**，
        # 否则消费方按文档写的代码在两条路上行为不同。
        "files": [{"file_name": m["name"], "layer": m["layer"],
                   "item_count": m["item_count"],
                   "sha256": hashlib.sha256(m["bytes"]).hexdigest(),
                   "bytes": len(m["bytes"]),
                   "content_sha256": m["content_sha256"]} for m in members]
               + [{"file_name": "README.md", "layer": None,
                   "sha256": hashlib.sha256(readme).hexdigest(),
                   "bytes": len(readme)}],
        "verify": "逐文件比 files[].sha256（文件名在 file_name）；"
                  "整版比 release.bundle_sha256"
                  "（算在内容上，不是 zip 字节上）",
        "masking": "按内容过（卡号/手机号/密钥/闭集合人名），"
                   "**不按字段名过**——按字段名会把本体元素名打成星号",
    }
    buf = io.BytesIO()
    # **固定时间戳**：ZipInfo 不带 mtime 的话每次打包字节都不同，
    # 对方没法用「文件哈希一样」判断「包没变」。内容校验在 MANIFEST 里，
    # zip 自己的字节稳定一点对集成方更友好。
    stamp = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name, data):
            zi = zipfile.ZipInfo(name, date_time=stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
        put("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2,
                                        default=str).encode("utf-8"))
        put("README.md", readme)
        for m in members:
            put(m["name"], m["bytes"])
    return buf.getvalue(), manifest


@router.get("/releases/{version}/bundle")
def bundle(version: str, request: Request):
    """整版资产包（zip）。**注意这是个旁路出口**：

    全局脱敏中间件按 content-type 放行非 JSON 响应，所以包内容在
    _build_bundle 里自己过了一遍按内容判定的脱敏。
    """
    r = _resolve(version)
    data, _m = _build_bundle(r)
    rp.log_access(r["release_id"], "bundle", _client(request), len(data))
    fname = "fjhy-%s.zip" % (r["rule_set_version"] or r["release_id"])
    return Response(content=data, media_type="application/zip",
                    headers={"content-disposition": _attachment(fname),
                             # release_id 也要过一遍：它同样来自人起的名字，
                             # 同样会让整个响应 500。
                             "x-fjhy-release": _header_safe(r["release_id"], "release"),
                             "x-fjhy-bundle-sha256": _header_safe(r["bundle_sha256"], "")})


@router.get("/releases/{version}/bundle-manifest")
def bundle_manifest(version: str, request: Request, pretty: bool = Query(False)):
    """只要清单不要包：集成方先看一眼有什么、多大、哈希是多少。"""
    r = _resolve(version)
    _data, m = _build_bundle(r)
    rp.log_access(r["release_id"], "bundle-manifest", _client(request), None)
    return m
