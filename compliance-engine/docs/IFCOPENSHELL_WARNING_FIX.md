# IfcOpenShell Malformed-File Warning Fix

## Symptom

With IfcOpenShell 0.8.5, corrupt SPF test files produced:

```text
PytestUnraisableExceptionWarning
KeyError in ifcopenshell.file.__del__
```

## Root cause

The public `ifcopenshell.file` constructor assigns `wrapped_data`, detects that
the low-level parser status is invalid and raises before registering the object
in IfcOpenShell's Python `file_dict` cache. During garbage collection,
`file.__del__` tries to delete the missing cache key.

This was an upstream wrapper lifecycle edge case, not a leaked valid model.

## Fix

`ingest/ifc_io.py` now:

1. calls the low-level parser;
2. inspects `wrapped.good()`;
3. raises a normal parser exception on invalid status;
4. constructs the public `ifcopenshell.file` wrapper only after success.

No warning filter, monkeypatch or test suppression is used.

Both the schema validator and direct IFC ingest use `open_ifc_safely()`.

## Verification

The complete test suite is run with:

```bash
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

Malformed IFC tests pass with no unraisable warnings.
