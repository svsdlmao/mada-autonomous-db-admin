"""Monitor & forecast agent: learns a baseline, flags anomalies, forecasts per-template load."""
import math
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression
from .. import config as C
from ..core import console, event

# metrics watched, and the direction that counts as "bad"
WATCH = {
    "p95_oltp_ms": "up", "p95_olap_ms": "up", "tps": "down", "errors/s": "up",
    "lock_waiting": "up", "locks_not_granted": "up", "connections": "up", "idle": "up",
    "temp_bytes/s": "up", "cpu_pct": "up",
    "dead:orders": "up", "dead:order_items": "up",
    "seq_tup:orders/s": "up", "seq_tup:order_items/s": "up", "seq_tup:reviews/s": "up",
}


class MonitorAgent:
    name = "monitor"

    def __init__(self):
        self.base = {}
        self.forest = None
        self.keys = []
        self.streak = 0

    def fit_baseline(self, rows):
        rows = [r for r in rows if "connections" in r]
        self.keys = [k for k in WATCH if all(k in r for r in rows)]
        for k in self.keys:
            v = np.array([r[k] for r in rows], dtype=float)
            mean, std = float(v.mean()), float(v.std())
            floor = max(0.15 * abs(mean), 1.0 if k not in ("tps",) else 0.5)
            self.base[k] = (mean, max(std, floor))
        X = np.array([[r[k] for k in self.keys] for r in rows], dtype=float)
        self.forest = IsolationForest(n_estimators=100, contamination=0.02, random_state=0).fit(X)
        event("baseline", self.name, f"baseline learned from {len(rows)} ticks over {len(self.keys)} metrics")
        return self.base

    def zscores(self, row):
        out = {}
        for k in self.keys:
            if k not in row:
                continue
            mean, std = self.base[k]
            z = (row[k] - mean) / std
            if WATCH[k] == "down":
                z = -z
            out[k] = z
        return out

    def check(self, row):
        """Returns an alert dict when the system has looked abnormal for ALERT_CONSECUTIVE ticks."""
        if "connections" not in row or not self.base:
            return None
        z = self.zscores(row)
        bad = {k: v for k, v in z.items() if v > C.ALERT_Z}
        iso = float(self.forest.decision_function([[row.get(k, 0.0) for k in self.keys]])[0])
        # moderate deviations need the forest to agree; an extreme one (> 3 x ALERT_Z) counts on its own,
        # because a forest cannot split on a metric that never varied while it was trained
        if bad and (iso < 0 or max(bad.values()) > 3 * C.ALERT_Z):
            self.streak += 1
        else:
            self.streak = 0
        if self.streak >= C.ALERT_CONSECUTIVE:
            self.streak = -10**9  # one alert per incident; reset() re-arms
            top = sorted(bad.items(), key=lambda kv: -kv[1])[:4]
            msg = ", ".join(f"{k} z={v:.1f}" for k, v in top)
            event("alert", self.name, f"anomaly: {msg}", metrics=dict(top), iso=iso)
            return {"t": row["t"], "metrics": dict(top), "iso": iso}
        return None

    def reset(self):
        self.streak = 0

    @staticmethod
    def forecast(recorder, horizon_s=60, history_s=120, bucket=10):
        """Per-template arrival rate: fit a line over the last history_s seconds, predict horizon_s ahead."""
        samples = recorder.window(history_s)
        if not samples:
            return {}
        t_end = max(s[0] for s in samples)
        out = {}
        for tpl in sorted({s[1] for s in samples}):
            counts = np.zeros(history_s // bucket)
            for s in samples:
                if s[1] == tpl:
                    i = int((t_end - s[0]) // bucket)
                    if i < len(counts):
                        counts[len(counts) - 1 - i] += 1
            X = np.arange(len(counts)).reshape(-1, 1)
            m = LinearRegression().fit(X, counts / bucket)
            nxt = float(m.predict([[len(counts) + horizon_s / bucket]])[0])
            out[tpl] = dict(now=float(counts[-1] / bucket), forecast=max(0.0, nxt))
        return out
