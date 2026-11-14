"""MortalBot: a trading bot whose budget is its life.

Give it a budget (default 5,000 PHP). It trades to survive. If its equity
falls to its death floor, or below the smallest order the exchange accepts,
it sells what it holds, writes its obituary, and never trades again.
"""

__version__ = "0.1.0"
