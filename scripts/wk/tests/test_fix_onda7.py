# -*- coding: utf-8 -*-
"""Testes unitários para o comando `wk init` (Onda 7) — migração de permissões.

Onda 7: `init` grava denies NOMINAIS (raw/wiki/inbox/index.db*/log/quarantine +
.codescan: state.json/agent-runs/agent-packs/sdd/modules/surface.json) + allow
`Write(<store>/.codescan/**/agent-outputs/**)`; NUNCA mais `Write(<store>/**)`
no deny; settings antigo com deny amplo é MIGRADO (removido) com
`permissoes_migradas: true`; `check`/`doctor` acusam deny amplo/allow ausente
com `acao` de migração.

Testes:
  (a) init em scratch → settings SEM `Write(<store>/**)` no deny, COM allow de
      agent-outputs, COM deny de raw/ e state.json
  (b) init sobre settings contendo o deny amplo → removido + `permissoes_migradas`
      true; 2ª execução → false e sem duplicatas
  (c) deny alheio (`Write(/etc/**)`) preservado
  (d) check com deny amplo → falha com `acao` de migração

Rodar:
    python -m pytest scripts/wk/tests/test_fix_onda7.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wk import cli


def _mock_docs():
    """Cria mock para _docs_manifest e _doc_text (necessário para init funcionar)."""
    fake_manifest = {"skill": {"file": "SKILL.md", "asset": "skill.md", "title": "Wiki AI"}}
    return {
        "_docs_manifest": fake_manifest,
        "_doc_text": "# Wiki AI Skill\n\nDocumentação embutida.\n"
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Roda cli.main com stdout/stderr capturados."""
    out, err = io.StringIO(), io.StringIO()
    try:
        fake = _mock_docs()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(cli, "_docs_manifest", return_value=fake["_docs_manifest"]), \
                 mock.patch.object(cli, "_doc_text", return_value=fake["_doc_text"]):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()
    except SystemExit as e:
        return e.code or 1, out.getvalue(), err.getvalue()


def _write(path: str, content: str) -> None:
    """Escreve arquivo com diretórios criados."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


class Onda7InitSettingsTests(unittest.TestCase):
    """Onda 7a: init em scratch → settings SEM deny amplo, COM allow agent-outputs, COM denies nominais."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda7_init_scratch_")
        self.store = tempfile.mkdtemp(prefix="wk_onda7_store_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda7_repo_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_init_creates_settings_without_broad_deny(self):
        """init em scratch → settings SEM `Write(<store>/**)`, COM narrower denies."""
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertFalse(result.get("permissoes_migradas", False))

        # Verificar que settings.json foi criado
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        self.assertTrue(os.path.exists(settings_path))

        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)

        perms = settings.get("permissions", {})
        deny = perms.get("deny", [])
        allow = perms.get("allow", [])

        # Garantir que NÃO há deny amplo (exatamente Write(<store>/**) ou Edit(<store>/**))
        broad_denies = [d for d in deny if f"Write({self.store}/**)" in d or f"Edit({self.store}/**)" in d]
        self.assertEqual(len(broad_denies), 0,
                        f"Não deve haver deny amplo, encontrados: {broad_denies}")

        # Verificar que há denies nominais (raw, wiki, inbox, state.json, etc)
        self.assertTrue(any("raw" in d for d in deny), "Deve haver deny para raw/")
        self.assertTrue(any("state.json" in d for d in deny), "Deve haver deny para state.json")

        # Verificar que há allow para agent-outputs
        agent_outputs_allow = [a for a in allow if "agent-outputs" in a]
        self.assertGreater(len(agent_outputs_allow), 0,
                          f"Deve haver allow para agent-outputs, found: {allow}")


class Onda7MigrationTests(unittest.TestCase):
    """Onda 7b: init sobre settings com deny amplo → removido + permissoes_migradas true."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda7_migration_")
        self.store = tempfile.mkdtemp(prefix="wk_onda7_store_migration_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda7_repo_migration_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_init_migrates_broad_deny_and_sets_flag(self):
        """init detecta deny amplo antigo → remove + permissoes_migradas=true."""
        # Criar settings.json com deny amplo antigo
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        old_settings = {
            "permissions": {
                "deny": [
                    f"Write({self.store}/**)",
                    f"Edit({self.store}/**)",
                ],
            }
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(old_settings, f)

        # Rodar init
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        # Deve ter migrado
        self.assertTrue(result.get("permissoes_migradas", False),
                       "permissoes_migradas deve ser true quando deny amplo foi removido")

        # Verificar que settings foi atualizado
        with open(settings_path, encoding="utf-8") as f:
            new_settings = json.load(f)

        perms = new_settings.get("permissions", {})
        deny = perms.get("deny", [])

        # Garantir que deny amplo foi removido
        broad_denies = [d for d in deny if f"Write({self.store}/**)" in d]
        self.assertEqual(len(broad_denies), 0,
                        "Deny amplo deve ter sido removido")

        # Verificar que denies nominais ainda estão lá
        self.assertTrue(any("raw" in d for d in deny), "Denies nominais devem estar presentes")

    def test_second_init_has_permissoes_migradas_false_no_duplicates(self):
        """2ª execução de init → permissoes_migradas=false, sem duplicatas."""
        # Criar settings.json com deny amplo
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        old_settings = {
            "permissions": {
                "deny": [
                    f"Write({self.store}/**)",
                    f"Edit({self.store}/**)",
                ],
            }
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(old_settings, f)

        # Primeira execução (com migração)
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result1 = json.loads(out)
        self.assertTrue(result1.get("permissoes_migradas", False))

        # Segunda execução (sem migração)
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])
        self.assertEqual(code, 0, err)
        result2 = json.loads(out)
        self.assertFalse(result2.get("permissoes_migradas", False),
                        "Segunda execução deve ter permissoes_migradas=false")

        # Verificar sem duplicatas
        with open(settings_path, encoding="utf-8") as f:
            new_settings = json.load(f)

        perms = new_settings.get("permissions", {})
        deny = perms.get("deny", [])

        # Contar ocorrências (não deve haver duplicatas)
        deny_counts = {}
        for d in deny:
            deny_counts[d] = deny_counts.get(d, 0) + 1

        duplicatas = [d for d, count in deny_counts.items() if count > 1]
        self.assertEqual(len(duplicatas), 0,
                        f"Não deve haver denies duplicados, encontrados: {duplicatas}")


class Onda7PreserveAlienDenyTests(unittest.TestCase):
    """Onda 7c: deny alheio (`Write(/etc/**)`) preservado durante migração."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda7_alien_")
        self.store = tempfile.mkdtemp(prefix="wk_onda7_store_alien_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda7_repo_alien_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_init_preserves_alien_deny_during_migration(self):
        """init remove deny amplo mas preserva deny alheio."""
        # Criar settings.json com deny amplo + deny alheio
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        alien_deny = "Write(/etc/**)"
        old_settings = {
            "permissions": {
                "deny": [
                    f"Write({self.store}/**)",
                    f"Edit({self.store}/**)",
                    alien_deny,
                ],
            }
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(old_settings, f)

        # Rodar init
        code, out, err = _run([
            "init",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        self.assertEqual(code, 0, err)

        # Verificar que deny alheio foi preservado
        with open(settings_path, encoding="utf-8") as f:
            new_settings = json.load(f)

        perms = new_settings.get("permissions", {})
        deny = perms.get("deny", [])

        self.assertIn(alien_deny, deny,
                     f"Deny alheio {alien_deny} deve ser preservado")

        # E deny amplo deve ter sido removido
        broad_denies = [d for d in deny if f"Write({self.store}/**)" in d]
        self.assertEqual(len(broad_denies), 0,
                        "Deny amplo deve ter sido removido")


class Onda7CheckCommandTests(unittest.TestCase):
    """Onda 7d: check com deny amplo → falha com acao de migração."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wk_onda7_check_")
        self.store = tempfile.mkdtemp(prefix="wk_onda7_store_check_")
        self.repo = tempfile.mkdtemp(prefix="wk_onda7_repo_check_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_check_fails_with_broad_deny_and_provides_migration_action(self):
        """check detecta deny amplo → exit não-zero, falha com acao."""
        # Criar settings.json com deny amplo
        settings_path = os.path.join(self.base, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        old_settings = {
            "permissions": {
                "deny": [
                    f"Write({self.store}/**)",
                    f"Edit({self.store}/**)",
                ],
                "additionalDirectories": [self.store, self.repo],
                "allow": [f"Read({self.store}/**)", f"Read({self.repo}/**)", "Bash(wk *)"],
            }
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(old_settings, f)

        # Rodar check
        code, out, err = _run([
            "check",
            "--base", self.base,
            "--engine", "claude-code",
            "--store", self.store,
            "--repo", self.repo,
        ])

        # Deve falhar
        self.assertNotEqual(code, 0, f"check deve falhar com deny amplo, mas retornou {code}")

        # Verificar que há acao de migração
        output = out or err
        try:
            result = json.loads(output)
            config_report = result.get("config_permissoes", [])
            if isinstance(config_report, list) and len(config_report) > 0:
                entry = config_report[0]
                self.assertFalse(entry.get("ok", True),
                               "config_permissoes deve ter ok=false")
                self.assertIn("acao", entry,
                            "Deve haver acao de migração")
                acao = entry.get("acao", "")
                self.assertIn("wk init", acao,
                            "acao deve mencionar 'wk init' para migração")
        except json.JSONDecodeError:
            self.fail(f"check output não é JSON válido: {output}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
