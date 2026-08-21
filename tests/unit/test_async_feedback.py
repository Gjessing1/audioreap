"""Contracts for the shared HTMX feedback and recovery system."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from service.api.shared import _error_badge

ROOT = Path(__file__).parents[2]
TEMPLATES = ROOT / "service" / "templates"


def _source(path: str) -> str:
    return (ROOT / path).read_text()


@pytest.mark.parametrize("state", ["working", "success", "warning", "error"])
def test_status_message_supports_every_feedback_state(state: str) -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    html = env.get_template("partials/status_message.html").render(
        state=state,
        message="A useful update",
        request_id="deadbeef" if state == "error" else None,
    )

    assert f"status-toast--{state}" in html
    assert "A useful update" in html
    assert ('role="alert"' in html) is (state == "error")
    if state == "error":
        assert "Request ID: deadbeef" in html


def test_base_has_polite_and_assertive_live_regions() -> None:
    base = _source("service/templates/base.html")

    assert 'id="feedback-status"' in base
    assert 'aria-live="polite"' in base
    assert 'id="feedback-errors"' in base
    assert 'aria-live="assertive"' in base


def test_mutation_lifecycle_is_global_retryable_and_request_correlated() -> None:
    app_js = _source("service/static/app.js")

    assert "['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)" in app_js
    assert "feedback-busy-label" in app_js
    assert "button.style.minWidth" in app_js
    assert "button.disabled = true" in app_js
    assert "button.disabled = record.wasDisabled" in app_js
    assert "Request ID: " in app_js
    assert "retry: retryFor(record)" in app_js
    assert "setTimeout(function() { if (toast.parentElement) toast.remove(); }, 6000)" not in app_js


def test_inline_route_errors_signal_feedback_severity() -> None:
    warning = _error_badge("Choose a track")
    failure = _error_badge("Queue unavailable", level="fail")

    assert warning.headers["X-Feedback-Level"] == "warning"
    assert failure.headers["X-Feedback-Level"] == "error"
    assert "Choose a track" in warning.body.decode()


def test_permanent_file_deletion_requires_typed_confirmation() -> None:
    trash = _source("service/templates/partials/trash_list.html")
    base = _source("service/templates/base.html")
    app_js = _source("service/static/app.js")

    assert trash.count('data-confirm-require="DELETE"') == 2
    assert 'id="confirm-dialog-require"' in base
    assert "requireText: data.confirmRequire" in app_js
    assert "requireInput.value !== requiredText" in app_js
