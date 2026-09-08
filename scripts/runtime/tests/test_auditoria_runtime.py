"""Regressões dos defeitos apontados pela auditoria em `runtime` (Parte 1).

Um teste por defeito, cada um falhando no código anterior à correção:

| Defeito                                                   | Teste                            |
|-----------------------------------------------------------|----------------------------------|
| `ClaudeCliExecutor` sem `shutdown` (subprocesso órfão)     | `test_close_encerra_subprocesso*`|
| `_envelopes[execution_id]` nunca removido (vazamento)      | `test_envelope_em_voo_*`         |
| `_binary_path` mutado fora do lock                         | `test_handshake_nao_muta_*`      |
| executor usado depois de `close()`                         | `test_uso_depois_de_close_*`     |
| `capabilities.vendor` vazando em `to_public_dict`          | `test_segredo_nao_sai_*`         |
| `AgentContractError` escapando do `connect`                | `test_connect_divergente_*`      |
| `tuple(x or ())` derrubando o coordenador                  | `test_campo_nao_iteravel_*`      |
| `save_binding` gravando segredo em texto puro              | `test_segredo_nao_e_persistido`  |
| escopo de repo sem `normcase`                              | `test_escopo_de_repo_*`          |
| `ALTER TABLE` concorrente / coluna duplicada               | `test_abertura_concorrente_*`    |
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from runtime import agents as A
from runtime import bindings as B
from runtime import envelopes as E
from runtime import tasks as T
from runtime.executors import agent_adapters as AD
from runtime.executors.claude_cli import ClaudeCliExecutor
from runtime.executors.local_thread import LocalThreadExecutor


def _binding(**kwargs):
    data = {
        "binding_id": "bind-1",
        "agent_id": "local",
        "adapter_version": "local/1",
        "transport": "process",
        "connected": True,
    }
    data.update(kwargs)
    return A.AgentBinding(**data)


class _FakeAdapter(AD.BaseExecutorAdapter):
    """Adaptador mínimo sobre um `LocalThreadExecutor` real."""

    agent_id = "local"
    adapter_version = "fake/1"

    def __init__(self, registry=None):
        super().__init__()
        self._registry = dict(registry or {"__wiki_ai_probe__": AD._probe_callable})
        self.criados = []

    def describe(self):
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process",),
            capabilities=A.AgentCapabilities(dispatch=True, vendor={"api_key": "SEGREDO-CAP"}),
            distribution="bundled",
            summary="adaptador de teste",
        )

    def _handshake(self, transport, config):
        return {"connected": True, "detail": "ok", "host_version": "x", "model": None}

    def _new_executor(self, config):
        executor = LocalThreadExecutor(registry=self._registry, max_workers=1)
        self.criados.append(executor)
        return executor


class AdapterLifecycleTest(unittest.TestCase):
    """Ciclo de vida do executor dentro do adaptador."""

    def test_envelope_em_voo_e_descartado_ao_terminar(self):
        """`_envelopes` é buffer de despacho — não pode crescer sem limite."""
        adapter = _FakeAdapter()
        binding = adapter.connect("process", {})
        self.addCleanup(adapter.close, binding)

        envelope = E.TaskEnvelope(
            task_id="t-1",
            objective_id="obj-1",
            input_revision="rev",
            context_hash="",
            objective={"kind": "__wiki_ai_probe__", "objective_id": "probe"},
            result_schema={"version": "worker_result/1"},
            lease_id="l", attempt_id="1",
        )
        execution_id = adapter.submit(binding, envelope)
        self.assertIn(execution_id, adapter._envelopes)

        limite = time.time() + 5
        observado = {}
        while time.time() < limite:
            observado = adapter.poll(binding, execution_id)
            if observado.get("result") is not None or observado.get("error"):
                break
            time.sleep(0.01)
        self.assertIsNotNone(observado.get("result"), observado)
        self.assertEqual(
            adapter._envelopes, {},
            "envelope de execução terminada precisa sair do buffer (vazamento)",
        )

    def test_uso_depois_de_close_recusa_com_erro_de_contrato(self):
        adapter = _FakeAdapter()
        binding = adapter.connect("process", {})
        adapter.close(binding)
        with self.assertRaises(A.AgentUnavailableError) as ctx:
            adapter.executor_for(binding)
        self.assertIn("fechado", str(ctx.exception))

    def test_close_e_idempotente(self):
        adapter = _FakeAdapter()
        binding = adapter.connect("process", {})
        adapter.close(binding)
        adapter.close(binding)  # não pode levantar

    def test_close_encerra_o_executor_de_verdade(self):
        adapter = _FakeAdapter()
        binding = adapter.connect("process", {})
        executor = adapter.criados[-1]
        adapter.close(binding)
        # Pool encerrado: submeter depois falha (o recurso foi liberado).
        with self.assertRaises(RuntimeError):
            executor.submit("t", {"kind": "__wiki_ai_probe__"}, [], {}, {})

    def test_claude_cli_expoe_shutdown_chamado_por_close(self):
        """`BaseExecutorAdapter.close` só encerra o processo se houver `shutdown`."""
        self.assertTrue(callable(getattr(ClaudeCliExecutor, "shutdown", None)))
        executor = ClaudeCliExecutor(binary_path="binario-que-nao-existe-wiki-ai")
        executor.shutdown(wait=True)  # idempotente e seguro sem execução alguma
        executor.shutdown(wait=False)

    def test_close_encerra_subprocesso_do_claude_cli(self):
        """Nenhum subprocesso sobrevive ao `close()` do adaptador."""
        if not os.path.exists(sys.executable):  # pragma: no cover
            self.skipTest("sem interpretador para o processo filho")
        root = tempfile.mkdtemp(prefix="wk-cli-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        script = os.path.join(root, "dorminhoco.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import sys, time\nif '--version' in sys.argv:\n"
                         "    print('fake 1.0')\n    sys.exit(0)\ntime.sleep(60)\n")

        executor = ClaudeCliExecutor(binary_path=[sys.executable, script], timeout_s=60.0)
        if not executor._detected:  # pragma: no cover - ambiente sem PATH utilizável
            self.skipTest(f"binário falso não detectado: {executor._detect_reason}")
        execution_id = executor.submit("t-1", {"kind": "x"}, [], {}, {})
        proc = executor._procs[execution_id]
        self.assertIsNone(proc.poll(), "o processo precisa estar vivo antes do shutdown")

        executor.shutdown(wait=True)
        self.assertIsNotNone(proc.poll(), "shutdown precisa encerrar o subprocesso órfão")

    def test_handshake_nao_muta_o_binary_path_do_adaptador(self):
        adapter = AD.ClaudeCodeAgentAdapter(binary_path="claude-original")
        try:
            adapter._handshake("process", {"binary_path": "outro-binario"})
        except Exception:  # pragma: no cover - o handshake pode falhar; o estado não muda
            pass
        self.assertEqual(
            adapter._binary_path, "claude-original",
            "o handshake não pode trocar o binário do adaptador (corrida entre connects)",
        )

    def test_handshake_concorrente_nao_mistura_binarios(self):
        adapter = AD.ClaudeCodeAgentAdapter(binary_path="claude-original")
        erros = []

        def _bate(nome):
            try:
                for _ in range(20):
                    adapter._handshake("process", {"binary_path": nome})
                    if adapter._binary_path != "claude-original":
                        erros.append(adapter._binary_path)
            except Exception as exc:  # pragma: no cover
                erros.append(str(exc))

        fios = [threading.Thread(target=_bate, args=(f"binario-{i}",)) for i in range(4)]
        for fio in fios:
            fio.start()
        for fio in fios:
            fio.join()
        self.assertEqual(erros, [])


class ConnectAndSecretsTest(unittest.TestCase):
    """`connect` sem recurso vazado e saída pública sem segredo."""

    def test_connect_divergente_fecha_recurso_e_levanta_erro_de_agente(self):
        class _Divergente(_FakeAdapter):
            def connect(self, transport, config):
                binding = super().connect(transport, config)
                # Binding que NÃO corresponde ao agente pedido.
                return A.AgentBinding(
                    binding_id=binding.binding_id,
                    agent_id="outro-agente",
                    adapter_version=binding.adapter_version,
                    transport=binding.transport,
                    connected=True,
                )

        adapter = _Divergente()
        registry = A.AgentRegistry()
        registry.register(adapter)
        with self.assertRaises(A.AgentUnavailableError) as ctx:
            registry.connect("local", "process")
        self.assertIn("divergente", str(ctx.exception))
        self.assertEqual(
            adapter._executors, {},
            "binding recusado não pode deixar executor vivo e sem dono",
        )

    def test_connect_sem_handshake_levanta_handshake_failed(self):
        class _NaoConecta(_FakeAdapter):
            def _handshake(self, transport, config):
                return {"connected": False, "detail": "host não autenticado"}

        registry = A.AgentRegistry()
        registry.register(_NaoConecta())
        with self.assertRaises(A.HandshakeFailed):
            registry.connect("local", "process")

    def test_segredo_nao_sai_no_vendor_das_capacidades(self):
        binding = _binding(
            capabilities=A.AgentCapabilities(vendor={"api_key": "SEGREDO-CAP", "regiao": "br"}),
            vendor={"token": "SEGREDO-TOPO", "host": "local"},
        )
        publico = binding.to_public_dict()
        texto = json.dumps(publico, ensure_ascii=False)
        self.assertNotIn("SEGREDO-CAP", texto)
        self.assertNotIn("SEGREDO-TOPO", texto)
        # O que NÃO é segredo continua visível.
        self.assertEqual(publico["capabilities"]["vendor"]["regiao"], "br")
        self.assertEqual(publico["vendor"]["host"], "local")


class EnvelopeMalformedTest(unittest.TestCase):
    """Campo não iterável vira recusa tipada, nunca `TypeError` no laço."""

    def _envelope(self):
        return E.TaskEnvelope(
            task_id="t-1", objective_id="obj-1", input_revision="rev",
            context_hash="", objective={}, result_schema={}, lease_id="l", attempt_id="1",
            execution_id="e-1",
        )

    def _resultado(self, **extra):
        base = {
            "protocol_version": E.PROTOCOL_VERSION,
            "task_id": "t-1", "execution_id": "e-1", "attempt_id": "1",
            "objective_id": "obj-1", "input_revision": "rev", "context_hash": "",
            "execution_status": "done", "claims": {"objective_id": "obj-1"},
        }
        base.update(extra)
        return base

    def test_campo_nao_iteravel_vira_envelope_error(self):
        for campo in ("evidence_refs", "reading_satisfied", "diagnostics", "remaining_needs"):
            with self.subTest(campo=campo):
                with self.assertRaises(E.EnvelopeError) as ctx:
                    E.validate_result(self._resultado(**{campo: 3}), self._envelope())
                self.assertEqual(ctx.exception.reason, E.R_MALFORMED)
                self.assertIn(campo, ctx.exception.detail)

    def test_string_nao_vira_lista_de_caracteres(self):
        with self.assertRaises(E.EnvelopeError):
            E.validate_result(self._resultado(evidence_refs="a,b"), self._envelope())

    def test_lista_valida_continua_aceita(self):
        validado = E.validate_result(
            self._resultado(evidence_refs=[{"path": "a.py"}], diagnostics=[{"message": "x"}]),
            self._envelope(),
        )
        self.assertEqual(len(validado.evidence_refs), 1)


class BindingStorePersistenceTest(unittest.TestCase):
    """`agents.json`: sem segredo, com escopo normalizado e trava entre processos."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-bind-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = B.BindingStore(self.root)

    def test_segredo_nao_e_persistido_em_texto_puro(self):
        self.store.save_binding(
            _binding(
                vendor={"api_key": "SEGREDO-TOPO"},
                capabilities=A.AgentCapabilities(vendor={"token": "SEGREDO-CAP"}),
            )
        )
        with open(self.store.path, encoding="utf-8") as handle:
            bruto = handle.read()
        self.assertNotIn("SEGREDO-TOPO", bruto)
        self.assertNotIn("SEGREDO-CAP", bruto)
        # O binding continua utilizável depois do round-trip.
        lido = self.store.get_binding()
        self.assertEqual(lido.agent_id, "local")
        self.assertEqual(lido.transport, "process")

    def test_segredo_nao_e_impresso_na_saida_publica(self):
        self.store.save_binding(
            _binding(capabilities=A.AgentCapabilities(vendor={"api_key": "SEGREDO-CAP"}))
        )
        self.assertNotIn("SEGREDO-CAP", json.dumps(self.store.to_public_dict()))

    def test_escopo_de_repo_ignora_caixa_e_separador(self):
        base = os.path.join(self.root, "Repo")
        os.makedirs(base, exist_ok=True)
        self.store.save_binding(_binding(binding_id="bind-repo"), repo=base)

        variantes = [base.replace("\\", "/"), base + os.sep]
        if os.path.normcase("A") == "a":  # sistema insensível à caixa (Windows)
            variantes.append(base.upper())
        for variante in variantes:
            with self.subTest(variante=variante):
                resolvido = self.store.resolve(repo=variante)
                self.assertEqual(resolvido.origin, B.ORIGIN_REPO, variante)
                self.assertEqual(resolvido.binding.binding_id, "bind-repo")

    def test_lease_de_repo_usa_o_mesmo_escopo_normalizado(self):
        base = os.path.join(self.root, "Repo2")
        os.makedirs(base, exist_ok=True)
        self.store.open_lease("lease-1", binding_id="bind-1", repo=base)
        ativos = self.store.active_leases(repo=base.replace("\\", "/"))
        self.assertEqual([r.lease_id for r in ativos], ["lease-1"])

    def test_escritas_concorrentes_nao_se_perdem(self):
        """Trava entre processos: dois escritores, nenhum lease sumido."""
        erros = []

        def _abre(indice):
            try:
                loja = B.BindingStore(self.root)
                for n in range(10):
                    loja.open_lease(f"lease-{indice}-{n}", binding_id="bind-1")
            except Exception as exc:  # pragma: no cover
                erros.append(str(exc))

        fios = [threading.Thread(target=_abre, args=(i,)) for i in range(4)]
        for fio in fios:
            fio.start()
        for fio in fios:
            fio.join()
        self.assertEqual(erros, [])
        self.assertEqual(
            len(self.store.active_leases()), 40,
            "nenhuma escrita pode ser sobrescrita pela concorrente",
        )


class _Cursor:
    """Cursor mínimo: só `fetchall`, que é o que `_ensure_columns` consome."""

    def __init__(self, linhas):
        self._linhas = list(linhas)

    def fetchall(self):
        return list(self._linhas)


class SchemaConcurrencyTest(unittest.TestCase):
    """`ALTER TABLE` concorrente não pode derrubar a abertura do banco."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-schema-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = os.path.join(self.root, "runtime.db")

    def test_coluna_duplicada_e_absorvida(self):
        """A coluna já existir é SUCESSO do passo, não falha.

        A corrida real é: dois processos leem `PRAGMA table_info` antes de
        qualquer um escrever, os dois decidem acrescentar a coluna, e o segundo
        `ALTER TABLE` recebe "duplicate column name". Aqui ela é reproduzida
        exatamente — o `table_info` mente (omite a coluna), o `ALTER` vai para
        o banco de verdade e falha — para que o teste exercite o `except`, e
        não apenas o caminho em que nada precisa ser feito.
        """
        T.TaskStore.open(self.path).close()
        real = sqlite3.connect(self.path)
        self.addCleanup(real.close)

        class _ConnCorrida:
            """Vê a tabela SEM `result_hash`; escreve no banco que já a tem."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.startswith("PRAGMA table_info"):
                    linhas = [
                        l for l in self._conn.execute(sql, *args).fetchall()
                        if l[1] != "result_hash"
                    ]
                    return _Cursor(linhas)
                return self._conn.execute(sql, *args)

        # Sem a correção, isto propaga sqlite3.OperationalError e derruba
        # `apply_schema` -> `connect` -> a abertura inteira de runtime.db.
        self.assertEqual(T._ensure_columns(_ConnCorrida(real)), [])

    def test_outro_operational_error_continua_subindo(self):
        """Só "duplicate column" é absorvido — o resto continua sendo erro."""
        T.TaskStore.open(self.path).close()
        real = sqlite3.connect(self.path)
        self.addCleanup(real.close)

        class _ConnQuebrada:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.startswith("PRAGMA table_info"):
                    return _Cursor([
                        l for l in self._conn.execute(sql, *args).fetchall()
                        if l[1] != "result_hash"
                    ])
                raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            T._ensure_columns(_ConnQuebrada(real))

    def test_aberturas_concorrentes_do_mesmo_banco(self):
        erros = []

        def _abre():
            try:
                loja = T.TaskStore.open(self.path)
                loja.close()
            except Exception as exc:
                erros.append(f"{type(exc).__name__}: {exc}")

        fios = [threading.Thread(target=_abre) for _ in range(8)]
        for fio in fios:
            fio.start()
        for fio in fios:
            fio.join()
        self.assertEqual(erros, [], "abrir o mesmo runtime.db em paralelo não pode falhar")


if __name__ == "__main__":
    unittest.main()
