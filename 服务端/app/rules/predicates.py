# -*- coding: utf-8 -*-
"""判定原语。规则实现只拼装这些原语，不各自写一套。

为什么要抽出来：同一种判断在多条规则里出现，各写一遍就会出现「这条规则的
关键词匹配带归一化、那条不带」这类不一致，而不一致在审核系统里直接表现为
同样的单据在不同规则下结论不同。原语只有一处实现，改一次全体生效。

每个原语返回的都是**三态**：True / False / None。
None 是「判不了」，不是「不符合」。把 None 当 False 会把取不到数据
当成违规，那是误报；把 None 当 True 会把风险放过，那是漏报。
两者都致命，所以 None 必须一路传到最后，变成「待人工」。
"""
import re
import unicodedata

SPACE_CHARS = (" ", "\t", "　")
_LATIN = re.compile(r"[A-Za-z]")


# ─────────────────────────────────────────────────────────────────────────────
# 文本归一与关键词
# ─────────────────────────────────────────────────────────────────────────────
def norm_keep_space(s):
    """NFKC + 大写，保留空白。给拉丁词边界匹配用。"""
    return unicodedata.normalize("NFKC", str(s or "")).upper()


def norm_strip_space(s):
    """再删空白。给子串匹配用。"""
    t = norm_keep_space(s)
    for ch in SPACE_CHARS:
        t = t.replace(ch, "")
    return t


def strip_station_suffix(s):
    """把地名/站名归到「城市」这一级，只给「像不像同一地方」的粗判用，
    绝不给「判定合规」用——这个函数只用来决定要不要转人工，不用来判 ok。

    读图把同一段往返的出发/到达地读成「福州南站」「福州南」「福州站」这几种
    写法混着来——同一个地方，「站」字有时有有时没有，方向字「南/北/东/西」
    有时紧跟在城市名后、有时被「站」字顶替掉了。逐字匹配会把同一个往返
    误判成两个不相干的地方，直接判「缺返程」就是把合规单错判成违规。

    所以两步都剥：先去结尾的「站」字，再去（剥站字之后）结尾的方向字。
    「厦门站」「厦门北站」都会被剥成「厦门」——**这是故意的**：剥到这一级
    只用来判「要不要转人工」，剥过头把两个真不同的地方合并了，最坏结果是
    该判 ok 的多问了一次人，而不是该转人工的被错判成确定违规——两种错的
    代价不对等，所以宁可在这一步剥得深一点。
    """
    t = norm_strip_space(s)
    if t.endswith("站") and len(t) > 1:
        t = t[:-1]
    if t and t[-1] in "南北东西" and len(t) > 1:
        t = t[:-1]
    return t


def keyword_hit(entry, text):
    """关键词匹配。entry 的形状与 敏感词表_v0.1.yaml 的条目一致。

    三种 match：
      substring                 —— 子串（归一后删空白）
      substring_with_exclusions —— 先过排除项，命中排除项直接放过
      word_boundary_latin       —— 拉丁词边界，**在保留空白的文本上判**

    最后那一条是踩出来的：先删空白的话，「XX SPA会馆」变成「XXSPA会馆」，
    SPA 的左邻成了拉丁字母 X，边界规则反而把真命中挡掉。
    返回 (命中?, 命中的片段)。
    """
    if not text:
        return False, None
    for ex in entry.get("exclusions") or []:
        if norm_strip_space(ex) in norm_strip_space(text):
            return False, None
    terms = [entry["term"]] + list(entry.get("aliases") or [])
    mode = entry.get("match") or "substring"
    if mode == "word_boundary_latin":
        hay = norm_keep_space(text)
        for t in terms:
            needle = norm_keep_space(t)
            for mo in re.finditer(re.escape(needle), hay):
                left = hay[mo.start() - 1] if mo.start() > 0 else ""
                right = hay[mo.end()] if mo.end() < len(hay) else ""
                if not (left and _LATIN.match(left)) and not (right and _LATIN.match(right)):
                    return True, _excerpt(text, t)
        return False, None
    hay = norm_strip_space(text)
    for t in terms:
        if norm_strip_space(t) in hay:
            return True, _excerpt(text, t)
    return False, None


def _excerpt(text, term, span=14):
    """回显命中的原文片段。

    提示里必须回显原文，不能只说「发现敏感内容」：
    命中「黄山」的单子，看到「福州黄山路XX酒店」就立刻知道是路名，三秒结束；
    只说「发现敏感内容」则要重新翻一遍发票。
    """
    t = str(text)
    i = norm_strip_space(t).find(norm_strip_space(term))
    if i < 0:
        return t[:40]
    # 归一会改变长度，所以位置只能近似；取一段上下文给人看，不做精确切分
    lo = max(0, i - span)
    return t[lo:lo + span * 2 + len(term)]


def longest_match(candidates, text, key="name", aliases_key="aliases"):
    """名单的最长匹配。短键截胡会取到错的附属信息。

    实例：内部单位名单里若启用两字的「闽运」，「闽运长乐公交公司」会先被它命中，
    city 取到 NULL，住宿费标准就无从匹配；取最长匹配才能拿到「长乐区」→ 福州档。
    """
    if not text:
        return None, None
    hay = norm_strip_space(text)
    best, best_key = None, None
    for c in candidates:
        for k in [c.get(key)] + list(c.get(aliases_key) or []):
            if not k:
                continue
            nk = norm_strip_space(k)
            if nk and nk in hay and (best_key is None or len(nk) > len(norm_strip_space(best_key))):
                best, best_key = c, k
    return best, best_key


# ─────────────────────────────────────────────────────────────────────────────
# 区间判定（degraded interval）
# ─────────────────────────────────────────────────────────────────────────────
def interval_verdict(actual, lower, upper):
    """缺字段时的三态判定。

    actual <= lower  →  必定不超（任何档位都不超）        → False（不违规）
    actual >  upper  →  必定超标（任何档位都超）          → True（违规）
    其间            →  取决于取不到的那个字段            → None（待人工）

    这是「字段取不到」时唯一不产生误报也不产生漏报的判法。
    直接按某一档判会往一个方向错：按低档判 → 合规的高档被报超标（误报）；
    按高档判 → 超标的低档被放过（漏报）。
    """
    if actual is None or lower is None or upper is None:
        return None
    if actual <= lower:
        return False
    if actual > upper:
        return True
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 数值
# ─────────────────────────────────────────────────────────────────────────────
def as_num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def le(actual, cap):
    """actual <= cap 的三态版本。任一侧取不到就返回 None。"""
    a, c = as_num(actual), as_num(cap)
    if a is None or c is None:
        return None
    return a <= c


def nonempty(s):
    """字符串非空。**空白串算空**——「补充说明」填一个空格不算说明。"""
    if s is None:
        return False
    return bool(str(s).strip())


def days_from_meal_allowance(meal_total, daily=100):
    """由伙食补助费倒算出差天数。

    伙食补助是按自然天数包干计发的，所以 总额 ÷ 日标准 = 天数。
    这是少数几条绕开 G-04（OA 未同步出差审批单）的路。

    **只有整除时才认**：不整除说明里面混了扣减（接待单位提供餐次要扣）
    或者日标准不是 100（西藏青海新疆是 120），这时候天数算不准，
    返回 (None, 原因)，由调用方转人工。
    实测印证：样本里「伙食补助费」金额为 100/200/300/400/600/900，
    全部是 100 的整数倍；FJPG-BX2610132121 摘要写明 9.28-9.29（2 天），金额正好 200。
    """
    t = as_num(meal_total)
    if t is None or t <= 0:
        return None, "没有伙食补助费明细，无法倒算出差天数"
    if abs(t % daily) > 1e-6:
        return None, ("伙食补助费 %s 不是日标准 %s 的整数倍，"
                      "可能含接待扣减或适用 120 元档，天数算不准" % (t, daily))
    return int(round(t / daily)), None


# ─────────────────────────────────────────────────────────────────────────────
# 范围匹配（scope）
# ─────────────────────────────────────────────────────────────────────────────
def scope_matches(scope, fact):
    """规则的 scope 是否覆盖这张单。

    `*` 表示不限。`excluded_biz_type` 是硬排除，不是可选项——
    审核要点 2.2.1 白纸黑字取消了外派差旅费的往返行程要求，
    不排除就会对所有外派单误报。
    返回 (匹配?, 不匹配的原因)。
    """
    bill = fact.get("bill") or {}
    biz = bill.get("biz_type")
    doc = bill.get("doc_type")
    org = (fact.get("org") or {}).get("name") or ""

    ex = scope.get("excluded_biz_type") or []
    if biz and biz in ex:
        return False, "业务类型 %s 被 scope 明确排除" % biz

    bt = scope.get("biz_type")
    if bt and "*" not in bt and biz not in bt:
        return False, "业务类型 %s 不在 %s 内" % (biz, bt)

    dt = scope.get("doc_type")
    if dt and "*" not in dt and doc not in dt:
        return False, "单据类型 %s 不在 %s 内" % (doc, dt)

    ent = scope.get("entity")
    if ent and "*" not in ent:
        if not any(norm_strip_space(e) in norm_strip_space(org) for e in ent):
            return False, "核算主体「%s」不属于 %s" % (org, ent)

    items = scope.get("expense_item")
    if items and "*" not in items:
        have = {d.get("expense_item") for d in fact.get("details") or []}
        if not (have & set(items)):
            return False, "本单没有 %s 这几类费用明细" % items
    return True, None


def details_of(fact, items):
    """取指定费用小类的明细行。items 可以是单个名称或列表。"""
    if isinstance(items, str):
        items = [items]
    want = set(items)
    return [d for d in fact.get("details") or [] if d.get("expense_item") in want]


def sum_of(fact, items):
    rows = details_of(fact, items)
    if not rows:
        return None
    total = 0.0
    for d in rows:
        v = as_num(d.get("amount"))
        if v is None:
            return None
        total += v
    return total
