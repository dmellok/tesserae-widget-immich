"""picture_immich orientation: Auto, Match the frame, and fixed shapes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = {"id": "home", "name": "Immich", "url": "http://immich", "api_key_secret": "k"}
WIDE = {"id": "wide", "width": 4032, "height": 3024, "originalMimeType": "image/jpeg"}
TALL = {"id": "tall", "width": 3024, "height": 4032, "originalMimeType": "image/jpeg"}
SQUARE = {"id": "sq", "width": 1000, "height": 1010, "originalMimeType": "image/jpeg"}
MIXED = {"frame_shapes": ["landscape", "portrait"]}


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
    return mod


def test_manifest_defaults_to_auto() -> None:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    opt = next(o for o in manifest["cell_options"] if o["name"] == "orientation")
    assert opt["default"] == "auto"
    assert [c["value"] for c in opt["choices"]][:3] == ["auto", "frame", "any"]


@pytest.mark.parametrize(
    ("orientation", "ctx", "expected"),
    [
        ("auto", {"cell_w": 800, "cell_h": 480, **MIXED}, {"landscape", "square"}),
        ("auto", {"cell_w": 480, "cell_h": 800, **MIXED}, {"portrait", "square"}),
        ("auto", {"cell_w": 800, "cell_h": 480, "frame_shapes": ["landscape"]}, None),
        ("auto", {"cell_w": 800, "cell_h": 480}, None),
        (None, {"cell_w": 800, "cell_h": 480, **MIXED}, {"landscape", "square"}),
        ("frame", {"cell_w": 480, "cell_h": 800}, {"portrait", "square"}),
        ("frame", {"panel_w": 1600, "panel_h": 1200}, {"landscape", "square"}),
        ("frame", {"cell_w": 500, "cell_h": 500}, None),
        ("any", {"cell_w": 800, "cell_h": 480, **MIXED}, None),
        ("portrait", {"cell_w": 800, "cell_h": 480}, {"portrait"}),
    ],
)
def test_wanted_shapes(server, orientation, ctx, expected) -> None:
    shapes = server._wanted_shapes({"orientation": orientation}, ctx)
    assert (set(shapes) if shapes else None) == expected


def test_asset_shape_reads_immich_size_then_rotated_exif(server) -> None:
    assert server._asset_shape(WIDE) == "landscape"
    assert server._asset_shape(SQUARE) == "square"
    sideways = {
        "exifInfo": {
            "exifImageWidth": 4032,
            "exifImageHeight": 3024,
            "orientation": "6",
        }
    }
    assert server._asset_shape(sideways) == "portrait"
    assert server._asset_shape({}) is None


def _random_fetch(server, monkeypatch, pool, **ctx):
    monkeypatch.setattr(server, "_post_json", lambda *a: list(pool))
    return server.fetch(
        {"library_id": "home", "mode": "random", "orientation": "auto"}, {}, ctx=ctx
    )


def test_auto_picks_the_frames_shape_on_mixed_frames(server, monkeypatch) -> None:
    for _ in range(10):
        wide = _random_fetch(
            server, monkeypatch, [TALL, WIDE], cell_w=800, cell_h=480, **MIXED
        )
        tall = _random_fetch(
            server, monkeypatch, [TALL, WIDE], cell_w=480, cell_h=800, **MIXED
        )
        assert (wide["asset_id"], tall["asset_id"]) == ("wide", "tall")


def test_no_photo_of_the_shape_shows_any_photo(server, monkeypatch) -> None:
    out = _random_fetch(server, monkeypatch, [TALL], cell_w=800, cell_h=480, **MIXED)
    assert out["asset_id"] == "tall"


def test_tag_and_every_picks_follow_the_shape(server, monkeypatch) -> None:
    monkeypatch.setattr(server, "_post_json", lambda *a: [TALL, WIDE, SQUARE])
    shapes = frozenset({"landscape", "square"})
    for _ in range(10):
        assert server._pick_asset_tag(LIB, "t", shapes)["asset_id"] in ("wide", "sq")
        picked = server._pick_asset_every(LIB, ["a"], ["t"], shapes)["asset_id"]
        assert picked in ("wide", "sq")


def test_album_timeline_keeps_images_of_the_shape_by_ratio(server, monkeypatch) -> None:
    cols = {
        "id": ["w1", "t1", "t2"],
        "isImage": [True, True, True],
        "isTrashed": [False, False, False],
        "ratio": [1.5, 0.75, 0.66],
    }

    def fake(url, **kwargs):
        if "/timeline/buckets" in url:
            return [{"timeBucket": "2026-01-01", "count": 3}]
        return cols

    monkeypatch.setattr(server, "fetch_json", fake)
    assert server._album_image_ids(
        "http://immich", "key", "al", frozenset({"portrait"})
    ) == [
        "t1",
        "t2",
    ]
    # No bucket holds the shape: the images are returned anyway.
    assert server._album_image_ids(
        "http://immich", "key", "al", frozenset({"square"})
    ) == [
        "w1",
        "t1",
        "t2",
    ]


def test_memory_prefers_the_shape(server, monkeypatch) -> None:
    memories = [
        {"data": {"year": 2020}, "assets": [{**TALL, "exifInfo": {"city": "X"}}, WIDE]}
    ]
    monkeypatch.setattr(server, "fetch_json", lambda url, **k: memories)
    for _ in range(10):
        out = server._pick_asset_memory(LIB, frozenset({"landscape", "square"}))
        assert out["asset_id"] == "wide"


def _search_page(*assets):
    return {"assets": {"items": list(assets), "total": len(assets), "nextPage": None}}


def test_lineups_list_only_what_each_frame_is_shown(
    server, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        server, "_post_json", lambda *a: _search_page(WIDE, TALL, SQUARE)
    )
    opts = {"library_id": "home", "mode": "albums_tags", "tag_id": ["t"]}
    base = {"data_dir": str(tmp_path), **MIXED}
    wide = {**base, "cell_w": 800, "cell_h": 480}
    tall = {**base, "cell_w": 480, "cell_h": 800}
    assert [i["key"] for i in server.lineup_items(opts, ctx=wide)] == ["wide", "sq"]
    assert [i["key"] for i in server.lineup_items(opts, ctx=tall)] == ["tall", "sq"]
    assert server.lineup_item_count(opts, ctx=wide) == 2
    # On frames of one shape, Auto lists every photo.
    one = {
        "data_dir": str(tmp_path),
        "frame_shapes": ["landscape"],
        "cell_w": 800,
        "cell_h": 480,
    }
    assert server.lineup_item_count(opts, ctx=one) == 3
    # A fixed Portrait lists portrait photos on any frame.
    assert [
        i["key"]
        for i in server.lineup_items({**opts, "orientation": "portrait"}, ctx=one)
    ] == ["tall"]


def test_album_lineups_read_the_shape_from_the_timeline_ratio(
    server, monkeypatch, tmp_path
) -> None:
    cols = {
        "id": ["w1", "t1"],
        "isImage": [True, True],
        "isTrashed": [False, False],
        "fileCreatedAt": ["2026-01-01T00:00:00Z"] * 2,
        "ratio": [1.5, 0.75],
    }

    def fake(url, **kwargs):
        if "/timeline/buckets" in url:
            return [{"timeBucket": "2026-01-01", "count": 2}]
        return cols

    monkeypatch.setattr(server, "fetch_json", fake)
    opts = {"library_id": "home", "mode": "albums_tags", "album_id": ["al"]}
    ctx = {"data_dir": str(tmp_path), **MIXED, "cell_w": 480, "cell_h": 800}
    assert [i["key"] for i in server.lineup_items(opts, ctx=ctx)] == ["t1"]


def test_a_cut_short_list_scales_its_count_by_the_share_that_fits(server) -> None:
    listing = {
        "items": [{"id": "a", "shape": "landscape"}, {"id": "b", "shape": "portrait"}],
        "total": 1000,
    }
    ctx = {**MIXED, "cell_w": 480, "cell_h": 800}
    shown, total = server._frame_listing(listing, {}, ctx)
    assert [i["id"] for i in shown] == ["b"] and total == 500
