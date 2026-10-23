# -*- coding: utf-8 -*-
"""数据库迁移执行器。

用法（在 服务端/ 下）：
    python -I db/migrate.py              # 应用所有未应用的迁移
    python -I db/migrate.py --status     # 只看状态，不改库
    python -I db/migrate.py --reset      # 删掉所有表重建（仅本地开发，会确认）

为什么不用 Alembic：
  这套 schema 的价值很大一部分在 SQL 注释里——口径说明、踩坑记录、
  为什么某一列取不到数据。Alembic 的 autogenerate 产出的是 Python op 调用，
  注释会在「模型 → 迁移」这一步丢掉，而注释恰恰是最贵的部分。
  手写 SQL + 一个顺序执行器，迁移文件本身就是可读的文档。

幂等性：
  每个迁移文件执行一次就记入 schema_migration。SQL 内部也一律用
  IF NOT EXISTS / CREATE OR REPLACE，所以重跑单个文件也不会炸。
  **迁移文件一旦应用过就不许再改**——改了会与库里的实际结构漂移，
  而执行器只按文件名判断是否应用过，发现不了内容变化。
  所以执行器会记录每个文件的 sha256，内容变了就报错而不是静默跳过。
"""
import argparse
import hashlib
import os
import sys
import time

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
MIG_DIR = os.path.join(HERE, "migrations")

DSN = dict(
    host=os.environ.get("FJHY_PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("FJHY_PG_PORT", "55460")),
    dbname=os.environ.get("FJHY_PG_DB", "fjhy_poc"),
    user=os.environ.get("FJHY_PG_USER", "fjhy"),
    password=os.environ.get("FJHY_PG_PASSWORD", "fjhy_poc_dev_only"),
)

# 五张审计表 → (表名, 时间列)。只有这五张做 hypertable：
# 它们按时间单向增长、永远按时间范围查询、需要保留策略。
# 业务表和规则资产表是会被更新的实体表，做分区只会添麻烦。
AUDIT_HYPERTABLES = [
    ("data_pool_snapshot", "taken_at"),
    ("cycle_log", "started_at"),
    ("agent_signal_log", "emitted_at"),
    ("feedback_log", "observed_at"),
    ("improve_log", "applied_at"),
]

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename    TEXT PRIMARY KEY,
    sha256      TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER
);
ALTER TABLE schema_migration ADD COLUMN IF NOT EXISTS body_sha256 TEXT;
COMMENT ON COLUMN schema_migration.body_sha256 IS
  '剥掉注释之后的 SQL 指纹。sha256 变了但 body_sha256 没变，说明改动只在注释里，'
  '这种改动允许——注释里是口径说明和踩坑记录，不该因为「文件不许动」而停在第一版。';
COMMENT ON COLUMN schema_migration.sha256 IS
  '迁移文件内容指纹。已应用的文件被改动时执行器报错退出，而不是静默跳过——'
  '静默跳过会让库结构与迁移文件永久不一致，且在 code review 里看不出来。';
"""


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()



def strip_sql_comments(sql):
    """去掉 SQL 注释与多余空白，用于判断「改动是否只在注释里」。

    已应用的迁移原则上不许改——改了会让库结构与文件永久不一致。
    但**只改注释**是例外：注释里记的是口径说明和踩坑记录，这类信息会随着
    理解加深不断补充，不该因为「文件不许动」而永远停在第一版。
    放宽的边界是可验证的：把注释剥掉之后字节相同，就说明结构一个字没变。
    """
    out = []
    in_block = False
    for line in sql.split(chr(10)):
        i = 0
        buf = []
        while i < len(line):
            if in_block:
                j = line.find("*/", i)
                if j < 0:
                    i = len(line)
                else:
                    in_block = False
                    i = j + 2
                continue
            if line.startswith("--", i):
                break
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            buf.append(line[i])
            i += 1
        t = "".join(buf).strip()
        if t:
            out.append(" ".join(t.split()))
    return chr(10).join(out)

def migrations():
    if not os.path.isdir(MIG_DIR):
        return []
    return sorted(f for f in os.listdir(MIG_DIR) if f.endswith(".sql"))


def connect():
    return psycopg2.connect(**DSN)


def cmd_status(conn):
    cur = conn.cursor()
    cur.execute("SELECT filename, sha256, applied_at FROM schema_migration ORDER BY filename")
    applied = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    print("迁移目录：%s" % MIG_DIR)
    drift = 0
    for fn in migrations():
        h = sha(os.path.join(MIG_DIR, fn))
        if fn not in applied:
            print("  [ ] %s  待应用" % fn)
        elif applied[fn][0] != h:
            print("  [!] %s  **已应用但文件被改过**（库里 %s… / 文件 %s…）"
                  % (fn, applied[fn][0][:8], h[:8]))
            drift += 1
        else:
            print("  [x] %s  %s" % (fn, applied[fn][1].strftime("%Y-%m-%d %H:%M:%S")))
    for fn in applied:
        if fn not in migrations():
            print("  [?] %s  库里有记录但文件不在了" % fn)
            drift += 1
    return drift


def cmd_apply(conn):
    cur = conn.cursor()
    cur.execute("SELECT filename, sha256, body_sha256 FROM schema_migration")
    _rows = cur.fetchall()
    applied = {r[0]: r[1] for r in _rows}
    applied_body = {r[0]: r[2] for r in _rows}
    todo = []
    for fn in migrations():
        h = sha(os.path.join(MIG_DIR, fn))
        if fn in applied:
            if applied[fn] != h:
                # 只改注释是允许的：剥掉注释后字节相同即证明结构没动。
                recorded = applied_body.get(fn)
                body = strip_sql_comments(open(os.path.join(MIG_DIR, fn), encoding="utf-8").read())
                if recorded is not None and recorded == hashlib.sha256(
                        body.encode("utf-8")).hexdigest():
                    with conn:
                        conn.cursor().execute(
                            "UPDATE schema_migration SET sha256=%s, body_sha256=%s "
                            "WHERE filename=%s", (h, recorded, fn))
                    print("  %s 仅注释变化，已更新指纹（结构未变）" % fn)
                    continue
                print("** 拒绝执行：%s 已应用过，而且**结构**变了（不只是注释）。" % fn)
                print("   已应用的迁移不许改结构——请新增一个迁移文件。")
                return 1
            continue
        todo.append((fn, h))
    if not todo:
        print("没有待应用的迁移。")
        return 0
    for fn, h in todo:
        path = os.path.join(MIG_DIR, fn)
        sql = open(path, encoding="utf-8").read()
        t0 = time.time()
        try:
            # 每个迁移在自己的事务里：失败则整个文件回滚，不留半截结构
            with conn:
                c = conn.cursor()
                c.execute(sql)
                ms = int((time.time() - t0) * 1000)
                body_h = hashlib.sha256(
                    strip_sql_comments(sql).encode("utf-8")).hexdigest()
                c.execute("INSERT INTO schema_migration (filename, sha256, body_sha256, "
                          "duration_ms) VALUES (%s, %s, %s, %s)", (fn, h, body_h, ms))
            print("  应用 %-26s %5d ms" % (fn, int((time.time() - t0) * 1000)))
        except Exception as exc:
            print("** %s 执行失败，已回滚：" % fn)
            print("   %s" % str(exc).strip().split("\n")[0])
            return 1
    return 0


def cmd_hypertables(conn):
    """把五张审计表转成 hypertable。没有 timescaledb 就明确说一句，不静默。"""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    if not cur.fetchone():
        print("  未启用 hypertable：本库没有 timescaledb 扩展，五张审计表保持普通表。")
        print("  （功能不受影响，只是没有自动分区与保留策略。）")
        return 0
    done, skipped = 0, 0
    for table, tcol in AUDIT_HYPERTABLES:
        cur.execute("SELECT 1 FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = %s", (table,))
        if cur.fetchone():
            skipped += 1
            continue
        with conn:
            c = conn.cursor()
            # migrate_data => TRUE：允许表里已有数据时转换
            c.execute("SELECT create_hypertable(%s, %s, if_not_exists => TRUE, "
                      "migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days')",
                      (table, tcol))
        done += 1
    print("  hypertable：新转换 %d 张，已是 hypertable 的 %d 张（分区间隔 7 天）" % (done, skipped))
    return 0


def cmd_reset(conn):
    print("** 这会删掉 fjhy_poc 库 public schema 下的全部表。")
    print("   只在本地开发用。容器名 fjhy-poc-sdafi-pg、库名 fjhy_poc，")
    print("   都带本 POC 标记，不会碰到 fjpg_audit_pg / sdafi-base-pg / tf_ai_pg。")
    if os.environ.get("FJHY_RESET_CONFIRM") != "yes":
        print("   需要显式确认：设 FJHY_RESET_CONFIRM=yes 再跑。")
        return 1
    with conn:
        c = conn.cursor()
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute("GRANT ALL ON SCHEMA public TO %s" % DSN["user"])
    print("   已重建 public schema。")
    return 0


def summary(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    tables = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM information_schema.views WHERE table_schema='public'")
    views = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'
    """)
    idx = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public' AND c.contype = 'c'
    """)
    checks = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public' AND c.contype = 'f'
    """)
    fks = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_description d "
                "JOIN pg_class c ON c.oid = d.objoid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname='public'")
    comments = cur.fetchone()[0]
    print()
    print("库内现状：表 %d · 视图 %d · 索引 %d · CHECK 约束 %d · 外键 %d · 注释 %d"
          % (tables, views, idx, checks, fks, comments))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--no-hypertable", action="store_true")
    args = ap.parse_args()

    try:
        conn = connect()
    except Exception as exc:
        print("** 连不上数据库：%s" % str(exc).strip().split("\n")[0])
        print("   先起容器：docker compose -f ../docker-compose.yml up -d")
        print("   当前 DSN：host=%s port=%s db=%s user=%s"
              % (DSN["host"], DSN["port"], DSN["dbname"], DSN["user"]))
        return 1

    with conn:
        conn.cursor().execute(BOOTSTRAP)

    if args.reset:
        rc = cmd_reset(conn)
        if rc:
            return rc
        with conn:
            conn.cursor().execute(BOOTSTRAP)

    if args.status:
        drift = cmd_status(conn)
        summary(conn)
        return 1 if drift else 0

    rc = cmd_apply(conn)
    if rc:
        return rc
    if not args.no_hypertable:
        cmd_hypertables(conn)
    cmd_status(conn)
    summary(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
