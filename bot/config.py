"""Runtime configuration. Everything is overridable via environment variables so
the same image runs locally and on any host (Render, Fly, Railway or ngrok)."""
import os
from datetime import datetime, timezone

# --- Team / metadata (shown at GET /v1/metadata; keep in sync with the portal) ---
TEAM_NAME = os.getenv("VERA_TEAM_NAME", "Aditya - magicpin Vera Challenge")
TEAM_MEMBERS = [m.strip() for m in os.getenv("VERA_TEAM_MEMBERS", "Aditya").split(",") if m.strip()]
CONTACT_EMAIL = os.getenv("VERA_CONTACT_EMAIL", "aditya.sps25@gmail.com")
REPO_URL = os.getenv("VERA_REPO_URL", "https://github.com/AdityaSingh-25/magicpin-vera-bot")
VERSION = os.getenv("VERA_VERSION", "1.0.0")
APPROACH = os.getenv(
    "VERA_APPROACH",
    "Trigger-routed composer: each trigger kind maps to a message archetype, composed by "
    "an LLM under strict context-grounding rules with a deterministic template fallback. "
    "A heuristic reply state machine handles auto-reply, intent and hostile detection then "
    "decides send, wait or end.",
)

# --- LLM (OpenAI-compatible). If no key is set, the bot runs fully on deterministic templates. ---
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("VERA_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = float(os.getenv("VERA_LLM_TIMEOUT", "9"))
LLM_MAX_TOKENS = int(os.getenv("VERA_LLM_MAX_TOKENS", "700"))

# --- Budgets (the local judge enforces tick/reply 15s; we aim well under) ---
TICK_DEADLINE = float(os.getenv("VERA_TICK_DEADLINE", "12"))
REPLY_DEADLINE = float(os.getenv("VERA_REPLY_DEADLINE", "12"))
MAX_COMPOSE_PER_TICK = int(os.getenv("VERA_MAX_COMPOSE", "6"))
MAX_ACTIONS_PER_TICK = int(os.getenv("VERA_MAX_ACTIONS", "20"))

SUBMITTED_AT = os.getenv("VERA_SUBMITTED_AT", datetime.now(timezone.utc).isoformat())
