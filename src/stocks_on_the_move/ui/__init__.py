"""The operator console (ADR-031): a loopback web application that reads ``runs/`` and starts the command.

Nothing in this package computes a strategy number; every figure on a page
comes from a file a run wrote. The package may import the column lists, the
settings and their redacted snapshot, the ledger readers and the session
record helpers, and nothing from the rules, the pipeline, the broker or the
candle cache; ``tests/test_ui.py`` walks the imports to enforce it.
"""
