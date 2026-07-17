"""Safe IFC opening utilities.

IfcOpenShell 0.8.5 has an upstream Python-wrapper edge case: when construction
of ``ifcopenshell.file`` raises for a malformed SPF file, ``wrapped_data`` has
already been assigned but the object has not yet been registered in
``file_dict``. Its destructor then tries to delete a missing cache key and emits
``PytestUnraisableExceptionWarning``.

This module checks the low-level parser status *before* constructing the public
Python wrapper. Valid files are wrapped normally; invalid files never create a
partially initialized public object. This fixes the warning at the boundary
instead of suppressing it.
"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Any


def _open_spf(path: Path) -> Any:
    import ifcopenshell
    from ifcopenshell import ifcopenshell_wrapper
    from ifcopenshell.file import (
        INVALID_SYNTAX,
        NO_HEADER,
        READ_ERROR,
        UNKNOWN,
        UNSUPPORTED_SCHEMA,
    )

    wrapped = ifcopenshell_wrapper.open(str(path.absolute()))
    if wrapped is None:
        raise OSError(f"IfcOpenShell returned no parser object for {path}")
    status = int(wrapped.good().value())
    if status == 0:
        return ifcopenshell.file(wrapped)
    if status == READ_ERROR:
        raise OSError(f"Unable to open IFC file for reading: {path}")
    if status == NO_HEADER:
        raise ifcopenshell.Error("Unable to parse IFC SPF header")
    if status == UNSUPPORTED_SCHEMA:
        raise ifcopenshell.SchemaError("Unsupported IFC schema")
    if status == INVALID_SYNTAX:
        raise ifcopenshell.Error("Syntax error during IFC parse")
    if status == UNKNOWN:
        raise ifcopenshell.Error("Unknown IFC parser status")
    raise ifcopenshell.Error(f"IFC parser failed with status {status}")


def open_ifc_safely(path: str | Path) -> Any:
    """Open an IFC/IFCZIP file without partially initialized public wrappers."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: '{p}'.")

    suffix = p.suffix.lower()
    if suffix in {".ifczip", ".zip"}:
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(p) as archive:
                candidates = [
                    name for name in archive.namelist()
                    if Path(name).suffix.lower() in {".ifc", ".ifcxml"}
                ]
                if not candidates:
                    raise LookupError(f"No .ifc or .ifcXML file found in {p}")
                info = archive.getinfo(candidates[0])
                if info.file_size > 200 * 1024 * 1024:
                    raise ValueError("Uncompressed IFCZIP member exceeds 200 MB safety limit")
                # Do not use ZipFile.extract on user-controlled member names;
                # write to a fixed basename to prevent path traversal.
                extracted = Path(temp_dir) / Path(candidates[0]).name
                extracted.write_bytes(archive.read(info))
                return open_ifc_safely(extracted)

    if suffix == ".ifcxml":
        # The destructor defect is specific to the SPF wrapper construction
        # path. Keep the official XML parser for IFCXML.
        import ifcopenshell
        return ifcopenshell.open(p)

    return _open_spf(p)
