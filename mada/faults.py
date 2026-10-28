"""Fault injectors for the evaluation (F-numbers follow the case study's fault table)."""
import threading
import time
import psycopg
from . import config as C


class Fault:
    def __init__(self, key, name, expected, rec=None):
        self.key, self.name, self.expected = key, name, expected
        self.rec = rec
        self.stop = threading.Event()
        self.threads = []

    def inject(self): ...
    def cleanup(self): ...

    def _bg(self, fn):
        th = threading.Thread(target=fn, daemon=True)
        th.start()
        self.threads.append(th)

    def end(self):
        self.stop.set()
        for th in self.threads:
            th.join(10)
        self.cleanup()


def run(sql, db=C.PROD_DB, user=C.SUPERUSER):
    with psycopg.connect(C.dsn(db, user), autocommit=True) as con:
        for s in sql.split(";"):
            if s.strip():
                con.execute(s)


class MissingIndex(Fault):
    def __init__(self, index_name, create_sql, **kw):
        super().__init__("F1", "Missing index", "missing_index", **kw)
        self.index_name, self.create_sql = index_name, create_sql

    def inject(self):
        run(f"DROP INDEX IF EXISTS {self.index_name}")

    def cleanup(self):
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            if not con.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (self.index_name,)).fetchone():
                con.execute(self.create_sql)


class BadConfig(Fault):
    def __init__(self, good_value, **kw):
        super().__init__("F2", "Bad configuration", "work_mem_spill", **kw)
        self.good = good_value

    def inject(self):
        run("ALTER SYSTEM SET work_mem = '64kB'; SELECT pg_reload_conf()")

    def cleanup(self):
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            if int(con.execute("SELECT setting FROM pg_settings WHERE name='work_mem'").fetchone()[0]) < 1024:
                con.execute(f"ALTER SYSTEM SET work_mem = '{self.good}'")
                con.execute("SELECT pg_reload_conf()")


class LockContention(Fault):
    def __init__(self, **kw):
        super().__init__("F4", "Lock contention", "lock_contention", **kw)

    def inject(self):
        def hold():
            try:
                with psycopg.connect(C.dsn(C.PROD_DB, "app_user")) as con:
                    con.execute("SET application_name = 'batch-price-update'")
                    con.execute("SELECT product_id FROM products WHERE product_id BETWEEN 1 AND 50 FOR UPDATE")
                    while not self.stop.wait(0.5):
                        pass
                    con.rollback()
            except psycopg.Error:
                pass   # terminated by MADA
        self._bg(hold)


class TableBloat(Fault):
    def __init__(self, **kw):
        super().__init__("F5", "Table bloat", "table_bloat", **kw)

    def inject(self):
        run("ALTER TABLE orders SET (autovacuum_enabled = false)")
        def churn():
            with psycopg.connect(C.dsn(), autocommit=True) as con:
                for _ in range(2):
                    if self.stop.is_set():
                        break
                    con.execute("UPDATE orders SET status = status WHERE order_id % 2 = 0")
        self._bg(churn)

    def cleanup(self):
        run("ALTER TABLE orders RESET (autovacuum_enabled)")


class ConnectionStorm(Fault):
    def __init__(self, n=80, **kw):
        super().__init__("F8", "Connection storm", "connection_storm", **kw)
        self.n = n

    def inject(self):
        def storm():
            cons = []
            for _ in range(self.n):
                try:
                    cons.append(psycopg.connect(C.dsn(C.PROD_DB, "app_user"), autocommit=True))
                except psycopg.Error:
                    break
            while not self.stop.wait(0.5):
                pass
            for c in cons:
                try:
                    c.close()
                except Exception:
                    pass
        self._bg(storm)


class SqlInjection(Fault):
    PAYLOADS = ["x%' UNION SELECT customer_id, email, 0 FROM customers --",
                "lamp' OR '1'='1",
                "pro%' UNION SELECT payment_id, card_last4, amount FROM payments --",
                "x' AND pg_sleep(1) IS NULL --"]

    def __init__(self, search_sql, **kw):
        super().__init__("F9", "SQL injection", "sql_injection", **kw)
        self.search_sql = search_sql

    def inject(self):
        def attack():
            try:
                with psycopg.connect(C.dsn(C.PROD_DB, "app_user"), autocommit=True) as con:
                    con.execute("SET application_name = 'web-frontend'")
                    i = 0
                    while not self.stop.wait(0.7):
                        sql = self.search_sql(self.PAYLOADS[i % len(self.PAYLOADS)])
                        i += 1
                        self.rec.log(time.time(), "app_user", "T5_product_search", sql)
                        try:
                            con.execute(sql).fetchall()
                        except psycopg.errors.Error as e:
                            if isinstance(e, (psycopg.errors.AdminShutdown, psycopg.OperationalError)):
                                return
            except psycopg.Error:
                pass
        self._bg(attack)


class AbnormalAccess(Fault):
    def __init__(self, **kw):
        super().__init__("F10", "Abnormal access", "abnormal_access", **kw)

    def inject(self):
        def snoop():
            try:
                with psycopg.connect(C.dsn(C.PROD_DB, "support"), autocommit=True) as con:
                    while not self.stop.wait(1.5):
                        sql = "SELECT payment_id, card_last4, amount FROM payments WHERE paid_at > now() - interval '30 days'"
                        self.rec.log(time.time(), "support", "unknown", sql)
                        con.execute(sql).fetchall()
            except psycopg.Error:
                pass
        self._bg(snoop)

    def cleanup(self):
        run("GRANT SELECT ON payments TO support")
