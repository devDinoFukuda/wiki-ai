from __future__ import annotations

from pathlib import Path

from tests.repository.fixtures_repos import java_repo, python_repo, snapshot_of, write
from wiki_ai.repository.config import ConfigHitKind, find_config


def test_env_declaration_and_python_usage(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    hits = find_config(snapshot, "INVOICE_TOPIC")
    kinds = {(hit.path, hit.kind, hit.mechanism) for hit in hits}
    assert (".env", ConfigHitKind.DECLARATION, "key_value") in kinds
    assert ("billing/service.py", ConfigHitKind.USAGE, "os.environ") in kinds
    for hit in hits:
        assert hit.line >= 1
        assert hit.column >= 1


def test_getenv_usage_is_detected(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    hits = find_config(snapshot, "GATEWAY_URL")
    mechanisms = {hit.mechanism for hit in hits}
    assert "getenv" in mechanisms
    assert "key_value" in mechanisms


def test_yaml_declaration_and_value_annotation(tmp_path: Path) -> None:
    snapshot = java_repo(tmp_path)
    hits = find_config(snapshot, "order.topic")
    declarations = [hit for hit in hits if hit.kind is ConfigHitKind.DECLARATION]
    usages = [hit for hit in hits if hit.kind is ConfigHitKind.USAGE]
    assert any(hit.path == "src/main/resources/application.yml" for hit in declarations)
    assert any(hit.mechanism == "value_annotation" for hit in usages)


def test_properties_json_toml_ini_and_xml_sources(tmp_path: Path) -> None:
    write(tmp_path, "conf/app.properties", "cache.ttl.seconds=300\n")
    write(tmp_path, "conf/app.json", '{\n  "cacheTtlSeconds": 300\n}\n')
    write(tmp_path, "conf/app.toml", 'cache_ttl_seconds = 300\n')
    write(tmp_path, "conf/app.ini", "[cache]\ncache.ttl.seconds = 300\n")
    write(tmp_path, "conf/app.xml", '<config><property name="cache.ttl.seconds">300</property></config>\n')
    snapshot = snapshot_of(tmp_path)
    hits = find_config(snapshot, "cache.ttl.seconds")
    paths = {hit.path for hit in hits}
    assert paths == {
        "conf/app.properties",
        "conf/app.json",
        "conf/app.toml",
        "conf/app.ini",
        "conf/app.xml",
    }
    values = {hit.path: hit.value for hit in hits}
    assert values["conf/app.properties"] == "300"
    assert values["conf/app.xml"] == "300"


def test_language_specific_usages(tmp_path: Path) -> None:
    write(tmp_path, "web/config.js", "const url = process.env.API_BASE_URL;\n")
    write(tmp_path, "web/other.js", "const url = process.env['API_BASE_URL'];\n")
    write(tmp_path, "jvm/Boot.java", 'String u = System.getProperty("API_BASE_URL");\n')
    write(tmp_path, "dotnet/Boot.cs", 'var u = ConfigurationManager.AppSettings["API_BASE_URL"];\n')
    snapshot = snapshot_of(tmp_path)
    hits = find_config(snapshot, "API_BASE_URL")
    mechanisms = {hit.mechanism for hit in hits}
    assert mechanisms == {"process.env", "system_get_property", "configuration_manager"}
    assert all(hit.kind is ConfigHitKind.USAGE for hit in hits)


def test_absent_key_and_empty_query(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    assert find_config(snapshot, "NO_SUCH_SETTING") == ()
    assert find_config(snapshot, "") == ()
    assert find_config(snapshot, "INVOICE_TOPIC", limit=0) == ()


def test_declarations_precede_usages(tmp_path: Path) -> None:
    snapshot = python_repo(tmp_path)
    hits = find_config(snapshot, "INVOICE_TOPIC")
    kinds = [hit.kind for hit in hits]
    assert kinds == sorted(kinds, key=lambda kind: kind is ConfigHitKind.USAGE)
