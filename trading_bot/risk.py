from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Union

from .models import Rejection, RiskLimits, Side, TradeProposal, ValidatedTrade


RiskDecision = Union[ValidatedTrade, Rejection]


@dataclass
class RiskEngine:
    limits: RiskLimits = RiskLimits()

    def validate(self, proposal: TradeProposal) -> RiskDecision:
        numeric_values = (proposal.entry_price, proposal.stop_loss, proposal.take_profit, proposal.account_equity, proposal.leverage)
        if not all(isfinite(value) for value in numeric_values):
            return Rejection("invalid_numbers", "Trade contains a non-finite numeric value.")
        if proposal.account_equity <= 0:
            return Rejection("invalid_equity", "Account equity must be greater than zero.")
        if proposal.entry_price <= 0 or proposal.stop_loss <= 0 or proposal.take_profit <= 0:
            return Rejection("invalid_price", "Entry, stop-loss, and take-profit must be positive.")
        if proposal.leverage <= 0 or proposal.leverage > self.limits.max_leverage:
            return Rejection("leverage_limit", "Requested leverage exceeds the configured hard limit.")
        if proposal.open_positions >= self.limits.max_open_positions:
            return Rejection("position_limit", "Maximum open-position count has been reached.")

        if proposal.side is Side.LONG:
            stop_distance = proposal.entry_price - proposal.stop_loss
            reward_distance = proposal.take_profit - proposal.entry_price
        else:
            stop_distance = proposal.stop_loss - proposal.entry_price
            reward_distance = proposal.entry_price - proposal.take_profit

        if stop_distance <= 0:
            return Rejection("invalid_stop", "The stop-loss is on the wrong side of the proposed entry.")
        if reward_distance <= 0:
            return Rejection("invalid_target", "The take-profit is on the wrong side of the proposed entry.")

        daily_loss_limit = proposal.account_equity * self.limits.max_daily_loss_percent / 100
        if proposal.current_daily_loss >= daily_loss_limit:
            return Rejection("daily_loss_limit", "The configured daily loss limit has been reached.")

        risk_budget = proposal.account_equity * self.limits.risk_per_trade_percent / 100
        risk_per_unit = stop_distance + proposal.estimated_fee + proposal.estimated_slippage
        quantity = risk_budget / risk_per_unit
        notional = quantity * proposal.entry_price
        notional_limit = proposal.account_equity * self.limits.max_notional_percent / 100
        if notional > notional_limit:
            quantity = notional_limit / proposal.entry_price
            notional = quantity * proposal.entry_price

        maximum_loss = quantity * risk_per_unit
        risk_reward_ratio = reward_distance / risk_per_unit
        return ValidatedTrade(proposal, quantity, notional, maximum_loss, risk_reward_ratio)
