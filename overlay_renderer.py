"""Render the overlay DSL described in README.md with FFmpeg."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


class OverlayConfigError(ValueError):
    """Raised when the YAML document does not match the overlay DSL."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OverlayConfigError(f"{name} must be an object")
    return value


def number(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OverlayConfigError(f"{name} must be a number")
    converted = float(value)
    if minimum is not None and converted < minimum:
        raise OverlayConfigError(f"{name} must be at least {minimum}")
    return converted


def color(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("#"):
        raise OverlayConfigError(f"{name} must be a hex color such as #ffffff")
    hex_value = value[1:]
    if len(hex_value) not in (3, 6, 8) or any(char not in "0123456789abcdefABCDEF" for char in hex_value):
        raise OverlayConfigError(f"{name} must be a valid hex color")
    return value


def validate_document(document: Any) -> dict[str, Any]:
    root = require_mapping(document, "document")
    for key in ("video", "output", "overlays"):
        if key not in root:
            raise OverlayConfigError(f"missing required field: {key}")
    if not isinstance(root["video"], str) or not isinstance(root["output"], str):
        raise OverlayConfigError("video and output must be strings")
    if not isinstance(root["overlays"], list):
        raise OverlayConfigError("overlays must be an array")

    canvas = require_mapping(root.get("canvas", {}), "canvas")
    canvas["width"] = int(number(canvas.get("width", 1080), "canvas.width", 1))
    canvas["height"] = int(number(canvas.get("height", 1920), "canvas.height", 1))
    if canvas.get("fit", "letterbox_blur") not in {"letterbox_blur", "letterbox_black", "crop"}:
        raise OverlayConfigError("canvas.fit must be letterbox_blur, letterbox_black, or crop")
    root["canvas"] = canvas
    root["defaults"] = require_mapping(root.get("defaults", {}), "defaults")

    for index, overlay in enumerate(root["overlays"]):
        item = require_mapping(overlay, f"overlays[{index}]")
        for key in ("text", "start", "end"):
            if key not in item:
                raise OverlayConfigError(f"overlays[{index}] is missing {key}")
        if not isinstance(item["text"], str):
            raise OverlayConfigError(f"overlays[{index}].text must be a string")
        start = number(item["start"], f"overlays[{index}].start", 0)
        end = number(item["end"], f"overlays[{index}].end", 0)
        if end <= start:
            raise OverlayConfigError(f"overlays[{index}].end must be greater than start")
        item["start"], item["end"] = start, end
    return root


def ffmpeg_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def ffmpeg_color(value: str, opacity: float = 1.0) -> str:
    return f"0x{value[1:]}@{opacity:g}"


WINDOWS_FONT_FILES: dict[str, dict[tuple[bool, bool], str]] = {
    "arial": {
        (False, False): "arial.ttf",
        (True, False): "arialbd.ttf",
        (False, True): "ariali.ttf",
        (True, True): "arialbi.ttf",
    },
    "calibri": {
        (False, False): "calibri.ttf",
        (True, False): "calibrib.ttf",
        (False, True): "calibrii.ttf",
        (True, True): "calibriz.ttf",
    },
    "segoe ui": {
        (False, False): "segoeui.ttf",
        (True, False): "segoeuib.ttf",
        (False, True): "segoeuii.ttf",
        (True, True): "segoeuiz.ttf",
    },
}


EMOJI_FONT_WINDOWS = Path("C:/Windows/Fonts/seguiemj.ttf")


def resolve_font_file(font: str, bold: bool = False, italic: bool = False) -> Path | None:
    font_path = Path(font)
    if font_path.is_file():
        return font_path.resolve()
    if sys.platform == "win32":
        variants = WINDOWS_FONT_FILES.get(font.casefold())
        if variants:
            windows_font = Path("C:/Windows/Fonts") / variants.get((bold, italic), variants[(False, False)])
            if windows_font.is_file():
                return windows_font
    return None


def font_option(font: str, bold: bool = False, italic: bool = False) -> str:
    resolved = resolve_font_file(font, bold, italic)
    if resolved:
        return f"fontfile='{ffmpeg_escape(str(resolved))}'"
    # fontconfig-style pattern lets drawtext resolve style without a font file
    style = " ".join(part for part in ("Bold" if bold else "", "Italic" if italic else "") if part)
    pattern = f"{font}:style={style}" if style else font
    return f"font='{ffmpeg_escape(pattern)}'"


def measure_text(font: str, bold: bool, italic: bool, size: int, text: str) -> tuple[int, int]:
    """Approximate rendered pixel size of text, used to size/position layers consistently."""
    resolved = resolve_font_file(font, bold, italic)
    try:
        from PIL import ImageFont
        pil_font = ImageFont.truetype(str(resolved) if resolved else font, size)
        left, top, right, bottom = pil_font.getbbox(text)
        return max(right - left, 1), max(bottom - top, 1)
    except Exception:
        return max(int(size * 0.6 * len(text)), 1), int(size * 1.2)


def alpha_expression(start: float, end: float, fade_in: float, fade_out: float) -> str:
    expression = "1"
    if fade_in > 0:
        expression = f"if(lt(t\\,{start + fade_in:g})\\,(t-{start:g})/{fade_in:g}\\,{expression})"
    if fade_out > 0:
        expression = f"if(gt(t\\,{end - fade_out:g})\\,({end:g}-t)/{fade_out:g}\\,{expression})"
    return expression


def numeric_position(value: Any, axis: str, dimension: float, canvas_dimension: float) -> float:
    if value in ("center", "middle"):
        return (canvas_dimension - dimension) / 2
    if axis == "x" and value == "left":
        return 0.0
    if axis == "x" and value == "right":
        return canvas_dimension - dimension
    if axis == "y" and value == "top":
        return 0.0
    if axis == "y" and value == "bottom":
        return canvas_dimension - dimension
    fraction = number(value, f"position.{axis}")
    if not 0 <= fraction <= 1:
        raise OverlayConfigError(f"position.{axis} must be between 0 and 1")
    return fraction * canvas_dimension - dimension / 2


def build_canvas_filters(canvas: dict[str, Any]) -> tuple[list[str], str]:
    width, height = canvas["width"], canvas["height"]
    fit = canvas.get("fit", "letterbox_blur")
    if fit == "crop":
        return [f"scale={width}:{height}:force_original_aspect_ratio=increase", f"crop={width}:{height}"], "v0"
    if fit == "letterbox_black":
        return [f"scale={width}:{height}:force_original_aspect_ratio=decrease", f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"], "v0"
    return [
        "[0:v]split=2[blur_source][sharp_source]",
        f"[blur_source]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=20:2[blurred]",
        f"[sharp_source]scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black[sharp]",
        "[blurred][sharp]overlay=shortest=1[v0]",
    ], "v0"


def build_filter_graph(document: dict[str, Any]) -> str:
    canvas = document["canvas"]
    filters, current = build_canvas_filters(canvas)
    graph: list[str] = []
    if canvas.get("fit") == "letterbox_blur":
        graph.extend(filters)
    else:
        graph.append("[0:v]" + ",".join(filters) + "[v0]")

    for index, raw_overlay in enumerate(document["overlays"]):
        overlay = deep_merge(document["defaults"], raw_overlay)
        style = require_mapping(overlay.get("style", {}), f"overlays[{index}].style")
        position = require_mapping(overlay.get("position", {}), f"overlays[{index}].position")
        start, end = overlay["start"], overlay["end"]
        fade_in = number(overlay.get("fade_in", 0), f"overlays[{index}].fade_in", 0)
        fade_out = number(overlay.get("fade_out", 0), f"overlays[{index}].fade_out", 0)
        if fade_in + fade_out > end - start:
            raise OverlayConfigError(f"overlays[{index}] fade durations exceed its duration")

        font = str(style.get("font", "Arial"))
        bold = bool(style.get("bold"))
        italic = bool(style.get("italic"))
        size = int(number(style.get("size", 48), f"overlays[{index}].style.size", 1))
        text = overlay["text"]
        emoji = str(overlay["emoji"]) if overlay.get("emoji") else None
        emoji_gap = max(size // 4, 4)
        emoji_font = str(EMOJI_FONT_WINDOWS) if EMOJI_FONT_WINDOWS.is_file() else "Segoe UI Emoji"

        text_lines = text.splitlines() or [""]
        line_metrics = [measure_text(font, bold, italic, size, line) for line in text_lines]
        text_w = max(width for width, _ in line_metrics)
        text_h = sum(height for _, height in line_metrics)
        if emoji:
            emoji_w, emoji_h = measure_text(emoji_font, False, False, size, emoji)
            total_w, total_h = text_w + emoji_gap + emoji_w, max(text_h, emoji_h)
        else:
            emoji_w = emoji_h = 0
            total_w, total_h = text_w, text_h

        background = require_mapping(style.get("background", {}), f"overlays[{index}].style.background")
        panel_w = panel_h = feather = 0
        panel_color = ""
        if background and background.get("style", "none") != "none":
            background_style = background.get("style", "solid")
            if background_style not in {"solid", "gradient_fade"}:
                raise OverlayConfigError(f"unsupported background style: {background_style}")
            opacity = number(background.get("opacity", 0.5), "background.opacity")
            if not 0 <= opacity <= 1:
                raise OverlayConfigError("background.opacity must be between 0 and 1")
            feather = int(number(background.get("feather", 24 if background_style == "gradient_fade" else 0), "background.feather", 0))
            sizing = background.get("sizing", "auto")
            if sizing == "fixed":
                panel_w = int(number(background.get("width"), "background.width", 1))
                panel_h = int(number(background.get("height"), "background.height", 1))
                margin = 12
                available_w = max(panel_w - 2 * margin, 1)
                if total_w > available_w:
                    scale = available_w / total_w
                    size = max(int(size * scale), 6)
                    line_metrics = [measure_text(font, bold, italic, size, line) for line in text_lines]
                    text_w = max(width for width, _ in line_metrics)
                    text_h = sum(height for _, height in line_metrics)
                    if emoji:
                        emoji_w, emoji_h = measure_text(emoji_font, False, False, size, emoji)
                        total_w, total_h = text_w + emoji_gap + emoji_w, max(text_h, emoji_h)
                    else:
                        total_w, total_h = text_w, text_h
            elif sizing == "auto":
                padding = int(number(background.get("padding", 16), "background.padding", 0))
                panel_w, panel_h = total_w + 2 * padding, total_h + 2 * padding
            else:
                raise OverlayConfigError(f"unsupported background sizing: {sizing}")
            panel_color = ffmpeg_color(color(background.get("color", "#000000"), "background.color"), opacity)

        anchor_x = numeric_position(position.get("x", "center"), "x", total_w, canvas["width"])
        anchor_y = numeric_position(position.get("y", "center"), "y", total_h, canvas["height"])
        text_x, text_y = anchor_x, anchor_y + (total_h - text_h) / 2
        emoji_x, emoji_y = anchor_x + text_w + emoji_gap, anchor_y + (total_h - emoji_h) / 2

        if panel_w and panel_h:
            panel_x = numeric_position(position.get("x", "center"), "x", panel_w, canvas["width"])
            panel_y = numeric_position(position.get("y", "center"), "y", panel_h, canvas["height"])
            source_w, source_h = panel_w + 2 * feather, panel_h + 2 * feather
            panel_src, box_label = f"bgsrc{index}", f"bgbox{index}"
            blur_label = f"bgblur{index}" if feather > 0 else box_label
            next_bg = f"vbg{index}"
            fade_parts = []
            if fade_in > 0:
                fade_parts.append(f"fade=t=in:st={start:g}:d={fade_in:g}:alpha=1")
            if fade_out > 0:
                fade_parts.append(f"fade=t=out:st={end - fade_out:g}:d={fade_out:g}:alpha=1")
            fade_chain = "".join(f",{part}" for part in fade_parts)
            graph.append(f"color=size={source_w}x{source_h}:color=black@0.0:duration={end + 0.5:.3f},format=rgba[{panel_src}]")
            graph.append(f"[{panel_src}]drawbox=x={feather}:y={feather}:w={panel_w}:h={panel_h}:color={panel_color}:t=fill:replace=1{fade_chain}[{box_label}]")
            if feather > 0:
                graph.append(f"[{box_label}]boxblur=luma_radius={feather}:luma_power=1:chroma_radius={feather}:chroma_power=1:alpha_radius={feather}:alpha_power=1[{blur_label}]")
            graph.append(f"[{current}][{blur_label}]overlay=x={panel_x - feather:g}:y={panel_y - feather:g}:enable='between(t,{start:g},{end:g})'[{next_bg}]")
            current = next_bg

        options = [
            font_option(font, bold, italic),
            f"fontsize={size}",
            f"fontcolor={ffmpeg_color(color(style.get('color', '#ffffff'), f'overlays[{index}].style.color'))}",
            f"x={text_x:g}",
            f"y={text_y:g}",
            f"alpha='{alpha_expression(start, end, fade_in, fade_out)}'",
            f"enable='between(t,{start:g},{end:g})'",
        ]
        outline = require_mapping(style.get("outline", {}), f"overlays[{index}].style.outline")
        outline_width = int(number(outline.get("width", 0), "outline.width", 0))
        if outline_width:
            options.extend([f"borderw={outline_width}", f"bordercolor={ffmpeg_color(color(outline.get('color', '#000000'), 'outline.color'))}"])
        shadow = require_mapping(style.get("shadow", {}), f"overlays[{index}].style.shadow")
        if shadow:
            shadow_opacity = number(shadow.get("opacity", 0.5), "shadow.opacity")
            if not 0 <= shadow_opacity <= 1:
                raise OverlayConfigError("shadow.opacity must be between 0 and 1")
            options.extend([
                f"shadowcolor={ffmpeg_color(color(shadow.get('color', '#000000'), 'shadow.color'), shadow_opacity)}",
                f"shadowx={int(number(shadow.get('offset_x', 2), 'shadow.offset_x'))}",
                f"shadowy={int(number(shadow.get('offset_y', 2), 'shadow.offset_y'))}",
            ])
        line_y = text_y
        for line_index, (line, (line_w, line_h)) in enumerate(zip(text_lines, line_metrics)):
            line_options = options.copy()
            line_options[line_options.index(f"x={text_x:g}")] = f"x={text_x + (text_w - line_w) / 2:g}"
            line_options[line_options.index(f"y={text_y:g}")] = f"y={line_y:g}"
            escaped_line = ffmpeg_escape(line)
            next_label = f"v{index + 1}_{line_index}"
            graph.append(f"[{current}]drawtext=text='{escaped_line}':" + ":".join(line_options) + f"[{next_label}]")
            current = next_label
            line_y += line_h

        if emoji:
            emoji_options = [
                f"fontfile='{ffmpeg_escape(emoji_font)}'" if Path(emoji_font).is_file() else f"font='{ffmpeg_escape(emoji_font)}'",
                f"fontsize={size}",
                "fontcolor=white",
                f"x={emoji_x:g}",
                f"y={emoji_y:g}",
                f"alpha='{alpha_expression(start, end, fade_in, fade_out)}'",
                f"enable='between(t,{start:g},{end:g})'",
            ]
            escaped_emoji = ffmpeg_escape(emoji)
            emoji_label = f"vemoji{index}"
            graph.append(f"[{current}]drawtext=text='{escaped_emoji}':" + ":".join(emoji_options) + f"[{emoji_label}]")
            current = emoji_label
    graph.append(f"[{current}]format=yuv420p[vout]")
    return ";".join(graph)



def render(spec_path: Path, dry_run: bool = False) -> None:
    try:
        document = validate_document(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
        input_path = (spec_path.parent / document["video"]).resolve()
        output_path = (spec_path.parent / document["output"]).resolve()
        if not input_path.is_file():
            raise OverlayConfigError(f"input video does not exist: {input_path}")
        if shutil.which("ffmpeg") is None:
            raise OverlayConfigError("ffmpeg was not found on PATH")
        command = [
            "ffmpeg", "-y", "-i", str(input_path), "-filter_complex", build_filter_graph(document),
            "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(output_path),
        ]
        if dry_run:
            print(json.dumps({"command": command, "filter_complex": command[command.index("-filter_complex") + 1]}, indent=2))
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"overlay-render: ffmpeg failed with exit code {error.returncode}") from error
    except (OSError, yaml.YAMLError, OverlayConfigError) as error:
        raise SystemExit(f"overlay-render: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a timed overlay YAML file with FFmpeg.")
    parser.add_argument("spec", type=Path, help="path to the overlay YAML file")
    parser.add_argument("--dry-run", action="store_true", help="print the generated FFmpeg command without running it")
    args = parser.parse_args()
    render(args.spec, args.dry_run)


if __name__ == "__main__":
    main()