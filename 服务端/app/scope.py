# -*- coding: utf-8 -*-
"""当前实例归属：**全站只认这一份**。

为什么单独一个模块：0040/0041 之后，rule / release / lookup_table / ontology /
expense_bill 等几十张表都带了 instance_id，于是"当前是哪个实例"这个判断会出现在
几乎每个查询里。这类判断一旦有两份实现就会分叉——engine.effective_release()
的注释已经为同类问题付过一次学费（生效版本的判定原先有两份，一份取"最新一版"
一份取"最新已发布版"，结果页面显示的版本不是引擎在用的那一版，而版本号要写进
cycle_log 用于复现历史结论，标错等于复现不了）。

与 api/domain.py 的关系：**domain.current() 仍然是唯一的真相来源**，这里只是
它的薄封装，多做两件事——校验实例真的存在、拿不到就抛（而不是挑一个默认值）。
刻意反向 import（核心模块引 api 层）的理由见 doclib.current_instance() 的注释：
domain.current() 里有"实例是否属于当前领域"的一致性校验，复制那段比反着引更糟。

与 doclib.py 的关系：doclib 在 0034 时就自己实现了一份（它是第一个真按实例过滤
的模块）。本模块落地后，doclib.resolve_instance() 改为委托到这里，只保留它自己的
异常类型（api/doclib.py 按那个类型回 400）。所以实现仍然只有一份。
"""


class ScopeError(Exception):
    """没选实例 / 实例不存在。

    调用方应当回 400 而不是 500：这是"请先去选实例"，不是程序出错。
    """


def current_instance():
    """当前实例 id，没选返回 None。"""
    from .api.domain import current as _cur
    return _cur().get("instance_id")


def resolve(instance_id=None):
    """传了就用传的，没传用当前的。**都没有就抛，不挑默认值。**

    挑默认值的后果（0034 已经实测过一次）：数据落到了另一个实例名下，
    而页面按当前实例过滤，于是"写成功了但清单里看不到"——
    这种现象要翻库才查得出来，比当场报错贵得多。

    返回 {instance_id, domain_id, instance_label, domain_label}。
    """
    from . import db
    iid = (instance_id or "").strip() or current_instance()
    if not iid:
        raise ScopeError("还没有选定实例。请先在「领域知识资产」页选好领域与实例——"
                         "规则、本体、单据都必须有归属，没有归属的资产"
                         "不知道该算哪个实例的。")
    row = db.query_one("""SELECT i.instance_id, i.domain_id, i.label AS instance_label,
                                 d.label AS domain_label
                            FROM domain_instance i
                            JOIN domain d ON d.domain_id = i.domain_id
                           WHERE i.instance_id = %s""", (iid,))
    if not row:
        raise ScopeError("实例不存在：%s" % iid)
    return row


def require(instance_id=None):
    """只要 id。查询拼 WHERE instance_id = %s 时用这个。

    **显式传了就直接用，不回库校验。** 两个理由：
      · 这是**内部调用之间传递已知归属**的通道（L0 把范围定死后传给下游、
        engine 把实例传给 load_lookups），再查一次库只是重复问同一个问题；
        这些路径一次业务动作会走几十遍，每遍一个来回。
      · 校验属于**边界**：外来的 instance_id（接口查询参数）要走 resolve()，
        它会确认实例真的存在；内部传递的不需要。原先这里一律 resolve()，
        结果是拿桩替掉数据库的单测全部崩在"实例不存在"上——
        而那些测试验的是三级阶梯和增量范围，和实例是否存在毫无关系。
    没传的时候才去解析当前实例（那一条路必须查库，也必须校验）。
    """
    iid = (instance_id or "").strip()
    return iid or resolve(None)["instance_id"]
