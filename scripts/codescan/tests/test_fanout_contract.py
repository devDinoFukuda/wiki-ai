"""Contrato de fan-out do `codescan` CLI (S7).

Cobre: `run-stage` passa a INSTRUIR fan-out (next_action/fanout_required/
agent_slot por batch, não só descrever); `merge-agent-output` exige `--agent`
real e recusa input reaproveitado de uma tentativa anterior; `done` detecta
no merge a ausência de fan-out (todos os batches vindos do mesmo agente);
resolução de `--store` deixa de cair silenciosamente em `./store`; e ordem de
flag errada (`--topic` antes do subcomando) vira erro acionável em vez do
`invalid choice` cru do argparse.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from codescan import cli as cli_mod
from codescan import state as st_mod
from codescan.cli import main


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _module_java(name: str) -> str:
    return "\n".join(
        [
            f"package app.{name};",
            f"class {name.capitalize()}Service {{",
            "  boolean run(String id) {",
            "    if (id == null) return false;",
            "    repository.save(id);",
            "    return true;",
            "  }",
            "}",
        ]
    )


class FanoutManifestContractTest(unittest.TestCase):
    """`run-stage modules` com >=4 módulos e `--batches 4`: cada batch precisa
    do próprio `agent_slot`, o manifesto precisa de `fanout_required` e a
    resposta (default e verbose) precisa de `next_action` instruindo o
    fan-out explicitamente."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        for i in range(1, 5):
            _write(
                os.path.join(self.repo, "src", f"m{i}", "Service.java"),
                _module_java(f"m{i}"),
            )
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def _prepare(self) -> None:
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)

    def test_run_stage_traz_next_action_fanout_required_e_agent_slot_por_batch(self):
        self._prepare()

        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "4"))

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["fanout_required"], 4)
        self.assertEqual(len(result["batches"]), 4)
        self.assertIn("next_action", result)
        self.assertIsInstance(result["next_action"], str)
        self.assertIn("4", result["next_action"])
        self.assertIn("fan-out", result["next_action"].lower())
        slots = sorted(b["agent_slot"] for b in result["batches"])
        self.assertEqual(slots, ["modules-b01", "modules-b02", "modules-b03", "modules-b04"])
        for batch in result["batches"]:
            self.assertIn(batch["output"], result["next_action"])
            self.assertIn(f"--agent {batch['agent_slot']}", batch["merge_command"])

        manifest_path = result["manifest"]
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["fanout_required"], 4)
        self.assertIn("next_action", manifest)

    def test_run_stage_com_quiet_explicito_preserva_next_action_e_fanout_required(self):
        # `--quiet` é opt-in (não é mais o padrão): precisa ser pedido
        # explicitamente para condensar a saída a uma linha só.
        self._prepare()

        code, out, err = _run(
            ["--store", self.store, "--repo", self.repo, "run-stage", "modules", "--batches", "4", "--quiet"]
        )

        self.assertEqual(code, 0, err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["fanout_required"], 4)
        self.assertIn("next_action", payload)
        self.assertIn("4", payload["next_action"])
        # campo condensado por ser lista: não deve reaparecer o corpo inteiro
        self.assertNotIn("batches", payload)
        self.assertIn("batches_count", payload)


class MergeAgentOutputAgentGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        self.wd = st_mod.workdir(self.store, self.repo)
        st_mod.init(self.wd, self.repo, topic=None)
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def _write_input(self) -> str:
        path = os.path.join(self.tmp.name, "agent.txt")
        _write(path, "=== MODULE: src/payments ===\nConteúdo mínimo.\n=== END ===\n")
        return path

    def test_merge_sem_agent_falha(self):
        inp = self._write_input()

        code, _out, err = _run(self._argv("merge-agent-output", "modules", "--input", inp))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("--agent", payload["error"])

    def test_merge_com_agent_generico_falha(self):
        inp = self._write_input()
        for generic in ("main", "orquestrador", "orchestrator", "self", "principal", "  "):
            with self.subTest(agent=generic):
                code, _out, err = _run(
                    self._argv("merge-agent-output", "modules", "--input", inp, "--agent", generic)
                )
                self.assertEqual(code, 2)

    def test_merge_com_agent_real_passa_validacao_de_identidade(self):
        inp = self._write_input()

        code, _out, err = _run(
            self._argv("merge-agent-output", "modules", "--input", inp, "--agent", "modules-b01")
        )

        self.assertEqual(code, 0, err)

    def test_merge_recusa_input_com_mtime_anterior_ao_plano(self):
        _write(
            os.path.join(self.repo, "src", "payments", "Service.java"),
            _module_java("payments"),
        )
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(
            self._argv("config", "--doc-level", "essencial", "--granularity", "module")
        )
        self.assertEqual(code, 0, err)

        # input escrito ANTES do run-stage (mtime anterior ao created_at do
        # plano) simula um arquivo reaproveitado de uma tentativa anterior.
        stale = os.path.join(self.tmp.name, "stale.txt")
        _write(stale, "=== MODULE: src/payments ===\nConteúdo antigo.\n=== END ===\n")
        old = 1_700_000_000.0  # bem antes de qualquer created_at gerado no teste
        os.utime(stale, (old, old))

        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "1"))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        item = result["batches"][0]["items"][0]
        fresh = os.path.join(self.tmp.name, "fresh.txt")
        _write(fresh, f"=== MODULE: {item} ===\nConteúdo fresco.\n=== END ===\n")

        code, _out, err = _run(
            self._argv("merge-agent-output", "modules", "--input", stale, "--agent", "modules-b01")
        )
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("mtime", payload["error"])

        code, _out, err = _run(
            self._argv("merge-agent-output", "modules", "--input", fresh, "--agent", "modules-b01")
        )
        self.assertEqual(code, 0, err)


def _write_plan_manifest(wd: str, stage: str, fanout_required: int) -> None:
    _write(
        os.path.join(wd, "agent-runs", f"{stage}-plan.json"),
        json.dumps(
            {
                "schema": "wiki-ai.run-stage-plan.v1",
                "stage": stage,
                "fanout_required": fanout_required,
                "created_at": "2026-01-01T00:00:00Z",
            },
            ensure_ascii=False,
        ),
    )


def _write_runs_manifest(wd: str, stage: str, agents: list[str]) -> None:
    runs = [
        {
            "stage": stage,
            "agent": agent,
            "items": [],
            "items_count": 0,
            "artifacts_count": 0,
            "created_at": "2026-01-01T00:01:00Z",
        }
        for agent in agents
    ]
    _write(
        os.path.join(wd, "agent-runs", f"{stage}.json"),
        json.dumps({"schema": "wiki-ai.agent-runs.v2", "stage": stage, "runs": runs}, ensure_ascii=False),
    )


class DoneFanoutGateTest(unittest.TestCase):
    """`done <stage>` precisa recusar fechar um estágio cujo plano exigia N>1
    batches mas cujos agent-runs registrados vieram todos do mesmo `agent` —
    é a prova, no merge, de que o fan-out não aconteceu de verdade."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        self.wd = st_mod.workdir(self.store, self.repo)
        st_mod.init(self.wd, self.repo, topic=None)
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def test_fanout_gate_error_none_quando_sem_plano(self):
        self.assertIsNone(cli_mod._fanout_gate_error(self.wd, "modules"))

    def test_fanout_gate_error_bloqueia_com_mesmo_agente_e_libera_com_distintos(self):
        _write_plan_manifest(self.wd, "modules", 4)

        _write_runs_manifest(self.wd, "modules", ["worker-solo", "worker-solo", "worker-solo", "worker-solo"])
        same_agent_error = cli_mod._fanout_gate_error(self.wd, "modules")
        self.assertIsNotNone(same_agent_error)
        self.assertIn("4", same_agent_error)
        self.assertIn("fan-out", same_agent_error.lower())

        _write_runs_manifest(self.wd, "modules", ["modules-b01", "modules-b02", "modules-b03", "modules-b04"])
        self.assertIsNone(cli_mod._fanout_gate_error(self.wd, "modules"))

    def test_done_bloqueia_quando_4_batches_registrados_pelo_mesmo_agente(self):
        _write_plan_manifest(self.wd, "modules", 4)
        _write_runs_manifest(self.wd, "modules", ["solo", "solo", "solo", "solo"])

        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("fan-out obrigatório", payload["error"])
        self.assertIn("4", payload["error"])

    def test_done_nao_acusa_fanout_quando_agentes_sao_distintos(self):
        _write_plan_manifest(self.wd, "modules", 4)
        _write_runs_manifest(
            self.wd,
            "modules",
            ["modules-b01", "modules-b02", "modules-b03", "modules-b04"],
        )

        code, _out, err = _run(self._argv("done", "modules"))

        # outros gates (itens concluídos, artefatos SDD) ainda podem faltar
        # nesse estado mínimo; o que este teste prova é que o gate de fan-out
        # especificamente não é mais o bloqueador.
        self.assertEqual(code, 2)
        payload = json.loads(err)
        blockers_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("fan-out obrigatório", blockers_text)


class StoreResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo, exist_ok=True)
        self._had_wk_store = "WK_STORE" in os.environ
        self._old_wk_store = os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()
        if self._had_wk_store:
            os.environ["WK_STORE"] = self._old_wk_store

    def test_sem_store_e_sem_wk_store_falha_com_mensagem_acionavel(self):
        code, _out, err = _run(["--repo", self.repo, "state"])

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("--store", payload["error"] + payload["acao"])
        self.assertIn("WK_STORE", payload["error"] + payload["acao"])

    def test_wk_store_env_supre_flag_ausente(self):
        store = os.path.join(self.tmp.name, "store-via-env")
        with mock.patch.dict(os.environ, {"WK_STORE": store}):
            code, _out, err = _run(["--repo", self.repo, "--verbose", "state"])

        # sem estado ainda (nenhum surface rodou), mas a resolução de store
        # em si não falhou — o erro é "sem estado", não "store não informado".
        self.assertEqual(code, 1)
        payload = json.loads(err)
        self.assertNotIn("store não informado", payload["error"])

    def test_flag_store_explicita_tem_prioridade_sobre_env(self):
        store = os.path.join(self.tmp.name, "store-explicito")
        with mock.patch.dict(os.environ, {"WK_STORE": os.path.join(self.tmp.name, "outro")}):
            code, _out, err = _run(["--store", store, "--repo", self.repo, "--verbose", "state"])

        self.assertEqual(code, 1)
        wd = st_mod.workdir(store, self.repo)
        self.assertFalse(os.path.isdir(wd))  # nunca rodou surface; só provamos a raiz certa


class FlagOrderHintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        os.environ.pop("WK_STORE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_topic_antes_do_subcomando_emite_correcao_em_vez_de_invalid_choice(self):
        code, _out, err = _run(
            ["--store", self.store, "--repo", self.repo, "--topic", "codebases/x", "surface"]
        )

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("--topic", payload["error"])
        self.assertIn("subcomando", payload["error"])
        self.assertIn("surface", payload["error"])
        self.assertNotIn("invalid choice", payload["error"])
        self.assertIn("acao", payload)
        self.assertIn("surface --topic codebases/x", payload["acao"])

    def test_ordem_correta_nao_dispara_a_deteccao(self):
        code, out, err = _run(
            ["--store", self.store, "--repo", self.repo, "surface", "--topic", "codebases/x", "--module-min-files", "1"]
        )

        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
