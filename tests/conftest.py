"""Test-session environment.

``stocks_on_the_move.momentum`` reads its configuration from environment
variables at import time and creates the candle-cache directory as a side
effect, so these have to be set before the module is first imported.
"""

import os
import tempfile

# Keep the test run away from the real .cache_candles/ directory.
os.environ.setdefault("CACHE_DIR", tempfile.mkdtemp(prefix="sotm-cache-"))
# Belt and braces: never let a test reach the broker.
os.environ.setdefault("ALLOW_KITE_EXECUTION", "0")

# Never touch the real Kite session cache, never bind the real redirect port,
# never open a browser (ADR-005). Tests pass explicit settings; these guard the defaults.
os.environ["KITE_SESSION_FILE"] = os.path.join(tempfile.mkdtemp(prefix="sotm-session-"), "kite_session.json")
os.environ["KITE_REDIRECT_PORT"] = "0"
os.environ["KITE_OPEN_BROWSER"] = "0"
# PyCharm sets this in its run console, where kite_auth then treats a pipe as a terminal.
os.environ.pop("PYCHARM_HOSTED", None)
