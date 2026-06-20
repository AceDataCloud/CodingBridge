import httpx
import respx

from coding_bridge import attachments


@respx.mock
def test_save_cdn_file_attachment(tmp_path):
    respx.get("https://cdn.acedata.cloud/report.pdf").mock(
        return_value=httpx.Response(
            200, content=b"pdf", headers={"content-type": "application/pdf"}
        )
    )
    files = attachments.save_attachments(
        [{"type": "file", "url": "https://cdn.acedata.cloud/report.pdf", "name": "report.pdf"}],
        str(tmp_path),
        session_id="s1",
    )
    assert len(files) == 1
    assert files[0]["kind"] == "file"
    assert files[0]["path"].endswith("report.pdf")
    assert ".tmp/attachments/" in files[0]["path"].replace("\\", "/")


@respx.mock
def test_save_cdn_image_attachment(tmp_path):
    respx.get("https://platform.cdn.acedata.cloud/pic.png").mock(
        return_value=httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
    )
    files = attachments.save_attachments(
        [{"type": "image", "url": "https://platform.cdn.acedata.cloud/pic.png"}],
        str(tmp_path),
        session_id="s1",
    )
    assert len(files) == 1
    assert files[0]["kind"] == "image"
    assert attachments.image_paths(files) == [files[0]["path"]]


@respx.mock
def test_rejects_non_cdn_url(tmp_path):
    files = attachments.save_attachments(
        [{"url": "https://example.com/report.pdf"}], str(tmp_path), session_id="s1"
    )
    assert files == []


@respx.mock
def test_rejects_oversized_header(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 3)
    respx.get("https://cdn.acedata.cloud/big.bin").mock(
        return_value=httpx.Response(200, content=b"1234", headers={"content-length": "4"})
    )
    files = attachments.save_attachments(
        [{"url": "https://cdn.acedata.cloud/big.bin"}], str(tmp_path), session_id="s1"
    )
    assert files == []


def test_attachment_note_lists_legacy_images_and_files():
    prompt = attachments.attachment_note(
        "check this",
        [{"kind": "file", "name": "report.pdf", "path": "/tmp/report.pdf"}],
        ["/tmp/image.png"],
    )
    assert "check this" in prompt
    assert "image 1: /tmp/image.png" in prompt
    assert "file: report.pdf -> /tmp/report.pdf" in prompt
