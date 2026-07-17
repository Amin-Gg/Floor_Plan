import zipfile
from pathlib import Path

from ingest.ifc_io import open_ifc_safely


FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_plan.ifc"


def test_ifczip_member_name_cannot_escape_temp_directory(tmp_path):
    archive_path = tmp_path / "plan.ifczip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../nested/plan.ifc", FIXTURE.read_bytes())
    model = open_ifc_safely(archive_path)
    assert model.by_type("IfcProject")
    assert not (tmp_path / "nested" / "plan.ifc").exists()
