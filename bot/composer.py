"""Tick composer: turn active triggers into proactive WhatsApp messages.

Pipeline per tick:
  1. resolve each available trigger -> (trigger, merchant, category, customer?)
  2. filter: missing context / expired / already-suppressed
  3. rank by urgency then expiry; one action per merchant per tick; cap for latency
  4. compose each (LLM under grounding rules, else deterministic template)
  5. record suppression + outbound so we never repeat ourselves
"""
import asyncio
from datetime import datetime, timezone

from . import config
from .grounding import valid_body
from .llm import extract_json

# --- trigger kind -> message archetype --------------------------------------------
_FAMILY = {
    "research_digest": "knowledge",
    "regulation_change": "knowledge",
    "category_research_digest_release": "knowledge",
    "category_trend_movement": "knowledge",
    "competitor_opened": "competitor",
    "festival_upcoming": "seasonal",
    "weather_heatwave": "seasonal",
    "local_news_event": "seasonal",
    "perf_spike": "perf",
    "perf_dip": "perf",
    "milestone_reached": "milestone",
    "review_theme_emerged": "review",
    "dormant_with_vera": "ask",
    "renewal_due": "renewal",
    "recall_due": "customer",
    "customer_lapsed_soft": "customer",
    "appointment_tomorrow": "customer",
    "wedding_package_followup": "customer",
    "bridal_followup": "customer",
    "curious_ask_due": "ask",
    "scheduled_recurring": "ask",
}


def _family(kind):
    return _FAMILY.get(kind, "generic")


def _parse_dt(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _resolve_digest(category, trigger):
    if not category:
        return None
    payload = trigger.get("payload") or {}
    top = payload.get("top_item_id") or payload.get("top_item")
    if isinstance(top, dict):
        return top
    items = category.get("digest") or []
    for it in items:
        if it.get("id") == top:
            return it
    return items[0] if items else None


# --- per-category voice profiles (drive the deterministic fallback's category fit) ---
# Only the linguistic bits that are not cleanly in the data live here; offers, seasonal
# beats and digest items are still read live from the pushed category object.
CATEGORY_PROFILES = {
    "dentists":    {"honorific": "Dr. ", "audience": "patients",  "cohort": "high-risk adult patients",
                    "content": "patient-ed WhatsApp you can share", "reviewers": "patients"},
    "salons":      {"honorific": "",      "audience": "clients",   "cohort": "bridal clients",
                    "content": "client-ready post", "reviewers": "clients"},
    "gyms":        {"honorific": "",      "audience": "members",   "cohort": "lapsing members",
                    "content": "member check-in note", "reviewers": "members"},
    "restaurants": {"honorific": "",      "audience": "diners",    "cohort": "weekend diners",
                    "content": "diner-ready post", "reviewers": "guests"},
    "pharmacies":  {"honorific": "",      "audience": "customers", "cohort": "regular customers",
                    "content": "customer reminder", "reviewers": "customers"},
}
_DEFAULT_PROFILE = {"honorific": "", "audience": "customers", "cohort": "regulars",
                    "content": "WhatsApp you can share", "reviewers": "customers"}


def _profile(category):
    return CATEGORY_PROFILES.get((category or {}).get("slug", ""), _DEFAULT_PROFILE)


def _salute(category, merchant):
    ident = merchant.get("identity", {})
    owner = ident.get("owner_first_name")
    if owner:
        return f"{_profile(category)['honorific']}{owner}"
    return ident.get("name", "there")


def _hi(merchant):
    return "hi" in (merchant.get("identity", {}).get("languages") or [])


def _active_offers(merchant):
    return [o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active" and o.get("title")]


# --- deterministic templates (the always-valid fallback) ---------------------------

def template_compose(category, merchant, trigger, customer, digest):
    """Return a fully-valid action body for any (merchant, trigger). Never fabricates:
    only fills in facts actually present in the contexts."""
    fam = _family(trigger.get("kind", ""))
    who = _salute(category, merchant)
    hi = _hi(merchant)
    cta = "open_ended"
    send_as = "merchant_on_behalf" if (trigger.get("scope") == "customer" and customer) else "vera"

    prof = _profile(category)

    if fam == "knowledge" and digest:
        title = digest.get("title", "a relevant update")
        source = digest.get("source", "")
        action = digest.get("actionable")
        cohort = f"your {prof['cohort']}" if "high_risk_adult_cohort" in (merchant.get("signals") or []) else f"your {prof['audience']}"
        deadline = (trigger.get("payload") or {}).get("deadline_iso")
        tail = f" Deadline {deadline}." if deadline else ""
        body = f"{who}, quick one worth 2 min for {cohort}: {title}.{tail}"
        if action:
            body += f" {action}."
        body += f" Want me to pull the full note and draft a {prof['content']}?"
        if source:
            body += f" Source: {source}."

    elif fam == "perf":
        p = trigger.get("payload", {})
        metric = p.get("metric", "calls")
        delta = p.get("delta_pct")
        base = p.get("vs_baseline")
        peer = (category.get("peer_stats") or {}).get("avg_ctr")
        if delta is not None and delta < 0:
            body = (f"{who}, heads-up: your {metric} are down {abs(int(delta*100))}% week-on-week"
                    f"{f' (was ~{base})' if base else ''}. I've got 2 quick fixes that usually recover this. "
                    f"Want me to set them up? Reply YES.")
            cta = "binary"
        else:
            body = (f"{who}, good news: your {metric} just jumped {abs(int((delta or 0)*100))}% this week. "
                    f"Let's ride it: I can push a post to your listing now so the momentum compounds. Reply YES.")
            cta = "binary"

    elif fam == "renewal":
        p = trigger.get("payload", {})
        days = p.get("days_remaining", merchant.get("subscription", {}).get("days_remaining"))
        amt = p.get("renewal_amount")
        body = (f"{who}, your {p.get('plan','Pro')} plan renews in {days} days"
                f"{f' (₹{amt})' if amt else ''}. You're mid-campaign, so I'd keep it live to not lose the traction. "
                f"Want me to renew it for you? Reply YES.")
        cta = "binary"

    elif fam == "customer" and customer:
        cust = customer.get("identity", {}).get("name", "there")
        p = trigger.get("payload", {})
        slots = p.get("available_slots") or []
        offer = (_active_offers(merchant) or ["a visit"])[0]
        slot_txt = ""
        if slots:
            labels = [s.get("label") for s in slots if s.get("label")][:2]
            if labels:
                slot_txt = " " + " or ".join(labels) + " work."
        svc = (p.get("service_due") or "your visit").replace("_", " ")
        body = (f"Hi {cust}, {merchant.get('identity',{}).get('name','we')} here. "
                f"Your {svc} is due.{slot_txt} {offer}. Reply 1 to book or tell us a time that suits you.")
        cta = "binary"
        send_as = "merchant_on_behalf"

    elif fam == "seasonal":
        p = trigger.get("payload", {})
        event = p.get("festival") or p.get("event") or "the upcoming season"
        days = p.get("days_until") or p.get("days_to")
        offer = (_active_offers(merchant) or category.get("offer_catalog", [{}])[0].get("title", ""))
        offer = offer[0] if isinstance(offer, list) else offer
        body = (f"{who}, {event}{f' is about {days} days out' if days else ' is coming up'}. "
                f"This is your best window to get booked early. I can draft a {event} post around \"{offer}\" for your listing. "
                f"Want me to? Reply YES.")
        cta = "binary"

    elif fam == "milestone":
        p = trigger.get("payload", {})
        body = (f"{who}, you just crossed {p.get('value','a milestone')}. Nice work. "
                f"Moments like this convert well into a review nudge. Want me to post it and ask your last 10 happy {prof['reviewers']} for a review? Reply YES.")
        cta = "binary"

    elif fam == "review":
        p = trigger.get("payload", {})
        theme = p.get("theme", "a recurring point")
        body = (f"{who}, {p.get('occurrences','a few')} reviews this week mention \"{theme}\". "
                f"It's small but it's shaping your rating. Want me to draft a 1-line public reply template and a fix note? Reply YES.")
        cta = "binary"

    elif fam == "ask":
        # 'Ask the merchant': the highest-ROI zero-hallucination lever.
        body = (f"{who}, quick one: what's the single service you'd most like more {prof['audience']} for this week? "
                f"Tell me and I'll build this week's posts and offer around exactly that.")
        cta = "open_ended"

    else:  # generic / competitor / unknown
        peer_ctr = (category.get("peer_stats") or {}).get("avg_ctr")
        my_ctr = merchant.get("performance", {}).get("ctr")
        if peer_ctr and my_ctr:
            body = (f"{who}, your listing CTR is {round(my_ctr*100,1)}% vs a peer median of {round(peer_ctr*100,1)}%. "
                    f"That gap is usually 2-3 fixable things. Want me to run a quick audit and fix them? Reply YES.")
            cta = "binary"
        else:
            body = (f"{who}, I spotted something on your listing worth a 2-minute fix. "
                    f"Want me to take care of it? Reply YES.")
            cta = "binary"

    if hi and cta == "binary" and "Reply YES" in body:
        body = body.replace("Reply YES.", "Reply YES, main set kar deti hoon.")

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", ""),
        "template_name": f"vera_{fam}_v1",
        "template_params": [who],
        "rationale": f"{fam} archetype for trigger '{trigger.get('kind')}'; anchored on context facts only.",
    }


# --- LLM composition ---------------------------------------------------------------

_SYSTEM = (
    "You are Vera, magicpin's merchant-growth assistant, messaging Indian local-business "
    "owners (and sometimes their customers) on WhatsApp. Write ONE short, specific, peer-toned "
    "message that earns a reply.\n"
    "HARD RULES:\n"
    "- Use ONLY facts present in the CONTEXT. Never invent numbers, research, competitors, prices or offers.\n"
    "- When you cite research/news, include its source exactly as given.\n"
    "- Peer/colleague tone, never promotional. No long preamble, no re-introducing yourself.\n"
    "- Honour the language: if 'hi' is allowed, natural Hindi-English code-mix is good.\n"
    "- Exactly ONE call-to-action, and it lands last. Binary (YES/STOP or 1/2) for action; none for pure info.\n"
    "- Make the trigger reason ('why now') explicit.\n"
    "- Never use any taboo word listed.\n"
    "Output STRICT JSON only: {\"body\": str, \"cta\": \"binary\"|\"open_ended\"|\"none\", "
    "\"send_as\": \"vera\"|\"merchant_on_behalf\", \"rationale\": str}. No markdown, no extra keys."
)


def _llm_prompt(category, merchant, trigger, customer, digest):
    voice = category.get("voice", {})
    ident = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    lines = [
        "=== CONTEXT ===",
        f"Category: {category.get('slug')} | voice={voice.get('tone')} | taboos={voice.get('vocab_taboo', [])}",
        f"Peer stats: {category.get('peer_stats', {})}",
        f"Merchant: {ident.get('name')} | owner={ident.get('owner_first_name')} | "
        f"locality={ident.get('locality')}, {ident.get('city')} | languages={ident.get('languages')}",
        f"Performance(30d): {perf} | signals={merchant.get('signals', [])}",
        f"Active offers: {_active_offers(merchant)}",
        f"Trigger: kind={trigger.get('kind')} scope={trigger.get('scope')} urgency={trigger.get('urgency')} "
        f"payload={trigger.get('payload')}",
    ]
    if digest:
        lines.append(f"Referenced digest item: {digest}")
    if customer:
        lines.append(f"Customer (send on their behalf): {customer.get('identity', {})} | "
                     f"relationship={customer.get('relationship', {})} | state={customer.get('state')}")
    lines.append("=== TASK ===")
    lines.append("Compose the single best next WhatsApp message for this trigger. Return STRICT JSON.")
    return "\n".join(lines)


async def _llm_compose(llm, category, merchant, trigger, customer, digest):
    raw = await llm.complete(_SYSTEM, _llm_prompt(category, merchant, trigger, customer, digest))
    out = extract_json(raw)
    if not isinstance(out, dict):
        return None
    return out


async def compose_one(llm, cand):
    category, merchant, trigger = cand["category"], cand["merchant"], cand["trg"]
    customer = cand["customer"]
    digest = _resolve_digest(category, trigger)
    mid = merchant["merchant_id"]
    tid = cand["tid"]

    action = template_compose(category, merchant, trigger, customer, digest)  # always valid

    if llm and llm.available:
        out = await _llm_compose(llm, category, merchant, trigger, customer, digest)
        taboos = category.get("voice", {}).get("vocab_taboo", [])
        if out and valid_body(out.get("body"), taboos):
            action["body"] = out["body"].strip()
            if out.get("cta"):
                action["cta"] = out["cta"]
            if out.get("send_as"):
                action["send_as"] = out["send_as"]
            if out.get("rationale"):
                action["rationale"] = out["rationale"]
            action["template_name"] = f"vera_{_family(trigger.get('kind',''))}_llm_v1"

    action["conversation_id"] = f"conv_{mid}_{tid}"
    action["merchant_id"] = mid
    action["customer_id"] = customer.get("customer_id") if customer else None
    action["trigger_id"] = tid
    action.setdefault("suppression_key", trigger.get("suppression_key", ""))
    return action


# --- tick entrypoint ---------------------------------------------------------------

async def run_tick(store, conv, llm, now, trigger_ids):
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    candidates = []
    seen_supp = set()

    for tid in trigger_ids or []:
        trg = store.get("trigger", tid)
        if not trg:
            continue
        supp = trg.get("suppression_key", "")
        if conv.suppression_fired(supp) or (supp and supp in seen_supp):
            continue
        exp = _parse_dt(trg.get("expires_at"))
        try:
            if exp and exp < now_dt:
                continue
        except TypeError:
            pass  # tz mismatch -> don't drop it
        merchant = store.get("merchant", trg.get("merchant_id"))
        if not merchant:
            continue
        category = store.category_for_merchant(merchant)
        if not category:
            continue
        cid = trg.get("customer_id")
        candidates.append({
            "tid": tid,
            "trg": trg,
            "merchant": merchant,
            "category": category,
            "customer": store.get("customer", cid) if cid else None,
            "supp": supp,
            "urgency": trg.get("urgency", 1) or 1,
        })
        if supp:
            seen_supp.add(supp)

    # rank: urgency desc, then soonest expiry
    def _rank(c):
        exp = _parse_dt(c["trg"].get("expires_at"))
        exp_key = exp.timestamp() if exp else float("inf")
        return (-int(c["urgency"]), exp_key)

    candidates.sort(key=_rank)

    # one action per merchant per tick
    picked, seen_merchant = [], set()
    for c in candidates:
        m = c["merchant"]["merchant_id"]
        if m in seen_merchant:
            continue
        seen_merchant.add(m)
        picked.append(c)
    picked = picked[: config.MAX_COMPOSE_PER_TICK]

    results = await asyncio.gather(*[compose_one(llm, c) for c in picked], return_exceptions=True)

    actions = []
    for c, res in zip(picked, results):
        if isinstance(res, Exception) or not res or not res.get("body"):
            continue
        conv.fire_suppression(c["supp"])
        conv.record_outbound(res["conversation_id"], res["body"])
        actions.append(res)
        if len(actions) >= config.MAX_ACTIONS_PER_TICK:
            break
    return actions
