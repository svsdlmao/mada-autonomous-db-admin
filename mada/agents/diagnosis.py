"""Diagnosis agent: explains an anomaly.

1. DBSherlock-style predicates: which metrics separate the anomaly window from normal time.
2. Knowledge base: each root cause has a metric signature and a probe (a SQL check that confirms it).
3. Ranked causes with evidence; the fix is routed to the agent that owns it.
The explanation text comes from the knowledge base (rule mode) or an LLM when one is configured."""
import numpy as np
import psycopg
from .. import config as C
from ..core import Proposal, console, event, llm


def _sep(anom, base, key):
    a = np.array([r[key] for r in anom if key in r], float)
    b = np.array([r[key] for r in base if key in r], float)
    if len(a) == 0 or len(b) < 3:
        return 0.0, 0.0, 0.0
    std = max(b.std(), 0.15 * abs(b.mean()), 1.0)
    return (a.mean() - b.mean()) / std, float(b.mean()), float(a.mean())


KB = [
    # cause, signature {metric: direction}, probe name, owner agent
    ("lock_contention", {"lock_waiting": 1, "locks_not_granted": 1, "p95:T4_place_order": 1}, "probe_blocker", "diagnosis"),
    ("work_mem_spill", {"temp_bytes/s": 1, "p95_olap_ms": 1, "p95:A1_revenue_by_category": 1}, "probe_work_mem", "knob"),
    ("missing_index", {"seq_tup:orders/s": 1, "seq_tup:order_items/s": 1, "seq_tup:reviews/s": 1, "p95_oltp_ms": 1, "p95:T1_customer_orders": 1}, "probe_indexes", "index"),
    ("table_bloat", {"dead:orders": 1, "dead:order_items": 1, "p95_oltp_ms": 1}, "probe_bloat", "diagnosis"),
    ("connection_storm", {"connections": 1, "idle": 1}, "probe_connections", "diagnosis"),
    ("cpu_saturation", {"cpu_pct": 1, "p95_oltp_ms": 1, "tps": -1}, "probe_cpu", "monitor"),
]

EXPLAIN = {
    "lock_contention": "Sessions are queued on row locks held by a long-running transaction; order placement waits until it ends.",
    "work_mem_spill": "Sorts and hash joins no longer fit in work_mem and spill to temporary files, so analytical queries slow down.",
    "missing_index": "An index the workload depends on is missing, so lookups fall back to sequential scans of a large table.",
    "table_bloat": "Dead row versions are piling up because vacuum is not running, so every scan reads far more pages than live rows.",
    "connection_storm": "A burst of client connections, most of them idle, is consuming connection slots and memory.",
    "cpu_saturation": "The host CPU is saturated by something outside the database; queries wait for CPU time.",
}


class DiagnosisAgent:
    name = "diagnosis"

    def __init__(self, index_agent=None, knob_agent=None, expected_indexes=None):
        self.index_agent = index_agent
        self.knob_agent = knob_agent
        self.expected_indexes = expected_indexes or {}   # name -> CREATE statement (applied earlier by MADA)
        with open("/proc/loadavg") as f:                  # load under normal traffic, for the CPU probe
            self.base_load = float(f.read().split()[0])

    # ---- probes: SQL checks that confirm a cause and gather what the fix needs ---------------------
    def probe_blocker(self, con):
        rows = con.execute("""SELECT b.pid, b.usename, now() - b.xact_start AS age, left(b.query, 60), count(*) AS waiters
                              FROM pg_stat_activity w JOIN LATERAL unnest(pg_blocking_pids(w.pid)) bp(pid) ON true
                              JOIN pg_stat_activity b ON b.pid = bp.pid GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 1""").fetchall()
        if not rows:
            return False, {}
        pid, user, age, q, waiters = rows[0]
        return True, {"blocker_pid": pid, "user": user, "xact_age_s": round(age.total_seconds(), 1), "query": q, "waiters": waiters}

    def probe_work_mem(self, con):
        kb = int(con.execute("SELECT setting FROM pg_settings WHERE name = 'work_mem'").fetchone()[0])
        return kb < 4096, {"work_mem": f"{kb}kB"}

    def probe_indexes(self, con):
        have = {r[0] for r in con.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
        missing = {n: d for n, d in self.expected_indexes.items() if n not in have}
        return bool(missing), {"missing": list(missing)}

    def probe_bloat(self, con):
        rows = con.execute("""SELECT s.relname, n_dead_tup, n_live_tup, coalesce(reloptions::text, '') FROM pg_stat_user_tables s
                              JOIN pg_class c ON c.oid = s.relid ORDER BY n_dead_tup DESC LIMIT 1""").fetchall()
        rel, dead, live, opts = rows[0]
        return dead >= 10_000 and dead > 0.2 * max(live, 1), {"table": rel, "dead": dead, "live": live, "autovacuum_off": "autovacuum_enabled=false" in opts}

    def probe_connections(self, con):
        rows = con.execute("""SELECT usename, count(*) FILTER (WHERE state = 'idle'), count(*) FROM pg_stat_activity
                              WHERE datname = %s AND backend_type = 'client backend' GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", (C.PROD_DB,)).fetchall()
        user, idle, total = rows[0]
        return idle > 20, {"user": user, "idle": idle, "total": total}

    def probe_cpu(self, con):
        with open("/proc/loadavg") as f:
            load = float(f.read().split()[0])
        return load > 1.5 * self.base_load + 1, {"loadavg_1m": load, "normal": self.base_load}

    # ---- main entry ---------------------------------------------------------------------------------
    def diagnose(self, anomaly_rows, baseline_rows, pre_rows=None):
        """pre_rows: telemetry from just before the fault. When there is enough of it, the anomaly is compared
        with that window rather than the long baseline, so leftovers of an earlier incident do not look new."""
        ref = pre_rows if pre_rows and len(pre_rows) >= 5 else baseline_rows
        keys = sorted({k for r in anomaly_rows for k in r} & {k for r in ref for k in r} - {"t"})
        preds = []
        for k in keys:
            z, b, a = _sep(anomaly_rows, ref, k)
            if abs(z) >= 3:
                preds.append((k, z, b, a))
        preds.sort(key=lambda x: -abs(x[1]))
        zmap = {k: z for k, z, _, _ in preds}
        scored = []
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            for cause, sig, probe, owner in KB:
                score = sum(min(10.0, max(0.0, d * zmap.get(m, 0.0))) for m, d in sig.items()) / len(sig)   # one huge metric cannot dominate
                confirmed, facts = getattr(self, probe)(con)
                if confirmed:
                    score = score * 1.5 + 2.0
                scored.append((cause, round(score, 2), confirmed, facts, owner))
        scored.sort(key=lambda x: (not x[2], -x[1]))          # a cause its SQL probe confirmed ranks above any unconfirmed one
        top = scored[:3]
        cause, score, confirmed, facts, owner = top[0]
        text = llm.explain({"cause": cause, "facts": facts, "predicates": preds[:5]}, EXPLAIN[cause])
        console.log(f"[bold red]diagnosis[/] predicates separating the anomaly from normal time:")
        for k, z, b, a in preds[:5]:
            console.log(f"[red]diagnosis[/]   {k:<28} normal {b:>12,.1f}  →  now {a:>12,.1f}   (separation {z:+.1f}σ)")
        for i, (c, s, ok, f, o) in enumerate(top, 1):
            console.log(f"[red]diagnosis[/]   #{i} {c:<18} score {s:>6.2f}  probe {'confirmed' if ok else 'not confirmed'}  {f if ok else ''}")
        console.log(f"[bold red]diagnosis[/] root cause: {cause} — {text}")
        event("diagnosis", self.name, f"{cause}: {text}", ranked=[c for c, *_ in top], facts=facts)
        return {"cause": cause, "ranked": [c for c, *_ in top], "facts": facts, "text": text, "predicates": preds[:5], "owner": owner,
                "confirmed": confirmed}

    # ---- fixes routed by cause ----------------------------------------------------------------------
    def fix(self, d):
        if not d.get("confirmed", True):
            console.log("[red]diagnosis[/] top cause not confirmed by its probe - no automatic fix, operator informed")
            return None
        c, f = d["cause"], d["facts"]
        if c == "lock_contention" and f.get("blocker_pid"):
            return Proposal(agent=self.name, category="availability", title=f"end blocking session pid {f['blocker_pid']} ({f['user']}, open {f['xact_age_s']} s)",
                            action_sql=f"SELECT pg_terminate_backend({f['blocker_pid']})", rollback_sql="-- not reversible: the client must retry its transaction",
                            resources=[f"session:{f['blocker_pid']}", "table:products"], risk="high", reversible=False, evidence=f,
                            templates=["T4_place_order"])
        if c == "table_bloat":
            t = f["table"]
            return Proposal(agent=self.name, category="availability", title=f"VACUUM (ANALYZE) {t} and re-enable autovacuum",
                            action_sql=f"ALTER TABLE {t} RESET (autovacuum_enabled); VACUUM (ANALYZE) {t}", rollback_sql="-- nothing to undo: VACUUM only removes dead row versions",
                            resources=[f"table:{t}", "io"], risk="medium", reversible=False, evidence=f,
                            templates=["T1_customer_orders", "A1_revenue_by_category"])
        if c == "connection_storm":
            return Proposal(agent=self.name, category="availability", title=f"end {f['idle']} idle sessions of role {f['user']}",
                            action_sql=f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{f['user']}' AND state = 'idle' AND state_change < now() - interval '5 seconds'",
                            rollback_sql="-- not reversible: clients reconnect", resources=[f"role:{f['user']}"], risk="high", reversible=False, evidence=f)
        if c == "work_mem_spill" and self.knob_agent:
            return self.knob_agent.repair("work_mem", f.get("work_mem"))
        if c == "missing_index" and f.get("missing"):
            name = f["missing"][0]
            stmt = self.expected_indexes[name]
            return Proposal(agent="index", category="availability", title=f"recreate missing index {name}",
                            action_sql=stmt.replace("CREATE INDEX", "CREATE INDEX CONCURRENTLY", 1), rollback_sql=f"DROP INDEX CONCURRENTLY IF EXISTS {name}",
                            resources=[f"table:{stmt.split(' ON ')[1].split()[0]}", "disk"], risk="low", evidence=f,
                            templates=["T1_customer_orders", "S1_support_lookup"])
        return None
