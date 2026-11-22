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


def test_manifest_defaults_to_any_and_offers_auto() -> None:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    opt = next(o for o in manifest["cell_options"] if o["name"] == "orientation")
    assert opt["default"] == "any"
    assert [c["value"] for c in opt["choices"]][:3] == ["auto", "frame", "any"]


@pytest.mark.parametrize(
    ("orientation", "ctx", "expected"),
    [
        ("auto", {"cell_w": 800, "cell_h": 480, **MIXED}, {"landscape", "square"}),
        ("auto", {"cell_w": 480, "cell_h": 800, **MIXED}, {"portrait", "square"}),
        ("auto", {"cell_w": 800, "cell_h": 480, "frame_shapes": ["landscape"]}, None),
        ("auto", {"cell_w": 800, "cell_h": 480}, None),
        # Unset reads as the default, Any, even on mixed frames.
        (None, {"cell_w": 800, "cell_h": 480, **MIXED}, None),
        # Sizes that are not numbers are treated as unknown.
        ("frame", {"cell_w": "wide", "cell_h": None}, None),
        ("frame", {"cell_w": "x", "cell_h": "y", "panel_w": 480, "panel_h": 800}, {"portrait", "square"}),
        ("frame", {"cell_w": "800", "cell_h": "480"}, {"landscape", "square"}),
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
    opts = {
        "library_id": "home",
        "mode": "albums_tags",
        "tag_id": ["t"],
        "orientation": "auto",
    }
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
    opts = {
        "library_id": "home",
        "mode": "albums_tags",
        "album_id": ["al"],
        "orientation": "auto",
    }
    ctx = {"data_dir": str(tmp_path), **MIXED, "cell_w": 480, "cell_h": 800}
    assert [i["key"] for i in server.lineup_items(opts, ctx=ctx)] == ["t1"]
    # A cell that saved no orientation lists every photo, as it renders.
    del opts["orientation"]
    assert [i["key"] for i in server.lineup_items(opts, ctx=ctx)] == ["w1", "t1"]


def test_a_cut_short_list_scales_its_count_by_the_share_that_fits(server) -> None:
    listing = {
        "items": [{"id": "a", "shape": "landscape"}, {"id": "b", "shape": "portrait"}],
        "total": 1000,
    }
    ctx = {**MIXED, "cell_w": 480, "cell_h": 800}
    shown, total = server._frame_listing(listing, {"orientation": "auto"}, ctx)
    assert [i["id"] for i in shown] == ["b"] and total == 500


# ----- the default, and strict fixed shapes -----------------------------


def test_an_unset_orientation_shows_any_photo_on_mixed_frames(
    server, monkeypatch
) -> None:
    """A cell saved before the option existed is not narrowed to the frame."""
    seen = set()
    for pool in ([TALL, WIDE], [WIDE, TALL]):
        monkeypatch.setattr(server, "_post_json", lambda *a, pool=pool: list(pool))
        out = server.fetch(
            {"library_id": "home", "mode": "random"},
            {},
            ctx={"cell_w": 480, "cell_h": 800, **MIXED},
        )
        seen.add(out["asset_id"])
    assert seen == {"tall", "wide"}


def _fixed(server, mode="random", orientation="portrait", **extra):
    opts = {"library_id": "home", "mode": mode, "orientation": orientation, **extra}
    return server.fetch(opts, {}, ctx={"cell_w": 800, "cell_h": 480, **MIXED})


def test_a_fixed_shape_asks_again_until_it_finds_one(server, monkeypatch) -> None:
    batches = [[WIDE] * 20, [WIDE] * 20, [WIDE] * 19 + [TALL]]
    calls = []

    def fake_post(url, headers, body, timeout):
        calls.append(body)
        return batches[len(calls) - 1]

    monkeypatch.setattr(server, "_post_json", fake_post)
    assert _fixed(server)["asset_id"] == "tall"
    assert len(calls) == 3


def test_a_fixed_shape_never_shows_another_shape(server, monkeypatch) -> None:
    calls = []

    def fake_post(url, headers, body, timeout):
        calls.append(body)
        return [WIDE, SQUARE] * 10

    monkeypatch.setattr(server, "_post_json", fake_post)
    out = _fixed(server)
    assert out == {
        "error": "No portrait photos found in this library. Choose another "
        "Orientation, or photos of that shape."
    }
    assert len(calls) == server.STRICT_SEARCH_TRIES  # bounded
    # A photo of unknown size is not shown either.
    monkeypatch.setattr(server, "_post_json", lambda *a: [{"id": "unknown"}])
    assert "No square photos found" in _fixed(server, orientation="square")["error"]


def test_a_short_batch_is_everything_so_the_search_stops(server, monkeypatch) -> None:
    calls = []

    def fake_post(url, headers, body, timeout):
        calls.append(body)
        return [WIDE, WIDE]

    monkeypatch.setattr(server, "_post_json", fake_post)
    out = _fixed(server, mode="albums_tags", tag_id=["t"])
    assert "No portrait photos found in the chosen albums and tags" in out["error"]
    assert len(calls) == 1


def test_a_fixed_shape_with_immich_down_blames_the_connection(
    server, monkeypatch
) -> None:
    def down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(server, "_post_json", down)
    monkeypatch.setattr(server, "fetch_json", down)
    assert "Check the library connection" in _fixed(server)["error"]
    out = _fixed(server, mode="albums_tags", album_id=["a"], tag_id=["t"], match="all")
    assert "did not answer" in out["error"]


def test_a_fixed_shape_with_every_keeps_the_two_messages_apart(
    server, monkeypatch
) -> None:
    every = {"mode": "albums_tags", "album_id": ["a"], "tag_id": ["t"], "match": "all"}
    monkeypatch.setattr(server, "_post_json", lambda *a: [])
    assert "every chosen album" in _fixed(server, **every)["error"]
    monkeypatch.setattr(server, "_post_json", lambda *a: [WIDE])
    assert "No portrait photos found" in _fixed(server, **every)["error"]
    monkeypatch.setattr(server, "_post_json", lambda *a: [WIDE, TALL])
    assert _fixed(server, **every)["asset_id"] == "tall"


def _album_api(monkeypatch, server, buckets):
    """A fake album timeline: ``buckets`` maps a time bucket to its ratios."""
    read = []

    def fake(url, **kwargs):
        if "/timeline/buckets" in url:
            return [{"timeBucket": b, "count": len(r)} for b, r in buckets.items()]
        if "/timeline/bucket?" in url:
            bucket = next(b for b in buckets if b in url)
            read.append(bucket)
            ratios = buckets[bucket]
            return {
                "id": [f"{bucket}-{i}" for i in range(len(ratios))],
                "isImage": [True] * len(ratios),
                "isTrashed": [False] * len(ratios),
                "ratio": ratios,
            }
        asset_id = url.rsplit("/", 1)[-1]
        return {"id": asset_id, "originalMimeType": "image/jpeg"}

    monkeypatch.setattr(server, "fetch_json", fake)
    return read


def test_a_fixed_shape_reads_more_album_buckets(server, monkeypatch) -> None:
    buckets = {f"2026-0{m}-01": [1.5, 1.5] for m in range(1, 6)}
    buckets["2026-06-01"] = [0.75]
    read = _album_api(monkeypatch, server, buckets)
    monkeypatch.setattr(
        server, "_post_json", lambda *a: pytest.fail("the timeline has it")
    )
    for _ in range(5):
        out = _fixed(server, mode="albums_tags", album_id=["al"])
        assert out["asset_id"] == "2026-06-01-0"
    assert len(read) <= 5 * server.STRICT_BUCKET_TRIES


def test_an_album_without_the_fixed_shape_says_so(server, monkeypatch) -> None:
    read = _album_api(
        monkeypatch, server, {"2026-01-01": [1.5, 1.4], "2026-02-01": [1.5]}
    )
    monkeypatch.setattr(
        server, "_post_json", lambda *a: pytest.fail("every bucket was read")
    )
    out = _fixed(server, mode="albums_tags", album_id=["al"])
    assert "No portrait photos found in the chosen albums and tags" in out["error"]
    assert sorted(read) == ["2026-01-01", "2026-02-01"]
    # Auto and Match the frame still fall back to what the album has.
    auto = _fixed(server, mode="albums_tags", album_id=["al"], orientation="frame")
    assert auto["asset_id"].startswith("2026-")
    tall = server.fetch(
        {"library_id": "home", "mode": "albums_tags", "album_id": ["al"], "orientation": "frame"},
        {},
        ctx={"cell_w": 480, "cell_h": 800},
    )
    assert tall["asset_id"].startswith("2026-")


def test_a_big_album_falls_through_to_the_search_when_buckets_run_out(
    server, monkeypatch
) -> None:
    buckets = {f"20{y}-01-01": [1.5] for y in range(10, 30)}
    read = _album_api(monkeypatch, server, buckets)
    monkeypatch.setattr(server, "_post_json", lambda *a: [WIDE, TALL])
    out = _fixed(server, mode="albums_tags", album_id=["al"])
    assert out["asset_id"] == "tall"
    assert len(read) == server.STRICT_BUCKET_TRIES


def test_any_passes_over_a_source_without_the_fixed_shape(server, monkeypatch) -> None:
    monkeypatch.setattr(server, "_source_counts", {})
    monkeypatch.setattr(server, "_source_count", lambda lib, kind, sid: 5)
    monkeypatch.setattr(server.random, "choices", lambda population, weights: [0])

    def fake_post(url, headers, body, timeout):
        return [WIDE] if body["tagIds"] == ["t1"] else [TALL]

    monkeypatch.setattr(server, "_post_json", fake_post)
    out = _fixed(server, mode="albums_tags", tag_id=["t1", "t2"])
    assert out["asset_id"] == "tall"
    monkeypatch.setattr(server, "_post_json", lambda *a: [WIDE])
    out = _fixed(server, mode="albums_tags", tag_id=["t1", "t2"])
    assert "No portrait photos found" in out["error"]


def test_memory_of_a_fixed_shape_or_a_random_photo_of_it(server, monkeypatch) -> None:
    memories = [{"data": {"year": 2020}, "assets": [WIDE]}]
    monkeypatch.setattr(server, "fetch_json", lambda url, **k: memories)
    assert server._pick_asset_memory(LIB, server.StrictShapes({"portrait"})) is None
    monkeypatch.setattr(server, "_post_json", lambda *a: [WIDE, TALL])
    assert _fixed(server, mode="memory")["asset_id"] == "tall"


def test_a_pinned_photo_ignores_a_fixed_shape(server, monkeypatch) -> None:
    monkeypatch.setattr(
        server, "_pinned_asset", lambda lib, asset_id: {"asset_id": asset_id}
    )
    assert _fixed(server, pinned_asset="wide")["asset_id"] == "wide"


def test_lineups_list_nothing_of_another_shape_for_a_fixed_shape(
    server, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "_post_json", lambda *a: _search_page(WIDE, SQUARE))
    opts = {
        "library_id": "home",
        "mode": "albums_tags",
        "tag_id": ["t"],
        "orientation": "portrait",
    }
    ctx = {"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 480}
    assert server.lineup_items(opts, ctx=ctx) == []
    assert server.lineup_item_count(opts, ctx=ctx) is None


def test_an_album_timeline_without_ratios_is_no_proof_of_absence(
    server, monkeypatch
) -> None:
    """An older server's timeline has no ratio column: the album is searched
    instead of being reported as having no portrait photos."""
    _album_api(monkeypatch, server, {"2026-01-01": [None, None]})
    monkeypatch.setattr(server, "_post_json", lambda *a: [WIDE, TALL])
    out = _fixed(server, mode="albums_tags", album_id=["al"])
    assert out["asset_id"] == "tall"
