"""The Claude brain: one API call (plus continuations) per thought.

Claude reads the briefing, may run a couple of web searches, and answers by
calling the strict `submit_decision` tool. This module also prices every
call so the bot can pay for its own thinking.

Needs the `anthropic` package and an API key (ANTHROPIC_API_KEY) from the
Claude Console. The API is billed separately from a Claude app subscription.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import BotConfig
from .prompts import decision_tool, system_prompt

log = logging.getLogger("mortalbot.brain")

# USD per million tokens: (input, output, cache read). 5-minute cache writes bill at 1.25x input.
# Check https://www.anthropic.com/pricing if you change models; unknown models are priced as Opus 5.5.
PRICES = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("0.20")),
    "claude-opus-5": (Decimal("5"), Decimal("25"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10"), Decimal("0.10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("0.20")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
    "claude-fable-5-2": (Decimal("8"), Decimal("40"), Decimal("0.20")),
}
WEB_SEARCH_USD = Decimal("0.01")  # $10 per 1,000 searches
MAX_REQUESTS = 4  # per thought: first call + continuations


class BrainUnavailable(Exception):
    """The API could not be reached or refused the request. `fatal` means retrying won't help."""

    def __init__(self, message: str, fatal: bool = False):
        self.fatal = fatal
        super().__init__(message)


@dataclass
class Thought:
    decision: dict | None
    cost_usd: Decimal
    note: str = "ok"
    searches: int = 0
    tokens: dict = field(default_factory=dict)


def price_usage(model: str, usage) -> tuple[Decimal, int, dict]:
    inp, out, cache_read = PRICES.get(model, PRICES["claude-opus-5-5"])

    def n(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    tokens = {"in": n("input_tokens"), "out": n("output_tokens"), "cache_write": n("cache_creation_input_tokens"),
              "cache_read": n("cache_read_input_tokens")}
    searches = int(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0)
    cost = (tokens["in"] * inp + tokens["cache_write"] * inp * Decimal("1.25") + tokens["cache_read"] * cache_read
            + tokens["out"] * out) / Decimal(1_000_000) + searches * WEB_SEARCH_USD
    return cost, searches, tokens


def web_search_tool(model: str, max_uses: int) -> dict:
    # Dynamic-filtering web search needs a current Opus/Sonnet/Fable model; other models get the basic tool.
    kind = "web_search_20250305" if model.startswith("claude-haiku") else "web_search_20260209"
    return {"type": kind, "name": "web_search", "max_uses": max_uses}


class ClaudeBrain:
    def __init__(self, cfg: BotConfig, client=None):
        self.cfg = cfg
        self.model = cfg.agent.model
        self.system = system_prompt(cfg)
        self._client = client
        if self.model not in PRICES:
            log.warning("no price table for %s; costs will be estimated at Opus 5.5 rates", self.model)

    def client(self):
        if self._client is None:
            import anthropic  # imported lazily: the rules brain and backtests don't need it

            self._client = anthropic.Anthropic(max_retries=3, timeout=300.0)
        return self._client

    def _create(self, tools: list, messages: list):
        try:
            return self.client().beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # if the model declines, the API retries on its default fallback model
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                tool_choice={"type": "auto"},
                output_config={"effort": self.cfg.agent.effort},
                messages=messages,
            )
        except Exception as e:
            raise self._translate(e) from e

    def _translate(self, e: Exception) -> Exception:
        """Map SDK errors (most specific first) to BrainUnavailable; anything else is a real bug."""
        try:
            import anthropic
        except ImportError:
            return e
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return BrainUnavailable(f"Claude API rejected the key: {e}", fatal=True)
        if isinstance(e, anthropic.NotFoundError):
            return BrainUnavailable(f"unknown model {self.model!r}: {e}", fatal=True)
        if isinstance(e, anthropic.BadRequestError):
            return BrainUnavailable(f"Claude API rejected the request: {e}", fatal=True)
        if isinstance(e, anthropic.RateLimitError):
            return BrainUnavailable(f"rate limited: {e}")
        if isinstance(e, anthropic.APIStatusError):
            return BrainUnavailable(f"Claude API error {e.status_code}: {e}", fatal=e.status_code < 500)
        if isinstance(e, anthropic.APIConnectionError):
            return BrainUnavailable(f"cannot reach the Claude API: {e}")
        return e

    def think(self, briefing: str, symbols: Sequence[str]) -> Thought:
        tools = [decision_tool(symbols, self.cfg)]
        if self.cfg.agent.web_search_max_uses > 0:
            tools.append(web_search_tool(self.model, self.cfg.agent.web_search_max_uses))
        messages: list = [{"role": "user", "content": briefing}]
        total, searches, tokens = Decimal("0"), 0, {}
        nudged = False
        for _ in range(MAX_REQUESTS):
            resp = self._create(tools, messages)
            cost, n, t = price_usage(self.model, resp.usage)
            total += cost
            searches += n
            tokens = {k: tokens.get(k, 0) + v for k, v in t.items()}
            if resp.stop_reason == "refusal":
                details = getattr(resp, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                return Thought(None, total, f"declined to decide (refusal: {category})", searches, tokens)
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_decision":
                    decision = block.input if isinstance(block.input, dict) else {}
                    return Thought(dict(decision), total, "ok", searches, tokens)
            if resp.stop_reason == "max_tokens":
                return Thought(None, total, "ran out of output tokens before deciding", searches, tokens)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue  # a long server-side search loop paused; resending resumes it
            if nudged:
                break
            messages.append({"role": "user", "content": "Call submit_decision now with your decision."})
            nudged = True
        return Thought(None, total, "finished without calling submit_decision", searches, tokens)
