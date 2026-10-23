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
    a = ap.parse_args()
    status() if a.status else load()
