# -*- coding: utf-8 -*-
"""规则资产一致性校验 —— 批次 1 的回归闸。

用法（在项目根目录下执行）：
    python -I 规则资产/validate.py            # 全部检查
    python -I 规则资产/validate.py --no-xlsx  # 跳过与源 xlsx 的对账（CI 里没有材料时）

为什么现在就写这个脚本、而不是等批次 5 的测试阶段：
  确定性的闸失效而没人发现，比没有这道闸更危险。规则资产是手工维护的
  YAML/JSON，一次手滑就能让某条规则永久不生效、或者让某个敏感词开始误伤。
  下面每一项都对应一次真实踩过的坑，坑的来源写在各自的 docstring 里。

退出码：0 全通过；1 有失败项（失败项会逐条打印）。
"""
import io
import json
import os
import re
import sys
import unicodedata

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RA = os.path.join(ROOT, "规则资产")
SPACE_CHARS = (" ", "\t", "　")
FAILS = []
NOTES = []


def fail(msg):
    FAILS.append(msg)


def load_yaml(rel):
    return yaml.safe_load(io.open(os.path.join(ROOT, rel), encoding="utf-8"))


def load_json(rel):
    return json.load(io.open(os.path.join(ROOT, rel), encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# 0. 四个资产文件必须都能解析
#    踩过的坑：本体_v0.1.yaml 曾经有 5 处 YAML 语法错误，一次都没解析成功过
#    （flow mapping 里出现裸 "] "、": "、"{}"，以及块标量 | 写在 {} 内）。
#    文件看着好好的，load 一次就炸——所以这一项放在最前面。
# ──────────────────────────────────────────────────────────────────────────────
def check_parse():
    assets = {}
    for rel in ("本体_v0.1.yaml",
                "规则资产/标准表_v0.1.yaml",
                "规则资产/必要附件决策表_v0.1.yaml",
                "规则资产/敏感词表_v0.1.yaml",
                "规则资产/内部单位名单_v0.1.yaml"):
        try:
            assets[rel] = load_yaml(rel)
        except Exception as exc:
            fail("[parse] %s 解析失败：%s" % (rel, str(exc).split("\n")[0]))
    for rel in ("规则清单_v0.1.json", "规则绑定_v0.1.json", "事实模型_v0.1.json"):
        try:
            assets[rel] = load_json(rel)
        except Exception as exc:
            fail("[parse] %s 解析失败：%s" % (rel, str(exc).split("\n")[0]))
    return assets


# ──────────────────────────────────────────────────────────────────────────────
# 1. 规则清单自身
# ──────────────────────────────────────────────────────────────────────────────
def check_rules(rules, onto):
    ids = [r["rule_id"] for r in rules]
    if len(set(ids)) != len(ids):
        fail("[rules] rule_id 重复")
    dims = {d["id"] if isinstance(d, dict) else d
            for d in onto["rule_schema"]["dimensions"]}
    for r in rules:
        rid = r["rule_id"]
        if r["dimension"] not in dims:
            fail("[rules] %s 的 dimension %s 不在本体 dimensions 里" % (rid, r["dimension"]))
        if r["rule_class"] not in ("hard", "mixed", "soft"):
            fail("[rules] %s 的 rule_class 非法：%s" % (rid, r["rule_class"]))
        if r["configurator"]["control_type"] not in ("禁止", "警告"):
            fail("[rules] %s 的 control_type 非法：%s" % (rid, r["configurator"]["control_type"]))
        # compiled=false 必须写明原因——「没做完」和「不能做」要分得开
        if not r.get("compiled") and not r.get("compiled_reason"):
            fail("[rules] %s compiled=false 但没有 compiled_reason" % rid)
        # OCR 来源的数字不得直接当硬规则阈值（纪律第 2 条）
        if (r["source_clause"].get("confidence") == "OCR"
                and r["rule_class"] == "hard" and r.get("compiled")
                and r.get("logic", {}).get("type") == "expr"
                and re.search(r"\d", str(r["logic"].get("expr", "")))
                and not r.get("ocr_threshold_cross_checked")):
            NOTES.append("[rules] %s 是 OCR 来源的硬阈值规则，需确认已交叉印证"
                         "（字段 ocr_threshold_cross_checked）" % rid)
        # 软规则不该带禁止
        if r["rule_class"] == "soft" and r["configurator"]["control_type"] == "禁止":
            fail("[rules] %s 是软规则却标禁止——模型辅助判断不能硬拦" % rid)


# ──────────────────────────────────────────────────────────────────────────────
# 2. 必要附件决策表
#    踩过的坑：rows 字段一度写成 25（那是「附件口径原文去重数」），
#    而决策表真正的行数是 29。差 4 行就是 4 类场景没有附件规则。
# ──────────────────────────────────────────────────────────────────────────────
def check_attachment_table(tbl, rules):
    rows = tbl["rows"]
    types = set(tbl["attachment_types"])
    if len(rows) != tbl["meta"]["rows"]:
        fail("[att] rows 实际 %d != meta.rows %d" % (len(rows), tbl["meta"]["rows"]))
    total = sum(r["row_count"] for r in rows)
    if total != tbl["meta"]["source_rows"]:
        fail("[att] row_count 合计 %d != meta.source_rows %d" % (total, tbl["meta"]["source_rows"]))

    def ids_of(item):
        if isinstance(item, dict):
            return item["any_of"] if "any_of" in item else [item["id"]]
        return [item]

    keys = [(r["key"]["大类编码"], r["key"]["小类编码"] or r["key"]["小类"]) for r in rows]
    if len(set(keys)) != len(keys):
        fail("[att] 决策表键不唯一")
    for r in rows:
        must = set(sum([ids_of(i) for i in r["must"]], []))
        opt = set(sum([ids_of(i) for i in r["optional"]], []))
        for code in must | opt:
            if code not in types:
                fail("[att] %s 引用了未定义的附件代码 %s" % (r["key"]["小类"], code))
        # 必要与个性化重叠会让判定自相矛盾
        if must & opt:
            fail("[att] %s 必要与个性化附件重叠：%s" % (r["key"]["小类"], must & opt))
        c = r.get("conflict")
        if c:
            if "resolution" not in c or "why" not in c:
                fail("[att] %s 的 conflict 缺 resolution/why" % r["key"]["小类"])
            if c.get("needs_confirm") and "gap" not in c:
                fail("[att] %s needs_confirm 但没有 gap 编号" % r["key"]["小类"])
    # 未命中时不得按最严或最宽兜底
    if tbl["on_key_not_found"]["action"] != "待人工":
        fail("[att] on_key_not_found 必须是「待人工」，不得按最严/最宽兜底")
    if tbl["on_key_not_found"].get("emit_violation"):
        fail("[att] on_key_not_found 不得直接出违规")
    # 与规则清单对齐
    d601 = [r for r in rules if r["rule_id"] == "TR-D6-01"]
    if d601:
        lg = d601[0]["logic"]
        if lg.get("rows") != len(rows):
            fail("[att] TR-D6-01.logic.rows=%s 与决策表 %d 行不一致" % (lg.get("rows"), len(rows)))


# ──────────────────────────────────────────────────────────────────────────────
# 3. 敏感词表：正反双向
#    纪律第 9 条：误报和漏报同样致命。没有 must_not_hit 的词不许进表。
# ──────────────────────────────────────────────────────────────────────────────
def _n_keep(s):
    return unicodedata.normalize("NFKC", str(s)).upper()


def _n_strip(s):
    s = _n_keep(s)
    for ch in SPACE_CHARS:
        s = s.replace(ch, "")
    return s


_LATIN = re.compile(r"[A-Za-z]")


def kw_hit(entry, text):
    for ex in entry.get("exclusions", []):
        if _n_strip(ex) in _n_strip(text):
            return False
    terms = [entry["term"]] + list(entry.get("aliases", []))
    if entry.get("match") == "word_boundary_latin":
        # 边界判断必须在保留空白的文本上做：删空白会把「XX SPA会馆」
        # 变成「XXSPA会馆」，左邻成了拉丁字母 X，真命中反被挡掉。
        hay = _n_keep(text)
        for t in terms:
            for mo in re.finditer(re.escape(_n_keep(t)), hay):
                left = hay[mo.start() - 1] if mo.start() > 0 else ""
                right = hay[mo.end()] if mo.end() < len(hay) else ""
                if not (left and _LATIN.match(left)) and not (right and _LATIN.match(right)):
                    return True
        return False
    return any(_n_strip(t) in _n_strip(text) for t in terms)


def check_keywords(kw):
    groups = ("sensitive_issuer_keywords", "sensitive_item_keywords", "excluded_purpose_keywords")
    n_hit = n_miss = 0
    for g in groups:
        for e in kw[g]:
            if not e.get("must_not_hit"):
                fail("[kw] 「%s」没有 must_not_hit——没有反例的词不许进表" % e["term"])
            for v in e.get("must_hit", []):
                n_hit += 1
                if not kw_hit(e, v):
                    fail("[kw] 「%s」应命中但没命中：%s" % (e["term"], v))
            for v in e.get("must_not_hit", []):
                n_miss += 1
                if kw_hit(e, v):
                    fail("[kw] 「%s」误伤了正常内容：%s" % (e["term"], v))
            for v in e.get("known_misses", []):
                if kw_hit(e, v):
                    fail("[kw] 「%s」声明为已知漏报却命中了，声明该更新：%s" % (e["term"], v))
    if kw["meta"]["control_type"] != "警告":
        fail("[kw] 敏感词三条规则只能是警告级，当前 %s" % kw["meta"]["control_type"])
    return n_hit, n_miss


# ──────────────────────────────────────────────────────────────────────────────
# 4. 内部单位名单：豁免名单的误报方向是反的，多收一个比少收一个更贵
# ──────────────────────────────────────────────────────────────────────────────
def _n_unit(s):
    s = unicodedata.normalize("NFKC", str(s))
    for ch in SPACE_CHARS:
        s = s.replace(ch, "")
    return s


def unit_lookup(units, deny, text):
    t = _n_unit(text)
    for name in deny:
        if _n_unit(name) in t:
            return ("DENY", name, None)
    best = None
    for e in units:
        if not e["enabled"]:
            continue
        for key in [e["name"]] + e["aliases"]:
            if _n_unit(key) in t and (best is None or len(_n_unit(key)) > len(_n_unit(best[1]))):
                best = (e, key)
    return ("HIT", best[1], best[0]["city"]) if best else ("MISS", None, None)


def check_units(ul):
    units = []
    for seg in ("verbatim", "directly_managed", "observed_with_place", "unconfirmed"):
        for e in ul[seg]:
            units.append({"name": e["name"], "aliases": e.get("aliases") or [],
                          "city": e.get("city"), "enabled": bool(e.get("enabled"))})
    deny = [x["name"] for x in ul["external_denylist"]]
    if not ul["matching"].get("longest_match_required"):
        fail("[unit] 必须声明最长匹配，否则「闽运」会截胡「闽运长乐公交公司」")
    if ul["matching"].get("on_city_null") != "待人工":
        fail("[unit] on_city_null 必须是「待人工」，不得按「福建其他」兜底")
    minyun = [e for e in units if e["name"] == "闽运"]
    if not minyun or minyun[0]["enabled"]:
        fail("[unit] 两字子串「闽运」不得启用——会把 6 家不同县市的下属公司一并豁免")
    for name in deny:
        if any(_n_unit(name) == _n_unit(e["name"]) for e in units if e["enabled"]):
            fail("[unit] 负面清单「%s」与启用条目重名" % name)
    cases = [("泉州港务集团", "HIT", "泉州市"), ("闽运长乐公交公司", "HIT", "长乐区"),
             ("连江闽运公司", "HIT", "连江县"), ("闽运平潭公司", "HIT", "平潭综合实验区"),
             ("厦门轮船公司", "HIT", "厦门市"), ("厦门新立基", "MISS", None),
             ("财智分公司", "MISS", None), ("金蝶软件", "DENY", None),
             ("中兴新云", "DENY", None), ("某某装饰有限公司", "MISS", None)]
    for text, want, city in cases:
        got, _key, got_city = unit_lookup(units, deny, text)
        if got != want or (want == "HIT" and got_city != city):
            fail("[unit] %s 期望 %s/%s，实际 %s/%s" % (text, want, city, got, got_city))
    return sum(1 for e in units if e["enabled"]), len(units), len(deny)


# ──────────────────────────────────────────────────────────────────────────────
# 5. 脱敏：全部出口 + 正反双向
#    纪律第 8、9 条。反例全部取自真实样本，不是编的。
# ──────────────────────────────────────────────────────────────────────────────
RE_CARD = re.compile(r"(?<![0-9])[0-9]{16,19}(?![0-9])")
RE_MOBILE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")


def mask_card(s):
    return RE_CARD.sub(lambda m: m.group(0)[:6] + "*" * 12 + m.group(0)[-4:], s)


def mask_mobile(s):
    return RE_MOBILE.sub(lambda m: m.group(0)[:3] + "*" * 4 + m.group(0)[-4:], s)


def mask_free(s, names):
    for n in sorted(names, key=len, reverse=True):
        s = s.replace(n, n[0] + "*" * (len(n) - 1))
    return s


def check_masking(fact):
    pols = {p["id"]: p for p in fact["masking"]["policies"]}
    sens = {x["concept_id"] for x in fact["sensitive_fields"]}
    covered = set()
    for p in pols.values():
        covered.update(p["applies_to"])
    missing = sens - covered
    if missing:
        fail("[mask] 敏感字段未被任何脱敏策略覆盖：%s" % sorted(missing))
    for cid in [f["concept_id"] for f in fact["free_text_leak_paths"]["fields"]]:
        if cid not in pols["M06"]["applies_to"]:
            fail("[mask] 自由文本泄露路径 %s 未被 M06 覆盖" % cid)
    if len(fact["masking"]["outlets"]) < 8:
        fail("[mask] outlets 少于 8 个——脱敏必须覆盖全部出口，不只是报告正文")
    n = 0
    for pid, fn in (("M02", mask_card), ("M03", mask_mobile)):
        for v in pols[pid]["must_hit"]:
            n += 1
            if fn(v) == v:
                fail("[mask] %s 应脱敏但没动：%s" % (pid, v))
        for v in pols[pid]["must_not_hit"]:
            n += 1
            if fn(v) != v:
                fail("[mask] %s 误伤正常业务内容：%s -> %s" % (pid, v, fn(v)))
    names = [x[0] + x[1:] for x in
             ["甘梓宁", "宓淑仪", "幸婉如", "蓝维安", "岑柏", "乐知行", "宿白", "逯承宇",
              "冼芷清", "宦允文", "笪澜生", "沃明远", "璩砺川", "郗岱", "鄢岚"]]
    for v in pols["M06"]["must_hit"]:
        n += 1
        if mask_free(v, names) == v:
            fail("[mask] M06 应脱敏但没动：%s" % v)
    for v in pols["M06"]["must_not_hit"]:
        n += 1
        if mask_free(v, names) != v:
            fail("[mask] M06 误伤单位名/部门名：%s" % v)
    # 混合文本：真实金额与单据编号一个字都不许动
    mixed = "收款账号622908113255360583，手机13800138000，单据FJPG-BX2610132121，金额1374.80，税金34.11"
    out = mask_mobile(mask_card(mixed))
    for keep in ("FJPG-BX2610132121", "1374.80", "34.11"):
        if keep not in out:
            fail("[mask] 混合文本里误伤了 %s" % keep)
    if "622908113255360583" in out or "13800138000" in out:
        fail("[mask] 混合文本里账号/手机号没脱干净")
    return n


# ──────────────────────────────────────────────────────────────────────────────
# 6. 规则绑定与事实模型：概念引用必须闭合
# ──────────────────────────────────────────────────────────────────────────────
def check_binding(bind, fact, rules, onto):
    concepts = set()
    for e in onto["business"]["entities"]:
        for c in e.get("concepts", []):
            concepts.add(c["id"])
    entities = {e["id"] for e in onto["business"]["entities"]}
    for r in rules:
        lg = r.get("logic", {}) or {}
        for cid in lg.get("concepts", []) or []:
            if cid in entities:
                fail("[bind] %s 把实体 ID %s 放进了 concepts——存在性判断请用 logic.entities"
                     % (r["rule_id"], cid))
            elif cid not in concepts:
                fail("[bind] %s 引用了本体里不存在的概念 %s" % (r["rule_id"], cid))
        # 存在性判断用 logic.entities，与 concepts 分开。混在一起会让契约校验
        # 把实体 ID 当成未定义概念（TR-D2-01 就是这么被抓到的）。
        for eid in lg.get("entities", []) or []:
            if eid not in entities:
                fail("[bind] %s 的 logic.entities 引用了不存在的实体 %s" % (r["rule_id"], eid))
        if lg.get("entities") and not lg.get("entities_semantics"):
            fail("[bind] %s 用了 logic.entities 但没写 entities_semantics" % r["rule_id"])
    if len(bind["bindings"]) != len(rules):
        fail("[bind] 绑定条数 %d != 规则条数 %d" % (len(bind["bindings"]), len(rules)))
    rmap = {r["rule_id"]: r for r in rules}
    ok_avail = set(bind["meta"]["availability_classes"]) - {
        "仅单人可得", "仅多人可得", "样本全空", "样本缺列", "需OA同步",
        "需票据OCR", "待业务侧明确", "需外部数据源"}
    for b in bind["bindings"]:
        if b["rule_id"] not in rmap:
            fail("[bind] 绑定里有规则清单没有的 %s" % b["rule_id"])
            continue
        if b["compiled"] != rmap[b["rule_id"]].get("compiled"):
            fail("[bind] %s 的 compiled 与规则清单不一致" % b["rule_id"])
        if b["demo_executable"] and not b["compiled"]:
            fail("[bind] %s 未编译却标演示可执行" % b["rule_id"])
        if not b["demo_executable"] and not b["demo_blocker"]:
            fail("[bind] %s 演示不可执行但没写原因" % b["rule_id"])
        # 演示可执行的规则，其概念必须真的取得到（允许「仅单人可得」，但要声明范围）；
        # 例外：规则显式声明了 degraded（区间判定 + 不确定段转人工），
        # 且不可得字段被 degraded.missing 完整覆盖。这是「说清楚怎么降级」，
        # 不是「网开一面」——没声明的照旧算失败。
        if b["demo_executable"]:
            deg = b.get("degraded") or {}
            deg_missing = set(deg.get("missing") or [])
            if deg and deg.get("on_indeterminate") != "待人工":
                fail("[bind] %s 声明了 degraded 但不确定区间不是转人工：%s"
                     % (b["rule_id"], deg.get("on_indeterminate")))
            for c in b["concepts"]:
                if c["availability"] in ok_avail | {"仅单人可得", "仅多人可得"}:
                    continue
                if c["id"] in deg_missing:
                    continue
                fail("[bind] %s 标演示可执行，但概念 %s 的可得性是 %s，"
                     "且没被 logic.degraded.missing 覆盖"
                     % (b["rule_id"], c["id"], c["availability"]))
            if any(c["availability"] == "仅单人可得" for c in b["concepts"]) \
                    and "仅单人" not in (b.get("demo_scope") or ""):
                fail("[bind] %s 依赖仅单人可得的字段，demo_scope 必须声明范围" % b["rule_id"])
    nfields = sum(e["field_count"] for e in fact["entities"])
    if nfields != len(concepts):
        fail("[fact] 事实模型字段数 %d != 本体概念数 %d（生成器与本体漂移了）"
             % (nfields, len(concepts)))
    if not fact["engine_contract"].get("excluded_rules_must_be_logged"):
        fail("[fact] 被剔除的规则必须记日志——静默跳过比没有这道闸更危险")
    return len(concepts), nfields


# ──────────────────────────────────────────────────────────────────────────────
# 7. 与源 xlsx 对账（可跳过）
# ──────────────────────────────────────────────────────────────────────────────
def check_xlsx(tbl):
    try:
        import openpyxl
    except ImportError:
        NOTES.append("[xlsx] 没装 openpyxl，跳过源表对账")
        return None
    path = os.path.join(ROOT, "材料", "差旅审核制度规则", "财务共享业务场景核算及审核规则.xlsx")
    if not os.path.exists(path):
        NOTES.append("[xlsx] 找不到源表，跳过对账：%s" % path)
        return None
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    rows = [r for r in wb["业务域"].iter_rows(values_only=True)
            if r and r[0] and "差旅" in str(r[1] or "")]
    groups = {}
    for r in rows:
        key = (str(r[2] or ""), (str(r[4]).strip() if r[4] else None))
        groups.setdefault(key, []).append(r)
    if len(rows) != tbl["meta"]["source_rows"]:
        fail("[xlsx] 源表筛选出 %d 行，决策表声明 %d 行" % (len(rows), tbl["meta"]["source_rows"]))
    if len(groups) != len(tbl["rows"]):
        fail("[xlsx] 源表唯一键 %d 个，决策表 %d 行" % (len(groups), len(tbl["rows"])))
    ymap = {(r["key"]["大类编码"], r["key"]["小类编码"]): r for r in tbl["rows"]}
    for key, rs in groups.items():
        if key not in ymap:
            fail("[xlsx] 源表有键但决策表没有：%s" % (key,))
        elif ymap[key]["row_count"] != len(rs):
            fail("[xlsx] %s 的 row_count：决策表 %d，源表 %d"
                 % (key, ymap[key]["row_count"], len(rs)))
    for key in ymap:
        if key not in groups:
            fail("[xlsx] 决策表有键但源表没有：%s" % (key,))
    return len(rows), len(groups)


# ──────────────────────────────────────────────────────────────────────────────
def main():
    skip_xlsx = "--no-xlsx" in sys.argv
    a = check_parse()
    if FAILS:
        report()
        return 1
    onto = a["本体_v0.1.yaml"]
    rules = a["规则清单_v0.1.json"]["rules"]
    tbl = a["规则资产/必要附件决策表_v0.1.yaml"]
    kw = a["规则资产/敏感词表_v0.1.yaml"]
    ul = a["规则资产/内部单位名单_v0.1.yaml"]
    bind = a["规则绑定_v0.1.json"]
    fact = a["事实模型_v0.1.json"]

    check_rules(rules, onto)
    check_attachment_table(tbl, rules)
    n_kh, n_km = check_keywords(kw)
    n_on, n_all, n_deny = check_units(ul)
    n_mask = check_masking(fact)
    n_concept, n_field = check_binding(bind, fact, rules, onto)
    xl = None if skip_xlsx else check_xlsx(tbl)

    print("规则 %d 条（可编译 %d / 演示可执行 %d）" % (
        len(rules), sum(1 for r in rules if r.get("compiled")),
        sum(1 for b in bind["bindings"] if b["demo_executable"])))
    print("本体概念 %d → 事实模型字段 %d" % (n_concept, n_field))
    print("必要附件决策表 %d 行，row_count 合计 %d" % (
        len(tbl["rows"]), sum(r["row_count"] for r in tbl["rows"])))
    if xl:
        print("  与源表对账：源表 %d 行 / %d 个唯一键，全部对上" % xl)
    print("敏感词 %d 个：正例 %d / 反例 %d" % (
        sum(len(kw[g]) for g in ("sensitive_issuer_keywords", "sensitive_item_keywords",
                                 "excluded_purpose_keywords")), n_kh, n_km))
    print("内部单位名单：启用 %d / 共 %d，负面清单 %d" % (n_on, n_all, n_deny))
    print("脱敏：策略 %d 条、出口 %d 个、正反用例 %d 例" % (
        len(fact["masking"]["policies"]), len(fact["masking"]["outlets"]), n_mask))
    print()
    return report()


def report():
    for n in NOTES:
        print("注意 " + n)
    if FAILS:
        print("** 校验失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("** 校验全通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
