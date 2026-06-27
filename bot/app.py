"""FastAPI app exposing the 5 challenge endpoints (+ optional teardown).

Design goals: never raise out of a handler, always return valid JSON, keep healthz
sub-second and hard-cap tick/reply latency so the judge never sees a timeout.
"""
import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import composer, config, replier
from .llm import LLM
from .store import VALID_SCOPES, ContextStore, ConversationStore

app = FastAPI(title="Vera Bot", version=config.VERSION)
START = time.time()

store = ContextStore()
conv = ConversationStore()
llm = LLM()


@app.get("/", response_class=HTMLResponse)
async def root():
    """Human-friendly landing page. The bot is an API; the judge drives /v1/* over HTTP."""
    counts = store.counts()
    mode = "live LLM" if llm.available else "deterministic templates"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Vera bot</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:48px auto;padding:0 20px;color:#1a1a1a;line-height:1.55}}
.badge{{display:inline-block;background:#0a7d33;color:#fff;border-radius:999px;padding:2px 11px;font-size:13px;vertical-align:middle}}
code{{background:#f3f3f3;padding:1px 6px;border-radius:4px}}
a{{color:#0a58ca;text-decoration:none}} a:hover{{text-decoration:underline}}
ul{{padding-left:18px}} li{{margin:5px 0}}
.muted{{color:#666;font-size:14px}}
</style></head>
<body>
<h2>Vera <span class="badge">live</span></h2>
<p>magicpin AI Challenge submission by <b>{config.TEAM_NAME}</b>. This service is the bot itself,
not a website: the judge harness drives it over HTTP.</p>
<p class="muted">Mode: <code>{mode}</code> &nbsp;|&nbsp; contexts loaded: <code>{sum(counts.values())}</code>
&nbsp;|&nbsp; version: <code>{config.VERSION}</code></p>
<h3>Endpoints</h3>
<ul>
<li><a href="/v1/healthz">GET /v1/healthz</a> &ndash; liveness and loaded-context counts</li>
<li><a href="/v1/metadata">GET /v1/metadata</a> &ndash; bot identity and approach</li>
<li><code>POST /v1/context</code> &ndash; ingest a context push</li>
<li><code>POST /v1/tick</code> &ndash; proactive composition</li>
<li><code>POST /v1/reply</code> &ndash; one conversation turn</li>
</ul>
<p class="muted">Source code, approach and tradeoffs: <a href="{config.REPO_URL}">{config.REPO_URL}</a></p>
</body></html>"""


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": store.counts(),
        "llm": "on" if llm.available else "template_only",
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": config.TEAM_NAME,
        "team_members": config.TEAM_MEMBERS,
        "model": config.LLM_MODEL if llm.available else "deterministic_templates",
        "approach": config.APPROACH,
        "contact_email": config.CONTACT_EMAIL,
        "version": config.VERSION,
        "submitted_at": config.SUBMITTED_AT,
    }


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        b = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed", "details": "invalid JSON"})

    scope, cid, version = b.get("scope"), b.get("context_id"), b.get("version")
    if scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"})
    if not cid or not isinstance(version, int) or isinstance(version, bool):
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "malformed", "details": "context_id (str) and integer version required"})

    res = store.upsert(scope, cid, version, b.get("payload") or {})
    if not res.get("accepted"):
        return JSONResponse(status_code=409, content=res)
    return res


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    try:
        actions = await asyncio.wait_for(
            composer.run_tick(store, conv, llm, b.get("now"), b.get("available_triggers") or []),
            timeout=config.TICK_DEADLINE,
        )
    except Exception:
        actions = []  # budget exceeded or unexpected error -> say nothing this tick
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    try:
        res = await asyncio.wait_for(
            replier.respond(store, conv, llm, b), timeout=config.REPLY_DEADLINE)
    except Exception:
        res = {"action": "wait", "wait_seconds": 1800, "rationale": "internal backoff after error"}
    return res


@app.post("/v1/teardown")
async def teardown():
    store.clear()
    conv.clear()
    return {"ok": True, "wiped": True}
