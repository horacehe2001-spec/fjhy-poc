# -*- coding: utf-8 -*-
"""多模态读图：OCR 丢结构的那几页，用读图模型再读一遍，并让两个模型互相印证。

app/ocr.py 的第 3 条纪律写了上半句：
  实测住宿费标准表 OCR 后数字全在，但城市↔职级↔金额的对应关系全乱了。
  **OCR 丢的不是字，是结构。** 而差旅规则的全部价值就在那个对应关系上。
  交通工具等级表更彻底：OCR 出来是「(1) 分 公 | Ti [Sree 责 人 凭据 报销 等 座」，
  TR-D3 那批规则因此一直编不出来。
  出路两条：**要么人眼读图，要么上多模态模型。**
这个模块是后者。

四条口径：

1. **提示词必须给模型一条「可以说不知道」的出路。**
   一个把所有格子都填满的回答，要么是真读清楚了，要么是把看不清的编出来了——
   而这两者在文本上长得一模一样，看回答本身分辨不出来。
   所以提示词里明确要求：看不清写 `?`，不许猜、不许补全、不许合并单元格。
   然后把 `?` 的个数数出来存进 unreadable —— **这个数比内容本身更重要**。

2. **两个模型各读一遍，只有两边一致的格子才算读出来。**
   这和规则抽取那边的「多源印证」是同一条纪律。
   一个模型读出来的东西，没有任何东西能证伪它。

3. **读出来的数字不自动可信。** 模型读图**不等于**人眼读图：
   它同样会把看不清的格子编出来，而且编得通顺。
   doc_page_vision.verified_by 为空时，这些数字不许进 standard_limit、
   不许进任何规则阈值。项目可信度三档这一条不因为换了模型而改变。

4. **这是唯一把客户制度文件发到外部的动作**（OCR 是本机 tesseract）。
   逐次留痕：哪一页、哪个型号、多少 token、几毫秒、回了什么。
"""
import io
import os
import re

from . import db
from .llm import client

SYSTEM = (
    "你是一个把扫描件里的表格**原样誊抄**成 Markdown 的工具。"
    "你的唯一职责是誊抄你**看得见**的内容。"
)

PROMPT_TABLE = """请把这一页里的表格原样誊抄成 Markdown 表格。

硬性要求（违反其中任何一条，这次誊抄就是废的）：
1. **看不清的格子写 `?`，不要猜、不要补全、不要按常识填。**
   我宁可要一张有十个 `?` 的表，也不要一张看起来完整但有一个格子是编的表。
2. 行数、列数、合并单元格的范围都要和原图一致。合并的单元格在每一行里重复写它的值。
3. 数字一个字符都不要改：不要补小数点、不要加千分位、不要四舍五入。
4. 表格之外的正文（标题、条款、页码、注释）也要抄，放在表格前后，用普通文本。
5. 如果这一页**根本没有表格**，就直接说「本页无表格」，然后把正文抄下来。
6. 不要解释、不要总结、不要评价。只誊抄。

最后单独起一行，写：`看不清格子数: N`（N 是你写了多少个 `?`）。"""

PROMPT_TEXT = """请把这一页上**看得见的文字**原样抄下来。

硬性要求：
1. 看不清的字写 `?`，不要猜。
2. 保持原来的换行与段落。
3. 如果这一页是空白页或只有扫描噪点，就直接说「本页无文字」。
4. 不要解释、不要总结。只誊抄。

最后单独起一行，写：`看不清格子数: N`。"""

PROMPTS = {"table": PROMPT_TABLE, "text": PROMPT_TEXT}


def _cfg(config_id=None):
    """取一条读图配置。**不回退到文本模型**——那正是要防的事。"""
    if config_id:
        row = db.query_one("SELECT * FROM llm_config WHERE config_id=%s", (config_id,))
    else:
        # **取默认那条，不是「按 config_id 排序的第一条」。**
        # 第一版就是按 config_id 取的，结果用户在页面上新配了一条并设为默认之后，
        # 这里仍然用着旧的那条——而日志里只会看到一个型号名对不上的 404，
        # 没人想得到是「选错了配置」。有默认就用默认，这也是页面上那个
        # 「设为默认」按钮唯一的意义。
        row = db.query_one("""SELECT * FROM llm_config
                               WHERE purpose='vision_read' AND enabled AND is_default
                               LIMIT 1""")
        if not row:
            row = db.query_one("""SELECT * FROM llm_config
                                   WHERE purpose='vision_read' AND enabled
                                   ORDER BY config_id LIMIT 1""")
    if not row:
        raise ValueError("没有可用的读图配置。去「大模型配置」配一条 purpose=vision_read 的。")
    return row


def pages_to_read(limit=40):
    """该读哪些页：OCR 标了表格的，或者一个字都没认出来的。

    **不是「所有页都读一遍」**：27 页的文件全读一遍是几万 token，
    而其中 23 页的原生文字层本来就是好的——拿钱换一份更差的副本。
    """
    return db.query("""SELECT o.file_id, f.file_name, o.page_no, o.chars, o.mean_conf,
                              o.has_table, o.image_path,
                              (SELECT count(*) FROM doc_page_vision v
                                WHERE v.file_id=o.file_id AND v.page_no=o.page_no
                                  AND v.ok) AS read_times
                         FROM doc_lib_ocr o JOIN doc_lib_file f ON f.file_id=o.file_id
                        WHERE o.has_table OR o.chars = 0
                        ORDER BY f.file_name, o.page_no LIMIT %s""", (limit,))


_UNREADABLE_RE = re.compile(r"看不清格子数[:：]\s*(\d+)")


def count_unreadable(content):
    """模型自报的「看不清」个数；自报不了就数 `?`。

    两个来源都要：自报的是它的自觉，数出来的是事实。
    取较大的那个——宁可把不确定说多，不可说少。
    """
    said = _UNREADABLE_RE.search(content or "")
    said_n = int(said.group(1)) if said else None
    body = _UNREADABLE_RE.sub("", content or "")
    counted = body.count("?") + body.count("？")
    if said_n is None:
        return counted
    return max(said_n, counted)


def count_rows(content):
    """数表格的数据行（去掉表头与分隔行）。

    **不要求行首行尾都有竖线。** 实测 qwen3-vl-plus 回的表第一行长这样：
    `其他地区 | 590 | 390 | …`——没有首尾竖线，而第一版非要首尾都有，
    于是把一张 22 行的表数成了 0 行。**0 行会被当成「这页没读出来」**，
    而其实内容是对的。判据改成「这一行有两个以上竖线，且不是分隔行」。
    """
    n = 0
    for ln in (content or "").splitlines():
        t = ln.strip()
        if t.count("|") < 2:
            continue
        if re.match(r"^\|?[\s:\-|]+\|?$", t):      # |---|---| 这类分隔行
            continue
        n += 1
    return max(0, n - 1) if n else 0


def read_page(file_id, page_no, config_id=None, kind="table", max_tokens=4000):
    """读一页，落一行。**永不覆盖**：同页同模型重读会 attempt+1 再插一行。"""
    cfg = _cfg(config_id)
    row = db.query_one("""SELECT image_path FROM doc_lib_ocr
                           WHERE file_id=%s AND page_no=%s""", (file_id, page_no))
    if not row or not row["image_path"]:
        raise ValueError("这一页没有渲好的 PNG（file_id=%s page=%s）。"
                         "先在文件库里跑一次 OCR，它会把图渲到 var/ocr/ 下。"
                         % (file_id, page_no))
    img = row["image_path"]
    if not os.path.exists(img):
        raise ValueError("PNG 不在了：%s" % img)
    size = os.path.getsize(img)

    attempt = (db.scalar("""SELECT max(attempt) FROM doc_page_vision
                             WHERE file_id=%s AND page_no=%s AND model_name=%s""",
                         (file_id, page_no, cfg["model_name"])) or 0) + 1

    res, content, use = client.chat_vision(cfg, SYSTEM, PROMPTS[kind], img,
                                           max_tokens=max_tokens)
    db.execute("""INSERT INTO doc_page_vision
        (file_id, page_no, attempt, config_id, model_name, model_echo, prompt_kind,
         image_path, image_bytes, ok, err_kind, message, content, rows_parsed,
         unreadable, latency_ms, prompt_tokens, completion_tokens)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (file_id, page_no, attempt, cfg["config_id"], cfg["model_name"],
         res.model_echo, kind, img, size, res.ok, res.err_kind, res.message,
         content or None,
         count_rows(content) if res.ok else None,
         count_unreadable(content) if res.ok else None,
         res.latency_ms, use.get("prompt_tokens"), use.get("completion_tokens")))
    return {"file_id": file_id, "page_no": page_no, "attempt": attempt,
            "model_name": cfg["model_name"], "ok": res.ok, "err_kind": res.err_kind,
            "message": res.message, "latency_ms": res.latency_ms,
            "rows": count_rows(content) if res.ok else None,
            "unreadable": count_unreadable(content) if res.ok else None,
            "tokens": use, "content": content}


# ─────────────────────────────────────────────────────────────────────────────
# 互相印证
# ─────────────────────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_of(content):
    """抄出来的所有数字（按出现顺序）。比对两个模型用。

    为什么比数字不比整段文本：两个模型的排版、标点、空格必然不同，
    比文本会得到「哪哪都不一样」这种没用的结论。
    而表格的价值全在数字与它所在的行列上——数字对不上才是真的对不上。
    """
    body = _UNREADABLE_RE.sub("", content or "")
    return _NUM_RE.findall(body)


def latest_by_model(file_id, page_no):
    return db.query("""SELECT DISTINCT ON (model_name)
                              model_name, attempt, ok, content, rows_parsed, unreadable,
                              latency_ms, prompt_tokens, completion_tokens, created_at
                         FROM doc_page_vision
                        WHERE file_id=%s AND page_no=%s AND ok
                        ORDER BY model_name, attempt DESC""", (file_id, page_no))


def cross_check(file_id, page_no):
    """两个模型读同一页，比数字序列。

    **只有两边一致的才算读出来。** 一个模型读出来的东西，
    没有任何东西能证伪它——这和规则抽取那边的「多源印证」是同一条纪律。
    """
    reads = latest_by_model(file_id, page_no)
    if len(reads) < 2:
        return {"file_id": file_id, "page_no": page_no, "models": len(reads),
                "verdict": "只有一个模型读过", "agree": None,
                "note": "**一个模型读出来的东西没有任何东西能证伪它。**"
                        "换另一个型号再读一遍，才谈得上印证。"}
    a, b = reads[0], reads[1]
    na, nb = numbers_of(a["content"]), numbers_of(b["content"])
    same = na == nb
    sa, sb = set(na), set(nb)
    only_a = sorted(sa - sb)
    only_b = sorted(sb - sa)
    return {
        "file_id": file_id, "page_no": page_no, "models": len(reads),
        "a": {"model_name": a["model_name"], "numbers": len(na),
              "rows": a["rows_parsed"], "unreadable": a["unreadable"]},
        "b": {"model_name": b["model_name"], "numbers": len(nb),
              "rows": b["rows_parsed"], "unreadable": b["unreadable"]},
        "agree": same,
        "only_a": only_a[:40], "only_b": only_b[:40],
        "verdict": "两边数字完全一致" if same else "两边不一致",
        "note": ("数字序列逐位相同——可以作为**候选**提交人工复核。"
                 if same else
                 "**不一致的部分一律不许用。** 不是取其中一个，也不是取交集："
                 "两个模型在同一个格子上给出不同的数字，说明那个格子本来就读不准。"),
    }


def summary():
    rows = db.query("""SELECT v.file_id, f.file_name, v.page_no,
                              count(*) FILTER (WHERE v.ok) AS reads,
                              count(DISTINCT v.model_name) FILTER (WHERE v.ok) AS models,
                              sum(v.prompt_tokens) AS prompt_tokens,
                              sum(v.completion_tokens) AS completion_tokens,
                              max(v.created_at) AS last_at,
                              bool_or(v.verified_by IS NOT NULL) AS verified
                         FROM doc_page_vision v JOIN doc_lib_file f ON f.file_id=v.file_id
                        GROUP BY v.file_id, f.file_name, v.page_no
                        ORDER BY f.file_name, v.page_no""")
    tot = db.query_one("""SELECT count(*) AS calls,
                                 count(*) FILTER (WHERE NOT ok) AS failed,
                                 coalesce(sum(prompt_tokens),0) AS pt,
                                 coalesce(sum(completion_tokens),0) AS ct,
                                 count(DISTINCT model_name) AS models
                            FROM doc_page_vision""")
    return {"pages": rows, "totals": tot,
            "note": "**读图结果不自动可信。** verified_by 为空的行，"
                    "数字不许进 standard_limit、不许进任何规则阈值——"
                    "模型读图不等于人眼读图，它同样会把看不清的格子编出来，"
                    "而且编得通顺。"}


def storage():
    return {"items": [
        {"table_name": "doc_lib_ocr", "what": "本机 OCR 的逐页文本与**渲好的 PNG 路径**",
         "written_by": "文件库的「跑 OCR」（本机 tesseract）", "on_recalc": "按文件重跑",
         "why": "读图发出去的就是这里记的那张 PNG，不是客户目录里的文件"},
        {"table_name": "doc_page_vision",
         "what": "读图结果：一页一模型一行，含 token 数与「看不清格子数」",
         "written_by": "读图动作（**唯一会把制度文件发到外部的动作**）",
         "on_recalc": "**永不覆盖**，重读加一行",
         "why": "两份独立证据都要留着才能互相印证；也要能回答「哪一页的图离开过本系统」"},
        {"table_name": "standard_limit", "what": "规则引擎真正用的限额费率",
         "written_by": "规则资产装载", "on_recalc": "装载时整表重建",
         "why": "**读图结果不会自动流到这里。** 要人工签字之后，"
                "由人决定录入哪几行——阈值错一位，硬规则就从保护变成伤害"},
    ], "note": "读图 → 印证 → **人工签字** → 才谈得上录入。中间少一步都不行。"}
