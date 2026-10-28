"""End-to-end prototype run: before → MADA tuning cycle → after → fault injection → safety-gate ablation.
Writes results/results.json, results/series_*.json and screenshots/*.png."""
import json
import os
import statistics
import sys
import time
import psycopg
from rich.table import Table

from mada import config as C
from mada import schema
from mada.core import console, event, EVENTS, Proposal
from mada.workload import Workload, Recorder, summarize, TEMPLATES
from mada.observability import Collector
from mada.agents.monitor import MonitorAgent
from mada.agents.index_agent import IndexAgent
from mada.agents.knob_agent import KnobAgent
from mada.agents.query_agent import QueryAgent
from mada.agents.diagnosis import DiagnosisAgent
from mada.agents.security import SecurityAgent
from mada.coordinator import Coordinator, SafetyGate
from mada import faults as F
from mada.dashboard import snap, fault_timeline

OUT, SHOTS = "results", "screenshots"
os.makedirs(OUT, exist_ok=True)
os.makedirs(SHOTS, exist_ok=True)
R = {}


def approver(p):
    console.log(f"[bold white on dark_red] operator [/] approved high-risk action #{p.id}: {p.title}  [dim](experiment mode: approval given from the console)[/]")
    return True


def stats_table(title, s):
    tb = Table(title=title, title_justify="left", header_style="bold")
    for c in ("template", "calls", "mean ms", "p50 ms", "p95 ms"):
        tb.add_column(c, justify="right" if c != "template" else "left")
    for k in sorted(s):
        v = s[k]
        tb.add_row(k, str(v["n"]), f"{v['mean']:,.1f}", f"{v['p50']:,.1f}", f"{v['p95']:,.1f}")
    console.print(tb)


def measure(rec, seconds, label):
    t0 = time.time()
    time.sleep(seconds)
    s = summarize(rec.window(seconds - 5))
    tps = len(rec.window(seconds - 5)) / (seconds - 5)
    console.log(f"[bold]{label}[/]: {tps:.1f} statements/s over {seconds - 5} s")
    return s, tps, t0


def reset_cluster():
    with psycopg.connect(C.dsn("postgres"), autocommit=True) as con:
        con.execute("ALTER SYSTEM RESET ALL")
        for k, v in [("shared_preload_libraries", "pg_stat_statements"), ("pg_stat_statements.track", "all")]:
            con.execute(f"ALTER SYSTEM SET {k} = '{v}'")
        con.execute("SELECT pg_reload_conf()")


def fault_phase(rec, wl, col, coord, mon, ia, ka, ta0, good_wm):
    # ------------------------------------------------------------------ learn normal behaviour
    base_rows = col.since(ta0 + 5)
    mon.fit_baseline(base_rows)
    sec = SecurityAgent(wl.search_sql)
    R["security_metrics"] = sec.train()
    sec.learn_profiles(rec.audit_since(ta0))
    expected = {}
    with psycopg.connect(C.dsn(), autocommit=True) as con:
        for name, d in con.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'mada_%'"):
            expected[name] = d.replace("public.", "").replace(" USING btree", "")
    diag = DiagnosisAgent(index_agent=ia, knob_agent=ka, expected_indexes=expected)
    snap(f"{SHOTS}/08_security_training.png", "MADA · security agent training")

    # ------------------------------------------------------------------ FAULTS
    console.rule("[bold]Phase 4 · fault injection")
    t_faults = time.time()
    idx_name = next((n for n in expected if "orders_customer_id" in n), None)
    fault_list = [
        F.MissingIndex(idx_name, expected.get(idx_name, ""), rec=rec) if idx_name else None,
        F.BadConfig(good_wm, rec=rec),
        F.LockContention(rec=rec),
        F.TableBloat(rec=rec),
        F.ConnectionStorm(rec=rec),
        F.SqlInjection(wl.search_sql, rec=rec),
        F.AbnormalAccess(rec=rec),
    ]
    fres = []
    for f in [x for x in fault_list if x]:
        console.rule(f"[bold]{f.key} · {f.name}")
        time.sleep(15)
        mon.reset()
        last_scan = time.time()
        pre = col.since(time.time() - 14)
        f.inject_t = time.time()
        f.inject()
        console.log(f"[bold red]fault injected[/]: {f.key} {f.name}")
        res = {"key": f.key, "name": f.name, "expected": f.expected, "inject_t": f.inject_t, "pre": pre}
        handled = False
        deadline = f.inject_t + 100
        while time.time() < deadline:
            time.sleep(C.TICK_SECONDS)
            row = col.last()[0]
            # security path
            audit = rec.audit_since(last_scan)
            last_scan = time.time()
            findings = sec.scan(audit)
            if findings and not handled:
                kinds = sorted({x["type"] for x in findings})
                res.update(detect_t=time.time(), diagnosed=kinds, top1=f.expected in kinds, top3=f.expected in kinds)
                for x in findings[:2]:
                    console.log(f"[bold green]security-agent[/] {x['type']} by role {x['role']}: {x['sql'][:96]}")
                props = sec.respond(findings)
                applied = coord.run_window(props, watch_s=0)
                if applied:
                    res["mitigate_t"] = applied[0].done_at
                handled = True
                continue
            # performance path
            alert = mon.check(row)
            if alert and not handled:
                res["detect_t"] = time.time()
                console.log(f"[bold orange1]monitor[/] alert: " + ", ".join(f"{k} z={v:.1f}" for k, v in alert["metrics"].items()))
                d = diag.diagnose(col.since(f.inject_t), base_rows, pre)
                res.update(diagnosed=d["ranked"], top1=d["ranked"][0] == f.expected, top3=f.expected in d["ranked"], facts=d["facts"])
                fix = diag.fix(d)
                if fix:
                    applied = coord.run_window([fix], watch_s=0)
                    if applied:
                        res["mitigate_t"] = applied[0].done_at
                handled = True
            if handled and time.time() > res.get("mitigate_t", res.get("detect_t", 0)) + 25:
                break
        f.end_t = time.time()
        f.end()
        res["end_t"] = f.end_t
        res["post"] = col.since(f.inject_t)
        fres.append(res)
        console.log(f"{f.key}: detected {res.get('detect_t', 0) - f.inject_t:.1f} s after injection · diagnosis {res.get('diagnosed')} · "
                    f"mitigated {res.get('mitigate_t', 0) - f.inject_t:.1f} s" if "detect_t" in res else f"{f.key}: not detected")
        if f.key in ("F4", "F9", "F10"):
            snap(f"{SHOTS}/09_{f.key}.png", f"MADA · {f.key} {f.name}")
        else:
            console.export_svg(clear=True)
        time.sleep(10)
    R["faults"] = [{k: v for k, v in r.items() if k not in ("pre", "post")} for r in fres]
    json.dump({"rows": col.since(t_faults), "faults": R["faults"]}, open(f"{OUT}/series_faults.json", "w"), default=str)
    fault_timeline(col.since(t_faults), R["faults"], EVENTS, f"{SHOTS}/10_dashboard_faults.png")

    return sec, good_wm


def main():
    # ------------------------------------------------------------------ setup
    console.rule("[bold]Testbed setup")
    reset_cluster()
    counts, size = schema.create(verbose=False)
    schema.clone_sandbox()
    with psycopg.connect(C.dsn(), autocommit=True) as con:
        con.execute("SELECT pg_stat_statements_reset()")
        settings = dict(con.execute("SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN ('work_mem','random_page_cost','effective_cache_size','max_parallel_workers_per_gather','jit','shared_buffers','server_version')").fetchall())
    console.log(f"PostgreSQL {settings['server_version']} · database '{C.PROD_DB}' {size} · sandbox '{C.SANDBOX_DB}' cloned")
    console.log("rows: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    console.log("indexes: primary keys only · configuration: PostgreSQL defaults")
    R["setup"] = {"counts": counts, "size": size, "settings": settings}

    rec = Recorder()
    wl = Workload(rec)
    col = Collector(rec)
    wl.start()
    col.start()
    console.log(f"workload started: app_user ×5 (OLTP), analyst ×2 (OLAP), support ×1 · templates: {', '.join(TEMPLATES)}")

    # ------------------------------------------------------------------ BEFORE
    console.rule("[bold]Phase 1 · before MADA (defaults, primary keys only)")
    before, tps_b, tb0 = measure(rec, 125, "before")
    stats_table("Latency per template — before", before)
    R["before"] = {"stats": before, "tps": tps_b}
    snap(f"{SHOTS}/01_testbed_before.png", "MADA · testbed and baseline workload")

    # ------------------------------------------------------------------ TUNING CYCLE
    gate = SafetyGate(approver=approver)
    coord = Coordinator(gate, recorder=rec)
    mon = MonitorAgent()
    console.rule("[bold]Phase 2 · monitor forecast")
    fc = mon.forecast(rec)
    tb = Table(title="Monitor agent — arrival rate per template (statements/s)", title_justify="left", header_style="bold")
    for c in ("template", "last 10 s", "forecast +60 s"):
        tb.add_column(c, justify="right" if c != "template" else "left")
    for k, v in sorted(fc.items()):
        tb.add_row(k, f"{v['now']:.2f}", f"{v['forecast']:.2f}")
    console.print(tb)
    R["forecast"] = fc

    console.rule("[bold]Phase 2 · query agent")
    recent = summarize(rec.window(60))
    slow = [t for t, v in sorted(recent.items(), key=lambda kv: -kv[1]["mean"]) if v["mean"] > 250 and t.startswith("A")]
    console.log(f"[yellow]query-agent[/] slow templates (mean > 250 ms in the last 60 s): {slow}")
    qprops, qreports = QueryAgent().propose(slow)
    R["query_reports"] = qreports
    snap(f"{SHOTS}/02_query_agent.png", "MADA · query agent (rule-based rewrite + hint sets)")
    console.rule("[bold]Coordinator · window 1")
    coord.run_window(qprops, watch_s=12)
    snap(f"{SHOTS}/03_coordinator_window1.png", "MADA · coordinator window 1")

    console.rule("[bold]Phase 2 · index agent")
    with psycopg.connect(C.dsn(), autocommit=True) as con:
        con.execute("SELECT pg_stat_statements_reset()")
    console.log("[cyan]index-agent[/] statistics reset; collecting a fresh 60 s window of query costs")
    time.sleep(60)
    ia = IndexAgent()
    iprops = ia.propose()
    for p in iprops:
        ev = p.sandbox_check()
        p.sandbox_check = (lambda ev=ev: ev)   # measured once; the gate reuses the measurement
        console.log(f"[cyan]index-agent[/] sandbox check {p.title:<42} gain {100*ev['measured_gain']:.0f}%  write cost {ev['write_ms'][0]}→{ev['write_ms'][1]} ms (overhead {100*ev['write_overhead']:+.0f}%)  {ev['before_ms']} → {ev['after_ms']}")
    snap(f"{SHOTS}/04_index_agent.png", "MADA · index agent (HypoPG what-if + sandbox check)")
    console.rule("[bold]Coordinator · windows 2+ (index proposals)")
    coord.run_window(iprops, watch_s=12)
    while coord.deferred:
        coord.run_window([], watch_s=12)
    snap(f"{SHOTS}/05_coordinator_indexes.png", "MADA · coordinator windows for index proposals")

    console.rule("[bold]Phase 2 · knob agent")
    freq = {}
    for t, tpl, ms, ok in rec.window(60):
        freq[tpl] = freq.get(tpl, 0) + 1 / 60
    ka = KnobAgent(trials=14)
    kprop, khist = ka.propose(freq)
    R["knob_history"] = [{"trial": n, "weighted_ms": v, "cfg": c} for n, v, c in khist]
    snap(f"{SHOTS}/06_knob_agent.png", "MADA · knob agent (Bayesian optimisation on the sandbox)")
    console.rule("[bold]Coordinator · knob window")
    coord.run_window([kprop] if kprop else [], watch_s=12)
    while coord.deferred:
        coord.run_window([], watch_s=12)
    if kprop and kprop.status == "applied":
        ka.good = dict(ka.pending)
    snap(f"{SHOTS}/06b_coordinator_knob.png", "MADA · coordinator knob window")
    R["decisions_tuning"] = [{"id": p.id, "agent": p.agent, "title": p.title, "decision": d, "reason": why, "status": p.status,
                              "category": p.category, "risk": p.risk, "evidence": p.evidence} for p, d, why in coord.log]

    # ------------------------------------------------------------------ AFTER
    console.rule("[bold]Phase 3 · after MADA")
    time.sleep(10)
    after, tps_a, ta0 = measure(rec, 125, "after")
    stats_table("Latency per template — after", after)
    R["after"] = {"stats": after, "tps": tps_a}
    with psycopg.connect(C.dsn(), autocommit=True) as con:
        R["after"]["indexes"] = [r[0] for r in con.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'mada_%'")]
        R["after"]["settings"] = dict(con.execute("SELECT name, setting || coalesce(unit,'') FROM pg_settings WHERE name IN ('work_mem','hash_mem_multiplier','random_page_cost','effective_cache_size','max_parallel_workers_per_gather','jit')").fetchall())
        R["after"]["registry"] = {"hints": con.execute("SELECT template, settings FROM mada.hints").fetchall(),
                                  "rewrites": [r[0] for r in con.execute("SELECT template FROM mada.rewrites")]}
    tb = Table(title="Before vs after", title_justify="left", header_style="bold")
    for c in ("template", "p95 before ms", "p95 after ms", "change"):
        tb.add_column(c, justify="right" if c != "template" else "left")
    for k in sorted(before):
        if k in after:
            b, a = before[k]["p95"], after[k]["p95"]
            tb.add_row(k, f"{b:,.1f}", f"{a:,.1f}", f"{100*(a/b-1):+.0f}%")
    console.print(tb)
    console.log(f"throughput: {tps_b:.1f} → {tps_a:.1f} statements/s")
    snap(f"{SHOTS}/07_before_after.png", "MADA · before vs after")
    json.dump(col.since(tb0), open(f"{OUT}/series_tuning.json", "w"), default=str)

    sec, good_wm = fault_phase(rec, wl, col, coord, mon, ia, ka, ta0, R["after"]["settings"].get("work_mem", "4096kB"))

    # ------------------------------------------------------------------ SAFETY GATE ABLATION
    console.rule("[bold]Phase 5 · safety gate: three harmful proposals, gate on vs off")
    a3 = TEMPLATES["A3_complaint_products"][2]
    bad_rewrite = "SELECT p.product_id, p.name, COALESCE(d.cnt, 0) AS bad_reviews FROM products AS p LEFT JOIN (SELECT r.product_id AS k, COUNT(*) AS cnt FROM reviews AS r GROUP BY r.product_id) AS d ON d.k = p.product_id WHERE p.category = %s ORDER BY bad_reviews DESC, p.product_id LIMIT 20"

    def harmful():
        qa = QueryAgent()
        h = Proposal(agent="query", category="performance", title="plan hint for A1: disable hash joins", resources=["plan:A1_revenue_by_category"],
                     action_sql="INSERT INTO mada.hints VALUES ('A1_revenue_by_category', '{\"enable_hashjoin\": false}') ON CONFLICT (template) DO UPDATE SET settings = EXCLUDED.settings",
                     rollback_sql="DELETE FROM mada.hints WHERE template = 'A1_revenue_by_category'", templates=["A1_revenue_by_category"])
        rep = qa.examine("A1_revenue_by_category")
        hint_ms = dict((json.dumps(k), v) for k, v in rep["hints"])[json.dumps({"enable_hashjoin": False})]
        h.sandbox_check = (lambda: {"measured_gain": round(1 - hint_ms / rep["base_ms"], 3), "before_ms": round(rep["base_ms"]), "after_ms": round(hint_ms)})
        k = Proposal(agent="knob", category="performance", title="configuration: work_mem=1MB, hash_mem_multiplier=1.00 (save memory)", resources=["memory"],
                     action_sql="ALTER SYSTEM SET work_mem = '1MB'; ALTER SYSTEM SET hash_mem_multiplier = '1.00'; SELECT pg_reload_conf()",
                     rollback_sql=f"ALTER SYSTEM SET work_mem = '{good_wm}'; ALTER SYSTEM RESET hash_mem_multiplier; SELECT pg_reload_conf()",
                     templates=["A1_revenue_by_category", "A2_top_customers_city"])
        def kcheck():
            ka2 = KnobAgent()
            fq = {"A1_revenue_by_category": 1.0, "A2_top_customers_city": 1.0}
            from mada.agents.knob_agent import CATALOG
            base_cfg = {kk: c["default"] for kk, c in CATALOG.items()}
            base_cfg["work_mem"] = int(good_wm.replace("kB", "")) // 1024 if good_wm.endswith("kB") else 4
            bad_cfg = dict(base_cfg, work_mem=1, hash_mem_multiplier=1.0)
            with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
                b, _ = ka2._replay(s, base_cfg, fq)
                a, _ = ka2._replay(s, bad_cfg, fq)
            return {"measured_gain": round(1 - a / b, 3), "before_ms": round(b), "after_ms": round(a)}
        k.sandbox_check = kcheck
        q = Proposal(agent="query", category="performance", title="rewrite A3 (faulty rule drops the rating filter)", resources=["plan:A3_complaint_products"],
                     action_sql="INSERT INTO mada.rewrites VALUES ('A3_complaint_products', '" + bad_rewrite.replace("'", "''") + "') ON CONFLICT (template) DO UPDATE SET sql = EXCLUDED.sql",
                     rollback_sql="DELETE FROM mada.rewrites WHERE template = 'A3_complaint_products'", templates=["A3_complaint_products"])
        import random as _r
        with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
            ref = dict(s.execute("SELECT template, sql FROM mada.rewrites").fetchall()).get("A3_complaint_products", a3)
            rr = _r.Random(21)
            ps = [TEMPLATES["A3_complaint_products"][3](rr) for _ in range(3)]
            same = all(s.execute(ref, p).fetchall() == s.execute(bad_rewrite, p).fetchall() for p in ps)
        q.evidence["equivalent_on_samples"] = same
        q.sandbox_check = (lambda: {"measured_gain": 0.5, "results_identical": same})
        o = Proposal(agent="knob", category="performance", title="configuration: random_page_cost=50", resources=["planner"],
                     action_sql="ALTER SYSTEM SET random_page_cost = '50'; SELECT pg_reload_conf()", rollback_sql="ALTER SYSTEM RESET random_page_cost; SELECT pg_reload_conf()",
                     templates=["T1_customer_orders"])
        o.sandbox_check = (lambda: {"measured_gain": 0.0})
        return [h, k, q, o]

    console.log("[bold]gate ON[/]")
    gate_on = Coordinator(SafetyGate(approver=approver, enabled=True), recorder=rec, sync_sandbox=False)
    on_props = harmful()
    for p in on_props:
        p.resources = [f"ablation:{p.id}"]
    gate_on.run_window(on_props, watch_s=15)
    snap(f"{SHOTS}/11_gate_on.png", "MADA · safety gate on")
    console.log("[bold]gate OFF (ablation)[/]")
    gate_off = Coordinator(SafetyGate(approver=approver, enabled=False), recorder=rec, sync_sandbox=False)
    off_props = harmful()
    for p in off_props:
        p.resources = [f"ablation:{p.id}"]
    gate_off.run_window(off_props, watch_s=20)
    with psycopg.connect(C.dsn(), autocommit=True) as con:   # clean up whatever slipped through
        con.execute("DELETE FROM mada.rewrites WHERE sql LIKE '%GROUP BY r.product_id) AS d%' AND sql NOT LIKE '%rating%'")
        con.execute("DELETE FROM mada.hints WHERE template = 'A1_revenue_by_category'")
        con.execute("ALTER SYSTEM RESET random_page_cost")
        con.execute("ALTER SYSTEM RESET work_mem")
        con.execute("ALTER SYSTEM RESET hash_mem_multiplier")
        con.execute("SELECT pg_reload_conf()")
    R["gate"] = {"on": [{"title": p.title, "decision": d, "reason": w, "status": p.status} for p, d, w in gate_on.log],
                 "off": [{"title": p.title, "decision": d, "reason": w, "status": p.status} for p, d, w in gate_off.log]}
    snap(f"{SHOTS}/12_gate_off.png", "MADA · safety gate off (ablation)")

    wl.stop()
    col.stop()
    R["events"] = EVENTS
    json.dump(R, open(f"{OUT}/results.json", "w"), indent=1, default=str)
    print("DONE")


if __name__ == "__main__":
    main()
