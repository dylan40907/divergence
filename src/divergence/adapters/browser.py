"""Playwright capture worker for trusted instrumented local web applications."""

from __future__ import annotations

import functools
import http.server
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

from divergence.models import LogEntry, Sample, Scenario, Trace, safe_child
from divergence.storage import atomic_json


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def capture(source: Path, output: Path, scenario: Scenario) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Install divergence-hunter[browser] and run playwright install chromium"
        ) from exc
    entry = safe_child(source, scenario.browser.entry)
    if not entry.is_file():
        raise ValueError(f"browser entry does not exist: {scenario.browser.entry}")
    handler = functools.partial(QuietHandler, directory=str(source))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    frames = output / "frames"
    frames.mkdir()
    samples = []
    pending_logs: list[LogEntry] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={
                    "width": scenario.browser.viewport[0],
                    "height": scenario.browser.viewport[1],
                },
                device_scale_factor=1,
                reduced_motion=scenario.browser.reduced_motion,
                locale="en-US",
                timezone_id="UTC",
                service_workers="block",
                record_video_dir=str(output / "video"),
                record_video_size={
                    "width": scenario.browser.viewport[0],
                    "height": scenario.browser.viewport[1],
                },
            )
            if not scenario.browser.allow_network:
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if urlparse(route.request.url).netloc == urlparse(origin).netloc
                        else route.abort()
                    ),
                )
            context.add_init_script(f"""(() => {{let s = {scenario.seed}; Math.random = () => {{
                s = (Math.imul(1664525,s) + 1013904223) >>> 0; return s / 4294967296;
            }};}})();""")
            page = context.new_page()
            page.set_default_timeout(5000)
            page.on(
                "console",
                lambda msg: pending_logs.append(
                    LogEntry(
                        level={"warn": "warning", "error": "error", "debug": "debug"}.get(
                            msg.type, "info"
                        ),
                        message=msg.text,
                    )
                ),
            )
            page.on(
                "pageerror",
                lambda error: pending_logs.append(LogEntry(level="error", message=str(error))),
            )
            page.clock.install(time=scenario.browser.clock_epoch_ms - 60000)
            page.clock.pause_at(scenario.browser.clock_epoch_ms)
            page.goto(f"{origin}/{entry.relative_to(source).as_posix()}", wait_until="load")
            page.evaluate(scenario.browser.reset_expression, scenario.seed)
            for t in range(0, scenario.duration_ms + 1, scenario.sample_ms):
                if t:
                    if scenario.browser.tick_expression:
                        page.evaluate(scenario.browser.tick_expression, scenario.sample_ms)
                    else:
                        page.clock.run_for(scenario.sample_ms)
                for action in scenario.browser.actions:
                    if action.at_ms != t:
                        continue
                    if action.kind == "evaluate":
                        page.evaluate(action.value)
                    elif action.kind == "click":
                        page.locator(action.target).click(timeout=5000)
                    elif action.kind == "press":
                        page.locator(action.target).press(action.value)
                    else:
                        page.locator(action.target).fill(action.value)
                snapshot = page.evaluate(scenario.browser.state_expression)
                name = f"frames/{len(samples):05d}.png"
                page.screenshot(
                    path=str(output / name),
                    animations="disabled" if scenario.browser.disable_animations else "allow",
                )
                sample = Sample(t_ms=t, **snapshot, frame=name)
                sample.logs.extend(pending_logs)
                pending_logs.clear()
                samples.append(sample)
            context.close()
            browser.close()
        trace = Trace(
            seed=scenario.seed,
            samples=samples,
            metadata={
                "adapter": "browser",
                "clock": "scenario",
                "video_clock": "wall",
                "clock_epoch_ms": scenario.browser.clock_epoch_ms,
                "note": "Frame sequence is synchronized to scenario time; WebM is wall-clock capture.",
            },
        )
        atomic_json(output / "trace.json", trace.model_dump())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    capture(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        Scenario.model_validate_json(Path(sys.argv[3]).read_text()),
    )
