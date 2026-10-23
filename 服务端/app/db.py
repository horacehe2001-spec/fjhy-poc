# -*- coding: utf-8 -*-
"""数据库访问。同步用 psycopg2，异步用 asyncpg。

为什么两套都要：
  · 同步：装载器、迁移、离线脚本。它们是一次性批处理，用同步写最省心。
  · 异步：SDAFI 运行时。三种触发里「消息」和「订阅」靠 PG LISTEN/NOTIFY，
    而 LISTEN 要占住一条连接长期等待——同步连接池在这件事上会把一条连接
    永久挂住，异步才能在同一个事件循环里既等通知又跑周期。

**不引入 ORM。** 表结构的价值很大一部分在 SQL 注释里（口径说明、踩坑记录），
ORM 模型类装不下这些，而且会诱导「改模型 → autogenerate 迁移」的流程，
那一步会把注释抹掉。这里一律写 SQL。
"""
import contextlib
import threading

import psycopg2
import psycopg2.extras

from . import config

_async_pool = None


# ── 同步 ─────────────────────────────────────────────────────────────────────
def connect():
    conn = psycopg2.connect(**config.DSN)
    conn.autocommit = False
    return conn


@contextlib.contextmanager
def tx():
    """一个事务。出异常回滚。

    A 阶段是唯一允许写库的阶段，而「写业务表」和「写 agent_signal_log」
    必须在同一个事务里——否则会出现「业务数据变了但审计记录没留下」，
    而 Audit-by-Design 的全部价值就在于这两者一致。
    """
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def cursor(dict_rows=True):
    conn = connect()
    try:
        with conn:
            factory = psycopg2.extras.RealDictCursor if dict_rows else None
            cur = conn.cursor(cursor_factory=factory)
            yield cur
    finally:
        conn.close()


def query(sql, params=None):
    with cursor() as cur:
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql, params=None):
    with cursor(dict_rows=False) as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return row[0] if row else None


def execute(sql, params=None):
    with cursor(dict_rows=False) as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


# ── 按表粒度的互斥闸 ─────────────────────────────────────────────────────────
# **为什么需要这道闸：几条链共用同一批 runner，而锁是各自的。**
# 本体工场复用了「本体建设」「统一口径」两条链的 7 个执行单元（同一函数对象），
# 三条链各有自己的 AgentJob 锁，所以「工场一键搭建」和「本体建设运行」
# 可以同时点下去。而这些 runner 做的是**整表清空后重建**
# （DELETE FROM onto_candidate / onto_align / onto_rule_cover）。
#
# 实测（2026-10-20，单跑一次候选链，50ms 采样 58 次）：
#   采样到的最小值 = 0        ← DELETE 已提交，表真的空过
#   低于最终值的采样 = 21 次  ← 36% 的时刻读到的是偏小的数
# 因为 execute() 每次自开连接、退出即提交，所以 save() 是
# 「DELETE 一个事务提交 → N 条 INSERT 各自提交」，中间是敞开的。
# 两条链同时跑：A 删完插到一半，B 又删一次，最终台账是两次运行的混合，
# 而 INSERT 带 ON CONFLICT DO NOTHING，冲突被吞掉——**不报错**。
#
# 闸放在做破坏性写入的函数里，不放 API 层：命令行入口也绕不过，
# 和「闸在装载器身上」那条既有口径一致。

# 固定键号，**不用 hashtext**：
#   1) hashtext 返回 int4、可能为负，而 pg_locks.objid 是无符号 oid，
#      要做位运算才对得上。映射写错不报错，只是查不到持有者——
#      一个报不出持有者的冲突提示，和没有提示差不多。
#   2) 两张表哈希撞了会**静默互斥**：功能看着是好的，只是偶尔莫名拒绝，
#      这种 bug 排查起来极贵。
# 新增表在这里加一行。**键号不许改**——改了等于换了一把锁，
# 旧版本进程还按老键号锁着，两边就互相看不见了。
_LOCK_NS = 0x666A6879                      # 'fjhy'，和别的库共用一个 PG 时不撞键空间
LOCK_KEYS = {
    "onto_candidate": 1,                   # 候选台账，onto_build.save()
    "onto_align": 2,                       # 正向对账，onto_coord.save_align()
    "onto_rule_cover": 3,                  # 反向覆盖，onto_coord.save_align()
    "rule": 4,                             # 规则资产一组，rules/loader.load_all()
    "ontology": 5,                         # ontology_* / process_* 一组，装载器整组重建
}


class TableBusy(RuntimeError):
    """这张表正被另一条链整表重算。**拒绝，不排队。**"""


# 进程内的持有者登记表：键号 → 人看得懂的名字。
# **为什么不靠 application_name 报持有者**：PG 会把它里面的非 ASCII 字节
# 逐个转义成 `\xNN`，再按 63 字节截断——「候选重算」变成
# `fjhy:\xe6\xb5\x8b\xe8\xaf\x95-\xe5\x8d\xa0\xe9\x94\x81\xe8\x80\`，
# 还断在半个字符上。实测踩到（test_报得出谁占着 当场红）。
# 而真实冲突场景本来就是同进程：三条链都跑在同一个 uvicorn 里。
# 跨进程（命令行装载 vs 服务）才退回 application_name，那时只有 ASCII 标签。
_HELD = {}
_HELD_GUARD = threading.Lock()

_ASCII_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:- ")


def _ascii_tag(who, names):
    """application_name 只放 ASCII。中文在这里是留不住的，不如不放。

    **残留太短就改用表名。** who 几乎全是中文时，ASCII 残留可能只剩一个
    字母——实测跨进程占锁报出来是「fjhy:A」，这种持有者名毫无用处。
    所以四个真实落点的 who 都带 ASCII 函数名（rules.loader.load_all 之类），
    而万一有人传了纯中文，退回表名也比一个字母强。
    """
    s = "".join(c for c in str(who) if c in _ASCII_OK).strip(" -:")
    if len(s) < 4:
        s = ",".join(names)
    return ("fjhy:" + s)[:63]


def _lock_holder(cur, key):
    """谁占着这把锁。先问进程内登记表（中文完整），再退回 PG。"""
    with _HELD_GUARD:
        who = _HELD.get(key)
    if who:
        return who
    try:
        cur.execute("""SELECT a.application_name
                         FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
                        WHERE l.locktype = 'advisory' AND l.granted
                          AND l.classid = %s AND l.objid = %s AND l.objsubid = 2
                        LIMIT 1""", (_LOCK_NS, key))
        row = cur.fetchone()
        if not row:
            return None
        # 走到这里说明不是本进程占的（命令行装载 vs 服务），
        # 只有 ASCII 标签可报。**不加「另一进程」之类的断言**：
        # 登记表写入和取锁成功之间有微秒级窗口，那一瞬间同进程也会走到这里，
        # 说成「另一进程」就是在报一件没核实的事。
        return (row[0] or "").strip() or None
    except Exception:                                          # noqa: BLE001
        return None


@contextlib.contextmanager
def table_lock(*tables, **kw):
    """整表重算期间占住这几张表，别的链不许同时重算同一张。

    **不能用 execute() 取这把锁**：advisory lock 是 session 级的，
    而 execute() 每次自开连接、退出即关——锁跟着连接一起没了，
    看着取到了，实际上下一行代码就已经没锁了。所以这里专占一条连接，
    用 with 的生命周期把锁按住。

    **用 try_ 不用阻塞版。** 阻塞会让「本体建设」页点下去一直转，
    而人看不出是在等别人、还是卡死了。拒绝并报出谁占着，比转圈强。

    **锁名排序后再取。** try_ 是非阻塞的，所以防的**不是死锁**——
    是「两边各占一张、然后都失败」。锁集合有交集而取序相反时
    （A 先拿 onto_align、B 先拿 onto_rule_cover），两边都拿不全、
    两次重算都白跑。排序保证全局一致的获取顺序，让其中一个干净地赢。
    """
    who = kw.pop("who", "?")
    if kw:
        raise TypeError("table_lock() 不认的参数：%s" % sorted(kw))
    unknown = [t for t in tables if t not in LOCK_KEYS]
    if unknown:
        # 名字写错了要当场炸。静默不加锁 = 以为有闸而其实没有，比没闸更坏。
        raise KeyError("LOCK_KEYS 里没有这些表：%s" % unknown)
    names = sorted(set(tables))
    conn = psycopg2.connect(**config.DSN)
    conn.autocommit = True                 # 取/放锁不走事务，否则 with conn 一提交就没了
    got = []
    try:
        cur = conn.cursor()
        cur.execute("SET application_name = %s", (_ascii_tag(who, names),))
        for t in names:
            key = LOCK_KEYS[t]
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NS, key))
            if not cur.fetchone()[0]:
                holder = _lock_holder(cur, key)
                raise TableBusy(
                    "表 %s 正在被重算%s，这一次不做。"
                    "两条链同时整表重算会把台账写成两次运行的混合，而且不报错。"
                    % (t, "（%s）" % holder if holder else ""))
            got.append((t, key))
            with _HELD_GUARD:
                _HELD[key] = str(who)
        yield names
    finally:
        # 先从登记表里摘掉再放锁：反过来的话会有一个窗口——
        # 锁已经放了而登记表还写着旧持有者，下一个调用方拿到锁却被报成「忙」。
        with _HELD_GUARD:
            for _, key in got:
                _HELD.pop(key, None)
        # 连接一关，session 级的锁本来就全放了；这里显式放一遍，
        # 好让 pg_locks 立刻干净——排查时看到的才是当下的真实占用。
        try:
            cur = conn.cursor()
            for _, key in got:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NS, key))
        except Exception:                                      # noqa: BLE001
            pass
        conn.close()


# ── 异步 ─────────────────────────────────────────────────────────────────────
async def async_pool():
    global _async_pool
    if _async_pool is None:
        import asyncpg
        _async_pool = await asyncpg.create_pool(
            config.ASYNC_DSN, min_size=2, max_size=max(4, config.MAX_CONCURRENT_CYCLES * 2))
    return _async_pool


async def async_close():
    global _async_pool
    if _async_pool is not None:
        await _async_pool.close()
        _async_pool = None


async def listen_connection():
    """专门用于 LISTEN 的独立连接，**不从池里拿**。

    从池里拿会出问题：池会在空闲时回收连接，而 LISTEN 注册是连接级的，
    连接一回收订阅就静默失效——没有报错、没有日志，只是从此收不到通知。
    这正是心跳触发不是可选项的原因之一：它兜住这种静默失效。
    """
    import asyncpg
    return await asyncpg.connect(config.ASYNC_DSN)


def healthcheck():
    """返回 (ok, 详情)。给 /health 和启动自检用。"""
    try:
        with cursor() as cur:
            cur.execute("SELECT current_database() AS db, current_user AS usr, now() AS ts")
            row = dict(cur.fetchone())
            cur.execute("SELECT count(*) AS n FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_type='BASE TABLE'")
            row["tables"] = dict(cur.fetchone())["n"]
            cur.execute("SELECT extname FROM pg_extension WHERE extname='timescaledb'")
            row["timescaledb"] = cur.fetchone() is not None
            cur.execute("SELECT count(*) AS n FROM schema_migration")
            row["migrations"] = dict(cur.fetchone())["n"]
        return True, row
    except Exception as exc:
        return False, {"error": str(exc).strip().split("\n")[0]}
