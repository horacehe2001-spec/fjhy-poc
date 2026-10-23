# -*- coding: utf-8 -*-
"""系统 · 大模型配置。

2026-10-18 从「领域知识资产」移到「系统」组：它是系统级配置，
不是规则加工链上的一环，挂在链里反而让那条链读不清。
接口路径随之从 /api/asset/llm-configs 改到 /api/sys/llm-configs，
旧路径在 asset.py 里留了 307 跳转——已经发出去的链接不该因为归类变了就 404。

【这个模块有两个会真的出网的接口】
  POST /llm-configs/{cid}/test
  POST /llm-probe
它们会把请求发到用户配的第三方地址。所以：
  · 只在用户点击时触发，**没有任何自动/定时调用**
  · 请求体里只有一句固定问候语，**不带任何业务数据**
    （不拿真实报账单去试一个还没验证过的地址）
  · 每次调用都往 llm_test_log 追加一条，谁在什么时候往哪儿发过，查得到

【Key 的三道处理】
  写入：crypto.encrypt（Fernet，可逆）——原来存的是 sha256，取不回明文，
        那样「测试连通」永远做不出来
  读出：所有读接口只回 api_key_mask，明文不出接口
  回显：厂商 4xx 报文常把 Authorization 原样回显，client._scrub 过一遍，
        库里 ck_llm_test_no_key 是最后一道防线
"""
import hashlib
import re

from fastapi import APIRouter, Body, HTTPException, Query

from .. import db, mask
from ..llm import client, crypto
from .common import must_exist

router = APIRouter(prefix="/api/sys", tags=["系统 · 大模型配置"])

PURPOSES = {
    "vision_read": "读票据/行程单图（必须多模态）",
    "rule_judge": "规则判定（长文本推理）",
    "extract": "从制度文件抽规则",
}

# 配置读接口统一的列。**不含 api_key_cipher** ——
# 不是"查询时记得去掉"，是列清单里压根没有它。
# 「记得去掉」这种约定迟早有人忘，而忘的那次就是泄露。
CFG_COLS = """config_id, name AS config_name, provider, preset_id, base_url, model_name,
              api_key_mask, temperature, max_tokens, enable_thinking,
              concurrency, timeout_s, purpose, enabled, is_default,
              last_test_at, last_test_ok, last_test_latency_ms,
              last_test_http_status, last_test_err_kind, last_test_model_echo,
              last_test_message, created_at, updated_at,
              -- Key 的存储格式。**不回密文，只回它是哪一类**。
              -- 必须逐条回：key-status 只给一个总数「有 N 条是旧格式」，
              -- 用户看得见数量却看不出是哪几条，等于知道有问题但不知道去改谁。
              -- 这里用 left() 而不是 LIKE：db.query 总会传一个空元组给 psycopg2，
              -- SQL 里的字面量百分号会被当成占位符，报 IndexError（这个坑踩过一次）。
              CASE WHEN api_key_cipher IS NULL THEN 'none'
                   WHEN left(api_key_cipher, 5) = 'fer1:' THEN 'ok'
                   ELSE 'legacy' END AS key_format"""


# 常见多模态型号名里的标记。**只用来判「有」，不用来判「没有」**：
# 名字里带这些的基本是多模态；不带的**判不出**（gpt-4o、kimi-k3、step-1o
# 这些多模态型号名里就没有 vl/vision）。所以缺标记只降级成「请自己确认」。
VISION_MARKERS = ("vl", "vision", "omni", "4o", "-v-", "glm-4v", "image")


def _looks_vision(model_name):
    m = (model_name or "").lower()
    return any(k in m for k in VISION_MARKERS)


def modality_warning(purpose, preset_id, model_name=None, config_id=None):
    """用途与模型能力的交叉校验。返回警告文案，没问题则返回空串。

    **这是这个模块里最重要的一条检查。**
    vision_read 是拿模型去读发票、行程单的图。配一个纯文本模型上去，
    结果不是报错——是模型根据 prompt 里的文字线索**编一段看起来合理的内容**。
    那种错误没有任何技术信号：HTTP 200、有回复、格式正确、读起来像真的。
    下游拿它当「已核验的票面信息」用，就会出现「AI 看过发票了」而其实它没看见图。

    **2026-10-19 收紧：原来只看预置，不看型号名。**
    实测后果：vision_read 的配置选了 aliyun_bailian_vision（标注 vision、
    建议 qwen-vl-max），而 model_name 填的是 qwen3.5-122b-a10b——
    和两个「文本」用途的配置一模一样。预置是 vision，于是这条检查一次都没报过。
    现在按证据强弱分三级：

      ① 预置标注 text            硬结论，不能用于读图
      ② 同型号还被配成文本用途    **事实**：同一个型号既当文本用又当读图用
      ③ 型号名没有多模态标记      只说「判不出」，并给出预置的建议型号

    为什么三条都只警告不拦：
    用户可能用的是自建网关、或者一个预置里没有的多模态型号，那时判不出来。
    拦死会把对的用法也挡掉。但警告必须写得让人没法当装饰看——
    所以文案里直接说后果；而第 ③ 条必须写明那是「判不出」，
    把「不知道」说成「没问题」和说成「有问题」都是错的。
    """
    if purpose != "vision_read":
        return ""
    if not preset_id:
        return ("用途是「读票据图」，但没有选厂商预置，所以判不出这个型号有没有视觉能力。"
                "**请自己确认它是多模态型号**——纯文本模型拿到图不会报错，"
                "会按 prompt 里的文字线索编一段看起来合理的内容。")
    row = db.query_one("""SELECT modality, suggest_model FROM llm_provider_preset
                           WHERE preset_id=%s""", (preset_id,))
    m = row["modality"] if row else None
    suggest = (row or {}).get("suggest_model")
    if m == "text":
        return ("**这个预置是纯文本型号，不能用于「读票据图」。**"
                "配上去不会报错，模型会按 prompt 里的文字线索编一段看起来合理的内容，"
                "而下游会把它当成已核验的票面信息。请换成带「读图」/ VL 的预置。")
    if m not in ("vision", "both"):
        return ("用途是「读票据图」，但这个预置的能力标注是 %r，判不出有没有视觉能力。"
                "请自己确认。" % (m,))

    # 预置说能读图，但真正发给厂商的是 model_name。下面两条查的是它。
    if not model_name:
        return ""
    same = db.query("""SELECT config_id, purpose FROM llm_config
                        WHERE model_name=%s AND purpose IN ('rule_judge','extract')
                          AND (%s IS NULL OR config_id <> %s)""",
                    (model_name, config_id, config_id))
    if same:
        # **要指名道姓**：既报型号名也报是哪几个配置。
        # 只报配置号的话，看警告的人还得自己去翻那几个配置才知道是哪个型号；
        # 而前端 llmState.ts 那版报的是型号名——两边声称是同一套判断，
        # 说出来的话就不能不一样。
        return ("**这个型号（%s）同时被配成了文本用途**（%s），"
                "所以它大概率是文本模型，不是读图模型——预置选对了不等于型号填对了。"
                "纯文本模型拿到图不会报错，会按 prompt 里的文字线索编一段看起来"
                "合理的内容，而下游会把它当成已核验的票面信息。"
                "这个预置建议的读图型号是 %s——**建议型号也可能已被厂商下架**"
                "（实测过:qwen-vl-max 就不在某个百炼账号的 177 个型号里），"
                "点「测试 · 只列型号」看你账号上真有哪些。"
                % (model_name, "、".join(x["config_id"] for x in same),
                   suggest or "（预置没给建议）"))
    if not _looks_vision(model_name):
        return ("预置标注能读图，但型号名 %r 里没有 vl / vision / omni 这类多模态标记，"
                "**判不出**它有没有视觉能力（gpt-4o、kimi-k3 这类多模态型号名里也没有，"
                "所以这不是结论）。请自己确认；这个预置建议的读图型号是 %s，"
                "但**建议型号可能已下架**——点「测试 · 只列型号」看你账号上真有哪些。"
                % (model_name, suggest or "（预置没给建议）"))
    return ""


def _cfg_row(cid):
    return db.query_one("SELECT %s FROM llm_config WHERE config_id=%%s" % CFG_COLS, (cid,))


def _cfg_for_call(cid):
    """取出调用厂商所需的字段，**含密文**。只在本模块内部用，不进响应。"""
    return db.query_one("""SELECT config_id, name, base_url, model_name, timeout_s,
                                  purpose, api_key_cipher, preset_id
                           FROM llm_config WHERE config_id=%s""", (cid,))


# ─────────────────────────────────────────────────────────────────────────────
# 厂商预置
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/llm-presets")
def list_presets():
    """国内主流厂商的 base_url 预置。

    **verified_on 为空的那几条要在界面上标出来。** 它们是凭印象填的地址，
    没对过官方文档。混在一个下拉里不作区分，用户配不通时会先怀疑自己的 Key、
    再怀疑网络，最后才想到是地址错——那段时间全是白花的。
    """
    rows = db.query("""SELECT preset_id, vendor, label, base_url, protocol,
                              suggest_model, modality, key_hint, model_list,
                              verified_on, source_url, caveat, sort_no
                       FROM llm_provider_preset
                       WHERE enabled ORDER BY sort_no, preset_id""")
    v = sum(1 for r in rows if r["verified_on"])
    return {
        "items": rows,
        "verified": v,
        "unverified": len(rows) - v,
        "purposes": [{"key": k, "label": v2} for k, v2 in PURPOSES.items()],
        "note": "suggest_model 只是建议值。型号名漂移很快（核对当天实测：智谱已到 "
                "glm-5.3、Kimi 已到 kimi-k3），请用「拉取型号」从厂商现取。",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 加密密钥的状态
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/llm-key-status")
def key_status():
    """如实回报 Key 的加密密钥从哪来，以及是否够生产用。

    不藏着：POC 默认把密钥写在 var/llm_secret.key，和密文同一台机器，
    拿到磁盘就等于拿到 Key。界面上要显示这句，否则交付时没人知道这个前提。
    """
    st = crypto.secret_source()
    # LIKE 的模式必须走参数。写成字面量 'enc:%' 会被 psycopg2 当成占位符
    # （db.scalar 总会传一个空元组，于是它尝试插值），报 IndexError。
    st["legacy_rows"] = db.scalar(
        "SELECT count(*) FROM llm_config WHERE api_key_cipher LIKE %s",
        (crypto.LEGACY_PREFIX + "%",)) or 0
    if st["legacy_rows"]:
        st["legacy_note"] = ("有 %d 条配置的 Key 还是旧的单向摘要格式，取不回明文，"
                             "测试会失败。请重新填一次这几条的 API Key。"
                             % st["legacy_rows"])
    return st


# ─────────────────────────────────────────────────────────────────────────────
# 配置的增删改查
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/llm-configs")
def list_configs():
    rows = db.query("SELECT %s FROM llm_config ORDER BY purpose NULLS LAST, config_id"
                    % CFG_COLS)
    # **存量配置也要带警告。** 原来只在保存那一刻算一次，于是已经存进去的
    # 错配置永远不报——实测就有一个 vision_read 配着文本型号躺在库里。
    for r in rows:
        r["modality_warning"] = modality_warning(
            r.get("purpose"), r.get("preset_id"), r.get("model_name"), r["config_id"])
    defaults = {r["purpose"]: r["config_id"] for r in rows if r["is_default"]}
    missing = [k for k in PURPOSES if k not in defaults]
    return {
        "items": rows,
        "defaults": defaults,
        "purposes": [{"key": k, "label": v, "default": defaults.get(k)}
                     for k, v in PURPOSES.items()],
        "missing_defaults": missing,
        "policy": "api_key 只回 api_key_mask（形如 sk-****3f2a）；明文写入后不可读出。"
                  "库里 ck_llm_no_plain 做最后一道防线。",
        "note": ("还有 %d 个用途没有设默认模型：%s。没有默认模型时，"
                 "该用途的规则只会输出「待人工：未调用模型」，不会报错——"
                 "所以这里要显示出来，否则看起来像模型判过了。"
                 % (len(missing), "、".join(PURPOSES[k] for k in missing)))
        if missing else "三个用途都已设默认模型",
    }


@router.post("/llm-configs")
def upsert_config(cfg: dict = Body(...)):
    """写入。接受明文 Key，但**只存密文 + 掩码，响应里不回明文**。"""
    plain = cfg.pop("api_key", None)
    if plain is not None:
        plain = str(plain).strip() or None

    purpose = cfg.get("purpose") or None
    if purpose and purpose not in PURPOSES:
        raise HTTPException(400, "purpose 只能是 %s 之一" % "、".join(PURPOSES))

    name = (cfg.get("name") or "").strip()
    model_name = (cfg.get("model_name") or "").strip()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(400, "base_url 不能为空")
    if not re.match(r"^https?://", base_url):
        raise HTTPException(400, "base_url 必须以 http:// 或 https:// 开头")
    if not model_name:
        raise HTTPException(400, "model_name 不能为空（火山方舟要填推理接入点 ID，ep- 开头）")

    cid = (cfg.get("config_id") or "").strip() or (
        "llm-" + hashlib.sha1(("%s|%s|%s" % (base_url, model_name, purpose or ""))
                              .encode("utf-8")).hexdigest()[:10])

    try:
        cipher = crypto.encrypt(plain) if plain else None
    except RuntimeError as e:
        # 加密不可用时**宁可存不进去**，绝不退回明文或摘要
        raise HTTPException(500, str(e))

    db.execute("""INSERT INTO llm_config
        (config_id, name, provider, preset_id, base_url, model_name,
         api_key_cipher, api_key_mask, temperature, max_tokens, enable_thinking,
         concurrency, timeout_s, purpose, enabled)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (config_id) DO UPDATE SET
          name=EXCLUDED.name, provider=EXCLUDED.provider,
          preset_id=EXCLUDED.preset_id, base_url=EXCLUDED.base_url,
          model_name=EXCLUDED.model_name,
          -- Key 没重填就保留原值：编辑别的字段不该把 Key 清空
          api_key_cipher=COALESCE(EXCLUDED.api_key_cipher, llm_config.api_key_cipher),
          api_key_mask=COALESCE(EXCLUDED.api_key_mask, llm_config.api_key_mask),
          temperature=EXCLUDED.temperature, max_tokens=EXCLUDED.max_tokens,
          enable_thinking=EXCLUDED.enable_thinking,
          concurrency=EXCLUDED.concurrency, timeout_s=EXCLUDED.timeout_s,
          purpose=EXCLUDED.purpose, enabled=EXCLUDED.enabled, updated_at=now()""",
        (cid, name or cid, cfg.get("provider") or "openai_compatible",
         cfg.get("preset_id") or None, base_url, model_name, cipher,
         mask.key_mask(plain) if plain else None,
         cfg.get("temperature") if cfg.get("temperature") is not None else 0,
         cfg.get("max_tokens"), bool(cfg.get("enable_thinking")),
         cfg.get("concurrency") or 3, cfg.get("timeout_s") or 60,
         purpose, cfg.get("enabled", True)))

    return {"config_id": cid,
            "api_key_mask": mask.key_mask(plain) if plain else None,
            "key_saved": bool(plain),
            # 用途与模型能力不匹配要在**保存的那一刻**就说，不是等到跑批时才发现
            "modality_warning": modality_warning(purpose, cfg.get("preset_id"),
                                                 model_name, cid),
            "note": "明文 Key 未返回，也不会出现在任何读取接口里。"
                    "改完建议点一次「测试」——存下来不等于能用。"}


@router.post("/llm-configs/{cid}/enabled")
def set_enabled(cid: str, on: bool = Query(...)):
    """启用 / 停用。

    单独一个接口，不走 upsert：upsert 要求带上 base_url 和 model_name，
    为了翻一个开关而把整条配置重发一遍，中间任何一个字段被前端的默认值覆盖
    都会悄悄改掉配置。开关就该是开关。

    停用当前默认模型是允许的，但要在返回里说清楚后果——
    「默认模型被停用」在界面上看起来仍然是有默认模型的。
    """
    row = must_exist(_cfg_row(cid), "配置 %s" % cid)
    db.execute("UPDATE llm_config SET enabled=%s, updated_at=now() WHERE config_id=%s",
               (bool(on), cid))
    warn = ""
    if not on and row["is_default"]:
        warn = ("这条是「%s」的默认模型，停用后该用途实际上没有可用模型了，"
                "相关规则会输出「待人工：未调用模型」。"
                % PURPOSES.get(row["purpose"], row["purpose"]))
    return {"config_id": cid, "enabled": bool(on), "warning": warn}


@router.delete("/llm-configs/{cid}")
def delete_config(cid: str, confirm: str = Query("", description="必须传 confirm=yes")):
    """删除一条配置。

    要求显式 confirm=yes：这个接口会连带删掉它的测试历史（外键 CASCADE），
    而测试历史是排查「什么时候开始不通」的唯一依据。
    """
    row = must_exist(_cfg_row(cid), "配置 %s" % cid)
    if confirm != "yes":
        raise HTTPException(400, "删除需要 confirm=yes。这会连带删掉 %d 条测试历史"
                            % (db.scalar("SELECT count(*) FROM llm_test_log "
                                         "WHERE config_id=%s", (cid,)) or 0))
    n_tests = db.scalar("SELECT count(*) FROM llm_test_log WHERE config_id=%s", (cid,)) or 0
    db.execute("DELETE FROM llm_config WHERE config_id=%s", (cid,))
    return {"deleted": cid, "was_default": row["is_default"],
            "tests_removed": n_tests,
            "note": "它是该用途的默认模型，删掉后这个用途没有默认模型了，"
                    "相关规则会输出「待人工：未调用模型」" if row["is_default"] else ""}


# ─────────────────────────────────────────────────────────────────────────────
# 设为默认
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/llm-configs/{cid}/default")
def set_default(cid: str):
    """把这条设成**它所属用途**的默认模型。

    为什么是按用途、不是全局一个：
    本系统三种用途对模型能力的要求不同（读图要多模态，判规则要长文本推理）。
    全局只允许一个默认，就会出现拿纯文本模型去读发票图——
    它不会报错，会编一段看起来合理的内容。那种错误最难发现。

    为什么必须在一个事务里：
    先清旧默认再设新默认，中间崩掉就变成「该用途没有默认模型」；
    反过来先设新的会撞上唯一索引。两步必须同生同死。
    """
    row = must_exist(_cfg_row(cid), "配置 %s" % cid)
    purpose = row["purpose"]
    if not purpose:
        raise HTTPException(400, "这条配置没有填 purpose（用途），没法设为某个用途的默认。"
                                 "请先选 %s 之一" % "、".join(PURPOSES))
    if not row["enabled"]:
        raise HTTPException(400, "这条配置是停用状态，不能设为默认——"
                                 "设成默认又停用，等于默认模型不可用，而界面上看着是有的")
    # 不强制要求测试通过（可能是内网地址、或用户就是想先配好），
    # 但没测过就要在返回里说清楚，不要让人以为设成默认就等于可用。
    prev = db.query_one("""SELECT config_id, name AS config_name FROM llm_config
                           WHERE is_default AND COALESCE(purpose,'-')=COALESCE(%s,'-')
                             AND config_id<>%s""", (purpose, cid))
    with db.tx() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE llm_config SET is_default=FALSE, updated_at=now()
                       WHERE is_default AND COALESCE(purpose,'-')=COALESCE(%s,'-')""",
                    (purpose,))
        cur.execute("""UPDATE llm_config SET is_default=TRUE, updated_at=now()
                       WHERE config_id=%s""", (cid,))
    warn = ""
    if row["last_test_ok"] is None:
        warn = "这条还没测试过。设为默认不等于能用——建议点一次「测试」。"
    elif not row["last_test_ok"]:
        warn = ("**这条最近一次测试是失败的**（%s）。已按要求设为默认，"
                "但跑批时会失败。" % (row["last_test_err_kind"] or "原因见测试记录"))
    mw = modality_warning(purpose, row["preset_id"])
    return {"config_id": cid, "purpose": purpose,
            "purpose_label": PURPOSES.get(purpose, purpose),
            "replaced": prev, "warning": warn,
            # 设为默认是「让它真的被用起来」那一步，这里必须再查一遍能力匹配
            "modality_warning": mw}


# ─────────────────────────────────────────────────────────────────────────────
# 测试
# ─────────────────────────────────────────────────────────────────────────────
def _log_and_update(cid, res):
    """测试结果落库：追加一条历史 + 更新 llm_config 上的「最近一次」。"""
    with db.tx() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO llm_test_log
            (config_id, kind, ok, http_status, latency_ms, model_echo,
             reply_excerpt, err_kind, message)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cid, res.kind, res.ok, res.http_status, res.latency_ms,
             res.model_echo, res.reply_excerpt, res.err_kind, res.message))
        cur.execute("""UPDATE llm_config SET
              last_test_at=now(), last_test_ok=%s, last_test_latency_ms=%s,
              last_test_http_status=%s, last_test_err_kind=%s,
              last_test_model_echo=%s, last_test_message=%s
            WHERE config_id=%s""",
            (res.ok, res.latency_ms, res.http_status, res.err_kind,
             res.model_echo, res.message, cid))


@router.post("/llm-configs/{cid}/test")
def test_config(cid: str,
                kind: str = Query("chat", pattern="^(chat|models)$",
                                  description="chat=真发一句话；models=只列型号，不耗 token")):
    """真的发一次请求到厂商。

    **这是出网动作。** 只在点击时触发，没有自动调用。
    请求体里只有一句固定问候语，不含任何业务数据——
    不拿真实报账单去试一个还没验证过的地址。
    """
    cfg = must_exist(_cfg_for_call(cid), "配置 %s" % cid)
    res = client.list_models(cfg) if kind == "models" else client.chat_ping(cfg)
    _log_and_update(cid, res)
    out = res.as_dict()
    out["config_id"] = cid
    out["outbound"] = {"base_url": cfg["base_url"],
                       "path": "/models" if kind == "models" else "/chat/completions",
                       "sent_business_data": False,
                       "prompt": client.PING_PROMPT if kind == "chat" else None}
    return out


@router.post("/llm-probe")
def probe(cfg: dict = Body(...), kind: str = Query("models", pattern="^(chat|models)$")):
    """测一个**还没保存**的配置。

    有这个接口是因为：先存再测，会在库里留下一堆配错的配置；
    而「存下来」在用户眼里往往等于「配好了」。先测通再存更不容易误解。
    这次调用**不写 llm_test_log**（还没有 config_id 可挂），所以返回里说明这一点。
    """
    if not (cfg.get("base_url") or "").strip():
        raise HTTPException(400, "base_url 不能为空")
    res = client.list_models(cfg) if kind == "models" else client.chat_ping(cfg)
    out = res.as_dict()
    out["persisted"] = False
    out["note"] = "这是未保存配置的试测，没有写入测试历史。保存后再测一次会留痕。"
    return out


@router.get("/llm-configs/{cid}/tests")
def test_history(cid: str, limit: int = Query(20, ge=1, le=200)):
    """测试历史。

    只看「最近一次」只能回答「现在通不通」。排错时要回答的是
    「从哪天开始不通的」——跑批出一堆「待人工：未调用模型」时，
    要能对上是哪天 Key 过期的。
    """
    must_exist(_cfg_row(cid), "配置 %s" % cid)
    rows = db.query("""SELECT id, tested_at, kind, ok, http_status, latency_ms,
                              model_echo, reply_excerpt, err_kind, message
                       FROM llm_test_log WHERE config_id=%s
                       ORDER BY tested_at DESC, id DESC LIMIT %s""", (cid, limit))
    return {"items": rows, "total": db.scalar(
        "SELECT count(*) FROM llm_test_log WHERE config_id=%s", (cid,)) or 0}
