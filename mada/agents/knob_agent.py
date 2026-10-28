"""Knob agent: tunes reload-only PostgreSQL settings with Bayesian optimisation (Optuna TPE) on the sandbox.

The knob catalog below plays the part of GPTuner's 'read the manual' step: each entry is the knob,
its documented meaning and a safe range.  In the prototype it is curated by hand from the PostgreSQL 16
documentation; with an LLM backend configured, the same structure would be extracted from the docs.
"""
import random
import statistics
import time
import optuna
import psycopg
from .. import config as C
from ..core import Proposal, console, event
from ..workload import TEMPLATES

optuna.logging.set_verbosity(optuna.logging.WARNING)

CATALOG = {
    "work_mem": {"doc": "memory per sort/hash before spilling to temporary files", "type": "mb", "low": 1, "high": 256, "default": 4},
    "hash_mem_multiplier": {"doc": "multiplier of work_mem allowed for hash tables", "type": "float", "low": 1.0, "high": 8.0, "default": 2.0},
    "random_page_cost": {"doc": "planner's cost of a non-sequential page read (lower on SSD/RAM)", "type": "float", "low": 1.1, "high": 4.0, "default": 4.0},
    "effective_cache_size": {"doc": "planner's estimate of memory available for caching", "type": "mb", "low": 1024, "high": 6144, "default": 4096},
    "max_parallel_workers_per_gather": {"doc": "parallel workers one query node may use", "type": "int", "low": 0, "high": 2, "default": 2},
    "jit": {"doc": "just-in-time compile expressions of expensive queries", "type": "bool", "default": True},
}

REPLAY = {"A1_revenue_by_category": 2, "A2_top_customers_city": 3, "A3_complaint_products": 2,
          "T1_customer_orders": 5, "T2_order_detail": 5, "T3_product_reviews": 5, "S1_support_lookup": 3}


def fmt(name, v):
    t = CATALOG[name]["type"]
    if t == "mb":
        return f"{int(v)}MB"
    if t == "bool":
        return "on" if v else "off"
    if t == "float":
        return f"{v:.2f}"
    return str(int(v))


class KnobAgent:
    name = "knob"

    def __init__(self, trials=14):
        self.trials = trials
        self.good = {}          # last configuration MADA actually applied (used when repairing a bad change)
        self.pending = {}       # configuration proposed but not yet applied

    def _replay(self, scon, settings, freq, seed=5):
        rewrites = dict(scon.execute("SELECT template, sql FROM mada.rewrites").fetchall())
        for k, v in settings.items():
            scon.execute(f"SET {k} = '{fmt(k, v)}'")
        r = random.Random(seed)
        total, per = 0.0, {}
        for tpl, n in REPLAY.items():
            _, _, sql, pf = TEMPLATES[tpl]
            sql = rewrites.get(tpl, sql)
            ts = []
            for _ in range(n):
                p = pf(r)
                t = time.perf_counter()
                try:
                    scon.execute(sql, p).fetchall()
                    ts.append((time.perf_counter() - t) * 1000)
                except psycopg.errors.QueryCanceled:
                    ts.append(30_000.0)
            per[tpl] = statistics.median(ts)
            total += freq.get(tpl, 0.0) * per[tpl]
        scon.execute("RESET ALL")
        return total, per

    def propose(self, freq):
        """freq: template -> calls per minute in production (from the recorder)."""
        defaults = {k: c["default"] for k, c in CATALOG.items()}
        history = []
        with psycopg.connect(C.dsn(C.SANDBOX_DB), autocommit=True) as s:
            s.execute("SET statement_timeout = '30s'")
            self._replay(s, defaults, freq)                     # warm the cache once
            base, base_per = self._replay(s, defaults, freq)

            def objective(trial):
                cfg = {}
                for k, c in CATALOG.items():
                    if c["type"] == "bool":
                        cfg[k] = trial.suggest_categorical(k, [True, False])
                    elif c["type"] == "float":
                        cfg[k] = trial.suggest_float(k, c["low"], c["high"])
                    elif c["type"] == "mb":
                        cfg[k] = trial.suggest_int(k, c["low"], c["high"], log=True)
                    else:
                        cfg[k] = trial.suggest_int(k, c["low"], c["high"])
                val, _ = self._replay(s, cfg, freq)
                history.append((trial.number, val, cfg))
                return val

            study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=3))
            study.enqueue_trial({k: c["default"] for k, c in CATALOG.items()})
            study.optimize(objective, n_trials=self.trials)
            best = study.best_params
            best_val, best_per = self._replay(s, best, freq)
        gain = 1 - best_val / base
        for n, v, cfg in history:
            console.log(f"[magenta]knob-agent[/] trial {n:>2}  weighted time {v/1000:8.2f} s   work_mem={fmt('work_mem', cfg['work_mem'])}, "
                        f"rpc={cfg['random_page_cost']:.2f}, parallel={cfg['max_parallel_workers_per_gather']}, jit={fmt('jit', cfg['jit'])}")
        changed = {k: v for k, v in best.items() if fmt(k, v) != fmt(k, defaults[k])}
        if not changed:
            console.log(f"[bold magenta]knob-agent[/] the best configuration is the default one; two replays of it took {base/1000:.2f} s and "
                        f"{best_val/1000:.2f} s (measurement noise) - no proposal")
            return None, history
        console.log(f"[bold magenta]knob-agent[/] best vs default: {base/1000:.2f} s → {best_val/1000:.2f} s  ({100*gain:.0f}% less weighted time)")
        sql = "; ".join(f"ALTER SYSTEM SET {k} = '{fmt(k, v)}'" for k, v in changed.items()) + "; SELECT pg_reload_conf()"
        rollback = "; ".join(f"ALTER SYSTEM RESET {k}" for k in changed) + "; SELECT pg_reload_conf()"
        p = Proposal(agent=self.name, category="performance", title="configuration: " + ", ".join(f"{k}={fmt(k, v)}" for k, v in changed.items()),
                     action_sql=sql, rollback_sql=rollback, resources=["memory", "planner"], risk="low",
                     evidence={"trials": len(history), "default_ms": round(base), "best_ms": round(best_val),
                               "per_template_default": {k: round(v, 1) for k, v in base_per.items()},
                               "per_template_best": {k: round(v, 1) for k, v in best_per.items()}},
                     predicted_gain=round(gain, 3), templates=[t for t in REPLAY])
        self.pending = {k: fmt(k, v) for k, v in changed.items()}
        p.sandbox_check = lambda: {"measured_gain": round(gain, 3), "before_ms": round(base), "after_ms": round(best_val)}
        event("proposal", self.name, p.title, gain=gain)
        return p, history

    def repair(self, param, current):
        """Restore a knob that drifted to a harmful value (route from the diagnosis agent)."""
        target = self.good.get(param, fmt(param, CATALOG[param]["default"]))
        p = Proposal(agent=self.name, category="availability", title=f"restore {param} from {current} to {target}",
                     action_sql=f"ALTER SYSTEM SET {param} = '{target}'; SELECT pg_reload_conf()",
                     rollback_sql=f"ALTER SYSTEM SET {param} = '{current}'; SELECT pg_reload_conf()",
                     resources=["memory"], risk="low", evidence={"current": current, "known_good": target},
                     templates=["A1_revenue_by_category", "A2_top_customers_city"])
        p.sandbox_check = lambda: {"measured_gain": None, "note": "restores last known-good value"}
        return p
