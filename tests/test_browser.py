import os
import shutil
from pathlib import Path

import pytest

from divergence.adapters import BrowserAdapter
from divergence.models import Scenario
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
