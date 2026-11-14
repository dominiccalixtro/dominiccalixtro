"""Exchange adapters.

Broker is the only thing the bot needs to trade. PaperBroker simulates fills;
CoinsPhClient talks to the real Coins.ph API; ShadowBroker simulates fills but
asks Coins.ph to validate every order first.
"""

from typing import Protocol

from ..models import Fill, SymbolRules
from decimal import Decimal


class Broker(Protocol):
    def market_buy(self, rules: SymbolRules, quote_amount: Decimal) -> Fill: ...

    def market_sell(self, rules: SymbolRules, base_qty: Decimal) -> Fill: ...
