"""DeskTalk - LAN real-time chat. Pure standard library."""

import logging

__version__ = "1.1.0"

# Library code only emits log records; the entry points decide where they go
# (see desktalk.log.setup_logging).
logging.getLogger("desktalk").addHandler(logging.NullHandler())
