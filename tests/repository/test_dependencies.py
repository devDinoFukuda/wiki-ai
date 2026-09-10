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
from wiki_ai.repository.dependencies import DependencyScope, detect_dependencies


def _targets(report, path: str) -> set[str]:
    return {item.target for item in report.imports if item.path == path}


def test_python_imports_split_internal_and_external(tmp_path: Path) -> None:
    report = detect_dependencies(python_repo(tmp_path))
    edges = {item.target: item for item in report.imports if item.path == "billing/service.py"}
    assert edges["os"].scope is DependencyScope.EXTERNAL
    assert edges["billing.repository"].scope is DependencyScope.INTERNAL
    assert edges["billing.repository"].line == 3
    assert edges["billing.repository"].mechanism == "import"


def test_java_imports_and_maven_manifest(tmp_path: Path) -> None:
    report = detect_dependencies(java_repo(tmp_path))
    controller = "src/main/java/com/acme/order/OrderController.java"
    assert "com.acme.order.OrderService" in _targets(report, controller)
    external = {
        item.target
        for item in report.imports
        if item.scope is DependencyScope.EXTERNAL and item.path == controller
    }
    assert "org.springframework.web.bind.annotation.RestController" in external
    names = {item.name: item for item in report.manifests}
    assert names["org.springframework.boot:spring-boot-starter-web"].version == "3.2.0"
    assert names["org.apache.kafka:kafka-clients"].path == "pom.xml"


def test_web_imports_require_and_package_json(tmp_path: Path) -> None:
    report = detect_dependencies(web_repo(tmp_path))
    client = _targets(report, "src/client.ts")
    assert {"axios", "./payload"} <= client
    worker = [item for item in report.imports if item.path == "src/worker.js"]
    assert worker[0].mechanism == "require"
    assert worker[0].target == "amqplib"
    manifests = {item.name: item.version for item in report.manifests}
    assert manifests["axios"] == "^1.6.0"
    assert manifests["jest"] == "^29.7.0"


def test_cobol_copy_and_call_and_jcl_exec(tmp_path: Path) -> None:
    report = detect_dependencies(mainframe_repo(tmp_path))
    cobol = {(item.target, item.mechanism) for item in report.imports if item.path == "cobol/PAYRUN.cbl"}
    assert ("PAYREC", "copy") in cobol
    assert ("TAXCALC", "call") in cobol
    jcl = {(item.target, item.mechanism) for item in report.imports if item.path == "jcl/PAYJOB.jcl"}
    assert ("PAYRUN", "exec_pgm") in jcl
    assert ("TAXCALC", "exec_pgm") in jcl
    assert ("PROD.PAYROLL.MASTER", "dd_dsn") in jcl


def test_sql_tables_from_and_join(tmp_path: Path) -> None:
    report = detect_dependencies(sql_repo(tmp_path))
    tables = {item.target for item in report.imports if item.path == "db/schema.sql"}
    assert {"PAYROLL_MASTER", "EMPLOYEE"} <= tables
    assert all(
        item.scope is DependencyScope.UNRESOLVED
        for item in report.imports
        if item.path == "db/schema.sql"
    )


def test_endpoints_and_hosts_carry_locator(tmp_path: Path) -> None:
    report = detect_dependencies(java_repo(tmp_path))
    endpoints = [item for item in report.endpoints]
    assert endpoints
    assert "api.acme.com" in report.hosts()
    for item in endpoints:
        assert item.line >= 1
        assert item.scheme in {"http", "https"}
        assert item.path


def test_messaging_hints_detect_kafka_and_queue(tmp_path: Path) -> None:
    report = detect_dependencies(java_repo(tmp_path))
    technologies = {item.technology for item in report.messaging}
    assert "kafka" in technologies
    assert "topic" in technologies
    for item in report.messaging:
        assert item.line >= 1
    rabbit = detect_dependencies(web_repo(tmp_path / "web"))
    assert {"rabbitmq", "queue"} <= {item.technology for item in rabbit.messaging}


def test_go_mod_and_gemfile_and_csproj_manifests(tmp_path: Path) -> None:
    write(tmp_path, "go.mod", "module acme\n\ngo 1.22\n\nrequire (\n\tgithub.com/google/uuid v1.6.0\n)\n")
    write(tmp_path, "Gemfile", "source 'https://rubygems.org'\ngem 'rails', '7.1.0'\n")
    write(
        tmp_path,
        "App.csproj",
        '<Project><ItemGroup><PackageReference Include="Serilog" Version="3.1.1" /></ItemGroup></Project>\n',
    )
    write(tmp_path, "requirements.txt", "flask>=3.0\n# a pinned entry\nboto3==1.34.0\n")
    report = detect_dependencies(snapshot_of(tmp_path))
    entries = {item.name: item for item in report.manifests}
    assert entries["github.com/google/uuid"].version == "v1.6.0"
    assert entries["rails"].version == "7.1.0"
    assert entries["Serilog"].version == "3.1.1"
    assert entries["boto3"].version == "==1.34.0"
    assert entries["flask"].manifest == "requirements"


def test_mixed_repository_reports_every_language(tmp_path: Path) -> None:
    report = detect_dependencies(mixed_repo(tmp_path))
    languages = {item.language_hint for item in report.imports}
    assert {"java", "python", "typescript", "javascript", "cobol", "jcl", "sql", "go"} <= languages
    assert report.internal_imports()
    assert report.external_imports()
    assert report.snapshot_digest
