# -*- coding: utf-8 -*-
"""客户端上传 → 托管目录：四条口径与一条一致性。

这个文件钉的是迁移 0034 那四个决策，**少钉一条就会出事**：

1. **写盘只发生在托管根。** 引用根（客户的资料目录）上的删除一律拒绝。
   0015 的只读纪律没有因为加了上传而放松，它的边界被写清楚了——
   而「边界写在注释里但入口没卡」等于没有边界（本项目踩过：missing_impl
   挂在从未被调用的 boot() 里，9 条空规则潜伏很久）。

2. **同内容拒绝、同名不同内容另存。** 这两件事相反，合并就会让使用者
   不知道自己刚传的新版本到底进没进库。

3. **删文件不删登记行。** doc_lib_file 的行一删，0017/0018/0027/0029
   四张派生表全部级联清空；而已产出条款/规则明细的文件一律拒删。

4. **按实例过滤是真的过滤。** 另一个实例的文件不许出现在本实例的清单里。

外加一条一致性，它是这条链路最容易出而最难发现的故障：
   **上传接受的集合必须是扫描登记集合的子集。** 上传落了盘而扫描跳过它
   （~$ 锁文件、$ 开头的目录、SKIP_DIRS），表现是「传成功了但清单里没有」，
   而磁盘上其实已经有了——这种现象要翻落盘目录才看得出来。

路径闸的测试**两个方向都做**（全局纪律第 9 条）：该拦的拦住，
而正常中文文件名（带《》〔〕（）、全角冒号、空格、&'#%[]{}!）一个都不许误伤。
误伤的代价更大——客户当场就会说「我这份制度传不上去」。

测试数据建在一个专用实例 FJHY-TEST-UPLOAD 下（独立的落盘目录），
跑完连目录带库行一起删干净（全局纪律第 12 条：验证产物要一眼可辨、事后好清理）。
"""
import io
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, doclib  # noqa: E402

INST = "fjhytest-upload"
LABEL = "FJHY-TEST-UPLOAD"


def up(name, data=b"\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1 test"):
    """造一份上传项。默认内容是「第一条 test」的 UTF-8 字节。"""
    return (name, io.BytesIO(data))


@pytest.fixture
def inst():
    """专用测试实例：落盘目录独立，跑完整个目录删掉。

    **不往 fjhy 的托管目录里传**：那是演示环境，混进 20 份测试文件之后
    客户下一次打开制度文件库看到的就是一堆 FJHY-TEST 开头的文件。
    """
    db.execute("""INSERT INTO domain_instance(instance_id, domain_id, label,
                                              is_default, sort_no, note)
                  VALUES (%s, 'audit', %s, FALSE, 9999, %s)
                  ON CONFLICT (instance_id) DO NOTHING""",
               (INST, LABEL, "单元测试用，跑完即删。见 tests/test_doclib_upload.py"))
    yield INST
    # 清理顺序：文件登记 → 台账 → 根 → 实例 → 磁盘目录。
    # 反过来会被外键挡住（doc_lib_root.instance_id REFERENCES ... ON DELETE RESTRICT）。
    rids = [r["root_id"] for r in db.query(
        "SELECT root_id FROM doc_lib_root WHERE instance_id=%s", (INST,))]
    for rid in rids:
        db.execute("DELETE FROM doc_lib_change WHERE file_id IN "
                   "(SELECT file_id FROM doc_lib_file WHERE root_id=%s)", (rid,))
    db.execute("DELETE FROM doc_lib_upload WHERE instance_id=%s", (INST,))
    db.execute("DELETE FROM doc_lib_root WHERE instance_id=%s", (INST,))
    db.execute("DELETE FROM doc_lib_scan WHERE instance_id=%s", (INST,))
    db.execute("DELETE FROM domain_instance WHERE instance_id=%s", (INST,))
    shutil.rmtree(os.path.join(doclib.config.DOCLIB_DIR, "audit", INST),
                  ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# 路径闸：两个方向都测
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "差旅费管理办法（闽港财智〔2025〕20号）.pdf",
    "附件1 住宿费标准表.xlsx",
    "关于印发《业务接待管理办法》的通知.docx",
    "2025年度/差旅/实施细则 v2.docx",
    "问答：住宿费篇.docx",                      # 全角冒号，中文文件名里是正常的
    "A&B's report (final).pdf",
    "制度#3[终稿]{v2}!.txt",
    "标准-表_2025+附录=最终@v1.csv",
    "住宿费标准～附表.md",                      # 全角波浪号，不是 ~$ 锁文件
])
def test_正常文件名一个都不许误伤(name):
    """误报方向。**这一组比下面那组更要紧**：

    漏掉一个穿越路径，攻击者要先能访问到这个内网 POC；
    而误伤一个正常文件名，客户当场就说「我这份制度传不上去」。
    """
    rel, _ = doclib.safe_rel(name)
    assert rel.endswith(os.path.basename(name.replace("\\", "/")))


@pytest.mark.parametrize("name,why", [
    ("../../Windows/System32/evil.txt", "目录穿越"),
    ("a/../../b.txt", "穿越藏在中间段"),
    ("C:/Windows/x.txt", "盘符"),
    ("报告.pdf:hidden.exe", "NTFS 备用数据流"),
    ("CON.txt", "Windows 保留设备名"),
    ("aux.pdf", "保留名不分大小写"),
    ("what?.txt", "ASCII 问号，Windows 建不出来"),
    ("~$差旅管理办法.docx", "Office 锁文件，扫描会跳过"),
    (".~lock.办法.docx", "锁文件的另一种前缀"),
    ("$Recycle/x.txt", "$ 开头的目录，扫描会跳过"),
    ("node_modules/a.txt", "在扫描跳过名单里"),
    ("木马.exe", "扩展名不在白名单"),
    ("没有扩展名", "没有扩展名"),
    ("x" * 300 + ".txt", "单段超长"),
    ("a/b/c/d/e/f/g/h/i/j/k/l/m/n.txt", "层数超过上限"),
    ("   ", "空名"),
    ("...", "只有点"),
])
def test_该拦的一个都不许放过(name, why):
    with pytest.raises(doclib.PathError):
        doclib.safe_rel(name)


def test_上传接受的集合是扫描登记集合的子集(inst):
    """这条一致性是整条链路最容易出、最难发现的故障。

    上传把文件落了盘，而 scan 的 _walk 跳过了它（锁文件前缀、$ 目录、
    SKIP_DIRS），表现就是「传成功了但清单里没有」，磁盘上却已经有了。
    所以 safe_rel 必须把「扫描会跳过的名字」也挡在门外，这里正向验一遍：
    **safe_rel 放行的每一个名字，上传之后都必须能在 doc_lib_file 里找到。**
    """
    names = [
        "差旅费管理办法（闽港财智〔2025〕20号）.txt",
        "附件1 住宿费标准表.csv",
        "2025年度/实施细则.md",
        "问答：住宿费篇.txt",
        "A&B's (final).txt",
    ]
    # 每份内容必须不同，否则会被 sha256 去重挡掉——这里验的是「名字能不能过」，
    # 不是去重（去重另有用例）。
    r = doclib.save_uploads(
        [up(n, ("第一条 内容 %d" % i).encode("utf-8")) for i, n in enumerate(names)],
        instance_id=inst, by=LABEL)
    assert r["ok_count"] == len(names), r["rejected"]
    for it in r["items"]:
        assert it["result"] == "已入库", it
        # file_id 是「扫描真的登记了它」的唯一证据：它是 scan() 之后
        # 按 abs_path 查回来的，查不到就会是 None。
        assert it["file_id"], "落了盘但扫描没登记：%s" % it["rel_path"]
        row = db.query_one("SELECT parse_state, origin, sha256, instance_id "
                           "FROM doc_lib_file WHERE file_id=%s", (it["file_id"],))
        assert row["parse_state"] == "已解析", row
        assert row["origin"] == "上传"
        assert row["sha256"] == it["sha256"]
        assert row["instance_id"] == inst


# ─────────────────────────────────────────────────────────────────────────────
# 口径二 · 同内容拒绝 / 同名不同内容另存
# ─────────────────────────────────────────────────────────────────────────────
def test_同内容重复上传不落第二份(inst):
    a = doclib.save_uploads([up("办法.txt", b"AAA")], instance_id=inst)
    assert a["items"][0]["result"] == "已入库"
    # 改个名字再传同样的内容：判的是内容不是文件名
    b = doclib.save_uploads([up("办法-改名.txt", b"AAA")], instance_id=inst)
    assert b["items"][0]["result"] == "同内容已存在"
    assert b["items"][0]["file_id"] == a["items"][0]["file_id"]
    assert b["ok_count"] == 0
    d = os.path.join(doclib.config.DOCLIB_DIR, "audit", inst)
    assert not os.path.exists(os.path.join(d, "办法-改名.txt")), "不该落第二份"


def test_同名不同内容存成新版本且不覆盖(inst):
    a = doclib.save_uploads([up("办法.txt", b"V1")], instance_id=inst)
    b = doclib.save_uploads([up("办法.txt", b"V2")], instance_id=inst)
    assert a["items"][0]["stored_name"] == "办法.txt"
    assert b["items"][0]["result"] == "同名另存"
    assert b["items"][0]["stored_name"] == "办法(2).txt"
    d = os.path.join(doclib.config.DOCLIB_DIR, "audit", inst)
    # 旧的那份**一个字节都没动**：覆盖会让已抽出的规则指向不存在的原文
    assert open(os.path.join(d, "办法.txt"), "rb").read() == b"V1"
    assert open(os.path.join(d, "办法(2).txt"), "rb").read() == b"V2"
    # 清单上要能看出「有同名的另一个版本」
    rows = doclib.list_files(instance_id=inst, q="办法")
    assert all(r["name_copies"] == 1 for r in rows), \
        "办法.txt 与 办法(2).txt 文件名不同，不该算同名"


def test_同一批里的重复也认得出来(inst):
    """库里还没有，所以查库查不到——要靠批内的 sha 记账。"""
    r = doclib.save_uploads([up("甲.txt", b"SAME"), up("乙.txt", b"SAME")],
                            instance_id=inst)
    assert [i["result"] for i in r["items"]] == ["已入库", "同内容已存在"]
    assert "这一批里前面已经有一份" in r["items"][1]["reason"]


def test_零字节文件当场拒绝(inst):
    r = doclib.save_uploads([up("空的.txt", b"")], instance_id=inst)
    assert r["ok_count"] == 0
    assert "0 字节" in r["items"][0]["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# 口径一 · 写盘只发生在托管根
# ─────────────────────────────────────────────────────────────────────────────
def test_引用根里的文件一律拒删(inst, tmp_path):
    """0015 的只读纪律：客户的资料目录是证据，不是工作区。

    **这个闸必须在 live path 上。** 只在注释里写「引用根只读」，
    而删除入口不校验 kind，等于没有这条纪律。
    """
    root = tmp_path / LABEL
    root.mkdir()
    (root / "客户制度.txt").write_text("第一条 内容", encoding="utf-8")
    res = doclib.add_roots([str(root)], label=LABEL, instance_id=inst)
    assert res[0]["ok"] and res[0]["kind"] == "引用"
    doclib.scan([res[0]["root_id"]])
    fid = db.scalar("SELECT file_id FROM doc_lib_file WHERE root_id=%s",
                    (res[0]["root_id"],))
    assert fid
    with pytest.raises(doclib.ScopeError):
        doclib.delete_managed_file(fid)
    # 拒了之后磁盘文件必须还在
    assert (root / "客户制度.txt").exists()


def test_托管区不能当引用根纳入(inst):
    """同一批文件有两个登记（一个可写一个只读），
    「能不能删这个文件」就取决于从哪条登记看它。"""
    doclib.ensure_managed_root(inst)
    d = os.path.join(doclib.config.DOCLIB_DIR, "audit", inst)
    res = doclib.add_roots([d], instance_id=inst)
    assert not res[0]["ok"]
    assert "托管区" in res[0]["reason"]


def test_托管根是幂等创建的(inst):
    a = doclib.ensure_managed_root(inst)
    b = doclib.ensure_managed_root(inst)
    assert a["root_id"] == b["root_id"] == doclib._rid(a["abs_path"])
    assert a["kind"] == "托管"
    assert os.path.isdir(a["abs_path"])
    # 一个实例只许一个托管根（唯一索引 ux_doclib_root_managed）
    assert db.scalar("SELECT count(*) FROM doc_lib_root "
                     "WHERE instance_id=%s AND kind='托管'", (inst,)) == 1


def test_没选实例不许上传(inst):
    with pytest.raises(doclib.ScopeError):
        doclib.save_uploads([up("x.txt")], instance_id="不存在的实例")


# ─────────────────────────────────────────────────────────────────────────────
# 口径三 · 删文件不删登记行；已产出的拒删
# ─────────────────────────────────────────────────────────────────────────────
def test_删托管文件后登记行按消失保留(inst):
    r = doclib.save_uploads([up("要删的.txt", b"X")], instance_id=inst)
    fid = r["items"][0]["file_id"]
    path = db.scalar("SELECT abs_path FROM doc_lib_file WHERE file_id=%s", (fid,))
    assert os.path.exists(path)

    d = doclib.delete_managed_file(fid, by=LABEL)
    assert d["removed_from_disk"]
    assert not os.path.exists(path), "磁盘文件没删掉"
    row = db.query_one("SELECT missing, last_change FROM doc_lib_file WHERE file_id=%s",
                       (fid,))
    assert row is not None, "**登记行被删了**——那会级联清空 OCR/读图/条款/规则明细"
    assert row["missing"] and row["last_change"] == "消失"
    # 变化台账要留下这一笔，且挂在一个真实的扫描批次上（不是手写的假批次）
    ch = db.query_one("""SELECT c.change, s.mode FROM doc_lib_change c
                           JOIN doc_lib_scan s ON s.scan_id = c.scan_id
                          WHERE c.file_id=%s ORDER BY c.id DESC LIMIT 1""", (fid,))
    assert ch and ch["change"] == "消失"


def test_已产出条款的文件拒删(inst):
    r = doclib.save_uploads([up("有条款的.txt", b"Y")], instance_id=inst)
    fid = r["items"][0]["file_id"]
    db.execute("""INSERT INTO doclib_clause(clause_id, file_id, seq, clause_no,
                                            body, char_pos, doc_kind, file_fingerprint)
                  VALUES (%s,%s,1,'第一条','测试条款',0,'制度','test')""",
               (LABEL + "-c1", fid))
    try:
        with pytest.raises(doclib.ConflictError) as e:
            doclib.delete_managed_file(fid)
        assert "条款" in str(e.value)
        # 拒了之后磁盘文件必须还在——拒绝不是「删了再说一声」
        path = db.scalar("SELECT abs_path FROM doc_lib_file WHERE file_id=%s", (fid,))
        assert os.path.exists(path)
    finally:
        db.execute("DELETE FROM doclib_clause WHERE clause_id=%s", (LABEL + "-c1",))


def test_删过的内容允许重新上传(inst):
    """唯一索引带了 NOT missing 就是为了这件事：
    误删一份之后必须能传回来，不然「删除」变成了不可逆操作。"""
    a = doclib.save_uploads([up("回传.txt", b"Z")], instance_id=inst)
    doclib.delete_managed_file(a["items"][0]["file_id"])
    b = doclib.save_uploads([up("回传.txt", b"Z")], instance_id=inst)
    assert b["items"][0]["result"] == "已入库", b["items"][0]


def test_有文件的托管根不许整个移出(inst):
    """整体移出会级联清掉条款/OCR/读图/规则明细，磁盘上留下一堆孤儿文件。"""
    r = doclib.save_uploads([up("占位.txt", b"K")], instance_id=inst)
    with pytest.raises(doclib.ConflictError):
        doclib.remove_root(r["root_id"])


# ─────────────────────────────────────────────────────────────────────────────
# 口径四 · 按实例过滤是真的过滤
# ─────────────────────────────────────────────────────────────────────────────
def test_另一个实例的文件不出现在本实例清单里(inst):
    doclib.save_uploads([up("只属于测试实例.txt", b"Q")], instance_id=inst)
    mine = doclib.list_files(instance_id=inst)
    assert any(f["file_name"] == "只属于测试实例.txt" for f in mine)
    other = doclib.list_files(instance_id="fjhy")
    assert not any(f["file_name"] == "只属于测试实例.txt" for f in other), \
        "测试实例的文件漏进了福建海运集团的清单"
    # 统计也要跟着过滤，否则页面上「文件数」和清单行数对不上
    assert doclib.stats(inst)["files"] == len(mine)


def test_扫描默认只扫本实例的根(inst, tmp_path):
    root = tmp_path / (LABEL + "-ref")
    root.mkdir()
    (root / "甲.txt").write_text("第一条", encoding="utf-8")
    doclib.add_roots([str(root)], label=LABEL, instance_id=inst)
    r = doclib.scan(instance_id=inst)
    paths = {x["path"] for x in r["roots"]}
    assert str(root) in paths
    assert all(doclib.db.scalar(
        "SELECT instance_id FROM doc_lib_root WHERE root_id=%s", (x["root_id"],)) == inst
        for x in r["roots"]), "扫到了别的实例的根"


# ─────────────────────────────────────────────────────────────────────────────
# 台账与暂存区
# ─────────────────────────────────────────────────────────────────────────────
def test_被拒的文件也在台账里留行(inst):
    """客户传了 20 份库里只多 17 份时，差的 3 份必须查得出是哪一种拒绝。"""
    doclib.save_uploads([up("好的.txt", b"OK"), up("木马.exe", b"BAD"),
                         up("../穿越.txt", b"BAD")], instance_id=inst)
    led = doclib.list_uploads(instance_id=inst)
    got = {i["file_name"]: i["result"] for i in led["items"]}
    assert got.get("好的.txt") == "已入库"
    assert got.get("木马.exe") == "扩展名不支持"
    assert got.get("穿越.txt") == "文件名不合法"
    # 非「已入库」的必须有原因（CHECK 约束 ck_doclib_upload_reason 也拦着）
    for i in led["items"]:
        if i["result"] != "已入库":
            assert i["reason"], i


def test_上传之后暂存区必须是干净的(inst):
    """每份上传要么 rename 走、要么当场删掉。残留说明有一次崩在中途。"""
    doclib.save_uploads([up("甲.txt", b"A"), up("乙.exe", b"B"),
                         up("丙.txt", b"A")], instance_id=inst)   # 收、拒、重复各一
    assert doclib.storage(inst)["staging_stale"] == []


def test_子目录也过同一道路径闸(inst):
    with pytest.raises(doclib.PathError):
        doclib.save_uploads([up("x.txt")], instance_id=inst, subdir="../../逃出去")
    r = doclib.save_uploads([up("x.txt", b"S")], instance_id=inst, subdir="2025年度")
    assert r["items"][0]["rel_path"] == "2025年度/x.txt"
    assert os.path.exists(os.path.join(doclib.config.DOCLIB_DIR, "audit", inst,
                                       "2025年度", "x.txt"))


def test_超过份数上限时整批拒绝(inst):
    n = doclib.config.MAX_UPLOAD_FILES + 1
    with pytest.raises(doclib.ScopeError):
        doclib.save_uploads([up("f%d.txt" % i, b"x") for i in range(n)],
                            instance_id=inst)


def test_超过单份大小上限不把它写完(inst):
    """先收完再判断，等于谁都能用一个大文件填满服务器磁盘。"""
    big = b"x" * (doclib.config.MAX_UPLOAD_BYTES + 1024)
    r = doclib.save_uploads([up("超大.txt", big)], instance_id=inst)
    assert r["items"][0]["result"] == "超过大小上限"
    assert doclib.storage(inst)["staging_stale"] == [], "半截文件留在暂存区了"
