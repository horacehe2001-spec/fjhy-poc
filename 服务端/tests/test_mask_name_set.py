"""脱敏闭集合的来源与覆盖面。

这组用例是补 2026-10-31 查出来的一个真实泄露：
`mask.set_name_set()` 全项目只被 mask.py 自己用内置名单调过，
**库里的真实姓名从来没进过闭集合**。于是第二实例「822港航」14 个员工
姓名在所有出口原样输出；而 `find_leaks()` 用的是同一个闭集合，
闭集合漏了谁它就看不见谁——闸和自检一起瞎，脱敏测试一条不红。

所以这里钉三件事，缺一件都会让同样的事再发生一次：
  ① 闭集合确实是从库里取的，两个实例的人一个不缺（漏报方向）
  ② 正常业务内容一个字都不许被改（误报方向——误报和漏报一样致命：
     一个单位名被打成星号，整份交付物就被打上不可信警告）
  ③ **反向自检存在且有效**：不能用闸自己的规则去验闸
"""

import io
import os
import re

import pytest

from app import db, mask


@pytest.fixture(autouse=True)
def _restore_name_set():
    """每个用例跑完都把闭集合还原成运行期真实状态。

    闭集合是模块级全局单例，钉住不还原会让后面的用例在一个假的小集合上
    跑——「当前实例」那个单例已经让我们栽过一次，52 个 error 全是假线索。
    """
    yield
    mask.refresh_names()


def _db_names(sql, params=()):
    return [r["v"] for r in db.query(sql, params) if r["v"]]


# ─────────────────────────────────────────────────────────────────────────────
# ① 漏报方向
# ─────────────────────────────────────────────────────────────────────────────
def test_闭集合必须从库里取而不是只有内置名单():
    mask.refresh_names()
    src = mask.name_set_source()
    assert "库" in src, "闭集合没从库里取，退回内置名单了：%s" % src
    assert len(mask.name_set()) > len(mask._BUILTIN_NAMES), (
        "闭集合人数 %d 不比内置名单 %d 多，说明库里一个人都没取到"
        % (len(mask.name_set()), len(mask._BUILTIN_NAMES)))


def test_每个实例的员工姓名一个都不许缺():
    """**不按实例过滤**：一条真名在哪个实例都是真名。
    按实例切闭集合只会切出漏洞——切错一次，另一个实例的人就裸着。"""
    mask.refresh_names()
    ns = set(mask.name_set())
    rows = db.query(
        "SELECT instance_id, name FROM employee WHERE name IS NOT NULL")
    missing = sorted({(r["instance_id"], r["name"])
                      for r in rows if r["name"].strip() not in ns})
    assert not missing, "这些员工姓名不在闭集合里，会原样输出：%s" % missing
    assert rows, "employee 表是空的，这个用例什么都没验到"


def test_审批人复合列里的真名也要进闭集合():
    """approval_record.approver 里有一批**员工表里没有的真人**
    （审批流上的人，如 1000536/黄露琳）。只扫 employee 会漏掉他们。"""
    mask.refresh_names()
    ns = set(mask.name_set())
    emp = set(_db_names("SELECT DISTINCT name AS v FROM employee"))
    only_approver = set()
    for raw in _db_names(
            "SELECT DISTINCT approver AS v FROM approval_record"):
        for tok in mask._name_tokens(raw):
            if tok not in emp:
                only_approver.add(tok)
    assert only_approver, "审批人列里没有员工表之外的人，这个用例没验到东西"
    assert not (only_approver - ns), (
        "只出现在审批人列的真名没进闭集合：%s" % sorted(only_approver - ns))


def test_代报单的被代报人靠显式名单兜住():
    """被代报人只写在 expense_bill.summary 自由文本里，
    submitter_no / payee / bill_traveler 全都没有她——
    names_from_db() 按构造取不到，只能靠 _FREETEXT_ONLY_NAMES。"""
    mask.refresh_names()
    ns = set(mask.name_set())
    for n in mask._FREETEXT_ONLY_NAMES:
        assert n in ns, "%s 不在闭集合里" % n
    rows = db.query(
        "SELECT bill_no, summary FROM expense_bill WHERE summary IS NOT NULL")
    leaked = [r["bill_no"] for r in rows
              for n in mask._FREETEXT_ONLY_NAMES
              if n in mask.mask_free_text(r["summary"])]
    assert not leaked, "摘要脱敏后仍含被代报人姓名：%s" % leaked


def test_人工驳回理由脱敏后不得残留闭集合里的姓名():
    """这批文本要送给大模型抽规则，是**出系统**的，必须先过脱敏。"""
    mask.refresh_names()
    ns = mask.name_set()
    rows = db.query("SELECT bill_no, return_reason FROM manual_review "
                    "WHERE return_reason IS NOT NULL")
    assert rows, "一条人工驳回都没有，这个用例没验到东西"
    bad = [(r["bill_no"], n) for r in rows for n in ns
           if n in mask.mask_free_text(r["return_reason"])]
    assert not bad, "驳回理由脱敏后仍含姓名：%s" % bad


# ─────────────────────────────────────────────────────────────────────────────
# ② 误报方向——和漏报一样致命
# ─────────────────────────────────────────────────────────────────────────────
#   单位名都是中文短词，任何「2~3 个汉字的人名模式」都会把它们打掉。
#   复合列里还混着 4 字动作词「撤回单据」和演示占位「演示审核」，
#   收进闭集合就会把界面上所有「撤回单据」打成星号。
_MUST_NOT_CHANGE_UNITS = [
    "闽运长乐公交公司", "厦门海隆", "厦门轮船公司", "泉州港务集团",
    "福港拖轮", "厦门新立基", "八方物流", "中兴新云", "金蝶软件",
    "福清宏源公司", "闽运平潭公司", "漳州港务",
]
_MUST_NOT_CHANGE_TERMS = [
    "撤回单据", "撤回", "演示审核", "系统管理", "入账期间", "业务招待费",
    "驻点辅导", "差旅费报销", "出差审批单", "招待明细区", "补充说明区",
    "日历区", "附件不全", "审批滞后",
]


@pytest.mark.parametrize("text", _MUST_NOT_CHANGE_UNITS)
def test_不许误伤单位名(text):
    mask.refresh_names()
    assert mask.mask_free_text(text) == text


@pytest.mark.parametrize("text", _MUST_NOT_CHANGE_TERMS)
def test_不许误伤业务词(text):
    mask.refresh_names()
    assert mask.mask_free_text(text) == text


def test_复合列里的动作词不许进闭集合():
    """「岑柏/1000507 撤回单据」切开后「撤回单据」是个合法的 4 字汉字串。
    复合列限 2~3 字就是为了挡它，别为了收 4 字姓名把这个限制放开。"""
    mask.refresh_names()
    ns = set(mask.name_set())
    for w in mask._NON_NAME_IN_NAME_COLS:
        assert w not in ns, "%s 不是人名，不许进闭集合" % w


def test_不许误伤规则名与本体名():
    """规则名/本体名是对外要看的业务标识，被脱敏过一次（字段名叫 name
    就按姓名策略脱），打成星号四次。这里再钉一道。"""
    mask.refresh_names()
    names = (_db_names("SELECT name AS v FROM rule")
             + _db_names("SELECT name AS v FROM ontology_concept")
             + _db_names("SELECT name AS v FROM ontology_entity"))
    assert names, "一个规则名/本体名都没取到，这个用例没验到东西"
    hurt = [n for n in names if mask.mask_free_text(n) != n]
    assert not hurt, "规则名/本体名被误伤：%s" % hurt[:10]


def test_不许误伤制度条款正文():
    mask.refresh_names()
    bodies = _db_names(
        "SELECT body AS v FROM doclib_clause WHERE body IS NOT NULL LIMIT 400")
    assert bodies, "一条条款正文都没取到，这个用例没验到东西"
    hurt = [b[:60] for b in bodies if mask.mask_free_text(b) != b]
    assert not hurt, "条款正文被误伤 %d 条：%s" % (len(hurt), hurt[:5])


# ─────────────────────────────────────────────────────────────────────────────
# ③ 反向自检：不能用闸自己的规则去验闸
# ─────────────────────────────────────────────────────────────────────────────
def test_find_leaks查不到闭集合外的姓名_这就是要有反向自检的理由():
    """把这个盲区钉死，是为了让后来的人看见「脱敏测试全绿」不等于没泄露。

    find_leaks 认的就是闭集合，所以闭集合漏掉的人它一个都查不出来。
    822港航 的 14 个人就是这样裸奔了很久而测试一条不红。
    """
    mask.set_name_set(["鄢岚"])          # 故意把真人「訾承望」排除在外
    assert not mask.find_leaks({"note": "审批人訾承望"}), (
        "这一行断言如果红了是好事：说明 find_leaks 不再只认闭集合了，"
        "可以把这个用例改掉")
    assert mask.find_leaks({"note": "经办人鄢岚"}), "闭集合内的姓名必须查得到"


def test_反向自检能发现闭集合漏人():
    """埋雷—检查：故意钉一个缺人的闭集合，verify_name_coverage 必须报出来。
    确定性的闸失效而没人发现，比没有这道闸更危险。"""
    mask.refresh_names()
    full = mask.name_set()
    victim = next((n for n in _db_names(
        "SELECT DISTINCT name AS v FROM employee") if len(n) >= 2), None)
    assert victim, "没取到员工姓名，埋不了雷"
    mask.set_name_set([n for n in full if n != victim])
    gaps = mask.verify_name_coverage()
    assert any(victim in g[2] for g in gaps), (
        "闭集合里抽掉了「%s」，反向自检却没报出来：%s" % (victim, gaps[:5]))


def test_反向自检对完整名单不报():
    mask.refresh_names()
    gaps = mask.verify_name_coverage()
    assert not gaps, "姓名源列还有没进闭集合的真名：%s" % gaps[:10]


def test_残留一个姓氏不算漏报():
    """脱敏按设计保留姓氏（「张灵」→「张*」）。第一版自检没区分，
    把 116 个姓氏全报成漏报，噪音盖住信号——自检报假阳性，
    下次就没人看它了，等于没有。"""
    mask.refresh_names()
    rows = db.query("SELECT DISTINCT name AS v FROM employee "
                    "WHERE name IS NOT NULL AND length(name) = 2 LIMIT 1")
    assert rows, "没有 2 字姓名可验"
    one = rows[0]["v"]
    masked = mask.mask_free_text(one)
    assert masked == one[0] + "*", masked
    assert not [g for g in mask.verify_name_coverage() if g[1] == one]


def test_收款人是单位名不算漏报():
    """收款人可以是单位。实测 822港航 支付区里有
    「福州市八方物业管理有限公司」——它不是人名，脱敏后理应原样留着，
    自检把它报成欠脱就是误报。误报和漏报同样致命：报假阳性，
    下次就没人看这个自检了。"""
    assert mask.looks_like_org("福州市八方物业管理有限公司")
    assert mask.looks_like_org("厦门轮船有限公司")
    assert mask.looks_like_org("福建省港航建设发展有限公司")
    mask.refresh_names()
    gaps, skipped = mask.verify_name_coverage(with_ignored=True)
    assert not gaps, "姓名源列还有没进闭集合的真名：%s" % gaps[:10]
    # 跳过的必须能列出来：「查过是阴性」和「碰巧没看见」是两回事
    assert all(mask.looks_like_org(s[2]) for s in skipped), skipped


def test_单位名规则不许把人名吃掉():
    """反方向：人名、2~3 字的词、以及以单位后缀结尾但只有 2~3 字的，
    都不许被当成单位名放过——放过一个就是一个静默的漏报窗口。"""
    for n in ("靳纭芷", "雒朔", "訾承望", "欧阳明月"):
        assert not mask.looks_like_org(n), n
    # 「酒店」「分店」这类 2~3 字本身就是普通词，长度闸必须拦住
    for n in ("酒店", "门店", "总公司"):
        assert not mask.looks_like_org(n), n
    # 不带单位后缀的长串也不许放过（可能是黏在一起的人名）
    assert not mask.looks_like_org("靳纭芷莆田差旅")


def test_招待对象名单整列不出明文():
    """这一列里的人**按构造进不了闭集合**，所以不能走自由文本那条路：
      · 他们是外部人员（招待对象），不在 employee 表里，names_from_db 取不到
      · 姓名和职务黏在一起没有分隔符，按 2~3 字 token 切一个都切不出来
    两条都不成立时，自由文本脱敏在这一列上等于没脱——
    下面第二个断言就是这个事实的留痕。"""
    raw = "福建省华侨实业集团有限责任公司总经理朱宗秋、总经理助理詹志强、创新事业部经理俞婷"
    assert mask.scrub({"guest_list": raw})["guest_list"] == "名单 3 人（已整列脱敏）"
    # 同一串值落在自由文本列上会原样出去。**这不是这条用例要修的东西**，
    # 它钉的是「为什么 guest_list 必须单列一条策略」。
    assert "朱宗秋" in mask.scrub({"summary": raw})["summary"]


def test_名单数不出人数时不许给一个假的数():
    """实测有 2 行填的是「详情见附件」。把它算成 1 人，
    「名单人数与陪同人数不符」这条规则就会静默判过。
    第一版拿「结尾是不是 2~3 个汉字」当判据——那个条件对任何中文串都成立，
    等于没判，「详情见附件」当时被算成了 1 人。"""
    out = mask.scrub({"guest_list": "详情见附件"})["guest_list"]
    assert "数不出人数" in out, out
    assert "1 人" not in out, out
    # 真有分隔符才报人数
    assert mask.scrub({"guest_list": "甲一、乙二"})["guest_list"] == "名单 2 人（已整列脱敏）"
    # 空与非字符串原样返回
    assert mask.scrub({"guest_list": ""})["guest_list"] == ""
    assert mask.scrub({"guest_list": None})["guest_list"] is None


def test_名单字段名不许带name():
    """带 name 的字段名会落到姓名策略上，而这一列要的是更严的处理。
    项目里为「对外字段不要叫 name」踩过四次，这里是第五次的预防。"""
    assert "guest_list" in mask._FIELD_POLICY
    assert mask._FIELD_POLICY["guest_list"] == "roster"
    cols = [c for _, c in mask._PURE_NAME_COLS + mask._COMPOSITE_NAME_COLS]
    assert "guest_list" not in cols, (
        "guest_list 被当成姓名来源列了：它整列不出明文，"
        "进姓名来源只会把外部人员的姓名收进闭集合")


def test_库里的招待对象名单过一遍脱敏_没有裸的人名():
    """反向自检：直接拿库里 17 行真值过一遍，不许有 2 个以上连着的汉字留下。"""
    rows = db.query("SELECT guest_list FROM entertain_detail WHERE guest_list IS NOT NULL")
    assert rows, "招待明细没入库，这条用例的前提没了"
    for r in rows:
        out = mask.scrub({"guest_list": r["guest_list"]})["guest_list"]
        assert out.startswith("名单"), out
        assert r["guest_list"] not in out


def test_内置名单搬到了仓库外_但闸还在起作用():
    """2026-11-01 公开仓库前搬的家：内置兜底名单（15 个真人 + 自由文本专有）
    从 app/mask.py 的源码里搬到了 服务端/var/name_seed.json。

    **这条用例守的是「搬走 ≠ 删掉」。** 直接删名单会让闸和 find_leaks()
    自检一起瞎——它们用的是同一份闭集合——而且**测试会全绿**。
    这正是本项目在闭集合上栽的第一个跟头（见 mask.py【改动一】）。

    原来这里断言的是某个具体真人在不在名单里。那种写法现在反而是个洞：
    断言本身就要把真名写进源码。改成按文件内容验，名字不出现在用例里。
    """
    seed, freetext = mask._load_seed_file()
    if not seed and not freetext:
        pytest.skip("本机没有 %s（换机器复现时由业务数据灌入），"
                    "此时闸退化成只认库里的 employee.name" % mask._SEED_FILE)
    assert seed, "seed 为空：兜底名单没了，闸只剩库这一条腿"
    assert mask._FREETEXT_ONLY_NAMES == freetext
    mask.refresh_names()
    have = mask.name_set()
    missing = [n for n in seed + freetext if n not in have]
    assert not missing, "内置名单里的人没进闭集合：%s" % missing


def test_源码里一个真名都不许有():
    """公开之后唯一还能发现「真名又被抄回源码」的闸。

    真相来源是**仓库外**的那两处：name_seed.json 和库里的 employee.name。
    拿它们去扫全部已跟踪文件——命中就是有人把真人姓名写回了仓库里，
    哪怕只是写在注释或测试夹具里。
    （演示数据生成器造的「演示甲/乙/丙」不算真人，排除。）
    """
    import subprocess
    seed, freetext = mask._load_seed_file()
    real = set(seed) | set(freetext) | {
        r["name"].strip() for r in db.query(
            "SELECT DISTINCT name FROM employee WHERE name IS NOT NULL")
        if r["name"] and r["name"].strip()}
    real -= {n for n in real if n.startswith("演示")}
    if not real:
        pytest.skip("本机既没有 name_seed.json 也没有员工数据，无从扫起")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.dirname(root)
    files = subprocess.run(["git", "ls-files"], cwd=root,
                           capture_output=True).stdout.decode("utf-8", "replace")
    bad = {}
    for rel in (x.strip() for x in files.split("\n")):
        if not rel:
            continue
        p = os.path.join(root, rel)
        try:
            if os.path.getsize(p) > 2 * 1024 * 1024:
                continue
            txt = io.open(p, "rb").read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue          # 二进制/超大文件不扫，它们由 .gitignore 这一层管
        for n in real:
            if n in txt:
                bad.setdefault(rel, set()).add(n)
    assert not bad, (
        "已跟踪文件里出现了真实姓名，公开仓库会把它们一起发出去：\n%s"
        % "\n".join("  %s: %s" % (f, "、".join(sorted(v)))
                    for f, v in sorted(bad.items())))


# ─────────────────────────────────────────────────────────────────────────────
# 退化行为
# ─────────────────────────────────────────────────────────────────────────────
def test_取库失败不许退化成更小的名单(monkeypatch):
    """已经有一份含库里真人的名单时，刷新失败必须**保留旧的**。
    退回内置名单等于把已经堵上的洞重新打开，而且没有任何报错。"""
    mask.refresh_names()
    before = set(mask.name_set())
    db_only = before - set(mask._BUILTIN_NAMES)
    assert db_only, "库里没有内置名单之外的人，这个用例没验到东西"

    def boom():
        raise RuntimeError("模拟取库失败")

    monkeypatch.setattr(mask, "names_from_db", boom)
    mask._ensure(force=True)
    after = set(mask.name_set())
    assert after == before, "刷新失败后名单变了：少了 %s" % sorted(before - after)
    assert "仍用上一份" in mask.name_set_source(), mask.name_set_source()


def test_闭集合来源能被看见():
    """「退回内置名单」出现在日志里就等于有人在裸奔，要当故障看。
    所以这个状态必须是可读出来的，不能只存在于某个局部变量里。"""
    mask.refresh_names()
    src = mask.name_set_source()
    assert re.search(r"库 \d+ 人", src), src


def test_新装载的人员能被刷进闭集合(monkeypatch):
    """装载完新实例的人员数据后调 refresh_names() 要立刻生效，
    不用等 TTL——TTL 只是兜底，不是唯一通路。"""
    mask.refresh_names()
    assert "桩测人甲" not in mask.name_set()
    real = mask.names_from_db
    monkeypatch.setattr(mask, "names_from_db",
                        lambda: real() | {"桩测人甲"})
    mask.refresh_names()
    assert "桩测人甲" in mask.name_set()
    assert mask.mask_free_text("经办人桩测人甲") == "经办人桩***"
