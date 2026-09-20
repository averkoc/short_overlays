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


def font_option(font: str) -> str:
    font_path = Path(font)
    if font_path.is_file():
        return f"fontfile='{ffmpeg_escape(str(font_path.resolve()))}'"
    if sys.platform == "win32":
        windows_font = Path("C:/Windows/Fonts") / {
            "arial": "arial.ttf",
            "arial bold": "arialbd.ttf",
            "arial italic": "ariali.ttf",
            "calibri": "calibri.ttf",
            "segoe ui": "segoeui.ttf",
        }.get(font.casefold(), "")
        if windows_font.is_file():
            return f"fontfile='{ffmpeg_escape(str(windows_font))}'"
    return f"font='{ffmpeg_escape(font)}'"


def alpha_expression(start: float, end: float, fade_in: float, fade_out: float) -> str:
    expression = "1"
    if fade_in > 0:
        expression = f"if(lt(t\\,{start + fade_in:g})\\,(t-{start:g})/{fade_in:g}\\,{expression})"
    if fade_out > 0:
        expression = f"if(gt(t\\,{end - fade_out:g})\\,({end:g}-t)/{fade_out:g}\\,{expression})"
    return expression


def position_expression(value: Any, axis: str, text_dimension: str, canvas_dimension: str) -> str:
    if value in ("center", "middle"):
        return f"({canvas_dimension}-{text_dimension})/2"
    if axis == "x" and value == "left":
        return "0"
    if axis == "x" and value == "right":
        return f"{canvas_dimension}-{text_dimension}"
    if axis == "y" and value == "top":
        return "0"
    if axis == "y" and value == "bottom":
        return f"{canvas_dimension}-{text_dimension}"
    fraction = number(value, f"position.{axis}")
    if not 0 <= fraction <= 1:
        raise OverlayConfigError(f"position.{axis} must be between 0 and 1")
    return f"{fraction:g}*{canvas_dimension}-{text_dimension}/2"


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
        text = overlay["text"]
        if overlay.get("emoji"):
            text += " " + str(overlay["emoji"])
        options = [
            font_option(str(style.get("font", "Arial"))),
            f"fontsize={int(number(style.get('size', 48), f'overlays[{index}].style.size', 1))}",
            f"fontcolor={ffmpeg_color(color(style.get('color', '#ffffff'), f'overlays[{index}].style.color'))}",
            f"x={position_expression(position.get('x', 'center'), 'x', 'text_w', 'w')}",
            f"y={position_expression(position.get('y', 'center'), 'y', 'text_h', 'h')}",
            f"alpha='{alpha_expression(start, end, fade_in, fade_out)}'",
            f"enable='between(t,{start:g},{end:g})'",
        ]
        if style.get("bold"):
            options.append("fontvariant=bold")
        if style.get("italic"):
            options.append("fontvariant=italic")
        outline = require_mapping(style.get("outline", {}), f"overlays[{index}].style.outline")
        outline_width = int(number(outline.get("width", 0), "outline.width", 0))
        if outline_width:
            options.extend([f"borderw={outline_width}", f"bordercolor={ffmpeg_color(color(outline.get('color', '#000000'), 'outline.color'))}"])
        shadow = require_mapping(style.get("shadow", {}), f"overlays[{index}].style.shadow")
        if shadow:
            opacity = number(shadow.get("opacity", 0.5), "shadow.opacity")
            if not 0 <= opacity <= 1:
                raise OverlayConfigError("shadow.opacity must be between 0 and 1")
            options.extend([
                f"shadowcolor={ffmpeg_color(color(shadow.get('color', '#000000'), 'shadow.color'), opacity)}",
                f"shadowx={int(number(shadow.get('offset_x', 2), 'shadow.offset_x'))}",
                f"shadowy={int(number(shadow.get('offset_y', 2), 'shadow.offset_y'))}",
            ])
        background = require_mapping(style.get("background", {}), f"overlays[{index}].style.background")
        if background and background.get("style", "none") != "none":
            background_style = background.get("style", "solid")
            if background_style not in {"solid", "gradient_fade"}:
                raise OverlayConfigError(f"unsupported background style: {background_style}")
            opacity = number(background.get("opacity", 0.5), "background.opacity")
            if not 0 <= opacity <= 1:
                raise OverlayConfigError("background.opacity must be between 0 and 1")
            if background.get("sizing", "auto") == "fixed":
                options.extend([
                    "box=1",
                    f"boxw={int(number(background.get('width'), 'background.width', 1))}",
                    f"boxh={int(number(background.get('height'), 'background.height', 1))}",
                ])
            else:
                options.extend(["box=1", f"boxborderw={int(number(background.get('padding', 0), 'background.padding', 0))}"])
            options.append(f"boxcolor={ffmpeg_color(color(background.get('color', '#000000'), 'background.color'), opacity)}")
        escaped_text = ffmpeg_escape(text.replace("\n", "\\n"))
        next_label = f"v{index + 1}"
        graph.append(f"[{current}]drawtext=text='{escaped_text}':" + ":".join(options) + f"[{next_label}]")
        current = next_label
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