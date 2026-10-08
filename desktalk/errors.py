"""Exception hierarchy. Everything DeskTalk raises on purpose derives from DeskTalkError."""


class DeskTalkError(Exception):
    """Base class for all DeskTalk errors."""


class ProtocolError(DeskTalkError, ValueError):
    """A message is malformed or violates the wire protocol.

    Also a ValueError so generic ``except ValueError`` handlers keep working.
    The message text is safe to show to end users.
    """


class StoreError(DeskTalkError):
    """Chat history could not be read or written."""
