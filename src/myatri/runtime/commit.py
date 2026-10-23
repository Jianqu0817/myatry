from myatri.contracts import Proposal


class Committer:
    """Cognitive tasks return proposals. Only this boundary publishes them."""
    def __init__(self, store):
        self.store = store

    def submit_proposal(self, proposal: Proposal):
        return self.store.commit(proposal.records, idempotency_key=proposal.proposal_id,
                                 expected=proposal.read_versions)

    commit_if_current = submit_proposal
