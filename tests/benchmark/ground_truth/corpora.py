from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from tests.benchmark.ground_truth.corpus_cobol import (
    COBOL_COPYBOOK_PATH,
    COBOL_JCL_PATH,
    COBOL_PROGRAM_PATH,
    COBOL_SUBPROGRAM_PATH,
    build_cobol,
)
from tests.benchmark.ground_truth.corpus_java import (
    JAVA_CONFIG_PATH,
    JAVA_CONTRADICTION_DOCUMENT,
    JAVA_CONTROLLER_PATH,
    JAVA_ELIGIBILITY_PATH,
    JAVA_PUBLISHER_PATH,
    JAVA_REPOSITORY_PATH,
    JAVA_SERVICE_PATH,
    JAVA_TEST_PATH,
    build_java,
)
from tests.benchmark.ground_truth.corpus_python import (
    PYTHON_API_PATH,
    PYTHON_IDEMPOTENCY_PATH,
    PYTHON_QUEUE_PATH,
    PYTHON_STATE_PATH,
    PYTHON_VALIDATION_PATH,
    build_python,
)
from tests.benchmark.ground_truth.sheets import CorpusUnknown, GeneratedCorpus

__all__ = [
    "CorpusUnknown",
    "GeneratedCorpus",
    "CORPUS_NAMES",
    "materialize",
    "build_java",
    "build_python",
    "build_cobol",
    "JAVA_CONTROLLER_PATH",
    "JAVA_SERVICE_PATH",
    "JAVA_ELIGIBILITY_PATH",
    "JAVA_REPOSITORY_PATH",
    "JAVA_PUBLISHER_PATH",
    "JAVA_TEST_PATH",
    "JAVA_CONFIG_PATH",
    "JAVA_CONTRADICTION_DOCUMENT",
    "PYTHON_API_PATH",
    "PYTHON_STATE_PATH",
    "PYTHON_VALIDATION_PATH",
    "PYTHON_IDEMPOTENCY_PATH",
    "PYTHON_QUEUE_PATH",
    "COBOL_PROGRAM_PATH",
    "COBOL_SUBPROGRAM_PATH",
    "COBOL_COPYBOOK_PATH",
    "COBOL_JCL_PATH",
]

_BUILDERS: Mapping[str, Callable[[Path], GeneratedCorpus]] = {
    "java": build_java,
    "python": build_python,
    "cobol": build_cobol,
}

CORPUS_NAMES: tuple[str, ...] = tuple(sorted(_BUILDERS))


def materialize(name: str, root: Path) -> GeneratedCorpus:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise CorpusUnknown(name)
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    return builder(target)
