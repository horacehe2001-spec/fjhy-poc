# -*- coding: utf-8 -*-
import yaml, io, json, collections
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def rel(*p): return os.path.join(ROOT, *p)

onto = yaml.safe_load(io.open(rel("本体_v0.1.yaml"), encoding="utf-8"))
bind = json.load(io.open(rel("规则绑定_v0.1.json"), encoding="utf-8"))
CA = bind["concept_availability"]
rev = bind["reverse_index"]
TYPE_MAP = {"string":"string","text":"string","int":"integer","money":"decimal(18,2)",
            "date":"date","datetime":"timestamp","daterange":"daterange","bool":"boolean",
            "enum":"enum","ref":"ref"}
doc = collections.OrderedDict()
doc["meta"] = {
 "version":"0.1","generated":"2026-10-18",
 "purpose":"规则引擎的输入契约。由 本体_v0.1.yaml 生成，不手写——手写必然与本体漂移。",
 "generator":"scratchpad/gen_fact.py（输入：本体_v0.1.yaml + 规则绑定_v0.1.json）",
 "note":("每个字段都带 availability。引擎在装载规则时**必须**检查：规则引用的字段若 availability "
         "不在可用集合内，这条规则不得进入执行集，并在启动日志里逐条列出被剔除的规则和原因——"
         "Fail Fast，不是静默跳过。静默跳过会让一条本该拦截的闸悄悄消失，比没有这道闸更危险。"),
}
ents = []
for e in onto["business"]["entities"]:
    fs = []
    for c in e.get("concepts", []):
        cid = c["id"]; a = CA.get(cid, {})
        fs.append(collections.OrderedDict([
            ("concept_id", cid), ("field", c["name"]),
            ("type", TYPE_MAP.get(c.get("type"), c.get("type"))),
            ("enum", c.get("enum")), ("ref", c.get("ref")),
            ("availability", a.get("availability")), ("gap", a.get("gap")),
            ("sensitive", bool(c.get("sensitive"))), ("ocr_sourced", bool(c.get("ocr"))),
            ("used_by_rules", rev.get(cid, [])),
        ]))
    ents.append(collections.OrderedDict([
        ("entity_id", e["id"]), ("entity", e["name"]), ("source", e.get("source")),
        ("field_count", len(fs)), ("fields", fs)]))
doc["entities"] = ents
doc["enums"] = {en["id"]: {"name": en["name"], "source": en.get("source"),
                           "values": en.get("values", [])} for en in onto["business"]["enums"]}
doc["derived_fields"] = bind["derivations"]

# ── 敏感字段与脱敏策略（给 服务端/mask.py）───────────────────────────────────
sens = [(e["entity_id"], e["entity"], f["concept_id"], f["field"])
        for e in ents for f in e["fields"] if f["sensitive"]]
doc["sensitive_fields"] = [{"entity": a, "entity_name": b, "concept_id": c, "field": d}
                           for a, b, c, d in sens]
doc["masking"] = collections.OrderedDict()
doc["masking"]["note"] = (
 "**脱敏必须覆盖全部出口，不是只守主输出路径。** 下面 outlets 列出的每一条，"
 "响应体都要过同一套规则：审核报告正文、审计接口（五张审计表的查询）、调试查询、"
 "配置文件接口、错误信息与堆栈、导出文件（Excel/PDF）、Prometheus 标签、日志。"
 "判断「有没有泄露」要把每个出口的响应都过一遍，而不是看 mask 模块写没写。")
doc["masking"]["outlets"] = [
 "AI 审核报告正文", "审计接口 /audit/*（data_pool_snapshot / cycle_log / agent_signal_log / feedback_log / improve_log）",
 "调试查询接口 /debug/*", "配置接口 /config/*（大模型 Key 只回掩码）",
 "错误响应与异常堆栈", "导出文件（xlsx / pdf）", "Prometheus 指标标签", "应用日志与 SQL 日志",
 "大模型请求体（送进模型前就要脱敏，不能只脱回来的）",
]
doc["masking"]["policies"] = [
 {"id":"M01","name":"姓名","applies_to":["C0123","C0801","C0603","C1203"],
  "rule":"保留姓氏 + 等长「*」，如 乐知行 → 林**；「工号/姓名」格式只脱姓名段",
  "must_hit":["乐知行","鄢岚","宦允文","笪澜生","甘梓宁","沃明远","冼芷清","蓝维安","璩砺川","郗岱","郯筠和"],
  "must_not_hit":["泉州港务集团","厦门新立基","闽运长乐公交公司","科技集团","财智分公司",
                  "aiAudit","运营管理部","总账报表部"],
  "why":"单位名、部门名、系统账号都是中文或含中文，用「2~4 个汉字」这类长度规则必然误伤单位名。只在已知的姓名字段上做，不做全文姓名识别。"},
 {"id":"M02","name":"银行账号","applies_to":["C0124"],
  "rule":"保留前 6 后 4，中间统一 12 个「*」",
  "regex":"(?<!\d)\d{16,19}(?!\d)",
  "must_hit":["622908113255360583"],
  "must_not_hit":["1374.80","34.11","3264","7209","FJPG-BX2610132121","2026-10-13 17:20:52",
                  "CL006004","622908"],
  "why":"**误报和漏报同样致命。** 一个正常金额被当成卡号，整份报告就被打上不可信警告。"
        "所以用「前后不得紧邻数字」的边界断言，且长度下限 16——报账金额 1374.80 带小数点、"
        "单据编号带字母和横线、金额 3264 只有 4 位，三类都不会命中。正反两个方向都要有单元测试钉住。"},
 {"id":"M03","name":"手机号","applies_to":["C0125","C0801"],
  "rule":"保留前 3 后 4，中间 4 个「*」",
  "regex":"(?<!\d)1[3-9]\d{9}(?!\d)",
  "must_hit":["13800138000"],
  "must_not_hit":["1374.80","13745","2026101308211","100","120","15000000"],
  "why":"11 位且以 1 开头的数字串在金额里也会出现（如 15000000 元），所以必须同时卡「第二位 3~9」与前后边界。"},
 {"id":"M04","name":"工号","applies_to":["C0802"],
  "rule":"保留后 2 位",
  "must_not_hit":["FJPG-BX2610132121"],
  "why":"工号与单据编号都含数字串，单据编号不能被脱——它是演示里逐张单对账的唯一抓手，脱了就没法核对。"},
 {"id":"M06","name":"自由文本里的姓名（摘要/事由/审批意见/补充说明）",
  "applies_to":["C0115","C0116","C1204","C0127"],
  "rule":"用**出差人姓名闭集合**逐个替换（取自提单人/报账人/出差人名单三处的并集），不做通用姓名模式匹配",
  "measured":{"摘要总数":40,"含姓名的摘要":14,"占比":"35%","闭集合人数":15,
              "对 19 个单位名/部门名的误伤":0},
  "must_hit":["报泉州港务集团驻点差旅费（乐知行，9.28-9.29）",
              "付蓝维安6.24闽运平潭公司驻点差旅费",
              "宦允文赴北京参加第二十届中国CFO大会（5.14-5.16）"],
  "must_not_hit":["泉州港务集团","厦门新立基","闽运长乐公交公司","科技集团","财智分公司",
                  "宁德港务集团","漳州港务","莆田港务","平潭港务","厦门轮船公司","厦门海隆",
                  "福清宏源公司","邵武客运公司","金蝶软件","中兴新云","财司协会","港口集团",
                  "运营管理部","总账报表部"],
  "why":("**这是最容易漏掉的那条旁路。** 摘要不是「姓名字段」，所以 M01 这种按字段脱敏的策略"
         "根本不经过它；而摘要恰恰会原文进 AI 审核报告、进大模型请求体、进导出文件。"
         "实测 40 条摘要里 14 条含真实姓名（35%）。"
         "不用通用姓名正则的理由也是实测的：单位名「闽运长乐公交公司」「厦门海隆」都是中文短词，"
         "任何「2~3 个汉字的人名模式」都会把它们打掉。改用闭集合后，19 个单位名/部门名误伤 0 个。"
         "代价是闭集合要随数据更新——这个代价必须付，因为误伤单位名会让整份报告失去可读性。")},
 {"id":"M05","name":"大模型 API Key","applies_to":["llm_config.api_key"],
  "rule":"只回 `sk-****` 形式的掩码，**明文一律不出库表之外**；写入时单向，读取接口永不回明文",
  "must_not_hit":["model_name","base_url","temperature"],
  "why":"大模型配置是旁路出口的典型：报告正文不含 Key，但 /config 接口会原样回。参照项目在这里栽过，这条单列。"},
]
doc["masking"]["test_requirement"] = (
 "每条 policy 的 must_hit 与 must_not_hit 都要落成单元测试，两个方向都测。"
 "must_not_hit 里的值直接取自真实样本（金额 1374.80 / 34.11 / 3264 / 7209、"
 "单据编号 FJPG-BX2610132121、业务小类编码 CL006004），不是编的。")

doc["free_text_leak_paths"] = {
 "note":("自由文本字段是脱敏的旁路出口。它们没有 sensitive 标记，却原文流进报告、"
         "大模型请求体和导出文件。M06 专管这一类。"),
 "fields":[
  {"concept_id":"C0115","field":"报账单.摘要","measured":"40/40 非空，14 条含真实姓名"},
  {"concept_id":"C0116","field":"报账单.补充说明","measured":"来自审批意见 PDF，含出差事由与地点说明"},
  {"concept_id":"C1204","field":"审批记录.审批意见","measured":"50 页 PDF，含退回理由原文"},
  {"concept_id":"C0127","field":"报账单.资金用途","measured":"来自审批意见 PDF"},
 ]}
doc["engine_contract"] = {
 "startup_checks": [
  "每条规则 logic.concepts 引用的概念都存在于本事实模型（不存在 → 启动失败）",
  "每条进入执行集的规则，其所有概念的 availability ∈ {样本可得, 样本可得(审批意见), 已录入资产, 可推导}",
  "规则引用的外部表/名单文件存在且可解析（必要附件决策表 / 敏感词表 / 内部单位名单 / 标准表）",
  "必要附件决策表的键在源表里唯一，且 row_count 合计 == source_rows",
  "枚举字段的取值集合与本事实模型 enums 一致（多一个表单上不存在的取值，规则就会去判一个永远为假的条件）",
  "所有 sensitive 字段都被至少一条 masking policy 覆盖（未覆盖 → 启动失败）",
  "free_text_leak_paths 列出的每个字段都被 M06 覆盖（未覆盖 → 启动失败）",
  "OCR 来源的数值字段不得出现在任何硬规则的阈值位置",
 ],
 "on_check_failure": "Fail Fast：打印全部不合格项后退出，不做部分启动。",
 "excluded_rules_must_be_logged": True,
}
doc["stats"] = {
 "entities": len(ents), "fields": sum(e["field_count"] for e in ents),
 "enums": len(doc["enums"]),
 "sensitive_fields": len(sens),
 "fields_used_by_rules": sum(1 for e in ents for f in e["fields"] if f["used_by_rules"]),
 "fields_unused": sum(1 for e in ents for f in e["fields"] if not f["used_by_rules"]),
 "availability": dict(collections.Counter(f["availability"] for e in ents for f in e["fields"])),
}
io.open(rel("事实模型_v0.1.json"), "w", encoding="utf-8", newline="").write(json.dumps(doc, ensure_ascii=False, indent=1))
s = doc["stats"]
print("事实模型：实体 %d / 字段 %d / 枚举 %d" % (s["entities"], s["fields"], s["enums"]))
print("  敏感字段 %d：%s" % (s["sensitive_fields"], [x["field"] for x in doc["sensitive_fields"]]))
print("  被规则引用 %d / 未被引用 %d" % (s["fields_used_by_rules"], s["fields_unused"]))
print("  脱敏策略 %d 条，覆盖出口 %d 个" % (len(doc["masking"]["policies"]), len(doc["masking"]["outlets"])))
print("  启动时契约校验 %d 项" % len(doc["engine_contract"]["startup_checks"]))
# 自查：每个 sensitive 字段都被策略覆盖
cov = set()
for pol in doc["masking"]["policies"]: cov.update(pol["applies_to"])
missing = [x["concept_id"] for x in doc["sensitive_fields"] if x["concept_id"] not in cov]
print("  未被脱敏策略覆盖的敏感字段: %s" % (missing if missing else "无"))
