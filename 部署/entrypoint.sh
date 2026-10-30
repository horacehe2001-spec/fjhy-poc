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

# 真实单必须在演示数据**之前**装：seed.py 的 clear() 是无条件
# `DELETE FROM expense_bill`（不带 is_demo 条件），跑在 generator 后面
# 会把刚生成的 29 张演示单连同它们的附件一起删光。start.ps1 第 286-287 行
# 就是这个顺序，照搬。seed.py 自带 clear，重启重跑是幂等的。
# 材料/ 由 compose 只读绑挂进来；没挂上就跳过这步，并说清后果。
if [ -d /app/材料/单据明细表与审批意见 ]; then
  echo "[entrypoint] 装载客户 40 张真实单（含审批意见 PDF）"
  python -I app/demo/seed.py || echo "  !! 真实单没装上，工作台只会有演示单"
else
  echo "[entrypoint] !! /app/材料 没挂上，跳过真实单装载——工作台只会有演示单"
fi

echo "[entrypoint] 生成演示数据（29 张，不含客户真实样本）"
python -I app/demo/generator.py

# seed.py 的 clear() 把 attachment 表整张删了，而 var/attachments/ 下的
# 图片文件不跟着删——不重抽一次，页面上就是「40 张单全都没有附件」，
# 和已被推翻的缺口 G-29 长得一模一样。extract_xlsx 是幂等的，
# 图已在盘上时只补登记。只抽图不判类型（类型要读图，要模型）。
if [ -d /app/材料/单据明细表与审批意见 ]; then
  echo "[entrypoint] 抽 xlsx 内嵌的真实附件图（219 张）"
  python -I -c "import sys,os;sys.path.insert(0,os.getcwd());from app.perception import attachment_extract as ax;import app.config as C;d=os.path.join(C.PROJECT_ROOT,'材料','单据明细表与审批意见');[print(ax.extract_xlsx(os.path.join(d,f))) for f in sorted(os.listdir(d)) if f.endswith('.xlsx') and not f.startswith('~$')]" || echo "  !! 附件图没抽成，真实单会显示没有附件"
fi

# generator 每次都先 clear 再生成，连演示单的 review_run / review_finding
# 一起删。不在这里补跑一轮，容器一重启页面上的审核结果就全空——
# 实测过：重启后 review_run 从 29 掉到 0。start.ps1 第 289 行做的就是这件事，
# 之前只有本地做了，服务器没做。
# 只跑规则引擎，不调大模型（模型补判留给页面上的「智能审核」按钮，要花钱）。
# 跑不成不让容器起不来：演示环境宁可少一屏数据，也不要一个起不来的后端，
# 所以失败只打一行可见的话，由人去点一次按钮补。
echo "[entrypoint] 跑一轮审核（规则引擎，不调模型）"
python -I -c "import sys,os;sys.path.insert(0,os.getcwd());from app.runtime.orchestrator import Orchestrator;Orchestrator(verbose=False).startup().heartbeat_once(limit=300)" || echo "  !! 审核没跑成，页面会显示 0 条审核结果，去页面上点一次「智能审核」"

echo "[entrypoint] 启动 uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
