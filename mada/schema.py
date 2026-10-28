"""Creates the e-commerce 'store' database, its roles and data, and the sandbox clone."""
import io
import random
import datetime as dt
import psycopg
from . import config as C

CATEGORIES = ["electronics", "books", "fashion", "home", "sports", "toys", "beauty", "grocery"]
CITIES = ["Chennai", "Vellore", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Kolkata",
          "Jaipur", "Kochi", "Ahmedabad", "Lucknow", "Indore", "Bhopal", "Nagpur", "Coimbatore"]
SEGMENTS = ["regular", "regular", "regular", "premium", "new"]
WORDS = ["smart", "classic", "eco", "pro", "mini", "ultra", "lite", "max", "prime", "neo",
         "phone", "lamp", "shoe", "book", "watch", "bag", "bottle", "chair", "kettle", "cable"]

DDL = """
CREATE TABLE customers (customer_id int PRIMARY KEY, name text NOT NULL, email text NOT NULL,
  city text NOT NULL, segment text NOT NULL, created_at timestamptz NOT NULL);
CREATE TABLE products (product_id int PRIMARY KEY, name text NOT NULL, category text NOT NULL,
  price numeric(10,2) NOT NULL, stock int NOT NULL);
CREATE TABLE orders (order_id bigint PRIMARY KEY, customer_id int NOT NULL, order_date timestamptz NOT NULL,
  status text NOT NULL, total_amount numeric(12,2) NOT NULL);
CREATE TABLE order_items (item_id bigint PRIMARY KEY, order_id bigint NOT NULL, product_id int NOT NULL,
  quantity int NOT NULL, unit_price numeric(10,2) NOT NULL);
CREATE TABLE reviews (review_id bigint PRIMARY KEY, product_id int NOT NULL, customer_id int NOT NULL,
  rating int NOT NULL, review_date timestamptz NOT NULL, body text NOT NULL);
CREATE TABLE payments (payment_id bigint PRIMARY KEY, order_id bigint NOT NULL, method text NOT NULL,
  card_last4 text, amount numeric(12,2) NOT NULL, paid_at timestamptz NOT NULL);
CREATE SEQUENCE order_seq; CREATE SEQUENCE item_seq;
CREATE SCHEMA mada;
CREATE TABLE mada.hints (template text PRIMARY KEY, settings jsonb NOT NULL, approved_at timestamptz DEFAULT now());
CREATE TABLE mada.rewrites (template text PRIMARY KEY, sql text NOT NULL, approved_at timestamptz DEFAULT now());
"""

GRANTS = """
GRANT USAGE ON SCHEMA public, mada TO app_user, analyst, support;
GRANT SELECT ON ALL TABLES IN SCHEMA mada TO app_user, analyst, support;
GRANT SELECT, INSERT, UPDATE ON customers, products, orders, order_items, reviews, payments TO app_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst;
GRANT SELECT ON customers, orders, order_items, products, payments TO support;
"""


def _copy(cur, table, rows):
    buf = io.StringIO()
    for r in rows:
        buf.write("\t".join(str(v) for v in r) + "\n")
    buf.seek(0)
    with cur.copy(f"COPY {table} FROM STDIN") as cp:
        cp.write(buf.read())


def create(verbose=True):
    rnd = random.Random(42)
    with psycopg.connect(C.dsn("postgres"), autocommit=True) as con:
        for db in (C.SANDBOX_DB, C.PROD_DB):
            con.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        for role in C.ROLES:
            con.execute(f"DROP ROLE IF EXISTS {role}")
            con.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{C.ROLE_PASSWORD}'")
        con.execute(f"CREATE DATABASE {C.PROD_DB}")
    with psycopg.connect(C.dsn(), autocommit=True) as con:
        cur = con.cursor()
        cur.execute("CREATE EXTENSION pg_stat_statements; CREATE EXTENSION hypopg;")
        cur.execute(DDL)
        S = C.SCALE
        now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        _copy(cur, "customers", ((i, f"Customer {i}", f"user{i}@mail.example", rnd.choice(CITIES), rnd.choice(SEGMENTS),
                                  (now - dt.timedelta(days=rnd.randint(0, 900))).isoformat()) for i in range(1, S["customers"] + 1)))
        prices = {}
        prows = []
        for i in range(1, S["products"] + 1):
            p = round(rnd.uniform(99, 49_999), 2)
            prices[i] = p
            prows.append((i, f"{rnd.choice(WORDS)} {rnd.choice(WORDS)} {i}", rnd.choice(CATEGORIES), p, rnd.randint(0, 500)))
        _copy(cur, "products", prows)
        orows, irows, payrows = [], [], []
        item_id = 0
        for o in range(1, S["orders"] + 1):
            cust = int(rnd.paretovariate(1.2)) % S["customers"] + 1  # some customers order a lot
            when = now - dt.timedelta(minutes=rnd.randint(0, 720 * 24 * 60))
            total = 0.0
            for _ in range(rnd.choice([1, 2, 3, 3, 4, 5])):
                item_id += 1
                pid = rnd.randint(1, S["products"])
                q = rnd.randint(1, 3)
                total += q * prices[pid]
                irows.append((item_id, o, pid, q, prices[pid]))
            orows.append((o, cust, when.isoformat(), rnd.choice(["delivered", "delivered", "shipped", "placed", "cancelled"]), round(total, 2)))
            payrows.append((o, o, rnd.choice(["upi", "card", "netbanking", "cod"]), f"{rnd.randint(0, 9999):04d}", round(total, 2), when.isoformat()))
        _copy(cur, "orders", orows)
        _copy(cur, "order_items", irows)
        _copy(cur, "payments", payrows)
        _copy(cur, "reviews", ((r, rnd.randint(1, S["products"]), rnd.randint(1, S["customers"]), rnd.randint(1, 5),
                                (now - dt.timedelta(days=rnd.randint(0, 700))).isoformat(), "good product " * rnd.randint(1, 6))
                               for r in range(1, S["reviews"] + 1)))
        cur.execute(f"SELECT setval('order_seq', {S['orders']}); SELECT setval('item_seq', {item_id});")
        cur.execute(GRANTS)
        cur.execute("VACUUM ANALYZE")
        counts = {t: cur.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ["customers", "products", "orders", "order_items", "reviews", "payments"]}
        size = cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    if verbose:
        print("loaded", counts, "size", size)
    return counts, size


def clone_sandbox():
    """Refresh the sandbox as an exact copy of production (production must be idle)."""
    with psycopg.connect(C.dsn("postgres"), autocommit=True) as con:
        con.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{C.PROD_DB}' AND pid <> pg_backend_pid()")
        con.execute(f"DROP DATABASE IF EXISTS {C.SANDBOX_DB} WITH (FORCE)")
        con.execute(f"CREATE DATABASE {C.SANDBOX_DB} TEMPLATE {C.PROD_DB}")


if __name__ == "__main__":
    create()
    clone_sandbox()
