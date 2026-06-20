from coding_bridge import fs


def test_list_dir_basic(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta.txt").write_text("x")
    result = fs.list_dir(str(tmp_path))
    assert result["path"] == str(tmp_path)
    assert "error" not in result
    names = [e["name"] for e in result["entries"]]
    # Directories sort before files, then case-insensitive by name.
    assert names == ["alpha", "beta.txt"]
    assert result["entries"][0]["type"] == "dir"
    assert result["entries"][1]["type"] == "file"


def test_list_dir_hides_dotfiles_by_default(tmp_path):
    (tmp_path / ".secret").write_text("x")
    (tmp_path / "visible.txt").write_text("x")
    result = fs.list_dir(str(tmp_path))
    names = [e["name"] for e in result["entries"]]
    assert names == ["visible.txt"]


def test_list_dir_show_hidden(tmp_path):
    (tmp_path / ".secret").write_text("x")
    (tmp_path / "visible.txt").write_text("x")
    result = fs.list_dir(str(tmp_path), show_hidden=True)
    names = {e["name"] for e in result["entries"]}
    assert names == {".secret", "visible.txt"}


def test_list_dir_reports_parent(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    result = fs.list_dir(str(child))
    assert result["parent"] == str(tmp_path)


def test_list_dir_root_has_no_parent():
    result = fs.list_dir("/")
    assert result["parent"] is None


def test_list_dir_missing_path(tmp_path):
    result = fs.list_dir(str(tmp_path / "nope"))
    assert result["error"]
    assert result["entries"] == []


def test_list_dir_file_path_lists_parent(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    result = fs.list_dir(str(f))
    assert result["path"] == str(tmp_path)
    assert "file.txt" in {e["name"] for e in result["entries"]}


def test_list_dir_truncates(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_ENTRIES", 3)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x")
    result = fs.list_dir(str(tmp_path))
    assert result["truncated"] is True
    assert len(result["entries"]) == 3


def test_list_dir_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "here.txt").write_text("x")
    result = fs.list_dir(None)
    assert result["path"] == str(tmp_path)
