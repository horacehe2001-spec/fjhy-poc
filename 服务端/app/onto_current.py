# -*- coding: utf-8 -*-
"""「当前本体版本是哪一版」——**全站唯一入口**。

迁移 0044/0045 之后本体按实例隔离，`ontology_version` 里每个实例各有一条
`is_current`。于是这个一直写在各处的子查询：

    SELECT version FROM ontology_version WHERE is_current

从「返回一行」变成「返回 N 行」。它出现在 20 多处，其中几处是标量子查询，
PG 直接报：

    CardinalityViolation: more than one row returned by a subquery
    used as an expression

实测是 /api/asset/nav 整个 500 掉了，而 test_mask_outlets 把它当成
「响应没经过脱敏中间件」报出来——**报错信息和真因差着十万八千里**。

**所以做成一个函数，不是在 20 处各加一个 AND instance_id=%s。**
各加一遍的后果今天已经见过两次（冲突闸、取文闸）：两处写法迟早分叉，
而分叉的表现是一个对不上的数字，没有任何报错。
"""
from . import db, scope


def version(instance_id=None):
    """当前实例的当前本体版本号。没有就返回 None，**不兜底、不取别人的**。"""
    iid = scope.require(instance_id)
    return db.scalar("""SELECT version FROM ontology_version
                         WHERE instance_id = %s AND is_current""", (iid,))


def row(instance_id=None, cols="version, source_hash, loaded_at, stats"):
    """当前本体版本那一行。cols 由调用方给，默认是最常用的四列。

    cols 直接拼进 SQL：它**只来自代码里的字面量**，不接受外来输入。
    接受外来输入就是注入口子，所以这个参数永远不许从请求里传进来。
    """
    iid = scope.require(instance_id)
    return db.query_one("SELECT %s FROM ontology_version "
                        "WHERE instance_id = %%s AND is_current" % cols, (iid,))


def require_version(instance_id=None):
    """取不到就抛，给那些「没有本体就不该往下走」的调用方用。"""
    iid = scope.require(instance_id)
    v = version(iid)
    if not v:
        raise RuntimeError(
            "实例「%s」没有当前本体版本。先跑第②步「装载本体源文件」——"
            "本体已按实例隔离（0044），每个实例装自己那份 YAML。" % iid)
    return v
