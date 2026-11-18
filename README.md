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
| `albums_tags`    | A single random asset from the chosen albums and     |
|                  | tags (see below).                                     |

### Albums and tags (`albums_tags`)

Choose any number of albums and tags. **Match** decides how they combine:

- **Any album or tag** (default): a photo from any of them. The album or tag
  is picked weighted by its photo count, so every photo is about equally
  likely. An album includes other members' photos in a shared album.
- **Every album and tag**: only photos that are in every chosen album and
  carry every chosen tag (`/api/search/random` with `albumIds` and `tagIds`).

The tag picker reads `/api/tags` and lists each tag by its full path
(`Displays/Kitchen`), since Immich tags can be nested; a parent tag includes
the tags under it. The API key needs the `tag.read` permission; without it the
picker shows "API key needs the tag.read permission" instead of your tags.
Pickers name the library after each album or tag only when more than one
library is connected.

Cells saved with the old `random_album` or `random_tag` modes open as Albums
and tags with their one album or tag.

### Orientation

**Auto** (the default) shows each frame photos of its own shape when the
dashboard is on both portrait and landscape frames, and any photo otherwise.
**Match the frame** always does; **Portrait**, **Landscape** and **Square** keep
that shape. Matching the frame keeps square photos too. The shape comes from
Immich's photo size (an album's timeline `ratio`, so no extra calls). When no
candidate has the shape, any photo is shown. Lineups lists every photo.

## Lineups

On the Lineups page and in the lineup editor, a step showing this widget lists
the photos it draws from, with previews and the real count: the chosen albums'
images (every member's, newest first) and the photos carrying the chosen tags,
each listed once, or today's memory. A random library (and a memory day with no memories) lists a
pool of random photos and the library's image total. **Show chosen** pins the
cell to one photo (the `pinned_asset` option, shown under Advanced); clear it to
go back to the mode. A pinned cell shows exactly that photo and picks nothing.

Listings are read from a small cache in the widget's data folder (an hour for
albums and tags, fifteen minutes for a random pool) and refilled with a
four-second limit on Immich, so a big album fills in over a few visits and a
slow or unreachable server never stalls the page. Lists stop at 1000 photos.
Previews come from Immich's small thumbnail through the widget's image route
(`?thumb=1`).

The widget still declares `"draw": true` for a random library; the host uses it
once the cell has no list to offer.

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
If you pick individual permissions instead of "All", the widget
needs `asset.read`, `asset.view`, `asset.download` (originals not
yet thumbnailed), `album.read` (album picker), `memory.read` (memory
mode), and `tag.read` (tag picker). Without
`tag.read` the tag picker shows "API key needs the tag.read
permission" instead of your tags.

## Caption format

With **Caption format** blank the caption is the built-in one (date, place,
library name). To write your own, fill in a text with `{fields}`:

| Field | Value |
| --- | --- |
| `{YYYY}` `{YY}` | year, `2026` / `26` |
| `{MMMM}` `{MMM}` `{MM}` `{M}` | month, `August` / `Aug` / `08` / `8` |
| `{DD}` `{D}` | day of the month, `02` / `2` |
| `{city}` `{state}` `{country}` | place from the photo's GPS, as Immich names it |
| `{cc}` | the country as an ISO code (`NL`); the full name if it isn't in the table |
| `{library}` | the library's name |
| `{memory}` | `3 years ago`, memory mode only |

For example `{D}/{M}/{YYYY} - {city}, {state}, {cc}` gives
`2/8/2026 - Otterlo, Gelderland, NL`. The date is the day the photo was taken
where it was taken. Month names are English.

A field with nothing in it is left out, and so is the separator that went with
it: the text between two fields is printed only between two fields that are
both shown, using the separator after the last one shown. So without a state
the example reads `2/8/2026 - Otterlo, NL`, and without a city
`2/8/2026 - Gelderland, NL`. Text before the first field and after the last
one is always printed (`({city}, {cc})` gives `(NL)` when only the country
is known), and a format that shows nothing at all gives no caption. An
unknown `{word}` is printed as it is.

## Tested against

- Immich OpenAPI `3.0.0-rc.2` (Immich server `v2.7.x` and the v3
  release line). The widget hits these endpoints:
  `/api/server/ping`, `/api/memories?for=<date>`,
  `/api/search/random` (for whole-library random, and per-album
  random via `albumIds` as a fallback; falling back to the legacy
  `/api/assets/random` and `/api/albums/<id>` on pre-v3 servers),
  `/api/timeline/buckets` + `/api/timeline/bucket?albumId=` and
  `/api/assets/<id>` (per-album random: unlike `search/random`, the
  timeline covers every member's assets of a shared album; same
  `asset.read` permission as `search/random`),
  `/api/albums`, and
  `/api/assets/<id>/thumbnail?size=preview` (with a fallback
  cascade to `?size=thumbnail` then `/original`). If a future
  Immich release changes these paths, the widget will fail loudly
  and surface the upstream error on the cell.

## Photo size

Each photo is sized to the cell it is drawn in, so a 4K screen gets a 4K photo
and an 800 x 480 e-paper tag about 800 px, never the full original. Immich's
preview (1440 px on the short edge by default) is used when it covers the cell;
a bigger cell reads the original and scales it down to just cover the cell.
`/original` needs the `asset.download` permission on the API key.

Set **Resolution** to **Full** to read the original first, even when a preview
covers the cell. This can help on TVs and other large screens when Immich's
preview looks soft. The photo is still sized to the cell and never enlarged.
If the original cannot be read, the widget uses a preview. On older hosts that
do not pass a cell size, Full keeps up to 3840 px on the long edge; Standard
keeps the existing 2000 px limit. Lineups previews stay small in either mode.

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
