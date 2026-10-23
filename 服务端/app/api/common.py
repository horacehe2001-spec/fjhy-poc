# -*- coding: utf-8 -*-
"""API 公共件：分页、错误、演示数据标记。"""
from fastapi import HTTPException
from pydantic import BaseModel

from .. import config, db


class Page(BaseModel):
    total: int
    page: int
    size: int
    items: list


def paginate(sql_count, sql_rows, params=(), page=1, size=20):
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    total = db.scalar(sql_count, params) or 0
    rows = db.query(sql_rows + " LIMIT %s OFFSET %s", tuple(params) + (size, (page - 1) * size))
    return {"total": total, "page": page, "size": size, "items": rows}


def must_exist(obj, what):
    if not obj:
        raise HTTPException(status_code=404, detail="%s 不存在" % what)
    return obj


def demo_banner():
    """演示数据的显著标注。

    全局纪律第 12 条：演示数据混进真实环境会造成困惑。
    所有列表接口都带这个字段，前端据此打标——
    「这条结论是跑真实单据得出的还是跑编的数据得出的」必须一眼可辨。
    """
    n_demo = db.scalar("SELECT count(*) FROM expense_bill WHERE is_demo") or 0
    n_real = db.scalar("SELECT count(*) FROM expense_bill WHERE NOT is_demo") or 0
    return {"demo_bills": n_demo, "real_bills": n_real,
            "prefix": config.DEMO_BILL_PREFIX,
            "note": "带 %s 前缀、is_demo=true 的是合成的演示数据；其余是客户真实单据"
                    % config.DEMO_BILL_PREFIX}
