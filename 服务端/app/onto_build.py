# -*- coding: utf-8 -*-
"""业务/流程本体的「建立」：从素材里抽候选，人采纳才进本体。

这条链补的是一个被藏起来的步骤。在它之前：
  本体骨架（17 实体 / 121 概念 / 21 枚举 / 116 取值 / 18 关系 / 9 节点 / 12 迁移）
  是**人读客户材料手写进 本体_v0.1.yaml** 的，再用命令行装进库。
  页面上看不到来源、没有入口、客户也没法自己再跑一遍。

**这条链能做什么、不能做什么，必须说清楚，不然就是夸大：**
  能：从素材里**数出来**有硬证据的东西——单据列头 39 个、枚举取值的真实使用
      次数、审批记录 368 条里的节点与迁移。每条候选都带「出自哪、出现几次」。
  不能：替代人读制度划实体、定概念语义与 source 归属。那是判断，不是统计。
  所以产出叫**候选**，采纳是人的动作，而且采纳后改的是 YAML（源），不是库（投影）。

**候选表只收可操作的那些。** 116 个枚举取值全塞进去，人看两眼就不看了——
「建议新增」「层间不一致」「样本与制度都没出现过」才进表，
对上的只进计数。噪声压不住的清单等于没有清单。
"""
import hashlib
import io
import re
import uuid

from . import config, db, onto_yaml

CLAUSE_SOURCE = "条款印证"


def _cid(kind, key):
    return "cand-" + hashlib.sha1(("%s|%s" % (kind, key)).encode("utf-8")).hexdigest()[:14]


# ── 本体里已有的名字（含别名）──────────────────────────────────────────────
def onto_names():
    """名字/别名 → 本体元素。**从库里取**：alias 进库之后（0024）才敢这么做。

    之前 alias 没进库，比对列头时「报账人公司」被报成缺概念，
    而它是 C0107 核算主体 的别名——照着去新增就长出一条重复概念。
    """
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    out = {}
    for r in db.query("""SELECT entity_id AS ref, name, alias, '实体' AS kind
                           FROM ontology_entity WHERE version = %s""", (v,)):
        for n in [r["name"]] + list(r["alias"] or []):
            out[n] = {"kind": "实体", "ref": r["ref"], "canonical": r["name"]}
    for r in db.query("""SELECT concept_id AS ref, entity_id, name, alias
                           FROM ontology_concept WHERE version = %s""", (v,)):
        for n in [r["name"]] + list(r["alias"] or []):
            out.setdefault(n, {"kind": "概念", "ref": r["ref"],
                               "canonical": r["name"], "entity": r["entity_id"]})
    return out, v


def entity_of_sheet(sheet, names):
    """工作表 → 实体，**按最长名字匹配，判不出就不猜**。

    挂错实体会让这个概念的语义归属整个错位，而页面上照样显示「有出处」。
    实测 6 个工作表全部唯一命中：1差旅费报账单→E01、1费用明细→E02、
    1报销附件→E13（「报销附件」含「附件」）、多人三张同理。
    """
    s = re.sub(r"^\d+|多人|差旅|[-－]", "", sheet or "")
    hits = [(n, v) for n, v in names.items() if v["kind"] == "实体" and n in (sheet or "")]
    if not hits:
        hits = [(n, v) for n, v in names.items() if v["kind"] == "实体" and n in s]
    if not hits:
        return None
    hits.sort(key=lambda kv: -len(kv[0]))
    if len(hits) > 1 and len(hits[0][0]) == len(hits[1][0]):
        return None                      # 并列第一，判不出
    return hits[0][1]["ref"]


# ── 候选一：单据列头 → 概念 ────────────────────────────────────────────────
SKIP_HEADERS = ("", "序号")


def bill_sheets():
    """文件库里登记的单据明细表。**从库里取路径**，不写死目录——
    文件库那一栏才是「材料在哪」的唯一来源。"""
    return db.query("""SELECT file_id, file_name, abs_path, rel_path
                         FROM doc_lib_file
                        WHERE ext = '.xlsx' AND NOT missing
                          AND rel_path LIKE %s
                        ORDER BY file_name""", ("%单据明细表%",))


def header_candidates():
    """→ (候选, 统计)。列头在本体里没有对应概念（含别名）的，就是候选。"""
    try:
        import openpyxl
    except ImportError:                                        # noqa: BLE001
        return [], {"列头": 0, "对上": 0, "候选": 0, "说明": "openpyxl 未安装，跳过列头比对"}
    names, _ = onto_names()
    cands, total, hit = [], 0, 0
    for f in bill_sheets():
        try:
            wb = openpyxl.load_workbook(f["abs_path"], read_only=True, data_only=True)
        except Exception as e:                                 # noqa: BLE001
            cands.append({"kind": "概念", "cand_key": "读不开:" + f["file_name"],
                          "cand_name": f["file_name"], "source_kind": "单据列头",
                          "evidence": "打不开这份单据：%s" % e, "occurrences": None,
                          "verdict": "建议新增", "existing_ref": None,
                          "basis": "文件读不开，列头无法比对——这不是「本体没有」，"
                                   "是**没查成**，两者必须分开",
                          "yaml_target": None})
            continue
        for ws in wb.worksheets:
            try:
                hdr = next(ws.iter_rows(max_row=1, values_only=True))
            except StopIteration:
                continue
            ent = entity_of_sheet(ws.title, names)
            for i, h in enumerate(hdr, 1):
                if h is None or str(h).strip() in SKIP_HEADERS:
                    continue
                h = str(h).strip()
                total += 1
                where = "%s / 工作表「%s」第 %d 列" % (f["file_name"], ws.title, i)
                if h in names:
                    hit += 1
                    continue
                cands.append({
                    "kind": "概念", "cand_key": "列头:" + h, "cand_name": h,
                    "source_kind": "单据列头", "evidence": where, "occurrences": None,
                    "verdict": "建议新增", "existing_ref": None,
                    "basis": "这一列在本体里没有对应概念，**连别名一起比过了**。"
                             "采纳会在实体 %s 下新增一个概念" % (ent or "（待人选）"),
                    "yaml_target": ent})
        wb.close()
    return cands, {"列头": total, "对上": hit, "候选": len(cands)}


# ── 候选二：枚举取值（真实取值 + 制度印证）─────────────────────────────────
def enum_bindings():
    """(表, 枚举号列, 取值列) —— **从复合外键里推出来，不手写映射表**。

    库里 24 条外键都是 (xxx_enum, xxx) → enum_value_current(enum_id, value)
    这个形状，所以「哪一列是哪个枚举」是能查出来的事实，不需要我维护一份清单
    （维护一份就一定会和库里不一致）。
    """
    rows = db.query("""
        SELECT t.relname AS tbl, pg_get_constraintdef(k.oid) AS def
          FROM pg_constraint k
          JOIN pg_class t ON t.oid = k.conrelid
          JOIN pg_class ft ON ft.oid = k.confrelid
         WHERE k.contype = 'f' AND ft.relname = 'enum_value_current'""")
    out = []
    for r in rows:
        m = re.search(r"FOREIGN KEY \((\w+), (\w+)\)", r["def"])
        if m:
            out.append((r["tbl"], m.group(1), m.group(2)))
    return out


def enum_candidates():
    """→ (候选, 统计)。两类进表：
       · 真实数据里出现、枚举里没有 → 建议新增
       · 枚举里有，而样本与制度**都**没出现过 → 可疑（可能是凭常识补的）
    """
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    onto = {}
    for r in db.query("""SELECT enum_id, value FROM ontology_enum_value
                          WHERE version = %s""", (v,)):
        onto.setdefault(r["enum_id"], set()).add(r["value"])
    enum_name = {r["enum_id"]: r["name"] for r in
                 db.query("SELECT enum_id, name FROM ontology_enum WHERE version=%s", (v,))}

    used = {}
    for tbl, ecol, vcol in enum_bindings():
        for r in db.query("SELECT %s AS e, %s AS v, count(*) AS n FROM %s "
                          "WHERE %s IS NOT NULL GROUP BY 1,2"
                          % (ecol, vcol, tbl, vcol)):
            if not r["e"]:
                continue
            key = (r["e"], r["v"])
            used[key] = used.get(key, 0) + int(r["n"])

    # 制度印证：枚举取值在**整份文件正文**里出现几次（含 OCR 文本）。
    # 一度只在 185 条「条款」里找，结果 32 个取值报「制度里 0 次」——
    # 而那些取值（福州港务、增值税普通发票、海运行业…）的出处在直管企业名单、
    # 发票类型表这些**没被切成条款的段落**里，甚至正落在那几页 OCR 读不出的表格页上。
    # **在比对范围之外不等于不存在**：按前一种算法报出来的「可疑」，
    # 大半是我没查全，而页面上会显示成数据有问题。
    blob = "\n".join(r["body"] or "" for r in
                     db.query("SELECT body FROM doc_lib_file WHERE NOT missing"))
    ocr_gap = db.scalar("SELECT coalesce(sum(ocr_table_pages), 0) FROM doc_lib_file")

    cands = []
    for eid, vals in onto.items():
        for val in vals:
            n_data = used.get((eid, val), 0)
            n_doc = blob.count(val) if val else 0
            if n_data or n_doc:
                continue
            cands.append({
                "kind": "枚举取值", "cand_key": "%s|%s" % (eid, val), "cand_name": val,
                "source_kind": "真实取值", "evidence": "样本数据 0 次 · 制度条款 0 次",
                "occurrences": 0, "verdict": "已在本体", "existing_ref": eid,
                "basis": "枚举 %s（%s）里有这个取值，而样本数据与 14 份材料的正文里"
                         "**都没检索到**。三种可能，方向要人判：样本没覆盖到／"
                         "出处落在那 %d 页 OCR 读不出的表格里／当初凭常识补的。"
                         "只有第三种该删——而它会让前端做出客户系统里不存在的筛选项"
                         % (eid, enum_name.get(eid, ""), ocr_gap),
                "yaml_target": eid})
    for (eid, val), n in used.items():
        if val not in onto.get(eid, set()):
            cands.append({
                "kind": "枚举取值", "cand_key": "%s|%s" % (eid, val), "cand_name": val,
                "source_kind": "真实取值", "evidence": "真实数据里出现 %d 次" % n,
                "occurrences": n, "verdict": "建议新增", "existing_ref": None,
                "basis": "数据里真实出现，而枚举 %s 里没有这个取值" % eid,
                "yaml_target": eid})
    return cands, {"枚举": len(onto), "枚举取值": sum(len(s) for s in onto.values()),
                   "数据里用到": len(used), "候选": len(cands)}


# ── 候选三：审批记录 → 流程节点与迁移 ──────────────────────────────────────
def _why_unresolved(name, nodes, enum_vals):
    """这个名字为什么钉不到节点。**三种原因对应三种不同的下一步动作。**"""
    pre = [n["name"] for n in nodes if n["name"].startswith(name)]
    if len(pre) > 1:
        return "本体里有 %d 个节点名以它开头（%s），判不出是哪一个，系统不替你选" \
               % (len(pre), "、".join(pre))
    if name in enum_vals:
        return "EN16 枚举里有这个取值，而节点层没有对应节点（层间不一致）"
    return "本体的节点与枚举里都没有这个名字"


def process_candidates():
    """→ (候选, 统计)。节点按**名字/别名/前缀**三种方式比对，判不出就标出来。"""
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    nodes = db.query("""SELECT node_id, name, alias FROM process_node
                         WHERE version = %s""", (v,))
    by_name = {n["name"]: n for n in nodes}
    by_alias = {a: n for n in nodes for a in (n["alias"] or [])}
    enum_vals = {r["value"] for r in db.query(
        "SELECT value FROM ontology_enum_value WHERE enum_id='EN16' AND version=%s", (v,))}

    measured = db.query("""SELECT node, count(*) n, count(DISTINCT bill_no) bills
                             FROM approval_record GROUP BY 1 ORDER BY 2 DESC""")
    cands, matched = [], 0
    for m in measured:
        name = m["node"]
        hit = by_name.get(name) or by_alias.get(name)
        how = "精确" if name in by_name else ("别名" if name in by_alias else None)
        if not hit:
            pre = [n for n in nodes if n["name"].startswith(name)]
            if len(pre) == 1:
                hit, how = pre[0], "前缀"
            elif len(pre) > 1:
                how = "多个候选"
        if hit:
            matched += 1
            continue
        in_enum = name in enum_vals
        cands.append({
            "kind": "流程节点", "cand_key": "节点:" + name, "cand_name": name,
            "source_kind": "审批记录",
            "evidence": "审批记录出现 %d 次，涉及 %d 张单" % (m["n"], m["bills"]),
            "occurrences": m["n"],
            "verdict": "层间不一致" if in_enum else "建议新增",
            "existing_ref": "EN16|" + name if in_enum else None,
            "basis": ("EN16「审批节点」枚举里**有**这个取值，而 process_node 这一层"
                      "没有对应节点——**本体内部两层口径不一致**。"
                      "该补节点层、还是枚举层当初写多了，方向要人判"
                      if in_enum else
                      "审批记录里真实出现，而本体的节点与枚举里都没有")
                     + ("。注意：本体里有 %d 个节点名以它开头，**判不出是哪一个**"
                        % len([n for n in nodes if n["name"].startswith(name)])
                        if how == "多个候选" else ""),
            "yaml_target": None})

    # 迁移：审批记录里相邻两步构成一条实际走过的边
    seq = {}
    for r in db.query("""SELECT bill_no, node, seq FROM approval_record
                          ORDER BY bill_no, seq"""):
        seq.setdefault(r["bill_no"], []).append(r["node"])
    pairs = {}
    for steps in seq.values():
        for a, b in zip(steps, steps[1:]):
            if a != b:
                pairs[(a, b)] = pairs.get((a, b), 0) + 1
    # 迁移两端的解析必须和节点比对**用同一套规则**：名字、别名、唯一前缀。
    # 只用名字+别名时，「部门负责人」（本体名是「部门负责人审批」）钉不到节点，
    # 于是「开始→部门负责人」这条实际走过 60 次的边被报成「有一端钉不到」——
    # **假的**。同一件事两处用两套规则，必然在其中一处报假账。
    resolve = {}
    for n in nodes:
        resolve[n["name"]] = n["node_id"]
        for a in (n["alias"] or []):
            resolve[a] = n["node_id"]
    for name in {a for a, _ in pairs} | {b for _, b in pairs}:
        if name in resolve:
            continue
        pre = [n for n in nodes if n["name"].startswith(name)]
        if len(pre) == 1:                 # 唯一前缀才算钉上；多个候选不猜
            resolve[name] = pre[0]["node_id"]
    have = {(t["from_node"], t["to_node"]) for t in db.query(
        "SELECT from_node, to_node FROM process_transition WHERE version=%s", (v,))}
    n_trans_hit = 0
    for (a, b), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        fa, fb = resolve.get(a), resolve.get(b)
        if fa and fb and (fa, fb) in have:
            n_trans_hit += 1
            continue
        cands.append({
            "kind": "流程迁移", "cand_key": "迁移:%s→%s" % (a, b),
            "cand_name": "%s → %s" % (a, b), "source_kind": "审批记录",
            "evidence": "审批记录里实际走过 %d 次" % n, "occurrences": n,
            "verdict": "建议新增" if (fa and fb) else "层间不一致",
            "existing_ref": ("%s→%s" % (fa, fb)) if (fa and fb) else None,
            # 钉不到时要说清**是哪一端、为什么**。只写「有一端钉不到」，
            # 16 条边长得一模一样，人没法判断它们其实都源自同几个节点问题。
            "basis": ("两端都能钉到本体节点（%s→%s），但本体的迁移表里没有这条边"
                      % (fa, fb)) if (fa and fb) else
                     ("钉不到的是「%s」：%s。**先把节点补齐再谈这条边**——"
                      "这条边本身不一定有问题"
                      % ("、".join(x for x, f in ((a, fa), (b, fb)) if not f),
                         "、".join(_why_unresolved(x, nodes, enum_vals)
                                  for x, f in ((a, fa), (b, fb)) if not f))),
            "yaml_target": None})
    return cands, {"实测节点名": len(measured), "节点对上": matched,
                   "实测迁移": len(pairs), "迁移对上": n_trans_hit,
                   "候选": len([c for c in cands])}


# ── 汇总、落库 ──────────────────────────────────────────────────────────────
def collect():
    h, s1 = header_candidates()
    e, s2 = enum_candidates()
    p, s3 = process_candidates()
    return h + e + p, {"列头": s1, "枚举": s2, "流程": s3}


def save(cands):
    """**整表重算**：候选是算出来的。人的动作在 onto_cand_action，重算不动它。

    **整段占住 onto_candidate。** 这个函数是「工场一键搭建」和
    「本体建设运行」共用的同一个函数对象，而两条链各有自己的作业锁、
    互不阻塞。两边同时跑的话，A 删完插到一半 B 又删一次，
    最终台账是两次运行的混合，而下面的 ON CONFLICT DO NOTHING
    会把冲突吞掉——不报错。闸放这里，命令行入口也绕不过。
    """
    with db.table_lock("onto_candidate", who="候选重算 onto_build.save"):
        return _save_locked(cands)


def _save_locked(cands):
    db.execute("DELETE FROM onto_candidate")
    for c in cands:
        db.execute("""INSERT INTO onto_candidate(cand_id, kind, cand_key, cand_name,
                          source_kind, evidence, occurrences, verdict, existing_ref,
                          basis, yaml_target)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (cand_id) DO NOTHING""",
                   (_cid(c["kind"], c["cand_key"]), c["kind"], c["cand_key"],
                    c["cand_name"], c["source_kind"], c["evidence"], c.get("occurrences"),
                    c["verdict"], c.get("existing_ref"), c["basis"], c.get("yaml_target")))
    return len(cands)


def list_candidates(kind=None, verdict=None, limit=300):
    sql = ["""SELECT c.*, (SELECT a.action FROM onto_cand_action a
                            WHERE a.cand_id = c.cand_id
                            ORDER BY a.acted_at DESC LIMIT 1) AS last_action,
                     (SELECT a.yaml_line FROM onto_cand_action a
                       WHERE a.cand_id = c.cand_id AND a.action = '采纳' LIMIT 1) AS yaml_line
                FROM onto_candidate c WHERE TRUE"""]
    args = []
    if kind:
        sql.append("AND c.kind = %s")
        args.append(kind)
    if verdict:
        sql.append("AND c.verdict = %s")
        args.append(verdict)
    sql.append("ORDER BY c.verdict, c.kind, coalesce(c.occurrences, 0) DESC LIMIT %s")
    args.append(max(1, min(500, int(limit))))
    return db.query(" ".join(sql), tuple(args))


def summary():
    by_v = {r["verdict"]: r["n"] for r in db.query(
        "SELECT verdict, count(*) n FROM onto_candidate GROUP BY 1")}
    by_k = {r["kind"]: r["n"] for r in db.query(
        "SELECT kind, count(*) n FROM onto_candidate GROUP BY 1")}
    acts = {r["action"]: r["n"] for r in db.query(
        "SELECT action, count(*) n FROM onto_cand_action GROUP BY 1")}
    src = onto_yaml.source_info()
    return {"by_verdict": {k: by_v.get(k, 0) for k in ("建议新增", "层间不一致", "已在本体")},
            "by_kind": by_k, "actions": acts, "source": src,
            "note": "候选只收可操作的那些：建议新增 / 层间不一致 / 样本与制度都没出现过。"
                    "对上的不进表，只进计数——噪声压不住的清单等于没有清单。"}


# ── 采纳 / 驳回 / 暂缓 ──────────────────────────────────────────────────────
class BuildError(ValueError):
    pass


def _cand(cand_id):
    c = db.query_one("SELECT * FROM onto_candidate WHERE cand_id = %s", (cand_id,))
    if not c:
        raise BuildError("没有这条候选：%s。先跑一次本体构建" % cand_id)
    return c


def preview(cand_id, yaml_target=None):
    """只算不写：把将要插入 YAML 的内容摆出来给人看。"""
    c = dict(_cand(cand_id))
    if yaml_target:
        c["yaml_target"] = yaml_target
    try:
        return onto_yaml.preview(c)
    except onto_yaml.PatchError as e:
        raise BuildError(str(e))


def adopt(cand_id, by, yaml_target=None, reload=True):
    """采纳：**改源文件 本体_v0.1.yaml，不是改库**，然后重装。

    顺序：备份 → 定点插入 → safe_load 验（不过就回滚）→ 重装 → 记台账。
    """
    c = dict(_cand(cand_id))
    if not (by or "").strip():
        raise BuildError("采纳必须署名：没有署名的采纳在审计上等于没发生")
    if db.query_one("SELECT 1 FROM onto_cand_action WHERE cand_id=%s AND action='采纳'",
                    (cand_id,)):
        raise BuildError("这条候选已经采纳过了。重复采纳会在 YAML 里插入两段同样的定义，"
                         "而 loader 对重复 id 是后者覆盖前者——"
                         "只有一段生效，另一段静静地留在源文件里误导下一个人")
    if yaml_target:
        c["yaml_target"] = yaml_target
    if c["verdict"] == "已在本体":
        raise BuildError("这条候选的判定是「已在本体」（对上 %s），不该采纳。"
                         "它进候选表是为了让人看见「枚举里有而样本与制度都没出现过」"
                         % c["existing_ref"])
    try:
        res = onto_yaml.apply_patch(c)
    except onto_yaml.PatchError as e:
        raise BuildError(str(e))

    reloaded, version = False, None
    err = None
    if reload:
        try:
            r = onto_yaml.reload()
            reloaded, version = bool(r.get("loaded")), r.get("version")
        except Exception as e:                                 # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)

    aid = "oca-" + uuid.uuid4().hex[:10]
    db.execute("""INSERT INTO onto_cand_action(action_id, cand_id, cand_kind, cand_key,
                      action, acted_by, yaml_file, yaml_line, yaml_patch, backup_path,
                      reloaded, onto_version)
                  VALUES (%s,%s,%s,%s,'采纳',%s,%s,%s,%s,%s,%s,%s)""",
               (aid, cand_id, c["kind"], c["cand_key"], by.strip(),
                "本体_v0.1.yaml", res["line"], res["patch"], res["backup"],
                reloaded, version))
    return {"action_id": aid, "cand_id": cand_id, "new_id": res.get("new_id"),
            "yaml_line": res["line"], "patch": res["patch"],
            "backup": res["backup"], "sha256": res["sha256"],
            "reloaded": reloaded, "onto_version": version, "reload_error": err,
            "note": "已写进源文件 本体_v0.1.yaml 并重装。"
                    "**改的是源，不是库**——库是投影，下次重装以源为准。"
                    + ("；**重装失败了，源文件已经变了而库还是旧的**，"
                       "这条必须处理掉" if err else "")}


def dispose(cand_id, action, by, reason):
    if action not in ("驳回", "暂缓"):
        raise BuildError("只能是「驳回」或「暂缓」；采纳走 adopt()")
    if not (by or "").strip():
        raise BuildError("必须署名")
    if not (reason or "").strip():
        raise BuildError("必须写理由：「为什么不进本体」比「进了什么」更容易被时间冲掉")
    c = _cand(cand_id)
    aid = "oca-" + uuid.uuid4().hex[:10]
    db.execute("""INSERT INTO onto_cand_action(action_id, cand_id, cand_kind, cand_key,
                      action, acted_by, reason)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)""",
               (aid, cand_id, c["kind"], c["cand_key"], action, by.strip(), reason))
    return {"action_id": aid, "cand_id": cand_id, "action": action}


BIZ_STORAGE = [
    ("本体_v0.1.yaml", "**源文件**：业务/规则/流程本体的全部定义，924 行含口径注释",
     "人工（AI 读材料手写）+ 人工采纳定点追加", "不清",
     "YAML 是源、库是投影、单向永不回写。改它只许按行定点插入，"
     "绝不许 yaml.load→dump——dump 会把注释一次性抹光，而注释里是口径纪律"),
    ("ontology_version", "本体版本与源文件 sha256", "装载脚本", "整版重建",
     "启动自检比 source_hash：不一致说明文件改了没重装，页面上看到的本体是旧的"),
    ("ontology_entity", "实体（含别名）", "装载脚本", "整版重建", "别名是口径信息，0024 才补上"),
    ("ontology_concept", "概念（含别名、可得性、OCR 标记）", "装载脚本", "整版重建",
     "可得性决定规则能不能进执行集；OCR 来源的数值不许当硬阈值"),
    ("ontology_enum", "枚举", "装载脚本", "整版重建", "枚举值只收材料里真出现的"),
    ("ontology_enum_value", "枚举取值", "装载脚本", "整版重建",
     "业务表的枚举列用复合外键 (xxx_enum, xxx) 指向它，24 条"),
    ("enum_value_current", "当前版枚举取值（外键目标）", "装载脚本", "整版重建",
     "业务表的护栏就指在这张表上"),
    ("ontology_relation", "实体之间的结构关系（含基数与来源）", "装载脚本", "整版重建",
     "本体没有关系就只是一张清单"),
    ("onto_candidate", "本体构建抽出的候选", "L1/L2 候选 Agent", "整表重算",
     "算出来的；每条带出处与出现次数，没有出处的不进表"),
    ("onto_cand_action", "采纳/驳回/暂缓（人的动作）+ YAML 补丁留痕", "只有人", "永不清",
     "Agent 写不进它，也写不进 ontology_* 任何一张表——白名单里就没给这个能力。"
     "采纳那行还记着改了哪一行、插了什么、备份在哪"),
]


VERSIONED = ("ontology_version", "ontology_entity", "ontology_concept", "ontology_enum",
             "ontology_enum_value", "ontology_relation")


def biz_storage():
    """业务本体的存储。**第一行是源文件，不是表**——它才是真源头。

    带版本的表**按当前版本计数**，并另给一个全版本数。
    直接 count(*) 会得到 34 个实体、240 个概念（库里还躺着一个旧版
    v0.1-draft），而页面上那个数会被当成「本体有多大」来读。
    **一个会被误读的数，比不给这个数更糟。**
    """
    cur = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    rows = []
    for name, what, who, on_recalc, why in BIZ_STORAGE:
        n = total = None
        if name.endswith(".yaml"):
            try:
                n = len(io.open(config.ONTOLOGY_YAML, encoding="utf-8").read().split("\n"))
            except OSError:
                n = None
        else:
            try:
                total = db.scalar("SELECT count(*) FROM " + name)
                n = (db.scalar("SELECT count(*) FROM %s WHERE version = %%s" % name, (cur,))
                     if name in VERSIONED else total)
            except Exception:                                  # noqa: BLE001
                n = total = None
        rows.append({"table_name": name, "what": what, "written_by": who,
                     "on_recalc": on_recalc, "why": why, "rows": n,
                     "rows_all_versions": total,
                     "stale_versions": (total - n) if (total is not None and n is not None
                                                       and total > n) else 0})
    others = db.query("""SELECT version, is_current, loaded_at FROM ontology_version
                          ORDER BY is_current DESC, loaded_at DESC""")
    return {"items": rows, "current_version": cur, "versions": others,
            "note": "**第一行是源文件，不是数据库表。** 本体的真源头是那份 YAML，"
                    "库里这些表全是它的投影——投影缺一列（0024 之前缺 alias），"
                    "下游就会报假红。行数按**当前版本**算；库里另有 %d 个旧版本的行，"
                    "它们不参与任何判定，但也没人清理过。"
                    % sum(r["stale_versions"] or 0 for r in rows)}


def list_actions(limit=200):
    return db.query("""SELECT * FROM onto_cand_action
                        ORDER BY acted_at DESC LIMIT %s""",
                    (max(1, min(500, int(limit))),))


# ── L0 感知用的素材账 ───────────────────────────────────────────────────────
def perceive():
    """素材有多少、本体有多少。**差额就是这一栏要干的活。**

    另外报两件原先各占一个页面面板的事实（2026-10-19 按要求并进这个 Agent）：

      源头      本体源文件是谁写的、多大、sha256、库里的投影是否还同步。
                这一条最容易被含糊过去：这份 YAML 是**人读客户材料手写的**，
                不是本系统的智能体协同产出。写在 L0 的感知里，
                比写在一个说明面板里多一样东西——「谁在什么时候看见的」。
      规则资产  装载现在**有按钮也有命令行**（2026-10-20），两条路共用装载器
                自己身上的那道闸；以及现在多少规则、其中多少是采纳进来的。

    两件都是「手上有什么」，所以属于感知。**它只读不写**：
    源文件的写入口仍然只有人工采纳（adopt）。装载本身不在这个 Agent 里，
    它是本体工场第②③步的按钮。
    """
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    si = onto_yaml.source_info()
    return {
        # 源头：文件事实 + 库里的投影是否跟得上
        "源文件": si["path"],
        "源文件行数": si["lines"],
        "源文件sha256": si["sha256"],
        "源文件谁写的": si["written_by"],
        "源文件怎么写的": si["written_how"],
        "与库同步": bool(si["loaded"]["in_sync"]),
        "库里版本": si["loaded"]["version"],
        # 规则资产装载：现状 + 为什么没做成按钮（原文搬自那个面板）
        "规则": db.scalar("SELECT count(*) FROM rule WHERE version=1"),
        "规则里采纳进来的": db.scalar("SELECT count(*) FROM rule WHERE version=1 "
                              "AND created_by LIKE 'onto_adopt%%'"),
        "release": db.scalar("SELECT count(*) FROM release"),
        "当前发布": db.scalar("SELECT count(*) FROM release_current"),
        "装载命令": "python -I app/rules/loader.py（页面上是第③步的按钮）",
        "装载会动什么":
            "只重建 created_by='asset_loader' 的规则和它自己那份 draft release。"
            "**已发布的版本和 release_current 一律不碰**，"
            "人采纳进来的规则（created_by=onto_adopt:谁）也保留。"
            "装载前过一道闸：本体没装、源文件没重装、或规则引用了库里没有的概念，"
            "一律拦住——rule_concept 没有指向 ontology_concept 的外键，"
            "顺序点反了不报错，只会留下一批静默的坏引用。",
        "单据明细表": len(bill_sheets()),
        "条款": db.scalar("SELECT count(*) FROM doclib_clause"),
        "审批记录": db.scalar("SELECT count(*) FROM approval_record"),
        "本体实体": db.scalar("SELECT count(*) FROM ontology_entity WHERE version=%s", (v,)),
        "本体概念": db.scalar("SELECT count(*) FROM ontology_concept WHERE version=%s", (v,)),
        "本体枚举取值": db.scalar(
            "SELECT count(*) FROM ontology_enum_value WHERE version=%s", (v,)),
        "流程节点": db.scalar("SELECT count(*) FROM process_node WHERE version=%s", (v,)),
        "现有候选": db.scalar("SELECT count(*) FROM onto_candidate"),
    }
