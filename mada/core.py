"""Shared types: action proposals, the console/event log, and the optional LLM backend."""
import os
import time
import json
import itertools
from dataclasses import dataclass, field, asdict
from rich.console import Console

console = Console(record=True, width=118, log_path=False)
EVENTS = []          # structured event log, shown on the dashboard
_ids = itertools.count(1)


def event(kind, agent, msg, **kw):
    e = dict(t=time.time(), kind=kind, agent=agent, msg=msg, **kw)
    EVENTS.append(e)
    return e


@dataclass
class Proposal:
    agent: str                    # who proposes
    category: str                 # security | availability | correctness | performance
    title: str                    # one-line human description
    action_sql: str               # statement(s) the gate will run on production
    rollback_sql: str             # how to undo it
    resources: list               # shared resources it touches, e.g. ["table:orders", "memory"]
    risk: str = "low"             # low | medium | high
    reversible: bool = True
    evidence: dict = field(default_factory=dict)   # measurements behind the proposal
    predicted_gain: float = 0.0   # fraction, e.g. 0.6 = 60% faster on the affected templates
    templates: list = field(default_factory=list)  # templates whose latency the action should change
    sandbox_check: object = None  # callable(sandbox_dsn) -> measured gain (filled by agents)
    id: int = field(default_factory=lambda: next(_ids))
    status: str = "proposed"

    def brief(self):
        d = asdict(self)
        d.pop("sandbox_check")
        return d

    def as_json(self):
        d = {k: v for k, v in self.brief().items() if k in ("agent", "action_sql", "evidence", "predicted_gain", "resources", "risk", "reversible", "rollback_sql")}
        return json.dumps(d, indent=2, default=str)


class LLM:
    """Pluggable reasoning backend.  With no API key the prototype runs in rule mode:
    explanations come from the knowledge base's templates instead of a language model."""

    def __init__(self):
        self.mode = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "rules"

    def explain(self, facts, fallback):
        if self.mode == "rules":
            return fallback
        try:  # optional path, used only when a key is configured
            import anthropic
            client = anthropic.Anthropic()
            msg = client.messages.create(model=os.environ.get("MADA_MODEL", "claude-sonnet-5-5"), max_tokens=300,
                                         messages=[{"role": "user", "content": "You are a PostgreSQL DBA. In two sentences, explain the root cause and the fix.\n" + json.dumps(facts, default=str)}])
            return msg.content[0].text
        except Exception:
            return fallback


llm = LLM()
