"""Context for failures without guessing application-specific behavior."""

from contextlib import contextmanager


@contextmanager
def browser_step(stage: str, at_ms: int | None = None):
    try:
        yield
    except Exception as exc:
        when = f" at {at_ms} ms" if at_ms is not None else ""
        hints = {
            "launch": "Run divergence doctor; install Chromium if it is missing.",
            "navigation": "Check browser.entry and whether the static build has been produced.",
            "reset_expression": "Provide the reset hook or use '() => {}' when a fresh page is sufficient.",
            "state_expression": "Return an object with state/events/logs/metrics. DOM selectors can replace application hooks.",
            "tick_expression": "Check the step hook, or omit tick_expression to advance browser timers.",
            "action": "Check the selector, element visibility, and action timing against this revision.",
            "snapshot": "Return a valid snapshot object without t_ms or frame; Divergence supplies those fields.",
        }
        hint = hints.get(stage, "Review the captured diagnostic and scenario configuration.")
        raise RuntimeError(f"Browser {stage} failed{when}. {hint}\n{exc}") from exc
