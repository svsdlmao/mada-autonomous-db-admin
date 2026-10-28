"""Query agent: for slow templates, tries plan hint sets (Bao-style) and rule-based rewrites
(LLM-R2-style: a fixed rule set, never free-form SQL), verifies equivalence, measures on the sandbox."""
import json
import random
import statistics
import time
import psycopg
import sqlglot
from sqlglot import exp
from .. import config as C
from ..core import Proposal, console, event
from ..workload import TEMPLATES

HINT_SETS = [
    {}, {"enable_nestloop": False}, {"enable_hashjoin": False}, {"enable_mergejoin": False},
    {"enable_seqscan": False}, {"jit": False}, {"enable_nestloop": False, "enable_mergejoin": False},
]


def _to_ast(sql):
    return sqlglot.parse_one(sql.replace("%s", ":p1"), read="postgres")


def _from_ast(tree):
    return tree.sql(dialect="postgres").replace("%(p1)s", "%s").replace(":p1", "%s")


def rule_decorrelate_count(sql):
    """R1: a correlated scalar COUNT(*) subquery in the SELECT list becomes a LEFT JOIN to a grouped
    derived table, so the inner table is scanned once instead of once per outer row."""
    tree = _to_ast(sql)
    outer_alias = {t.alias_or_name for t in tree.args["from_"].find_all(exp.Table)}
    for proj in tree.expressions:
        sub = proj.this if isinstance(proj, exp.Alias) else proj
        if not isinstance(sub, exp.Subquery):
            continue
        inner = sub.this
        if not (isinstance(inner, exp.Select) and len(inner.expressions) == 1 and isinstance(inner.expressions[0], exp.Count)):
            continue
        where = inner.args.get("where")
        if not where:
            continue
        conj = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
        corr, rest = None, []
        for c in conj:
            if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column):
                a, b = c.this, c.expression
                if b.table in outer_alias and a.table not in outer_alias:
                    corr = (a, b)
                    continue
                if a.table in outer_alias and b.table not in outer_alias:
                    corr = (b, a)
                    continue
            rest.append(c)
        if not corr:
            continue
        inner_col, outer_col = corr
        name = proj.alias if isinstance(proj, exp.Alias) else "cnt"
        derived = exp.select(exp.alias_(inner_col.copy(), "k"), exp.alias_(exp.Count(this=exp.Star()), "cnt")).from_(inner.args["from_"].this.copy())
        if rest:
            derived = derived.where(exp.and_(*[r.copy() for r in rest]))
        derived = derived.group_by(inner_col.copy())
        oc = outer_col.copy()
        proj.replace(exp.alias_(exp.func("COALESCE", exp.column("cnt", "d"), exp.Literal.number(0)), name))
        tree = tree.join(exp.alias_(exp.Subquery(this=derived), "d"), on=exp.EQ(this=exp.column("k", "d"), expression=oc), join_type="left", copy=False)
        return _from_ast(tree), "R1 decorrelate scalar COUNT(*) subquery → LEFT JOIN + GROUP BY"
    return None, None


RULES = [rule_decorrelate_count]


class QueryAgent:
    name = "query"

    @staticmethod
    def _time(scon, sql, params_list, hint):
        ts, rows = [], []
        for p in params_list:
            with scon.transaction():
                for k, v in hint.items():
                    scon.execute(f"SET LOCAL {k} = {'on' if v else 'off'}")
                t = time.perf_counter()
                try:
                    rows.append(scon.execute(sql, p).fetchall())
                    ts.append((time.perf_counter() - t) * 1000)
                except psycopg.errors.QueryCanceled:
                    ts.append(30_000.0)
                    rows.append(None)
        return statistics.median(ts), rows

    def examine(self, tpl, runs=3, seed=21):
        _, _, sql, pf = TEMPLATES[tpl]
        r = random.Random(seed)
        params = [pf(r) for _ in range(runs)]
        out = {"template": tpl, "hints": [], "rewrites": [], "skipped_hints": False}
        with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
            s.execute("SET statement_timeout = '45s'")
            base, base_rows = self._time(s, sql, params, {})
            out["base_ms"] = base
            for rule in RULES:
                new_sql, why = rule(sql)
                if not new_sql:
                    continue
                ms, rows = self._time(s, new_sql, params, {})
                same = all(a == b for a, b in zip(base_rows, rows))
                out["rewrites"].append({"rule": why, "sql": new_sql, "ms": ms, "equivalent": same})
            if any(rw["equivalent"] and rw["ms"] < 0.1 * base for rw in out["rewrites"]) and base > 5000:
                out["skipped_hints"] = True       # a rewrite already removes >= 90% of a very slow query
                return out
            for h in HINT_SETS[1:]:
                ms, _ = self._time(s, sql, params, h)
                out["hints"].append((h, ms))
        return out

    def propose(self, slow_templates):
        props, reports = [], []
        for tpl in slow_templates:
            if TEMPLATES[tpl][2] is None:
                continue
            rep = self.examine(tpl)
            reports.append(rep)
            base = rep["base_ms"]
            console.log(f"[bold yellow]query-agent[/] {tpl}: default plan {base:,.1f} ms")
            for h, ms in rep["hints"]:
                console.log(f"[yellow]query-agent[/]   hint {json.dumps(h):<48} {ms:>10,.1f} ms")
            for rw in rep["rewrites"]:
                console.log(f"[yellow]query-agent[/]   rewrite {rw['rule']}: {rw['ms']:,.1f} ms · results identical: {rw['equivalent']}")
            if rep.get("skipped_hints"):
                console.log(f"[yellow]query-agent[/]   hint search skipped: the rewrite already removes over 90% of the time")
            best = None
            for h, ms in rep["hints"]:
                if best is None or ms < best[1]:
                    best = ("hint", ms, h)
            for rw in rep["rewrites"]:
                if rw["equivalent"] and (best is None or rw["ms"] < best[1]):
                    best = ("rewrite", rw["ms"], rw)
            if not best or best[1] > (1 - C.MIN_GAIN) * base:
                console.log(f"[yellow]query-agent[/]   nothing beats the default plan by {int(100*C.MIN_GAIN)}% — no proposal")
                continue
            gain = 1 - best[1] / base
            if best[0] == "hint":
                h = best[2]
                p = Proposal(agent=self.name, category="performance", title=f"plan hint for {tpl}: {h}",
                             action_sql=f"INSERT INTO mada.hints VALUES ('{tpl}', '{json.dumps(h)}') ON CONFLICT (template) DO UPDATE SET settings = EXCLUDED.settings",
                             rollback_sql=f"DELETE FROM mada.hints WHERE template = '{tpl}'",
                             resources=[f"plan:{tpl}"], risk="low", templates=[tpl], predicted_gain=round(gain, 3),
                             evidence={"default_ms": round(base, 1), "hinted_ms": round(best[1], 1)})
            else:
                rw = best[2]
                q = rw["sql"].replace("'", "''")
                p = Proposal(agent=self.name, category="performance", title=f"rewrite {tpl} ({rw['rule'].split(' ')[0]})",
                             action_sql=f"INSERT INTO mada.rewrites VALUES ('{tpl}', '{q}') ON CONFLICT (template) DO UPDATE SET sql = EXCLUDED.sql",
                             rollback_sql=f"DELETE FROM mada.rewrites WHERE template = '{tpl}'",
                             resources=[f"plan:{tpl}"], risk="medium", templates=[tpl], predicted_gain=round(gain, 3),
                             evidence={"rule": rw["rule"], "default_ms": round(base, 1), "rewritten_ms": round(rw["ms"], 1),
                                       "equivalent_on_samples": rw["equivalent"], "new_sql": rw["sql"]})
            p.sandbox_check = (lambda g=gain, b=base, a=best[1]: {"measured_gain": round(g, 3), "before_ms": round(b, 1), "after_ms": round(a, 1)})
            props.append(p)
            event("proposal", self.name, p.title, gain=gain)
        return props, reports
