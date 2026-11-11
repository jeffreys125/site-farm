"""Provider-agnostic OpenAI-compatible chat client.

Works with the official DeepSeek API and with self-hosted gateways
(New API / one-api / vLLM / Ollama ...). Reasoning models are handled by
turning thinking off when configured, and by failing with an actionable
message instead of a bare KeyError when a model returns only reasoning
content.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

from .config import LLMConfig, THINKING_MODEL_HINTS


class LLMError(RuntimeError):
    pass


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _strip_think_blocks(text: str) -> str:
    """Some upstreams inline <think>...</think> into `content` instead of
    using a separate `reasoning_content` field."""
    if "<think" not in text.lower():
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text).strip()
    return cleaned or text


class LLM:
    def __init__(self, cfg: LLMConfig):
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

    # ------------------------------------------------------------ internals
    def thinking_off(self) -> bool:
        """Whether to ask the upstream to disable reasoning."""
        mode = (self.cfg.thinking or "on").lower()
        if mode == "off":
            return True
        if mode == "on":
            return False
        name = (self.cfg.model or "").lower()
        return any(hint in name for hint in THINKING_MODEL_HINTS)

    def _payload(
        self,
        messages: list[dict],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.thinking_off():
            # Two spellings, so either a vLLM/Qwen3-style endpoint or an
            # OpenAI-style gateway can honour it. Neither is sent for the
            # default DeepSeek preset (thinking=on), which keeps that request
            # byte-for-byte compatible with the official API.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_effort"] = "none"
        return payload

    @staticmethod
    def _extract_content(data: dict) -> str:
        """Pull `content` out of a response, with a useful error if missing.

        A reasoning model that burns its whole token budget on thinking can
        return only `reasoning_content` (no `content` at all). The old code
        raised a bare KeyError('content'); we now explain what to change.
        """
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式异常，缺少 choices[0].message：{str(data)[:400]}") from exc

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return _strip_think_blocks(content)

        reasoning = message.get("reasoning_content") or ""
        finish = choice.get("finish_reason")
        hint = ""
        if reasoning or finish == "length":
            hint = (
                " 该模型只返回了思考内容：思考耗尽了 max_tokens，或没有关闭思考模式。"
                "请在 .env 里设置 LLM_THINKING=off（自建 New API 用 "
                "LLM_PROVIDER=newapi 会自动关闭），必要时调大 LLM_MAX_TOKENS。"
            )
        raise LLMError(
            f"模型响应里没有正文 content（finish_reason={finish}, "
            f"reasoning_content 长度={len(reasoning)}）。{hint}"
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

        payload = self._payload(messages, temperature, max_tokens, json_mode)

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
                    return self._extract_content(r.json())
                # Not every OpenAI-compatible provider implements
                # response_format=json_object; several answer 400 instead of
                # ignoring it. Drop the hint and let _parse_json clean up.
                if r.status_code == 400 and json_mode:
                    json_mode = False
                    payload.pop("response_format", None)
                    last_err = f"HTTP 400 with json mode, retrying without it: {r.text[:200]}"
                    continue
                if r.status_code in (401, 403):
                    raise LLMError(
                        f"HTTP {r.status_code} 认证失败，请检查 LLM_API_KEY / "
                        f"DEEPSEEK_API_KEY：{r.text[:300]}"
                    )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    time.sleep((2 ** attempt) + random.random())
                    continue
                raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
            except requests.RequestException as exc:
                last_err = str(exc)
                time.sleep((2 ** attempt) + random.random())
        raise LLMError(
            f"LLM 调用失败（{self.cfg.provider}/{self.cfg.model}，"
            f"{retries} 次重试后）：{last_err}"
        )

    # -------------------------------------------------------------- helpers
    def json_object(self, system: str, user: str, **kwargs) -> dict:
        raw = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
            **kwargs,
        )
        return _parse_json(raw)

    # ---------------------------------------------------------------- probe
    def probe(self) -> dict[str, Any]:
        """One tiny real request, used by `python run.py llm-check`."""
        if self.cfg.mock:
            return {"ok": False, "error": "没有配置 API key（当前是 mock 模式）"}
        payload = self._payload(
            [{"role": "user", "content": 'Reply with exactly this JSON: {"ok":true}'}],
            temperature=0,
            max_tokens=64,
            json_mode=True,
        )
        t0 = time.time()
        try:
            r = self.session.post(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                json=payload,
                timeout=60,
            )
        except requests.RequestException as exc:
            return {"ok": False, "error": f"网络错误：{exc}"}
        elapsed = round(time.time() - t0, 2)
        try:
            data = r.json()
        except ValueError:
            return {"ok": False, "http": r.status_code, "elapsed": elapsed,
                    "error": r.text[:300]}

        out: dict[str, Any] = {
            "ok": False,
            "http": r.status_code,
            "elapsed": elapsed,
            "model": data.get("model") or self.cfg.model,
            "usage": data.get("usage") or {},
            "thinking_off": self.thinking_off(),
        }
        if r.status_code != 200:
            out["error"] = str(data.get("error") or r.text[:300])[:300]
            return out
        try:
            content = self._extract_content(data)
        except LLMError as exc:
            out["error"] = str(exc)
            return out
        message = (data.get("choices") or [{}])[0].get("message") or {}
        out["reasoning_content"] = bool(message.get("reasoning_content"))
        try:
            json.loads(content)
            out["json_ok"] = True
        except (ValueError, TypeError):
            out["json_ok"] = False
        out["content"] = content[:120]
        out["ok"] = bool(content)
        return out


# Old name kept so `from .llm import DeepSeek` still works.
DeepSeek = LLM


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
pipeline can be exercised end to end before you plug in a real LLM key.</p>
<h2>Why this section exists</h2>
<p>The generator normally receives a structured brief, researches the topic and
returns a long-form JSON payload. In mock mode we emit a short placeholder so
that publishing, sitemaps and IndexNow submission can still be verified.</p>
<h2>What to check</h2>
<ul><li>The slug is unique for this site.</li><li>The WordPress endpoint accepts the payload.</li>
<li>The sitemap picks the post up after publishing.</li></ul>
<h2>Next step</h2>
<p>Set <code>LLM_API_KEY</code> (or <code>DEEPSEEK_API_KEY</code>) in the
environment and re-run without <code>--mock</code> to generate production copy.</p>
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
