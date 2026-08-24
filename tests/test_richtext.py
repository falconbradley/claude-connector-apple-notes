"""Markdown -> Notes HTML conversion.

What Notes honours was established by writing a probe note and decoding
the result; these tests pin the conversion, not Notes' behaviour.
"""

import pytest

from apple_notes_mcp.applescript import text_to_note_html
from apple_notes_mcp.richtext import markdown_to_notes_html as md
from apple_notes_mcp.richtext import plain_to_notes_html as plain


# ---------------------------------------------------------------------------
# Inline formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("**bold**", "<b>bold</b>"),
        ("__bold__", "<b>bold</b>"),
        ("*italic*", "<i>italic</i>"),
        ("_italic_", "<i>italic</i>"),
        ("~~gone~~", "<s>gone</s>"),
        ("`code`", "<tt>code</tt>"),
        ("**a** and *b*", "<b>a</b> and <i>b</i>"),
    ],
)
def test_inline_markers(source, expected):
    assert md(source) == f"<div>{expected}</div>"


def test_links_render_as_anchors():
    assert md("[site](https://example.com)") == (
        '<div><a href="https://example.com">site</a></div>'
    )


def test_underscores_inside_words_are_not_italics():
    # snake_case identifiers must survive intact
    assert md("my_var_name") == "<div>my_var_name</div>"


def test_asterisk_inside_word_is_not_italics():
    assert md("2*3*4") == "<div>2*3*4</div>"


def test_code_span_contents_are_not_parsed_as_markdown():
    assert md("`**not bold**`") == "<div><tt>**not bold**</tt></div>"


# ---------------------------------------------------------------------------
# Escaping — the security-relevant part
# ---------------------------------------------------------------------------


def test_html_in_markdown_is_escaped():
    out = md("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_html_inside_code_span_is_escaped():
    assert "&lt;b&gt;" in md("`<b>`")
    assert "<div><tt>&lt;b&gt;</tt></div>" == md("`<b>`")


def test_ampersands_escaped_in_text_and_titles():
    assert "&amp;" in md("Tom & Jerry")
    assert "&amp;" in md("body", title="A & B")


def test_quote_in_link_url_cannot_break_the_attribute():
    """A quote in the URL must not terminate the href attribute."""
    out = md('[x](https://e.com/")')
    assert 'href="https://e.com/&quot;"' in out
    assert out.count('href="') == 1


# ---------------------------------------------------------------------------
# Block structure
# ---------------------------------------------------------------------------


def test_headings_by_level():
    assert md("# One") == "<div><h1>One</h1></div>"
    assert md("## Two") == "<div><h2>Two</h2></div>"
    assert md("### Three") == "<div><h3>Three</h3></div>"


def test_deep_headings_clamp_to_h3():
    # Notes has no h4+; clamping keeps the text visible and bold
    assert md("###### Six") == "<div><h3>Six</h3></div>"


def test_consecutive_bullets_form_one_list():
    assert md("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"


def test_numbered_list():
    assert md("1. a\n2. b") == "<ol><li>a</li><li>b</li></ol>"


def test_lists_may_carry_inline_formatting():
    assert md("- **a**") == "<ul><li><b>a</b></li></ul>"


def test_separate_lists_stay_separate():
    out = md("- a\n\ntext\n\n- b")
    assert out.count("<ul>") == 2


def test_blank_lines_become_breaks():
    assert "<div><br></div>" in md("a\n\nb")


def test_title_is_emitted_first():
    assert md("body", title="T").startswith("<div><h1>T</h1></div>")


def test_empty_body_still_produces_valid_html():
    assert md("") == "<div><br></div>"


# ---------------------------------------------------------------------------
# Plain mode must not format — the backwards-compatible default
# ---------------------------------------------------------------------------


def test_plain_mode_leaves_markers_literal():
    assert plain("**bold**") == "<div>**bold**</div>"
    assert plain("# not a heading") == "<div># not a heading</div>"


def test_plain_mode_still_escapes_html():
    assert "&lt;script&gt;" in plain("<script>")


def test_bridge_default_is_plain():
    """Existing callers keep byte-for-byte identical output."""
    assert text_to_note_html(None, "**x**") == "<div>**x**</div>"
    assert text_to_note_html(None, "**x**", markdown=True) == "<div><b>x</b></div>"
