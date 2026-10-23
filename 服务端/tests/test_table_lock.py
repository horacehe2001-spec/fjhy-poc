# -*- coding: utf-8 -*-
"""按表粒度的互斥闸：几条链共用 runner，整表重算不许同时跑两份。

**这道闸守的是一件坏了不报错的事。** 本体工场复用了「本体建设」「统一口径」
两条链的 7 个执行单元（同一函数对象），而三条链各有自己的 AgentJob 锁、
互不阻塞。这些 runner 做的是整表清空后重建，且 `db.execute()` 每次自开连接、
退出即提交——所以 `save()` 是「DELETE 一个事务提交 → N 条 INSERT 各自提交」。

两条链同时点下去：A 删完插到一半，B 又删一次，最终台账是两次运行的混合，
而 INSERT 带 ON CONFLICT DO NOTHING，冲突被吞掉——**整个过程不报错**。

所以这组测试钉四件事：
  1. 第二个调用方**当场被拒**，不是排队等（排队会让页面一直转，人看不出在等谁）
  2. **报得出谁占着**——报不出持有者的冲突提示，和没有提示差不多
  3. 锁名写错**当场炸**，不许静默不加锁（以为有闸而其实没有，比没闸更坏）
  4. 四个破坏性写入点**真的套上了**，而且闸在 DELETE 之前

第 4 条用源码断言，理由和 test_workshop 里那条闸一样：
行为测试要真跑一次整表重算才测得到，而那会把库搅一遍；
源码断言钉的是「闸还在不在、在不在 DELETE 前面」，恰好是会被改掉的那部分。
"""
import inspect
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


# ── 闸本身 ──────────────────────────────────────────────────────────────────
def test_第二个调用方当场被拒不排队():
    with db.table_lock("onto_candidate", who="测试-先占着"):
        with pytest.raises(db.TableBusy):
            with db.table_lock("onto_candidate", who="测试-后来的"):
                pytest.fail("不该拿到锁")


def test_报得出谁占着():
    """**持有者要写在报错里。** 「表正在被重算」而不说是谁，
    人只能去猜是哪个页面点过——而猜错就会去重启服务。"""
    with db.table_lock("onto_candidate", who="测试-占锁者甲"):
        with pytest.raises(db.TableBusy) as e:
            with db.table_lock("onto_candidate", who="测试-乙"):
                pass
    assert "测试-占锁者甲" in str(e.value), str(e.value)
    assert "onto_candidate" in str(e.value), str(e.value)


def test_with退出后锁要放掉():
    """放不掉的锁等于一次性闸：跑过一轮之后谁也别想再重算。"""
    with db.table_lock("onto_align", who="测试-第一轮"):
        pass
    with db.table_lock("onto_align", who="测试-第二轮"):
        pass            # 拿不到就会抛，走到这里就是放掉了


def test_抛出来之后也要放掉锁():
    """**异常路径也得放。** 链在重算中途失败是正常的（闸没过、数据缺），
    那一次失败不该把表永久锁死。"""
    with pytest.raises(ValueError):
        with db.table_lock("onto_rule_cover", who="测试-中途炸"):
            raise ValueError("模拟重算失败")
    with db.table_lock("onto_rule_cover", who="测试-炸完还能拿"):
        pass


def test_锁名写错当场炸():
    """**不许静默放行。** 名字打错而闸静默不生效，是「以为有闸」——
    比没有这道闸更危险，因为没人会再去查它。"""
    with pytest.raises(KeyError):
        with db.table_lock("onto_candidat", who="测试-少个e"):
            pytest.fail("写错的表名不该放行")


def test_多张表要么全拿到要么全不拿():
    """一次对账的正反两面只锁住一张，另一张仍会被别的链中途重算，
    结果是正向反向对不上——而这种不一致不报错。"""
    with db.table_lock("onto_align", who="测试-占了正向"):
        with pytest.raises(db.TableBusy):
            with db.table_lock("onto_align", "onto_rule_cover", who="测试-想要两张"):
                pytest.fail("交集非空就不该放行")
    # 甲放手之后，两张一起拿得到
    with db.table_lock("onto_align", "onto_rule_cover", who="测试-现在两张都拿"):
        pass


def test_不相干的表互不影响():
    """按表粒度的意义就在这里：锁住候选不该把本体装载也挡住。
    一把全局锁会让「本体建设」页在工场跑的时候完全点不动。"""
    with db.table_lock("onto_candidate", who="测试-占候选"):
        with db.table_lock("ontology", who="测试-装本体照样能跑"):
            pass


def test_取锁顺序固定防两边都失败():
    """try_ 是非阻塞的，所以这里防的**不是死锁**，是「两边各占一张、都失败」。

    取序相反时（甲先拿 onto_align、乙先拿 onto_rule_cover）两边都拿不全，
    两次重算都白跑。排序保证全局一致的获取顺序，让其中一个干净地赢。
    """
    order = []
    with db.table_lock("onto_rule_cover", "onto_align", who="测试-乱序传入") as names:
        order = list(names)
    assert order == sorted(order), order
    assert order == ["onto_align", "onto_rule_cover"], order


def test_真并发只有一个赢():
    """**真起线程打一次。** 单线程的 with 嵌套证明不了跨连接的互斥。"""
    wins, busy, other = [], [], []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        try:
            with db.table_lock("onto_candidate", who="测试-并发%d" % i):
                wins.append(i)
                # 占住一会儿，确保其余线程确实在这段时间里撞上
                threading.Event().wait(0.25)
        except db.TableBusy:
            busy.append(i)
        except Exception as e:                                 # noqa: BLE001
            other.append("%s: %s" % (type(e).__name__, e))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not other, other
    assert len(wins) == 1, "拿到锁的线程数应为 1，实际 %s" % wins
    assert len(busy) == 7, "被拒的线程数应为 7，实际 %s" % busy


# ── 键表 ────────────────────────────────────────────────────────────────────
def test_键号不重复():
    """两张表共用一个键号 = 静默互斥：功能看着是好的，只是偶尔莫名拒绝。"""
    keys = list(db.LOCK_KEYS.values())
    assert len(keys) == len(set(keys)), db.LOCK_KEYS


def test_跨进程兜底标签必须有信息量():
    """PG 的 application_name 存不住中文（逐字节转义成 \\xNN 再按 63 字节截断）。

    所以跨进程占锁时只能报 ASCII 标签。实测踩到：who 全是中文时残留只剩
    一个字母，报出来是「fjhy:A」——这种持有者名等于没报。
    四个真实落点的 who 都带 ASCII 函数名，这里钉住「短了就退回表名」。
    """
    assert db._ascii_tag("实测A 外部占锁", ["onto_candidate"]) == "fjhy:onto_candidate"
    assert db._ascii_tag("候选重算 onto_build.save", ["onto_candidate"]) \
        == "fjhy:onto_build.save"
    assert db._ascii_tag("规则资产装载 rules.loader.load_all", ["rule"]) \
        == "fjhy:rules.loader.load_all"
    assert len(db._ascii_tag("x" * 200, ["rule"])) <= 63


@pytest.mark.parametrize("mod,fn,tables,_d", [
    ("app.onto_build", "save", ["onto_candidate"], None),
    ("app.onto_coord", "save_align", ["onto_align", "onto_rule_cover"], None),
    ("app.rules.loader", "load_all", ["rule"], None),
    ("app.ontology.loader", "load", ["ontology"], None),
])
def test_落点的who带得出ASCII函数名(mod, fn, tables, _d):
    """**who 不许只写中文。** 只写中文的话跨进程占锁报不出有用的持有者，
    而「命令行跑装载 vs 页面点按钮」正是跨进程的那种冲突。"""
    import importlib
    import re
    m = importlib.import_module(mod)
    src = inspect.getsource(getattr(m, fn))
    whos = re.findall(r'who="([^"]+)"', src)
    assert whos, "%s.%s 没给 who" % (mod, fn)
    for w in whos:
        tag = db._ascii_tag(w, tables)
        assert tag != "fjhy:" + ",".join(tables), \
            "%s.%s 的 who=%r 里没有 ASCII 函数名，跨进程只能退回表名" % (mod, fn, w)


def test_键号都是正整数():
    """0 和负数在 pg_locks 里对不上 objid（无符号 oid），查不到持有者。"""
    for t, k in db.LOCK_KEYS.items():
        assert isinstance(k, int) and k > 0, (t, k)


# ── 四个破坏性写入点真的套上了 ──────────────────────────────────────────────
# (模块, 函数名, 该占住的表, DELETE 的目标)
SITES = [
    ("app.onto_build", "save", ["onto_candidate"], "DELETE FROM onto_candidate"),
    ("app.onto_coord", "save_align", ["onto_align", "onto_rule_cover"],
     "DELETE FROM onto_align"),
    ("app.rules.loader", "load_all", ["rule"], None),
    ("app.ontology.loader", "load", ["ontology"], None),
]


@pytest.mark.parametrize("mod,fn,tables,_del", SITES)
def test_写入点套了锁(mod, fn, tables, _del):
    import importlib
    m = importlib.import_module(mod)
    src = inspect.getsource(getattr(m, fn))
    assert "table_lock(" in src, "%s.%s 没套 table_lock" % (mod, fn)
    for t in tables:
        assert '"%s"' % t in src, "%s.%s 没占住 %s" % (mod, fn, t)


@pytest.mark.parametrize("mod,fn,tables,del_sql", SITES)
def test_闸在DELETE之前(mod, fn, tables, del_sql):
    """**闸要在清空之前。** 放在后面等于先把表删了再问能不能删。"""
    if not del_sql:
        pytest.skip("这个写入点的 DELETE 在被包裹的内部函数里，由上一条测试覆盖")
    import importlib
    m = importlib.import_module(mod)
    whole = open(m.__file__, encoding="utf-8").read()
    assert whole.index("table_lock(") < whole.index(del_sql), \
        "%s 里 table_lock 出现在 %s 之后" % (mod, del_sql)


def test_共用runner的两条链各自的锁确实互不阻塞():
    """这是**这道闸存在的前提**，前提没了闸就该重新想。

    三条链各有自己的 AgentJob 锁，所以作业级的锁拦不住跨链并发——
    哪天它们合并成一把锁了，这条会红，提醒人回来重新评估。
    """
    from app import onto_build_orchestrator as borch, onto_orchestrator as orch
    from app import onto_workshop as ws
    locks = {id(ws._JOB._lock), id(borch._JOB._lock), id(orch._JOB._lock)}
    assert len(locks) == 3, "三条链的作业锁合并了，table_lock 的前提变了"
    # 而且复用的是同一个函数对象——所以锁必须下沉到表上
    for k, v in borch.RUNNERS.items():
        assert ws.RUNNERS[k] is v, k
    for k, v in orch.RUNNERS.items():
        assert ws.RUNNERS[k] is v, k
