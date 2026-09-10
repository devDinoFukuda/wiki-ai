from __future__ import annotations

from pathlib import Path

from wiki_ai.quality.source_hygiene import (
    DIRECTIVE_TOKENS,
    HygieneKind,
    MARKER_WORDS,
    scan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "tests")


def _kinds(violations, kind: HygieneKind):
    return [item for item in violations if item.kind is kind]


def _format(violations) -> str:
    return "\n".join(f"{v.path}:{v.line} {v.detail}" for v in violations)


def test_no_comments() -> None:
    found = _kinds(scan(SCANNED_ROOTS), HygieneKind.COMMENT)
    assert not found, _format(found)


def test_no_docstrings() -> None:
    found = _kinds(scan(SCANNED_ROOTS), HygieneKind.DOCSTRING)
    assert not found, _format(found)


def test_no_unparsable_sources() -> None:
    found = _kinds(scan(SCANNED_ROOTS), HygieneKind.UNPARSABLE)
    assert not found, _format(found)


def test_scan_detects_python_comment(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 1  " + chr(35) + " remark\n", encoding="utf-8")
    found = _kinds(scan([tmp_path]), HygieneKind.COMMENT)
    assert [item.line for item in found] == [1]


def test_scan_detects_python_docstrings(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(
        '"""module"""\n\n\nclass A:\n    """klass"""\n\n\ndef f():\n    """fn"""\n    return 1\n',
        encoding="utf-8",
    )
    found = _kinds(scan([tmp_path]), HygieneKind.DOCSTRING)
    assert len(found) == 3


def test_scan_detects_markers_and_directives(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    marker = MARKER_WORDS[0]
    directive = DIRECTIVE_TOKENS[2]
    target.write_text(
        f'value = "{marker} later"\nother = 1  ' + chr(35) + f" {directive}\n",
        encoding="utf-8",
    )
    violations = scan([tmp_path])
    assert _kinds(violations, HygieneKind.MARKER)
    assert _kinds(violations, HygieneKind.DIRECTIVE)


def test_portuguese_word_todo_in_string_is_not_a_marker(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(
        'value = "todo pedido colocado"\nother = "Todo cliente"\n',
        encoding="utf-8",
    )
    violations = scan([tmp_path])
    assert not _kinds(violations, HygieneKind.MARKER), _format(violations)


def test_uppercase_todo_marker_is_still_detected(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    marker = MARKER_WORDS[0]
    target.write_text(
        f'value = "{marker} fix this"\nother = 1  '
        + chr(35)
        + f" {marker} cleanup\n",
        encoding="utf-8",
    )
    found = _kinds(scan([tmp_path]), HygieneKind.MARKER)
    assert [item.line for item in found] == [1, 2]


def test_sql_pragma_in_string_literal_is_not_a_directive(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(
        'conn.execute("PRAGMA journal_mode")\nconn.execute("PRAGMA journal_mode=WAL")\n',
        encoding="utf-8",
    )
    violations = scan([tmp_path])
    assert not _kinds(violations, HygieneKind.DIRECTIVE), _format(violations)


def test_pragma_inside_comment_is_a_directive(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 1  " + chr(35) + " pragma: no cover\n", encoding="utf-8")
    violations = scan([tmp_path])
    found = _kinds(violations, HygieneKind.DIRECTIVE)
    assert [item.line for item in found] == [1]
    assert found[0].detail == "pragma"


def test_scan_detects_slash_comments(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("var a = 1; // remark\n", encoding="utf-8")
    (tmp_path / "b.java").write_text("/* block\nstill block */\nint a;\n", encoding="utf-8")
    found = _kinds(scan([tmp_path]), HygieneKind.COMMENT)
    assert {item.path.rsplit("/", 1)[-1] for item in found} == {"a.js", "b.java"}


def test_scan_ignores_slash_inside_string(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text('var url = "http://example.org";\n', encoding="utf-8")
    assert not _kinds(scan([tmp_path]), HygieneKind.COMMENT)


def test_scan_detects_markup_comments(tmp_path: Path) -> None:
    (tmp_path / "a.html").write_text("<p>x</p><!-- remark -->\n", encoding="utf-8")
    (tmp_path / "b.xml").write_text("<root/>\n", encoding="utf-8")
    found = _kinds(scan([tmp_path]), HygieneKind.COMMENT)
    assert [item.path.rsplit("/", 1)[-1] for item in found] == ["a.html"]


def test_scan_skips_markdown(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("<!-- remark -->\n", encoding="utf-8")
    assert not scan([tmp_path])
