# -*- coding: utf-8 -*-
"""本体构建：候选质量 + 改源文件的安全性。

这组测试钉的全是「会让人动错东西」的地方：
  别名没比到      → 报「缺概念」，人照着加，本体里长出重复概念
  候选没出处      → 页面上一个名字，人没法判断该不该信
  预览写了文件    → 「只看看」变成了改源文件
  插坏不回滚      → 本体解析失败，后端下次启动直接拒绝（Fail Fast）
  已在本体可采纳  → 往 YAML 里插一条已经存在的定义
  Agent 能写本体  → 「系统自己悄悄改了本体」这条路就存在了
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, onto_build as ob, onto_yaml as oy  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 一、候选质量
# ─────────────────────────────────────────────────────────────────────────────
def test_比对必须连别名一起比():
    """别名没比到会报出重复概念。

    「报账人公司」是 C0107 核算主体 的**别名**，只有读了 alias 才认得出来——
    这是这条测试真正要钉的东西。
    「费用明细」顺带也验一下：它既是实体 E02 的名字、又是 C0202 费用小类 的别名，
    两条路都能识别出来，所以**不该**成为候选。
    （第一版这条测试断言 费用明细 的 canonical 是「费用小类」，红了——
    实体名先匹配上了。错的是断言，不是代码。）
    """
    names, _ = ob.onto_names()
    assert "报账人公司" in names, "alias 没进库，或者比对没读 alias"
    assert names["报账人公司"]["ref"] == "C0107"
    assert names["报账人公司"]["canonical"] == "核算主体"
    assert "费用明细" in names


def test_流程节点的别名也要比到():
    """「开始」是 N01 提单 的别名，审批记录里出现 103 次。

    不比别名，这 103 次会被报成「本体里找不到这个节点」——假红。
    """
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    aliases = {a for r in db.query(
        "SELECT alias FROM process_node WHERE version=%s", (v,)) for a in (r["alias"] or [])}
    assert "开始" in aliases, "process_node.alias 里没有「开始」：装载脚本又把 alias 丢了"
    cands, _ = ob.process_candidates()
    assert "开始" not in [c["cand_name"] for c in cands], \
        "「开始」被报成候选节点了——别名没比到"


def test_别名不该出现在候选里():
    cands, _ = ob.collect()
    bad = [c["cand_name"] for c in cands
           if c["verdict"] == "建议新增" and c["cand_name"] in ("报账人公司", "费用明细")]
    assert not bad, "把别名报成了缺概念：%s —— 照着加会长出重复概念" % bad


def test_每条候选都有出处():
    cands, _ = ob.collect()
    assert cands, "一条候选都没有，抽取可能坏了"
    for c in cands:
        assert c["evidence"] and c["evidence"].strip(), "%s 没有出处" % c["cand_name"]
        assert c["basis"] and c["basis"].strip(), "%s 没有依据" % c["cand_name"]


def test_读不出不等于建议新增():
    """打不开一份单据是「没查成」，不是「本体里没有」。"""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "onto_build.py"), encoding="utf-8").read()
    assert "读不开" in src and "没查成" in src, \
        "读取失败的分支没了：一次读取失败会变成一条「本体缺概念」的结论"


def test_流程候选的判定分层间不一致与建议新增():
    cands, _ = ob.process_candidates()
    vs = {c["verdict"] for c in cands}
    assert vs <= {"建议新增", "层间不一致", "已在本体"}
    for c in cands:
        if c["verdict"] == "层间不一致" and c["kind"] == "流程节点":
            assert "EN16" in c["basis"], "层间不一致必须说清是哪两层对不上"


def test_迁移两端的解析和节点比对用同一套规则():
    """只用名字+别名时，「开始→部门负责人」这条走过 60 次的边会被报成钉不到。"""
    cands, stats = ob.process_candidates()
    assert stats["迁移对上"] > 0, \
        "一条迁移都没对上：迁移两端的解析很可能没带别名或唯一前缀"


# ─────────────────────────────────────────────────────────────────────────────
# 二、改源文件的安全性
# ─────────────────────────────────────────────────────────────────────────────
def _sha():
    return oy.sha256()


def test_预览一个字都不许写():
    before = _sha()
    oy.preview({"kind": "概念", "cand_name": "测试列头", "yaml_target": "E01",
                "evidence": "单元测试"})
    assert _sha() == before, "preview 改了源文件——「只看看」变成了改文件"


def test_节点编号不许续出三位数():
    """N99 是「流程结束」，9x 段是保留号。算进最大号就会续出 N100。"""
    pv = oy.preview({"kind": "流程节点", "cand_name": "测试节点", "evidence": "单元测试"})
    assert pv["new_id"] and len(pv["new_id"]) == 3, "编号口径破了：%s" % pv["new_id"]
    assert pv["new_id"] != "N99"


def test_已有的枚举取值不许重复插入():
    with pytest.raises(oy.PatchError) as e:
        oy.preview({"kind": "枚举取值", "cand_name": "总经理", "yaml_target": "EN16",
                    "evidence": "单元测试"})
    assert "已经有" in str(e.value)


def test_插坏了必须回滚且文件原样():
    """**这条是整组里最重要的。**

    本体解析失败会让后端下次启动直接拒绝（Fail Fast）。
    所以插入后必须 safe_load 验一遍，不过就从备份回滚。
    这里故意造一个破格式的补丁，验证回滚真的发生、文件真的原样。
    """
    before_sha, before_text = _sha(), io.open(config.ONTOLOGY_YAML, encoding="utf-8").read()
    orig = oy.PATCHERS["概念"]

    def broken(lines, cand):
        return 60, "        - {id: C9999, name: 坏补丁, type: string", None

    oy.PATCHERS["概念"] = broken
    try:
        with pytest.raises(oy.PatchError) as e:
            oy.apply_patch({"kind": "概念", "cand_name": "坏补丁", "yaml_target": "E01",
                            "evidence": "单元测试"})
        assert "回滚" in str(e.value)
    finally:
        oy.PATCHERS["概念"] = orig
    assert _sha() == before_sha, "回滚没把文件恢复原样"
    assert io.open(config.ONTOLOGY_YAML, encoding="utf-8").read() == before_text


def test_每次写之前都要备份():
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "onto_yaml.py"), encoding="utf-8").read()
    i_bak, i_write = src.index("bak = backup()"), src.index('open(config.ONTOLOGY_YAML, "w"')
    assert i_bak < i_write, "备份要在写之前——这个项目没有 git，备份是唯一的退路"


def test_绝不走解析再序列化的路子():
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "onto_yaml.py"), encoding="utf-8").read()
    # 查**调用**（带左括号），不查这个词：模块注释里正解释着「为什么不用 dump」，
    # 按词查会把那段解释判成违规——第一版就这么红了一次。
    assert "yaml.dump(" not in src and "yaml.safe_dump(" not in src, \
        "出现了 yaml.dump 调用：它会把 924 行里的口径注释一次性抹光"
    assert "yaml.dump" in src, \
        "连那段「为什么不许 dump」的说明都没了——下一个人会直接 dump 一遍"


# ─────────────────────────────────────────────────────────────────────────────
# 三、采纳的闸
# ─────────────────────────────────────────────────────────────────────────────
def test_已在本体的候选不许采纳():
    c = db.query_one("SELECT cand_id FROM onto_candidate WHERE verdict='已在本体' LIMIT 1")
    if not c:
        pytest.skip("当前没有「已在本体」的候选")
    with pytest.raises(ob.BuildError) as e:
        ob.adopt(c["cand_id"], "测试", None)
    assert "已在本体" in str(e.value)


def test_采纳必须署名():
    c = db.query_one("SELECT cand_id FROM onto_candidate WHERE verdict='建议新增' LIMIT 1")
    if not c:
        pytest.skip("当前没有「建议新增」的候选")
    with pytest.raises(ob.BuildError) as e:
        ob.adopt(c["cand_id"], "  ", None)
    assert "署名" in str(e.value)


def test_驳回暂缓必须写理由():
    c = db.query_one("SELECT cand_id FROM onto_candidate LIMIT 1")
    if not c:
        pytest.skip("没有候选")
    with pytest.raises(ob.BuildError) as e:
        ob.dispose(c["cand_id"], "驳回", "测试", "")
    assert "理由" in str(e.value)


# ─────────────────────────────────────────────────────────────────────────────
# 四、Agent 的能力边界
# ─────────────────────────────────────────────────────────────────────────────
def test_agent写不进本体也写不进处置台账():
    from app.runtime import yaml_loader
    assert "onto_candidate" in yaml_loader.WRITABLE_TABLES
    for t in ("ontology_entity", "ontology_concept", "ontology_enum",
              "ontology_enum_value", "ontology_relation", "process_node",
              "process_transition", "onto_cand_action"):
        assert t not in yaml_loader.WRITABLE_TABLES, \
            "%s 进了白名单：系统就有了一条自己改本体的路" % t


def test_四个agent的契约都在且有执行体():
    from app import onto_build_orchestrator as obo
    from app.runtime import yaml_loader
    r = yaml_loader.load(verbose=False)
    for a in obo.AGENTS:
        assert a["id"] in r.agents, "%s 在编排图里但没有 YAML 契约" % a["id"]
        assert a["id"] in obo.RUNNERS, "%s 没有执行体" % a["id"]
    assert all(a["kind"] == "确定性" for a in obo.AGENTS), \
        "这条链应当全是确定性 Agent：口径对不对是能复核的硬判断"


def test_每个agent的forbidden都写明不许碰源文件():
    from app.runtime import yaml_loader
    r = yaml_loader.load(verbose=False)
    for aid in ("fj_l0_04_material_perception", "fj_l1_04_field_candidate",
                "fj_l1_05_process_candidate", "fj_l2_03_onto_cand_align"):
        fb = " ".join(str(x) for x in (r.agents[aid].improve.get("forbidden") or []))
        assert "本体" in fb and ("yaml" in fb.lower() or "源文件" in fb), \
            "%s 的 forbidden 没写明不许碰本体源文件" % aid


# ─────────────────────────────────────────────────────────────────────────────
# 五、存储说明里的数不许被误读
# ─────────────────────────────────────────────────────────────────────────────
def test_存储行数按当前版本算():
    """直接 count(*) 会得到 34 个实体、240 个概念（库里还有旧版 v0.1-draft），
    而页面上那个数会被当成「本体有多大」来读。"""
    st = ob.biz_storage()
    by = {r["table_name"]: r for r in st["items"]}
    ent = by["ontology_entity"]
    assert ent["rows"] == 17, "当前版实体数不是 17：%s" % ent["rows"]
    if ent["rows_all_versions"] and ent["rows_all_versions"] > ent["rows"]:
        assert ent["stale_versions"] > 0, "旧版残留没算出来，页面上会看不见它"


def test_存储第一行是源文件():
    st = ob.biz_storage()
    assert st["items"][0]["table_name"].endswith(".yaml"), \
        "存储清单第一行应当是源文件——本体的真源头是那份 YAML，不是库里的表"
