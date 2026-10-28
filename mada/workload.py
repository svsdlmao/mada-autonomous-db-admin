"""Workload driver: plays the role of the store's application, reporting team and support desk.

Each worker thread connects as a real PostgreSQL role, picks query templates by weight,
records client-side latency, and writes an audit-log line per statement (used by the
security agent).  Approved plan hints and query rewrites are read from mada.hints /
mada.rewrites, standing in for pg_hint_plan's hint table in production.
"""
import json
import random
import threading
import time
import collections
import datetime as dt
import psycopg
from . import config as C
from .schema import WORDS, CITIES, CATEGORIES

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)

TEMPLATES = {
    # name: (role, class, sql, param function)
    "T1_customer_orders": ("app_user", "oltp",
        "SELECT order_id, order_date, total_amount FROM orders WHERE customer_id = %s ORDER BY order_date DESC LIMIT 20",
        lambda r: (r.randint(1, C.SCALE["customers"]),)),
    "T2_order_detail": ("app_user", "oltp",
        "SELECT oi.product_id, p.name, oi.quantity, oi.unit_price FROM order_items oi JOIN products p ON p.product_id = oi.product_id WHERE oi.order_id = %s",
        lambda r: (r.randint(1, C.SCALE["orders"]),)),
    "T3_product_reviews": ("app_user", "oltp",
        "SELECT rating, review_date, body FROM reviews WHERE product_id = %s ORDER BY review_date DESC LIMIT 10",
        lambda r: (r.randint(1, C.SCALE["products"]),)),
    "T4_place_order": ("app_user", "oltp", None, None),  # multi-statement transaction, see _place_order
    "T5_product_search": ("app_user", "oltp", None, None),  # string-built query (the injectable endpoint)
    "T6_product_lookup": ("app_user", "oltp",
        "SELECT product_id, name, category, price, stock FROM products WHERE product_id = %s",
        lambda r: (r.randint(1, C.SCALE["products"]),)),
    "A1_revenue_by_category": ("analyst", "olap",
        "SELECT p.category, date_trunc('month', o.order_date) AS month, sum(oi.quantity * oi.unit_price) AS revenue "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id "
        "WHERE o.order_date >= %s GROUP BY 1, 2 ORDER BY 1, 2",
        lambda r: (T0 - dt.timedelta(days=r.randint(150, 365)),)),
    "A2_top_customers_city": ("analyst", "olap",
        "SELECT c.customer_id, c.name, sum(o.total_amount) AS spent FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
        "WHERE c.city = %s GROUP BY c.customer_id, c.name ORDER BY spent DESC LIMIT 10",
        lambda r: (r.choice(CITIES),)),
    "A3_complaint_products": ("analyst", "olap",
        "SELECT p.product_id, p.name, (SELECT count(*) FROM reviews r WHERE r.product_id = p.product_id AND r.rating <= 2) AS bad_reviews "
        "FROM products p WHERE p.category = %s ORDER BY bad_reviews DESC, p.product_id LIMIT 20",
        lambda r: (r.choice(CATEGORIES),)),
    "S1_support_lookup": ("support", "oltp",
        "SELECT c.name, c.email, o.order_id, o.status FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
        "WHERE c.customer_id = %s ORDER BY o.order_date DESC LIMIT 5",
        lambda r: (r.randint(1, C.SCALE["customers"]),)),
}

# Workers per role and template weights inside each role
MIX = {
    "app_user": {"T1_customer_orders": 30, "T2_order_detail": 25, "T3_product_reviews": 15, "T4_place_order": 15,
                 "T5_product_search": 10, "T6_product_lookup": 5},
    "analyst": {"A1_revenue_by_category": 40, "A2_top_customers_city": 40, "A3_complaint_products": 20},
    "support": {"S1_support_lookup": 100},
}
WORKERS = {"app_user": 5, "analyst": 2, "support": 1}
THINK = {"app_user": 0.02, "analyst": 0.5, "support": 0.3}
TIMEOUT_MS = {"app_user": 5000, "analyst": 30000, "support": 5000}


class Recorder:
    """Thread-safe store of latency samples and audit-log lines."""

    def __init__(self):
        self.lock = threading.Lock()
        self.samples = collections.deque(maxlen=200_000)   # (t, template, ms, ok)
        self.audit = collections.deque(maxlen=50_000)      # (t, role, template, sql)

    def add(self, t, tpl, ms, ok):
        with self.lock:
            self.samples.append((t, tpl, ms, ok))

    def log(self, t, role, tpl, sql):
        with self.lock:
            self.audit.append((t, role, tpl, sql))

    def window(self, seconds, now=None):
        now = now or time.time()
        with self.lock:
            return [s for s in self.samples if s[0] >= now - seconds]

    def audit_since(self, t):
        with self.lock:
            return [a for a in self.audit if a[0] >= t]


class Workload:
    def __init__(self, recorder, seed=7, db=C.PROD_DB):
        self.rec = recorder
        self.db = db
        self.stop_ev = threading.Event()
        self.threads = []
        self.rnd = random.Random(seed)
        self.hints, self.rewrites = {}, {}
        self.extra = []   # injected behaviours (faults) appended at run time: callables(worker_rng, conn) -> None

    # ---- registry of approved hints / rewrites -------------------------------------------
    def _refresh_registry(self):
        try:
            with psycopg.connect(C.dsn(self.db), autocommit=True) as con:
                self.hints = {t: s for t, s in con.execute("SELECT template, settings FROM mada.hints")}
                self.rewrites = {t: s for t, s in con.execute("SELECT template, sql FROM mada.rewrites")}
        except Exception:
            pass

    def _registry_loop(self):
        while not self.stop_ev.wait(2.0):
            self._refresh_registry()

    # ---- statement helpers --------------------------------------------------------------------
    def _run(self, con, role, tpl, sql, params):
        hint = self.hints.get(tpl)
        sql = self.rewrites.get(tpl, sql)
        t = time.time()
        ok = True
        try:
            with con.transaction():
                if hint:
                    for k, v in hint.items():
                        con.execute(f"SET LOCAL {k} = {'on' if v else 'off'}" if isinstance(v, bool) else f"SET LOCAL {k} = '{v}'")
                con.execute(sql, params).fetchall()
        except psycopg.errors.QueryCanceled:
            ok = False
        except psycopg.Error:
            ok = False
        ms = (time.time() - t) * 1000
        self.rec.add(t, tpl, ms, ok)
        self.rec.log(t, role, tpl, sql if params is None else sql % tuple(repr(str(p)) for p in params))
        if con.closed or con.broken:          # session was ended (e.g. by the security agent): reconnect
            raise psycopg.OperationalError("connection lost")

    def _place_order(self, con, r):
        t = time.time()
        ok = True
        try:
            with con.transaction():
                cust = r.randint(1, C.SCALE["customers"])
                oid = con.execute("INSERT INTO orders VALUES (nextval('order_seq'), %s, now(), 'placed', 0) RETURNING order_id", (cust,)).fetchone()[0]
                total = 0
                for _ in range(2):
                    pid = r.randint(1, 50)   # popular products: a hot set of rows
                    q = r.randint(1, 2)
                    price = con.execute("UPDATE products SET stock = stock - %s WHERE product_id = %s RETURNING price", (q, pid)).fetchone()[0]
                    con.execute("INSERT INTO order_items VALUES (nextval('item_seq'), %s, %s, %s, %s)", (oid, pid, q, price))
                    total += q * float(price)
                con.execute("UPDATE orders SET total_amount = %s WHERE order_id = %s", (total, oid))
                con.execute("INSERT INTO payments VALUES (%s, %s, 'upi', NULL, %s, now())", (oid, oid, total))
        except psycopg.Error:
            ok = False
        self.rec.add(t, "T4_place_order", (time.time() - t) * 1000, ok)
        self.rec.log(t, "app_user", "T4_place_order", "INSERT INTO orders ...; UPDATE products ...; INSERT INTO order_items ...; INSERT INTO payments ...")
        if con.closed or con.broken:
            raise psycopg.OperationalError("connection lost")

    def search_sql(self, term):
        # deliberately string-built, like a careless web endpoint
        return f"SELECT product_id, name, price FROM products WHERE name ILIKE '%{term}%' LIMIT 20"

    def _search(self, con, r, term=None):
        term = term or r.choice(WORDS)
        sql = self.search_sql(term)
        t = time.time()
        ok = True
        try:
            con.execute(sql).fetchall()
        except psycopg.Error:
            ok = False
            if not con.closed:
                con.rollback()
        self.rec.add(t, "T5_product_search", (time.time() - t) * 1000, ok)
        self.rec.log(t, "app_user", "T5_product_search", sql)
        if con.closed or con.broken:
            raise psycopg.OperationalError("connection lost")

    # ---- workers ------------------------------------------------------------------------------
    def _worker(self, role, idx):
        r = random.Random(hash((role, idx, self.rnd.random())))
        names = list(MIX[role])
        weights = [MIX[role][n] for n in names]
        while not self.stop_ev.is_set():
            try:
                with psycopg.connect(C.dsn(self.db, role), autocommit=True) as con:
                    con.execute(f"SET statement_timeout = {TIMEOUT_MS[role]}")
                    while not self.stop_ev.is_set():
                        tpl = r.choices(names, weights)[0]
                        if tpl == "T4_place_order":
                            self._place_order(con, r)
                        elif tpl == "T5_product_search":
                            self._search(con, r)
                        else:
                            _, _, sql, pf = TEMPLATES[tpl]
                            self._run(con, role, tpl, sql, pf(r))
                        time.sleep(THINK[role] * r.uniform(0.5, 1.5))
            except psycopg.Error:
                time.sleep(0.5)

    def start(self):
        self._refresh_registry()
        self.stop_ev.clear()
        th = threading.Thread(target=self._registry_loop, daemon=True)
        th.start()
        self.threads.append(th)
        for role, n in WORKERS.items():
            for i in range(n):
                th = threading.Thread(target=self._worker, args=(role, i), daemon=True)
                th.start()
                self.threads.append(th)

    def stop(self):
        self.stop_ev.set()
        for th in self.threads:
            th.join(timeout=35)
        self.threads = []


def summarize(samples):
    """Per-template latency stats from recorder samples."""
    by = collections.defaultdict(list)
    for t, tpl, ms, ok in samples:
        by[tpl].append(ms)
    out = {}
    for tpl, v in by.items():
        v.sort()
        out[tpl] = dict(n=len(v), mean=sum(v) / len(v), p50=v[len(v) // 2], p95=v[min(len(v) - 1, int(0.95 * len(v)))])
    return out
