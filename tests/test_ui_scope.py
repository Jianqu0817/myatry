"""The standalone viewer must not discover a neighbouring private project."""
from myatry.context_viewer import libraries


def test_only_this_project_library_is_visible(tmp_path):
    project = tmp_path / "myatry"
    own = project / "var/groups" / ("a" * 32)
    neighbour = tmp_path / "MYATRI/var/xiaowennuan"
    for folder in (own, neighbour):
        (folder / "domains").mkdir(parents=True)
        (folder / "runtime.sqlite3").touch()
    found = libraries(project)
    assert len(found) == 1
    assert next(iter(found.values()))[0] == own
