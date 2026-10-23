#!/bin/sh
# 容器启动序列，对齐 scripts/start.ps1 的顺序（README「怎么跑起来」）。
# 这台服务器是演示部署：只装载规则资产/本体/演示数据，不跑 app/demo/seed.py
# ——那一步会把客户的 40 张真实报销单明细灌进这台多租户共享机器，按约定跳过。
set -e

mkdir -p var/uploads var/artifacts var/doclib var/attachments var/ocr var/log var/run

echo "[entrypoint] 等数据库就绪"
python -I - <<'PY'
import os, time, psycopg2
dsn = dict(host=os.environ["FJHY_PG_HOST"], port=int(os.environ["FJHY_PG_PORT"]),
           dbname=os.environ["FJHY_PG_DB"], user=os.environ["FJHY_PG_USER"],
           password=os.environ["FJHY_PG_PASSWORD"])
for i in range(60):
    try:
        psycopg2.connect(**dsn).close()
        break
    except Exception as e:
        print(f"  等待中（{i+1}/60）：{e}")
        time.sleep(2)
else:
    raise SystemExit("数据库 60 次重试后仍不可用")
PY

echo "[entrypoint] 建表（迁移）"
python -I db/migrate.py

echo "[entrypoint] 装载本体"
python -I app/ontology/loader.py

echo "[entrypoint] 装载规则资产"
python -I app/rules/loader.py

echo "[entrypoint] 装载国内厂商预置"
python -I app/llm/presets.py

echo "[entrypoint] 生成演示数据（19 张，不含客户真实样本）"
python -I app/demo/generator.py

echo "[entrypoint] 启动 uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
