"""DeskTalk test suite. Run from the repo root:  python -m unittest discover -s tests -v"""

import logging

# Tests deliberately trigger error paths; keep their log output out of the test report.
logging.disable(logging.CRITICAL)
