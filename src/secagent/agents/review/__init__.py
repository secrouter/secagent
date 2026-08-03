"""UC100: GitLab merge-request review agent.

Reviews new MRs with an initial structured comment, replies in-thread when tagged,
and reads the affordance store (UC1) to reason about cross-component impact. Behaviour
is steered by an editable persona/alignment profile (see ``persona``).
"""

from .agent import respond_to_mention, review_merge_request

__all__ = ["review_merge_request", "respond_to_mention"]
