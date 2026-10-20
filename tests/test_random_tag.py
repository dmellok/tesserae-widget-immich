"""picture_immich random_tag mode: tag picker + tag-filtered random search.

Mocked HTTP only, mirroring the album tests in ``test_smoke.py``."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_server():
    pytest.importorskip("flask")
    pytest.importorskip("PIL")
    try:
        import app.plugin_http  # noqa: F401
    except Exception:
        pytest.skip("host app.plugin_http not importable; skipping server probe")
    import sys

    sys.path.insert(0, str(ROOT))
    try:
        import server  # type: ignore[import-not-found]
    finally:
        if str(ROOT) in sys.path:
            sys.path.remove(str(ROOT))
    return server


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://immich/api/tags", code, "Forbidden", {}, io.BytesIO(b"{}")
    )


def test_manifest_declares_tag_mode_and_picker() -> None:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    mode = next(o for o in manifest["cell_options"] if o["name"] == "mode")
    assert "random_tag" in {c["value"] for c in mode["choices"]}
    tag_opt = next(o for o in manifest["cell_options"] if o["name"] == "tag_id")
    assert tag_opt["type"] == "select"
    assert tag_opt["choices_from"] == "tags"


def test_tag_uses_search_random_with_tagids(monkeypatch) -> None:
    server = _load_server()
    seen: dict = {}

    def fake_post(url, headers, body, timeout):
        seen["url"] = url
        seen["body"] = body
        return [{"id": "tagged", "originalMimeType": "image/jpeg"}]

    monkeypatch.setattr(server, "_post_json", fake_post)
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")),
    )
    out = server._fetch_random_assets("http://immich", "key", tag_id="tag-1")
    assert out == [{"id": "tagged", "originalMimeType": "image/jpeg"}]
    assert seen["url"].endswith("/api/search/random")
    assert seen["body"] == {"size": 20, "withExif": True, "tagIds": ["tag-1"]}


def test_tag_has_no_legacy_fallback(monkeypatch) -> None:
    # The legacy GET /api/assets/random can't filter by tag, so a failed
    # search must not quietly paint an untagged photo.
    server = _load_server()
    monkeypatch.setattr(
        server,
        "_post_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")),
    )
    assert server._fetch_random_assets("http://immich", "key", tag_id="t") is None


def test_tag_pick_carries_location_and_skips_heic(monkeypatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server, "_HEIF_AVAILABLE", False)
    assets = [
        {"id": "heic", "originalMimeType": "image/heic"},
        {
            "id": "jpeg",
            "originalMimeType": "image/jpeg",
            "fileCreatedAt": "2024-03-15T00:00:00Z",
            "exifInfo": {"city": "Lyon", "country": "France"},
        },
    ]
    seen: dict = {}

    def fake_fetch(base, key, album_id=None, tag_id=None):
        seen["tag_id"] = tag_id
        seen["album_id"] = album_id
        return list(assets)

    monkeypatch.setattr(server, "_fetch_random_assets", fake_fetch)
    monkeypatch.setattr(server, "_unwrap_token", lambda stored: stored)
    lib = {"url": "http://immich", "api_key_secret": "key"}
    for _ in range(10):
        picked = server._pick_asset_tag(lib, "tag-1")
        assert picked["asset_id"] == "jpeg"
    assert picked["location"] == "Lyon, France"
    assert picked["taken_at"] == "2024-03-15T00:00:00Z"
    assert seen == {"tag_id": "tag-1", "album_id": None}
    assert server._pick_asset_tag(lib, "") is None


def test_list_tags_labels_by_full_path(monkeypatch) -> None:
    server = _load_server()

    def fake_get(url, **kwargs):
        assert url == "http://immich/api/tags"
        return [
            {"id": "t2", "name": "Kitchen", "value": "Displays/Kitchen"},
            {"id": "t1", "name": "Displays", "value": "Displays"},
            {"id": "t3", "name": "beach", "value": "Holidays/beach"},
            {"name": "no id"},
        ]

    monkeypatch.setattr(server, "fetch_json", fake_get)
    monkeypatch.setattr(server, "_unwrap_token", lambda stored: stored)
    tags, hint = server._list_tags({"url": "http://immich", "api_key_secret": "k"})
    assert hint is None
    assert tags == [
        {"id": "t1", "name": "Displays"},
        {"id": "t2", "name": "Displays/Kitchen"},
        {"id": "t3", "name": "Holidays/beach"},
    ]


def test_list_tags_403_hints_at_permission(monkeypatch) -> None:
    server = _load_server()

    def fake_get(url, **kwargs):
        raise _http_error(403)

    monkeypatch.setattr(server, "fetch_json", fake_get)
    monkeypatch.setattr(server, "_unwrap_token", lambda stored: stored)
    tags, hint = server._list_tags({"url": "http://immich", "api_key_secret": "k"})
    assert tags == []
    assert hint == "API key needs the tag.read permission"


def test_tag_choices_merge_hints_and_tags(monkeypatch) -> None:
    server = _load_server()
    libs = [
        {"id": "home", "name": "Home", "url": "http://a", "api_key_secret": "k"},
        {"id": "nas", "name": "NAS", "url": "http://b", "api_key_secret": "k"},
    ]
    monkeypatch.setattr(server, "_data_dir", lambda: Path("."))
    monkeypatch.setattr(server, "_load_libraries", lambda d: libs)

    def fake_list(lib):
        if lib["id"] == "home":
            return [{"id": "t1", "name": "Displays/Kitchen"}], None
        return [], "API key needs the tag.read permission"

    monkeypatch.setattr(server, "_list_tags", fake_list)
    assert server.choices("tags") == [
        {"value": "", "label": "API key needs the tag.read permission (NAS)"},
        {"value": "t1", "label": "Displays/Kitchen, Home"},
    ]

    monkeypatch.setattr(server, "_list_tags", lambda lib: ([], None))
    only = server.choices("tags")
    assert len(only) == 1 and only[0]["value"] == ""


def test_fetch_random_tag_mode(monkeypatch) -> None:
    server = _load_server()
    lib = {"id": "home", "name": "Home", "url": "http://a", "api_key_secret": "k"}
    monkeypatch.setattr(server, "_data_dir", lambda: Path("."))
    monkeypatch.setattr(server, "_by_id", lambda d, i: lib)
    monkeypatch.setattr(
        server,
        "_pick_asset_tag",
        lambda library, tag_id: {"asset_id": f"from-{tag_id}", "location": None},
    )
    out = server.fetch(
        {"library_id": "home", "mode": "random_tag", "tag_id": "tag-1"}, {}, ctx={}
    )
    assert out["asset_id"] == "from-tag-1"
    assert out["mode"] == "random_tag"
    out = server.fetch({"library_id": "home", "mode": "random_tag"}, {}, ctx={})
    assert out == {"error": "Pick a tag in the cell editor."}
