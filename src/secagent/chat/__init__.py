"""UC101: Mattermost chat-ops front end (``secagent chat serve``).

secagent's own transport — a small FastAPI receiver for Mattermost slash-command and
outgoing-webhook deliveries — not the ``pi-mattermost`` plugin. Mentions/DMs/slash
commands route through :func:`dispatch_chat_event` to the same engines UC0/UC100
already use (``agents.review``, the affordance API), reply in-thread as the
``secagent`` bot, and are audited via ``audit.AuditLogger.record_chat`` with the
invoking Mattermost user's own identity — distinct from the bot's service principal.
"""

from .mattermost import MattermostClient, MattermostError
from .router import ChatRequest, MattermostAuthError, dispatch_chat_event

__all__ = [
    "ChatRequest",
    "MattermostAuthError",
    "MattermostClient",
    "MattermostError",
    "dispatch_chat_event",
]
