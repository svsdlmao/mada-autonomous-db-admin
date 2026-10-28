"""Coordinator and safety gate.

Coordinator: collects proposals, drops invalid ones, builds a conflict graph over shared resources,
orders by priority (security > availability > correctness > performance) then gain/risk, defers
conflicting proposals, and sends the rest through the safety gate one at a time.

Safety gate: static policy checks -> sandbox evidence -> gain check -> risk level -> apply -> watch -> rollback.
"""
import re
import time
import psycopg
from . import config as C
from .core import console, event
from .agents.knob_agent import CATALOG

ALLOWED = [
    r"^CREATE INDEX( CONCURRENTLY)? \w+ ON \w+ \([\w, ]+\)$",
    r"^DROP INDEX( CONCURRENTLY)?( IF EXISTS)? \w+$",
    r"^ALTER SYSTEM (SET (\w+) = '[\w.]+'|RESET (\w+))$",
    r"^SELECT pg_reload_conf\(\)$",
    r"^SELECT pg_terminate_backend\(.*\)( FROM .*)?$",
    r"^INSERT INTO mada\.(hints|rewrites) VALUES .*$",
    r"^DELETE FROM mada\.(hints|rewrites) WHERE template = '\w+'$",
    r"^VACUUM \(ANALYZE\) \w+$",
    r"^ALTER TABLE \w+ RESET \(autovacuum_enabled\)$",
    r"^(REVOKE|GRANT) SELECT ON \w+ (FROM|TO) \w+$",
]


def split_sql(s):
    out, cur, q = [], [], False
    for ch in s:
        if ch == "'":
            q = not q
        if ch == ";" and not q:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [x for x in out if x and not x.startswith("--")]


def static_check(p):
    for stmt in split_sql(p.action_sql):
        if not any(re.match(a, stmt, re.S) for a in ALLOWED):
            return False, f"statement not on the allow-list: {stmt[:70]}"
        m = re.match(r"^ALTER SYSTEM (?:SET (\w+)|RESET (\w+))", stmt)
        if m and (m.group(1) or m.group(2)) not in CATALOG:
            return False, f"knob {(m.group(1) or m.group(2))} is not in the knob catalog"
        m = re.match(r"^ALTER SYSTEM SET (\w+) = '([\d.]+)(MB)?'", stmt)
        if m and m.group(1) in CATALOG and CATALOG[m.group(1)]["type"] != "bool":
            c, v = CATALOG[m.group(1)], float(m.group(2))
            if not (c["low"] <= v <= c["high"]):
                return False, f"{m.group(1)}={m.group(2)} is outside the documented safe range {c['low']}–{c['high']}"
    if p.reversible and not split_sql(p.rollback_sql):
        return False, "no rollback statement"
    return True, "ok"


def p95(samples, templates, t0, t1):
    v = sorted(ms for t, tpl, ms, ok in samples if t0 <= t < t1 and (not templates or tpl in templates))
    return v[int(0.95 * (len(v) - 1))] if v else None


class SafetyGate:
    def __init__(self, approver=None, enabled=True):
        self.approver = approver or (lambda p: False)
        self.enabled = enabled

    def check(self, p):
        """Returns (decision, reason, evidence). decision in PASS / REJECT."""
        if not self.enabled:
            return "PASS", "gate disabled (ablation run)", {}
        ok, why = static_check(p)
        if not ok:
            return "REJECT", why, {}
        ev = p.sandbox_check() if p.sandbox_check else {}
        if p.category == "performance":
            g = ev.get("measured_gain")
            if g is None or g < C.MIN_GAIN:
                return "REJECT", f"measured gain on sandbox {0 if g is None else 100*g:.0f}% < {int(100*C.MIN_GAIN)}%", ev
            if ev.get("write_overhead", 0) > C.MAX_WRITE_OVERHEAD:
                return "REJECT", f"write overhead {100*ev['write_overhead']:.0f}% > {int(100*C.MAX_WRITE_OVERHEAD)}%", ev
            if p.evidence.get("equivalent_on_samples") is False:
                return "REJECT", "rewrite returns different results", ev
        if p.risk == "high":
            if not self.approver(p):
                return "REJECT", "high-risk action not approved by a human", ev
            return "PASS", "high risk — approved by operator", ev
        return "PASS", f"{p.risk} risk — automatic", ev


class Coordinator:
    def __init__(self, gate, recorder=None, sync_sandbox=True):
        self.gate = gate
        self.rec = recorder
        self.sync_sandbox = sync_sandbox
        self.log = []          # decision log: (proposal, decision, reason)
        self.deferred = []

    @staticmethod
    def _apply(sql, db=C.PROD_DB):
        with psycopg.connect(C.dsn(db), autocommit=True) as con:
            for stmt in split_sql(sql):
                con.execute(stmt)

    def _mirror(self, p):
        """Keep the sandbox in step with production for schema/registry changes."""
        if not self.sync_sandbox:
            return
        if p.action_sql.startswith(("CREATE INDEX", "INSERT INTO mada", "DROP INDEX")):
            try:
                self._apply(p.action_sql.replace(" CONCURRENTLY", ""), C.SANDBOX_DB)
            except psycopg.Error:
                pass

    def run_window(self, proposals, watch_s=15, approve_log=None):
        props = self.deferred + list(proposals)
        self.deferred = []
        if not props:
            return []
        console.rule(f"[bold]coordinator window · {len(props)} proposal(s)")
        props.sort(key=lambda p: (C.PRIORITY[p.category], -(p.predicted_gain or 0), {"low": 0, "medium": 1, "high": 2}[p.risk]))
        accepted, taken = [], {}
        for p in props:
            clash = [r for r in p.resources if r in taken and r not in ("disk",)]
            if clash:
                p.status = "deferred"
                self.deferred.append(p)
                why = f"shares {clash[0]} with #{taken[clash[0]]} in this window"
                console.log(f"[bold blue]coordinator[/] #{p.id:<3} [{p.category}] {p.title}  →  [yellow]DEFER[/] ({why})")
                self.log.append((p, "DEFER", why))
                event("decision", "coordinator", f"#{p.id} deferred: {why}", pid=p.id)
                continue
            for r in p.resources:
                taken[r] = p.id
            accepted.append(p)
        applied = []
        for p in accepted:
            decision, reason, ev = self.gate.check(p)
            p.evidence.setdefault("sandbox", ev)
            colour = "green" if decision == "PASS" else "red"
            console.log(f"[bold blue]coordinator[/] #{p.id:<3} [{p.category}] {p.title}  →  [{colour}]{decision}[/] ({reason})")
            self.log.append((p, decision, reason))
            event("decision", "coordinator", f"#{p.id} {decision}: {reason}", pid=p.id, title=p.title)
            if decision != "PASS":
                p.status = "rejected"
                continue
            t_apply = time.time()
            try:
                self._apply(p.action_sql)
            except psycopg.Error as e:
                console.log(f"[red]coordinator[/] #{p.id} failed to apply: {str(e).splitlines()[0]}")
                p.status = "failed"
                continue
            self._mirror(p)
            p.status = "applied"
            p.applied_at = t_apply
            t_done = time.time()
            p.done_at = t_done
            event("applied", "coordinator", f"#{p.id} applied: {p.title}", pid=p.id)
            if self.rec and watch_s and p.templates and p.category == "performance":
                time.sleep(watch_s)
                before = p95(list(self.rec.samples), p.templates, t_apply - 60, t_apply)
                after = p95(list(self.rec.samples), p.templates, t_done + 1, time.time())
                if before and after:
                    rise = after / before - 1
                    console.log(f"[blue]coordinator[/] #{p.id} watch: p95 of {', '.join(p.templates[:3])}{'…' if len(p.templates) > 3 else ''} {before:,.1f} → {after:,.1f} ms")
                    if rise > C.ROLLBACK_P95_RISE and after - before > 5:
                        self._apply(p.rollback_sql)
                        p.status = "rolled back"
                        console.log(f"[bold red]coordinator[/] #{p.id} ROLLBACK: p95 rose {100*rise:.0f}% after apply")
                        event("rollback", "coordinator", f"#{p.id} rolled back (p95 +{100*rise:.0f}%)", pid=p.id)
                        continue
            applied.append(p)
        return applied
