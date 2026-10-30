# -*- coding: utf-8 -*-
"""把 config/llm_presets.yaml 装进 llm_provider_preset。

装载后自检：**行数对上、而且核对过的那几条字段非空**。
只打印「装了 N 行」是不够的——这个项目里已经吃过一次亏：
标准表装载器字段名猜错，30 行地区全写成 NULL，却照样打印「30 行」。
从那以后装载器一律装完回读。

用法（在 服务端/ 下）：
    python -X utf8 -I app/llm/presets.py
    python -X utf8 -I app/llm/presets.py --status
"""
import argparse
import io
import os
import sys

import yaml

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)), "config", "llm_presets.yaml")

COLS = ("preset_id", "vendor", "label", "base_url", "protocol", "suggest_model",
        "modality", "key_hint", "model_list", "verified_on", "source_url",
        "caveat", "sort_no", "enabled")


def read_yaml(path=YAML_PATH):
    with io.open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    items = doc.get("presets") or []
    if not items:
        raise RuntimeError("llm_presets.yaml 里没有 presets，拒绝装载空表")
    seen = set()
    for it in items:
        pid = it.get("preset_id")
        if not pid:
            raise RuntimeError("有一条预置缺 preset_id：%r" % (it,))
        if pid in seen:
            raise RuntimeError("preset_id 重复：%s" % pid)
        seen.add(pid)
        for k in ("vendor", "label", "base_url"):
            if not it.get(k):
                raise RuntimeError("%s 缺 %s" % (pid, k))
        if not str(it["base_url"]).startswith(("http://", "https://")):
            raise RuntimeError("%s 的 base_url 不是 http(s)：%s" % (pid, it["base_url"]))
        # 没核对的必须写明为什么没核对，否则看不出是「忘了填」还是「确实没核对」
        if not it.get("verified_on") and not it.get("caveat"):
            raise RuntimeError("%s 既没有 verified_on 也没有 caveat——"
                               "没核对过的预置必须说明，不然用户无从判断可信度" % pid)
    return doc, items


def load(verbose=True):
    doc, items = read_yaml()
    with db.tx() as conn:
        cur = conn.cursor()
        for it in items:
            cur.execute(
                """INSERT INTO llm_provider_preset
                   (preset_id, vendor, label, base_url, protocol, suggest_model,
                    modality, key_hint, model_list, verified_on, source_url,
                    caveat, sort_no, enabled)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (preset_id) DO UPDATE SET
                     vendor=EXCLUDED.vendor, label=EXCLUDED.label,
                     base_url=EXCLUDED.base_url, protocol=EXCLUDED.protocol,
                     suggest_model=EXCLUDED.suggest_model, modality=EXCLUDED.modality,
                     key_hint=EXCLUDED.key_hint, model_list=EXCLUDED.model_list,
                     verified_on=EXCLUDED.verified_on, source_url=EXCLUDED.source_url,
                     caveat=EXCLUDED.caveat, sort_no=EXCLUDED.sort_no,
                     enabled=EXCLUDED.enabled""",
                (it["preset_id"], it["vendor"], it["label"], it["base_url"],
                 it.get("protocol") or "openai_compatible",
                 it.get("suggest_model") or None, it.get("modality") or "text",
                 it.get("key_hint"), bool(it.get("model_list", True)),
                 it.get("verified_on") or None, it.get("source_url") or None,
                 it.get("caveat"), int(it.get("sort_no") or 99),
                 bool(it.get("enabled", True))))

    # ── 装完回读自检 ─────────────────────────────────────────────────────────
    rows = db.query("SELECT %s FROM llm_provider_preset" % ", ".join(COLS))
    got = {r["preset_id"]: r for r in rows}
    problems = []
    if len(got) != len(items):
        problems.append("库里 %d 条，文件 %d 条" % (len(got), len(items)))
    for it in items:
        r = got.get(it["preset_id"])
        if r is None:
            problems.append("%s 没进库" % it["preset_id"])
            continue
        if r["base_url"] != it["base_url"]:
            problems.append("%s 的 base_url 装错：库里 %r，文件 %r"
                            % (it["preset_id"], r["base_url"], it["base_url"]))
        # 核对过的那几条，verified_on 必须真的落进去了。
        # 这一项专门防「日期被 YAML 解析成字符串又被静默丢弃」——
        # 丢掉以后界面上「已核对」的会全变成「未核对」，看起来像我一条都没查。
        if it.get("verified_on") and r["verified_on"] is None:
            problems.append("%s 的 verified_on 丢了" % it["preset_id"])
    if problems:
        raise RuntimeError("预置装载自检不过：" + chr(10) +
                           chr(10).join("  - " + p for p in problems))

    v = sum(1 for r in rows if r["verified_on"])
    if verbose:
        print("OK  厂商预置 %d 条（核对过官方文档 %d · 未核对 %d）"
              % (len(rows), v, len(rows) - v))
        print("    核对方式：%s" % doc.get("verified_method", "(未声明)"))
        for r in sorted(rows, key=lambda x: x["sort_no"]):
            print("    %-28s %-52s %s"
                  % (r["label"][:28], r["base_url"][:52],
                     r["verified_on"] or "** 未核对 **"))
    return rows


def purpose_defaults(path=YAML_PATH):
    with io.open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out = doc.get("purpose_defaults") or []
    for it in out:
        for k in ("purpose", "prefer_model", "why"):
            if not (it.get(k) or "").strip():
                raise RuntimeError("purpose_defaults 有一条缺 %s：%r" % (k, it))
    return out


def apply_defaults(apply=False, verbose=True, path=YAML_PATH):
    """按 config/llm_presets.yaml 的 purpose_defaults 对齐各用途的默认型号。

    **只记策略不记凭证。** llm_config 那几条是人在界面上建的、带加密 API Key，
    不在仓库里也不该在——所以这里按**型号名**去库里找已启用的配置，
    找不到就明说找不到，**绝不自动创建配置**：没有 Key 创建出来也是不可用的，
    而界面上看着像有，那比没有更坏。

    **默认不覆盖人工设定**（apply=False）。该用途已经有默认时只报告差异。
    界面上的手工切换往往是现场有意为之（比如某个型号临时限流），
    种子脚本把它悄悄改回去是最难查的那类问题。要强制对齐才传 apply=True。
    """
    plan = purpose_defaults(path)
    rows = db.query("""SELECT config_id, name, purpose, model_name, enabled, is_default
                         FROM llm_config ORDER BY purpose, config_id""")
    by_purpose = {}
    for r in rows:
        by_purpose.setdefault(r["purpose"], []).append(r)

    report = []
    for it in plan:
        pur, want, avoid = it["purpose"], it["prefer_model"], it.get("avoid_model")
        cands = by_purpose.get(pur) or []
        cur = next((c for c in cands if c["is_default"]), None)
        tgt = next((c for c in cands
                    if c["model_name"] == want and c["enabled"]), None)
        if not cands:
            state, act = "这个用途一条配置都没有", "去「大模型配置」页建一条（要填 API Key，仓库里不存）"
        elif tgt is None:
            state = "库里没有已启用的 %s" % want
            act = "建一条 %s 的配置，或改 purpose_defaults 换一个推荐型号" % want
        elif cur is None:
            state = "这个用途还没有默认"
            if apply:
                _set_default(tgt["config_id"], pur)
                act = "已设为默认：%s" % want
            else:
                act = "可设为默认：%s（加 --apply-defaults 执行）" % want
        elif cur["config_id"] == tgt["config_id"]:
            state, act = "已经是推荐型号", "无需处理"
        else:
            bad = " **而且它正是策略里要避开的那个**" if (
                avoid and cur["model_name"] == avoid) else ""
            state = "当前默认是 %s，策略推荐 %s%s" % (cur["model_name"], want, bad)
            if apply:
                _set_default(tgt["config_id"], pur)
                act = "已改为 %s" % want
            else:
                act = "**没有改**（种子不覆盖人工设定）。要对齐加 --apply-defaults"
        report.append({"purpose": pur, "prefer": want, "state": state,
                       "action": act, "why": it["why"]})

    if verbose:
        print("各用途默认型号策略（config/llm_presets.yaml 的 purpose_defaults）")
        for r in report:
            print("    %-12s 推荐 %-24s %s" % (r["purpose"], r["prefer"], r["state"]))
            print("                 → %s" % r["action"])
    return report


def _set_default(config_id, purpose):
    """和 api/sys_llm.set_default 同一个做法：**两步必须在一个事务里**。
    先清旧默认再设新默认，中间崩掉就变成「该用途没有默认模型」；
    反过来先设新的会撞上 ux_llm_default_per_purpose 唯一索引。"""
    with db.tx() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE llm_config SET is_default=FALSE, updated_at=now()
                        WHERE is_default AND COALESCE(purpose,'-')=COALESCE(%s,'-')""",
                    (purpose,))
        cur.execute("""UPDATE llm_config SET is_default=TRUE, updated_at=now()
                        WHERE config_id=%s""", (config_id,))


def status():
    rows = db.query("""SELECT preset_id, label, base_url, verified_on, model_list
                       FROM llm_provider_preset ORDER BY sort_no""")
    for r in rows:
        print("%-26s %-50s %s" % (r["preset_id"], r["base_url"],
                                  r["verified_on"] or "未核对"))
    print("共 %d 条" % len(rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--apply-defaults", action="store_true",
                    help="强制把各用途的默认型号对齐到 purpose_defaults。"
                         "不加这个开关只报告差异，不改人工设定。")
    a = ap.parse_args()
    if a.status:
        status()
        apply_defaults(apply=False)
    else:
        load()
        # 装预置的同时把默认型号策略过一遍。**默认只报告不改**——
        # 这一步挂在 load 里而不是另起一个脚本，是因为部署脚本
        # （部署/entrypoint.sh 的 `python -I app/llm/presets.py`）只会跑
        # 它知道的那几条命令；另起一个脚本就得记得去改部署脚本，
        # 而「忘了改部署脚本」这件事不会报错，只会在下一次重建库时
        # 悄悄丢掉这个结论——这正是这段代码要防的事。
        apply_defaults(apply=a.apply_defaults)
