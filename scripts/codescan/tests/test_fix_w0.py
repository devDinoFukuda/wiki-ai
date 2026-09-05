"""Testes para as correções W0 (F01, F02, F04).

F01: VERIFIED_STAGES — `done evidence`/`done verify` agora só funcionam se há
     registro de verificação real com hash íntegro.
F02: drift_report — adiciona campos `dirty`, `dirty_files`, `content_state` para
     rastrear sujeira do worktree independente de mudanças em HEAD.
F04: _record_agent_run — múltiplos runs com mesmo agent+input_sha256 agora
     registram campo `conflito` em vez de sobrescrever.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from codescan import agentmerge as agentmerge_mod
from codescan import state as st_mod
from codescan import surface as surface_mod
from codescan.cli import main


def _write(path: str, text: str = None) -> None:
    """Escreve arquivo, criando diretórios se necessário."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if text is None:
        text = "\n".join(f"# Line {i}" for i in range(1, 21)) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Executa CLI capturando stdout/stderr."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _git_init(repo: str, initial_file: str = None) -> None:
    """Inicializa um repo git vazio com um commit inicial."""
    os.makedirs(repo, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    if initial_file:
        _write(initial_file, "initial content\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)


class TestF01VerifiedStages(unittest.TestCase):
    """F01: done evidence/verify precisam de record_verification com hash íntegro."""

    def test_done_evidence_recusado_sem_execucao(self) -> None:
        """done evidence sem execução prévia de evidence é recusado."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Tentar done evidence sem antes executar evidence
            code, out, err = _run(["--repo", repo, "--store", store, "done", "evidence"])

            self.assertEqual(code, 2, f"Esperado code=2, got {code}. err={err}")
            self.assertIn("nenhuma verificação registrada", err)
            state = st_mod.load(wd) or {}
            # evidence não deve estar em "done"
            self.assertNotEqual(state.get("stages", {}).get("evidence", {}).get("status"), "done")

    def test_done_evidence_aceito_apos_record_verification(self) -> None:
        """done evidence é aceito se há record_verification com hash válido."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Criar artefato de evidence
            artifact = os.path.join(wd, "sdd", "confirmed.md")
            _write(artifact, "# Evidence\n\nSome analysis here.\n")

            # Registrar a verificação
            st_mod.record_verification(wd, "evidence", "confirmed", artifact)

            # Agora done evidence deve funcionar
            code, out, err = _run(["--repo", repo, "--store", store, "done", "evidence"])

            self.assertEqual(code, 0, f"Esperado code=0, got {code}. err={err}")
            state = st_mod.load(wd) or {}
            self.assertEqual(state["stages"]["evidence"]["status"], "done")

    def test_done_evidence_recusado_apos_editar_artefato(self) -> None:
        """done evidence recusa se artefato foi editado após record_verification."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Criar e registrar artefato
            artifact = os.path.join(wd, "sdd", "confirmed.md")
            _write(artifact, "# Evidence v1\n\nInitial version.\n")
            st_mod.record_verification(wd, "evidence", "confirmed", artifact)

            # Editar o artefato
            _write(artifact, "# Evidence v1\n\nEdited version — content changed.\n")

            # verification_ok deve retornar False agora
            ok, reason = st_mod.verification_ok(wd, "evidence")
            self.assertFalse(ok)
            self.assertIn("alterado", reason)

    def test_done_verify_sempre_recusado(self) -> None:
        """done verify é SEMPRE recusado (status derivado de coverage)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Criar artefatos de verify
            confirmed = os.path.join(wd, "sdd", "confirmed.md")
            _write(confirmed, "# Confirmed\n\nContent.\n")
            st_mod.record_verification(wd, "verify", "confirmed", confirmed)

            # done verify deve ser recusado mesmo com verificação registrada
            code, out, err = _run(["--repo", repo, "--store", store, "done", "verify"])

            self.assertEqual(code, 2)
            self.assertIn("done verify não é permitido", err)

    def test_revalidate_verified_stages_derruba_status_ao_editar(self) -> None:
        """revalidate_verified_stages derruba status para 'failed' quando artefato é editado."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Criar, registrar e marcar como done
            artifact = os.path.join(wd, "sdd", "confirmed.md")
            _write(artifact, "# Evidence\n\nv1.\n")
            st_mod.record_verification(wd, "evidence", "confirmed", artifact)
            st_mod.mark(wd, "evidence", "done", artifact)

            # Verificar que está em "done"
            state = st_mod.load(wd)
            self.assertEqual(state["stages"]["evidence"]["status"], "done")

            # Editar o artefato
            _write(artifact, "# Evidence\n\nv1 — EDITED.\n")

            # Chamar revalidate — deve descer pra "failed"
            st_mod.revalidate_verified_stages(wd)

            state = st_mod.load(wd)
            self.assertEqual(state["stages"]["evidence"]["status"], "failed")
            self.assertIn("invalidated", state["stages"]["evidence"])

    def test_revalidate_called_no_main_before_commands(self) -> None:
        """revalidate_verified_stages é chamado em cli.main antes de subcomandos."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Setup: evidence pronta
            artifact = os.path.join(wd, "sdd", "confirmed.md")
            _write(artifact, "# Evidence\n\nv1.\n")
            st_mod.record_verification(wd, "evidence", "confirmed", artifact)
            st_mod.mark(wd, "evidence", "done", artifact)

            # Editar
            _write(artifact, "# Evidence\n\nv1 — EDITED.\n")

            # Rodar qualquer comando (state aqui) — revalidate deve ter executado
            code, out, err = _run(["--repo", repo, "--store", store, "state"])

            state = st_mod.load(wd)
            self.assertEqual(state["stages"]["evidence"]["status"], "failed")


class TestF02DriftReport(unittest.TestCase):
    """F02: drift_report rastreia sujeira do worktree além de mudanças em HEAD."""

    def test_drift_report_limpo_sem_sujeira(self) -> None:
        """drift_report com worktree limpo retorna dirty=False, content_state=head."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            # Pegar o HEAD curto (como usado em surface.py)
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head_short = result.stdout.strip()

            report = surface_mod.drift_report(repo, head_short)

            self.assertEqual(report["dirty"], False)
            self.assertEqual(report["dirty_files"], [])
            self.assertEqual(report["content_state"], head_short)

    def test_drift_report_com_unstaged_changes(self) -> None:
        """drift_report detecta arquivo unstaged (modificado mas não staged)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head = result.stdout.strip()

            # Editar arquivo sem staged
            _write(os.path.join(repo, "file.txt"), "modified content\n")

            report = surface_mod.drift_report(repo, head)

            self.assertEqual(report["dirty"], True)
            self.assertIn("file.txt", report["dirty_files"])
            # content_state deve incluir dirty hash
            self.assertIn("dirty:", report["content_state"])
            self.assertTrue(report["content_state"].startswith(head + "+dirty:"))

    def test_drift_report_com_staged_changes(self) -> None:
        """drift_report detecta arquivo staged (modified, staged)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head = result.stdout.strip()

            # Editar e stage
            _write(os.path.join(repo, "file.txt"), "staged content\n")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, capture_output=True)

            report = surface_mod.drift_report(repo, head)

            self.assertEqual(report["dirty"], True)
            self.assertIn("file.txt", report["dirty_files"])
            self.assertIn("dirty:", report["content_state"])

    def test_drift_report_com_untracked_files(self) -> None:
        """drift_report detecta arquivo não rastreado."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head = result.stdout.strip()

            # Criar novo arquivo sem track
            _write(os.path.join(repo, "untracked.txt"), "new file\n")

            report = surface_mod.drift_report(repo, head)

            self.assertEqual(report["dirty"], True)
            self.assertIn("untracked.txt", report["dirty_files"])
            self.assertIn("dirty:", report["content_state"])

    def test_drift_report_com_arquivo_deletado(self) -> None:
        """drift_report detecta arquivo deletado (unstaged deletion)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head = result.stdout.strip()

            # Deletar arquivo
            os.remove(os.path.join(repo, "file.txt"))

            report = surface_mod.drift_report(repo, head)

            self.assertEqual(report["dirty"], True)
            self.assertIn("file.txt", report["dirty_files"])
            self.assertIn("dirty:", report["content_state"])

    def test_drift_report_content_state_estavel_mesmo_conteudo(self) -> None:
        """Dois drift_report com mesmo conteúdo sujo têm mesmo content_state."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head = result.stdout.strip()

            # Editar arquivo
            _write(os.path.join(repo, "file.txt"), "same dirty content\n")

            report1 = surface_mod.drift_report(repo, head)
            report2 = surface_mod.drift_report(repo, head)

            self.assertEqual(report1["content_state"], report2["content_state"])
            self.assertEqual(report1["dirty_files"], report2["dirty_files"])

    def test_drift_report_head_mudou_detectado(self) -> None:
        """drift_report detecta que HEAD mudou (commit novo)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _git_init(repo, os.path.join(repo, "file.txt"))

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head_old = result.stdout.strip()

            # Fazer novo commit
            _write(os.path.join(repo, "file2.txt"), "new file\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "second"], cwd=repo, check=True, capture_output=True)

            # Obter novo HEAD (também curto)
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            head_new = result.stdout.strip()

            report = surface_mod.drift_report(repo, head_old)

            self.assertEqual(report["drift"], True)
            self.assertNotEqual(report["head_atual"], head_old)
            self.assertEqual(report["head_atual"], head_new)


class TestF04AgentRun(unittest.TestCase):
    """F04: _record_agent_run sob _lock com field 'conflito' em duplicatas."""

    def test_record_agent_run_grava_manifest_atomicamente(self) -> None:
        """_record_agent_run grava agent-runs/<stage>.json com _write_json_atomic."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Preparar input
            input_file = os.path.join(tmp, "input.json")
            _write(input_file, json.dumps({"stage": "modules", "items": []}, ensure_ascii=False))

            # Preparar artefato
            artifact_file = os.path.join(wd, "modules", "test.md")
            _write(artifact_file, "# Test\n\nContent.\n")

            # Criar um artifact para gravar
            class MockArtifact:
                def __init__(self):
                    self.item = "test"
                    self.artifacts = [artifact_file]

            # Gravar run
            path = agentmerge_mod._record_agent_run(
                wd=wd,
                stage="modules",
                input_path=input_file,
                agent="test-agent",
                artifacts=[MockArtifact()],
                output_text="",
            )

            # Verificar que o arquivo foi criado
            self.assertTrue(os.path.isfile(path))
            with open(path) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["schema"], "wiki-ai.agent-runs.v2")
            self.assertEqual(len(manifest["runs"]), 1)

    def test_record_agent_run_duplicata_adiciona_conflito(self) -> None:
        """_record_agent_run detecta duplicata (mesmo agent+input_sha256) e grava conflito."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Preparar input (será idêntico para ambos runs)
            input_file = os.path.join(tmp, "input.json")
            _write(input_file, json.dumps({"stage": "modules", "items": []}, ensure_ascii=False))

            # Preparar artefato
            artifact_file = os.path.join(wd, "modules", "test.md")
            _write(artifact_file, "# Test\n\nContent.\n")

            class MockArtifact:
                def __init__(self):
                    self.item = "test"
                    self.artifacts = [artifact_file]

            # Primeiro run
            path = agentmerge_mod._record_agent_run(
                wd=wd,
                stage="modules",
                input_path=input_file,
                agent="test-agent",
                artifacts=[MockArtifact()],
                output_text="output 1",
            )

            # Segundo run COM MESMA INPUT (duplicata)
            agentmerge_mod._record_agent_run(
                wd=wd,
                stage="modules",
                input_path=input_file,
                agent="test-agent",
                artifacts=[MockArtifact()],
                output_text="output 2",
            )

            # Verificar manifesto
            with open(path) as f:
                manifest = json.load(f)
            runs = manifest["runs"]
            self.assertEqual(len(runs), 2)

            # Segundo run deve ter campo 'conflito'
            second_run = runs[1]
            self.assertIn("conflito", second_run)
            self.assertEqual(second_run["conflito"]["tipo"], "execucao_duplicada")
            self.assertEqual(second_run["conflito"]["run_anterior_id"], 1)

            # Primeiro run NÃO foi sobrescrito
            self.assertNotIn("conflito", runs[0])

    def test_record_agent_run_diferentes_inputs_sem_conflito(self) -> None:
        """_record_agent_run com inputs diferentes não gera conflito."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Primeiro input
            input_file1 = os.path.join(tmp, "input1.json")
            _write(input_file1, json.dumps({"stage": "modules", "items": ["item1"]}, ensure_ascii=False))

            # Segundo input (diferente)
            input_file2 = os.path.join(tmp, "input2.json")
            _write(input_file2, json.dumps({"stage": "modules", "items": ["item2"]}, ensure_ascii=False))

            artifact_file = os.path.join(wd, "modules", "test.md")
            _write(artifact_file, "# Test\n\nContent.\n")

            class MockArtifact:
                def __init__(self):
                    self.item = "test"
                    self.artifacts = [artifact_file]

            # Primeiro run com input1
            path = agentmerge_mod._record_agent_run(
                wd=wd,
                stage="modules",
                input_path=input_file1,
                agent="test-agent",
                artifacts=[MockArtifact()],
                output_text="output 1",
            )

            # Segundo run com input2 (diferente)
            agentmerge_mod._record_agent_run(
                wd=wd,
                stage="modules",
                input_path=input_file2,
                agent="test-agent",
                artifacts=[MockArtifact()],
                output_text="output 2",
            )

            with open(path) as f:
                manifest = json.load(f)
            runs = manifest["runs"]
            self.assertEqual(len(runs), 2)

            # Nenhum deve ter conflito (inputs diferentes)
            for run in runs:
                self.assertNotIn("conflito", run)


class TestF01IntegrationWithCLI(unittest.TestCase):
    """Testes de integração: F01 no contexto CLI."""

    def test_cli_hook_revalidate_ao_rodar_qualquer_comando(self) -> None:
        """CLI chama revalidate automaticamente ao iniciar."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            _git_init(repo, os.path.join(repo, "init.txt"))
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")

            # Setup evidence como done com artefato verificado
            artifact = os.path.join(wd, "sdd", "confirmed.md")
            _write(artifact, "v1")
            st_mod.record_verification(wd, "evidence", "confirmed", artifact)
            st_mod.mark(wd, "evidence", "done", artifact)

            # Editar artefato
            _write(artifact, "v2")

            # Rodar comando (state)
            code, out, err = _run(["--repo", repo, "--store", store, "state"])

            # Estado deve ter sido revalidado
            state = st_mod.load(wd)
            self.assertEqual(state["stages"]["evidence"]["status"], "failed")
            self.assertIn("invalidated", state["stages"]["evidence"])


if __name__ == "__main__":
    unittest.main()
