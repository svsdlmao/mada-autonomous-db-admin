"""Index agent: finds expensive queries, proposes indexes with HypoPG what-if costing, and keeps only
indexes whose gain is confirmed by real execution on the sandbox (the 'AI Meets AI' idea)."""
import difflib
import json
import random
import re
import statistics
import time
import psycopg
import sqlglot
from sqlglot import exp
from .. import config as C
from ..core import Proposal, console, event
from ..workload import TEMPLATES

SKIP = re.compile(r"^\s*(COPY|EXPLAIN|SET|SHOW|BEGIN|COMMIT|ROLLBACK|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|VACUUM|ANALYZE|SELECT\s+(pg_|hypopg|count\(\*\)\s+FROM\s+pg_))", re.I)


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"\$\d+|%s|'[^']*'|\b\d+\b", "?", s)).strip().lower()


def template_for(query):
    best, score = None, 0.0
    for name, (_, _, sql, _) in TEMPLATES.items():
        if not sql:
            continue
        r = difflib.SequenceMatcher(None, norm(sql), norm(query)).ratio()
        if r > score:
            best, score = name, r
    return best if score > 0.9 else None


def existing_index_leads(con):
    """(table, first column) for every existing index, so we never propose a duplicate."""
    rows = con.execute("""SELECT t.relname, a.attname FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
                          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
                          JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public'""").fetchall()
    return {(t, c) for t, c in rows}


def extract_candidates(sql):
    """Columns used for filtering, joining and ordering, resolved to their tables."""
    try:
        tree = sqlglot.parse_one(sql.replace("$", ":p"), read="postgres")
    except Exception:
        return set()
    alias = {}
    for t in tree.find_all(exp.Table):
        alias[t.alias_or_name] = t.name
    tables = set(alias.values())

    def table_of(col):
        if col.table:
            return alias.get(col.table)
        return next(iter(tables)) if len(tables) == 1 else None

    eq, rng, order = {}, {}, {}
    for pred in tree.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        for side in (pred.this, pred.expression):
            if isinstance(side, exp.Column):
                t = table_of(side)
                if t:
                    (eq if isinstance(pred, exp.EQ) else rng).setdefault(t, []).append(side.name)
    for o in tree.find_all(exp.Ordered):
        if isinstance(o.this, exp.Column):
            t = table_of(o.this)
            if t:
                order.setdefault(t, []).append(o.this.name)
    cands = set()
    for t, cols in eq.items():
        for c in dict.fromkeys(cols):
            cands.add((t, (c,)))
            for oc in order.get(t, []):
                if oc != c:
                    cands.add((t, (c, oc)))
            for rc in rng.get(t, []):
                if rc != c:
                    cands.add((t, (c, rc)))
    for t, cols in rng.items():
        for c in dict.fromkeys(cols):
            cands.add((t, (c,)))
    return cands


def ddl(t, cols):
    return f"CREATE INDEX ON {t} ({', '.join(cols)})"


class IndexAgent:
    name = "index"

    def __init__(self, budget_mb=C.INDEX_BUDGET_MB):
        self.budget_mb = budget_mb

    # ---- 1. what is expensive? -------------------------------------------------------------------
    def top_queries(self, con, n=10):
        rows = con.execute("""SELECT query, sum(calls)::bigint, sum(total_exec_time) FROM pg_stat_statements s
                              JOIN pg_database d ON d.oid = s.dbid WHERE d.datname = %s
                              GROUP BY query ORDER BY 3 DESC LIMIT 40""", (C.PROD_DB,)).fetchall()
        out = [(q, c, t) for q, c, t in rows if not SKIP.match(q) and not any(x in q for x in ("mada.", "information_schema", "hypopg"))
               and not re.search(r"\bpg_\w+", q)]          # monitoring and catalog queries are not the workload
        return out[:n]

    # ---- 2. what-if costing with HypoPG ----------------------------------------------------------
    @staticmethod
    def cost(con, q):
        try:
            plan = con.execute("EXPLAIN (GENERIC_PLAN, FORMAT JSON) " + q).fetchone()[0]
            return float(plan[0]["Plan"]["Total Cost"])
        except psycopg.Error:
            return None

    def whatif(self, con, queries, cands):
        """Greedy selection (in the spirit of the 'Extend' algorithm): repeatedly add the hypothetical
        index with the best cost reduction per MB until nothing helps or the budget is used."""
        con.execute("SELECT hypopg_reset()")
        base = {q: self.cost(con, q) for q, _, _ in queries}
        tot = sum(t for _, _, t in queries) or 1.0
        weight = {q: t / tot for q, _, t in queries}          # share of total execution time
        cur = dict(base)
        chosen, used = [], 0.0
        trace = []
        while True:
            best = None
            for cand in sorted(cands - {c for c, _ in chosen}):
                con.execute("SELECT hypopg_reset()")
                for c, _ in chosen:
                    con.execute("SELECT * FROM hypopg_create_index(%s)", (ddl(*c),))
                try:
                    oid = con.execute("SELECT indexrelid FROM hypopg_create_index(%s)", (ddl(*cand),)).fetchone()[0]
                except psycopg.Error:
                    continue                                  # not an indexable user table
                mb = con.execute("SELECT hypopg_relation_size(%s)", (oid,)).fetchone()[0] / 2**20
                after = {q: self.cost(con, q) for q in cur}
                # benefit = time-weighted relative cost reduction across the expensive queries
                gain = sum(weight[q] * (cur[q] - after[q]) / cur[q] for q in cur if cur[q] and after[q])
                if gain > 0.01 and used + mb <= self.budget_mb and (best is None or gain / max(mb, 1.0) > best[2]):
                    best = (cand, mb, gain / max(mb, 1.0), after, gain)
            if not best:
                break
            cand, mb, _, after, gain = best
            helped = [q for q in cur if cur[q] and after[q] and after[q] < 0.9 * cur[q]]
            chosen.append((cand, {"size_mb": round(mb, 1), "benefit": round(gain, 3), "helped": helped,
                                  "cost_before": {q: round(cur[q], 1) for q in helped}, "cost_after": {q: round(after[q], 1) for q in helped}}))
            trace.append((ddl(*cand), round(mb, 1), round(100 * gain, 1)))
            used += mb
            cur = after
        con.execute("SELECT hypopg_reset()")
        # prune redundant picks: an index whose columns are a prefix of another chosen index on the same table
        keep = []
        for c, info in chosen:
            wider = [d for d, _ in chosen if d != c and d[0] == c[0] and d[1][:len(c[1])] == c[1]]
            if wider:
                trace.append((ddl(*c), info["size_mb"], "pruned: covered by " + ddl(*wider[0])))
            else:
                keep.append((c, info))
        return keep, base, trace

    # ---- 3. confirm on the sandbox with real execution ------------------------------------------
    @staticmethod
    def _time_template(scon, tpl, runs=7, seed=11, rewrites=None):
        _, _, sql, pf = TEMPLATES[tpl]
        sql = (rewrites or {}).get(tpl, sql)      # time the SQL production actually runs
        r = random.Random(seed)
        params = [pf(r) for _ in range(runs)]
        ts = []
        for i, p in enumerate(params):
            t = time.perf_counter()
            scon.execute(sql, p).fetchall()
            ts.append((time.perf_counter() - t) * 1000)
            if i == 2 and statistics.median(ts) > 1000:   # slow query: 3 runs are enough
                break
        return statistics.median(ts)

    @staticmethod
    def _time_writes(scon, n=80):
        ts = []
        for i in range(n):
            t = time.perf_counter()
            with scon.transaction(force_rollback=True):
                scon.execute("INSERT INTO orders VALUES (900000000 + %s, 1, now(), 'placed', 10)", (i,))
                scon.execute("INSERT INTO order_items VALUES (900000000 + %s, 900000000 + %s, 7, 1, 10)", (i, i))
                scon.execute("INSERT INTO reviews VALUES (900000000 + %s, 7, 1, 4, now(), 'ok')", (i,))
            ts.append((time.perf_counter() - t) * 1000)
        return statistics.quantiles(ts, n=5)[0]   # 20th percentile: load spikes on the shared host only add time

    def sandbox_check(self, cand, templates):
        """Create the index for real on the sandbox, time the affected templates and the write path.
        Write cost (20th percentile of 80 rolled-back order transactions) is measured before, with, and again
        after dropping the index (A/B/A) so that load drift on the shared host cancels out; differences under
        0.5 ms per transaction count as noise."""
        with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
            s.execute("SET statement_timeout = '60s'")
            rw = dict(s.execute("SELECT template, sql FROM mada.rewrites").fetchall())
            before = {t: self._time_template(s, t, rewrites=rw) for t in templates}
            w_before = self._time_writes(s)
            name = f"mada_chk_{cand[0]}_{'_'.join(cand[1])}"
            s.execute(f"CREATE INDEX {name} ON {cand[0]} ({', '.join(cand[1])})")
            after = {t: self._time_template(s, t, rewrites=rw) for t in templates}
            w_after = self._time_writes(s)
            s.execute(f"DROP INDEX {name}")
            w_before2 = self._time_writes(s)
        gain = 1 - sum(after.values()) / max(1e-9, sum(before.values()))
        w_base = (w_before + w_before2) / 2
        w_over = 0.0 if (w_after - w_base) < 0.5 else w_after / w_base - 1
        return {"before_ms": {k: round(v, 1) for k, v in before.items()}, "after_ms": {k: round(v, 1) for k, v in after.items()},
                "measured_gain": round(gain, 3), "write_overhead": round(w_over, 3),
                "write_ms": [round(w_before, 2), round(w_after, 2), round(w_before2, 2)]}

    # ---- main entry ----------------------------------------------------------------------------
    def propose(self, focus_table=None):
        with psycopg.connect(C.dsn(), autocommit=True, prepare_threshold=None) as con:
            queries = self.top_queries(con)
            have = existing_index_leads(con)
            cands = set()
            for q, _, _ in queries:
                cands |= extract_candidates(q)
            user_tables = {r[0] for r in con.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
            cands = {c for c in cands if c[0] in user_tables}
            cands = {c for c in cands if (c[0], c[1][0]) not in have or len(c[1]) > 1 and (c[0], c[1][0]) not in have}
            if focus_table:
                cands = {c for c in cands if c[0] == focus_table}
            console.log(f"[bold cyan]index-agent[/] {len(queries)} expensive queries · {len(cands)} candidate indexes")
            chosen, base, trace = self.whatif(con, queries, cands)
        for d, mb, g in trace:
            if isinstance(g, str):
                console.log(f"[cyan]index-agent[/] [dim]{d:<48} {g}[/]")
            else:
                console.log(f"[cyan]index-agent[/] what-if {d:<48} ≈{mb:>5} MB   weighted cost −{g}%")
        props = []
        for cand, info in chosen:
            templates = sorted({template_for(q) for q in info["helped"]} - {None})
            if not templates:
                continue
            p = Proposal(agent=self.name, category="performance", title=f"index on {cand[0]}({', '.join(cand[1])})",
                         action_sql=f"CREATE INDEX CONCURRENTLY mada_{cand[0]}_{'_'.join(cand[1])} ON {cand[0]} ({', '.join(cand[1])})",
                         rollback_sql=f"DROP INDEX CONCURRENTLY IF EXISTS mada_{cand[0]}_{'_'.join(cand[1])}",
                         resources=[f"table:{cand[0]}", "disk", "planner"], risk="low",
                         evidence={"what_if_size_mb": info["size_mb"], "templates": templates},
                         templates=templates)
            p.sandbox_check = (lambda cand=cand, templates=templates: self.sandbox_check(cand, templates))
            props.append(p)
            event("proposal", self.name, p.title, templates=templates)
        return props
