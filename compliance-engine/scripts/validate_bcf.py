"""Validate a generated BCF XML 2.1 archive and print a JSON summary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from reporting.bcf_exporter import BcfValidationError, validate_bcf_archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bcf", type=Path, help="Path to the .bcf archive")
    parser.add_argument(
        "--ifc",
        type=Path,
        help="Optional source IFC; selected component GUIDs must exist in it",
    )
    args = parser.parse_args()
    allowed_ifc_guids = None
    if args.ifc is not None:
        from ingest.ifc_io import open_ifc_safely

        model = open_ifc_safely(args.ifc)
        allowed_ifc_guids = {
            str(getattr(entity, "GlobalId", "") or "")
            for entity in model.by_type("IfcRoot")
        }
    try:
        summary = validate_bcf_archive(
            args.bcf, allowed_ifc_guids=allowed_ifc_guids
        )
    except BcfValidationError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"valid": True, **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
