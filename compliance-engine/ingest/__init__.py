"""
ingest/ — Step 2 entry: turn a Step-1 enriched IFC into compliance verdicts.

    from ingest import run_ifc_compliance
    result, bim_data = run_ifc_compliance("plan.ifc", clauses)

The package owns the IFC→bim_data loader (B1), the confidence/review pre-pass
and honest-degradation post-pass (B2), and the Lane-2 pipeline. Step 2 reads
ONLY the IFC file; the compliance agents are not modified.
"""

from ingest.ifc_to_bim_data import ifc_to_bim_data
from ingest.review_prepass import apply_review_prepass, downgrade_flagged_findings
from ingest.ifc_pipeline import run_ifc_compliance

__all__ = [
    "ifc_to_bim_data",
    "apply_review_prepass",
    "downgrade_flagged_findings",
    "run_ifc_compliance",
]
