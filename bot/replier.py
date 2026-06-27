"""Reply handler: given an inbound merchant/customer message, decide send / wait / end.

Classification is deterministic (fast, and it gates the replay tests) then the *body*
of a 'send' may be enriched by the LLM and re-validated. The three production pains the
brief calls out map directly to branches here:
  - auto-reply pollution  -> detect + at most one re-route, then end
  - intent-handoff fail   -> on commitment, switch to action mode (no more qualifying)
  - knowing when to stop   -> hostile / hard-no / repeated silence -> end
"""
from .grounding import is_action_mode, normalize, valid_body
from .llm import extract_json

AUTO_PHRASES = [
    "thank you for contacting", "thanks for contacting", "will respond shortly",
    "will get back to you", "we will get back", "automated", "auto-reply", "auto reply",
    "out of office", "team will", "currently unavailable", "received your message",
    "shukriya", "team tak pahunch",
]
HOSTILE = ["spam", "useless", "stop messaging", "stop texting", "leave me alone",
           "shut up", "nonsense", "bakwaas", "bekaar", "mat bhejo", "band karo"]
HARD_NO = ["not interested", "no thanks", "no thank you", "don't message", "dont message",
           "unsubscribe", "remove me", "nahi chahiye", "mujhe nahi"]
INTENT = ["lets do it", "let's do it", "go ahead", "do it", "sign me up", "i want to join",
          "join magicpin", "yes please", "lets go", "let's go", "ready to start", "ok lets",
          "ok let's", "haan kar do", "kar do", "chalega", "theek hai karo", "set it up"]
POSITIVE = ["yes", "sure", "okay", "ok", "sounds good", "interested", "tell me more",
            "haan", "thik hai", "great"]
WH = ("what", "why", "how", "when", "where", "who", "which", "kya", "kaise", "kitna", "kab")

INTENT_ACTION = ("Perfect, setting it up now. I've drafted everything on my side and will "
                 "confirm here the moment it's live. Next, I'll send the preview for your quick approval. 👍")
AUTOREPLY_REROUTE = ("No problem, I'll keep this to one line. There's a 2-minute fix on your Google "
                     "listing ready on my side. Reply YES and I'll do it, or STOP and I won't message again.")
QUESTION_FALLBACK = ("Good question. Short version: I can set this up for you in about 2 minutes at no cost "
                     "to try. Want me to go ahead? Reply YES.")
NUDGE_FALLBACK = ("One small thing on your listing is costing you views. I can fix it in 2 minutes. "
                  "Want me to? Reply YES or STOP.")


def classify(message, repeat_count):
    m = normalize(message)
    if not m:
        return "neutral"
    # Repeated identical inbound across the merchant = canned auto-reply.
    if repeat_count >= 2 and len(m) > 8:
        return "auto_reply"
    if any(k in m for k in AUTO_PHRASES):
        return "auto_reply"
    if any(k in m for k in HOSTILE):
        return "hostile"
    if any(k in m for k in HARD_NO):
        return "hard_no"
    if any(k in m for k in INTENT):
        return "intent"
    if m.endswith("?") or m.startswith(WH):
        return "question"
    if any(k in m for k in POSITIVE):
        return "positive"
    return "neutral"


def _send(conv, conv_id, body, cta, rationale):
    # Anti-repetition guard: never send a body we already sent in this conversation.
    if conv.already_sent(conv_id, body):
        return {"action": "end", "rationale": "Nothing new to add without repeating; exiting cleanly."}
    conv.record_outbound(conv_id, body)
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


async def _maybe_llm_body(llm, store, payload, classification, default_body):
    """Enrich a send body with the LLM when we have context; otherwise keep the template.
    Always re-validate so intent stays in action-mode and nothing leaks taboo words."""
    if not (llm and llm.available):
        return default_body
    merchant = store.get("merchant", payload.get("merchant_id"))
    category = store.category_for_merchant(merchant)
    if not (merchant and category):
        return default_body
    taboos = category.get("voice", {}).get("vocab_taboo", [])
    system = (
        "You are Vera continuing a WhatsApp chat with a merchant. Reply in ONE short, specific, "
        "peer-toned message using ONLY facts in the context. Honour Hindi-English code-mix if allowed. "
        "One CTA, last. Output STRICT JSON: {\"body\": str}."
    )
    if classification == "intent":
        system += " The merchant just committed, so ACT now: say what you're doing or sending next. Do NOT ask qualifying questions."
    prompt = (
        f"Merchant: {merchant.get('identity', {})}\nSignals: {merchant.get('signals', [])}\n"
        f"Active offers: {[o.get('title') for o in merchant.get('offers', []) if o.get('status')=='active']}\n"
        f"Their message: \"{payload.get('message','')}\"\nWrite the reply JSON."
    )
    out = extract_json(await llm.complete(system, prompt, max_tokens=400))
    body = (out or {}).get("body", "").strip() if isinstance(out, dict) else ""
    if not valid_body(body, taboos):
        return default_body
    if classification == "intent" and not is_action_mode(body):
        return default_body  # would fail the intent-transition gate
    return body


async def respond(store, conv, llm, payload):
    conv_id = payload.get("conversation_id", "conv_unknown")
    merchant_id = payload.get("merchant_id")
    message = payload.get("message", "") or ""

    repeat = conv.record_inbound(conv_id, merchant_id, message)
    cls = classify(message, repeat)

    if cls == "auto_reply":
        # Try once after detecting, then stop wasting turns (brief Pattern B).
        if conv.autoreply_sends(merchant_id) == 0:
            conv.bump_autoreply(merchant_id)
            return _send(conv, conv_id, AUTOREPLY_REROUTE, "binary",
                         "Likely auto-reply detected; one polite re-route before exiting.")
        return {"action": "end",
                "rationale": "Repeated canned/auto-reply text; exiting to avoid burning turns."}

    if cls in ("hostile", "hard_no"):
        return {"action": "end",
                "rationale": "Merchant signalled stop/not-interested; exiting gracefully and politely."}

    if cls == "intent":
        body = await _maybe_llm_body(llm, store, payload, "intent", INTENT_ACTION)
        if not is_action_mode(body):
            body = INTENT_ACTION
        return _send(conv, conv_id, body, "open_ended",
                     "Explicit commitment detected; switched from qualifying to action immediately.")

    if cls == "question":
        body = await _maybe_llm_body(llm, store, payload, "question", QUESTION_FALLBACK)
        return _send(conv, conv_id, body, "binary", "Answered the merchant's question and advanced with one CTA.")

    if cls == "positive":
        body = await _maybe_llm_body(llm, store, payload, "positive", INTENT_ACTION)
        return _send(conv, conv_id, body, "open_ended", "Positive signal; moving toward action.")

    # neutral / unclear
    if conv.outbound_count(conv_id) >= 2:
        return {"action": "end", "rationale": "No clear engagement after multiple nudges; stopping respectfully."}
    body = await _maybe_llm_body(llm, store, payload, "neutral", NUDGE_FALLBACK)
    return _send(conv, conv_id, body, "binary", "Neutral reply; one low-friction nudge with a binary CTA.")
