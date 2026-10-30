# -*- coding: utf-8 -*-
"""给 822港航 那 74 条制度抽取规则补登「判定需要哪些业务数据项」。

为什么要补：`app/rule_feasibility.py` 判一条规则能不能真的跑，靠的是它
登记的数据项。14 条反向抽取规则当时记了，这 74 条没记，于是全部停在
「未登记数据项」——而**「未登记」和「判不了」是两回事**：前者是我们还没
过这一关，后者是过了没过。混在一起就说不清 88 条里到底评估了多少。

数据项由模型读规则的判定逻辑抽出来（型号见 JSON 里的 model），
**有没有这些数据不问模型**——那由 data_availability 去库里实测。

两件必须知道的事：
  1. 模型回的 rule_id 是「TR-会议 -01」，**连字符前多一个空格**。
     抽取脚本有一道「rule_id 必须和库里一一对上」的核对，当场抓到了。
     JSON 里存的是规范化之后的编号，并在 note 字段写明做过这个处理。
     不加那道核对，这批数据会静默地一条都对不上，而写库不报错。
  2. 模型按要求把词表外的数据项写成「其他：<名字>」，回来 23 个。
     其中只有 4 个库里有字段，**另外 19 个连表都没有**——采购/招标/
     供应商/合同/租房/备用金在这个 schema 里不存在。那不是缺数据，
     是缺数据模型。NEED_MAP 里对这 19 个标了 NO_FIELD。

写到 rule.note 的「判定需要的数据项：」那一行，和 14 条那批同一个格式，
这样 rule_feasibility.need_items() 一套代码读两批。
可重入：已有这一行就替换，不追加。
用法：python -m app.demo.load_822gh_need_fields
      **不要加 -I**：隔离模式会把 cwd 从 sys.path 摘掉，import app 会失败。
"""
import io
import json
import os

from .. import db

INSTANCE = "822gh"
LINE_PREFIX = "判定需要的数据项："
_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "822gh_need_fields.json")


def _merge_note(note, items):
    """把数据项那一行塞进 note。**已有就替换，不追加**——追加会让同一条
    规则出现两行「判定需要的数据项」，而 need_items() 只认第一行，
    于是库里写着新的、程序读到的是旧的。"""
    line = LINE_PREFIX + "、".join(items)
    lines = [l for l in (note or "").splitlines()]
    for i, l in enumerate(lines):
        if l.startswith(LINE_PREFIX):
            lines[i] = line
            return "\n".join(lines)
    lines.append(line)
    return "\n".join([l for l in lines if l.strip()])


def load():
    d = json.load(io.open(_JSON, encoding="utf-8"))
    rows = {r["rule_id"]: r["note"] for r in db.query(
        """SELECT rule_id, note FROM rule
            WHERE instance_id = %s AND version = 1
              AND created_by = 'ai_draft_bulk'""", (INSTANCE,))}
    unknown = [g["rule_id"] for g in d["rules"] if g["rule_id"] not in rows]
    assert not unknown, ("这些编号在库里找不到，**不要写**：%s。"
                         "模型改过编号格式，先核对再装。" % unknown)
    missing = sorted(set(rows) - {g["rule_id"] for g in d["rules"]})
    n = 0
    for g in d["rules"]:
        items = [x.strip() for x in g["need_fields"] if x.strip()]
        if not items:
            continue
        db.execute("""UPDATE rule SET note = %s
                       WHERE instance_id = %s AND rule_id = %s AND version = 1""",
                   (_merge_note(rows[g["rule_id"]], items), INSTANCE,
                    g["rule_id"]))
        n += 1
    return {"written": n, "model": d["model"], "no_need_fields": missing}


def main():
    r = load()
    print("补登 %d 条（型号 %s）" % (r["written"], r["model"]))
    if r["no_need_fields"]:
        print("**这些规则没拿到数据项，仍是未登记**：%s" % r["no_need_fields"])
    n = db.scalar("""SELECT count(*) FROM rule
                      WHERE instance_id = %s AND version = 1
                        AND note LIKE %s""", (INSTANCE, "%" + LINE_PREFIX + "%"))
    tot = db.scalar("SELECT count(*) FROM rule WHERE instance_id = %s AND version = 1",
                    (INSTANCE,))
    print("822港航 已登记数据项的规则 %d / %d 条" % (n, tot))


if __name__ == "__main__":
    main()
