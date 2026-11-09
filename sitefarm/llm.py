"""DeepSeek chat client with retry, rate limiting and JSON coercion."""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

from .config import DeepSeekConfig


class LLMError(RuntimeError):
    pass


class DeepSeek:
    def __init__(self, cfg: DeepSeekConfig):
        self.cfg = cfg
        self._last_call = 0.0
        self._min_gap = 60.0 / max(1, cfg.requests_per_minute)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------- raw call
    def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        retries: int = 3,
    ) -> str:
        if self.cfg.mock:
            return _mock_reply(messages)

        gap = time.time() - self._last_call
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err = ""
        for attempt in range(retries):
            try:
                r = self.session.post(
                    f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    timeout=180,
                )
                self._last_call = time.time()
                if r.status_code == 200:
                    data = r.json()
                    return data["choices"][0]["message"]["content"]
                # Not every OpenAI-compatible provider implements
                # response_format=json_object; several answer 400 instead of
                # ignoring it. Drop the hint and let _parse_json clean up.
                if r.status_code == 400 and json_mode:
                    json_mode = False
                    payload.pop("response_format", None)
                    last_err = f"HTTP 400 with json mode, retrying without it: {r.text[:200]}"
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    time.sleep((2 ** attempt) + random.random())
                    continue
                raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
            except requests.RequestException as exc:
                last_err = str(exc)
                time.sleep((2 ** attempt) + random.random())
        raise LLMError(f"DeepSeek failed after {retries} attempts: {last_err}")

    # -------------------------------------------------------------- helpers
    def json_object(self, system: str, user: str, **kwargs) -> dict:
        raw = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
            **kwargs,
        )
        return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    # strip markdown fences if the model added them anyway
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidate = raw[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)  # trailing commas
            return json.loads(candidate)
    raise LLMError(f"model did not return JSON: {raw[:400]}")


# ------------------------------------------------------------------- mock
_MOCK_BODY = """
<p>This is a mock article body produced without an API key. It exists so the
pipeline can be exercised end to end before you plug in a real DeepSeek key.</p>
<h2>Why this section exists</h2>
<p>The generator normally receives a structured brief, researches the topic and
returns a long-form JSON payload. In mock mode we emit a short placeholder so
that publishing, sitemaps and IndexNow submission can still be verified.</p>
<h2>What to check</h2>
<ul><li>The slug is unique for this site.</li><li>The WordPress endpoint accepts the payload.</li>
<li>The sitemap picks the post up after publishing.</li></ul>
<h2>Next step</h2>
<p>Set <code>DEEPSEEK_API_KEY</code> in the environment and re-run without
<code>--mock</code> to generate production copy.</p>
"""


def _mock_reply(messages: list[dict]) -> str:
    user = messages[-1]["content"] if messages else ""

    # keyword-suggestion requests ask for a "keywords" array
    if '"keywords"' in user:
        seeds = []
        m = re.search(r"Seed themes:\s*(.+)", user)
        if m:
            seeds = [s.strip() for s in m.group(1).split(",") if s.strip()][:3]
        templates = [
            "how to {s} on a budget",
            "what does {s} cost in 2025",
            "best tools for {s} for beginners",
            "{s} mistakes to avoid",
            "how long does {s} take",
        ]
        out = []
        for seed in seeds or ["home repair"]:
            for tpl in templates:
                out.append(
                    {
                        "keyword": tpl.format(s=seed).lower(),
                        "intent": "commercial" if "best" in tpl or "cost" in tpl else "informational",
                        "priority": 7,
                    }
                )
        return json.dumps({"keywords": out}, ensure_ascii=False)

    kw = ""
    m = re.search(r"KEYWORD:\s*(.+)", user)
    if m:
        kw = m.group(1).strip().split("\n")[0]
    kw = kw or "Mock Topic"
    title = f"{kw.title()}: A Practical Walkthrough"
    return json.dumps(
        {
            "title": title,
            "slug": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
            "meta_description": (
                f"A practical, example-driven guide to {kw.lower()}, including the numbers, "
                "trade-offs and mistakes worth knowing before you start."
            )[:158],
            "excerpt": f"A practical guide to {kw.lower()} with concrete numbers and a checklist.",
            "tags": [kw.title(), "How To", "Buying Guide"],
            "faq": [
                {"q": f"How long does {kw.lower()} usually take?",
                 "a": "Plan for a weekend for a first attempt, less once you have the tools on hand."},
                {"q": f"What does {kw.lower()} typically cost?",
                 "a": "Budget for materials plus a 15 percent contingency for surprises."},
            ],
            "body_html": _MOCK_BODY,
            "word_count": 180,
        },
        ensure_ascii=False,
    )
