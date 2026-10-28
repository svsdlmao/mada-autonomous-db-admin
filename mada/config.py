"""Central configuration for the MADA prototype."""
import os

PG_HOST = os.environ.get("MADA_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("MADA_PG_PORT", "5432"))
SUPERUSER = os.environ.get("MADA_PG_USER", "postgres")
PG_PASSWORD = os.environ.get("MADA_PG_PASSWORD", "")      # superuser password (empty with trust auth)
ROLE_PASSWORD = "mada_demo"                                # password of the three workload roles

PROD_DB = "store"            # the "production" database the agents look after
SANDBOX_DB = "store_sandbox"  # clone where every change is tested first

# Roles that the application and people use (created by schema.py)
ROLES = {
    "app_user": "web application (OLTP)",
    "analyst": "reporting team (OLAP)",
    "support": "customer-support staff",
}

# Data scale (kept modest so the prototype runs on a 2-vCPU laptop/VM)
SCALE = dict(customers=50_000, products=5_000, orders=300_000, reviews=150_000)

# Monitoring
TICK_SECONDS = 2.0           # collector sampling interval
ALERT_Z = 4.0                # z-score that marks a metric as anomalous
ALERT_CONSECUTIVE = 2        # anomalous ticks in a row before raising an alert

# Safety gate
MIN_GAIN = 0.20              # performance actions must show >= 20% measured gain on the sandbox
MAX_WRITE_OVERHEAD = 0.15    # index may not slow the write template by more than 15%
ROLLBACK_P95_RISE = 0.10     # roll back if p95 of affected templates rises > 10% after apply
INDEX_BUDGET_MB = 120        # storage budget for new indexes

# Coordinator
PRIORITY = {"security": 0, "availability": 1, "correctness": 2, "performance": 3}


def dsn(db=PROD_DB, user=None):
    user = user or SUPERUSER
    pw = PG_PASSWORD if user == SUPERUSER else ROLE_PASSWORD
    s = f"host={PG_HOST} port={PG_PORT} dbname={db} user={user}"
    return s + (f" password={pw}" if pw else "")
