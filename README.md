# Overlay DSL

A small YAML format for describing timed text/emoji overlays (with fades, shadows, and a
soft gradient background) to be composited onto a video — built for YouTube Shorts made
from static sheet-music scores with a moving playhead.

The DSL is a plain data file. A separate tool (not included here) parses it and generates
an `ffmpeg` `filter_complex` to render the final video.

## Quick example

```yaml
video: sheet_music.mp4
output: out.mp4

canvas:
  width: 1080
  height: 1920
  fit: letterbox_blur

defaults:
  position: { x: center, y: 0.4 }
  fade_in: 0.3
  fade_out: 0.3
  style:
    font: "Arial"
    size: 48
    color: "#22c55e"
    bold: true
    shadow:
      color: "#000000"
      opacity: 0.5
      offset_x: 2
      offset_y: 2
      blur: 3
    background:
      style: gradient_fade
      color: "#000000"
      opacity: 0.5
      sizing: fixed
      width: 500
      height: 160
      feather: 24

overlays:
  - text: "A happy accident!"
    emoji: "🎻"
    start: 3.0
    end: 5.5

  - text: "Nice tempo shift"
    start: 12.0
    end: 14.0

  - text: "Rushing a bit here"
    start: 20.5
    end: 22.0
    style:
      color: "#f97316"
```

## Top-level fields

| Field    | Type   | Required | Description                                      |
|----------|--------|----------|---------------------------------------------------|
| `video`  | string | yes      | Path to the input video file.                     |
| `output` | string | yes      | Path to write the rendered video to.              |
| `canvas` | object | no       | Target frame size and how the input is fit to it. |
| `defaults` | object | no     | Shared overlay properties (deep-merged into each overlay). |
| `overlays` | array  | yes    | List of overlays to render, in any order.         |

### `canvas`

Describes the output frame (typically the 9:16 Shorts frame) and how a source video whose
aspect ratio doesn't match is fit into it.

| Field    | Type   | Default | Description |
|----------|--------|---------|-------------|
| `width`  | int    | `1080`  | Output frame width, px. |
| `height` | int    | `1920`  | Output frame height, px. |
| `fit`    | enum   | `letterbox_blur` | How to fit the source into the canvas. See below. |

`fit` options:

- **`letterbox_blur`** — the source is scaled to fit fully inside the canvas (nothing
  cropped), and any remaining space (top/bottom or left/right, whichever applies) is filled
  with a blurred, zoomed copy of the same source rather than plain bars. This is the
  default and the recommended choice for sheet-music sources, since it never crops staves.
- **`letterbox_black`** — same fitting, but the padding is solid black instead of blurred
  video.
- **`crop`** — the source fills the canvas edge-to-edge with no padding, cropping whatever
  doesn't fit. Not recommended for scores, since it can permanently cut off staves.

### `defaults`

Any of the per-overlay fields below (everything except `text`, `emoji`, `start`, `end`) can
be set here once and will apply to every overlay that doesn't explicitly override them.
Overlay-level values are **deep-merged** into `defaults` — e.g. overriding just
`style.color` on one overlay does not require repeating `style.font`, `style.shadow`, etc.

`defaults` is optional. If omitted, every overlay must fully specify its own fields.

## Overlay fields

Each entry in `overlays` describes one timed piece of text.

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `text`     | string | yes      | The overlay text. |
| `emoji`    | string | no       | One or more emoji, rendered after the text. |
| `start`    | float  | yes      | Start time, in seconds, from the start of the video. |
| `end`      | float  | yes      | End time, in seconds. |
| `fade_in`  | float  | no       | Fade-in duration in seconds. Falls back to `defaults`. |
| `fade_out` | float  | no       | Fade-out duration in seconds. Falls back to `defaults`. |
| `position` | object | no       | Where the overlay is placed. Falls back to `defaults`. |
| `style`    | object | no       | Text/background styling. Deep-merged with `defaults.style`. |

### `position`

| Field | Type            | Description |
|-------|-----------------|-------------|
| `x`   | `center` \| `left` \| `right` \| float (0–1) | Horizontal position. A float is a fraction of the canvas width. |
| `y`   | `top` \| `center` \| `bottom` \| float (0–1) | Vertical position. A float is a fraction of the canvas height. |

For YouTube Shorts, keep `y` roughly within **0.05–0.80** and `x` away from the rightmost
**~15%** of the frame, to avoid the app's own like/comment/share UI and title area covering
the overlay.

### `style`

| Field        | Type   | Description |
|--------------|--------|-------------|
| `font`       | string | Font family name. |
| `size`       | int    | Font size, px. |
| `color`      | string | Hex color, e.g. `"#22c55e"`. |
| `bold`       | bool   | Bold text. |
| `italic`     | bool   | Italic text. |
| `outline`    | object | See below. Set `width: 0` for no outline. |
| `shadow`     | object | See below. Omit for no shadow. |
| `background` | object | See below. Omit or set `style: none` for no background. |

#### `style.outline`

| Field   | Type   | Description |
|---------|--------|-------------|
| `color` | string | Outline color. |
| `width` | int    | Outline width, px. `0` disables it. |

#### `style.shadow`

A drop shadow, separate from the outline (offset + blur, sits behind the text).

| Field      | Type   | Description |
|------------|--------|-------------|
| `color`    | string | Shadow color. |
| `opacity`  | float  | 0–1. |
| `offset_x` | int    | Horizontal offset, px. |
| `offset_y` | int    | Vertical offset, px. |
| `blur`     | int    | Blur radius, px. `0` = hard-edged offset shadow. |

#### `style.background`

A soft panel behind the text. Uses a feathered gradient rather than a hard rectangle, so
there's no rounded-corner rendering to get right — the edges simply fade to transparent.

| Field     | Type   | Description |
|-----------|--------|-------------|
| `style`   | `solid` \| `gradient_fade` \| `none` | Background type. |
| `color`   | string | Background color. |
| `opacity` | float  | Peak opacity (at the center, for `gradient_fade`). |
| `sizing`  | `auto` \| `fixed` | `auto` sizes the box to the text; `fixed` uses `width`/`height` for every overlay regardless of text length. |
| `padding` | int    | (`sizing: auto` only) Margin around the text bounding box before the fade starts. |
| `width`   | int    | (`sizing: fixed` only) Fixed box width, px. |
| `height`  | int    | (`sizing: fixed` only) Fixed box height, px. |
| `feather` | int    | How gradual the fade to transparent is, px. Larger = softer. |

With `sizing: fixed`, text wider than the box is shrunk to fit rather than clipped or
allowed to overflow.

## Notes

- Timestamps are in seconds (floats), matching what you'd read off a video player —
  no frame counting.
- `position` values as floats are always fractions of the canvas (post-fit) dimensions,
  so overlays line up correctly regardless of the source video's original resolution.
- Overlapping overlay time ranges and the exact emoji-rendering strategy (native text
  rendering vs. pre-rendered glyph images) are implementation details of the renderer,
  not the DSL — this document covers the input format only.
