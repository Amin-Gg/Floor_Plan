# ADR-009 — Final release consolidation and dead-file policy

## Status

Accepted.

## Context

The remediation program produced multiple phase reports, checksum sets,
acceptance outputs, OpenAPI snapshots, one-time finalizers, and duplicated
engine delivery bundles. Those files were useful while each phase was being
audited, but retaining all of them in the operational source release created
three risks:

1. developers could follow an obsolete README, Compose file, OpenAPI snapshot,
   or compatibility path;
2. generated evidence could be mistaken for runtime input;
3. future changes would have to update several copies of the same contract.

A second issue was found in runtime code: the inactive 15-class Mask2Former
registry was still used by confidence diagnostics even though the deployed
Mask R-CNN model emits only wall, window, and door IDs.

## Decision

The final source release has one current path for each responsibility.

- `README.md` is the only root project introduction.
- `FINAL_CHANGELOG_FA.md` and `FINAL_RUNBOOK_FA.md` replace per-phase root
  reports.
- Current OpenAPI snapshots use stable filenames without phase suffixes.
- Historical baselines and JUnit archives are removed; compact verified
  summaries remain under `release/evidence/`.
- One-time phase finalizers and duplicate delivery bundles are removed.
- Regression scripts that still validate active trust, semantics, packaging,
  security, and evaluation behavior are retained.
- The active primary detector registry is `config/runtime_classes.py`.
- The removed Mask2Former registry is not preserved as misleading cold code;
  future model work must arrive as a separately reviewed training subsystem.

## Dead-file criteria

A file is removed when all of the following are true:

- no production entry point imports or executes it;
- no current test or CI job depends on it;
- it duplicates a current contract, document, or release artifact; and
- deleting it does not remove required migration or backward-compatibility
  behavior.

Generated evidence is not considered production source. Only compact summaries
needed to substantiate the last verified release are retained.

## Consequences

### Positive

- one README, one Compose file, and one current OpenAPI snapshot per service;
- smaller release archive and faster review;
- less chance of using stale phase instructions;
- runtime class IDs match the actual deployed model;
- clear separation between source, external artifacts, and generated evidence.

### Trade-offs

- detailed phase-by-phase JUnit and report bundles are no longer inside the
  source ZIP;
- old phase finalizers cannot recreate historical archives from this checkout;
- the full historical packages remain available as earlier release ZIPs, not as
  nested content in the final release.

These trade-offs are intentional. The final repository is an operational and
maintainable source release, not an archive of every intermediate build.
