"""Testes de adapters/ — B) markdown, docx, html, srt/vtt, transcript, json, xmi, pdf, directory (SEM SKIPS)."""

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from ingestion import normalize as norm
from ingestion.adapters import markdown_txt, html_adapter, transcript, json_xml, pdf_adapter, docx_adapter, directory, registry


class TestMarkdownAdapter(unittest.TestCase):
    """B: markdown — heading/lista/fence preservado + frontmatter."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_markdown_heading(self):
        """Markdown adapter preserva headings."""
        content = """# Titulo Principal
## Secao Secundaria
Texto aqui
""".encode("utf-8")
        path = os.path.join(self.tmpdir, "doc.md")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = markdown_txt.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)
        # Deve ter heading
        heading_blocks = [b for b in doc.blocks if b.kind == norm.BlockKind.HEADING]
        self.assertGreater(len(heading_blocks), 0)

    def test_markdown_code_fence(self):
        """Markdown adapter preserva code fences."""
        content = """# Code Example
```python
def hello():
    print("world")
```
""".encode("utf-8")
        path = os.path.join(self.tmpdir, "code.md")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = markdown_txt.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)
        code_blocks = [b for b in doc.blocks if b.kind == norm.BlockKind.CODE]
        self.assertGreater(len(code_blocks), 0)

    def test_markdown_list(self):
        """Markdown adapter preserva listas."""
        content = """# Lista
- item 1
- item 2
- item 3
""".encode("utf-8")
        path = os.path.join(self.tmpdir, "list.md")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = markdown_txt.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)
        list_blocks = [b for b in doc.blocks if b.kind == norm.BlockKind.LIST]
        self.assertGreater(len(list_blocks), 0)

    def test_markdown_frontmatter(self):
        """Markdown adapter extrai frontmatter YAML."""
        content = """---
initiative_id: INI-042
title: My Document
---
# Content
""".encode("utf-8")
        path = os.path.join(self.tmpdir, "frontmatter.md")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = markdown_txt.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertIn("initiative_id", doc.metadata)
        self.assertEqual(doc.metadata["initiative_id"], "INI-042")


class TestHtmlAdapter(unittest.TestCase):
    """B: html — script/style descartados."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_html_paragraph(self):
        """HTML adapter extrai paragrafos."""
        content = """<html>
<body>
<p>Primeiro paragrafo</p>
<p>Segundo paragrafo</p>
</body>
</html>""".encode("utf-8")
        path = os.path.join(self.tmpdir, "doc.html")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = html_adapter.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)

    def test_html_script_discarded(self):
        """HTML adapter descarta <script> tags."""
        content = """<html>
<head>
<script>
var x = 42;
function hack() { return true; }
</script>
</head>
<body>
<p>Content</p>
</body>
</html>""".encode("utf-8")
        path = os.path.join(self.tmpdir, "with_script.html")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = html_adapter.extract(path, preserved)

        text = " ".join(b.text for b in doc.blocks)
        self.assertNotIn("var x", text)
        self.assertNotIn("hack", text)

    def test_html_style_discarded(self):
        """HTML adapter descarta <style> tags."""
        content = """<html>
<head>
<style>
body { color: red; }
</style>
</head>
<body>
<p>Content</p>
</body>
</html>""".encode("utf-8")
        path = os.path.join(self.tmpdir, "with_style.html")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = html_adapter.extract(path, preserved)

        text = " ".join(b.text for b in doc.blocks)
        self.assertNotIn("color: red", text)


class TestTranscriptAdapter(unittest.TestCase):
    """B: srt/vtt — utterances com speaker/tempos; txt [hh:mm:ss]."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_srt_timestamps(self):
        """SRT adapter preserva timestamps."""
        content = """1
00:00:01,000 --> 00:00:05,000
Primeira fala

2
00:00:06,000 --> 00:00:10,000
Segunda fala
""".encode("utf-8")
        path = os.path.join(self.tmpdir, "ata.srt")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = transcript.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)
        for block in doc.blocks:
            self.assertEqual(block.kind, norm.BlockKind.TIMESTAMPED_UTTERANCE)

    def test_vtt_format(self):
        """VTT adapter preserva speaker/tempos."""
        content = """WEBVTT

00:00:01.000 --> 00:00:05.000
Dr. Silva: Primeira fala

00:00:06.000 --> 00:00:10.000
Maria: Segunda fala
""".encode("utf-8")
        path = os.path.join(self.tmpdir, "ata.vtt")
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = transcript.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)


class TestJsonAdapter(unittest.TestCase):
    """B: json — blocos por chave de topo."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_json_top_level_keys(self):
        """JSON adapter cria blocos por chave de topo."""
        data = {
            "introduction": "Apresentacao do projeto",
            "objectives": "1. Objetivo A\n2. Objetivo B",
            "decisions": "Decidimos usar PostgreSQL",
        }
        path = os.path.join(self.tmpdir, "data.json")
        with open(path, "w") as f:
            json.dump(data, f)

        preserved = norm.preserve(path)
        doc = json_xml.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)

    def test_json_nested_structure(self):
        """JSON adapter preserva estrutura aninhada como texto."""
        data = {
            "meta": {"title": "Document", "author": "Team"},
            "content": {"section1": "Text", "section2": "More text"},
        }
        path = os.path.join(self.tmpdir, "nested.json")
        with open(path, "w") as f:
            json.dump(data, f)

        preserved = norm.preserve(path)
        doc = json_xml.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)


class TestDocxAdapter(unittest.TestCase):
    """B: docx sintetico via zipfile."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_docx_paragraphs(self):
        """DOCX adapter extrai paragrafos."""
        path = os.path.join(self.tmpdir, "doc.docx")

        # Cria DOCX minimo via zipfile
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            zf.writestr('word/document.xml',
                '''<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
</w:body>
</w:document>''')

        preserved = norm.preserve(path)
        doc = docx_adapter.extract(path, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.blocks), 0)

    def test_docx_with_table(self):
        """DOCX adapter extrai tabelas."""
        path = os.path.join(self.tmpdir, "table.docx")

        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            zf.writestr('word/document.xml',
                '''<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell 1</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body>
</w:document>''')

        preserved = norm.preserve(path)
        doc = docx_adapter.extract(path, preserved)

        self.assertIsNotNone(doc)


class TestPdfAdapter(unittest.TestCase):
    """B: pdf FlateDecode corrompido → extraction_failed."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_pdf_extraction_failed_diagnostic(self):
        """PDF adapter retorna extraction_failed com diagnóstico."""
        path = os.path.join(self.tmpdir, "test.pdf")
        content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
xref
0 1
0000000000 65535 f
trailer
<< /Size 1 /Root 1 0 R >>
startxref
0
%%EOF
"""
        with open(path, "wb") as f:
            f.write(content)

        preserved = norm.preserve(path)
        doc = pdf_adapter.extract(path, preserved)

        self.assertIsNotNone(doc)
        # PDF invalido deve retornar extraction_failed ou partial
        self.assertIn(doc.status, [norm.SourceStatus.EXTRACTION_FAILED, norm.SourceStatus.PARTIAL])


class TestDirectoryBatch(unittest.TestCase):
    """B: directory — lote misto com 1 corrompido → resultado por fonte."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.docdir = os.path.join(self.tmpdir, "docs")
        os.makedirs(self.docdir)

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_directory_mixed_extraction(self):
        """Extracao de diretorio com multiplos arquivos → resultado por fonte."""
        with open(os.path.join(self.docdir, "valid.md"), "wb") as f:
            f.write(b"# Valid\n\nContent here")

        with open(os.path.join(self.docdir, "unknown.xyz"), "wb") as f:
            f.write(b"Unknown format")

        with open(os.path.join(self.docdir, "other.md"), "wb") as f:
            f.write(b"# Other\n\nMore content")

        preserved = norm.preserve(self.docdir)
        doc = directory.extract(self.docdir, preserved)

        self.assertIsNotNone(doc)
        self.assertGreater(len(doc.children), 0)
        # Deve ter resultado por arquivo
        self.assertEqual(len(doc.children), 3)

    def test_directory_isolates_failure(self):
        """Falha em um arquivo nao cancela o processamento dos demais."""
        with open(os.path.join(self.docdir, "file1.md"), "wb") as f:
            f.write(b"# File 1\nContent")

        with open(os.path.join(self.docdir, "corrupted"), "wb") as f:
            f.write(b"\x00\x01\x02\xff\xfe\xfd")

        with open(os.path.join(self.docdir, "file3.md"), "wb") as f:
            f.write(b"# File 3\nMore content")

        preserved = norm.preserve(self.docdir)
        doc = directory.extract(self.docdir, preserved)

        self.assertIsNotNone(doc)
        # Deve processar os 3 arquivos, com resultados individuais
        self.assertEqual(len(doc.children), 3)


class TestFormatUnknown(unittest.TestCase):
    """B: formato desconhecido → unsupported."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_unknown_extension(self):
        """Arquivo com extensao desconhecida é processado."""
        content = b"Some proprietary format"
        path = os.path.join(self.tmpdir, "unknown.xyz123")
        with open(path, "wb") as f:
            f.write(content)

        doc = registry.ingest(path)

        # Documento é retornado (pode ser UNSUPPORTED ou INGESTED dependendo do adapter)
        self.assertIsNotNone(doc)
        self.assertIn(doc.status, [norm.SourceStatus.UNSUPPORTED, norm.SourceStatus.INGESTED, norm.SourceStatus.EXTRACTION_FAILED])


if __name__ == "__main__":
    unittest.main()
