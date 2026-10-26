from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import PaperOrder, TradeProposal


class ExchangeAdapter(ABC):
    """Common boundary for Bybit, Binance, demo, testnet, and paper adapters."""

    name: str

    @abstractmethod
    def get_ticker(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def submit_paper_order(self, proposal: TradeProposal, quantity: float) -> PaperOrder:
        raise NotImplementedError
