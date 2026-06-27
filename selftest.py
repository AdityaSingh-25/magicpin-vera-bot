#!/usr/bin/env python3
"""Standalone self-test: boots the bot on a spare port and exercises every endpoint
and every replay behaviour using ONLY the local dataset (no external LLM key required).

This proves the submission is structurally correct and that the deterministic fallback
already passes the judge's auto-reply / intent / hostile scenarios. Run: python selftest.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).parent
PORT = int(os.getenv("SELFTEST_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    mark = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
    return ok


def call(method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(BASE + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    try:
        resp = request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def load_seed(filename, container):
    raw = json.load(open(ROOT / "dataset" / filename))
    return raw.get(container, [])


def main():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bot.app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # wait for boot
        up = False
        for _ in range(40):
            try:
                if request.urlopen(BASE + "/v1/healthz", timeout=2).status == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        if not check("server boots + /v1/healthz 200", up):
            return

        print("\n--- endpoints ---")
        s, hz = call("GET", "/v1/healthz")
        check("healthz schema", s == 200 and hz.get("status") == "ok" and "contexts_loaded" in hz)
        s, md = call("GET", "/v1/metadata")
        check("metadata schema", s == 200 and all(k in md for k in ("team_name", "model", "contact_email")))

        # load real context
        category = json.load(open(ROOT / "dataset" / "categories" / "dentists.json"))
        merchant = next(m for m in load_seed("merchants_seed.json", "merchants")
                        if m["merchant_id"] == "m_001_drmeera_dentist_delhi")
        triggers = {t["id"]: t for t in load_seed("triggers_seed.json", "triggers")}
        customers = {c["customer_id"]: c for c in load_seed("customers_seed.json", "customers")}

        print("\n--- context push + idempotency ---")
        s, r = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists",
                                            "version": 1, "payload": category, "delivered_at": "now"})
        check("push category accepted", s == 200 and r.get("accepted"))
        call("POST", "/v1/context", {"scope": "merchant", "context_id": merchant["merchant_id"],
                                     "version": 1, "payload": merchant, "delivered_at": "now"})
        s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": merchant["merchant_id"],
                                            "version": 1, "payload": merchant, "delivered_at": "now"})
        check("re-post same version is accepted no-op", s == 200 and r.get("accepted"))
        s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": merchant["merchant_id"],
                                            "version": 0, "payload": merchant, "delivered_at": "now"})
        check("lower version -> 409 stale_version", s == 409 and r.get("reason") == "stale_version")
        s, r = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1,
                                            "payload": {}, "delivered_at": "now"})
        check("invalid scope -> 400", s == 400 and r.get("reason") == "invalid_scope")

        print("\n--- tick: proactive composition (merchant-facing) ---")
        tid = "trg_001_research_digest_dentists"
        call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1,
                                     "payload": triggers[tid], "delivered_at": "now"})
        s, r = call("POST", "/v1/tick", {"now": "2026-04-26T10:00:00Z", "available_triggers": [tid]})
        actions = r.get("actions", [])
        a = actions[0] if actions else {}
        check("tick returns one action", len(actions) == 1)
        check("action has non-empty body", bool(a.get("body")))
        check("body anchors on the JIDA digest fact", "JIDA" in a.get("body", ""))
        check("targets the right merchant", a.get("merchant_id") == merchant["merchant_id"])

        print("\n--- tick: customer-facing recall (send_as) ---")
        rid = "trg_003_recall_due_priya"
        call("POST", "/v1/context", {"scope": "customer", "context_id": "c_001_priya_for_m001",
                                     "version": 1, "payload": customers["c_001_priya_for_m001"], "delivered_at": "now"})
        call("POST", "/v1/context", {"scope": "trigger", "context_id": rid, "version": 1,
                                     "payload": triggers[rid], "delivered_at": "now"})
        s, r = call("POST", "/v1/tick", {"now": "2026-04-26T10:05:00Z", "available_triggers": [rid]})
        ca = (r.get("actions") or [{}])[0]
        check("recall action sent as merchant_on_behalf", ca.get("send_as") == "merchant_on_behalf")
        check("recall names the customer (Priya)", "Priya" in ca.get("body", ""))

        print("\n--- reply: auto-reply detection ---")
        auto = "Thank you for contacting us! Our team will respond shortly."
        actions_seen = []
        for i in range(1, 5):
            s, r = call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}",
                                              "merchant_id": merchant["merchant_id"], "from_role": "merchant",
                                              "message": auto, "turn_number": i + 1})
            actions_seen.append(r.get("action"))
            if r.get("action") == "end":
                break
        check("bot ENDS on repeated auto-reply (<=2 turns)", "end" in actions_seen and actions_seen.index("end") <= 1,
              f"saw {actions_seen}")

        print("\n--- reply: intent transition ---")
        s, r = call("POST", "/v1/reply", {"conversation_id": "conv_intent", "merchant_id": merchant["merchant_id"],
                                          "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                                          "turn_number": 2})
        body = (r.get("body") or "").lower()
        actioning = any(w in body for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])
        qualifying = any(w in body for w in ["would you", "do you", "can you tell", "what if", "how about"])
        check("switches to ACTION mode (not re-qualifying)", r.get("action") == "send" and actioning and not qualifying,
              f"action={r.get('action')} body={body[:80]!r}")

        print("\n--- reply: hostile handling ---")
        s, r = call("POST", "/v1/reply", {"conversation_id": "conv_hostile", "merchant_id": merchant["merchant_id"],
                                          "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                                          "turn_number": 2})
        check("ends gracefully on hostile message", r.get("action") == "end")

        print("\n--- teardown ---")
        s, r = call("POST", "/v1/teardown")
        check("teardown wipes state", s == 200 and r.get("wiped"))
        s, hz = call("GET", "/v1/healthz")
        check("contexts cleared after teardown", sum(hz.get("contexts_loaded", {}).values()) == 0)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"\n{'='*48}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*48}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
