import pytest

from models.signal import OrderSide
from models.trade import (
    PlannedTakeProfit,
    TradePlan,
    TradeProposal,
)
from services.proposal_service import (
    ProposalExpired,
    ProposalMismatch,
    ProposalNotFound,
    ProposalService,
)


def make_plan(symbol="BTCUSDT"):
    return TradePlan(
        symbol=symbol,
        side=OrderSide.LONG,
        entry_min=49,
        entry_max=51,
        current_price=50,
        in_range=True,
        total_quantity=2,
        stop_loss=45,
        take_profits=(
            PlannedTakeProfit(price=55, quantity=2),
        ),
        leverage=10,
        risk_percent=1,
        risk_budget=10,
    )


def test_proposals_are_isolated_between_users():
    first_user = {}
    second_user = {}
    first = TradeProposal.create(make_plan("BTCUSDT"), now=100)
    second = TradeProposal.create(make_plan("ETHUSDT"), now=100)
    ProposalService.store(first_user, first)
    ProposalService.store(second_user, second)

    consumed = ProposalService.consume(
        first_user,
        first.proposal_id,
        now=101,
    )

    assert consumed.plan.symbol == "BTCUSDT"
    assert second_user["trade_proposal"].plan.symbol == "ETHUSDT"


def test_proposal_can_be_consumed_only_once():
    user_data = {}
    proposal = TradeProposal.create(make_plan(), now=100)
    ProposalService.store(user_data, proposal)

    ProposalService.consume(user_data, proposal.proposal_id, now=101)

    with pytest.raises(ProposalNotFound):
        ProposalService.consume(
            user_data,
            proposal.proposal_id,
            now=101,
        )


def test_wrong_button_does_not_remove_valid_proposal():
    user_data = {}
    proposal = TradeProposal.create(make_plan(), now=100)
    ProposalService.store(user_data, proposal)

    with pytest.raises(ProposalMismatch):
        ProposalService.consume(user_data, "wrong-id", now=101)

    assert user_data["trade_proposal"] is proposal


def test_expired_proposal_is_removed():
    user_data = {}
    proposal = TradeProposal.create(
        make_plan(),
        ttl_seconds=300,
        now=100,
    )
    ProposalService.store(user_data, proposal)

    with pytest.raises(ProposalExpired):
        ProposalService.consume(
            user_data,
            proposal.proposal_id,
            now=400,
        )

    assert user_data == {}
