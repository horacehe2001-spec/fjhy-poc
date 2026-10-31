# -*- coding: utf-8 -*-
r"""只重建 822港航 的 trip_application / trip_calendar，**不碰 attachment**。

用法（**不要加 -I**，理由同 load_822gh_bills）：

    cd 服务端
    python -m app.demo.fix_822gh_trip_fields          # 干跑
    WRITE2=1 python -m app.demo.fix_822gh_trip_fields # 真写库

【为什么不直接重跑 load_822gh_attachments】
  那个程序开头就 `DELETE FROM attachment WHERE instance_id='822gh'`，
  而 attachment_vision / attachment_extract / attachment_judge_log
  三张表都是 ON DELETE CASCADE 挂在 attachment 上。
  **这件事已经真实发生过一次**：重跑一次 load_822gh_bills，
  连带删掉附件 147 / 发票 53 / 出差审批单 15 / 逐日 87 /
  视觉台账 198 行。前四样重建不要钱，视觉台账要重新烧 75 次模型调用——
  而那张台账是「哪些客户凭证离开过我们系统」的唯一证据，
  重建出来的是新记录，原来那 198 行的时间与结论永久丢了。

  所以本次口径订正走定点脚本：
    · 只 DELETE / INSERT trip_application（trip_calendar 由它 CASCADE 带走）
    · attachment 一行不动，并在前后各数一次，不等就回滚

【改了什么】
  见 db/migrations/0056。backfill_date 从「填报日期」订正为
  「出差人签名日期」，填报日期挪进新列 report_date。
"""
import os
import sys

# 先把 WRITE 摘掉再 import —— load_822gh_attachments 是模块级脚本，
# 带着 WRITE=1 导入它会把 attachment 删一遍。这一行是闸，不是洁癖。
_WRITE_WAS = os.environ.pop("WRITE", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app import db  # noqa: E402
from app.demo.load_822gh_attachments import (  # noqa: E402
    SELF_CAR, SELF_MEAL, SLOTS, parse_trip, pdf_lines)

IID = "822gh"
WRITE = os.environ.get("WRITE2") == "1"


def _one_or_none(vals):
    s = {v for v in vals if v}
    return s.pop() if len(s) == 1 else None


def main():
    if _WRITE_WAS is not None:
        print("注意：环境里有 WRITE=%r，已摘掉（本程序用 WRITE2）" % _WRITE_WAS)

    before = {t: db.scalar(
        "SELECT count(*) FROM %s" % t) for t in (
            "attachment", "attachment_vision", "attachment_extract")}

    rows = db.query("""SELECT bill_no, file_name, stored_path
                         FROM attachment
                        WHERE instance_id = %s
                          AND file_name LIKE %s
                          AND lower(file_name) LIKE %s
                        ORDER BY bill_no, file_name""",
                    (IID, "%出差审批%", "%.pdf"))
    print("出差审批单 PDF %d 个" % len(rows))

    import re
    trips = []
    for r in rows:
        try:
            lines = pdf_lines(open(r["stored_path"], "rb").read())
        except Exception as e:            # noqa: BLE001
            print("  打不开 %s —— %s" % (r["file_name"], e))
            continue
        t = parse_trip(lines)
        if not t:
            print("  不是审批单格式 %s" % r["file_name"])
            continue
        m = (re.search(r"\((\d{8,24})\)", r["file_name"])
             or re.search(r"(\d{16,24})", r["file_name"]))
        t["app_no"] = m.group(1) if m else ("%s-OA" % r["bill_no"])
        t["bill_no"] = r["bill_no"]
        trips.append(t)

    print("解析出 %d 份" % len(trips))
    n_sign = sum(1 for t in trips if t["backfill_date"])
    n_rep = sum(1 for t in trips if t["report_date"])
    n_diff = sum(1 for t in trips
                 if t["backfill_date"] and t["report_date"]
                 and t["backfill_date"] != t["report_date"])
    print("  有出差人签名日期: %d / %d" % (n_sign, len(trips)))
    print("  有填报日期      : %d / %d" % (n_rep, len(trips)))
    print("  两个日期不同的  : %d 份（订正前这两格装的是同一个值）" % n_diff)

    if not WRITE:
        print("\n（干跑，没写库。设 WRITE2=1 才写）")
        return

    with db.tx() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM trip_application WHERE instance_id=%s", (IID,))
        n_cal = 0
        for t in trips:
            meal_vals = [v for _, vs in t["meal_days"] for v in vs]
            car_vals = [v for _, vs in t["car_days"] for v in vs]
            cur.execute("""INSERT INTO trip_application(app_no, bill_no,
                               start_date, end_date, place, days, person_count,
                               approve_date, backfill_date, report_date,
                               meal_situation, car_situation, reason,
                               extracted_from)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   '审批单读图')
                           ON CONFLICT (app_no) DO NOTHING""",
                        (t["app_no"], t["bill_no"], t["start_date"],
                         t["end_date"], t["place"], t["days"],
                         t["person_count"], t["approve_date"],
                         t["backfill_date"], t["report_date"],
                         _one_or_none(meal_vals), _one_or_none(car_vals),
                         t["reason"]))
            meal_by_day = {d: vs for d, vs in t["meal_days"]}
            car_by_day = {d: vs for d, vs in t["car_days"]}
            for d in sorted(set(meal_by_day) | set(car_by_day)):
                vs = meal_by_day.get(d, [])
                has_car = any(v and v != SELF_CAR
                              for v in car_by_day.get(d, []))
                for k, slot in enumerate(SLOTS):
                    v = vs[k] if k < len(vs) else None
                    cur.execute("""INSERT INTO trip_calendar(app_no, cal_date,
                                       meal_slot, has_meal, has_car)
                                   VALUES (%s,%s,%s,%s,%s)""",
                                (t["app_no"], d, slot,
                                 (v is not None and v != SELF_MEAL), has_car))
                    n_cal += 1

        # 闸：attachment 系三张表一行都不许变。不等就整个事务回滚。
        # 必须用本事务的 cursor 数，不能用 db.scalar——那是另一个连接，
        # 看不到本事务里尚未提交的删除，这道闸就永远是绿的。
        for k, v in before.items():
            cur.execute("SELECT count(*) FROM %s" % k)
            now = cur.fetchone()[0]
            if now != v:
                raise SystemExit(
                    "中止：%s 从 %d 变成 %d —— 本程序不该碰它" % (k, v, now))
        print("\n**已写库**  出差审批单 %d 份 / 逐日行 %d 行" % (len(trips), n_cal))

    for lab, q in (
            ("出差审批单", "SELECT count(*) FROM trip_application "
                       "WHERE instance_id=%s"),
            ("·有签名回填日期", "SELECT count(backfill_date) FROM trip_application "
                          "WHERE instance_id=%s"),
            ("·有填报日期", "SELECT count(report_date) FROM trip_application "
                        "WHERE instance_id=%s"),
            ("逐日行", "SELECT count(*) FROM trip_calendar WHERE instance_id=%s")):
        print("  %s: %s" % (lab, db.scalar(q, (IID,))))
    print("  附件 %s / 视觉台账 %s（应和改之前一样：%s / %s）"
          % (db.scalar("SELECT count(*) FROM attachment"),
             db.scalar("SELECT count(*) FROM attachment_vision"),
             before["attachment"], before["attachment_vision"]))
    print("  别家没被动：fjhy 出差审批单 %s"
          % db.scalar("SELECT count(*) FROM trip_application "
                      "WHERE instance_id='fjhy'"))


if __name__ == "__main__":
    main()
