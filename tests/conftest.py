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
