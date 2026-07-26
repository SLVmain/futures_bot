from collections.abc import MutableMapping

from models.trade import TradeProposal


PROPOSAL_KEY = "trade_proposal"


class ProposalError(Exception):
    pass


class ProposalNotFound(ProposalError):
    pass


class ProposalMismatch(ProposalError):
    pass


class ProposalExpired(ProposalError):
    pass


class ProposalService:
    @staticmethod
    def store(
        user_data: MutableMapping,
        proposal: TradeProposal,
    ) -> None:
        user_data[PROPOSAL_KEY] = proposal

    @staticmethod
    def discard(user_data: MutableMapping) -> None:
        user_data.pop(PROPOSAL_KEY, None)

    @staticmethod
    def consume(
        user_data: MutableMapping,
        proposal_id: str,
        now: float | None = None,
    ) -> TradeProposal:
        proposal = user_data.get(PROPOSAL_KEY)
        if not isinstance(proposal, TradeProposal):
            raise ProposalNotFound(
                "Предложение сделки не найдено или уже использовано"
            )
        if proposal.proposal_id != proposal_id:
            raise ProposalMismatch(
                "Кнопка относится к другому предложению сделки"
            )
        if proposal.is_expired(now):
            user_data.pop(PROPOSAL_KEY, None)
            raise ProposalExpired(
                "Время подтверждения сделки истекло"
            )

        removed = user_data.pop(PROPOSAL_KEY, None)
        if removed is not proposal:
            raise ProposalNotFound(
                "Предложение сделки уже используется"
            )
        return proposal
