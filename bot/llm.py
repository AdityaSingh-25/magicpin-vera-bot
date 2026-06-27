"""Thin async LLM adapter using the OpenAI-compatible Chat Completions API.

Provider-agnostic: point LLM_BASE_URL at any OpenAI-compatible endpoint and set
LLM_API_KEY plus LLM_MODEL. If no key is configured, or the call fails or times out,
complete() returns None and callers fall back to deterministic templates. Temperature
defaults to 0 for the determinism the brief requires.
"""
import json

import httpx

from . import config


class LLM:
    def __init__(self):
        self.key = config.LLM_API_KEY
        self.model = config.LLM_MODEL
        self.base_url = config.LLM_BASE_URL.rstrip("/")

    @property
    def available(self):
        return bool(self.key)

    async def complete(self, system, prompt, max_tokens=None, temperature=0.0):
        if not self.available:
            return None
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        body = {
            "model": self.model,
            "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception:
            return None


def extract_json(text):
    """Best-effort extraction of the first JSON object from an LLM response."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None
