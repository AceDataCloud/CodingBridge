import base64

from coding_bridge import images

# Minimal valid 1x1 PNG; content isn't validated, only base64-decodability.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_DATA_URL = f"data:image/png;base64,{PNG_B64}"


def test_save_images_none_returns_empty(tmp_path):
    assert images.save_images(None, str(tmp_path), session_id="s1") == []


def test_save_images_empty_list(tmp_path):
    assert images.save_images([], str(tmp_path), session_id="s1") == []


def test_save_data_url_png(tmp_path):
    paths = images.save_images([PNG_DATA_URL], str(tmp_path), session_id="s1")
    assert len(paths) == 1
    from pathlib import Path

    written = Path(paths[0])
    assert written.exists()
    assert written.suffix == ".png"
    assert written.read_bytes() == base64.b64decode(PNG_B64)
    # Stored under <cwd>/.tmp/images/<session>-<ts>/.
    assert ".tmp/images/" in paths[0].replace("\\", "/")
    assert str(tmp_path) in paths[0]


def test_save_raw_base64_defaults_to_png(tmp_path):
    paths = images.save_images([PNG_B64], str(tmp_path), session_id="s1")
    assert len(paths) == 1
    assert paths[0].endswith(".png")


def test_save_dict_with_name(tmp_path):
    paths = images.save_images(
        [{"data": PNG_DATA_URL, "name": "pic.jpg"}], str(tmp_path), session_id="s1"
    )
    assert len(paths) == 1
    assert paths[0].endswith("pic.jpg")


def test_save_dict_with_media_type(tmp_path):
    paths = images.save_images(
        [{"base64": PNG_B64, "media_type": "image/webp"}], str(tmp_path), session_id="s1"
    )
    assert len(paths) == 1
    assert paths[0].endswith(".webp")


def test_path_traversal_name_is_stripped(tmp_path):
    paths = images.save_images(
        [{"data": PNG_DATA_URL, "name": "../../evil"}], str(tmp_path), session_id="s1"
    )
    assert len(paths) == 1
    from pathlib import Path

    written = Path(paths[0])
    assert written.name == "evil.png"
    # The file must live under the cwd, never above it.
    assert str(tmp_path) in str(written.resolve())


def test_oversized_image_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 4)
    big = base64.b64encode(b"\x00" * 16).decode()
    assert images.save_images([big], str(tmp_path), session_id="s1") == []


def test_invalid_entries_skipped(tmp_path):
    paths = images.save_images(
        [123, "   ", {"name": "no-data.png"}, PNG_DATA_URL],
        str(tmp_path),
        session_id="s1",
    )
    # Only the last valid data-URL is written.
    assert len(paths) == 1
    assert paths[0].endswith(".png")
