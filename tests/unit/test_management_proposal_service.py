import pytest

from models.management import ManagementAction, ManagementProposal
from services.management_proposal_service import (
    ManagementProposalError,
    ManagementProposalService,
)


def test_management_proposal_can_only_be_consumed_once():
    user_data = {}
    proposal = ManagementProposal.create(
        ManagementAction.CANCEL_ORDER,
        "order-1",
        "BTCUSDT",
        now=100,
    )
    ManagementProposalService.store(user_data, proposal)

    assert (
        ManagementProposalService.consume(
            user_data,
            proposal.proposal_id,
            now=101,
        )
        == proposal
    )
    with pytest.raises(ManagementProposalError, match="уже использована"):
        ManagementProposalService.consume(
            user_data,
            proposal.proposal_id,
            now=102,
        )


def test_expired_management_proposal_is_rejected():
    user_data = {}
    proposal = ManagementProposal.create(
        ManagementAction.CLOSE_POSITION,
        "position-1",
        ttl_seconds=5,
        now=100,
    )
    ManagementProposalService.store(user_data, proposal)

    with pytest.raises(ManagementProposalError, match="истекло"):
        ManagementProposalService.consume(
            user_data,
            proposal.proposal_id,
            now=105,
        )


def test_signal_action_proposal_keeps_all_targets():
    proposal = ManagementProposal.create_signal_action(
        ManagementAction.CLOSE_AND_CANCEL,
        "PUMPUSDT",
        order_ids=("order-1", "order-2"),
        position_ids=("position-1",),
        signal_event_type="SIGNAL_CLOSE_OPPOSITE",
        source_event_id="telegram:1:10",
        now=100,
    )

    assert proposal.target_id == "position-1"
    assert proposal.order_ids == ("order-1", "order-2")
    assert proposal.position_ids == ("position-1",)
    assert proposal.expires_at == 400
