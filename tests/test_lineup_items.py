"""Lineups items: the photo list, its previews, the count and the pin."""

from __future__ import annotations

import io
import sys
import time
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
    lib = {"id": "lib1", "name": "Home", "url": "http://immich", "api_key_secret": "k"}
    monkeypatch.setattr(mod, "_by_id", lambda data_dir, library_id: lib if library_id == "lib1" else None)
    monkeypatch.setattr(mod, "_unwrap_token", lambda stored: "key")
    mod.CTX = {"data_dir": str(tmp_path)}
    return mod


def _ctx(server):
    return server.CTX


def _opts(**extra):
    return {"library_id": "lib1", "mode": "random_album", "album_id": "al1", **extra}


class _Timeline:
    """Fake Immich album timeline: three buckets, newest first."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.buckets = {
            "2026-08-01T00:00:00.000Z": {
                "id": ["a1", "a2", "vid", "gone"],
                "isImage": [True, True, False, True],
                "isTrashed": [False, False, False, True],
                "fileCreatedAt": ["2026-08-20T10:00:00Z"] * 4,
            },
            "2026-07-01T00:00:00.000Z": {
                "id": ["b1"],
                "isImage": [True],
                "isTrashed": [False],
                "fileCreatedAt": ["2026-07-02T10:00:00Z"],
            },
        }

    def __call__(self, url, **kwargs):
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append(parsed.path)
        if parsed.path == "/api/timeline/buckets":
            return [{"timeBucket": tb, "count": len(c["id"])} for tb, c in self.buckets.items()]
        if parsed.path == "/api/timeline/bucket":
            return self.buckets[query["timeBucket"]]
        raise AssertionError(url)


def test_album_is_listed_with_real_count_and_small_thumbs(server, monkeypatch) -> None:
    monkeypatch.setattr(server, "fetch_json", _Timeline())
    items = server.lineup_items(_opts(), ctx=_ctx(server))
    assert [i["key"] for i in items] == ["a1", "a2", "b1"]  # no video, no trashed
    assert items[0]["label"] == "2026-08-20"
    assert items[0]["thumb"] == "/plugins/picture_immich/image/lib1/a1?thumb=1"
    assert server.lineup_item_count(_opts(), ctx=_ctx(server)) == 3


def test_a_second_listing_reads_the_cache_not_immich(server, monkeypatch) -> None:
    fake = _Timeline()
    monkeypatch.setattr(server, "fetch_json", fake)
    server.lineup_items(_opts(), ctx=_ctx(server))
    fake.calls.clear()
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    )
    assert len(server.lineup_items(_opts(), ctx=_ctx(server))) == 3
    assert server.lineup_item_count(_opts(), ctx=_ctx(server)) == 3
    assert fake.calls == []


def test_a_slow_album_fills_in_over_several_requests(server, monkeypatch) -> None:
    fake = _Timeline()
    monkeypatch.setattr(server, "fetch_json", fake)
    clock = iter([0.0, 0.0, 0.0, 99.0, 99.0, 99.0, 99.0, 99.0])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock, 99.0))
    first = server.lineup_items(_opts(), ctx=_ctx(server))
    assert [i["key"] for i in first] == ["a1", "a2"]  # one bucket before the deadline
    # The unfinished list is resumed (not restarted) once its short TTL passes.
    monkeypatch.setattr(server, "LINEUP_PARTIAL_TTL_S", -1)
    monkeypatch.setattr(server.time, "monotonic", time.monotonic)
    fake.calls.clear()
    second = server.lineup_items(_opts(), ctx=_ctx(server))
    assert [i["key"] for i in second] == ["a1", "a2", "b1"]
    assert "/api/timeline/buckets" not in fake.calls


def test_immich_down_returns_what_is_cached_or_nothing(server, monkeypatch) -> None:
    def down(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(server, "fetch_json", down)
    monkeypatch.setattr(server, "_post_json", down)
    assert server.lineup_items(_opts(), ctx=_ctx(server)) == []
    assert server.lineup_item_count(_opts(), ctx=_ctx(server)) is None
    # Warm the cache, let it go stale, then take Immich down: the list stays.
    monkeypatch.setattr(server, "fetch_json", _Timeline())
    server.lineup_items(_opts(), ctx=_ctx(server))
    monkeypatch.setattr(server, "LINEUP_TTL_S", -1)
    monkeypatch.setattr(server, "fetch_json", down)
    assert len(server.lineup_items(_opts(), ctx=_ctx(server))) == 3


def test_no_library_lists_nothing(server) -> None:
    assert server.lineup_items({"mode": "random"}, ctx=_ctx(server)) == []
    assert server.lineup_items({"library_id": "nope"}, ctx=_ctx(server)) == []


def test_tag_is_listed_page_by_page(server, monkeypatch) -> None:
    bodies = []

    def post(url, headers, body, timeout):
        bodies.append((url, body))
        assert url.endswith("/api/search/metadata")
        page = body["page"]
        items = [{"id": f"t{page}{n}", "originalFileName": f"IMG_{page}{n}.jpg"} for n in (1, 2)]
        return {"assets": {"items": items, "total": 4, "nextPage": "2" if page == 1 else None}}

    monkeypatch.setattr(server, "_post_json", post)
    items = server.lineup_items(
        {"library_id": "lib1", "mode": "random_tag", "tag_id": "tg"}, ctx=_ctx(server)
    )
    assert [i["key"] for i in items] == ["t11", "t12", "t21", "t22"]
    assert items[0]["label"] == "IMG_11.jpg"
    assert bodies[0][1]["tagIds"] == ["tg"]
    assert server.lineup_item_count(
        {"library_id": "lib1", "mode": "random_tag", "tag_id": "tg"}, ctx=_ctx(server)
    ) == 4


def test_memory_lists_todays_assets(server, monkeypatch) -> None:
    def get(url, **kw):
        assert "/api/memories" in url
        return [
            {"assets": [{"id": "m1", "type": "IMAGE", "localDateTime": "2022-05-01T09:00:00Z"}]},
            {"assets": [{"id": "m2", "type": "VIDEO"}, {"id": "m3", "type": "IMAGE"}]},
        ]

    monkeypatch.setattr(server, "fetch_json", get)
    items = server.lineup_items({"library_id": "lib1", "mode": "memory"}, ctx=_ctx(server))
    assert [i["key"] for i in items] == ["m1", "m3"]


def test_empty_memory_and_random_list_a_random_pool_with_the_library_total(
    server, monkeypatch
) -> None:
    def get(url, **kw):
        if "/api/memories" in url:
            return []
        assert url.endswith("/api/assets/statistics")
        return {"images": 5321, "videos": 10}

    monkeypatch.setattr(server, "fetch_json", get)
    monkeypatch.setattr(
        server, "_post_json", lambda url, h, body, t: [{"id": "r1"}, {"id": "r2"}]
    )
    for mode in ("memory", "random"):
        opts = {"library_id": "lib1", "mode": mode}
        assert [i["key"] for i in server.lineup_items(opts, ctx=_ctx(server))] == ["r1", "r2"]
        assert server.lineup_item_count(opts, ctx=_ctx(server)) == 5321


def test_old_server_without_timeline_lists_a_random_album_pool(server, monkeypatch) -> None:
    def get(url, **kw):
        raise RuntimeError("404")

    seen = {}

    def post(url, headers, body, timeout):
        seen.update(body)
        return [{"id": "o1"}]

    monkeypatch.setattr(server, "fetch_json", get)
    monkeypatch.setattr(server, "_post_json", post)
    items = server.lineup_items(_opts(), ctx=_ctx(server))
    assert [i["key"] for i in items] == ["o1"]
    assert seen["albumIds"] == ["al1"]


def test_junk_ids_are_not_listed(server, monkeypatch) -> None:
    fake = _Timeline()
    fake.buckets = {
        "2026-08-01T00:00:00.000Z": {"id": ["ok", "../x", "a b"], "isImage": [True] * 3}
    }
    monkeypatch.setattr(server, "fetch_json", fake)
    assert [i["key"] for i in server.lineup_items(_opts(), ctx=_ctx(server))] == ["ok"]


# ----- the pin -----------------------------------------------------------


def _fetch(server, monkeypatch, options, detail=None, picked=None):
    monkeypatch.setattr(server, "_data_dir", lambda: Path("/nonexistent"))
    monkeypatch.setattr(
        server,
        "_detail_asset",
        lambda base, key, asset_id: detail.get(asset_id) if detail else None,
    )
    calls = []

    def pick(*a, **k):
        calls.append(a)
        return picked

    for name in ("_pick_asset_random", "_pick_asset_album", "_pick_asset_tag", "_pick_asset_memory"):
        monkeypatch.setattr(server, name, pick)
    out = server.fetch(options, {}, ctx={"cell_w": 800, "cell_h": 480})
    return out, calls


def test_a_pinned_cell_shows_that_photo_and_picks_nothing(server, monkeypatch) -> None:
    detail = {
        "p1": {
            "id": "p1",
            "type": "IMAGE",
            "originalMimeType": "image/jpeg",
            "localDateTime": "2020-01-02T03:04:05Z",
            "exifInfo": {"city": "Lyon", "country": "France"},
        }
    }
    out, calls = _fetch(server, monkeypatch, _opts(pinned_asset="p1"), detail)
    assert out["asset_id"] == "p1"
    assert out["image_url"] == "/plugins/picture_immich/image/lib1/p1?w=800&h=480"
    assert out["location"] == "Lyon, France"
    assert calls == []


def test_a_pin_that_is_gone_falls_back_to_the_mode(server, monkeypatch) -> None:
    picked = {"asset_id": "z9", "taken_at": None, "memory_year": None, "mime": "image/jpeg"}
    for detail in ({}, {"p1": {"id": "p1", "isTrashed": True}}, {"p1": {"id": "p1", "type": "VIDEO"}}):
        out, calls = _fetch(server, monkeypatch, _opts(pinned_asset="p1"), detail, picked)
        assert out["asset_id"] == "z9"
        assert len(calls) == 1


def test_unpinned_cell_picks_as_before(server, monkeypatch) -> None:
    picked = {"asset_id": "z9", "taken_at": None, "memory_year": None, "mime": "image/jpeg"}
    out, calls = _fetch(server, monkeypatch, _opts(), None, picked)
    assert out["asset_id"] == "z9" and len(calls) == 1


# ----- the preview proxy -------------------------------------------------


def _jpeg(size):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


def test_thumb_reads_the_small_thumbnail_and_is_cacheable(server, monkeypatch) -> None:
    flask = pytest.importorskip("flask")
    asked = []

    class Resp(io.BytesIO):
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=0):
        asked.append(req.full_url)
        return Resp(_jpeg((250, 167)))

    monkeypatch.setattr(server, "_data_dir", lambda: None)
    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    app = flask.Flask(__name__)
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/picture_immich")
    resp = app.test_client().get("/plugins/picture_immich/image/lib1/a1?thumb=1&w=3840&h=2160")
    assert resp.status_code == 200
    assert asked == ["http://immich/api/assets/a1/thumbnail?size=thumbnail"]
    assert "max-age" in resp.headers["Cache-Control"]
    from PIL import Image

    assert Image.open(io.BytesIO(resp.data)).size == (250, 167)  # never enlarged
