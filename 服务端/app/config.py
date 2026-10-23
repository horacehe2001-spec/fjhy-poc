# -*- coding: utf-8 -*-
"""运行配置。

所有可变项都走环境变量，默认值是本地开发用的那一套。
**密码之类的默认值只在本地有效**：容器 fjhy-poc-sdafi-pg 只绑 127.0.0.1，
不对外暴露；部署时必须从环境注入，不要依赖这里的默认值。
"""
import os

# ── 项目路径 ─────────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(APP_DIR)
PROJECT_ROOT = os.path.dirname(SERVER_DIR)

# 规则资产的四个手写文件 + 两个生成物，都在项目根下
ONTOLOGY_YAML = os.path.join(PROJECT_ROOT, "本体_v0.1.yaml")
RULES_JSON = os.path.join(PROJECT_ROOT, "规则清单_v0.1.json")
BINDING_JSON = os.path.join(PROJECT_ROOT, "规则绑定_v0.1.json")
FACT_MODEL_JSON = os.path.join(PROJECT_ROOT, "事实模型_v0.1.json")
ASSET_DIR = os.path.join(PROJECT_ROOT, "规则资产")
STANDARDS_YAML = os.path.join(ASSET_DIR, "标准表_v0.1.yaml")
ATTACHMENT_TABLE_YAML = os.path.join(ASSET_DIR, "必要附件决策表_v0.1.yaml")
KEYWORDS_YAML = os.path.join(ASSET_DIR, "敏感词表_v0.1.yaml")
INTERNAL_UNITS_YAML = os.path.join(ASSET_DIR, "内部单位名单_v0.1.yaml")

AGENT_CONFIG_DIR = os.path.join(SERVER_DIR, "config", "agents")
UPLOAD_DIR = os.path.join(SERVER_DIR, "var", "uploads")
ARTIFACT_DIR = os.path.join(SERVER_DIR, "var", "artifacts")

# ── 制度文件库的托管存储区 ───────────────────────────────────────────────────
# 客户端上传的制度文件落在这里，按 {领域id}/{实例id}/ 分目录（迁移 0034）。
# 目录名用 id 不用中文展示名：展示名允许改，改了磁盘目录不会跟着改，abs_path 就对不上。
#
# **这是本系统唯一允许写入的文件库目录。** doc_lib_root.kind='引用' 的那些
# （客户的资料目录）一个字节都不写，见 0015 与 0034 的列注释。
DOCLIB_DIR = os.path.join(SERVER_DIR, "var", "doclib")

# ── 单据附件的存储区 ─────────────────────────────────────────────────────────
# 从 xlsx 内嵌图片抽出来的单据附件落在这里，按 {领域id}/{实例id}/{单据号}/ 分目录。
#
# **刻意和 DOCLIB_DIR 分开，不混进制度文件库的托管区**（迁移 0035 的决策）：
# 制度文件库是**规则的上游**（文件 → 条款 → 规则，要能溯源）；
# 单据附件是**业务数据**（某一张报账单的凭证）。两者混在一个目录下之后，
# 「规则是从哪堆资料里抽的」就再也说不清，而制度文件库的文件数、
# 「待抽取」、溯源统计也会被几百张发票图稀释。
ATTACH_DIR = os.path.join(SERVER_DIR, "var", "attachments")


def _env(key, default):
    return os.environ.get(key, default)


# ── 上传闸门 ─────────────────────────────────────────────────────────────────
# 放在 _env 之后，不是放在上面的路径段里：这两个值要能从环境覆盖，
# 而 _env 在那时候还没定义（第一版就写在上面，启动直接 NameError）。
#
# 单份大小上限取 200MB，与 doclib.MAX_OOXML_BYTES 对齐——docx/xlsx 的体积
# 主要是内嵌图片，制度文件带扫描图附件很容易过百兆，卡在 50MB 会把真实
# 制度文件挡在门外。
MAX_UPLOAD_BYTES = int(_env("FJHY_MAX_UPLOAD_BYTES", str(200 << 20)))
# 单次份数上限。整个文件夹拖进来时一次几百份是常态，但没有上限的话
# 一次拖一整个盘能把服务拖死。
MAX_UPLOAD_FILES = int(_env("FJHY_MAX_UPLOAD_FILES", "200"))


# ── 数据库 ───────────────────────────────────────────────────────────────────
PG_HOST = _env("FJHY_PG_HOST", "127.0.0.1")
PG_PORT = int(_env("FJHY_PG_PORT", "55460"))
PG_DB = _env("FJHY_PG_DB", "fjhy_poc")
PG_USER = _env("FJHY_PG_USER", "fjhy")
PG_PASSWORD = _env("FJHY_PG_PASSWORD", "fjhy_poc_dev_only")

DSN = dict(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD)
ASYNC_DSN = "postgresql://%s:%s@%s:%d/%s" % (PG_USER, PG_PASSWORD, PG_HOST, PG_PORT, PG_DB)

# ── SDAFI 运行时 ─────────────────────────────────────────────────────────────
# 三种触发的节奏。白皮书给的量级：消息毫秒~秒、订阅秒~分、心跳 30 秒~5 分。
HEARTBEAT_SECONDS = int(_env("FJHY_HEARTBEAT_SECONDS", "30"))
SIGNAL_CLEANUP_SECONDS = int(_env("FJHY_SIGNAL_CLEANUP_SECONDS", "300"))
RETENTION_SECONDS = int(_env("FJHY_RETENTION_SECONDS", "3600"))
# 孤儿信号回收：claimed 超过这个时长还没 done，说明处理进程崩了
CLAIM_TIMEOUT_SECONDS = int(_env("FJHY_CLAIM_TIMEOUT_SECONDS", "600"))
MAX_CONCURRENT_CYCLES = int(_env("FJHY_MAX_CONCURRENT_CYCLES", "3"))

# ── 契约校验 ─────────────────────────────────────────────────────────────────
# Fail Fast：启动时校验不过就不启动，不做部分启动。
# 唯一的放宽口子是显式设成 "warn"，给排障用，生产不许开。
CONTRACT_MODE = _env("FJHY_CONTRACT_MODE", "fail_fast")   # fail_fast | warn

# ── 脱敏 ─────────────────────────────────────────────────────────────────────
# 默认开。关掉需要显式设成 "off"，而且会在启动日志里打一行醒目的警告。
# 这个开关只给单元测试用来验证「关掉之后确实不脱」，生产永远不要动它。
MASK_MODE = _env("FJHY_MASK_MODE", "on")                  # on | off

# ── 演示数据 ─────────────────────────────────────────────────────────────────
# 演示数据的单据号前缀。一眼可辨、事后好清理（全局纪律第 12 条）。
DEMO_BILL_PREFIX = "FJHY-DEMO-"

# ── 版本 ─────────────────────────────────────────────────────────────────────
APP_VERSION = "0.1.0"
