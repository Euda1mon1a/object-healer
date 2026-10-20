import pytest

from object_healer import __version__, cli, floaters
from object_healer.blender import BlenderNotFound, HealResult


@pytest.fixture
def fake_blender(monkeypatch):
    monkeypatch.setattr(cli, "find_blender", lambda env=None: "/bin/blender")


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["gui"]])
def test_no_command_opens_the_window(monkeypatch, argv):
    import object_healer.gui as gui
    monkeypatch.setattr(gui, "run_gui", lambda: 0)
    assert cli.main(argv) == 0


def test_output_needs_a_single_input(fake_blender, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["heal", "a.stl", "b.stl", "-o", "c.stl"])
    assert e.value.code == 2
    assert "single IN" in capsys.readouterr().err


def test_timeout_must_be_positive(fake_blender):
    with pytest.raises(SystemExit) as e:
        cli.main(["heal", "a.stl", "--timeout", "0"])
    assert e.value.code == 2


def test_strip_floaters_without_the_extra(monkeypatch, fake_blender, capsys):
    monkeypatch.setattr(floaters, "available", lambda: False)
    assert cli.main(["heal", "--strip-floaters", "a.ply"]) == 2
    assert "object-healer[scan]" in capsys.readouterr().err


def test_no_blender(monkeypatch, capsys):
    def missing(env=None):
        raise BlenderNotFound("Blender was not found.")
    monkeypatch.setattr(cli, "find_blender", missing)
    assert cli.main(["heal", "a.stl"]) == 2
    assert "Blender was not found" in capsys.readouterr().err


def test_blender_flag_is_checked(tmp_path, capsys):
    assert cli.main(["heal", "a.stl", "--blender", str(tmp_path / "nope")]) == 2
    assert "is not an executable file" in capsys.readouterr().err


def test_heal_reports_each_file_and_exit_status(monkeypatch, fake_blender, capsys):
    calls = []

    def fake_heal(src, dst, *, blender, strip_floaters, timeout):
        calls.append((src, dst, blender, strip_floaters, timeout))
        if src == "bad.stl":
            return HealResult(False, src, reason="no mesh in the file")
        return HealResult(True, src, "/out/" + src, 1234, 2000, floaters_removed=2)

    monkeypatch.setattr(cli, "heal_file", fake_heal)
    assert cli.main(["heal", "good.stl", "bad.stl", "--timeout", "9"]) == 1
    out, err = capsys.readouterr()
    assert "healed good.stl -> /out/good.stl (1,234 vertices, 2,000 faces; removed 2" in out
    assert "failed bad.stl: no mesh in the file" in err
    assert calls[0] == ("good.stl", None, "/bin/blender", False, 9.0)


def test_heal_all_good_is_zero(monkeypatch, fake_blender):
    monkeypatch.setattr(cli, "heal_file",
                        lambda src, dst, **kw: HealResult(True, src, "x_healed.stl", 8, 12))
    assert cli.main(["heal", "x.stl", "-o", "y.stl"]) == 0
