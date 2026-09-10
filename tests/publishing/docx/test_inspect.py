from __future__ import annotations

import zipfile

import pytest

from wiki_ai.publishing.docx import CoreProperties, Heading, build_package, read_package
from wiki_ai.publishing.docx.errors import PackageValidationError


def _rebuild_without(source, target, dropped):
    with zipfile.ZipFile(source) as origin, zipfile.ZipFile(target, "w") as copy:
        for name in origin.namelist():
            if name == dropped:
                continue
            copy.writestr(name, origin.read(name))


def _rebuild_replacing(source, target, name, payload):
    with zipfile.ZipFile(source) as origin, zipfile.ZipFile(target, "w") as copy:
        for entry in origin.namelist():
            copy.writestr(entry, payload if entry == name else origin.read(entry))


@pytest.fixture
def valid_docx(tmp_path):
    path = tmp_path / "valido.docx"
    build_package([Heading(level=1, text="ola")], CoreProperties(title="t"), path)
    return path


def test_read_package_lists_parts_sorted(valid_docx):
    summary = read_package(valid_docx)
    assert list(summary.parts) == sorted(summary.parts)
    assert "word/document.xml" in summary.parts


def test_read_package_rejects_missing_document(tmp_path, valid_docx):
    broken = tmp_path / "sem-document.docx"
    _rebuild_without(valid_docx, broken, "word/document.xml")
    with pytest.raises(PackageValidationError, match="obrigatorias"):
        read_package(broken)


def test_read_package_rejects_missing_core_properties(tmp_path, valid_docx):
    broken = tmp_path / "sem-core.docx"
    _rebuild_without(valid_docx, broken, "docProps/core.xml")
    with pytest.raises(PackageValidationError, match="obrigatorias"):
        read_package(broken)


def test_read_package_rejects_malformed_xml(tmp_path, valid_docx):
    broken = tmp_path / "malformado.docx"
    _rebuild_replacing(valid_docx, broken, "word/document.xml", b"<w:document><w:body>")
    with pytest.raises(PackageValidationError, match="malformado"):
        read_package(broken)


def test_read_package_rejects_doctype(tmp_path, valid_docx):
    payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><w:document/>'
    broken = tmp_path / "doctype.docx"
    _rebuild_replacing(valid_docx, broken, "word/document.xml", payload)
    with pytest.raises(PackageValidationError, match="DOCTYPE"):
        read_package(broken)


def test_read_package_rejects_unexpected_document_root(tmp_path, valid_docx):
    broken = tmp_path / "raiz.docx"
    _rebuild_replacing(valid_docx, broken, "word/document.xml", b"<html/>")
    with pytest.raises(PackageValidationError, match="raiz inesperada"):
        read_package(broken)


def test_read_package_rejects_content_types_without_document_override(tmp_path, valid_docx):
    broken = tmp_path / "ct.docx"
    payload = (
        b'<?xml version="1.0"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="a"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b"</Types>"
    )
    _rebuild_replacing(valid_docx, broken, "[Content_Types].xml", payload)
    with pytest.raises(PackageValidationError, match="content type de documento principal"):
        read_package(broken)


def test_read_package_rejects_override_pointing_to_absent_part(tmp_path, valid_docx):
    broken = tmp_path / "orfao.docx"
    with zipfile.ZipFile(valid_docx) as origin:
        original = origin.read("[Content_Types].xml").decode("utf-8")
    payload = original.replace(
        "</Types>",
        '<Override PartName="/word/fantasma.xml" ContentType="application/xml"/></Types>',
    ).encode("utf-8")
    _rebuild_replacing(valid_docx, broken, "[Content_Types].xml", payload)
    with pytest.raises(PackageValidationError, match="sem parte correspondente"):
        read_package(broken)


def test_read_package_rejects_part_without_content_type(tmp_path, valid_docx):
    broken = tmp_path / "sem-tipo.docx"
    with zipfile.ZipFile(valid_docx) as origin, zipfile.ZipFile(broken, "w") as copy:
        for name in origin.namelist():
            copy.writestr(name, origin.read(name))
        copy.writestr("word/media/x.bmp", b"bmp")
    with pytest.raises(PackageValidationError, match="sem content type"):
        read_package(broken)


def test_read_package_rejects_non_zip(tmp_path):
    path = tmp_path / "texto.docx"
    path.write_bytes(b"isto nao e um zip")
    with pytest.raises(PackageValidationError, match="zip valido"):
        read_package(path)


def test_read_package_rejects_missing_file(tmp_path):
    with pytest.raises(PackageValidationError, match="inexistente"):
        read_package(tmp_path / "ausente.docx")
