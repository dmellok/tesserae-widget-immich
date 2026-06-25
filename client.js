// picture_immich, full-bleed Immich photo.
// Same call shape as picture_gallery: ``(shadow, ctx)``. We render
// into ``shadow.innerHTML`` directly and pick up the Spectra
// widget stylesheet so the error tile + bleed layout inherit the
// rest of Tesserae's look. Widget-specific tweaks (caption overlay)
// live inline below.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function objectFitFor(scale) {
  switch (scale) {
    case "fill": return "cover";
    case "center": return "none";
    case "stretch": return "fill";
    case "fit":
    default: return "contain";
  }
}

function captionFor(data) {
  const parts = [];
  if (data.memory_year) {
    parts.push(`${data.memory_year} year${data.memory_year === 1 ? "" : "s"} ago`);
  }
  if (data.taken_at) {
    const dt = new Date(data.taken_at);
    if (!Number.isNaN(dt.getTime())) {
      parts.push(
        dt.toLocaleDateString(undefined, {
          day: "numeric", month: "short", year: "numeric",
        }),
      );
    }
  }
  if (data.library_name) parts.push(data.library_name);
  return parts.join("  ·  ");
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const opts = ctx?.cell?.options || {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="picture_immich">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>Immich</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  if (!data.image_url) {
    shadow.innerHTML = `
      ${css}
      <div class="w is-bleed" data-widget="picture_immich">
        <div class="bleed-empty">No photo to display.</div>
      </div>`;
    return;
  }

  const fit = objectFitFor(data.scale || opts.scale || "fit");
  const showCaption = data.show_caption !== false && opts.show_caption !== false;
  const caption = showCaption ? captionFor(data) : "";

  // ``object-fit`` lives on the inline ``style`` attribute on
  // ``<img>`` so it outranks ``spectra-widgets.css`` ``.w.is-bleed
  // > img``, which otherwise hardcodes ``cover`` and ignores the
  // cell's scale option.
  shadow.innerHTML = `
    ${css}
    <style>
      .pi-caption {
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        padding: 8px 14px;
        background: rgba(0, 0, 0, 0.55);
        color: #fff;
        font-size: 13px;
        line-height: 1.25;
        letter-spacing: 0.04em;
        text-shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
      }
    </style>
    <div class="w is-bleed" data-widget="picture_immich">
      <img src="${escapeHtml(data.image_url)}" alt="" loading="eager"
           style="width:100%;height:100%;display:block;object-fit:${fit};object-position:center;">
      ${caption ? `<div class="pi-caption">${escapeHtml(caption)}</div>` : ""}
    </div>`;
}
