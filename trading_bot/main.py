from __future__ import annotations

from .exchanges.paper import PaperExchange
from .models import RiskLimits, Side, TradeProposal
from .risk import RiskEngine


def main() -> None:
    proposal = TradeProposal(
        exchange="paper",
        symbol="BTCUSDT",
        side=Side.LONG,
        entry_price=100_000,
        stop_loss=98_500,
        take_profit=103_000,
        account_equity=10_000,
        estimated_fee=2,
        estimated_slippage=3,
    )
    decision = RiskEngine(RiskLimits()).validate(proposal)
    if hasattr(decision, "reason"):
        print(f"REJECTED [{decision.code}]: {decision.reason}")
        return

    order = PaperExchange().submit_paper_order(proposal, decision.quantity)
    print("PAPER MODE - no real exchange order was submitted.")
    print(f"Order: {order.order_id} {order.side.value} {order.quantity:.6f} {order.symbol}")
    print(f"Maximum loss: {decision.maximum_loss:.2f}")
    print(f"Risk/reward: {decision.risk_reward_ratio:.2f}")


if __name__ == "__main__":
    main()
