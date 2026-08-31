import os
import shutil
from pathlib import Path

import pytest

from divergence.adapters import BrowserAdapter
from divergence.demo import create_demo
from divergence.engine import Search
from divergence.models import Scenario, load_scenario
from divergence.recorder import record

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("RUN_BROWSER_TESTS") != "1",
        reason="set RUN_BROWSER_TESTS=1 after installing Chromium",
    ),
]


def test_real_recording_replays_and_captures_video(tmp_path):
    pytest.importorskip("playwright")
    source = tmp_path / "app"
    shutil.copytree(Path(__file__).parents[1] / "src/divergence/demo_templates/browser", source)
    scenario = Scenario(
        name="recording",
        adapter="browser",
        duration_ms=1000,
        sample_ms=250,
        browser={"tick_expression": "ms => window.divergence.step(ms)"},
        assertions=[{"path": "door.open", "value": True}],
    )

    def act(page, t):
        if t == 500:
            page.locator("#switch").click()

    recorded = record(source, scenario, tmp_path / "recorded.yaml", headless=True, on_tick=act)
    assert len(recorded.browser.actions) == 1
    assert recorded.browser.actions[0].at_ms == 500
    trace = BrowserAdapter().capture(source, tmp_path / "capture", recorded)
    assert trace.samples[1].state["door"]["open"] is False
    assert trace.samples[2].state["door"]["open"] is True
    assert list((tmp_path / "capture/video").glob("*.webm"))
    assert trace.samples[2].frame is not None


def test_virtual_timer_does_not_include_wall_clock_capture_time(tmp_path):
    pytest.importorskip("playwright")
    source = tmp_path / "timers"
    source.mkdir()
    (source / "index.html").write_text("""<!doctype html><body>Clock fixture<script>
    let ticks=0, start=0, interval;
    window.divergence={reset(){clearInterval(interval);ticks=0;start=Date.now();interval=setInterval(()=>ticks++,100)},
    snapshot(){return {state:{ticks,elapsed:Date.now()-start}}}};
    </script>""")
    scenario = Scenario(
        name="timers",
        adapter="browser",
        duration_ms=500,
        sample_ms=100,
        assertions=[{"path": "ticks", "value": 5}],
    )
    first = BrowserAdapter().capture(source, tmp_path / "first", scenario)
    second = BrowserAdapter().capture(source, tmp_path / "second", scenario)
    assert [sample.state["elapsed"] for sample in first.samples] == [0, 100, 200, 300, 400, 500]
    assert [sample.state["ticks"] for sample in first.samples] == [0, 1, 2, 3, 4, 5]
    assert [sample.state for sample in first.samples] == [sample.state for sample in second.samples]


@pytest.mark.parametrize("failure", ["reset", "selector", "snapshot"])
def test_browser_failure_explains_stage_and_retains_diagnostic(tmp_path, failure):
    from divergence.process import ExecutionError

    source = tmp_path / "app"
    source.mkdir()
    (source / "index.html").write_text(
        '<!doctype html><title>Plain app</title><button id="present">Continue</button>'
    )
    scenario = Scenario(
        name="setup failure",
        adapter="browser",
        duration_ms=100,
        sample_ms=100,
        assertions=[{"path": "ready", "value": True}],
        browser={"reset_expression": "() => {}", "state_expression": "({state:{ready:true}})"},
    )
    if failure == "reset":
        scenario.browser.reset_expression = "seed => window.divergence.reset(seed)"
        expected = "Browser reset_expression failed at 0 ms"
    elif failure == "selector":
        from divergence.models import Action

        scenario.browser.actions = [Action(at_ms=100, kind="click", target="#missing")]
        expected = "Browser action failed at 100 ms"
    else:
        scenario.browser.state_expression = "null"
        expected = "Browser snapshot failed at 0 ms"
    output = tmp_path / "capture"
    with pytest.raises(ExecutionError, match=expected):
        BrowserAdapter().capture(source, output, scenario)
    assert (output / "diagnostic.json").is_file()
    assert (output / "process.log").is_file()
    assert not (output / "trace.json").exists()


def test_dom_only_cart_history_finds_repeated_coupon_bug(tmp_path):
    demo = create_demo(tmp_path / "cart", commits=8, bug_at=6, adapter="browser", fixture="cart")
    scenario = load_scenario(Path(demo["scenario"]))
    result = Search(
        Path(demo["repository"]), demo["good"], demo["bad"], scenario, tmp_path / "run"
    ).run()
    assert result["status"] == "FOUND"
    assert result["culprit"] == demo["expected_culprit"]
    assert result["trials"][result["culprit"]]["comparison"]["first_divergence_ms"] == 1500
    assert "if (couponApplied) return;" in result["diff"]
    assert len(result["trials"]) <= 5


def test_browser_capture_uses_filesystem_entry_limit(tmp_path):
    from divergence.artifacts import ArtifactLimitError
    from divergence.process import ExecutionError

    source = tmp_path / "app"
    source.mkdir()
    (source / "index.html").write_text("<!doctype html><title>Capture limit</title>Local fixture")
    scenario = Scenario(
        name="bounded browser",
        adapter="browser",
        duration_ms=100,
        sample_ms=100,
        assertions=[{"path": "ready", "value": True}],
        browser={"reset_expression": "() => {}", "state_expression": "({state:{ready:true}})"},
        capture_limits={"max_entries": 2},
    )
    with pytest.raises(
        (ArtifactLimitError, ExecutionError), match="capture exceeded 2 filesystem entries"
    ):
        BrowserAdapter().capture(source, tmp_path / "capture", scenario)


def test_recorder_keyboard_duplicate_selectors_and_private_fields(tmp_path):
    source = tmp_path / "forms"
    source.mkdir()
    (source / "index.html").write_text("""<!doctype html><html><body>
    <input id="text"><input name="duplicate"><input name="duplicate">
    <input id="password" type="password"><input id="otp" autocomplete="one-time-code">
    <div data-divergence-private><input id="private"></div>
    <button id="activate">Activate</button><output id="count">0</output>
    <script>
    const button=document.querySelector('#activate'), count=document.querySelector('#count');
    button.addEventListener('click',()=>count.textContent=Number(count.textContent)+1);
    document.body.addEventListener('keydown',e=>{if(e.target===document.body && e.key==='Escape')document.body.dataset.escaped='yes'});
    </script></body></html>""")
    scenario = Scenario(
        name="form recorder",
        adapter="browser",
        duration_ms=500,
        sample_ms=100,
        browser={
            "reset_expression": "() => {}",
            "state_expression": "({state:{text:document.querySelector('#text').value, duplicate:document.querySelectorAll('[name=duplicate]')[1].value, count:Number(document.querySelector('#count').textContent), escaped:document.body.dataset.escaped || ''}})",
        },
        assertions=[{"path": "count", "value": 2}],
    )

    def act(page, t):
        if t == 100:
            page.locator("#text").press_sequentially("hello world")
            page.locator("[name=duplicate]").nth(1).fill("second field")
        if t == 200:
            for target in ("#password", "#otp", "#private"):
                page.locator(target).press_sequentially("q" * 16)
                page.locator(target).press("ArrowLeft")
        if t == 300:
            page.locator("#activate").press("Enter")
        if t == 400:
            page.locator("#activate").press("Space")
        if t == 500:
            page.locator("#activate").evaluate("(element) => element.blur()")
            page.keyboard.press("Escape")

    destination = tmp_path / "recorded.yaml"
    recorded = record(source, scenario, destination, headless=True, on_tick=act)
    assert "q" * 16 not in destination.read_text()
    assert all(
        action.target not in {"#password", "#otp", "#private"}
        for action in recorded.browser.actions
    )
    assert [action.value for action in recorded.browser.actions if action.kind == "press"] == [
        "Enter",
        "Space",
        "Escape",
    ]
    trace = BrowserAdapter().capture(source, tmp_path / "replay", recorded)
    assert trace.samples[-1].state == {
        "text": "hello world",
        "duplicate": "second field",
        "count": 2,
        "escaped": "yes",
    }
