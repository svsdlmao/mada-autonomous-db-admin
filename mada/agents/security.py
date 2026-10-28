"""Security agent: SQL-injection classifier + per-role access profiles (after Kamra, Terzi & Bertino)."""
import random
import re
import numpy as np
import psycopg
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_score, recall_score, f1_score, confusion_matrix
from sklearn.pipeline import make_pipeline
from .. import config as C
from ..core import Proposal, console, event
from ..schema import WORDS

TABLE_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)", re.I)

PAYLOADS = {
    "tautology": ["{w}' OR '1'='1", "{w}' OR 1=1 --", "{w}%' OR 'a'='a", "' or ''='", "{w}' OR 2>1 --"],
    "union": ["{w}%' UNION SELECT customer_id, email, 0 FROM customers --", "' UNION ALL SELECT name, email, NULL FROM customers--",
              "{w}%' UNION SELECT payment_id, card_last4, amount FROM payments --"],
    "stacked": ["{w}'; DROP TABLE orders; --", "'; UPDATE products SET price = 0; --", "{w}'; DELETE FROM reviews; --"],
    "time_based": ["{w}' AND pg_sleep(3) IS NULL --", "'||(SELECT pg_sleep(2))||'", "{w}%' AND 1=(SELECT 1 FROM pg_sleep(1)) --"],
    "error_based": ["{w}' AND 1=CAST((SELECT version()) AS int) --", "{w}' AND 1=(SELECT current_user)::int --"],
}


def obfuscate(s, r):
    if r.random() < 0.3:
        s = s.replace(" ", "/**/")
    if r.random() < 0.3:
        s = "".join(ch.upper() if r.random() < 0.5 else ch.lower() for ch in s)
    return s


def dataset(search_sql, n_benign=1500, seed=1):
    r = random.Random(seed)
    X, y, fam = [], [], []
    for _ in range(n_benign):
        k = r.randint(1, 3)
        term = " ".join(r.choice(WORDS) for _ in range(k))
        if r.random() < 0.15:
            term += f" {r.randint(1, 9999)}"
        if r.random() < 0.05:
            term = term.replace(" ", "-")
        X.append(search_sql(term)); y.append(0); fam.append("benign")
    for family, pats in PAYLOADS.items():
        for _ in range(140):
            p = r.choice(pats).format(w=r.choice(WORDS))
            X.append(search_sql(obfuscate(p, r))); y.append(1); fam.append(family)
    return X, np.array(y), fam


class SecurityAgent:
    name = "security"

    def __init__(self, search_sql):
        self.search_sql = search_sql
        self.model = None
        self.profile = {}
        self.metrics = {}

    def train(self):
        X, y, fam = dataset(self.search_sql)
        r = random.Random(3)
        idx = list(range(len(X)))
        r.shuffle(idx)
        cut = int(0.7 * len(idx))
        tr, te = idx[:cut], idx[cut:]
        pipe = make_pipeline(TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2), LogisticRegression(max_iter=2000, class_weight="balanced"))
        pipe.fit([X[i] for i in tr], y[tr])
        pred = pipe.predict([X[i] for i in te])
        self.metrics["random_split"] = dict(precision=precision_score(y[te], pred), recall=recall_score(y[te], pred), f1=f1_score(y[te], pred),
                                            cm=confusion_matrix(y[te], pred).tolist(), n_test=len(te))
        # harder test: hold out a whole attack family the model never saw
        held = "time_based"
        tr2 = [i for i in range(len(X)) if fam[i] != held and r.random() < 0.7]
        te2 = [i for i in range(len(X)) if fam[i] == held or (fam[i] == "benign" and i not in tr2)]
        pipe2 = make_pipeline(TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2), LogisticRegression(max_iter=2000, class_weight="balanced"))
        pipe2.fit([X[i] for i in tr2], y[tr2])
        pred2 = pipe2.predict([X[i] for i in te2])
        self.metrics["unseen_family"] = dict(family=held, precision=precision_score(y[te2], pred2), recall=recall_score(y[te2], pred2),
                                             f1=f1_score(y[te2], pred2), n_test=len(te2))
        self.model = pipe
        m, u = self.metrics["random_split"], self.metrics["unseen_family"]
        console.log(f"[bold green]security-agent[/] injection classifier: precision {m['precision']:.3f}, recall {m['recall']:.3f}, F1 {m['f1']:.3f} on {m['n_test']} held-out queries")
        console.log(f"[green]security-agent[/] unseen attack family '{u['family']}': precision {u['precision']:.3f}, recall {u['recall']:.3f}")
        return self.metrics

    @staticmethod
    def tables(sql):
        return {t.lower() for t in TABLE_RE.findall(sql)} - {"mada"}

    def learn_profiles(self, audit):
        prof = {}
        for t, role, tpl, sql in audit:
            p = prof.setdefault(role, {"tables": set(), "templates": set(), "n": 0})
            if tpl != "T5_product_search":
                p["tables"] |= self.tables(sql)
            else:
                p["tables"].add("products")
            p["templates"].add(tpl)
            p["n"] += 1
        self.profile = prof
        for role, p in prof.items():
            console.log(f"[green]security-agent[/] profile {role:<9} tables={sorted(p['tables'])}")
        return prof

    def scan(self, audit):
        findings = []
        search = [(t, role, tpl, sql) for t, role, tpl, sql in audit if tpl == "T5_product_search"]   # the one entry point that splices user input into SQL
        if search and self.model:
            proba = self.model.predict_proba([s for *_, s in search])[:, 1]
            for (t, role, tpl, sql), pr in zip(search, proba):
                if pr > 0.5:
                    findings.append({"type": "sql_injection", "t": t, "role": role, "score": float(pr), "sql": sql})
        for t, role, tpl, sql in audit:
            allowed = self.profile.get(role, {}).get("tables", set())
            extra = self.tables(sql) - allowed if tpl not in ("T5_product_search",) else set()
            if extra:
                findings.append({"type": "abnormal_access", "t": t, "role": role, "tables": sorted(extra), "sql": sql})
        return findings

    def respond(self, findings):
        props = []
        inj = [f for f in findings if f["type"] == "sql_injection"]
        acc = [f for f in findings if f["type"] == "abnormal_access"]
        with psycopg.connect(C.dsn(), autocommit=True) as con:
            if inj:
                pids = [r[0] for r in con.execute("SELECT pid FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() AND (query ILIKE '%%union%%select%%' OR query ILIKE '%%pg_sleep%%' OR query ILIKE '%%'' or%%')", (C.PROD_DB,))]
                f = max(inj, key=lambda x: x["score"])
                event("security", self.name, f"SQL injection detected (score {f['score']:.2f}): {f['sql'][:90]}", n=len(inj))
                if pids:
                    props.append(Proposal(agent=self.name, category="security", title=f"end session(s) {pids} sending injected SQL ({len(inj)} statements flagged)",
                                          action_sql=f"SELECT pg_terminate_backend(pid) FROM unnest(ARRAY{pids}) pid", rollback_sql="-- not reversible",
                                          resources=[f"session:{p}" for p in pids], risk="high", reversible=False,
                                          evidence={"flagged": len(inj), "example": f["sql"][:120], "score": round(f["score"], 3)}))
            if acc:
                f = acc[0]
                role, tabs = f["role"], f["tables"]
                pids = [r[0] for r in con.execute("SELECT pid FROM pg_stat_activity WHERE usename = %s AND datname = %s", (role, C.PROD_DB))]
                event("security", self.name, f"abnormal access: role {role} read {tabs} outside its profile ({len(acc)} statements)")
                props.append(Proposal(agent=self.name, category="security", title=f"revoke {role}'s access to {', '.join(tabs)} and end its sessions",
                                      action_sql="; ".join([f"REVOKE SELECT ON {t} FROM {role}" for t in tabs] + [f"SELECT pg_terminate_backend(pid) FROM unnest(ARRAY{pids or [0]}) pid"]),
                                      rollback_sql="; ".join(f"GRANT SELECT ON {t} TO {role}" for t in tabs),
                                      resources=[f"role:{role}"], risk="high", reversible=True,
                                      evidence={"statements": len(acc), "tables": tabs, "example": f["sql"][:120]}))
        return props
