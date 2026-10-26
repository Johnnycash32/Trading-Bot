from __future__ import annotations

from itertools import count
from typing import Any

from ..models import PaperOrder, TradeProposal
from .base import ExchangeAdapter


class PaperExchange(ExchangeAdapter):
    """Deterministic adapter used until an exchange sandbox is explicitly enabled."""

    name = "paper"

    def __init__(self) -> None:
        self._orders = count(1)

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "last": None, "source": "paper-placeholder"}

    def submit_paper_order(self, proposal: TradeProposal, quantity: float) -> PaperOrder:
        return PaperOrder(
            order_id=f"PAPER-{next(self._orders):06d}",
            exchange=proposal.exchange,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=quantity,
            entry_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
        )
