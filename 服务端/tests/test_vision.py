# -*- coding: utf-8 -*-
"""多模态读图:解析、印证、以及**不许绕过的那道人工闸**。

这组测试不出网（出网的部分由人点按钮触发，见 app/api/doclib.py 的 /vision/read）。
钉的是三件会让「AI 说它看过了」变成事实的事:

  提示词不给「可以说不知道」的出路 → 模型把看不清的格子编出来，而且编得通顺
  只用一个模型读                   → 没有任何东西能证伪它
  读图结果直接进阈值               → 阈值错一位，硬规则就从保护变成伤害
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, vision  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


# ─────────────────────────────────────────────────────────────────────────────
# 一、提示词:必须给模型一条「可以说不知道」的出路
# ─────────────────────────────────────────────────────────────────────────────
def test_提示词必须允许模型说看不清():
    """**一个把所有格子都填满的回答，看文本分辨不出是读清楚了还是编的。**

    所以提示词必须明确给出 `?` 这条出路，再把 `?` 的个数数出来。
    """
    for kind, p in vision.PROMPTS.items():
        assert "?" in p, "%s 的提示词没给「看不清」的写法" % kind
        assert "不要猜" in p, "%s 的提示词没写「不要猜」" % kind
        assert "看不清格子数" in p, "%s 没要求模型自报看不清的个数" % kind
    assert "不要补全" in vision.PROMPT_TABLE
    assert "四舍五入" in vision.PROMPT_TABLE, "没禁止改数字格式"
    assert "合并单元格" in vision.PROMPT_TABLE, "没要求保持合并单元格的范围"


def test_看不清个数取自报与实数的较大值():
    """自报的是模型的自觉，数出来的是事实。**宁可把不确定说多，不可说少。**"""
    assert vision.count_unreadable("| a | ? |\n看不清格子数: 1") == 1
    # 自报 1 但实际写了 3 个 ? —— 取 3
    assert vision.count_unreadable("| ? | ? | ? |\n看不清格子数: 1") == 3
    # 自报 5 但只写了 1 个 —— 取 5（它自己说不确定，就按它说的算）
    assert vision.count_unreadable("| ? |\n看不清格子数: 5") == 5
    assert vision.count_unreadable("| 480 | 380 |\n看不清格子数: 0") == 0
    # 中文问号也算
    assert vision.count_unreadable("| ？ |") == 1


def test_数行数不把分隔行算进去():
    md = "| 省 | 限额 |\n|---|---|\n| 福建 | 480 |\n| 厦门 | 650 |"
    assert vision.count_rows(md) == 2
    assert vision.count_rows("本页无表格") == 0
    assert vision.count_rows(None) == 0


def test_抽数字忽略自报行():
    """自报的「看不清格子数: 3」里那个 3 不是表里的数字，不能混进来比对。"""
    md = "| 福建 | 480 | 380 |\n看不清格子数: 3"
    assert vision.numbers_of(md) == ["480", "380"]


# ─────────────────────────────────────────────────────────────────────────────
# 二、互相印证
# ─────────────────────────────────────────────────────────────────────────────
def test_只有一个模型读过时不许说一致():
    """**一个模型读出来的东西，没有任何东西能证伪它。**"""
    c = vision.cross_check("dlf-不存在", 999)
    assert c["agree"] is None
    assert "只有一个模型" in c["verdict"]
    assert "证伪" in c["note"]


def test_不一致时不许取其中一个():
    """两个模型在同一个格子上给不同的数字，说明那个格子本来就读不准。"""
    src = io.open(os.path.join(APP_DIR, "vision.py"), encoding="utf-8").read()
    fn = src[src.index("def cross_check"):src.index("def summary")]
    assert "不是取其中一个" in fn and "也不是取交集" in fn


# ─────────────────────────────────────────────────────────────────────────────
# 三、不许绕过的那道人工闸
# ─────────────────────────────────────────────────────────────────────────────
def test_读图结果不许自动流进阈值表():
    """**写法测试。** standard_limit 是规则引擎真正用的费率表。

    读图结果要进它，中间必须隔着人工签字 + 人决定录哪几行。
    代码里出现「从 doc_page_vision 直接写 standard_limit」就是把这道闸拆了。
    """
    for fn in ("vision.py", os.path.join("api", "doclib.py")):
        src = io.open(os.path.join(APP_DIR, fn), encoding="utf-8").read()
        assert "INSERT INTO standard_limit" not in src, fn
        assert "insert into standard_limit" not in src.lower(), fn


def test_签字必须留下谁签的和什么时候():
    """库里有 CHECK 钉着:verified_by 与 verified_at 必须成对出现。"""
    import psycopg2
    row = db.query_one("SELECT id FROM doc_page_vision WHERE ok LIMIT 1")
    if not row:
        pytest.skip("还没有成功的读图记录")
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.execute("UPDATE doc_page_vision SET verified_by='张三' WHERE id=%s",
                   (row["id"],))


def test_默认不签字():
    """新落的行 verified_by 必须为空——**默认不可信**，签字是额外动作。"""
    n = db.scalar("SELECT count(*) FROM doc_page_vision WHERE verified_by IS NOT NULL")
    total = db.scalar("SELECT count(*) FROM doc_page_vision")
    if not total:
        pytest.skip("还没有读图记录")
    assert n == 0 or n < total, "所有行都签过字了？签字应该是人一条条点的"


# ─────────────────────────────────────────────────────────────────────────────
# 四、选配置:取**默认**那条
# ─────────────────────────────────────────────────────────────────────────────
def test_取默认的读图配置而不是第一条():
    """**修过的 bug。**

    第一版按 config_id 排序取第一条。用户在页面上新配了一条并设为默认之后，
    这里仍然用着旧的那条——而日志里只会看到一个型号名对不上的 404，
    没人想得到是「选错了配置」。
    """
    rows = db.query("""SELECT config_id, is_default FROM llm_config
                        WHERE purpose='vision_read' AND enabled ORDER BY config_id""")
    if len(rows) < 2:
        pytest.skip("只有一条读图配置，分不出默认与第一条")
    default = [r["config_id"] for r in rows if r["is_default"]]
    if not default:
        pytest.skip("没有设默认")
    assert vision._cfg()["config_id"] == default[0], \
        "没取默认那条（取成了 %s）" % vision._cfg()["config_id"]


def test_不回退到文本模型():
    """读图配置缺失时宁可报错，也不许退回文本模型——那正是要防的事。"""
    src = io.open(os.path.join(APP_DIR, "vision.py"), encoding="utf-8").read()
    fn = src[src.index("def _cfg"):src.index("def pages_to_read")]
    assert "rule_judge" not in fn and "extract" not in fn, "_cfg 会摸到文本用途的配置"
    assert "没有可用的读图配置" in fn


# ─────────────────────────────────────────────────────────────────────────────
# 五、只读该读的页
# ─────────────────────────────────────────────────────────────────────────────
def test_只读表格页与空白页():
    """27 页全读一遍是几万 token，而其中 23 页的原生文字层本来就是好的。"""
    rows = vision.pages_to_read(200)
    assert rows, "一页都没挑出来"
    for r in rows:
        assert r["has_table"] or r["chars"] == 0, \
            "挑了一页既不是表格也不是空白的:%s p%s" % (r["file_name"], r["page_no"])


def test_出网要留痕():
    """哪一页、哪个型号、多少 token、几毫秒——事后都要查得到。"""
    cols = {c["column_name"] for c in db.query(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name='doc_page_vision'""")}
    for must in ("image_path", "model_name", "model_echo", "prompt_tokens",
                 "completion_tokens", "latency_ms", "attempt", "unreadable"):
        assert must in cols, "少了 %s，出网这件事就说不清了" % must


def test_存储表写明了读图不会自动流到阈值():
    s = vision.storage()
    names = [x["table_name"] for x in s["items"]]
    assert "standard_limit" in names
    sl = [x for x in s["items"] if x["table_name"] == "standard_limit"][0]
    assert "不会自动流到这里" in sl["why"]
    assert "人工签字" in s["note"]
