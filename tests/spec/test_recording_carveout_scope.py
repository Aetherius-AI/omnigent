"""Recording rules distinguish CLI output from internal results and caption the screen."""

from pathlib import Path

from omnigent.spec import load

_DEV = Path(__file__).resolve().parents[2] / "dev"
_REPRO_AGENT = _DEV / "repro-agent"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_lane_rules_distinguish_cli_output_from_internal_results() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "record the real command and its output" in lanes
    assert "output is only an error message, hint, or status line" in lanes
    assert "`omnigent host` prints the wrong error after login expires" in lanes
    assert "written evidence is enough when no user interface shows the result" in lanes
    assert "an error string, a value, a log line" not in lanes


def test_lane_recording_blockers_are_explicit_and_do_not_block_delivery() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "required tool is missing" in lanes
    assert "Name the specific blocker in `recording_unavailable_reason`" in lanes
    assert "Text-only CLI output is not a reason to skip recording" in lanes
    assert "A missing recording from an earlier run is not a reason either" in lanes
    assert "Do not block the verdict, fix, or PR" in lanes


def test_repro_recording_rules_match_the_shared_guide() -> None:
    instructions = _normalized(_REPRO_AGENT / "AGENTS.md")

    assert (
        "For internal/API-only results with no visible user interaction, "
        "written evidence is enough" in instructions
    )
    assert (
        "record the real command and its output, even if only an error message changes"
        in instructions
    )
    assert "Text-only CLI output is not a reason to skip recording" in instructions
    assert "name the specific blocker in `recording_unavailable_reason`" in instructions
    assert "Do not block the verdict because footage is missing or rejected" in instructions


def test_repro_agent_loads_the_screen_evidence_caption_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)
    assert "Caption the screen from screen evidence" in spec.instructions
    assert "Base screen claims on inspected frames" in _normalized(_DEV / "recording-lanes.md")


def test_caption_rules_require_visible_assertions_not_attribute_reads() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")
    instructions = " ".join(load(_REPRO_AGENT, expand_env=False).instructions.split())

    assert (
        "A visible-state assertion is `expect(locator).to_be_visible()` on the element "
        "the caption describes" in lanes
    )
    assert "scrolling that element into the recorded viewport" in lanes
    assert "on the visible element that paints it, not on a container" in lanes
    assert "text matching alone is not a substitute" in lanes
    assert "satisfied by hidden or screen-reader-only descendants" in lanes
    assert "quote such text only after an inspected frame shows it" in lanes
    assert "or `to_have_text(...)`" not in lanes
    assert '`get_attribute("aria-label")`' in lanes
    assert "never-opened elements, so they are not screen evidence" in lanes
    assert "Quote only text the clip renders" in lanes
    assert (
        "a native `title` tooltip has no locator and counts only if an inspected frame "
        "shows it" in lanes
    )
    assert "say so in the caption" in lanes

    assert "`to_be_visible()` on the element the caption describes" in instructions
    assert "text matching alone passes on hidden content" in instructions
    assert "even a visible container matches screen-reader-only descendants" in instructions
    assert "`to_be_visible()` / `to_have_text()`" not in instructions
    assert '`get_attribute("aria-label")`' in instructions
    assert "they are not screen evidence" in instructions
    assert "say so in the caption and claim only what visibility assertions established" in (
        instructions
    )
    assert "not from a transcript, API check, or DOM attribute read" in instructions
