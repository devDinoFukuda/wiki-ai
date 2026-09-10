from __future__ import annotations

import ast
import zipfile
from pathlib import Path

import pytest

from tests.benchmark.ground_truth import corpora
from tests.benchmark.ground_truth.truth import RepositoryTruth
from wiki_ai.app import api

JAVA_REQUIRED_SYMBOLS = (
    "class RenewalController",
    "class RenewalService",
    "class EligibilityPolicy",
    "interface ContractRepository",
    "class RenewalEventPublisher",
    "@PostMapping",
    "@Transactional",
    "JpaRepository",
    "KafkaTemplate",
    "PESSIMISTIC_WRITE",
)

COBOL_REQUIRED_SYMBOLS = (
    "PROGRAM-ID. BILLRUN",
    "PROCEDURE DIVISION",
    "PERFORM PROCESS-ACCOUNT",
    'CALL "CHKLIMIT"',
    "COPY ACCTREC",
    "EXEC SQL",
    "END-EXEC",
)

JCL_REQUIRED_SYMBOLS = ("//BILLRUN  JOB", "//STEP010  EXEC", "//STEP020  EXEC PGM=BILLRUN")


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_corpus_is_generated_with_every_anchor_path_present(
    tmp_path: Path, name: str
) -> None:
    corpus = corpora.materialize(name, tmp_path / name)
    assert corpus.root.is_dir()
    for path in corpus.truth.anchor_paths():
        assert (corpus.root / path).is_file(), path


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_every_anchor_points_at_an_existing_line(tmp_path: Path, name: str) -> None:
    corpus = corpora.materialize(name, tmp_path / name)
    for item in corpus.truth.all_items():
        for anchor in item.anchors:
            lines = (corpus.root / anchor.path).read_text(encoding="utf-8").splitlines()
            assert 1 <= anchor.line_start <= anchor.line_end <= len(lines), item.key


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_every_anchor_span_carries_the_key_terms(tmp_path: Path, name: str) -> None:
    corpus = corpora.materialize(name, tmp_path / name)
    for item in corpus.truth.all_items():
        excerpt = "\n".join(
            "\n".join(
                (corpus.root / anchor.path)
                .read_text(encoding="utf-8")
                .splitlines()[anchor.line_start - 1 : anchor.line_end]
            )
            for anchor in item.anchors
        )
        lowered = excerpt.lower()
        hits = [term for term in item.key_terms if term.lower() in lowered]
        assert hits, f"{item.key} sem termo-chave no excerpt"


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_declared_symbols_exist_in_the_generated_files(tmp_path: Path, name: str) -> None:
    corpus = corpora.materialize(name, tmp_path / name)
    for path, symbols in corpus.truth.symbols_by_path.items():
        content = (corpus.root / path).read_text(encoding="utf-8")
        for symbol in symbols:
            assert symbol in content, f"{path}: {symbol}"


def test_python_corpus_parses_with_ast(tmp_path: Path) -> None:
    corpus = corpora.materialize("python", tmp_path / "python")
    modules = sorted(corpus.root.rglob("*.py"))
    assert modules
    for module in modules:
        ast.parse(module.read_text(encoding="utf-8"), filename=module.as_posix())


def test_python_corpus_exposes_the_state_machine(tmp_path: Path) -> None:
    corpus = corpora.materialize("python", tmp_path / "python")
    tree = ast.parse((corpus.root / corpora.PYTHON_STATE_PATH).read_text(encoding="utf-8"))
    classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "OrderState" in classes
    assert "advance" in functions


def test_java_corpus_carries_the_expected_symbols(tmp_path: Path) -> None:
    corpus = corpora.materialize("java", tmp_path / "java")
    joined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(corpus.root.rglob("*.java"))
    )
    for symbol in JAVA_REQUIRED_SYMBOLS:
        assert symbol in joined, symbol


def test_java_braces_are_balanced(tmp_path: Path) -> None:
    corpus = corpora.materialize("java", tmp_path / "java")
    for path in sorted(corpus.root.rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        assert text.count("{") == text.count("}"), path.as_posix()
        assert text.count("(") == text.count(")"), path.as_posix()


def test_cobol_corpus_carries_the_expected_symbols(tmp_path: Path) -> None:
    corpus = corpora.materialize("cobol", tmp_path / "cobol")
    program = (corpus.root / corpora.COBOL_PROGRAM_PATH).read_text(encoding="utf-8")
    for symbol in COBOL_REQUIRED_SYMBOLS:
        assert symbol in program, symbol
    jcl = (corpus.root / corpora.COBOL_JCL_PATH).read_text(encoding="utf-8")
    for symbol in JCL_REQUIRED_SYMBOLS:
        assert symbol in jcl, symbol
    copybook = (corpus.root / corpora.COBOL_COPYBOOK_PATH).read_text(encoding="utf-8")
    assert "ACCT-RECORD" in copybook


def test_cobol_jcl_declares_two_steps(tmp_path: Path) -> None:
    corpus = corpora.materialize("cobol", tmp_path / "cobol")
    jcl = (corpus.root / corpora.COBOL_JCL_PATH).read_text(encoding="utf-8")
    steps = [line for line in jcl.splitlines() if " EXEC " in line or " EXEC\t" in line]
    assert len(steps) == 2


def test_java_corpus_ships_a_contradicting_document(tmp_path: Path) -> None:
    corpus = corpora.materialize("java", tmp_path / "java")
    assert len(corpus.documents) == 1
    document = corpus.documents[0]
    assert document.suffix == ".docx"
    with zipfile.ZipFile(document) as archive:
        payload = archive.read("word/document.xml").decode("utf-8")
    planted = corpus.truth.contradictions[0]
    assert "45" in payload
    assert planted.document_name == document.name
    code = (corpus.root / planted.code_anchors[0].path).read_text(encoding="utf-8")
    assert "30" in code
    assert "45" not in code


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_inspect_reads_the_generated_repository(tmp_path: Path, name: str) -> None:
    corpus = corpora.materialize(name, tmp_path / name)
    report = api.inspect(corpus.root)
    assert report.total_files > 0
    assert report.analyzable_files > 0
    assert report.snapshot_digest


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_inspect_sees_every_anchor_path(tmp_path: Path, name: str) -> None:
    corpus = corpora.materialize(name, tmp_path / name)
    report = api.inspect(corpus.root)
    assert report.total_files >= len(corpus.truth.anchor_paths())


@pytest.mark.parametrize("name", corpora.CORPUS_NAMES)
def test_truth_keys_are_unique(tmp_path: Path, name: str) -> None:
    truth: RepositoryTruth = corpora.materialize(name, tmp_path / name).truth
    keys = [item.key for item in truth.all_items()]
    assert len(keys) == len(set(keys))


def test_unknown_corpus_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(corpora.CorpusUnknown):
        corpora.materialize("rust", tmp_path / "rust")
