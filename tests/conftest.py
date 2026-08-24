"""Shared test guards.

Unit tests must never reach Notes.app. Locally an unmocked scripting
call quietly succeeds against the real app — which is how a test that
forgot a mock passed here and then hung for 120s on CI, where there is
no Notes.app to answer. This makes that failure immediate and obvious
instead of environment-dependent.
"""

import pytest

from apple_notes_mcp import applescript


@pytest.fixture(autouse=True)
def no_live_scripting(monkeypatch):
    def guard(script, params=None):
        raise AssertionError(
            "test reached live Notes.app scripting via _run_jxa(). Mock the "
            "applescript function the code path calls (e.g. get_note_body, "
            "replace_note_body) rather than letting it out to osascript."
        )

    monkeypatch.setattr(applescript, "_run_jxa", guard)
