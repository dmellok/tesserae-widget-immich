"""picture_immich albums_tags mode: several albums and tags, matched Any or Every."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = {"id": "home", "name": "Immich", "url": "http://immich", "api_key_secret": "k"}


def _load_server():
    pytest.importorskip("flask")
    pytest.importorskip("PIL")
    try:
        import app.plugin_http  # noqa: F401
    except Exception:
        pytest.skip("host app.plugin_http not importable; skipping server probe")
    sys.path.insert(0, str(ROOT))
    try:
        import server  # type: ignore[import-not-found]
    finally:
        if str(ROOT) in sys.path:
            sys.path.remove(str(ROOT))
    return server


@pytest.fixture
def server(monkeypatch, tmp_path):
    mod = _load_server()
    monkeypatch.setattr(mod, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "_by_id", lambda d, i: LIB if i == "home" else None)
    monkeypatch.setattr(mod, "_unwrap_token", lambda stored: "key")
    monkeypatch.setattr(mod, "_source_counts", {})
    return mod


def _opts(**extra):
    return {"library_id": "home", "mode": "albums_tags", **extra}


def test_old_one_source_cells_keep_their_one_source(server) -> None:
    # A tag left over from switching modes doesn't join an old album cell.
    assert server._selection(
        {"mode": "random_album", "album_id": "a", "tag_id": "t"}
    ) == (
        ["a"],
        [],
        "any",
    )
    assert server._selection(
        {"mode": "random_tag", "album_id": "a", "tag_id": "t"}
    ) == (
        [],
        ["t"],
        "any",
    )
    assert server._selection(
        {
            "mode": "albums_tags",
            "album_id": ["a", "a", " "],
            "tag_id": ["t"],
            "match": "all",
        }
    ) == (["a"], ["t"], "all")


def test_nothing_chosen_asks_for_an_album_or_tag(server) -> None:
    out = server.fetch(_opts(album_id=[], tag_id=[]), {}, ctx={})
    assert out == {"error": "Pick an album or a tag in the cell editor."}


def test_any_picks_a_source_weighted_by_its_photo_count(server, monkeypatch) -> None:
    counts = {("album", "a"): 0, ("tag", "t1"): 7, ("tag", "t2"): 3}
    monkeypatch.setattr(
        server, "_source_count", lambda lib, kind, sid: counts[(kind, sid)]
    )
    weights_seen = []

    def fake_choices(population, weights):
        weights_seen.append(list(weights))
        return [0]

    monkeypatch.setattr(server.random, "choices", fake_choices)
    picked = []
    monkeypatch.setattr(server, "_pick_asset_album", lambda lib, a: picked.append(a))
    monkeypatch.setattr(
        server, "_pick_asset_tag", lambda lib, t: picked.append(t) or {"asset_id": t}
    )
    out = server.fetch(_opts(album_id=["a"], tag_id=["t1", "t2"]), {}, ctx={})
    assert out["asset_id"] == "t1"
    # The empty album is never tried; the tags are weighted 7:3.
    assert weights_seen == [[7, 3]]
    assert picked == ["t1"]


def test_any_tries_another_source_when_one_comes_back_empty(
    server, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_source_count", lambda lib, kind, sid: 5)
    monkeypatch.setattr(server.random, "choices", lambda population, weights: [0])
    monkeypatch.setattr(server, "_pick_asset_album", lambda lib, a: None)
    monkeypatch.setattr(
        server, "_pick_asset_tag", lambda lib, t: {"asset_id": f"from-{t}"}
    )
    out = server.fetch(_opts(album_id=["a"], tag_id=["t"]), {}, ctx={})
    assert out["asset_id"] == "from-t"


def test_one_album_keeps_the_shared_album_timeline(server, monkeypatch) -> None:
    monkeypatch.setattr(
        server, "_source_count", lambda *a: pytest.fail("one source needs no count")
    )
    monkeypatch.setattr(
        server, "_pick_asset_album", lambda lib, a: {"asset_id": f"from-{a}"}
    )
    out = server.fetch(_opts(album_id="a", match="all"), {}, ctx={})
    assert out["asset_id"] == "from-a"


def test_every_asks_immich_for_all_albums_and_tags_at_once(server, monkeypatch) -> None:
    seen = {}

    def fake_post(url, headers, body, timeout):
        seen["url"], seen["body"] = url, body
        return [{"id": "x1", "originalMimeType": "image/jpeg"}]

    monkeypatch.setattr(server, "_post_json", fake_post)
    out = server.fetch(
        _opts(album_id=["a"], tag_id=["t1", "t2"], match="all"), {}, ctx={}
    )
    assert out["asset_id"] == "x1"
    assert seen["url"] == "http://immich/api/search/random"
    assert seen["body"]["albumIds"] == ["a"]
    assert seen["body"]["tagIds"] == ["t1", "t2"]


def test_every_with_no_overlap_says_so(server, monkeypatch) -> None:
    monkeypatch.setattr(server, "_post_json", lambda *a: [])
    out = server.fetch(_opts(album_id=["a"], tag_id=["t"], match="all"), {}, ctx={})
    assert "every chosen album" in out["error"]


def _forbidden():
    return urllib.error.HTTPError(
        "http://immich", 403, "Forbidden", {}, io.BytesIO(b"{}")
    )


def test_tag_count_falls_back_to_one_page_and_is_cached(server, monkeypatch) -> None:
    calls = []

    def fake_post(url, headers, body, timeout):
        calls.append(url)
        if url.endswith("/statistics"):
            raise _forbidden()
        return {"assets": {"items": [{"id": "1"}, {"id": "2"}], "total": 2}}

    monkeypatch.setattr(server, "_post_json", fake_post)
    assert server._source_count(LIB, "tag", "t") == 2
    assert server._source_count(LIB, "tag", "t") == 2
    assert calls == [
        "http://immich/api/search/statistics",
        "http://immich/api/search/metadata",
    ]


def test_album_count_reads_asset_count(server, monkeypatch) -> None:
    monkeypatch.setattr(server, "fetch_json", lambda url, **k: {"assetCount": 40})
    assert server._source_count(LIB, "album", "a") == 40


def test_count_failure_still_gives_the_source_a_turn(server, monkeypatch) -> None:
    def down(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(server, "fetch_json", down)
    assert server._source_count(LIB, "album", "a") == 1


@pytest.mark.parametrize(
    ("libraries", "label"),
    [
        ([LIB], "People/Julie"),
        ([LIB, {**LIB, "id": "nas", "name": "NAS"}], "People/Julie, Immich"),
    ],
)
def test_labels_name_the_library_only_when_there_are_several(
    server, monkeypatch, libraries, label
) -> None:
    monkeypatch.setattr(server, "_load_libraries", lambda d: libraries)
    monkeypatch.setattr(
        server,
        "_list_tags",
        lambda lib: (
            ([{"id": "t1", "name": "People/Julie"}], None) if lib is LIB else ([], None)
        ),
    )
    monkeypatch.setattr(
        server,
        "_list_albums",
        lambda lib: (
            [{"id": "b", "name": "beach"}, {"id": "a", "name": "Atlanta"}]
            if lib is LIB
            else []
        ),
    )
    assert server.choices("tags")[0]["label"] == label
    albums = server.choices("albums")
    assert [a["value"] for a in albums] == ["a", "b"]  # sorted by name, ignoring case
    assert albums[0]["label"] == label.replace("People/Julie", "Atlanta")


def _items(*ids):
    return {
        "assets": {
            "items": [{"id": i, "originalFileName": f"{i}.jpg"} for i in ids],
            "total": len(ids),
            "nextPage": None,
        }
    }


def test_lineup_lists_every_source_once(server, monkeypatch, tmp_path) -> None:
    bodies = []

    def fake_post(url, headers, body, timeout):
        bodies.append(body)
        return _items("x", "y") if body["tagIds"] == ["t1"] else _items("y", "z")

    monkeypatch.setattr(server, "_post_json", fake_post)
    ctx = {"data_dir": str(tmp_path)}
    opts = _opts(tag_id=["t1", "t2"])
    assert [i["key"] for i in server.lineup_items(opts, ctx=ctx)] == ["x", "y", "z"]
    assert server.lineup_item_count(opts, ctx=ctx) == 3
    # Each tag has its own cache entry, so the second listing asks Immich nothing.
    assert len(bodies) == 2


def test_lineup_every_lists_one_combined_search(server, monkeypatch, tmp_path) -> None:
    bodies = []

    def fake_post(url, headers, body, timeout):
        bodies.append(body)
        return _items("x")

    monkeypatch.setattr(server, "_post_json", fake_post)
    opts = _opts(album_id=["a"], tag_id=["t2", "t1"], match="all")
    items = server.lineup_items(opts, ctx={"data_dir": str(tmp_path)})
    assert [i["key"] for i in items] == ["x"]
    assert bodies[0]["albumIds"] == ["a"]
    assert bodies[0]["tagIds"] == ["t1", "t2"]
    cached = [json.loads(p.read_text()) for p in tmp_path.glob("lineup_*.json")]
    assert [c["source"] for c in cached] == ["search"]
