"""The bot's memory: its own ledger and journal, saved to disk after every change.

The ledger, not the exchange balance, is the bot's life. If your Coins.ph
account holds more than the bot's budget, the bot still only spends its own
money. A dead bot's state file stays dead: a new life needs a new name.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from pathlib import Path

STATE_VERSION = 2


class BotIsDead(Exception):
    def __init__(self, state: "BotState"):
        self.state = state
        super().__init__(f"{state.name} is dead ({state.cause_of_death}). A new life needs a new name.")


class StateMismatch(Exception):
    pass


@dataclass
class Position:
    symbol: str
    qty: Decimal
    cost: Decimal  # pesos paid, fees included
    entry_price: Decimal
    entry_time: int
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None
    thesis: str = ""  # why it was bought

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        dec = {"qty", "cost", "entry_price", "stop_price", "take_profit"}
        return cls(**{k: (Decimal(v) if k in dec and v is not None else v) for k, v in d.items()})


_DECIMALS = ("budget", "cash", "peak_equity", "last_equity", "fees_paid", "thinking_paid", "upkeep_paid",
             "think_spent_today")
_DECIMAL_MAPS = ("last_prices", "think_prices", "dust")


@dataclass
class BotState:
    name: str
    mode: str
    symbol: str  # identity: the rules brain's market, or "agent"
    budget: Decimal
    born_at: int
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    last_prices: dict[str, Decimal] = field(default_factory=dict)
    peak_equity: Decimal = Decimal("0")
    last_equity: Decimal = Decimal("0")
    health: str = "HEALTHY"
    alive: bool = True
    died_at: int | None = None
    cause_of_death: str | None = None
    consecutive_losses: int = 0
    rest_candles_left: int = 0  # rules brain
    rest_until: int = 0  # Claude brain (ms timestamp)
    last_candle_close_time: int = 0
    fees_paid: Decimal = Decimal("0")
    thinking_paid: Decimal = Decimal("0")
    upkeep_paid: Decimal = Decimal("0")
    upkeep_until: int = 0
    # Claude brain bookkeeping
    last_think_at: int = 0
    next_think_at: int = 0
    think_day: str = ""
    think_spent_today: Decimal = Decimal("0")
    buys_day: str = ""
    buys_today: int = 0
    think_prices: dict[str, Decimal] = field(default_factory=dict)
    pending_wake: list[str] = field(default_factory=list)
    regime_seen: dict[str, bool] = field(default_factory=dict)  # last known daily trend per coin (True = up)
    dust: dict[str, Decimal] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    journal: list[dict] = field(default_factory=list)
    version: int = STATE_VERSION

    @classmethod
    def newborn(cls, name: str, mode: str, symbol: str, budget: Decimal, now_ms: int) -> "BotState":
        return cls(name=name, mode=mode, symbol=symbol, budget=budget, born_at=now_ms, cash=budget,
                   peak_equity=budget, last_equity=budget, upkeep_until=now_ms)

    def to_dict(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "positions":
                v = {k: p.to_dict() for k, p in v.items()}
            elif f.name in _DECIMAL_MAPS:
                v = {k: str(x) for k, x in v.items()}
            elif isinstance(v, Decimal):
                v = str(v)
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BotState":
        d = dict(d)
        if d.get("version", 1) != STATE_VERSION:
            raise StateMismatch(f"state file is version {d.get('version', 1)}, this bot needs {STATE_VERSION}. "
                                "Start a new life with a new name.")
        for k in _DECIMALS:
            if d.get(k) is not None:
                d[k] = Decimal(d[k])
        for k in _DECIMAL_MAPS:
            d[k] = {s: Decimal(x) for s, x in d.get(k, {}).items()}
        d["positions"] = {s: Position.from_dict(p) for s, p in d.get("positions", {}).items()}
        return cls(**d)


class StateStore:
    def __init__(self, directory: str | Path, name: str, mode: str):
        self.dir = Path(directory)
        self.path = self.dir / f"{name}.{mode}.json"
        self.obituary_path = self.dir / f"{name}.{mode}.obituary.txt"

    def load(self) -> BotState | None:
        if not self.path.exists():
            return None
        with open(self.path, encoding="utf-8") as fh:
            return BotState.from_dict(json.load(fh))

    def save(self, state: BotState) -> None:
        """Write atomically so a crash mid-write can never corrupt the ledger."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def write_obituary(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.obituary_path.write_text(text + "\n", encoding="utf-8")


def awaken(store: StateStore, *, name: str, mode: str, symbol: str, budget: Decimal, now_ms: int) -> tuple[BotState, bool]:
    """Load the bot, or give birth to it. Returns (state, newly_born). Raises BotIsDead."""
    state = store.load()
    if state is None:
        state = BotState.newborn(name, mode, symbol, budget, now_ms)
        store.save(state)
        return state, True
    if not state.alive:
        raise BotIsDead(state)
    if state.symbol != symbol:
        raise StateMismatch(f"{name} was born as '{state.symbol}', but the config now says '{symbol}'. "
                            "Use a new name for a new kind of life.")
    return state, False
