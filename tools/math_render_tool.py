"""Render LaTeX math to tight PNG/SVG files for messaging platforms.

The public tool is Python-native for Hermes, but delegates typesetting to a
small local MathJax + sharp Node helper. This is deterministic rendering, not
image generation: no model call, no browser, no external service.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from tools.registry import registry

_RENDERER_VERSION = "mathjax-sharp-v1"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_RENDERER_SCRIPT = Path(__file__).resolve().parent / "helpers" / "render_math.js"


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _render_dir() -> Path:
    return get_hermes_home() / "math-renders"


def _is_valid_png(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _cleanup_paths(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _temporary_output_path(final_path: Path) -> Path:
    return final_path.with_name(
        f".{final_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp{final_path.suffix}"
    )


def _cache_key(*, latex: str, display: bool, density: int, padding: int, background: str) -> str:
    payload = json.dumps(
        {
            "version": _RENDERER_VERSION,
            "latex": latex,
            "display": display,
            "density": density,
            "padding": padding,
            "background": background,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def check_math_render_requirements() -> bool:
    """Return True when the local Node renderer and packages are available."""
    node = shutil.which("node")
    if not node or not _RENDERER_SCRIPT.exists():
        return False
    try:
        probe = subprocess.run(
            [
                node,
                "-e",
                "require.resolve('mathjax-full'); require.resolve('sharp');",
            ],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def render_math_tool(
    latex: str,
    display: bool = True,
    density: int = 300,
    padding: int = 20,
    background: str = "#ffffff",
) -> str:
    """Render LaTeX math to a tightly cropped PNG and return a MEDIA tag."""
    latex = (latex or "").strip()
    if not latex:
        return _json_result({"success": False, "error": "latex is required"})

    density = _clamp_int(density, default=300, minimum=72, maximum=600)
    padding = _clamp_int(padding, default=20, minimum=0, maximum=120)
    background = (background or "#ffffff").strip() or "#ffffff"

    node = shutil.which("node")
    if not node:
        return _json_result({"success": False, "error": "node is required to render math"})
    if not _RENDERER_SCRIPT.exists():
        return _json_result({"success": False, "error": f"renderer script not found: {_RENDERER_SCRIPT}"})

    out_dir = _render_dir()
    key = _cache_key(
        latex=latex,
        display=bool(display),
        density=density,
        padding=padding,
        background=background,
    )
    png_path = out_dir / f"{key}.png"
    svg_path = out_dir / f"{key}.svg"

    if png_path.exists():
        if _is_valid_png(png_path):
            return _json_result(
                {
                    "success": True,
                    "path": str(png_path),
                    "image": str(png_path),
                    "media": f"MEDIA:{png_path}",
                    "svg": str(svg_path) if svg_path.exists() else None,
                    "latex": latex,
                    "cached": True,
                }
            )
        _cleanup_paths(png_path, svg_path)

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_png_path = _temporary_output_path(png_path)
    tmp_svg_path = _temporary_output_path(svg_path)
    _cleanup_paths(tmp_png_path, tmp_svg_path)
    request = {
        "latex": latex,
        "display": bool(display),
        "density": density,
        "padding": padding,
        "background": background,
        "output_png": str(tmp_png_path),
        "output_svg": str(tmp_svg_path),
    }

    try:
        proc = subprocess.run(
            [node, str(_RENDERER_SCRIPT)],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=str(_REPO_ROOT),
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _cleanup_paths(tmp_png_path, tmp_svg_path)
        return _json_result({"success": False, "error": "math render timed out"})
    except OSError as exc:
        _cleanup_paths(tmp_png_path, tmp_svg_path)
        return _json_result({"success": False, "error": f"failed to run renderer: {exc}"})

    try:
        rendered = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        rendered = {"success": False, "error": "renderer returned invalid JSON"}

    if proc.returncode != 0 or not rendered.get("success") or not _is_valid_png(tmp_png_path):
        _cleanup_paths(tmp_png_path, tmp_svg_path)
        return _json_result(
            {
                "success": False,
                "error": rendered.get("error") or f"renderer exited with {proc.returncode}",
                "stderr": proc.stderr[-2000:],
                "latex": latex,
            }
        )

    tmp_png_path.replace(png_path)
    tmp_svg_path.replace(svg_path)

    return _json_result(
        {
            "success": True,
            "path": str(png_path),
            "image": str(png_path),
            "media": f"MEDIA:{png_path}",
            "svg": str(svg_path),
            "latex": latex,
            "cached": False,
            "width": rendered.get("width"),
            "height": rendered.get("height"),
            "size": rendered.get("size"),
        }
    )


RENDER_MATH_SCHEMA = {
    "name": "render_math",
    "description": (
        "Render a LaTeX math expression to a tightly cropped local PNG/SVG for "
        "Telegram or other messaging platforms. Returns a MEDIA:/path.png tag; "
        "use this instead of raw LaTeX when the chat platform cannot render math."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "latex": {
                "type": "string",
                "description": "LaTeX math body, without surrounding $...$ or \\[...\\] delimiters.",
            },
            "display": {
                "type": "boolean",
                "description": "Render in display mode. Defaults to true for standalone formulas.",
                "default": True,
            },
            "density": {
                "type": "integer",
                "description": "Rasterization density for PNG output, clamped to 72-600. Default 300.",
                "default": 300,
            },
            "padding": {
                "type": "integer",
                "description": "White padding in pixels after trimming, clamped to 0-120. Default 20.",
                "default": 20,
            },
            "background": {
                "type": "string",
                "description": "PNG background color. Default #ffffff.",
                "default": "#ffffff",
            },
        },
        "required": ["latex"],
    },
}


def _handle_render_math(args, **kw):
    return render_math_tool(
        latex=args.get("latex", ""),
        display=args.get("display", True),
        density=args.get("density", 300),
        padding=args.get("padding", 20),
        background=args.get("background", "#ffffff"),
    )


registry.register(
    name="render_math",
    toolset="math",
    schema=RENDER_MATH_SCHEMA,
    handler=_handle_render_math,
    check_fn=check_math_render_requirements,
    requires_env=[],
    is_async=False,
    emoji="🧮",
)
