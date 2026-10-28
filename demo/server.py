"""MADA live demo: a web dashboard over the running multi-agent system.

Run:  python -m demo.server      (then open http://localhost:8000)

What it does
  * builds the 'store' database and its sandbox clone on first start (about 2 minutes), or resets an
    existing one to its untuned state;
  * runs the eight-session workload and the 2-second telemetry collector continuously;
  * lets the presenter run the tuning cycle, inject faults, approve or reject high-risk actions,
    switch the safety gate off and send harmful proposals through it - all from the browser.
"""
import json
import os
import random
import re
import sys
import threading
import time
from collections import deque

import psycopg
from flask import Flask, jsonify, request, send_from_directory

from mada import config as C
from mada import schema
from mada import faults as F
from mada.core import console, Proposal
from mada.workload import Workload, Recorder, summarize, TEMPLATES
from mada.observability import Collector
from mada.agents.monitor import MonitorAgent
from mada.agents.index_agent import IndexAgent
from mada.agents.knob_agent import KnobAgent, CATALOG
from mada.agents.query_agent import QueryAgent
from mada.agents.diagnosis import DiagnosisAgent
from mada.agents.security import SecurityAgent
from mada.coordinator import Coordinator, SafetyGate

STATIC = os.path.join(os.path.dirname(__file__), "static")
PORT = int(os.environ.get("MADA_DEMO_PORT", "8000"))
INDEX_WAIT_S = int(os.environ.get("MADA_INDEX_WAIT", "45"))     # fresh statistics before the index agent runs
BASELINE_S = int(os.environ.get("MADA_BASELINE_S", "60"))       # healthy traffic the monitor learns from
KNOB_TRIALS = int(os.environ.get("MADA_KNOB_TRIALS", "10"))

LABELS = {
    "A1_revenue_by_category": "A1 revenue by category", "A2_top_customers_city": "A2 top customers per city",
    "A3_complaint_products": "A3 complaints report", "S1_support_lookup": "S1 support lookup",
    "T1_customer_orders": "T1 order history", "T2_order_detail": "T2 order detail", "T3_product_reviews": "T3 product reviews",
    "T4_place_order": "T4 place order", "T5_product_search": "T5 product search", "T6_product_lookup": "T6 product lookup",
}
AGENTS = ["monitor", "index-agent", "knob-agent", "query-agent", "diagnosis", "security-agent", "coordinator", "operator"]


# ------------------------------------------------------------------ console capture
class LogTee:
    """Receives everything the agents print through the shared rich console."""
    ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

    def __init__(self):
        self.lines = []           # (seq, time, text)
        self.seq = 0
        self.lock = threading.Lock()
        self.last_seen = {a: 0.0 for a in AGENTS}
        self._buf = ""

    def write(self, s):
        sys.__stdout__.write(s)
        self._buf += self.ANSI.sub("", s)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if not line.strip():
                continue
            now = time.time()
            with self.lock:
                self.seq += 1
                self.lines.append((self.seq, now, line.rstrip()))
                if len(self.lines) > 3000:
                    self.lines = self.lines[-2000:]
                low = line.lower()
                for a in AGENTS:
                    if a in low:
                        self.last_seen[a] = now
        return len(s)

    def flush(self):
        sys.__stdout__.flush()

    def isatty(self):
        return False

    def since(self, seq):
        with self.lock:
            return [{"seq": n, "t": t, "text": x} for n, t, x in self.lines if n > seq][-500:]


TEE = LogTee()
console.file = TEE
console.record = False
console.width = 96          # fits the dashboard's log pane


def say(msg):
    console.log(msg)


# ------------------------------------------------------------------ the demo controller
class Demo:
    def __init__(self):
        self.phase = "starting"
        self.detail = "connecting to PostgreSQL"
        self.busy = threading.Lock()          # one long action (tuning, harmful batch) at a time
        self.rec = Recorder()
        self.wl = Workload(self.rec)
        self.col = Collector(self.rec)
        self.gate = SafetyGate(approver=self.approve, enabled=True)
        self.coord = Coordinator(self.gate, recorder=self.rec)
        self.mon = MonitorAgent()
        self.sec = SecurityAgent(self.wl.search_sql)
        self.ia = IndexAgent()
        self.ka = KnobAgent(trials=KNOB_TRIALS)
        self.diag = None
        self.base_rows = []
        self.before = {}
        self.before_tps = None
        self.tuned = False
        self.watching = False
        self.auto_approve = False
        self.pending = None                    # proposal waiting for the operator
        self._decision = None
        self._decided = threading.Event()
        self.fault = None
        self.incidents = []
        self.harmful_applied = False
        self.harm_backup = None
        self.expected_indexes = {}
        self.error = None

    # ---------------- human approval ----------------
    def approve(self, p):
        if self.auto_approve:
            say(f"[bold]operator[/] auto-approved high-risk action #{p.id}: {p.title}")
            return True
        self.pending = {"id": p.id, "title": p.title, "agent": p.agent, "category": p.category, "risk": p.risk,
                        "action": p.action_sql[:300], "evidence": json.loads(json.dumps(p.evidence, default=str))}
        self._decided.clear()
        say(f"[bold]operator[/] approval needed for #{p.id}: {p.title}")
        ok = self._decided.wait(90) and self._decision
        say(f"[bold]operator[/] {'approved' if ok else 'rejected'} #{p.id}")
        self.pending = None
        return bool(ok)

    def decide(self, ok):
        self._decision = ok
        self._decided.set()

    # ---------------- start-up ----------------
    def start(self):
        threading.Thread(target=self._startup, daemon=True).start()
        threading.Thread(target=self._incident_loop, daemon=True).start()

    def _db_ready(self):
        try:
            with psycopg.connect(C.dsn(C.PROD_DB), autocommit=True, connect_timeout=5) as con:
                n = con.execute("SELECT count(*) FROM orders").fetchone()[0]
            with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True, connect_timeout=5) as con:
                con.execute("SELECT 1 FROM orders LIMIT 1")
            return n > 0
        except psycopg.Error:
            return False

    def _wait_for_postgres(self):
        for _ in range(60):
            try:
                with psycopg.connect(C.dsn("postgres"), autocommit=True, connect_timeout=3) as con:
                    con.execute("SELECT 1")
                return
            except psycopg.OperationalError as e:
                self.detail = f"waiting for PostgreSQL at {C.PG_HOST}:{C.PG_PORT} ({str(e).splitlines()[0][:80]})"
                time.sleep(2)
        raise RuntimeError(f"PostgreSQL at {C.PG_HOST}:{C.PG_PORT} is not reachable")

    def _reset_cluster_settings(self):
        with psycopg.connect(C.dsn("postgres"), autocommit=True) as con:
            for k in CATALOG:
                con.execute(f"ALTER SYSTEM RESET {k}")
            con.execute("SELECT pg_reload_conf()")

    def reset_to_untuned(self):
        """Fast reset: drop MADA's indexes and registry rows, restore defaults and grants."""
        for db in (C.PROD_DB, C.SANDBOX_DB):
            with psycopg.connect(C.dsn(db), autocommit=True) as con:
                for (name,) in con.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'mada%'").fetchall():
                    con.execute(f"DROP INDEX IF EXISTS {name}")
                con.execute("DELETE FROM mada.hints")
                con.execute("DELETE FROM mada.rewrites")
                con.execute("ALTER TABLE orders RESET (autovacuum_enabled)")
                con.execute("GRANT SELECT ON payments TO support")
        self._reset_cluster_settings()
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            con.execute("VACUUM (ANALYZE) orders")
            try:
                con.execute("SELECT pg_stat_statements_reset()")
            except psycopg.Error:
                pass

    def _startup(self):
        try:
            self._wait_for_postgres()
            with psycopg.connect(C.dsn("postgres"), autocommit=True) as con:
                ver = con.execute("SHOW server_version").fetchone()[0]
                libs = con.execute("SHOW shared_preload_libraries").fetchone()[0]
            if "pg_stat_statements" not in libs:
                raise RuntimeError("pg_stat_statements is not in shared_preload_libraries; see README (database settings)")
            say(f"PostgreSQL {ver} at {C.PG_HOST}:{C.PG_PORT}")
            if self._db_ready():
                self.phase, self.detail = "preparing", "resetting the existing database to its untuned state"
                say("existing 'store' database found - resetting it to the untuned state")
                self.reset_to_untuned()
            else:
                self.phase, self.detail = "preparing", "building the 168 MB store database and its sandbox (about 2 minutes, first start only)"
                say("building the 'store' database (first start only)")
                self._reset_cluster_settings()
                counts, size = schema.create(verbose=False)
                schema.clone_sandbox()
                say(f"database built: {size}; " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
            self.wl.start()
            self.col.start()
            say("workload started: 5 application, 2 analyst and 1 support sessions running 10 query templates")
            self.phase, self.detail = "baseline", "measuring the untuned database for 60 seconds"
            time.sleep(60)
            self.before = summarize(self.rec.window(55))
            self.before_tps = len(self.rec.window(55)) / 55
            say(f"baseline: {self.before_tps:.1f} statements/s before MADA")
            self.phase, self.detail = "ready", "untuned - press 'Run tuning cycle'"
        except Exception as e:  # noqa: BLE001 - shown on the dashboard
            self.error = f"{type(e).__name__}: {e}"
            self.phase, self.detail = "error", self.error
            say(f"[red]start-up failed[/]: {self.error}")

    # ---------------- tuning cycle ----------------
    def tune(self):
        if not self.busy.acquire(blocking=False):
            return False
        threading.Thread(target=self._tune, daemon=True).start()
        return True

    def _tune(self):
        try:
            self.watching = False
            self.before = summarize(self.rec.window(60))
            self.before_tps = len(self.rec.window(60)) / 60
            console.rule("[bold]Tuning cycle")
            self.phase, self.detail = "tuning", "query agent: looking for slow analytical statements"
            recent = summarize(self.rec.window(60))
            slow = [t for t, v in sorted(recent.items(), key=lambda kv: -kv[1]["mean"]) if v["mean"] > 250 and t.startswith("A")]
            say(f"query-agent slow templates (mean > 250 ms in the last 60 s): {slow}")
            qprops, _ = QueryAgent().propose(slow)
            self.detail = "coordinator: window for the query proposals"
            self.coord.run_window(qprops, watch_s=12)

            self.detail = f"index agent: collecting {INDEX_WAIT_S} s of fresh query statistics"
            with psycopg.connect(C.dsn(), autocommit=True) as con:
                con.execute("SELECT pg_stat_statements_reset()")
            say(f"index-agent statistics reset; collecting {INDEX_WAIT_S} s of query costs")
            time.sleep(INDEX_WAIT_S)
            self.detail = "index agent: what-if costing, then real timing on the sandbox"
            iprops = self.ia.propose()
            for p in iprops:
                ev = p.sandbox_check()
                p.sandbox_check = (lambda ev=ev: ev)
                say(f"index-agent sandbox check {p.title}: gain {100*ev['measured_gain']:.0f}%, write overhead {100*ev['write_overhead']:+.0f}%, "
                    f"{ev['before_ms']} -> {ev['after_ms']}")
            self.detail = "coordinator: index windows (one planner change at a time)"
            self.coord.run_window(iprops, watch_s=12)
            while self.coord.deferred:
                self.coord.run_window([], watch_s=12)

            self.detail = f"knob agent: {KNOB_TRIALS} Bayesian-optimisation trials on the sandbox"
            freq = {}
            for t, tpl, ms, ok in self.rec.window(60):
                freq[tpl] = freq.get(tpl, 0) + 1 / 60
            kprop, _ = self.ka.propose(freq)
            if kprop:
                self.coord.run_window([kprop], watch_s=12)
            if kprop and kprop.status == "applied":
                self.ka.good = dict(self.ka.pending)
            self.tuned = True

            self.phase, self.detail = "learning", f"learning normal behaviour of the tuned system ({BASELINE_S} s)"
            say(f"monitor learning the tuned system's normal behaviour for {BASELINE_S} s")
            t0 = time.time()
            time.sleep(BASELINE_S)
            self._arm_incident_agents(t0)
            self.phase, self.detail = "watching", "tuned - agents are watching; inject a fault to see them respond"
        except Exception as e:  # noqa: BLE001
            self.phase, self.detail = "error", f"tuning failed: {type(e).__name__}: {e}"
            say(f"[red]tuning failed[/]: {e}")
        finally:
            self.busy.release()

    def _arm_incident_agents(self, t0):
        self.base_rows = self.col.since(t0 + 4)
        self.mon.fit_baseline(self.base_rows)
        self.sec.train()
        self.sec.learn_profiles(self.rec.audit_since(t0))
        self.expected_indexes = {}
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            for name, d in con.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'mada_%'"):
                self.expected_indexes[name] = d.replace("public.", "").replace(" USING btree", "")
        self.diag = DiagnosisAgent(index_agent=self.ia, knob_agent=self.ka, expected_indexes=self.expected_indexes)
        self.mon.reset()
        self.watching = True
        say("monitor, diagnosis and security agents armed")

    # ---------------- faults ----------------
    FAULTS = {
        "F1": "Drop an index MADA built", "F2": "Set work_mem to 64 kB", "F4": "Hold row locks on hot products",
        "F5": "Bloat the orders table", "F8": "Open 80 idle connections", "F9": "SQL injection on search",
        "F10": "Support reads card data",
    }

    def inject(self, key):
        if not self.watching or self.fault:
            return False, "faults can be injected once the system is tuned and no other fault is active"
        if key == "F1":
            name = next((n for n in self.expected_indexes if "orders_customer_id" in n), None) or next(iter(self.expected_indexes), None)
            if not name:
                return False, "no MADA index to drop"
            f = F.MissingIndex(name, self.expected_indexes[name], rec=self.rec)
        elif key == "F2":
            wm = self.ka.good.get("work_mem", "4MB")
            f = F.BadConfig(wm, rec=self.rec)
        elif key == "F4":
            f = F.LockContention(rec=self.rec)
        elif key == "F5":
            f = F.TableBloat(rec=self.rec)
        elif key == "F8":
            f = F.ConnectionStorm(rec=self.rec)
        elif key == "F9":
            f = F.SqlInjection(self.wl.search_sql, rec=self.rec)
        elif key == "F10":
            f = F.AbnormalAccess(rec=self.rec)
        else:
            return False, "unknown fault"
        self.mon.reset()
        f.inject_t = time.time()
        f.inject()
        inc = {"key": key, "name": self.FAULTS[key], "inject_t": f.inject_t, "detected_s": None, "cause": None,
               "fix": None, "decision": None, "fixed_s": None, "ended": False}
        self.incidents.append(inc)
        self.fault = (f, inc)
        console.rule(f"[bold]{key} · {self.FAULTS[key]}")
        say(f"[bold red]fault injected[/]: {key} {self.FAULTS[key]}")
        threading.Thread(target=self._fault_timer, args=(f, inc), daemon=True).start()
        return True, "injected"

    def _fault_timer(self, f, inc):
        while time.time() < f.inject_t + 100:
            if inc["fixed_s"] is not None and time.time() > f.inject_t + inc["fixed_s"] + 20:
                break
            if inc["decision"] in ("REJECT", "none") and time.time() > f.inject_t + (inc["detected_s"] or 0) + 30:
                break
            time.sleep(1)
        self.end_fault()

    def end_fault(self):
        if not self.fault:
            return
        f, inc = self.fault
        try:
            f.end()
        except Exception as e:  # noqa: BLE001
            say(f"fault clean-up: {e}")
        if f.key == "F5":
            with psycopg.connect(C.dsn(), autocommit=True) as con:
                con.execute("VACUUM (ANALYZE) orders")
        inc["ended"] = True
        self.fault = None
        say(f"{f.key} ended; fault removed")
        time.sleep(8)
        self.mon.reset()

    def _handle(self, inc, proposals, cause):
        inc["cause"] = cause
        if not proposals:
            inc["fix"], inc["decision"] = "no fix proposed", "none"
            return
        inc["fix"] = proposals[0].title
        applied = self.coord.run_window(proposals, watch_s=0)
        last = self.coord.log[-1] if self.coord.log else None
        inc["decision"] = "PASS" if applied else (last[1] if last else "REJECT")
        if applied:
            inc["fixed_s"] = round(applied[0].done_at - inc["inject_t"], 1)

    def _incident_loop(self):
        last_scan = time.time()
        while True:
            time.sleep(C.TICK_SECONDS)
            if not self.watching or not self.diag:
                last_scan = time.time()
                continue
            inc = self.fault[1] if self.fault else None
            handled = inc is not None and inc["detected_s"] is not None
            audit = self.rec.audit_since(last_scan)
            last_scan = time.time()
            findings = self.sec.scan(audit)
            if findings and inc and not handled:
                inc["detected_s"] = round(time.time() - inc["inject_t"], 1)
                for x in findings[:2]:
                    say(f"[bold green]security-agent[/] {x['type']} by role {x['role']}: {x['sql'][:96]}")
                self._handle(inc, self.sec.respond(findings), ", ".join(sorted({x['type'] for x in findings})))
                continue
            rows = self.col.last()
            if not rows:
                continue
            alert = self.mon.check(rows[0])
            if alert and inc and not handled:
                inc["detected_s"] = round(time.time() - inc["inject_t"], 1)
                say("[bold]monitor[/] alert: " + ", ".join(f"{k} z={v:.1f}" for k, v in alert["metrics"].items()))
                pre = [r for r in self.col.since(inc["inject_t"] - 16) if r["t"] < inc["inject_t"]]
                d = self.diag.diagnose(self.col.since(inc["inject_t"]), self.base_rows, pre)
                self._handle(inc, [p for p in [self.diag.fix(d)] if p], d["cause"])
            elif alert and not inc:
                say("[bold]monitor[/] alert with no injected fault: " + ", ".join(f"{k} z={v:.1f}" for k, v in alert["metrics"].items()))

    # ---------------- safety gate ablation ----------------
    def harmful(self):
        if not self.watching or not self.busy.acquire(blocking=False):
            return False
        threading.Thread(target=self._harmful, daemon=True).start()
        return True

    def _harmful(self):
        try:
            self.watching = False
            console.rule(f"[bold]Harmful proposals · safety gate {'ON' if self.gate.enabled else 'OFF'}")
            if not self.harmful_applied:          # remember the good state so 'undo' can restore it exactly
                with psycopg.connect(C.dsn(), autocommit=True) as con:
                    self.harm_backup = {
                        "hint": con.execute("SELECT settings::text FROM mada.hints WHERE template = 'A1_revenue_by_category'").fetchone(),
                        "rewrite": con.execute("SELECT sql FROM mada.rewrites WHERE template = 'A3_complaint_products'").fetchone(),
                        "settings": dict(con.execute("SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN "
                                                     "('work_mem', 'hash_mem_multiplier', 'random_page_cost')").fetchall()),
                    }
            props = build_harmful(self.ka.good.get("work_mem", "4MB"))
            for p in props:
                p.resources = [f"demo:{p.id}"]
            applied = self.coord.run_window(props, watch_s=10)
            self.harmful_applied = self.harmful_applied or bool(applied)
            say(f"{len(applied)} of {len(props)} harmful proposals reached production")
        finally:
            self.mon.reset()
            self.watching = True
            self.busy.release()

    def undo_harmful(self):
        b = getattr(self, "harm_backup", None)
        if not b:
            return
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            con.execute("DELETE FROM mada.hints WHERE template = 'A1_revenue_by_category'")
            if b["hint"]:
                con.execute("INSERT INTO mada.hints (template, settings) VALUES ('A1_revenue_by_category', %s::jsonb)", (b["hint"][0],))
            con.execute("DELETE FROM mada.rewrites WHERE template = 'A3_complaint_products'")
            if b["rewrite"]:
                con.execute("INSERT INTO mada.rewrites (template, sql) VALUES ('A3_complaint_products', %s)", (b["rewrite"][0],))
            for name, val in b["settings"].items():
                con.execute(f"ALTER SYSTEM SET {name} = '{val}'")
            con.execute("SELECT pg_reload_conf()")
        self.harmful_applied = False
        say("harmful changes undone: hint, rewrite and settings restored to their state before the harmful batch")

    # ---------------- reset ----------------
    def reset(self):
        if not self.busy.acquire(blocking=False):
            return False
        def run():
            try:
                self.watching = False
                if self.fault:
                    self.end_fault()
                self.phase, self.detail = "preparing", "resetting to the untuned state"
                console.rule("[bold]Reset to untuned")
                self.reset_to_untuned()
                self.tuned, self.diag, self.expected_indexes = False, None, {}
                self.ka = KnobAgent(trials=KNOB_TRIALS)
                self.phase, self.detail = "baseline", "measuring the untuned database for 60 seconds"
                time.sleep(60)
                self.before = summarize(self.rec.window(55))
                self.before_tps = len(self.rec.window(55)) / 55
                self.phase, self.detail = "ready", "untuned - press 'Run tuning cycle'"
            finally:
                self.busy.release()
        threading.Thread(target=run, daemon=True).start()
        return True

    # ---------------- state for the dashboard ----------------
    def state(self):
        now = time.time()
        series = [{"t": r["t"], "tps": r.get("tps"), "oltp": r.get("p95_oltp_ms"), "olap": r.get("p95_olap_ms"),
                   "conn": r.get("connections"), "locks": r.get("lock_waiting"), "cpu": r.get("cpu_pct")}
                  for r in self.col.since(now - 300)] if self.col.series else []
        cur = summarize([x for x in self.rec.window(30) if x[3]])      # successful statements only
        templates = []
        for tpl in TEMPLATES:
            b, c = self.before.get(tpl), cur.get(tpl)
            templates.append({"tpl": tpl, "label": LABELS.get(tpl, tpl), "before": b and round(b["p95"], 1), "now": c and round(c["p95"], 1), "calls": c and c["n"]})
        decisions = [{"id": p.id, "agent": p.agent, "category": p.category, "risk": p.risk, "title": p.title, "decision": d,
                      "reason": why, "status": p.status} for p, d, why in self.coord.log[-40:]]
        info = {}
        try:
            with psycopg.connect(C.dsn(), autocommit=True, connect_timeout=2) as con:
                info["indexes"] = [r[0] for r in con.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'mada_%' ORDER BY 1")]
                info["settings"] = dict(con.execute("SELECT name, setting || coalesce(unit,'') FROM pg_settings WHERE name = ANY(%s)", (list(CATALOG),)).fetchall())
                info["rewrites"] = [r[0] for r in con.execute("SELECT template FROM mada.rewrites")]
                info["hints"] = [f"{t}: {json.dumps(s)}" for t, s in con.execute("SELECT template, settings FROM mada.hints")]
        except psycopg.Error:
            pass
        tps_now = len(self.rec.window(30)) / 30 if self.rec.samples else 0
        return {
            "phase": self.phase, "detail": self.detail, "error": self.error, "tuned": self.tuned, "watching": self.watching,
            "busy": self.busy.locked(), "gate": self.gate.enabled, "auto_approve": self.auto_approve, "pending": self.pending,
            "fault": self.fault[1] if self.fault else None, "faults": self.FAULTS, "incidents": self.incidents[-12:],
            "harmful_applied": self.harmful_applied, "series": series, "templates": templates, "decisions": decisions,
            "before_tps": self.before_tps and round(self.before_tps, 1), "tps_now": round(tps_now, 1),
            "agents": {a: now - t < 4 for a, t in TEE.last_seen.items()}, "db": info, "server_time": now,
        }


def build_harmful(good_wm):
    """Four deliberately harmful proposals, each with real sandbox evidence."""
    a1 = TEMPLATES["A1_revenue_by_category"]
    a3 = TEMPLATES["A3_complaint_products"]
    r = random.Random(5)
    with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
        s.execute("SET statement_timeout = '60s'")
        params = [a1[3](r) for _ in range(2)]

        def run_a1(hashjoin):
            s.execute(f"SET enable_hashjoin = {'on' if hashjoin else 'off'}")
            t = time.perf_counter()
            for p in params:
                s.execute(a1[2], p).fetchall()
            s.execute("RESET enable_hashjoin")
            return (time.perf_counter() - t) * 1000 / len(params)

        base_ms, hint_ms = run_a1(True), run_a1(False)
        ref = dict(s.execute("SELECT template, sql FROM mada.rewrites").fetchall()).get("A3_complaint_products", a3[2])
        bad = ("SELECT p.product_id, p.name, COALESCE(d.cnt, 0) AS bad_reviews FROM products AS p LEFT JOIN (SELECT r.product_id AS k, COUNT(*) AS cnt "
               "FROM reviews AS r GROUP BY r.product_id) AS d ON d.k = p.product_id WHERE p.category = %s ORDER BY bad_reviews DESC, p.product_id LIMIT 20")
        ps = [a3[3](random.Random(21 + i)) for i in range(3)]
        same = all(s.execute(ref, p).fetchall() == s.execute(bad, p).fetchall() for p in ps)
    ka = KnobAgent()
    fq = {"A1_revenue_by_category": 1.0, "A2_top_customers_city": 1.0}
    base_cfg = {k: c["default"] for k, c in CATALOG.items()}
    with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
        s.execute("SET statement_timeout = '60s'")
        kb, _ = ka._replay(s, base_cfg, fq)
        kbad, _ = ka._replay(s, dict(base_cfg, work_mem=1, hash_mem_multiplier=1.0), fq)

    h = Proposal(agent="query", category="performance", title="plan hint for A1: disable hash joins", resources=[],
                 action_sql="INSERT INTO mada.hints VALUES ('A1_revenue_by_category', '{\"enable_hashjoin\": false}') ON CONFLICT (template) DO UPDATE SET settings = EXCLUDED.settings",
                 rollback_sql="DELETE FROM mada.hints WHERE template = 'A1_revenue_by_category'", templates=["A1_revenue_by_category"])
    h.sandbox_check = lambda: {"measured_gain": round(1 - hint_ms / base_ms, 3), "before_ms": round(base_ms), "after_ms": round(hint_ms)}
    k = Proposal(agent="knob", category="performance", title="configuration: work_mem=1MB, hash_mem_multiplier=1.00 (save memory)", resources=[],
                 action_sql="ALTER SYSTEM SET work_mem = '1MB'; ALTER SYSTEM SET hash_mem_multiplier = '1.00'; SELECT pg_reload_conf()",
                 rollback_sql=f"ALTER SYSTEM SET work_mem = '{good_wm}'; ALTER SYSTEM RESET hash_mem_multiplier; SELECT pg_reload_conf()",
                 templates=["A1_revenue_by_category", "A2_top_customers_city"])
    k.sandbox_check = lambda: {"measured_gain": round(1 - kbad / kb, 3), "before_ms": round(kb), "after_ms": round(kbad)}
    q = Proposal(agent="query", category="performance", title="rewrite A3 (faulty rule drops the rating filter)", resources=[],
                 action_sql="INSERT INTO mada.rewrites VALUES ('A3_complaint_products', '" + bad.replace("'", "''") + "') ON CONFLICT (template) DO UPDATE SET sql = EXCLUDED.sql",
                 rollback_sql="DELETE FROM mada.rewrites WHERE template = 'A3_complaint_products'", templates=["A3_complaint_products"])
    q.evidence["equivalent_on_samples"] = same
    q.sandbox_check = lambda: {"measured_gain": 0.5, "results_identical": same}
    o = Proposal(agent="knob", category="performance", title="configuration: random_page_cost=50", resources=[],
                 action_sql="ALTER SYSTEM SET random_page_cost = '50'; SELECT pg_reload_conf()",
                 rollback_sql="ALTER SYSTEM RESET random_page_cost; SELECT pg_reload_conf()", templates=["T1_customer_orders"])
    o.sandbox_check = lambda: {"measured_gain": 0.0}
    return [h, k, q, o]


# ------------------------------------------------------------------ web app
app = Flask(__name__, static_folder=None)
DEMO = Demo()


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


@app.get("/api/state")
def api_state():
    return jsonify(DEMO.state())


@app.get("/api/log")
def api_log():
    return jsonify(TEE.since(int(request.args.get("since", 0))))


@app.post("/api/tune")
def api_tune():
    if DEMO.phase not in ("ready", "watching"):
        return jsonify(ok=False, msg="wait until the baseline is measured"), 409
    return jsonify(ok=DEMO.tune())


@app.post("/api/fault/<key>")
def api_fault(key):
    ok, msg = DEMO.inject(key)
    return jsonify(ok=ok, msg=msg), (200 if ok else 409)


@app.post("/api/fault/end")
def api_fault_end():
    threading.Thread(target=DEMO.end_fault, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/approve")
def api_approve():
    DEMO.decide(bool(request.json.get("ok")))
    return jsonify(ok=True)


@app.post("/api/gate")
def api_gate():
    DEMO.gate.enabled = bool(request.json.get("enabled"))
    say(f"[bold]operator[/] safety gate switched {'ON' if DEMO.gate.enabled else 'OFF'}")
    return jsonify(ok=True, gate=DEMO.gate.enabled)


@app.post("/api/auto_approve")
def api_auto():
    DEMO.auto_approve = bool(request.json.get("enabled"))
    return jsonify(ok=True)


@app.post("/api/harmful")
def api_harmful():
    return jsonify(ok=DEMO.harmful())


@app.post("/api/harmful/undo")
def api_undo():
    DEMO.undo_harmful()
    return jsonify(ok=True)


@app.post("/api/reset")
def api_reset():
    return jsonify(ok=DEMO.reset())


def main():
    DEMO.start()
    print(f"\n  MADA dashboard: http://localhost:{PORT}\n", file=sys.__stdout__)
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
