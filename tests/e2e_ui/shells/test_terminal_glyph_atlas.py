"""E2E: the Terminal view must keep drawing already-correct cells correctly
while the WebGL glyph atlas repacks under heavy CJK churn.

A long CJK-heavy pane pushes thousands of unique glyphs through xterm's WebGL
texture atlas. Once the atlas reaches its page cap, ``min(32,
MAX_TEXTURE_IMAGE_UNITS)``, it repacks — merging and deleting pages and
recycling slot indices. ``@xterm/addon-webgl`` 0.19.0 re-uploads a texture
slot only when ``pages[i].version !== _atlasTextures[i].version``, so a page
swapped into a slot whose version counter coincidentally matches is never
re-uploaded. The slot then keeps a stale texture, and the forced full repaint
draws already-correct cells — a status footer, a sidebar — with the wrong
glyphs even though the pane bytes underneath are correct.

``test_atlas_page_repack_rebinds_swapped_textures`` drives the app's bundled
renderer directly and detects that stale-slot condition deterministically.
``test_static_rows_survive_cjk_glyph_churn`` drives the same renderer through
the real app terminal; its visible corruption depends on which glyph lands on
a stale slot, so it is a user-journey/recording driver rather than a
deterministic check.
``test_shared_atlas_invalidates_each_renderer_once`` covers the pooled-atlas
side: clearing or repacking an atlas shared by several terminals must refresh
every renderer once and then return to incremental redraws.
"""

from __future__ import annotations

import io
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import Page, expect

from tests.e2e_ui.shells.test_new_shell import _open_new_shell

_ROOT = Path(__file__).resolve().parents[3]
_XTERM_LIB = _ROOT / "web" / "node_modules" / "@xterm" / "xterm"
_WEBGL_LIB = _ROOT / "web" / "node_modules" / "@xterm" / "addon-webgl"
# /tmp is private per sandbox, so the shell and the test share the worktree.
_SCRATCH_ROOT = _ROOT / ".omnigent" / "e2e-scratch"

_ROW1 = "Build · Claude Fable 5.1 (thinking/xhigh)  JSON parsing failed: Text:  227 231 tokens"
_ROW2 = "How remove the facilitator-only open-items section from the participant handbook"

_FLOOD_WINDOW_S = 150.0
_SAMPLE_S = 1.0
_PIXEL_TOLERANCE = 16
_MIN_CHANGED_PIXELS = 25

# Present a GPU with 8 texture units so the atlas page cap is 8 and CJK churn
# forces page merges within seconds, matching the reported sub-32-unit GPU.
_CLAMP_TEXTURE_UNITS = """
(() => {
  const MAX_TEXTURE_IMAGE_UNITS = 0x8872;
  for (const proto of [WebGLRenderingContext.prototype, WebGL2RenderingContext.prototype]) {
    const orig = proto.getParameter;
    proto.getParameter = function (pname) {
      const value = orig.call(this, pname);
      return pname === MAX_TEXTURE_IMAGE_UNITS ? Math.min(8, value) : value;
    };
  }
})();
"""

_FLOOD_SCRIPT = f"""\
import os, subprocess, sys, time

ROW1 = {_ROW1!r}
ROW2 = {_ROW2!r}
state, max_s = sys.argv[1], float(sys.argv[2])
os.makedirs(state, exist_ok=True)
out = sys.stdout
size = os.get_terminal_size()
cols, rows = size.columns, size.lines
with open(os.path.join(state, "size"), "w") as f:
    f.write(f"{{cols}} {{rows}}\\n")

# Autowrap off and cursor hidden so nothing scrolls or repaints rows 1-2.
out.write("\\x1b[?25l\\x1b[?7l\\x1b[2J\\x1b[H")
out.write("\\x1b[1;1H\\x1b[2K\\x1b[1;36m" + ROW1[:5] + "\\x1b[22;39m" + ROW1[5:41]
          + "  \\x1b[2;31m" + ROW1[43:69] + "\\x1b[22;39m  \\x1b[38;5;214m"
          + ROW1[71:] + "\\x1b[0m")
out.write("\\x1b[2;1H\\x1b[2K\\x1b[2;38;5;250m" + ROW2 + "\\x1b[0m")
out.flush()

pane = os.environ.get("TMUX_PANE")

def capture(wave):
    if pane:
        try:
            txt = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception as exc:
            txt = f"CAPTURE_ERROR {{exc!r}}\\n"
    else:
        txt = "NO_TMUX\\n"
    tmp = os.path.join(state, "capture.txt.tmp")
    with open(tmp, "w") as f:
        f.write(f"# wave {{wave}}\\n" + txt)
    os.replace(tmp, os.path.join(state, "capture.txt"))

per_row = max(1, (cols - 2) // 2)
body_rows = range(4, rows)
cp, cp_end = 0x4E00, 0x9FFF
wave = 0
start = time.time()
capture(0)
while time.time() - start < max_s and not os.path.exists(os.path.join(state, "stop")):
    buf = []
    for r in body_rows:
        color = 16 + (wave * 7 + r * 13) % 216
        weight = ("\\x1b[1m", "\\x1b[2m", "")[(wave + r) % 3]
        chars = []
        for _ in range(per_row):
            chars.append(chr(cp))
            cp = 0x4E00 if cp >= cp_end else cp + 1
        sgr = f"\\x1b[{{r}};1H\\x1b[2K\\x1b[0m{{weight}}\\x1b[38;5;{{color}}m"
        buf.append(sgr + "".join(chars) + "\\x1b[0m")
    out.write("".join(buf))
    out.flush()
    wave += 1
    with open(os.path.join(state, "wave"), "w") as f:
        f.write(f"{{wave}}\\n")
    if wave % 50 == 0:
        capture(wave)
    time.sleep(0.05)
capture(wave)
out.write("\\x1b[?25h\\x1b[?7h")
out.flush()
"""

_WEBGL_INFO_JS = """
(screen) => {
  const canvases = Array.from(screen.querySelectorAll("canvas"));
  for (const c of canvases) {
    const gl = c.getContext("webgl2") || c.getContext("webgl");
    if (gl) {
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      return {
        webgl: true,
        canvases: canvases.length,
        maxTextureImageUnits: gl.getParameter(gl.MAX_TEXTURE_IMAGE_UNITS),
        renderer: dbg
          ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
          : gl.getParameter(gl.RENDERER),
      };
    }
  }
  return { webgl: false, canvases: canvases.length };
}
"""

_HARNESS_HTML = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="file://{xterm_css}">
<script src="file://{xterm_js}"></script>
<script src="file://{webgl_js}"></script>
<style>html,body{{margin:0;background:#000}}#t{{width:960px;height:480px}}</style>
</head><body><div id="t"></div></body></html>"""

_HARNESS_SETUP = """
() => {
  const term = new Terminal({
    cols: 100, rows: 30,
    minimumContrastRatio: 4.5,
    fontFamily: 'monospace',
  });
  term.open(document.getElementById('t'));
  const addon = new WebglAddon.WebglAddon();
  term.loadAddon(addon);
  window.term = term;
  window.addon = addon;
  return true;
}
"""

_HARNESS_INSTRUMENT = """
() => {
  const renderer = window.addon._renderer;
  const atlas = renderer._charAtlas;
  const glyphRenderer = renderer._glyphRenderer.value;
  if (!atlas || !glyphRenderer) return 'not-ready';
  // Cap pages at 8 (read live by the atlas) so repacks happen within seconds.
  atlas.constructor.maxAtlasPages = 8;
  window.__uploads = {};
  window.__merges = 0;
  const origBind = glyphRenderer._bindAtlasPageTexture;
  glyphRenderer._bindAtlasPageTexture = function (gl, a, i) {
    window.__uploads[i] = a.pages[i].canvas;
    return origBind.call(this, gl, a, i);
  };
  const atlasProto = Object.getPrototypeOf(atlas);
  const origMerge = atlasProto._mergePages;
  atlasProto._mergePages = function (...args) {
    window.__merges++;
    return origMerge.apply(this, args);
  };
  // A slot is stale when its texture was uploaded from a canvas that no
  // longer occupies the slot and the version counters match, so the
  // version-only rebind rule would never refresh it.
  window.__staleSlots = () => {
    const a = window.addon._renderer._charAtlas;
    const g = window.addon._renderer._glyphRenderer.value;
    const out = [];
    for (let i = 0; i < a.pages.length; i++) {
      const uploaded = window.__uploads[i];
      if (uploaded && uploaded !== a.pages[i].canvas
          && a.pages[i].version === g._atlasTextures[i].version) {
        out.push(i);
      }
    }
    return out;
  };
  return 'ok';
}
"""

_HARNESS_CHURN = r"""
() => {
  let cp = 0, wave = 0;
  window.__churn = setInterval(() => {
    const parts = [];
    for (let row = 4; row < 30; row++) {
      const color = 22 + (wave * 7) % 200;
      const weight = wave % 3 === 0 ? '1' : (wave % 3 === 1 ? '2' : '0');
      let line = `\x1b[${row};1H\x1b[0;${weight};38;5;${color}m`;
      for (let c = 0; c < 49; c++) {
        line += String.fromCharCode(0x4e00 + (cp % 20000));
        cp++;
      }
      parts.push(line);
    }
    window.term.write(parts.join(''));
    wave++;
  }, 50);
  return true;
}
"""


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    size = {"width": 1800, "height": 1000}
    return {**browser_context_args, "viewport": size, "record_video_size": size}


def test_atlas_page_repack_rebinds_swapped_textures(page: Page, tmp_path: Path) -> None:
    """Every atlas page slot swapped by a repack gets its texture re-uploaded.

    Drives the app's bundled ``@xterm/xterm`` + ``@xterm/addon-webgl`` directly
    (no server) and churns unique CJK glyphs until the atlas has repacked many
    times, asserting no texture slot is left bound to a page that moved away.
    """
    html_path = tmp_path / "atlas_harness.html"
    html_path.write_text(
        _HARNESS_HTML.format(
            xterm_css=_XTERM_LIB / "css" / "xterm.css",
            xterm_js=_XTERM_LIB / "lib" / "xterm.js",
            webgl_js=_WEBGL_LIB / "lib" / "addon-webgl.js",
        ),
        encoding="utf-8",
    )
    page.goto(f"file://{html_path}")
    page.evaluate(_HARNESS_SETUP)
    page.wait_for_timeout(500)

    # WebGL may be unavailable (no GPU context): the addon then never builds
    # an atlas, and there is nothing to exercise.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if page.evaluate(_HARNESS_INSTRUMENT) == "ok":
            break
        page.wait_for_timeout(250)
    else:
        raise AssertionError("WebGL glyph atlas never initialized")

    page.evaluate(_HARNESS_CHURN)

    stale: list[int] = []
    merges = 0
    started = time.monotonic()
    while time.monotonic() - started < 60.0:
        page.wait_for_timeout(500)
        merges = page.evaluate("() => window.__merges")
        candidates = page.evaluate("() => window.__staleSlots()")
        if candidates:
            # A slot legitimately lags between a repack and the next rendered
            # frame; only a bind that survives further frames is stale.
            page.wait_for_timeout(250)
            stale = page.evaluate("() => window.__staleSlots()")
            if stale:
                break
        if merges >= 30:
            break

    assert not stale, (
        f"glyph atlas left texture slots {stale} bound to pages that moved "
        f"away during a repack (after {merges} page merges); those slots draw "
        f"the wrong glyphs on every repaint"
    )
    assert merges >= 5, (
        f"atlas churn produced only {merges} page merges; the repack path was not exercised"
    )


def _changed_pixels(a: Image.Image, b: Image.Image) -> int:
    if a.size != b.size:
        return a.size[0] * a.size[1]
    diff = ImageChops.difference(a, b).convert("L")
    return sum(diff.histogram()[_PIXEL_TOLERANCE + 1 :])


def _read_pane_rows(state: Path) -> tuple[str, list[str]]:
    capture = state / "capture.txt"
    if not capture.exists():
        return "missing", []
    lines = capture.read_text().splitlines()
    if len(lines) < 3 or lines[1].startswith(("NO_TMUX", "CAPTURE_ERROR")):
        return "unavailable", lines[:2]
    return "ok", lines[1:3]


@pytest.mark.timeout(360)
def test_static_rows_survive_cjk_glyph_churn(
    request: pytest.FixtureRequest,
    terminal_session: tuple[str, str],
) -> None:
    """Two never-rewritten status rows stay pixel-identical during a CJK flood.

    The rail shell runs a script that draws two styled status rows, then
    rewrites every other row ~20 times a second with unique CJK glyphs in
    cycling colors and weights. The top strip is sampled once a second and
    compared with its settled baseline; a difference that persists on the next
    sample is a renderer defect, since the pane bytes for those rows are
    checked against the exact strings the script drew. Visible corruption here
    depends on which glyph lands on a stale slot, so this is the user-journey
    and recording driver; the harness test above is the deterministic check.
    """
    base_url, session_id = terminal_session
    scratch = _SCRATCH_ROOT / f"glyph-atlas-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True)
    script = scratch / "cjk_flood.py"
    script.write_text(_FLOOD_SCRIPT)
    state = scratch / "state"

    page: Page = request.getfixturevalue("page")
    try:
        page.add_init_script(_CLAMP_TEXTURE_UNITS)
        page.goto(f"{base_url}/c/{session_id}")
        _open_new_shell(page)

        rail = page.get_by_role("complementary", name="Workspace")
        terminal_view = rail.get_by_test_id("terminal-view").last
        expect(terminal_view).to_be_visible(timeout=60_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
        # Full screen widens the pane so each wave covers more cells; the
        # shell refits before the script reads its size.
        full_screen = rail.get_by_role("button", name="Full screen", exact=True)
        if full_screen.count():
            full_screen.first.click()
            expect(rail.get_by_role("button", name="Exit full screen")).to_be_visible()
            page.wait_for_timeout(1_000)
            expect(terminal_view).to_have_attribute("data-state", "connected")

        screen = terminal_view.locator(".xterm-screen")
        expect(screen).to_be_visible()
        renderer = page.evaluate(_WEBGL_INFO_JS, screen.element_handle())
        print(f"terminal renderer: {renderer}")
        assert renderer["webgl"], f"terminal is not using the WebGL renderer: {renderer}"

        terminal_view.locator("textarea.xterm-helper-textarea").focus()
        page.wait_for_timeout(1_000)
        page.keyboard.type(f"{sys.executable} {script} {state} {_FLOOD_WINDOW_S + 60:.0f}")
        page.keyboard.press("Enter")

        deadline = time.monotonic() + 30
        while not (state / "size").exists():
            assert time.monotonic() < deadline, "flood script did not start in the shell"
            page.wait_for_timeout(250)
        cols, rows = (int(v) for v in (state / "size").read_text().split())
        print(f"pane size: {cols}x{rows}")

        box = screen.bounding_box()
        assert box is not None
        cell_h = box["height"] / rows
        # Rows 1-3 (row 3 stays blank); trim the right edge to skip the scrollbar.
        clip = {
            "x": box["x"],
            "y": box["y"],
            "width": max(1.0, box["width"] - 24),
            "height": cell_h * 3,
        }

        def sample() -> Image.Image:
            return Image.open(io.BytesIO(page.screenshot(clip=clip))).convert("RGB")

        def wave() -> int:
            try:
                return int((state / "wave").read_text())
            except (OSError, ValueError):
                return 0

        page.wait_for_timeout(2_000)
        baseline = sample()
        settled = 0
        for _ in range(20):
            page.wait_for_timeout(int(_SAMPLE_S * 1000))
            candidate = sample()
            if _changed_pixels(baseline, candidate) == 0:
                settled += 1
                if settled >= 2:
                    break
            else:
                settled = 0
                baseline = candidate
        assert settled >= 2, "status rows never settled to a stable baseline"
        baseline.save(scratch / "baseline.png")
        baseline_wave = wave()

        first_change: tuple[float, int, int] | None = None
        start = time.monotonic()
        while time.monotonic() - start < _FLOOD_WINDOW_S:
            page.wait_for_timeout(int(_SAMPLE_S * 1000))
            current = sample()
            changed = _changed_pixels(baseline, current)
            if changed < _MIN_CHANGED_PIXELS:
                continue
            page.wait_for_timeout(int(_SAMPLE_S * 1000))
            confirm = sample()
            confirmed = _changed_pixels(baseline, confirm)
            if confirmed < _MIN_CHANGED_PIXELS:
                continue
            first_change = (time.monotonic() - start, wave(), confirmed)
            current.save(scratch / "first-change.png")
            confirm.save(scratch / "confirmed-change.png")
            page.screenshot(path=str(scratch / "pane-at-change.png"), clip=box)
            page.wait_for_timeout(3_000)
            break

        (state / "stop").touch()
        page.wait_for_timeout(1_500)
        pane_status, pane_rows = _read_pane_rows(state)
        print(f"pane bytes: {pane_status} {pane_rows!r}")
        if pane_status == "ok":
            assert pane_rows[0].rstrip() == _ROW1, pane_rows
            assert pane_rows[1].rstrip() == _ROW2, pane_rows
        expect(terminal_view).to_have_attribute("data-state", "connected")

        assert first_change is None, (
            f"never-rewritten status rows redrew with different pixels "
            f"{first_change[0]:.1f}s into the flood (wave {first_change[1]}, baseline wave "
            f"{baseline_wave}, {first_change[2]} px changed) while the pane bytes stayed "
            f"{pane_status}; images in {scratch}"
        )
    finally:
        state.mkdir(parents=True, exist_ok=True)
        (state / "stop").touch()


@pytest.mark.parametrize("bundle", ["js", "mjs"])
def test_shared_atlas_invalidates_each_renderer_once(page: Page, bundle: str) -> None:
    """Clearing a shared atlas refreshes both terminals once, then stays incremental."""
    page.set_content(
        "<style>.terminal { width: 960px; height: 480px; }</style>"
        '<div id="a" class="terminal"></div><div id="b" class="terminal"></div>'
    )
    page.add_style_tag(path=str(_XTERM_LIB / "css" / "xterm.css"))
    page.add_script_tag(path=str(_XTERM_LIB / "lib" / "xterm.js"))
    if bundle == "js":
        page.add_script_tag(path=str(_WEBGL_LIB / "lib" / "addon-webgl.js"))
    else:
        page.evaluate(
            """async source => {
              const url = URL.createObjectURL(new Blob([source], {type: 'text/javascript'}));
              try { window.WebglAddon = await import(url); }
              finally { URL.revokeObjectURL(url); }
            }""",
            (_WEBGL_LIB / "lib" / "addon-webgl.mjs").read_text(encoding="utf-8"),
        )

    result = page.evaluate(
        """async () => {
          const terms = [], addons = [];
          try {
            for (const id of ['a', 'b']) {
              const term = new Terminal({cols: 100, rows: 30, fontFamily: 'monospace'});
              terms.push(term);
              term.open(document.getElementById(id));
              const addon = new WebglAddon.WebglAddon();
              term.loadAddon(addon);
              addons.push(addon);
              await new Promise(resolve => term.write('Static text 日本語', resolve));
            }
            await new Promise(resolve => {
              requestAnimationFrame(() => requestAnimationFrame(resolve));
            });
            const renderers = addons.map(addon => addon._renderer);
            const shared = renderers[0]._charAtlas === renderers[1]._charAtlas;
            const calls = renderers.map(renderer => {
              const updates = [];
              const original = renderer._updateModel;
              renderer._updateModel = function(start, end) {
                updates.push([start, end]);
                return original.call(this, start, end);
              };
              return updates;
            });
            const render = () => {
              calls.forEach(updates => { updates.length = 0; });
              renderers.forEach(renderer => renderer.renderRows(10, 10));
              return calls.map(updates => updates.slice());
            };
            const before = render();
            addons[0].clearTextureAtlas();
            const afterClear = render();
            const nextFrame = render();
            addons[1].clearTextureAtlas();
            const afterSiblingClear = render();
            const nextSiblingFrame = render();
            renderers.forEach(renderer => {
              renderer._glyphRenderer.value.setAtlas(renderer._charAtlas);
            });
            const afterReattach = render();
            const nextReattachFrame = render();
            const originalUpdate = renderers[0]._updateModel;
            let clearDuringUpdate = true;
            renderers[0]._updateModel = function(start, end) {
              originalUpdate.call(this, start, end);
              if (clearDuringUpdate) {
                clearDuringUpdate = false;
                this._charAtlas.clearTexture();
              }
            };
            const afterMidFrameClear = render();
            const nextMidFrame = render();
            return {shared, before, afterClear, nextFrame, afterSiblingClear,
                    nextSiblingFrame, afterReattach, nextReattachFrame,
                    afterMidFrameClear, nextMidFrame};
          } finally {
            terms.forEach(term => term.dispose());
          }
        }"""
    )
    assert result["shared"], "the terminals must exercise the same pooled atlas"
    partial = [[[10, 10]], [[10, 10]]]
    full = [[[0, 29]], [[0, 29]]]
    assert result["before"] == partial
    assert result["afterClear"] == full
    assert result["nextFrame"] == partial, "atlas clear must not force full redraws forever"
    assert result["afterSiblingClear"] == full
    assert result["nextSiblingFrame"] == partial
    assert result["afterReattach"] == full
    assert result["nextReattachFrame"] == partial
    assert result["afterMidFrameClear"] == [[[10, 10], [0, 29]], [[0, 29]]]
    assert result["nextMidFrame"] == partial
