"""In-memory, version-aware context store + conversation state.

Single-process, single-worker design (the challenge says in-memory is fine; just no
restarts mid-test). A threading.Lock guards mutations so it is safe even if uvicorn
is run with a thread pool.
"""
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .grounding import normalize

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ContextStore:
    """Keyed by (scope, context_id). Idempotent + version-replacing per the spec."""

    def __init__(self):
        self._ctx = {}  # (scope, cid) -> {"version", "payload", "stored_at"}
        self._lock = threading.Lock()

    def upsert(self, scope, cid, version, payload):
        key = (scope, cid)
        with self._lock:
            cur = self._ctx.get(key)
            if cur and cur["version"] > version:
                return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
            if cur and cur["version"] == version:
                # Re-posting the same version is a no-op (still acknowledged).
                return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": cur["stored_at"]}
            stored_at = _now_iso()
            self._ctx[key] = {"version": version, "payload": payload, "stored_at": stored_at}
            return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": stored_at}

    def get(self, scope, cid):
        if not cid:
            return None
        entry = self._ctx.get((scope, cid))
        return entry["payload"] if entry else None

    def category_for_merchant(self, merchant):
        if not merchant:
            return None
        return self.get("category", merchant.get("category_slug", ""))

    def counts(self):
        c = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _cid) in self._ctx:
            c[scope] = c.get(scope, 0) + 1
        return c

    def clear(self):
        with self._lock:
            self._ctx.clear()


class ConversationStore:
    """Per-conversation history + cross-conversation auto-reply tracking + suppression.

    Auto-reply detection keys off (merchant_id, normalized message) so it works even
    when the harness fragments the canned text across different conversation_ids
    (as the local simulator does).
    """

    def __init__(self):
        self._conv = defaultdict(lambda: {"inbound": [], "outbound": []})
        self._merchant_inbound = defaultdict(Counter)   # merchant_id -> Counter(norm_msg)
        self._merchant_autoreply_sends = Counter()      # merchant_id -> reroute count
        self._fired_suppressions = set()
        self._lock = threading.Lock()

    def record_inbound(self, conv_id, merchant_id, message):
        norm = normalize(message)
        with self._lock:
            self._conv[conv_id]["inbound"].append(message)
            if merchant_id:
                self._merchant_inbound[merchant_id][norm] += 1
                return self._merchant_inbound[merchant_id][norm]
        return 1

    def record_outbound(self, conv_id, body):
        with self._lock:
            self._conv[conv_id]["outbound"].append(body)

    def already_sent(self, conv_id, body):
        target = normalize(body)
        return any(normalize(b) == target for b in self._conv[conv_id]["outbound"])

    def outbound_count(self, conv_id):
        return len(self._conv[conv_id]["outbound"])

    def autoreply_sends(self, merchant_id):
        return self._merchant_autoreply_sends[merchant_id]

    def bump_autoreply(self, merchant_id):
        with self._lock:
            self._merchant_autoreply_sends[merchant_id] += 1

    def suppression_fired(self, key):
        return bool(key) and key in self._fired_suppressions

    def fire_suppression(self, key):
        if key:
            with self._lock:
                self._fired_suppressions.add(key)

    def clear(self):
        with self._lock:
            self._conv.clear()
            self._merchant_inbound.clear()
            self._merchant_autoreply_sends.clear()
            self._fired_suppressions.clear()
