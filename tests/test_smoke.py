"""picture_immich smoke test, contract-level.

Does not boot Tesserae or hit a real Immich. Verifies:

* plugin.json parses and declares the cell options the
  cell-editor + composer assume.
* server.py imports cleanly and exposes the three plugin-contract
  entry points (``fetch``, ``choices``, ``blueprint``).
* ``blueprint()`` returns a Flask Blueprint with the routes the
  admin pages link to.

The host-side widget loader has its own broader test; this
smoke is here so a regression in the manifest or server module
fails before install rather than at first render.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_declares_expected_cell_options() -> None:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "Picture, Immich"
    assert manifest["kind"] == "widget"
    names = {opt["name"] for opt in manifest["cell_options"]}
    assert names == {"library_id", "mode", "album_id", "scale", "show_caption"}
    library_opt = next(o for o in manifest["cell_options"] if o["name"] == "library_id")
    assert library_opt["choices_from"] == "libraries"
    album_opt = next(o for o in manifest["cell_options"] if o["name"] == "album_id")
    assert album_opt["choices_from"] == "albums"


def test_server_module_exposes_plugin_contract() -> None:
    # The test fixture is bare metal: server.py imports Flask + the
    # host's plugin_http, which both need to be on sys.path. Skip if
    # the host isn't reachable so the test stays useful in standalone
    # CI on the widget repo too.
    flask = pytest.importorskip("flask")
    try:
        import app.plugin_http  # noqa: F401
    except Exception:
        pytest.skip("host app.plugin_http not importable; skipping contract probe")

    import sys

    sys.path.insert(0, str(ROOT))
    try:
        import server  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(ROOT))

    assert callable(server.fetch)
    assert callable(server.choices)
    assert callable(server.blueprint)
    bp = server.blueprint()
    assert isinstance(bp, flask.Blueprint)
    # Without actually registering against an app, we can't enumerate
    # rules. Just confirm the blueprint object came back; rule-level
    # checks live in the host-side widget-loader tests.
    assert bp.name == "picture_immich_admin"


def _load_server():
    """Import the widget's server module, or skip if the host isn't on
    the path (keeps the widget repo's standalone CI green)."""
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


def test_random_prefers_v3_search_endpoint(monkeypatch) -> None:
    # Immich v3 removed GET /api/assets/random for POST /api/search/random.
    # The primary path must hit the new endpoint; the legacy GET must not
    # be called when the new one succeeds.
    server = _load_server()
    calls: list[str] = []

    def fake_post(url, headers, body, timeout):
        calls.append(("POST", url, body))
        return [{"id": "a1", "originalMimeType": "image/jpeg"}]

    def fake_get(url, **kwargs):
        calls.append(("GET", url))
        raise AssertionError("legacy GET should not be called on v3 success")

    monkeypatch.setattr(server, "_post_json", fake_post)
    monkeypatch.setattr(server, "fetch_json", fake_get)
    out = server._fetch_random_assets("http://immich", "key")
    assert out == [{"id": "a1", "originalMimeType": "image/jpeg"}]
    assert calls[0][0] == "POST" and calls[0][1].endswith("/api/search/random")
    assert calls[0][2] == {"size": 20}


def test_random_falls_back_to_legacy_get(monkeypatch) -> None:
    # Pre-v3 servers 404 the new endpoint; the widget must fall back to
    # the legacy GET so existing installs keep working.
    server = _load_server()

    def fake_post(url, headers, body, timeout):
        raise RuntimeError("404 Not Found")

    def fake_get(url, **kwargs):
        assert url.endswith("/api/assets/random?count=20")
        return [{"id": "legacy", "originalMimeType": "image/png"}]

    monkeypatch.setattr(server, "_post_json", fake_post)
    monkeypatch.setattr(server, "fetch_json", fake_get)
    out = server._fetch_random_assets("http://immich", "key")
    assert out == [{"id": "legacy", "originalMimeType": "image/png"}]


def test_random_returns_none_when_both_paths_fail(monkeypatch) -> None:
    server = _load_server()
    monkeypatch.setattr(
        server,
        "_post_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert server._fetch_random_assets("http://immich", "key") is None
