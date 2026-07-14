"""picture_immich, full-bleed photos from one or more Immich servers.

A "library" is a saved connection to an Immich server: name, URL, and
API key. Connections live in ``libraries.json`` inside the plugin's
``data_dir``; API keys are wrapped with the host's ``SecretBox`` so
they never sit in plaintext on disk. The admin pages under
``/plugins/picture_immich/`` let the user CRUD libraries; cell options
pick a library + mode per cell.

Modes:
* ``memory``       — today's "on this day" memory (Immich's
  ``/api/memories``). Returns an asset taken on this calendar day in
  a previous year. Falls back to ``random`` when no memory exists for
  the date.
* ``random``       — random asset from the whole library
  (``POST /api/search/random``; legacy ``GET /api/assets/random``
  fallback for pre-v3 servers).
* ``random_album`` — random asset from a specific album
  (``POST /api/search/random`` with ``albumIds``; legacy
  ``GET /api/albums/<id>`` embedded-assets fallback for pre-v3).

The browser/panel client never sees the API key. All image fetches go
through ``GET /plugins/picture_immich/image/<library_id>/<asset_id>``,
which proxies to Immich's preview endpoint with the stored key.
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

import io
import urllib.error
import urllib.request

from PIL import Image, ImageOps, UnidentifiedImageError

from app.plugin_http import fetch_json

# HEIC / HEIF support is optional but high-value: most iPhone photos
# arrive HEIC, and Immich v2.x serves only the original file. When
# ``pillow-heif`` is installed in the Tesserae environment we
# register it with Pillow at import time so the proxy can decode
# + transcode HEIC the same way it handles JPEG. Without it, HEIC
# assets surface as a blank tile and the README points the
# operator at ``pip install pillow-heif``.
try:
    import pillow_heif  # type: ignore[import-not-found]

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    _HEIF_AVAILABLE = False

# Cap on the long edge of the downscaled JPEG the proxy serves to the
# panel. Bigger than any panel we ship today, smaller than a typical
# phone original. Keeps push payloads to a sensible size without
# softening the image more than the panel itself would.
PROXY_MAX_EDGE_PX = 2000
PROXY_JPEG_QUALITY = 88
# Preview cascade. Immich exposes the same ``/api/assets/<id>/thumbnail``
# endpoint with a ``?size=...`` switch (see Immich OpenAPI spec
# 3.0.0-rc.2, AssetMediaSize enum: original / fullsize / preview /
# thumbnail). ``original`` returns 400, the documented sizes return
# 200 only after the host's thumbnail-generation job has processed
# the asset; otherwise we get 404 "Asset media not found".
#
# Strategy: try ``preview`` first (best balance of fidelity + size,
# transcoded to JPEG server-side so HEIC just works), fall back to
# ``thumbnail`` (smaller but always covers the cases where preview
# wasn't generated), and finally fall back to the original bytes
# we transcode locally for assets whose derivatives don't exist
# yet (new uploads, jobs queue paused, etc.).
IMMICH_THUMB_CASCADE = (
    "/api/assets/{asset_id}/thumbnail?size=preview",
    "/api/assets/{asset_id}/thumbnail?size=thumbnail",
)
IMMICH_ORIGINAL = "/api/assets/{asset_id}/original"

logger = logging.getLogger(__name__)

LIBRARIES_FILE = "libraries.json"
HTTP_TIMEOUT_S = 12
PROXY_TIMEOUT_S = 30
_LIBRARY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


# ----- data_dir + libraries store -------------------------------------


def _data_dir() -> Path:
    registry = current_app.config["PLUGIN_REGISTRY"]
    plugin = registry.get("picture_immich")
    if plugin is None:
        raise RuntimeError("picture_immich plugin not registered")
    path: Path = plugin.data_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _libraries_path(data_dir: Path) -> Path:
    return data_dir / LIBRARIES_FILE


def _secret_box() -> Any:
    return current_app.config.get("SECRET_BOX")


def _load_libraries(data_dir: Path) -> list[dict[str, Any]]:
    """Return the raw on-disk list. Keys stay wrapped here; callers
    that need the plaintext token call ``_unwrap_token``."""
    path = _libraries_path(data_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("picture_immich: libraries.json unreadable; treating as empty")
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out.append(entry)
    return out


def _save_libraries(data_dir: Path, libraries: list[dict[str, Any]]) -> None:
    path = _libraries_path(data_dir)
    path.write_text(json.dumps(libraries, indent=2, sort_keys=True), encoding="utf-8")


def _wrap_token(plain: str) -> str:
    box = _secret_box()
    if box is None or not plain:
        return plain
    return str(box.wrap(plain))


def _unwrap_token(stored: str) -> str:
    box = _secret_box()
    if box is None or not stored:
        return stored
    try:
        return str(box.unwrap(stored))
    except Exception:
        logger.warning(
            "picture_immich: stored API key can't be decrypted; treating as empty"
        )
        return ""


def _by_id(data_dir: Path, library_id: str) -> dict[str, Any] | None:
    for lib in _load_libraries(data_dir):
        if lib.get("id") == library_id:
            return lib
    return None


def _public_view(library: dict[str, Any]) -> dict[str, Any]:
    """Strip the wrapped key out of a record for cell-editor / template
    consumption. Indicates whether a key is set without leaking it."""
    return {
        "id": library.get("id", ""),
        "name": library.get("name", ""),
        "url": library.get("url", ""),
        "has_key": bool(library.get("api_key_secret")),
    }


def _new_id(existing: list[dict[str, Any]]) -> str:
    taken = {lib.get("id") for lib in existing}
    while True:
        candidate = secrets.token_hex(3)
        if candidate not in taken:
            return candidate


# ----- Immich API helpers ---------------------------------------------


def _api_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "Accept": "application/json",
        "User-Agent": "tesserae-picture_immich/0.1",
    }


def _post_json(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: float
) -> Any:
    """POST a JSON body and decode the JSON response.

    ``app.plugin_http.fetch_json`` is GET-only, and Immich's random
    search (the v3 replacement for the removed ``GET /api/assets/random``)
    is a POST, so this is the one call that needs its own request."""
    data = json.dumps(body).encode("utf-8")
    req_headers = dict(headers)
    req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ping(url: str, api_key: str) -> tuple[bool, str]:
    """Return ``(ok, message)``. Used by the admin "Test" button."""
    base = url.rstrip("/")
    try:
        fetch_json(
            f"{base}/api/server/ping",
            headers=_api_headers(api_key),
            timeout=HTTP_TIMEOUT_S,
        )
        return True, "Reachable."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _list_albums(library: dict[str, Any]) -> list[dict[str, Any]]:
    base = library.get("url", "").rstrip("/")
    if not base:
        return []
    key = _unwrap_token(library.get("api_key_secret", ""))
    if not key:
        return []
    try:
        payload = fetch_json(
            f"{base}/api/albums",
            headers=_api_headers(key),
            timeout=HTTP_TIMEOUT_S,
        )
    except Exception as exc:
        logger.info(
            "picture_immich: list_albums failed for %s: %s", library.get("id"), exc
        )
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for a in payload:
        if isinstance(a, dict) and isinstance(a.get("id"), str):
            out.append({"id": a["id"], "name": a.get("albumName") or a["id"]})
    return out


def _pick_asset_memory(library: dict[str, Any]) -> dict[str, Any] | None:
    base = library.get("url", "").rstrip("/")
    key = _unwrap_token(library.get("api_key_secret", ""))
    if not (base and key):
        return None
    today = datetime.now(tz=UTC).date().isoformat()
    try:
        payload = fetch_json(
            f"{base}/api/memories?for={today}",
            headers=_api_headers(key),
            timeout=HTTP_TIMEOUT_S,
        )
    except Exception as exc:
        logger.info("picture_immich: memories fetch failed: %s", exc)
        return None
    if not isinstance(payload, list):
        return None
    # Flatten ``memories[].assets[]`` and pick one at random. The
    # memory wrapping (year banner etc.) is up to the client; the
    # server hands back a single chosen asset.
    flat: list[dict[str, Any]] = []
    for memory in payload:
        if not isinstance(memory, dict):
            continue
        assets = memory.get("assets")
        year_offset = (
            memory.get("data", {}).get("year")
            if isinstance(memory.get("data"), dict)
            else None
        )
        if isinstance(assets, list):
            for asset in assets:
                if isinstance(asset, dict) and isinstance(asset.get("id"), str):
                    flat.append({"asset": asset, "memory_year": year_offset})
    if not flat:
        return None
    chosen = random.choice(flat)  # noqa: S311  (display-only, not crypto)
    asset = chosen["asset"]
    return {
        "asset_id": asset["id"],
        "taken_at": asset.get("fileCreatedAt") or asset.get("localDateTime"),
        "memory_year": chosen.get("memory_year"),
    }


def _is_browser_friendly(mime: str | None) -> bool:
    """JPEG / PNG / WebP / GIF render in every browser engine without
    extra decoders. HEIC needs pillow-heif (not bundled) so we'd
    rather pick a different asset."""
    if not isinstance(mime, str):
        return False
    mime = mime.lower()
    return any(
        mime.endswith(suffix) for suffix in ("/jpeg", "/jpg", "/png", "/webp", "/gif")
    )


def _fetch_random_assets(
    base: str, key: str, album_id: str | None = None
) -> list[dict[str, Any]] | None:
    """Fetch a batch of random assets, tolerant of the Immich API changes.

    Immich v3 reworked both paths this widget relied on:

    * ``GET /api/assets/random`` was removed in favour of
      ``POST /api/search/random`` (body ``{"size": N}``).
    * ``GET /api/albums/{id}`` no longer embeds an ``assets`` array
      (``AlbumResponseDto`` now carries only ``assetCount``); album assets
      come from ``POST /api/search/random`` with ``albumIds``.

    So the v3 path is one endpoint for both cases: POST search/random, with
    ``albumIds`` added when ``album_id`` is given. Fall back to the legacy
    GET (whole-library random, or the album detail's embedded ``assets``)
    for pre-v3 servers. Returns the asset list, or ``None`` when both paths
    fail (network / auth / an even older server)."""
    headers = _api_headers(key)
    body: dict[str, Any] = {"size": 20}
    if album_id:
        body["albumIds"] = [album_id]
    try:
        payload = _post_json(f"{base}/api/search/random", headers, body, HTTP_TIMEOUT_S)
        if isinstance(payload, list):
            return payload
    except Exception as exc:
        logger.info(
            "picture_immich: search/random failed, trying legacy endpoint: %s", exc
        )
    # Legacy (pre-v3) fallbacks.
    try:
        if album_id:
            payload = fetch_json(
                f"{base}/api/albums/{urllib.parse.quote(album_id)}",
                headers=headers,
                timeout=HTTP_TIMEOUT_S,
            )
            assets = payload.get("assets") if isinstance(payload, dict) else None
            return assets if isinstance(assets, list) else None
        payload = fetch_json(
            f"{base}/api/assets/random?count=20",
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
        )
    except Exception as exc:
        logger.info("picture_immich: random fetch failed: %s", exc)
        return None
    return payload if isinstance(payload, list) else None


def _pick_asset_random(library: dict[str, Any]) -> dict[str, Any] | None:
    """Ask Immich for a bunch of random candidates so we can skip past
    formats Pillow (without pillow-heif) can't decode. Falls through
    to the first asset if every candidate is HEIC."""
    base = library.get("url", "").rstrip("/")
    key = _unwrap_token(library.get("api_key_secret", ""))
    if not (base and key):
        return None
    payload = _fetch_random_assets(base, key)
    if not payload:
        return None
    candidates = [a for a in payload if isinstance(a, dict) and a.get("id")]
    if not candidates:
        return None
    # When HEIF is available, every format is fair game; when it
    # isn't, fall back to JPEG / PNG / WebP / GIF or surface a
    # friendlier error rather than paint a blank.
    if _HEIF_AVAILABLE:
        asset = candidates[0]
    else:
        preferred = [
            a for a in candidates if _is_browser_friendly(a.get("originalMimeType"))
        ]
        asset = preferred[0] if preferred else candidates[0]
    return {
        "asset_id": asset.get("id"),
        "taken_at": asset.get("fileCreatedAt") or asset.get("localDateTime"),
        "memory_year": None,
        "mime": asset.get("originalMimeType"),
    }


def _pick_asset_album(library: dict[str, Any], album_id: str) -> dict[str, Any] | None:
    base = library.get("url", "").rstrip("/")
    key = _unwrap_token(library.get("api_key_secret", ""))
    if not (base and key and album_id):
        return None
    payload = _fetch_random_assets(base, key, album_id)
    if not payload:
        return None
    pool = [a for a in payload if isinstance(a, dict) and a.get("id")]
    if not pool:
        return None
    random.shuffle(pool)  # noqa: S311  (display-only)
    if _HEIF_AVAILABLE:
        chosen = pool[0]
    else:
        chosen = next(
            (a for a in pool if _is_browser_friendly(a.get("originalMimeType"))),
            pool[0],
        )
    return {
        "asset_id": chosen.get("id"),
        "taken_at": chosen.get("fileCreatedAt") or chosen.get("localDateTime"),
        "memory_year": None,
        "mime": chosen.get("originalMimeType"),
    }


# ----- plugin contract ------------------------------------------------


def choices(name: str) -> list[dict[str, Any]]:
    """Cell-editor populates dropdowns from this. ``name`` is the
    ``choices_from`` value declared in plugin.json."""
    data_dir = _data_dir()
    if name == "libraries":
        libraries = _load_libraries(data_dir)
        if not libraries:
            return [
                {"value": "", "label": "Add an Immich library under Widgets → Immich"}
            ]
        return [
            {"value": lib["id"], "label": lib.get("name") or lib["id"]}
            for lib in libraries
        ]
    if name == "albums":
        # The cell-editor doesn't know which library is selected at
        # choices() time, so we collapse across every configured
        # library. Each option is labelled "<album>, <library>" so
        # they don't collide.
        out: list[dict[str, Any]] = []
        for lib in _load_libraries(data_dir):
            for album in _list_albums(lib):
                out.append(
                    {
                        "value": album["id"],
                        "label": f"{album['name']}, {lib.get('name') or lib['id']}",
                    }
                )
        if not out:
            return [{"value": "", "label": "No albums; check the library connection."}]
        return out
    return []


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Return a state dict the client renders against."""
    del settings, ctx
    library_id = (options.get("library_id") or "").strip()
    if not library_id:
        return {"error": "Pick a library in the cell editor."}
    library = _by_id(_data_dir(), library_id)
    if library is None:
        return {"error": f"Library {library_id!r} no longer exists."}

    mode = (options.get("mode") or "memory").strip()
    asset: dict[str, Any] | None
    if mode == "random":
        asset = _pick_asset_random(library)
    elif mode == "random_album":
        asset = _pick_asset_album(library, (options.get("album_id") or "").strip())
    else:
        # ``memory`` (default). Falls back to ``random`` when the
        # library has no memory for today, so the cell still paints
        # something useful rather than an empty error tile.
        asset = _pick_asset_memory(library) or _pick_asset_random(library)

    if asset is None or not asset.get("asset_id"):
        return {"error": "Immich returned no asset. Check the library connection."}

    # Friendlier error when the chosen asset is HEIC and the host
    # doesn't have ``pillow-heif`` installed. Better to surface
    # "install pillow-heif" than paint a blank tile.
    if (
        not _HEIF_AVAILABLE
        and isinstance(asset.get("mime"), str)
        and "heic" in asset["mime"].lower()
    ):
        return {
            "error": (
                "This asset is HEIC and the Tesserae host has no HEIF decoder. "
                "Run `pip install pillow-heif` inside the Tesserae environment, "
                "or pick an album with JPEG / PNG photos."
            ),
        }

    # Plain-string URL rather than ``url_for``: ``fetch()`` runs in the
    # composer's background context, which has no Flask request, so a
    # ``url_for`` call there raises "Unable to build URLs outside an
    # active request without 'SERVER_NAME' configured". The route shape
    # is stable inside this plugin, so a hand-built path is safe.
    safe_asset = urllib.parse.quote(asset["asset_id"], safe="")
    safe_library = urllib.parse.quote(library_id, safe="")
    return {
        "library_name": library.get("name") or library_id,
        "asset_id": asset["asset_id"],
        "taken_at": asset.get("taken_at"),
        "memory_year": asset.get("memory_year"),
        "image_url": f"/plugins/picture_immich/image/{safe_library}/{safe_asset}",
        "scale": options.get("scale") or "fit",
        "show_caption": bool(options.get("show_caption", True)),
        "mode": mode,
    }


# ----- admin blueprint ------------------------------------------------


def blueprint() -> Blueprint:
    bp = Blueprint("picture_immich_admin", __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        data_dir = _data_dir()
        libraries = [_public_view(lib) for lib in _load_libraries(data_dir)]
        return render_template("picture_immich/index.html", libraries=libraries)

    @bp.get("/libraries/new")
    def new_library() -> str:
        return render_template(
            "picture_immich/library.html",
            library=None,
            mode="new",
            test_result=None,
        )

    @bp.post("/libraries")
    def create_library() -> Response:
        data_dir = _data_dir()
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip().rstrip("/")
        api_key = (request.form.get("api_key") or "").strip()
        if not name or not url or not api_key:
            flash("Name, URL, and API key are all required.", "warn")
            return redirect(url_for("picture_immich_admin.new_library"))
        libraries = _load_libraries(data_dir)
        libraries.append(
            {
                "id": _new_id(libraries),
                "name": name,
                "url": url,
                "api_key_secret": _wrap_token(api_key),
            }
        )
        _save_libraries(data_dir, libraries)
        flash(f"Added Immich library {name!r}.", "ok")
        return redirect(url_for("picture_immich_admin.index"))

    @bp.get("/libraries/<library_id>")
    def show_library(library_id: str) -> str | Response:
        if not _LIBRARY_ID_RE.match(library_id):
            abort(404)
        library = _by_id(_data_dir(), library_id)
        if library is None:
            abort(404)
        return render_template(
            "picture_immich/library.html",
            library=_public_view(library),
            mode="edit",
            test_result=None,
        )

    @bp.post("/libraries/<library_id>")
    def update_library(library_id: str) -> Response:
        if not _LIBRARY_ID_RE.match(library_id):
            abort(404)
        data_dir = _data_dir()
        libraries = _load_libraries(data_dir)
        target = next((lib for lib in libraries if lib["id"] == library_id), None)
        if target is None:
            abort(404)
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip().rstrip("/")
        api_key = (request.form.get("api_key") or "").strip()
        if not name or not url:
            flash("Name and URL are required.", "warn")
            return redirect(
                url_for("picture_immich_admin.show_library", library_id=library_id)
            )
        target["name"] = name
        target["url"] = url
        # Empty ``api_key`` field on edit means "leave as-is"; the
        # field is rendered as a password input with no displayed
        # value, so the user only types when they want to rotate.
        if api_key:
            target["api_key_secret"] = _wrap_token(api_key)
        _save_libraries(data_dir, libraries)
        flash(f"Updated {name!r}.", "ok")
        return redirect(url_for("picture_immich_admin.index"))

    @bp.post("/libraries/<library_id>/delete")
    def delete_library(library_id: str) -> Response:
        if not _LIBRARY_ID_RE.match(library_id):
            abort(404)
        data_dir = _data_dir()
        libraries = _load_libraries(data_dir)
        new_libraries = [lib for lib in libraries if lib["id"] != library_id]
        if len(new_libraries) == len(libraries):
            abort(404)
        _save_libraries(data_dir, new_libraries)
        flash("Library removed.", "ok")
        return redirect(url_for("picture_immich_admin.index"))

    @bp.post("/libraries/<library_id>/test")
    def test_library(library_id: str) -> str | Response:
        if not _LIBRARY_ID_RE.match(library_id):
            abort(404)
        library = _by_id(_data_dir(), library_id)
        if library is None:
            abort(404)
        ok, msg = _ping(
            library["url"], _unwrap_token(library.get("api_key_secret", ""))
        )
        return render_template(
            "picture_immich/library.html",
            library=_public_view(library),
            mode="edit",
            test_result={"ok": ok, "message": msg},
        )

    @bp.get("/image/<library_id>/<asset_id>")
    def serve_image(library_id: str, asset_id: str) -> Response:
        """Proxy Immich's preview endpoint with the stored API key,
        so the panel client never sees it. Cached by Tesserae's
        outer render cache; we ourselves never cache the bytes."""
        if not _LIBRARY_ID_RE.match(library_id):
            abort(404)
        library = _by_id(_data_dir(), library_id)
        if library is None:
            abort(404)
        key = _unwrap_token(library.get("api_key_secret", ""))
        base = library.get("url", "").rstrip("/")
        if not (key and base):
            abort(404)
        safe_asset = urllib.parse.quote(asset_id, safe="")
        # Cascade: try Immich's transcoded thumbnail variants first
        # (already JPEG, sized for display, cheap on both ends);
        # only fall back to /original + local Pillow transcode when
        # the derivative hasn't been generated yet. Tested against
        # Immich OpenAPI 3.0.0-rc.2.
        raw_bytes: bytes | None = None
        raw_ct: str | None = None
        for tmpl in (
            *IMMICH_THUMB_CASCADE,
            IMMICH_ORIGINAL,
        ):
            url = f"{base}{tmpl.format(asset_id=safe_asset)}"
            req = urllib.request.Request(url, headers=_api_headers(key))
            try:
                with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_S) as upstream:
                    raw_bytes = upstream.read()
                    raw_ct = upstream.headers.get("Content-Type") or "image/jpeg"
                    break
            except urllib.error.HTTPError as exc:
                # 404 "Asset media not found" means the derivative
                # hasn't been generated yet; move to the next rung.
                # 400 / 401 / 403 are terminal: stop the cascade
                # and surface 502 below.
                if exc.code != 404:
                    logger.info(
                        "picture_immich: proxy fetch %s for %s: HTTP %s",
                        url,
                        asset_id,
                        exc.code,
                    )
                    break
            except (urllib.error.URLError, OSError) as exc:
                logger.info(
                    "picture_immich: proxy fetch failed for %s/%s: %s",
                    library_id,
                    asset_id,
                    exc,
                )
                abort(502)

        if raw_bytes is None:
            abort(502)

        body, content_type = _downscale_jpeg(raw_bytes, raw_ct or "image/jpeg")
        resp = Response(body, mimetype=content_type)
        # The widget cell already has its own server-side render
        # cache; tell the browser this URL is fresh per request so
        # we don't have to bust query strings on update.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return bp


# ----- proxy helpers --------------------------------------------------


def _downscale_jpeg(raw: bytes, content_type: str) -> tuple[bytes, str]:
    """Open with Pillow, resize the long edge down to ``PROXY_MAX_EDGE_PX``,
    re-encode as JPEG. On any decode failure (HEIC without pillow-heif
    installed, exotic formats, truncated bytes), pass the bytes
    through unchanged so a half-working cell beats a 502 tile."""
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im) or im
            im.thumbnail(
                (PROXY_MAX_EDGE_PX, PROXY_MAX_EDGE_PX), Image.Resampling.LANCZOS
            )
            if im.mode not in {"RGB", "L"}:
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=PROXY_JPEG_QUALITY, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.info(
            "picture_immich: Pillow can't decode (%s); passing original bytes through",
            type(exc).__name__,
        )
        return raw, content_type or "application/octet-stream"
