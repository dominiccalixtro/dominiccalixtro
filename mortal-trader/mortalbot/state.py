"""The bot's memory: its own ledger, saved to disk after every change.

The ledger, not the exchange balance, is the bot's life. If your Coins.ph
account holds more than the bot's budget, the bot still only spends its own
money. A dead bot's state file stays dead: a new life needs a new name.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

_DECIMALS = ("budget", "cash", "position_qty", "position_cost", "entry_price", "stop_price",
             "peak_equity", "last_equity", "fees_paid", "dust_qty")


class BotIsDead(Exception):
    def __init__(self, state: "BotState"):
        self.state = state
        super().__init__(f"{state.name} is dead ({state.cause_of_death}). A new life needs a new name.")


class StateMismatch(Exception):
    pass


@dataclass
class BotState:
    name: str
    mode: str
    symbol: str
    budget: Decimal
    born_at: int
    cash: Decimal
    position_qty: Decimal = Decimal("0")
    position_cost: Decimal = Decimal("0")  # pesos paid for the open position, fees included
    entry_price: Decimal | None = None
    entry_time: int | None = None
    stop_price: Decimal | None = None
    peak_equity: Decimal = Decimal("0")
    last_equity: Decimal = Decimal("0")
    health: str = "HEALTHY"
    alive: bool = True
    died_at: int | None = None
    cause_of_death: str | None = None
    consecutive_losses: int = 0
    rest_candles_left: int = 0
    last_candle_close_time: int = 0
    fees_paid: Decimal = Decimal("0")
    dust_qty: Decimal = Decimal("0")
    trades: list[dict] = field(default_factory=list)

    @classmethod
    def newborn(cls, name: str, mode: str, symbol: str, budget: Decimal, now_ms: int) -> "BotState":
        return cls(name=name, mode=mode, symbol=symbol, budget=budget, born_at=now_ms, cash=budget,
                   peak_equity=budget, last_equity=budget)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in _DECIMALS:
            if d[k] is not None:
                d[k] = str(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BotState":
        d = dict(d)
        for k in _DECIMALS:
            if d.get(k) is not None:
                d[k] = Decimal(d[k])
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
            json.dump(state.to_dict(), fh, indent=2)
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
        raise StateMismatch(f"{name} trades {state.symbol}, but the config says {symbol}. Use a new name.")
    return state, False
