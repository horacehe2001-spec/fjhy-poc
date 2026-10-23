# -*- coding: utf-8 -*-
"""本体源文件（本体_v0.1.yaml）的「摆在明面上」那一层：看得见、能定点改、能重装。

这个模块存在的理由是一句用户的话：**所有东西都摆在明面上操作**。
在它之前，本体这一步全在命令行和对话里：
  · 924 行的 YAML 是我读客户材料**手写**的（设计/进度.md 批次 1 有记录）
  · 装进库要在命令行跑 `python -m app.ontology.loader`
  · 页面上既看不到源文件，也没有任何入口
于是客户没法自己再跑一遍，也查不到任何证据链。

**三条口径，一条都不许破：**

一、**YAML 是源，库是投影，单向。** 改本体就改 YAML，然后重装。
   库里改一行不会回到 YAML——那样库和源会静默分叉，而「本体未漂移」
   那道启动自检比的是 YAML 文件的 sha256，不比行数，这种分叉它恰好查不出来。

二、**改 YAML 只许定点按行插入，绝不许 yaml.load → yaml.dump。**
   dump 会把 924 行里的注释一次性抹光，而注释里是口径纪律和踩坑记录
   （「OCR 的数字一律视为未经确认」「没出处的概念不许进本体」这类）。
   这种损失在 code review 里极难发现：文件还在、结构还对、信息没了。

三、**插完先 safe_load 验一遍，解析不过立刻从备份回滚。**
   按行插入必然有破格式的风险，而本体解析失败会让整个后端拒绝启动
   （Fail Fast）。不验就写，等于把「系统起不来」的风险留给下一次重启。
   实测这份文件有过 5 处语法错、从来没解析成功过的历史，所以这一条不是假想。
"""
import datetime
import hashlib
import io
import os
import re
import shutil

import yaml

from . import config, db

BACKUP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var", "onto_yaml_backup")


class PatchError(RuntimeError):
    pass


# ── 源文件：摆出来给人看 ────────────────────────────────────────────────────
def _lines():
    with io.open(config.ONTOLOGY_YAML, encoding="utf-8") as f:
        return f.read().split("\n")


def sha256():
    return hashlib.sha256(io.open(config.ONTOLOGY_YAML, "rb").read()).hexdigest()


SECTION_RE = re.compile(r"^(meta|business|rule_schema|glossary|process):\s*$")
SUB_RE = re.compile(r"^  (entities|enums|relations|nodes|bypasses|transitions):\s*$")


def sections():
    """源文件的段落索引（名字 + 起始行）。页面上点一下就能跳到那一段。"""
    out = []
    for i, ln in enumerate(_lines(), 1):
        m = SECTION_RE.match(ln) or SUB_RE.match(ln)
        if m:
            # section_name，不是 name：实测段落名被脱成了 m***、b*******
            out.append({"section_name": m.group(1), "line": i,
                        "level": 1 if SECTION_RE.match(ln) else 2})
    return out


def source_info():
    """源文件的全部可见信息。**包括「谁写的」——这一条最容易被含糊过去。**"""
    lines = _lines()
    raw = yaml.safe_load("\n".join(lines))
    meta = raw.get("meta") or {}
    v = db.query_one("""SELECT version, source_hash, loaded_at, stats
                          FROM ontology_version WHERE is_current""")
    h = sha256()
    return {
        "path": os.path.basename(config.ONTOLOGY_YAML),
        "abs_path": config.ONTOLOGY_YAML,
        "lines": len(lines),
        "bytes": os.path.getsize(config.ONTOLOGY_YAML),
        "sha256": h,
        "version": str(meta.get("version") or ""),
        "maturity": meta.get("maturity"),
        "date": str(meta.get("date") or ""),
        "sections": sections(),
        "counts": {"实体": len(raw["business"]["entities"]),
                   "概念": sum(len(e.get("concepts") or [])
                             for e in raw["business"]["entities"]),
                   "枚举": len(raw["business"]["enums"]),
                   "枚举取值": sum(len(e.get("values") or [])
                               for e in raw["business"]["enums"]),
                   "实体关系": len(raw["business"].get("relations") or []),
                   "流程节点": len(raw["process"]["nodes"])
                           + len(raw["process"].get("bypasses") or []),
                   "流程迁移": len(raw["process"].get("transitions") or [])},
        "written_by": "人工（AI 助手读客户材料后手写）",
        "written_how": "读 材料\\ 下 14 份客户文件后逐行写成。"
                       "**不是本系统的智能体协同产出**：没有可复跑的链路，"
                       "没有 agent_signal_log / cycle_log 痕迹，"
                       "每个概念的 source 字段是人工标注的，不是程序从素材里定位的。"
                       "「本体构建」那条协同链补的正是这一块，但它产出的是候选，"
                       "实体怎么划、概念的语义归属，仍然是人读制度做的判断。",
        "derived": [
            {"file": "事实模型_v0.1.json", "by": "规则资产/gen/gen_fact.py",
             "note": "引擎输入契约。脚本从本体 YAML 生成，**不手写**——手写必然与本体漂移"},
            {"file": "规则绑定_v0.1.json", "by": "规则资产/gen/gen_bind.py",
             "note": "概念可得性与规则反向索引，由本体 YAML + 规则清单生成"},
        ],
        "loaded": {"version": v["version"] if v else None,
                   "source_hash": v["source_hash"] if v else None,
                   "loaded_at": v["loaded_at"] if v else None,
                   "stats": v["stats"] if v else None,
                   "in_sync": bool(v and v["source_hash"] == h)} if True else None,
        "in_sync_note": "库里记的 source_hash 与当前文件指纹一致才算没漂移。"
                        "不一致说明**文件改了但没重装**，此时页面上看到的本体是旧的。",
    }


def read_section(name, max_lines=400):
    """把某一段的原文摘出来给人看。**原文照抄，含注释**——
    注释才是这份文件最贵的部分，摘要掉等于没给。"""
    lines = _lines()
    secs = sections()
    start = None
    for i, s in enumerate(secs):
        if s["section_name"] == name:
            start = s["line"]
            end = secs[i + 1]["line"] - 1 if i + 1 < len(secs) else len(lines)
            break
    if start is None:
        raise PatchError("源文件里没有这一段：%s" % name)
    body = lines[start - 1:min(end, start - 1 + max_lines)]
    return {"section_name": name, "line_start": start, "line_end": end,
            "truncated": end - start + 1 > max_lines, "text": "\n".join(body)}


# ── 备份 ────────────────────────────────────────────────────────────────────
def backup():
    """定点插入前先整份备份。**这个项目没有 git**，备份就是唯一的退路。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, "%s.%s.bak" % (
        os.path.basename(config.ONTOLOGY_YAML), ts))
    shutil.copy2(config.ONTOLOGY_YAML, dst)
    return dst


# ── 定点插入 ────────────────────────────────────────────────────────────────
def _entity_block(lines, entity_id):
    """返回某实体在源文件里的 (起始行, 结束行)，1 基，含。"""
    start = None
    for i, ln in enumerate(lines, 1):
        if re.match(r"^    - id: %s\s*$" % re.escape(entity_id), ln):
            start = i
        elif start and re.match(r"^    - id: ", ln):
            return start, i - 1
        elif start and re.match(r"^  \w+:\s*$", ln):      # 下一个二级段
            return start, i - 1
    if start:
        return start, len(lines)
    raise PatchError("源文件里找不到实体 %s" % entity_id)


def _next_concept_id(lines, entity_id):
    """沿用现有编号口径 C{实体号}{序号}，取该实体现有最大号 +1。"""
    s, e = _entity_block(lines, entity_id)
    no = re.sub(r"^E", "", entity_id)
    mx = 0
    for ln in lines[s - 1:e]:
        for m in re.finditer(r"\bid:\s*C%s(\d{2})\b" % no, ln):
            mx = max(mx, int(m.group(1)))
    if not mx:
        raise PatchError("实体 %s 下没有现成概念编号可续，采纳前需人工确认编号口径" % entity_id)
    return "C%s%02d" % (no, mx + 1)


def _concept_patch(lines, cand):
    """概念：在目标实体 concepts 列表末尾加一行，**跟原文一个风格**
    （8 空格缩进的单行 flow map）。"""
    ent = cand.get("yaml_target")
    if not ent:
        raise PatchError("这条候选没有确定目标实体，采纳时必须人选——"
                         "挂错实体会让这个概念的语义归属整个错位")
    s, e = _entity_block(lines, ent)
    last = None
    for i in range(s - 1, e):
        if re.match(r"^        - \{", lines[i]) or re.match(r"^           ", lines[i]):
            last = i + 1
    if last is None:
        raise PatchError("实体 %s 下找不到 concepts 列表" % ent)
    cid = _next_concept_id(lines, ent)
    ev = (cand.get("evidence") or "").replace("\n", " ")[:90]
    patch = ("        - {id: %s, name: %s, type: string, source: 单据样本,\n"
             "           note: 由「本体构建」协同采纳。证据：%s。"
             "类型与语义待业务侧确认}" % (cid, cand["cand_name"], ev))
    return last, patch, cid


def _enum_patch(lines, cand):
    """枚举取值：定点改 `values: [...]` 那一行，在 ] 前插入。

    **改的是那一行，不是整段**：这一行上下都是注释（EN02 的注释写着
    「枚举值只收材料里真出现的，凭常识补全的状态会让前端做出
    客户系统里不存在的筛选项」），整段重写会把它们抹掉。
    """
    eid = (cand.get("yaml_target") or "").strip()
    if not re.match(r"^EN\d+$", eid):
        raise PatchError("枚举取值候选必须指明枚举号（ENxx）")
    start = None
    for i, ln in enumerate(lines, 1):
        if re.match(r"^    - id: %s\s*$" % eid, ln):
            start = i
        elif start and re.match(r"^      values:\s*\[", lines[i - 1]):
            row = lines[i - 1]
            if "]" not in row:
                raise PatchError("%s 的 values 不是单行写法，需人工处理" % eid)
            val = cand["cand_name"]
            if val in row:
                raise PatchError("%s 里已经有「%s」了" % (eid, val))
            return i, row.replace("]", ", %s]" % val), None
        elif start and re.match(r"^    - id: ", ln) and i > start:
            break
    raise PatchError("源文件里找不到 %s 的 values 行" % eid)


def _node_patch(lines, cand):
    """流程节点：在 nodes 段末尾加一个块，风格照抄 N02（最简的那种）。"""
    start = end = None
    for i, ln in enumerate(lines, 1):
        if re.match(r"^  nodes:\s*$", ln):
            start = i
        elif start and re.match(r"^  \w+:\s*$", ln):
            end = i - 1
            break
    if start is None:
        raise PatchError("源文件里找不到 process.nodes 段")
    end = end or len(lines)
    # **9x 段是终止态的保留号**（N99 = 流程结束），不能算进「最大号」：
    # 算进去就会续出 N100，既破了两位数的编号口径，又把新节点排到了
    # 「流程结束」之后。实测第一版就产出了 N100。
    nos = [int(m.group(1)) for ln in lines[start:end]
           for m in [re.match(r"^    - id: N(\d+)", ln)] if m]
    normal = [n for n in nos if n < 90]
    nid = "N%02d" % ((max(normal) + 1) if normal else 1)
    if nid in ("N%02d" % n for n in nos):
        raise PatchError("算出来的节点号 %s 已被占用，编号口径需人工确认" % nid)
    ev = (cand.get("evidence") or "").replace("\n", " ")[:90]
    patch = ("    - id: %s\n      name: %s\n      alias: []\n"
             "      note: |\n        由「本体构建」协同采纳。证据：%s。\n"
             "        **这个节点的 actor 与前后迁移待业务侧确认**——"
             "迁移没补全时它在流程图上是个孤点。" % (nid, cand["cand_name"], ev))
    return end, patch, nid


PATCHERS = {"概念": _concept_patch, "枚举取值": _enum_patch, "流程节点": _node_patch}


def preview(cand):
    """只算不写：给出将要插入的行号、补丁原文和上下文。

    **采纳是改用户手写的源文件，必须先给人看清要插什么。**
    """
    kind = cand["kind"]
    if kind not in PATCHERS:
        raise PatchError("这类候选（%s）还不支持自动插入，需人工编辑 YAML。"
                         "不支持比插错强：插错格式会让本体解析失败、后端拒绝启动" % kind)
    lines = _lines()
    at, patch, new_id = PATCHERS[kind](lines, cand)
    replace = kind == "枚举取值"
    return {"kind": kind, "line": at, "patch": patch, "replace_line": replace,
            "new_id": new_id,
            "context_before": "\n".join(lines[max(0, at - 4):at]),
            "context_after": "\n".join(lines[at:at + 3]),
            "note": ("定点改第 %d 行（在 ] 前插入一个取值）" % at) if replace
                    else ("在第 %d 行之后插入 %d 行" % (at, patch.count("\n") + 1))}


def apply_patch(cand):
    """真写。顺序是**备份 → 插入 → safe_load 验 → 不过就回滚**。"""
    pv = preview(cand)
    bak = backup()
    lines = _lines()
    if pv["replace_line"]:
        lines[pv["line"] - 1] = pv["patch"]
    else:
        lines[pv["line"]:pv["line"]] = pv["patch"].split("\n")
    text = "\n".join(lines)
    with io.open(config.ONTOLOGY_YAML, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        raw = yaml.safe_load(text)
        if not raw or "business" not in raw or "process" not in raw:
            raise PatchError("插入后结构不完整")
    except Exception as e:                                     # noqa: BLE001
        shutil.copy2(bak, config.ONTOLOGY_YAML)
        raise PatchError("插入后 YAML 解析不过，已从备份回滚（%s）：%s"
                         % (os.path.basename(bak), e))
    return {"line": pv["line"], "patch": pv["patch"], "backup": bak,
            "new_id": pv["new_id"], "sha256": sha256()}


# ── 重装 ────────────────────────────────────────────────────────────────────
def reload(check_only=False):
    """把命令行的 `python -m app.ontology.loader` 搬到明面上。

    check_only 对应 `--check`：只比对指纹，不写库。
    **两个入口分开**：重装会 DELETE 掉同版本的全部投影再重建，
    和「只看看有没有漂移」完全是两件事，合成一个按钮迟早有人误点。
    """
    from .ontology import loader as onto_loader
    h = sha256()
    cur = db.query_one("""SELECT version, source_hash, loaded_at
                            FROM ontology_version WHERE is_current""")
    if check_only:
        return {"checked": True, "file_sha256": h,
                "db_source_hash": cur["source_hash"] if cur else None,
                "in_sync": bool(cur and cur["source_hash"] == h),
                "note": "只比对，没写库。不一致说明文件改了但没重装。"}
    stats = onto_loader.load(verbose=False)
    after = db.query_one("""SELECT version, source_hash, loaded_at, stats
                             FROM ontology_version WHERE is_current""")
    return {"checked": False, "file_sha256": h, "loaded": True,
            "version": after["version"] if after else None,
            "db_source_hash": after["source_hash"] if after else None,
            "in_sync": bool(after and after["source_hash"] == h),
            "stats": stats if isinstance(stats, dict) else (after or {}).get("stats"),
            "note": "同版本重装：先清掉旧投影再重建，避免「YAML 删了一个概念"
                    "而库里还留着」。"}
