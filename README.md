# picture_immich

A Tesserae widget that paints full-bleed photos from one or more
Immich servers.

## What it does

- Connect any number of Immich libraries under
  **Widgets → Immich** in the Tesserae admin. Each library is a
  saved URL + API key.
- Add a cell on a page, pick the widget, pick a library, pick a mode.
- The cell paints a single photo per render. API keys never leave
  the server; the panel client only sees a Tesserae-proxied
  preview URL.

## Modes

| Mode             | Behaviour                                             |
| ---------------- | ----------------------------------------------------- |
| `memory`         | "On this day in past years." Falls back to `random`   |
|                  | when Immich returns no memory for today's date.       |
| `random`         | A single random asset from the whole library.         |
| `random_album`   | A single random asset from the chosen album.          |

## Install (when published to the catalog)

Open **Settings → Widgets → Browse** in Tesserae and search for
*Picture, Immich*. One click installs the widget into your
`data/plugins/` directory.

## Manual install (development)

```sh
cp -r /path/to/this/repo /path/to/tesserae/data/plugins/picture_immich
```

Restart Tesserae, then **Widgets → Immich** appears in the admin nav.

## Configuration

Each library record stores:

- `name` — friendly label shown in the cell editor's library dropdown.
- `url`  — base URL of the Immich server, no trailing slash.
- `api_key_secret` — your Immich API key, wrapped at rest with the
  same SecretBox that handles other Tesserae secrets.

Generate the API key from your Immich profile under
**Account Settings → API Keys**. A read-only scope is enough.

## Tested against

- Immich OpenAPI `3.0.0-rc.2` (Immich server `v2.7.x` and the v3
  release line). The widget hits these endpoints:
  `/api/server/ping`, `/api/memories?for=<date>`,
  `/api/search/random` (for whole-library and per-album random,
  the latter via `albumIds`; falling back to the legacy
  `/api/assets/random` and `/api/albums/<id>` on pre-v3 servers),
  `/api/albums`, and
  `/api/assets/<id>/thumbnail?size=preview` (with a fallback
  cascade to `?size=thumbnail` then `/original`). If a future
  Immich release changes these paths, the widget will fail loudly
  and surface the upstream error on the cell.

## HEIC support

Most iPhone photos arrive as HEIC. The widget transcodes HEIC to
JPEG on the proxy path using `pillow-heif`, which ships in the
host Tesserae environment as of `v0.64.28`. No extra install
needed.

For older Tesserae hosts (`<= v0.64.27`), the widget detects the
missing decoder at import time and surfaces a friendly
"install pillow-heif" message on cells whose chosen asset is
HEIC. Run `pip install pillow-heif` inside that environment to
unlock the path.

Either way, once you've kicked off **Administration → Jobs →
Generate thumbnails → Missing** in Immich, every asset has a
`?size=preview` derivative and the widget pulls those instead;
the local HEIC transcode only runs for the brief window between
upload and processing.

## License

AGPL-3.0-or-later, same as Tesserae.
