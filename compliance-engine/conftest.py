"""
Repo-root conftest.py — pins the test environment.

CRAG_ENABLED=0 keeps build_default_retriever() returning the bare Stage 1
MabhasRetriever in unit tests and regression baselines. `setdefault` (not
assignment) so an explicit `CRAG_ENABLED=1 pytest ...` run can still exercise
the wrapped path on purpose.
"""

import os

os.environ.setdefault("CRAG_ENABLED", "0")
os.environ.setdefault("GRAPH_ENABLED", "0")
