"""Observability layer: samples PostgreSQL and OS metrics every tick into an in-memory series."""
import time
import threading
import psycopg
from . import config as C
from .workload import summarize

TABLES = ["customers", "products", "orders", "order_items", "reviews", "payments"]


def _cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:8]]
    idle = v[3] + v[4]
    return sum(v), idle


class Collector:
    """Every TICK_SECONDS: client-side latency (from the workload recorder) + database statistics."""

    def __init__(self, recorder):
        self.rec = recorder
        self.series = []            # list of dict, one per tick
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.prev = None
        self.hist = []              # recent (t, snapshot) pairs: counter rates are taken over a 10 s window
        self.prev_cpu = _cpu()
        self.th = None

    def _db_snapshot(self, con):
        d = {}
        r = con.execute("SELECT xact_commit, temp_files, temp_bytes, blks_read, blks_hit, deadlocks FROM pg_stat_database WHERE datname = %s",
                        (C.PROD_DB,)).fetchone()
        d.update(xact_commit=r[0], temp_files=r[1], temp_bytes=r[2], blks_read=r[3], blks_hit=r[4], deadlocks=r[5])
        r = con.execute("""SELECT count(*) FILTER (WHERE state = 'active'),
                                  count(*) FILTER (WHERE state = 'idle'),
                                  count(*) FILTER (WHERE wait_event_type = 'Lock'),
                                  count(*)
                           FROM pg_stat_activity WHERE datname = %s AND backend_type = 'client backend'""", (C.PROD_DB,)).fetchone()
        d.update(active=r[0], idle=r[1], lock_waiting=r[2], connections=r[3])
        d["locks_not_granted"] = con.execute("SELECT count(*) FROM pg_locks WHERE NOT granted").fetchone()[0]
        for rel, seq, seqtup, idx, dead, live in con.execute(
                "SELECT relname, seq_scan, seq_tup_read, coalesce(idx_scan,0), n_dead_tup, n_live_tup FROM pg_stat_user_tables WHERE schemaname='public'"):
            d[f"seq_scan:{rel}"] = seq
            d[f"seq_tup:{rel}"] = seqtup
            d[f"idx_scan:{rel}"] = idx
            d[f"dead:{rel}"] = dead
            d[f"live:{rel}"] = live
        return d

    COUNTERS = ("xact_commit", "temp_files", "temp_bytes", "blks_read", "blks_hit", "deadlocks")

    def tick(self, con):
        now = time.time()
        snap = self._db_snapshot(con)
        row = {"t": now}
        if self.prev:
            # rates over the same 10 s window as the latency percentiles, so bursty counters
            # (temp files written by one analytical query) do not flicker from tick to tick
            t_ref, ref = next(((t, sn) for t, sn in self.hist if t >= now - 10.5), (self.prev["t"], self.prev["snap"]))
            dtt = max(now - t_ref, 1e-3)
            for k, v in snap.items():
                if k in self.COUNTERS or k.startswith(("seq_scan:", "seq_tup:", "idx_scan:")):
                    row[k + "/s"] = max(0, v - ref.get(k, v)) / dtt
                else:
                    row[k] = v
        tot, idle = _cpu()
        pt, pi = self.prev_cpu
        row["cpu_pct"] = 100.0 * (1 - (idle - pi) / max(1, tot - pt))
        self.prev_cpu = (tot, idle)
        win = self.rec.window(10, now)
        st = summarize(win)
        row["tps"] = len([s for s in win if s[0] >= now - 10]) / 10.0
        oltp = sorted(s[2] for s in win if s[1][0] in "TS")
        olap = sorted(s[2] for s in win if s[1][0] == "A")
        row["p95_oltp_ms"] = oltp[int(0.95 * (len(oltp) - 1))] if oltp else 0.0
        row["p95_olap_ms"] = olap[int(0.95 * (len(olap) - 1))] if olap else 0.0
        row["errors/s"] = len([s for s in win if not s[3]]) / 10.0
        for tpl, v in st.items():
            row[f"p95:{tpl}"] = v["p95"]
        self.prev = {"t": now, "snap": snap}
        self.hist = [(t, sn) for t, sn in self.hist if t >= now - 12] + [(now, snap)]
        if len(row) > 3:
            with self.lock:
                self.series.append(row)
        return row

    def _loop(self):
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            while not self.stop_ev.is_set():
                try:
                    self.tick(con)
                except psycopg.Error:
                    pass
                self.stop_ev.wait(C.TICK_SECONDS)

    def start(self):
        self.stop_ev.clear()
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def stop(self):
        self.stop_ev.set()
        if self.th:
            self.th.join(5)

    def last(self, n=1):
        with self.lock:
            return list(self.series[-n:])

    def since(self, t):
        with self.lock:
            return [r for r in self.series if r["t"] >= t]
