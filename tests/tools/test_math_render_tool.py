from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_render_math_invokes_renderer_and_returns_media_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import math_render_tool

    calls = []

    def fake_which(name):
        assert name == "node"
        return "/usr/bin/node"

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        request = json.loads(kwargs["input"])
        Path(request["output_png"]).parent.mkdir(parents=True, exist_ok=True)
        Path(request["output_png"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-data")
        Path(request["output_svg"]).write_text("<svg></svg>")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"success": True, "width": 598, "height": 191, "size": 9}),
            stderr="",
        )

    monkeypatch.setattr(math_render_tool.shutil, "which", fake_which)
    monkeypatch.setattr(math_render_tool.subprocess, "run", fake_run)

    result = json.loads(
        math_render_tool.render_math_tool(
            r"\left[\frac{(2y^4x^2)^6}{(xy)^3}\right] \times \left(\frac{x^5}{y}\right)^2"
        )
    )

    assert result["success"] is True
    assert result["cached"] is False
    assert result["media"].startswith("MEDIA:")
    assert result["path"].startswith(str(tmp_path / "math-renders"))
    assert result["path"].endswith(".png")
    assert Path(result["path"]).read_bytes() == b"\x89PNG\r\n\x1a\n" + b"png-data"
    assert result["width"] == 598
    assert result["height"] == 191
    assert len(calls) == 1


def test_render_math_reuses_cached_png_without_renderer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import math_render_tool

    monkeypatch.setattr(math_render_tool.shutil, "which", lambda name: "/usr/bin/node")
    key = math_render_tool._cache_key(
        latex=r"x^2",
        display=True,
        density=300,
        padding=20,
        background="#ffffff",
    )
    cached = tmp_path / "math-renders" / f"{key}.png"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"\x89PNG\r\n\x1a\n" + b"existing")

    def fail_run(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("renderer should not be called for cached output")

    monkeypatch.setattr(math_render_tool.subprocess, "run", fail_run)

    result = json.loads(math_render_tool.render_math_tool(r"x^2"))

    assert result["success"] is True
    assert result["cached"] is True
    assert result["path"] == str(cached)
    assert result["media"] == f"MEDIA:{cached}"


def test_render_math_reports_renderer_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import math_render_tool

    monkeypatch.setattr(math_render_tool.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(
        math_render_tool.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"success": False, "error": "bad latex"}),
            stderr="stack trace",
        ),
    )

    result = json.loads(math_render_tool.render_math_tool(r"\not-a-real-command"))

    assert result["success"] is False
    assert "bad latex" in result["error"]
    assert result["latex"] == r"\not-a-real-command"


def test_render_math_toolset_registered():
    from tools.math_render_tool import RENDER_MATH_SCHEMA
    from toolsets import TOOLSETS, resolve_toolset

    assert RENDER_MATH_SCHEMA["name"] == "render_math"
    assert "render_math" in TOOLSETS["math"]["tools"]
    assert "render_math" in resolve_toolset("hermes-telegram")
