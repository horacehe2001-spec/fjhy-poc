# -*- coding: utf-8 -*-
"""解析审批意见 PDF。

这三份 PDF 是**例外出口的唯一数据来源**，不解析它就等于把 12 条规则的
例外条款全部当成不存在：
  TR-D6-03 / D6-06 / D6-02 / D6-05 / D9-01 等都以「补充说明非空」收口。
  补充说明一律为空，这些规则就会对所有有例外情形的正常单据报违规——
  第一次空跑时就出现了 18 条这样的命中，全部是误报。

PDF 里还有几样原以为取不到的东西：
  申请人栏「姓名/职级/部门」—— **职级就在这里**。本体里 EN12 的 note 自己
      写着「审批意见页「申请人」栏为『姓名/其他员工/部门』」，
      可 availability 却标成「待业务侧明确」。观察到了却没落到标记上，
      结果住宿费档位链被当成断的。
  出行人员「姓名(工号),姓名(工号)」—— 多人单的真实人数与名单
  收款方 / 收款账号 / 收款银行 —— 敏感字段，出口必须脱敏
  单据审批记录 —— 审批节点 / 审批人 / 审批意见（含真实退回理由）

解析方式：PDF 有完整文字层（不是扫描件），按阅读顺序取文本，
用「已知标签集合」切分标签与值。值会跨行折断（「运营管\n理部」），
所以是「累积到下一个已知标签为止」，不是逐行配对。
"""
import io
import os
import re

import fitz

# 表头块的标签，顺序即 PDF 里的出现顺序
HEADER_LABELS = [
    "申请人", "核算主体", "业务类型", "金额", "商旅结算", "核销金额",
    "挂账及付款金额", "附件张数", "凭证编号", "事由", "补充说明", "补充信息",
    "出行人员", "标准依据人", "挂账及付款明细", "单据审批记录",
]
PAY_LABELS = ["支付方式", "收款方", "收款账号", "支付金额", "付款用途", "收款银行"]
APPROVAL_HEADER = ["序号", "审批节点", "审批人", "审批意见", "审批时间"]
ALL_LABELS = set(HEADER_LABELS) | set(PAY_LABELS) | set(APPROVAL_HEADER)

RE_BILL_NO = re.compile(r"单据编号[：:]\s*([A-Z]+-[A-Z]+\d+)")
# 页脚还有**一行纯单据号**（没有「单据编号：」前缀）。
# 不认它的后果：它会被并进最后一条审批记录的「审批意见」里，
# 于是意见变成「同意2025-12-17 18:50:11FJPG-BX2512150529」，
# 审批时间也取不到（最后一行不是时间戳了）。
# 拿这种意见去和「同意」比较，40 张单里 23 张会被误判成「人工退回」——
# 差一点就据此报出「AI 漏报 23 张」，而那个数字完全是解析产物。
RE_BARE_BILL_NO = re.compile(r"^[A-Z]{2,6}-[A-Z]{2,4}\d{6,}$")
RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$")
RE_SEQ = re.compile(r"^\d{1,3}$")
RE_TRAVELER = re.compile(r"([^\s,，()（）]+)[(（](\d+)[)）]")


def parse_file(path):
    """返回 {bill_no: {...}}。一张单可能跨多页。"""
    doc = fitz.open(path)
    pages = []
    for i in range(doc.page_count):
        txt = doc[i].get_text()
        m = RE_BILL_NO.search(txt)
        pages.append((m.group(1) if m else None, txt))
    doc.close()

    # 页归属：**向前填充**，不是向后。
    #
    # 实测核对出来的：带单据编号的页同时带「申请人」表头，是一张单的**首页**；
    # 不带编号的页没有表头，是**续页**，属于前一张单。
    # 第一版写成向后填充（续页归给后面最近一个带编号的页），后果是
    # 续页的审批行被并进了下一张单的文本，而下一张单的表头解析会把
    # 开头那些无标签的行整段吃掉 —— 于是 80 条审批记录凭空消失（335 → 255）。
    #
    # 这个错误**不报错、不抛异常**，只是少 24% 的数据。
    # 发现它的唯一办法是独立数一遍 PDF 原文里「整行等于节点名」的次数当真值
    # 去对账。所以下面 assert 把这个对账固化成断言。
    owner = [None] * len(pages)
    cur = None
    for i in range(len(pages)):
        if pages[i][0]:
            cur = pages[i][0]
        owner[i] = cur

    grouped = {}
    for (bn, txt), own in zip(pages, owner):
        key = bn or own
        if not key:
            continue
        grouped.setdefault(key, []).append(txt)

    out = {}
    for bn, texts in grouped.items():
        out[bn] = _parse_bill("\n".join(texts), bn)
    return out


def _lines(text):
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _parse_bill(text, bill_no):
    lines = _lines(text)
    rec = {"bill_no": bill_no, "payments": [], "approvals": []}

    # ── 表头块：累积到下一个已知标签为止 ────────────────────────────────────
    fields = {}
    cur_label, buf = None, []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln == "单据审批记录":
            if cur_label:
                fields[cur_label] = "".join(buf).strip()
            i += 1
            break
        if ln in ALL_LABELS:
            if cur_label:
                fields.setdefault(cur_label, "".join(buf).strip())
            cur_label, buf = ln, []
        elif cur_label:
            buf.append(ln)
        i += 1
    else:
        if cur_label:
            fields.setdefault(cur_label, "".join(buf).strip())

    rec["raw_fields"] = fields
    rec["applicant"] = fields.get("申请人")
    rec["org_name"] = fields.get("核算主体")
    rec["biz_type"] = fields.get("业务类型")
    rec["reason"] = fields.get("事由")
    rec["remark"] = fields.get("补充说明") or None
    rec["extra_info"] = fields.get("补充信息") or None
    rec["voucher_no"] = fields.get("凭证编号")
    rec["attach_pages"] = _int(fields.get("附件张数"))
    rec["travel_settlement"] = _num(fields.get("商旅结算"))
    rec["writeoff_amount"] = _num(fields.get("核销金额"))
    rec["payable_amount"] = _num(fields.get("挂账及付款金额"))
    rec["standard_basis_person"] = fields.get("标准依据人") or None

    # 申请人「姓名/职级/部门」。**职级就在这里。**
    rec["applicant_name"], rec["applicant_rank"], rec["applicant_dept"] = _split_applicant(
        rec["applicant"])

    # 出行人员「姓名(工号),姓名(工号)」
    rec["travelers"] = [{"name": m.group(1), "emp_no": m.group(2)}
                        for m in RE_TRAVELER.finditer(fields.get("出行人员") or "")]

    # ── 挂账及付款明细：按 PAY_LABELS 循环出现 ──────────────────────────────
    rec["payments"] = _parse_payments(lines)

    # ── 审批记录表 ──────────────────────────────────────────────────────────
    rec["approvals"] = _parse_approvals(lines[i:] if i < len(lines) else [])
    return rec


def _split_applicant(s):
    if not s:
        return None, None, None
    parts = [p for p in str(s).split("/") if p.strip()]
    if len(parts) >= 3:
        return parts[0].strip(), parts[1].strip(), "/".join(parts[2:]).strip()
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), None
    return parts[0].strip(), None, None


def _parse_payments(lines):
    """付款明细可以有多笔（多人单每人一笔）。按「支付方式」切段。"""
    out = []
    idxs = [k for k, ln in enumerate(lines) if ln == "支付方式"]
    for n, start in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else len(lines)
        seg = lines[start:end]
        cur_label, buf, got = None, [], {}
        for ln in seg:
            if ln in PAY_LABELS:
                if cur_label:
                    got.setdefault(cur_label, "".join(buf).strip())
                cur_label, buf = ln, []
            elif ln in ("单据审批记录",):
                break
            elif cur_label:
                buf.append(ln)
        if cur_label:
            got.setdefault(cur_label, "".join(buf).strip())
        if got.get("收款方") or got.get("收款账号"):
            out.append({"pay_method": got.get("支付方式"), "payee": got.get("收款方"),
                        "payee_account": got.get("收款账号"),
                        "amount": _num(got.get("支付金额")),
                        "pay_purpose": got.get("付款用途"),
                        "payee_bank": got.get("收款银行")})
    return out


def _parse_approvals(lines):
    """审批记录表。

    第一版用「以时间戳结尾」切行，结果**系统性漏了 24%**：
    独立核对 PDF 原文里「整行等于节点名」的次数是 335 条，解析只得 255 条，
    「审批人和报账人相同跳过」5 次、「分管领导」2 次一条都没解出来。
    原因是页眉页脚的行（「单据编号：…」「单据日期：…」「XX报账单」）
    会被累积进缓冲区，一次错位之后后面全错。
    24% 的审批记录丢失会让 TR-D4-06（查审批链是否含分管领导/总经理）系统性误判。

    这一版改成按**序号列**切行：审批记录表的序号是从 1 开始单调递增的整数，
    这是表格最稳的锚点。噪音行先整体剔掉，再按序号切块，
    块内最后一个时间戳是审批时间，没有时间戳的块也保留（系统跳过的节点就没有）。
    """
    noise = _drop_noise(lines)
    starts = []
    for k, ln in enumerate(noise):
        if not RE_SEQ.match(ln):
            continue
        n = int(ln)
        # 序号必须单调递增才算行首，否则「金额 5」这类纯数字会被误当序号
        if not starts or n == int(noise[starts[-1]]) + 1 or n == 1:
            starts.append(k)
    out = []
    for i, k in enumerate(starts):
        end_i = starts[i + 1] if i + 1 < len(starts) else len(noise)
        rec = _one_approval(noise[k:end_i])
        if rec:
            out.append(rec)
    return out


def _drop_noise(lines):
    """剔掉页眉页脚与表头。**必须整体先剔，不能边切边剔。**"""
    out = []
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        if t in APPROVAL_HEADER or t == "流程结束":
            continue
        if RE_BILL_NO.search(t) or RE_BARE_BILL_NO.match(t):
            continue
        if t.startswith("单据日期") or t.startswith("单据编号"):
            continue
        if t.endswith("报账单") and len(t) <= 12:
            continue
        out.append(t)
    return out


def _one_approval(buf):
    """buf[0] 是序号。其余依次是 节点 / 审批人 / [意见…] / [时间]。"""
    if not buf or not RE_SEQ.match(buf[0]):
        return None
    seq = int(buf[0])
    body = buf[1:]
    ts = None
    if body and RE_TS.match(body[-1]):
        ts = body[-1]
        body = body[:-1]
    node = body[0] if body else None
    approver = body[1] if len(body) > 1 else None
    opinion = "".join(body[2:]).strip() or None
    # 「撤回」行的格式是「鄢岚/2001645 撤回单据」，意见并在审批人里。
    # 不强行拆：拆错了会把人名当成意见。原样留着，由上层判断。
    return {"seq": seq, "node": node, "approver": approver,
            "opinion": opinion, "approved_at": ts}


def _num(s):
    if not s:
        return None
    t = re.sub(r"[,\s，]", "", str(s))
    try:
        return float(t)
    except ValueError:
        return None


def _int(s):
    v = _num(s)
    return int(v) if v is not None else None


def parse_all(material_dir):
    out = {}
    for fn in sorted(os.listdir(material_dir)):
        if not fn.lower().endswith(".pdf"):
            continue
        for bn, rec in parse_file(os.path.join(material_dir, fn)).items():
            rec["source_pdf"] = fn
            out.setdefault(bn, rec)
    return out


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from app import config
    d = parse_all(os.path.join(config.PROJECT_ROOT, "材料", "单据明细表与审批意见"))
    print("解析到 %d 张单" % len(d))
    n_remark = sum(1 for v in d.values() if v["remark"])
    n_rank = sum(1 for v in d.values() if v["applicant_rank"])
    n_appr = sum(len(v["approvals"]) for v in d.values())
    n_pay = sum(len(v["payments"]) for v in d.values())
    n_trav = sum(1 for v in d.values() if v["travelers"])
    print("  有补充说明的      %d" % n_remark)
    print("  有职级的          %d" % n_rank)
    print("  审批记录合计      %d 条" % n_appr)
    print("  付款明细合计      %d 笔" % n_pay)
    print("  有出行人员名单的  %d" % n_trav)
    rank_vals = sorted({v["applicant_rank"] for v in d.values() if v["applicant_rank"]})
    print("  职级取值          %s" % rank_vals)
    nodes = sorted({a["node"] for v in d.values() for a in v["approvals"] if a["node"]})
    print("  审批节点取值      %s" % nodes)
    out = os.path.join(os.environ.get("TEMP", "."), "approval_parsed.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=1))
    print("  明细已写入 %s" % out)
