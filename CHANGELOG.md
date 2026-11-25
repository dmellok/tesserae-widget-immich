# Changelog

## [Unreleased]

- Each frame gets its own photo. Two frames of the same size on one dashboard used to show the identical random photo, because a dashboard is rendered once per panel size and copied to every frame of that size. The widget now asks for a render per frame (`render.per_device_id`). In a mode that holds one photo for the day the extra render draws the same photo. (#4)
- Add an Orientation option. The default is Any. Auto matches the cell's shape on each frame when a dashboard is on both portrait and landscape frames (needs Tesserae newer than 0.514.0). Portrait, Landscape and Square show only that shape.
- Pick several albums and tags in one Albums and tags mode, matched Any or Every. Pickers drop the library name when only one is connected. Every only finds photos owned by the API key's user. Old Random from album and Random from tag cells open in the new mode on Tesserae 0.499.3 or later.
- Add a Full resolution option that reads the original photo first for large screens. An original this server can't draw (a RAW file, one past Pillow's pixel limit) or one over 64 MB is skipped and the preview is shown instead.
