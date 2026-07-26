from collections.abc import MutableMapping

from models.management import ManagementProposal


MANAGEMENT_PROPOSAL_KEY = "management_proposal"


class ManagementProposalError(Exception):
    pass


class ManagementProposalService:
    @staticmethod
    def store(
        user_data: MutableMapping,
        proposal: ManagementProposal,
    ) -> None:
        user_data[MANAGEMENT_PROPOSAL_KEY] = proposal

    @staticmethod
    def consume(
        user_data: MutableMapping,
        proposal_id: str,
        now: float | None = None,
    ) -> ManagementProposal:
        proposal = user_data.get(MANAGEMENT_PROPOSAL_KEY)
        if not isinstance(proposal, ManagementProposal):
            raise ManagementProposalError(
                "Операция не найдена или уже использована"
            )
        if proposal.proposal_id != proposal_id:
            raise ManagementProposalError(
                "Кнопка относится к другой операции"
            )
        if proposal.is_expired(now):
            user_data.pop(MANAGEMENT_PROPOSAL_KEY, None)
            raise ManagementProposalError(
                "Время подтверждения операции истекло"
            )
        removed = user_data.pop(MANAGEMENT_PROPOSAL_KEY, None)
        if removed is not proposal:
            raise ManagementProposalError(
                "Операция уже выполняется"
            )
        return proposal
