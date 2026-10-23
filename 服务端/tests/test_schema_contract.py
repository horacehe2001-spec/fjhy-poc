# -*- coding: utf-8 -*-
"""schema 与本体的契约测试。

这组测试只做一件事：**保证库结构和本体不漂移**。

漂移的典型长相：本体加了一个概念（比如 OA 终于同步了「出差地点」），
规则开始引用它，但业务表没有对应的列——于是规则在运行期取到 None，
判出一个「字段为空所以合规」的结论。这种失败不报错、不抛异常，
只是悄悄少判一条，和纪律第 9 条说的「确定性的闸失效而没人发现」是同一类。

所以这里用 0004_business.sql 里的 `-- Cxxxx` 标记做双向断言：
  · 本体的每个概念都必须出现在某一列的标记里（不漏）
  · 标记里出现的每个概念都必须在本体里存在（不多）
"""
import io
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.join(ROOT, "服务端")
MIG_DIR = os.path.join(SERVER, "db", "migrations")
FACT = os.path.join(ROOT, "事实模型_v0.1.json")


def fact_model():
    return json.load(io.open(FACT, encoding="utf-8"))


def concepts_in_fact():
    out = {}
    for e in fact_model()["entities"]:
        for f in e["fields"]:
            out[f["concept_id"]] = (e["entity_id"], e["entity"], f["field"], f["availability"])
    return out


def concepts_in_sql():
    """从**全部**迁移文件的 `-- Cxxxx 名称` 标记里抽概念 ID。

    一开始只扫 0004_business.sql，结果后来在 0012 里补的两个概念
    （C0129 出行人员 / C0130 标准依据人）扫不到，测试红了。
    概念会在后续迁移里继续增补，所以必须扫全部迁移。
    """
    text = chr(10).join(io.open(os.path.join(MIG_DIR, f), encoding="utf-8").read()
                     for f in sorted(os.listdir(MIG_DIR)) if f.endswith(".sql"))
    # 只扫业务表。资产层的表（本体、规则、查找表、发布、大模型配置）不承载业务概念，
    # 而且 0003_ontology.sql 里有「-- C0101 ~ C1704」这种**范围说明**，
    # 扫进来会让 C0101 看起来同时落在 expense_bill 和 ontology_concept 两张表上。
    ASSET_PREFIXES = ("ontology_", "process_", "enum_value", "rule", "lookup_",
                      "release", "llm_", "policy_", "extract_", "schema_migration",
                      "load_quarantine", "pool_maintenance", "agent_", "data_pool_",
                      "cycle_log", "feedback_log", "improve_log", "rank_tier_map",
                      "review_", "manual_review")

    def is_asset(t):
        return bool(t) and any(t.startswith(p) for p in ASSET_PREFIXES)

    found = {}
    table = None
    for line in text.split("\n"):
        # 跟踪当前所在的表。一个概念落到**同一张表**的多列上是正常的
        # （C0129「出行人员」是 ref 型概念，在 bill_traveler 里天然是
        # emp_no + name 两列）；落到**不同表**才是问题——那才叫
        # 「两处存储、两个真相」。
        m_tab = re.search(r"(?:CREATE TABLE(?: IF NOT EXISTS)?|ALTER TABLE)\s+(\w+)",
                          line, re.I)
        if m_tab:
            table = m_tab.group(1)
        # 只认「列定义行上的行尾注释」，不认大段说明里顺口提到的概念号。
        # 判据：注释出现在 -- 之后，且该行在 -- 之前还有非空内容（即确实是列定义）。
        if "--" not in line or is_asset(table):
            continue
        code, _, comment = line.partition("--")
        if not code.strip():
            continue
        for m in re.finditer(r"\b(C\d{4})\b", comment):
            col = code.strip().split()[0].strip('",')
            found.setdefault(m.group(1), []).append("%s.%s" % (table or "?", col))
    return found


def test_每个本体概念都有对应的列():
    fact = concepts_in_fact()
    sql = concepts_in_sql()
    missing = sorted(set(fact) - set(sql))
    detail = ["%s %s.%s（%s）" % (c, fact[c][0], fact[c][2], fact[c][3]) for c in missing]
    assert not missing, (
        "本体有 %d 个概念在迁移文件里没有列承载：\n  %s\n"
        "取不到数据的字段也必须建列——规则引用的是概念，概念要有落点，"
        "否则规则连「为什么跑不了」都说不清。" % (len(missing), "\n  ".join(detail)))


def test_列标记里没有本体不存在的概念():
    fact = concepts_in_fact()
    sql = concepts_in_sql()
    extra = sorted(set(sql) - set(fact))
    assert not extra, (
        "迁移文件标了 %d 个本体里不存在的概念：%s\n"
        "要么是本体删了这个概念而表没跟，要么是标记写错了号。" % (len(extra), extra))


def test_一个概念不应落到多张表上():
    """同一张表的多列是正常的，跨表才是问题。

    C0129「出行人员」是 ref 型概念，在 bill_traveler 里天然落在 emp_no + name
    两列上——那是一处存储。而同一个概念出现在两张表里，取数时就有两个真相，
    规则会随取数路径不同给出不同结论。
    """
    sql = concepts_in_sql()
    dup = {}
    for c, cols in sql.items():
        if len({x.split(".")[0] for x in cols}) > 1:
            dup[c] = sorted(set(cols))
    assert not dup, (
        "同一个概念落到了多张表上：%s\n"
        "一个概念两处存储，取数时就有两个真相。" % dup)


def test_敏感字段数与事实模型一致():
    fact = fact_model()
    declared = {x["concept_id"] for x in fact["sensitive_fields"]}
    from_entities = {f["concept_id"] for e in fact["entities"] for f in e["fields"] if f["sensitive"]}
    assert declared == from_entities, (
        "sensitive_fields 与实体里打了 sensitive 标记的字段不一致：\n"
        "  只在清单里：%s\n  只在实体里：%s" % (declared - from_entities, from_entities - declared))


def test_每个敏感字段都被脱敏策略覆盖():
    fact = fact_model()
    covered = set()
    for p in fact["masking"]["policies"]:
        covered.update(p["applies_to"])
    missing = {x["concept_id"] for x in fact["sensitive_fields"]} - covered
    assert not missing, (
        "敏感字段没有脱敏策略覆盖：%s\n"
        "脱敏要覆盖全部出口。漏一个字段，它就会原样流进审计接口、调试查询、"
        "导出文件和大模型请求体。" % sorted(missing))


def test_自由文本泄露路径都被_M06_覆盖():
    fact = fact_model()
    m06 = [p for p in fact["masking"]["policies"] if p["id"] == "M06"][0]
    for f in fact["free_text_leak_paths"]["fields"]:
        assert f["concept_id"] in m06["applies_to"], (
            "%s（%s）是自由文本泄露路径但不在 M06 的作用范围里。\n"
            "摘要这类字段没有 sensitive 标记，按字段脱敏的策略根本不经过它，"
            "而它恰恰会原文进 AI 报告、进大模型请求体、进导出文件。"
            "实测 40 条摘要里 14 条含真实姓名。" % (f["concept_id"], f["field"]))


def _rules():
    return json.load(io.open(os.path.join(ROOT, "规则清单_v0.1.json"), encoding="utf-8"))["rules"]


def _standards():
    import yaml
    return yaml.safe_load(io.open(os.path.join(ROOT, "规则资产", "标准表_v0.1.yaml"), encoding="utf-8"))


def _resolve(node, path):
    """按 a/b[0]/c 形式在嵌套结构里取节点；取不到返回 None。"""
    cur = node
    for seg in [s for s in path.split("/") if s]:
        m = re.match(r"^([^\[]+)((?:\[\d+\])*)$", seg)
        if not m:
            return None
        key, idxs = m.group(1), m.group(2)
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        for i in re.findall(r"\[(\d+)\]", idxs):
            if not isinstance(cur, list) or int(i) >= len(cur):
                return None
            cur = cur[int(i)]
    return cur


def test_用到OCR来源字段的规则必须声明阈值出处():
    """OCR 的数字错一位，规则就从保护变成伤害。

    注意过滤条件是 hard **和** mixed：混合规则的硬条件部分照样用阈值。
    第一版只过滤 hard，结果这条测试空转——唯一的 OCR 概念 C1004 被两条
    mixed 规则引用，一条都抓不到。过滤条件写错的测试比没有测试更坏，
    因为它会让人以为这件事已经被守住了。
    """
    fact = fact_model()
    ocr_concepts = {f["concept_id"]: f["field"]
                    for e in fact["entities"] for f in e["fields"] if f["ocr_sourced"]}
    assert ocr_concepts, "事实模型里一个 ocr_sourced 字段都没有，这条测试会空转，先确认本体标记"
    checked = 0
    for r in _rules():
        if r["rule_class"] not in ("hard", "mixed") or not r.get("compiled"):
            continue
        used = set((r.get("logic") or {}).get("concepts") or [])
        hit = used & set(ocr_concepts)
        if not hit:
            continue
        checked += 1
        ts = r.get("threshold_source")
        assert ts, (
            "%s 是已编译的 %s 规则，用到 OCR 来源字段 %s，但没有声明 threshold_source。\n"
            "必须写明这个阈值到底是从哪一档取的——OCR / 人眼读图 / 原生文本，"
            "三档的可信度完全不同。" % (r["rule_id"], r["rule_class"], sorted(hit)))
        node = _resolve(_standards(), ts["path"])
        assert node is not None, (
            "%s 的 threshold_source.path=%s 在标准表里找不到。"
            "溯源指向一个不存在的位置，等于没溯源。" % (r["rule_id"], ts["path"]))
        conf = ts["confidence"]
        ok = conf in ("人眼读图", "原生文本") or ts.get("cross_checked")
        assert ok, (
            "%s 的阈值出处 %s 可信度是「%s」且未交叉印证。\n"
            "OCR 档必须先与真实单据交叉印证才允许编译：实测把"
            "「闽港财智〔2025〕20号」识别成「赣港财知2025] 20号」，"
            "数字错一位，这条禁止级规则就会开始误伤正常单据。"
            % (r["rule_id"], ts["path"], conf))
    assert checked > 0, "没有任何已编译规则用到 OCR 来源字段——这条测试空转了，检查过滤条件"


def test_标准表里OCR来源的数值阈值都有交叉印证():
    """资产层面的同一条纪律：带数字的 OCR 节点必须有 cross_check。"""
    def walk(node, path=""):
        if isinstance(node, dict):
            yield path, node
            for k, v in node.items():
                yield from walk(v, path + "/" + str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from walk(v, path + "[%d]" % i)

    risky = []
    for p, n in walk(_standards()):
        if not isinstance(n, dict) or n.get("confidence") != "OCR" or n.get("cross_check"):
            continue
        nums = [(k, v) for k, v in n.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        for k, v in list(n.items()):
            if isinstance(v, dict):
                nums += [(k + "." + k2, v2) for k2, v2 in v.items()
                         if isinstance(v2, (int, float)) and not isinstance(v2, bool)]
        if nums:
            risky.append((p, nums))
    assert not risky, (
        "标准表里有 %d 个节点是 OCR 来源、带数值、却没有 cross_check：\n  %s\n"
        "已交叉印证的两组可以参照：伙食补助 100 元/天与样本 100/200/300/400/600/900 全部吻合；"
        "市内交通费 80 元/天 + 40 元档解释了样本里唯一的异常值 120。"
        % (len(risky), risky))
