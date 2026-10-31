# -*- coding: utf-8 -*-
r"""把 822港航 已经写了实现的规则标成「已编译 / 演示可执行」。

用法（**不要加 -I**）：

    cd 服务端
    python -m app.demo.compile_822gh_rules          # 干跑
    WRITE=1 python -m app.demo.compile_822gh_rules  # 真写库（可重入）

**可执行性从注册表推，不手工维护一张名单。**
福建海运集团那边 demo_executable 来自 规则绑定_v0.1.json（gen_bind.py 按
概念可得性算出来的）；822港航 没有那个文件，所以这里用一个更直接的判据：
    `app/rules/impl_822gh.py` 里用 @rule 注册了实现的 → 可执行
加一条实现、重跑一次就生效；删掉一条实现、重跑一次就撤销。
手工维护名单的话，实现和名单迟早对不上，而对不上的表现是
「契约说该跑、代码里没有」或者反过来「写了实现但从来没跑过」——
后者尤其隐蔽，因为它什么都不报。

【绑哪个流程节点】
  一律绑 **P01 提单**。这 7 条判的都是「提单时能不能拦住」。
  **不绑 P09 共享审核**：那是人看的节点，规则在那里才跑等于没拦住，
  单据已经流到人手里了。
  哪天有规则确实要在共享审核才判（比如要等前面节点的审批意见），
  再单独给它绑别的节点——不要图省事全绑一个。

【不动 compiled 以外的列】
  demo_blocker / demo_scope 是 rule_feasibility.apply() 写的（数据可行性结论），
  这里一个字都不碰。两边写同一列就会互相覆盖，而覆盖是静默的。
"""
import io
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

from app import config, db                                      # noqa: E402
from app.rules import engine                                    # noqa: E402
from app.rules import impl_822gh                                # noqa: E402,F401

IID = "822gh"
NODE = "P01"
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")

out = []


def p(s=""):
    out.append(str(s))


# 本实例的规则编号形如 TR-差旅-NN / TR-接待-NN；福建海运集团是 TR-DN-NN。
# 按「库里有没有这条规则」筛，而不是按编号前缀猜——编号规则会变，库不会骗人。
have = {r["rule_id"] for r in db.query(
    "SELECT rule_id FROM rule WHERE instance_id=%s AND version=1", (IID,))}
impl = sorted(set(engine.registered()) & have)
orphan = sorted(set(engine.registered()) - have
                - {r["rule_id"] for r in db.query(
                    "SELECT rule_id FROM rule WHERE instance_id='fjhy' AND version=1")})

p("=== 实现与规则表对账 ===")
p("  注册表里共 %d 条实现" % len(engine.registered()))
p("  其中属于本实例的 %d 条：%s" % (len(impl), impl))
p("  **写了实现但库里没有这条规则**的 %d 条：%s" % (len(orphan), orphan or "无"))
assert not orphan, "这些实现对不上任何规则，先查编号：%s" % orphan

node_ok = db.scalar("""SELECT count(*) FROM process_node
                        WHERE instance_id=%s AND node_id=%s AND version='0.1'""",
                    (IID, NODE))
p("  流程节点 %s 存在：%s" % (NODE, bool(node_ok)))
assert node_ok, "绑定的节点 %s 不在本实例的流程本体里" % NODE

cur_exec = {r["rule_id"] for r in db.query(
    """SELECT rule_id FROM rule WHERE instance_id=%s AND version=1
        AND demo_executable""", (IID,))}
p("")
p("=== 变化 ===")
p("  现在标成可执行的 %d 条：%s" % (len(cur_exec), sorted(cur_exec) or "无"))
p("  应该标成可执行的 %d 条" % len(impl))
p("  要加上的：%s" % (sorted(set(impl) - cur_exec) or "无"))
p("  要撤销的：%s" % (sorted(cur_exec - set(impl)) or "无"))

if os.environ.get("WRITE") == "1":
    with db.tx() as conn:
        cur = conn.cursor()
        # 先全撤，再按注册表重标。**这样删掉一条实现也会被撤销**，
        # 不会留下一条「标着可执行、却没有实现」的规则——
        # 那种规则在报告上会显示「规则在执行集里但没有实现」，
        # 看起来像代码缺口，实际是这张表没同步。
        cur.execute("""UPDATE rule SET demo_executable=FALSE, compiled=FALSE,
                              binding_node=NULL, impl_path=NULL, algo_type=NULL
                        WHERE instance_id=%s AND version=1""", (IID,))
        cur.execute("""UPDATE rule SET demo_executable=TRUE, compiled=TRUE,
                              binding_node=%s, impl_path='系统规则',
                              algo_type='手写实现'
                        WHERE instance_id=%s AND version=1
                          AND rule_id = ANY(%s)""", (NODE, IID, impl))
    p("")
    p("**已入库**")
    for q, lab in (
            ("""SELECT count(*) FROM rule WHERE instance_id=%s AND version=1
                  AND demo_executable""", "演示可执行"),
            ("""SELECT count(*) FROM rule WHERE instance_id=%s AND version=1
                  AND compiled""", "已编译"),
            ("""SELECT count(*) FROM rule WHERE instance_id=%s AND version=1
                  AND binding_node IS NOT NULL""", "已绑流程节点")):
        p("  %s: %s" % (lab, db.scalar(q, (IID,))))
    p("  fjhy 未被动：可执行 %s 条 / 已编译 %s 条"
      % (db.scalar("""SELECT count(*) FROM rule WHERE instance_id='fjhy'
                        AND version=1 AND demo_executable"""),
         db.scalar("""SELECT count(*) FROM rule WHERE instance_id='fjhy'
                        AND version=1 AND compiled""")))
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "compile822.txt"), "w",
        encoding="utf-8").write("\n".join(out))
print("ok: 有实现的 %d 条" % len(impl))
