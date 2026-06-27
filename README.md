# Vera: magicpin AI Challenge submission

A stateful HTTP bot that plays magicpin's merchant-growth assistant **Vera**: it ingests
the 4-context framework (category, merchant, trigger and customer), proactively starts
WhatsApp conversations on `/v1/tick` and handles merchant replies on `/v1/reply`.

The submission is the **running service**: one public base URL exposing the 5 endpoints.

## Endpoints (`/v1/*`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/context` | Version-aware idempotent context ingest (`category`/`merchant`/`customer`/`trigger`). |
| POST | `/v1/tick` | Proactive composition. Returns 0..N `actions[]`. |
| POST | `/v1/reply` | Continue a conversation. Returns `send` / `wait` / `end`. |
| GET | `/v1/healthz` | Liveness plus `contexts_loaded` counts. |
| GET | `/v1/metadata` | Team identity plus model and approach. |
| POST | `/v1/teardown` | (optional) wipe all state. |

## Approach

**Trigger-routed composition.** Each trigger `kind` maps to a message *archetype*
(`knowledge`, `perf`, `renewal`, `customer`, `seasonal`, `milestone`, `review`, `ask`,
`generic`). The archetype picks the framing. The body is written by an LLM (temperature 0)
under strict grounding rules, with a **deterministic template fallback** for every
archetype. If no API key is configured, or the model is slow, errors or returns something
off-voice, the template is used, so the bot is always correct and always fast.

The LLM layer is **provider-agnostic**: it speaks the OpenAI-compatible Chat Completions
API, so `LLM_BASE_URL`, `LLM_API_KEY` and `VERA_MODEL` can point at any compatible
endpoint. No vendor is hard-coded.

**Grounding first.** The system prompt forbids inventing numbers, research, competitors or
offers and requires citing sources verbatim. Output is re-validated for taboo words
(`category.voice.vocab_taboo`), length and a single CTA before it leaves the box. This
targets the rubric's *Specificity* dimension and the hallucination penalty.

**Reply state machine.** Inbound messages are classified deterministically (this gates the
replay tests) then a `send` body may be LLM-enriched and re-validated:

- **Auto-reply** (canned phrasing or the same text repeated across the merchant) leads to
  one polite re-route then `end`. Detection is keyed on `(merchant, message)`, so it works
  even when the harness spreads the canned text across different `conversation_id` values.
- **Intent commitment** ("ok let's do it") switches to **action mode** immediately. The body
  is validated to contain action words and no qualifiers, matching the judge's check exactly.
- **Hostile or not-interested** leads to a graceful `end`.
- **Question, positive or neutral** leads to an answer or a low-friction nudge, stopping
  after repeated silence.

**Operational safety** (protects the penalty bucket): handlers never raise and always
return valid JSON. `/v1/tick` and `/v1/reply` are wrapped in hard deadlines (default 12s,
well under the local judge's 15s) and return `{"actions": []}` or a safe `wait` on timeout.
`/v1/healthz` is sub-millisecond. One action per `(merchant)` per tick, plus suppression
keys and per-conversation anti-repetition, prevent spam and verbatim repeats.

## Tradeoffs

- **Heuristic classifier rather than an LLM for reply routing.** It is deterministic and
  instant, and it makes the gated replay behaviours reliable. The LLM only enriches copy and
  never decides send, wait or end.
- **One LLM call per composed action**, capped at 6 compositions per tick and run
  concurrently, to stay inside the latency budget. Restraint is both rewarded by the rubric
  and required by the budget.
- **In-memory state** (single worker) matches the "no restarts mid-test" contract. Nothing
  is persisted (privacy rule).
- **Number-grounding is enforced softly** (re-prompt or fallback rather than hard rejection)
  to avoid discarding good copy on false positives.

## What additional context would have helped most

1. **Resolved trigger payloads.** Triggers reference `top_item_id`. Bundling the full digest
   item on the trigger would remove a lookup and de-risk specificity.
2. **Open booking slots and live offer inventory** on the merchant for customer-facing sends.
3. **Per-merchant message history with outcomes** (what was sent, did it convert) to tune cadence.
4. **An explicit script preference** beyond `languages` (Latin vs Devanagari) for code-mix.

## Run locally

```bash
make install        # python venv plus deps
make selftest       # boots the bot on :8099 and checks every endpoint and replay (no key needed)
make run PORT=8090  # serve (8080 may be taken by Docker on this machine)
```

Add an API key to switch from templates to live LLM composition:

```bash
export LLM_API_KEY=...
export LLM_BASE_URL=https://api.openai.com/v1
export VERA_MODEL=gpt-4o-mini
make run PORT=8090
```

## Deploy (pick one) then submit the base URL

- **Render**: repo includes `render.yaml` (uses `$PORT`, healthcheck `/v1/healthz`). Set
  `LLM_API_KEY` in the dashboard. Use the **starter** plan rather than free (free idles out
  and can trip the 3-times-healthz-fail disqualification).
- **Fly or Railway**: `Dockerfile` and `Procfile` are provided. Both inject `$PORT`.
- **ngrok**: `make run PORT=8090` then `ngrok http 8090`. Use a **reserved domain** so the
  URL survives the full test window.

Then submit `https://<your-host>` (base URL, no path) in the portal. `/v1/metadata` is wired
from env vars (`VERA_TEAM_NAME`, `VERA_CONTACT_EMAIL` and so on), so keep it consistent with
the portal.

## Layout

```
bot/
  app.py        # FastAPI: the 5 endpoints plus deadline guards
  composer.py   # /tick: trigger ranking, archetype routing, LLM plus templates
  replier.py    # /reply: classify then send/wait/end, intent and auto-reply handling
  store.py      # versioned context store plus conversation/suppression state
  llm.py        # async OpenAI-compatible adapter (no SDK dep) plus JSON extraction
  grounding.py  # taboo/number/anti-repeat plus action-mode validation
  config.py     # env-driven settings plus team metadata
selftest.py     # end-to-end check, no external key required
```
