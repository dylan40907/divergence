"""Record user actions against the scenario clock into a replayable browser recipe."""

from __future__ import annotations

import functools
import http.server
import threading
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from divergence.adapters.browser import QuietHandler
from divergence.models import Action, Scenario, safe_child

RECORDER_SCRIPT = r"""(() => {
  window.__divergenceRecorded = [];
  window.__divergenceTick = 0;
  const unique = (query, element) => {
    const matches = document.querySelectorAll(query);
    return matches.length === 1 && matches[0] === element;
  };
  const selector = element => {
    const candidates=[];
    if(element.id) candidates.push('#'+CSS.escape(element.id));
    if(element.hasAttribute('data-testid')) candidates.push('[data-testid="'+CSS.escape(element.getAttribute('data-testid'))+'"]');
    if(element.name) candidates.push(element.tagName.toLowerCase()+'[name="'+CSS.escape(element.name)+'"]');
    for(const candidate of candidates) if(unique(candidate,element)) return candidate;
    const path=[];
    while(element && element.nodeType===1) {
      const tag=element.tagName.toLowerCase(); let index=1, sibling=element.previousElementSibling;
      while(sibling){if(sibling.tagName===element.tagName) index++; sibling=sibling.previousElementSibling;}
      path.unshift(tag+':nth-of-type('+index+')'); element=element.parentElement;
    }
    return path.join(' > ');
  };
  const privateField = element => {
    if(element.closest('[data-divergence-private],input[type="password"]')) return true;
    const tokens=(element.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
    return tokens.some(token => token.startsWith('cc-') || ['current-password','new-password','one-time-code'].includes(token));
  };
  const push=(kind,element,value='')=>window.__divergenceRecorded.push({at_ms:window.__divergenceTick,kind,target:selector(element),value});
  document.addEventListener('click',event=>{
    if(!event.isTrusted || event.detail===0 || privateField(event.target))return;
    const target=event.target.closest('button,a,input[type=checkbox],input[type=radio],[role=button]');
    if(target)push('click',target);
  },true);
  document.addEventListener('input',event=>{
    if(!event.isTrusted || privateField(event.target))return;
    const target=event.target;
    if((target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) &&
       !['password','checkbox','radio','file','hidden','range','color'].includes(target.type))push('fill',target,target.value);
  },true);
  document.addEventListener('keydown',event=>{
    if(!event.isTrusted || privateField(event.target))return;
    const target=event.target;
    const editable=target.isContentEditable || target instanceof HTMLTextAreaElement ||
      (target instanceof HTMLInputElement && !['button','submit','reset','checkbox','radio','range','color','file','hidden'].includes(target.type));
    // Text is represented by its resulting value. Replaying Space as well would
    // emit an extra input event; cursor movements are already reflected in fills.
    if(editable && (event.key===' ' || event.key.startsWith('Arrow')))return;
    if(event.key==='Enter' && (target instanceof HTMLTextAreaElement || target.isContentEditable))return;
    if(['Enter','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight',' '].includes(event.key))push('press',event.target,event.key===' '?'Space':event.key);
  },true);
})();"""


def record(
    source: Path,
    scenario: Scenario,
    destination: Path,
    *,
    headless: bool = False,
    on_tick: Callable | None = None,
) -> Scenario:
    """Interactive recording. on_tick is a deterministic hook for the integration test."""
    if scenario.adapter != "browser":
        raise ValueError("record currently supports browser scenarios only")
    if destination.exists():
        raise ValueError("recording output exists; choose a new filename")
    from playwright.sync_api import sync_playwright

    source = source.resolve()
    entry = safe_child(source, scenario.browser.entry)
    if not entry.is_file():
        raise ValueError("scenario browser entry does not exist")
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(source))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                viewport={
                    "width": scenario.browser.viewport[0],
                    "height": scenario.browser.viewport[1],
                },
                locale="en-US",
                timezone_id="UTC",
                service_workers="block",
                reduced_motion=scenario.browser.reduced_motion,
            )
            origin = f"http://127.0.0.1:{server.server_port}"
            if not scenario.browser.allow_network:
                from urllib.parse import urlparse

                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if urlparse(route.request.url).netloc == urlparse(origin).netloc
                        else route.abort()
                    ),
                )
            context.add_init_script(
                f"""(() => {{let s={scenario.seed}; Math.random=()=>{{s=(Math.imul(1664525,s)+1013904223)>>>0;return s/4294967296}};}})();"""
            )
            page = context.new_page()
            page.clock.install(time=scenario.browser.clock_epoch_ms - 60000)
            page.clock.pause_at(scenario.browser.clock_epoch_ms)
            page.goto(origin + "/" + entry.relative_to(source).as_posix())
            page.evaluate(scenario.browser.reset_expression, scenario.seed)
            page.evaluate(RECORDER_SCRIPT)
            for t in range(0, scenario.duration_ms + 1, scenario.sample_ms):
                if t:
                    if scenario.browser.tick_expression:
                        page.evaluate(scenario.browser.tick_expression, scenario.sample_ms)
                    else:
                        page.clock.run_for(scenario.sample_ms)
                page.evaluate("t => window.__divergenceTick = t", t)
                if on_tick:
                    on_tick(page, t)
                if not headless:
                    # Playwright pumps incoming events while this command is in flight;
                    # real-time cadence makes the UI usable without altering scenario time.
                    until = time.monotonic() + scenario.sample_ms / 1000
                    while time.monotonic() < until:
                        page.evaluate("document.readyState")
                        time.sleep(0.01)
            raw = page.evaluate("window.__divergenceRecorded")
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    actions = []
    for item in raw:
        action = Action.model_validate(item)
        if (
            actions
            and action.kind == "fill"
            and actions[-1].kind == "fill"
            and action.target == actions[-1].target
            and action.at_ms == actions[-1].at_ms
        ):
            actions[-1] = action
        else:
            actions.append(action)
    recorded = scenario.model_copy(deep=True)
    recorded.browser.actions = actions
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(
            recorded.model_dump(exclude_defaults=True), sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )
    return recorded
