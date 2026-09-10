from __future__ import annotations

from pathlib import Path

from tests.repository.fixtures_repos import (
    java_repo,
    mainframe_repo,
    mixed_repo,
    python_repo,
    snapshot_of,
    sql_repo,
    web_repo,
    write,
)
from wiki_ai.repository.symbols import Confidence, SymbolKind, find_symbols


def _named(symbols, name: str):
    return [item for item in symbols if item.name == name]


def test_python_symbols_are_parsed_with_block_end(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    symbols = find_symbols(snapshot, "billing/service.py")
    service = _named(symbols, "InvoiceService")
    assert service and service[0].kind is SymbolKind.CLASS
    assert service[0].confidence is Confidence.PARSED
    assert service[0].line_end is not None and service[0].line_end > service[0].line_start
    assert _named(symbols, "issue")[0].kind is SymbolKind.FUNCTION


def test_java_symbols_cover_class_interface_and_method(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    base = "src/main/java/com/acme/order"
    controller = find_symbols(snapshot, f"{base}/OrderController.java")
    assert _named(controller, "OrderController")[0].kind is SymbolKind.CLASS
    assert _named(controller, "place")[0].kind is SymbolKind.METHOD
    repository = find_symbols(snapshot, f"{base}/OrderRepository.java")
    assert _named(repository, "OrderRepository")[0].kind is SymbolKind.INTERFACE
    assert all(item.confidence is Confidence.HEURISTIC for item in repository)


def test_web_symbols_cover_class_function_and_const(tmp_path: Path) -> None:
    snapshot = web_repo(tmp_path)
    client = find_symbols(snapshot, "src/client.ts")
    assert _named(client, "Cart")[0].kind is SymbolKind.INTERFACE
    assert _named(client, "submitCart")[0].kind is SymbolKind.FUNCTION
    assert _named(client, "resetCart")[0].kind is SymbolKind.FUNCTION
    worker = find_symbols(snapshot, "src/worker.js")
    assert _named(worker, "consumeQueue")[0].kind is SymbolKind.FUNCTION


def test_cobol_and_jcl_symbols(tmp_path: Path) -> None:
    snapshot = mainframe_repo(tmp_path)
    program = find_symbols(snapshot, "cobol/PAYRUN.cbl")
    assert _named(program, "PAYRUN")[0].kind is SymbolKind.PROGRAM
    assert _named(program, "MAIN-LOGIC")[0].kind is SymbolKind.SECTION
    assert _named(program, "TAXCALC")[0].kind is SymbolKind.CALL
    assert _named(program, "PAYREC")[0].kind is SymbolKind.CALL
    jcl = find_symbols(snapshot, "jcl/PAYJOB.jcl")
    assert _named(jcl, "PAYJOB")[0].kind is SymbolKind.PROGRAM
    assert _named(jcl, "STEP01")[0].kind is SymbolKind.STEP
    assert _named(jcl, "STEP02")[0].kind is SymbolKind.STEP


def test_sql_symbols(tmp_path: Path) -> None:
    snapshot = sql_repo(tmp_path)
    symbols = find_symbols(snapshot, "db/schema.sql")
    kinds = {item.name: item.kind for item in symbols}
    assert kinds["PAYROLL_MASTER"] is SymbolKind.TABLE
    assert kinds["EMPLOYEE"] is SymbolKind.TABLE
    assert kinds["CALC_TAX"] is SymbolKind.PROCEDURE
    assert kinds["NET_AMOUNT"] is SymbolKind.FUNCTION


def test_unknown_language_falls_back_without_error(tmp_path: Path) -> None:
    write(tmp_path, "edge/thing.zzz", "widget PayrollWidget = {\n  compute(hours)\n}\n")
    snapshot = snapshot_of(tmp_path)
    symbols = find_symbols(snapshot, "edge/thing.zzz")
    assert symbols
    assert all(item.confidence is Confidence.HEURISTIC for item in symbols)
    assert all(item.language_hint == "unknown" for item in symbols)
    assert all(item.kind is SymbolKind.UNKNOWN for item in symbols)


def test_binary_and_missing_paths_never_raise(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    snapshot = snapshot_of(tmp_path)
    assert find_symbols(snapshot, "blob.bin") == ()
    assert find_symbols(snapshot, "does/not/exist.py") == ()


def test_find_symbols_filters_by_name_and_kind(tmp_path: Path) -> None:
    snapshot = mixed_repo(tmp_path)
    by_name = find_symbols(snapshot, None, "OrderService")
    assert by_name
    assert {item.name for item in by_name} == {"OrderService"}
    tables = find_symbols(snapshot, None, None, kinds=(SymbolKind.TABLE,))
    assert {item.name for item in tables} == {"PAYROLL_MASTER", "EMPLOYEE"}


def test_find_symbols_honours_max_results(tmp_path: Path) -> None:
    snapshot = mixed_repo(tmp_path)
    assert len(find_symbols(snapshot, None, None, max_results=3)) == 3
    assert find_symbols(snapshot, None, None, max_results=0) == ()
