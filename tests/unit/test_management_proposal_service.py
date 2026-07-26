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
