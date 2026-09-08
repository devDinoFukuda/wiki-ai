"""Remediação da 4ª auditoria — achados 3, 4 (runtime.db) e 8.

O que cada teste PROVA, por execução:

* Achado 3 — `connect` com `auto` custava um handshake REAL por chamada de
  `describe()`/`available_transports()`/`connect()` (no `claude-code`, um
  `claude --version` com 5 s de timeout cada). Aqui um contador de
  subprocessos e um contador de handshakes mostram **1 por `connect`**, e a
  janela NÃO sobrevive à operação: um segundo `connect` volta a sondar o host.
* Achado 4 — abrir um `runtime.db` JÁ inicializado abria `BEGIN IMMEDIATE`
  (lock de escrita) para não escrever nada. `set_trace_callback` mostra o
  statement ausente na segunda abertura e presente na primeira; 8 threads
  concorrentes continuam abrindo sem erro.
* Achado 8 — a redação olhava só o NOME da chave em `vendor`; token escrito
  DENTRO de `detail`/`diagnostics` (string livre de adaptador de extensão)
  chegava inteiro a `agent status`, à sonda e ao `agents.json`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from typing import Any, Mapping

from runtime import agents as A
from runtime import bindings as B
from runtime import tasks as T
from runtime.executors import agent_adapters as AD
from runtime.executors.base import ExecutionState


# --------------------------------------------------------------------------
# Achado 3 — 1 handshake real por connect
# --------------------------------------------------------------------------


class _CompletedProcess:
    """Resposta mínima de `subprocess.run` para `claude --version`."""

    returncode = 0
    stdout = "1.2.3 (Claude Code)"
    stderr = ""


class _MemoryExecutor:
    def capabilities(self) -> dict:
        return {"dispatch": True, "concurrency": 1, "structured_output": True}

    def submit(self, task_id, objective, references=None, schema=None, policy=None) -> str:
        return f"mem:{task_id}"

    def status(self, execution_id) -> dict:
        return {"state": ExecutionState.DONE, "heartbeat": None}

    def result(self, execution_id) -> dict:
        return {"execution_id": execution_id, "output": {"objective_id": "probe"}}

    def cancel(self, execution_id) -> bool:
        return True

    def shutdown(self, wait: bool = True) -> None:
        return None


class _CountingClaudeAdapter(AD.ClaudeCodeAgentAdapter):
    """`claude-code` real, com o SUBPROCESSO trocado por um contador.

    Só `_detect` (a sonda do binário) e `_new_executor` são substituídos: a
    rota de `describe`/`available_transports`/`connect` é a de produção.
    """

    def __init__(self) -> None:
        super().__init__(binary_path="claude")
        self.detects = 0

    def _detect(self, binary_path: Any = AD._UNSET):
        self.detects += 1
        return True, "handshake por processo verificado", "1.2.3"

    def _new_executor(self, config: Mapping[str, Any]) -> _MemoryExecutor:
        return _MemoryExecutor()


class UmHandshakePorConnectTest(unittest.TestCase):
    def test_connect_auto_faz_um_unico_handshake_real(self):
        adapter = _CountingClaudeAdapter()
        registry = A.AgentRegistry()
        registry.register(adapter)
        adapter.detects = 0  # `register` já chamou describe(): conta o connect

        binding = registry.connect("claude-code", "auto", {})

        self.assertTrue(binding.connected)
        self.assertEqual(binding.transport, "process")
        self.assertEqual(binding.transport_selection, A.SELECTION_VERIFIED)
        self.assertEqual(adapter.detects, 1)

    def test_connect_explicito_tambem_faz_um_unico_handshake_real(self):
        adapter = _CountingClaudeAdapter()
        registry = A.AgentRegistry()
        registry.register(adapter)
        adapter.detects = 0

        registry.connect("claude-code", "process", {})

        self.assertEqual(adapter.detects, 1)

    def test_janela_nao_sobrevive_a_operacao(self):
        """Não é cache de disponibilidade: cada `connect` sonda o host de novo."""
        adapter = _CountingClaudeAdapter()
        registry = A.AgentRegistry()
        registry.register(adapter)
        adapter.detects = 0

        registry.connect("claude-code", "auto", {})
        registry.connect("claude-code", "auto", {})

        self.assertEqual(adapter.detects, 2)

    def test_um_unico_subprocesso_por_connect(self):
        """O contador olha o `subprocess.run` de verdade, não um gancho."""
        runs: list[list[str]] = []

        def fake_run(argv, **kwargs):
            runs.append(list(argv))
            return _CompletedProcess()

        original_run = AD.subprocess.run
        original_which = AD.shutil.which
        AD.subprocess.run = fake_run
        AD.shutil.which = lambda name: "/usr/bin/" + str(name)
        try:
            adapter = AD.ClaudeCodeAgentAdapter(binary_path="claude")
            adapter._new_executor = lambda config: _MemoryExecutor()  # type: ignore[assignment]
            registry = A.AgentRegistry()
            registry.register(adapter)
            runs.clear()
            registry.connect("claude-code", "auto", {})
        finally:
            AD.subprocess.run = original_run
            AD.shutil.which = original_which

        self.assertEqual(len(runs), 1, runs)
        self.assertEqual(runs[0][-1], "--version")

    def test_adaptador_sem_operation_scope_continua_conectando(self):
        """Extensão que não implementa a janela mantém o contrato mínimo."""

        class SemJanela:
            def describe(self) -> A.AgentDescriptor:
                return A.AgentDescriptor(
                    agent_id="sem-janela",
                    adapter_version="sem-janela/1",
                    transports=("process",),
                    capabilities=A.AgentCapabilities(dispatch=True),
                    setup_steps=(
                        A.SetupStep(
                            step_id="verify",
                            description="verificar o despacho",
                            argv=("wk", "doctor", "--probe-agent"),
                        ),
                    ),
                    distribution="extension",
                )

            def connect(self, transport, config) -> A.AgentBinding:
                return A.AgentBinding(
                    binding_id=A.new_binding_id("sem-janela"),
                    agent_id="sem-janela",
                    adapter_version="sem-janela/1",
                    transport=transport,
                    connected=True,
                )

            def probe(self, binding) -> A.ProbeResult:
                return A.ProbeResult(integration_available=True, agent_connected=True)

            def submit(self, binding, envelope) -> str:
                return "x:1"

            def poll(self, binding, execution_id) -> dict:
                return {"state": "done", "result": None, "error": None}

            def cancel(self, binding, execution_id) -> bool:
                return True

            def close(self, binding) -> None:
                return None

        registry = A.AgentRegistry()
        registry.register(SemJanela())
        binding = registry.connect("sem-janela", "auto", {})
        self.assertTrue(binding.connected)
        self.assertEqual(binding.transport_selection, A.SELECTION_DECLARED)

    def test_binarios_diferentes_nao_compartilham_a_memoria_da_janela(self):
        """A chave é o comando resolvido: um `connect` não lê o host do outro."""
        sondados: list[Any] = []

        class _PorBinario(AD.ClaudeCodeAgentAdapter):
            def _detect(self, binary_path: Any = AD._UNSET):
                resolved = self._binary_path if binary_path is AD._UNSET else binary_path
                sondados.append(resolved)
                return True, f"binário {resolved!r} respondeu", "1.2.3"

            def _new_executor(self, config):
                return _MemoryExecutor()

        adapter = _PorBinario(binary_path="claude")
        registry = A.AgentRegistry()
        registry.register(adapter)
        sondados.clear()
        registry.connect("claude-code", "auto", {"binary_path": "outro-claude"})
        # `describe()` sonda o binário do adaptador; o handshake sonda o do
        # config. São hosts distintos: nenhum reaproveita a resposta do outro.
        self.assertIn("claude", sondados)
        self.assertIn("outro-claude", sondados)


# --------------------------------------------------------------------------
# Achado 4 — abertura de runtime.db já inicializado não paga lock de escrita
# --------------------------------------------------------------------------


class _Trace:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, statement: str) -> None:
        self.statements.append(str(statement))

    def has(self, needle: str) -> bool:
        return any(needle.upper() in s.upper() for s in self.statements)


class AberturaSemLockDeEscritaTest(unittest.TestCase):
    def _db_path(self) -> str:
        tmp = tempfile.mkdtemp(prefix="onda13-runtime-")
        self.addCleanup(_rmtree, tmp)
        return os.path.join(tmp, "runtime.db")

    def test_primeira_abertura_instala_sob_begin_immediate(self):
        path = self._db_path()
        trace = _Trace()
        conn = sqlite3.connect(path)
        conn.set_trace_callback(trace)
        try:
            T.apply_schema(conn, "2026-01-01T00:00:00+00:00")
        finally:
            conn.close()
        self.assertTrue(trace.has("BEGIN IMMEDIATE"), trace.statements)

    def test_reabertura_nao_executa_begin_immediate(self):
        path = self._db_path()
        primeira = sqlite3.connect(path)
        try:
            T.apply_schema(primeira, "2026-01-01T00:00:00+00:00")
        finally:
            primeira.close()

        trace = _Trace()
        segunda = sqlite3.connect(path)
        segunda.set_trace_callback(trace)
        try:
            instalada = T.apply_schema(segunda, "2026-01-01T00:00:01+00:00")
        finally:
            segunda.close()

        self.assertEqual(instalada, T.SCHEMA_VERSION)
        self.assertFalse(trace.has("BEGIN IMMEDIATE"), trace.statements)

    def test_oito_threads_abrindo_o_mesmo_db_nao_falham(self):
        path = self._db_path()
        erros: list[BaseException] = []
        versoes: list[int] = []
        barreira = threading.Barrier(8)

        def abrir() -> None:
            try:
                barreira.wait(timeout=10)
                conn = T.connect(path)
                try:
                    row = conn.execute(
                        "SELECT MAX(version) FROM schema_version"
                    ).fetchone()
                    versoes.append(int(row[0]))
                finally:
                    conn.close()
            except BaseException as exc:  # pragma: no cover - só falha se regredir
                erros.append(exc)

        threads = [threading.Thread(target=abrir) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(erros, [])
        self.assertEqual(versoes, [T.SCHEMA_VERSION] * 8)


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# Achado 8 — segredo escrito DENTRO de string livre
# --------------------------------------------------------------------------


class RedacaoDeValorLivreTest(unittest.TestCase):
    def test_padroes_de_segredo_sao_redigidos_mantendo_a_frase(self):
        casos = {
            "falhou com api_key=abc123": "falhou com api_key=[REDACTED]",
            "header authorization: Bearer abc.def-123": (
                "header authorization: [REDACTED]"
            ),
            "usou token=xyz987 no host": "usou token=[REDACTED] no host",
            "chave sk-ant-api03-zzz recusada": "chave sk-[REDACTED] recusada",
            "Bearer aaaabbbbcccc expirou": "Bearer [REDACTED] expirou",
        }
        for entrada, esperado in casos.items():
            with self.subTest(entrada=entrada):
                self.assertEqual(A.redact_secrets(entrada), esperado)

    def test_texto_sem_segredo_nao_muda(self):
        for texto in (
            "binário não encontrado no PATH: 'claude'",
            "'claude --version' saiu com 127",
            "worker local em processo",
            "orçamento max_tokens: 512 respeitado",
        ):
            with self.subTest(texto=texto):
                self.assertEqual(A.redact_secrets(texto), texto)

    def test_detail_de_transporte_sai_redigido_na_sonda_e_no_erro(self):
        indisponivel = A.TransportAvailability(
            transport="process",
            available=False,
            detail="ponte recusou api_key=abc123",
        )
        self.assertEqual(
            indisponivel.to_dict()["detail"], "ponte recusou api_key=[REDACTED]"
        )

        erro = A.TransportUnavailable(
            "agente 'x' sem transporte disponível (process: api_key=abc123)",
            agent_id="x",
            options=(indisponivel,),
        )
        payload = erro.to_dict()
        self.assertNotIn("abc123", json.dumps(payload))
        self.assertIn("[REDACTED]", payload["detail"])

        sonda = A.ProbeResult(
            integration_available=True,
            detail="acesso não utilizável: api_key=abc123",
            diagnostics=("authorization: Bearer abc.def-123",),
            transports=(indisponivel,),
        )
        saida = sonda.to_dict()
        self.assertNotIn("abc123", json.dumps(saida))
        self.assertIn("[REDACTED]", saida["detail"])
        self.assertIn("[REDACTED]", saida["diagnostics"][0])

    def test_detail_de_binding_sai_redigido_na_saida_publica_e_no_arquivo(self):
        binding = A.AgentBinding(
            binding_id="bind-ext-1",
            agent_id="ext-agent",
            adapter_version="ext/1",
            transport="process",
            connected=True,
            established_at="2026-01-01T00:00:00+00:00",
            capabilities=A.AgentCapabilities(
                dispatch=True, reason="host respondeu com api_key=abc123"
            ),
            detail="conectado usando api_key=abc123",
        )

        publico = binding.to_public_dict()
        self.assertNotIn("abc123", json.dumps(publico))
        self.assertEqual(publico["detail"], "conectado usando api_key=[REDACTED]")
        self.assertIn("[REDACTED]", publico["capabilities"]["reason"])

        tmp = tempfile.mkdtemp(prefix="onda13-agents-")
        self.addCleanup(_rmtree, tmp)
        store = B.BindingStore(tmp)
        store.save_binding(binding)
        with open(store.path, "r", encoding="utf-8") as handle:
            bruto = handle.read()
        self.assertNotIn("abc123", bruto)
        self.assertIn("[REDACTED]", bruto)

    def test_string_livre_dentro_de_vendor_tambem_e_redigida(self):
        capacidades = A.AgentCapabilities(
            dispatch=True,
            vendor={"last_error": "recusado: authorization: Bearer abc.def-123"},
        )
        publico = capacidades.to_public_dict()
        self.assertNotIn("abc.def-123", json.dumps(publico))
        self.assertIn("[REDACTED]", publico["vendor"]["last_error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
