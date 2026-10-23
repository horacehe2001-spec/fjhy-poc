# -*- coding: utf-8 -*-
"""智能体协同作业的公共编排机。

抽这一层出来的理由很直接：规则抽取和规则本体是**两条链**，但进度怎么算、
失败怎么表达、信号怎么记，必须是**同一套语义**。
两份拷贝的下场是可预见的——改了一处忘了另一处，然后两个页面上
「跑到一半失败」长得不一样，而没人说得清哪个才是对的。

四条硬规则（都是实测换来的，不是风格偏好）：

1. **进度条单调不回退。** 回退会让人以为任务重来了。

2. **数字全部来自实跑，前端不估算。**
   确定性 Agent 显示「规则引擎 · 不消耗」而不是 0——
   「不知道」不是 0，「不消耗」也不是 0，三者在页面上必须分得开。

3. **某个 Agent 失败就红在原地，后面的标「未执行」，不跳过不粉饰。**
   跳过继续跑会得到一份「看起来跑完了」的结果，而中间那段是空的。

4. **进度权重按实测耗时折算，不按「已完成个数/总数」。**
   这两条链的耗时都严重偏斜（抽取链里 AI 草稿一步占 96% 时长）。
   按个数算，进度条会在某一格卡住几分钟，比没有进度条更像死机。
   第一次跑没有基线就等权估，并在页面上说明；跑完把实测耗时存成基线。
"""
import json
import os
import threading
import time
import uuid

from . import db

PHASES = [("S", "Sense", "读"), ("D", "Decide", "判"),
          ("A", "Act", "写"), ("F", "Feedback", "量"), ("I", "Improve", "调")]

VAR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var")


def blank_agent(meta):
    """**字段名用 agent_name 而不是 name**，这不是风格问题。

    脱敏中间件按**字段名**选策略：叫 `name` 的字段一律走人名脱敏。
    那条策略本身是对的——本系统里 `name` 绝大多数真是人名。
    但智能体名字不是人名，复用这个键的后果是页面上显示
    「文******」「条***」，看着像系统坏了。
    改字段名，不动脱敏规则：规则是对的，错的是我复用了它的键。
    """
    return {"id": meta["id"], "agent_name": meta["agent_name"], "layer": meta["layer"],
            "kind": meta["kind"], "desc": meta["desc"],
            "listens": meta.get("listens"), "emits": meta.get("emits"),
            "pool_keys": meta["pool_keys"],
            "status": "waiting", "phase": None, "phase_started": None,
            "timings": {}, "elapsed": None, "outputs": {}, "note": None,
            "error": None, "cost": None}


def phase(agent, code):
    agent["phase"] = code
    agent["phase_started"] = time.time()


def phase_done(agent, code, t0):
    agent["timings"][code] = round(time.time() - t0, 2)


class AgentJob:
    """一条链的编排器。实例化一个就是一条链，状态在实例里，不在模块全局。

    `runners` 的签名统一成 (job, agent, prev) → prev。
    一度让 L0 用两参、其余用三参，于是调用处要写
    `fn(job, a, prev) if fn is not _run_l0 else fn(job, a)`——
    加第二条链时这行会原样复制一遍，而第二条链的 L0 不叫这个名字。
    """

    def __init__(self, agents, runners, baseline_name, idle_note, bus, bus_note):
        self.agents = agents
        self.runners = runners
        self.baseline_path = os.path.join(VAR_DIR, baseline_name)
        self.idle_note = idle_note
        self.bus = bus
        self.bus_note = bus_note
        self._lock = threading.Lock()
        self._job = None            # 当前/最近一次作业快照

    # ── 基线 ────────────────────────────────────────────────────────────────
    def load_baseline(self):
        try:
            with open(self.baseline_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                      # noqa: BLE001
            return None

    def save_baseline(self, agents):
        """把这一次各 Agent 的实测耗时存成下次的权重基线。

        **只存成功跑完的那次**：失败那次某个 Agent 耗时是 0 或半截，
        拿它当基线会让下次的进度条更难看。
        """
        data = {a["id"]: max(0.3, round(a.get("elapsed") or 0, 2)) for a in agents
                if a.get("status") == "done"}
        if not data:
            return
        try:
            os.makedirs(os.path.dirname(self.baseline_path), exist_ok=True)
            with open(self.baseline_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass        # 存不下基线不算错，下次等权估就是了

    # ── 进度 ────────────────────────────────────────────────────────────────
    def pct(self, job):
        """加权进度。**单调不回退**：只取历史最大值。"""
        base = self.load_baseline()
        ws, tot = {}, 0.0
        for a in job["agents"]:
            w = (base or {}).get(a["id"], 1.0)
            ws[a["id"]] = w
            tot += w
        acc = 0.0
        for a in job["agents"]:
            if a["status"] in ("done", "skipped"):
                acc += ws[a["id"]]
            elif a["status"] == "running":
                frac = len(a["timings"]) / float(len(PHASES))
                acc += ws[a["id"]] * frac
        p = int(min(100, round(acc / (tot or 1) * 100)))
        job["_pct_seen"] = max(job.get("_pct_seen", 0), p)
        return job["_pct_seen"]

    # ── 快照 ────────────────────────────────────────────────────────────────
    def snapshot(self):
        """给界面轮询的快照。没跑过也要能返回编排图——演示现场最怕白屏。"""
        with self._lock:
            if self._job is None:
                return {"job_id": None, "status": "idle", "pct": 0, "elapsed": 0,
                        "agents": [blank_agent(m) for m in self.agents],
                        "signals": [], "log_errors": [],
                        "measured_baseline": bool(self.load_baseline()),
                        "totals": {"agents": len(self.agents), "agents_done": 0},
                        "note": self.idle_note}
            j = dict(self._job)
            j["agents"] = [dict(a) for a in self._job["agents"]]
            now = time.time()
            j["elapsed"] = round((j.get("finished_at") or now) - j["started_at"], 1)
            for a in j["agents"]:
                if a["status"] == "running" and a.get("phase_started"):
                    a["phase_elapsed"] = round(now - a["phase_started"], 1)
                if a["status"] == "running" and a.get("started_at"):
                    a["elapsed"] = round(now - a["started_at"], 1)
            j["pct"] = self.pct(j)
            j["measured_baseline"] = bool(self.load_baseline())
            j["totals"] = {"agents": len(self.agents),
                           "agents_done": sum(1 for a in j["agents"]
                                              if a["status"] in ("done", "skipped"))}
            j.pop("_pct_seen", None)
            return j

    def meta(self):
        """编排元数据：没开跑时也能画图。"""
        return {"agents": [blank_agent(m) for m in self.agents],
                "phases": [{"code": c, "phase_name": n, "cn": cn} for c, n, cn in PHASES],
                "bus": self.bus, "note": self.bus_note}

    # ── 信号 ────────────────────────────────────────────────────────────────
    def emit(self, job, agent, channel, payload=None):
        """投信号。**信号是 Agent 之间唯一的联系**——它们不互相调用。

        落库的列必须和 app/audit/recorder.py:record_action 用同一套：
        agent_signal_log 是「A 阶段唯一允许写库」这条纪律的可验证形式，
        一张表两套写法，审计就对不上了。

        **2026-10-19 修的 bug**：原来这里写的是 channel / kind / from_agent /
        to_agent / subject_id 五个**库里不存在的列**，两个 NOT NULL 列
        （cycle_id、idempotency_key）也没给值，所以这条 INSERT **从来没成功过**；
        而 `except: pass` 把它静默了整整一天。实测后果：
        三条链跑了几十次，agent_signal_log 里来自它们的记录是 **0 行**，
        页面上却显示「投了 N 条信号」——两边都没人报错。
        """
        job["signals"].append({"from": agent["id"], "channel": channel,
                               "at": round(time.time() - job["started_at"], 1),
                               "payload": payload or {}})
        seq = len(job["signals"])
        try:
            db.execute("""INSERT INTO agent_signal_log(signal_id, cycle_id, agent_id,
                              action, target, payload, written_tables,
                              idempotency_key, masked)
                          VALUES (%s,%s,%s,'emit_signal',%s,%s,%s,%s,TRUE)""",
                       ("sig-" + uuid.uuid4().hex[:12], job["job_id"], agent["id"],
                        channel,
                        json.dumps(payload or {}, ensure_ascii=False, default=str),
                        json.dumps({"agent_signal_log": 1}, ensure_ascii=False),
                        # 幂等键带作业号与序号：同一条链重跑一次是**另一次**协同，
                        # 两次都该留痕；而同一次作业里重复投同一个信号不该留两行。
                        "%s:%s:%s:%d" % (job["job_id"], agent["id"], channel, seq)))
        except Exception as exc:                               # noqa: BLE001
            # 落库失败不让整条链停下来（信号的作用已经在内存里生效了），
            # 但**绝不静默**：记进作业快照，页面上看得见。
            # 上一版这里是 `pass`，于是「一次都没成功」这件事藏了一天。
            job.setdefault("log_errors", []).append(
                {"from": agent["id"], "channel": channel,
                 "error": "%s: %s" % (type(exc).__name__, str(exc).split(chr(10))[0])})

    # ── 作业 ────────────────────────────────────────────────────────────────
    def _work(self, job):
        prev = None
        for a in job["agents"]:
            if job["status"] == "failed":
                # **失败后面的标「未执行」，不跳过不粉饰。**
                a["status"] = "skipped"
                a["note"] = "上游失败，未执行"
                continue
            a["status"] = "running"
            a["started_at"] = time.time()
            try:
                prev = self.runners[a["id"]](job, a, prev)
                a["status"] = "done"
            except Exception as e:                             # noqa: BLE001
                a["status"] = "failed"
                a["error"] = "%s: %s" % (type(e).__name__, e)
                job["status"] = "failed"
                job["error"] = a["error"]
            finally:
                a["elapsed"] = round(time.time() - a["started_at"], 2)
                a["phase"] = None
                a["phase_started"] = None
        if job["status"] != "failed":
            job["status"] = "done"
            self.save_baseline(job["agents"])
        job["finished_at"] = time.time()

    def start(self, **opts):
        """启动一次协同。同一条链同一时刻只允许一个作业在跑。

        `opts` 原样放进 `job["opts"]`，给 runner 读（如抽取链的 only_changed）。
        **放在 job 上而不是靠 prev 往下传**：prev 是「上一个 Agent 的返回值」，
        要让第四个 Agent 看到第一个 Agent 定的范围，中间两个都得记得把它带上——
        而其中任何一个忘了带，范围就静默退化成「全部」。
        作业级的参数只能有一个落脚处。
        """
        with self._lock:
            if self._job is not None and self._job["status"] == "running":
                return {"ok": False, "reason": "已有作业在运行",
                        "job_id": self._job["job_id"]}
            self._job = {"job_id": "job-" + uuid.uuid4().hex[:10], "status": "running",
                         "started_at": time.time(), "finished_at": None, "error": None,
                         "agents": [blank_agent(m) for m in self.agents],
                         # log_errors：信号落库失败要在页面上看得见，不许静默
                         "signals": [], "log_errors": [], "_pct_seen": 0,
                         "opts": dict(opts)}
            job = self._job
        threading.Thread(target=self._work, args=(job,), daemon=True).start()
        return {"ok": True, "job_id": job["job_id"]}
