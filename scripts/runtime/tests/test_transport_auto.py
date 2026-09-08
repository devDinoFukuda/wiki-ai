"""Seleção de transporte por capacidade verificada (spec §10.4.3).

Requisito coberto (S10.4.3-05): "`auto` é seleção do transporte pelo adaptador
e pelas capacidades verificadas; não é tentativa de adivinhar argumentos. Se
nenhum transporte estiver disponível, o estado é bloqueado com instrução
específica para aquele agente."

O que estes testes provam, em código:

* `auto` NÃO devolve mais `descriptor.transports[0]` cegamente: com `process`
  indisponível e `session` disponível, a escolha é `session`.
* Nenhum transporte disponível ⇒ `TransportUnavailable` (um `HandshakeFailed`)
  carregando os `setup_steps` executáveis daquele agente — bloqueio com
  instrução, nunca tentativa às cegas.
* Transporte EXPLÍCITO indisponível ⇒ erro com o detalhe verificado daquele
  transporte, sem cair no outro transporte disponível.
* Adaptador legado (sem `available_transports`) continua funcionando com o
  comportamento histórico, mas a escolha fica marcada `transport_selection=
  "declared"` no binding e na sonda — nunca `"verified"`.
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping

from runtime import agents as A
from runtime.executors.agent_adapters import (
    BaseExecutorAdapter,
    ClaudeCodeAgentAdapter,
    LocalAgentAdapter,
)
from runtime.executors.base import ExecutionState


# --------------------------------------------------------------------------
# Adaptadores de teste
# --------------------------------------------------------------------------


class _MemoryExecutor:
    """Executor síncrono em memória (protocolo `AgentExecutor`)."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._n = 0

    def capabilities(self) -> dict:
        return {"dispatch": True, "concurrency": 1, "structured_output": True}

    def submit(self, task_id, objective, references=None, schema=None, policy=None) -> str:
        self._n += 1
        execution_id = f"mem:{task_id}:{self._n}"
        self._records[execution_id] = {"state": ExecutionState.DONE}
        return execution_id

    def status(self, execution_id) -> dict:
        return {"state": self._records[execution_id]["state"], "heartbeat": None}

    def result(self, execution_id) -> dict:
        return {"execution_id": execution_id, "output": {"objective_id": "probe"}}

    def cancel(self, execution_id) -> bool:
        self._records[execution_id]["state"] = ExecutionState.CANCELLED
        return True

    def shutdown(self, wait: bool = True) -> None:
        return None


class VerifyingAdapter(BaseExecutorAdapter):
    """Declara `("process", "session")` e VERIFICA cada um (§10.4.3).

    `availability` é a condição real simulada: no `process`, o binário que
    responde; no `session`, a ponte que responde ao handshake.
    """

    agent_id = "fake-transport"
    adapter_version = "fake-transport/1"

    def __init__(self, availability: Mapping[str, tuple[bool, str]] | None = None) -> None:
        super().__init__()
        self.availability = dict(
            availability
            or {
                "process": (True, "binário respondeu a --version"),
                "session": (True, "ponte respondeu ao handshake"),
            }
        )
        self.verified_calls: list[str] = []

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.adapter_version,
            transports=("process", "session"),
            capabilities=A.AgentCapabilities(dispatch=True, supports_session=True),
            verified_versions=("fake-host/1",),
            setup_steps=(
                A.SetupStep(
                    step_id="install-bridge",
                    description="instalar a ponte de sessão do agente",
                    argv=("fake-agent", "install", "--bridge"),
                ),
                A.SetupStep(
                    step_id="authenticate",
                    description="autenticar o host",
                    manual_action="concluir o login do provedor no host",
                    verify_argv=("fake-agent", "whoami"),
                ),
            ),
            distribution="extension",
            summary="adaptador de teste com verificação de transporte",
        )

    def _verify_transport(self, transport: str, config: Mapping[str, Any]) -> tuple[bool, str]:
        self.verified_calls.append(transport)
        return self.availability.get(transport, (False, "transporte não verificado"))

    def _handshake(self, transport: str, config: Mapping[str, Any]) -> dict:
        available, detail = self.availability.get(transport, (False, "indisponível"))
        return {
            "connected": available,
            "detail": detail,
            "host_version": "fake-host/1",
            "model": None,
        }

    def _new_executor(self, config: Mapping[str, Any]) -> _MemoryExecutor:
        return _MemoryExecutor()


class LegacyTransportAdapter:
    """Adaptador do contrato MÍNIMO: sem `available_transports`.

    É a compatibilidade exigida: extensões já registradas continuam válidas e
    caem no comportamento histórico (primeiro transporte declarado).
    """

    agent_id = "legacy-transport"

    def __init__(self) -> None:
        self.connected_with: list[str] = []

    def describe(self) -> A.AgentDescriptor:
        return A.AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version="legacy-transport/1",
            transports=("process", "session"),
            capabilities=A.AgentCapabilities(dispatch=True),
            setup_steps=(
                A.SetupStep(
                    step_id="verify",
                    description="verificar o despacho pela sonda do fluxo real",
                    argv=("wk", "doctor", "--probe-agent"),
                ),
            ),
            distribution="extension",
        )

    def connect(self, transport: str, config: Mapping[str, Any]) -> A.AgentBinding:
        self.connected_with.append(transport)
        return A.AgentBinding(
            binding_id=A.new_binding_id(self.agent_id),
            agent_id=self.agent_id,
            adapter_version="legacy-transport/1",
            transport=transport,
            connected=True,
        )

    def probe(self, binding: A.AgentBinding) -> A.ProbeResult:
        return A.ProbeResult(integration_available=True, agent_connected=True)

    def submit(self, binding, envelope) -> str:  # pragma: no cover - não usado
        return "legacy:1"

    def poll(self, binding, execution_id) -> dict:  # pragma: no cover - não usado
        return {"state": "done", "result": None, "error": None}

    def cancel(self, binding, execution_id) -> bool:  # pragma: no cover - não usado
        return True

    def close(self, binding) -> None:
        return None


def _registry(adapter) -> A.AgentRegistry:
    registry = A.AgentRegistry()
    registry.register(adapter)
    return registry


# --------------------------------------------------------------------------
# `auto` escolhe por disponibilidade verificada
# --------------------------------------------------------------------------


class AutoSelectionTest(unittest.TestCase):
    def test_auto_pula_transporte_indisponivel_e_escolhe_o_disponivel(self):
        adapter = VerifyingAdapter(
            {
                "process": (False, "binário não encontrado no PATH"),
                "session": (True, "ponte respondeu ao handshake"),
            }
        )
        registry = _registry(adapter)
        choice = registry.resolve_transport_choice("fake-transport", "auto")
        self.assertEqual(choice.transport, "session")
        self.assertEqual(choice.selection, A.SELECTION_VERIFIED)
        self.assertTrue(choice.verified)
        # E a decisão inteira sai pelo atalho histórico (string).
        self.assertEqual(registry.resolve_transport("fake-transport", "auto"), "session")
        self.assertEqual(
            registry.resolve_transport("fake-transport"), "session",
            "o padrão do parâmetro também é seleção verificada",
        )

    def test_auto_respeita_a_ordem_de_preferencia_do_descritor(self):
        registry = _registry(VerifyingAdapter())
        choice = registry.resolve_transport_choice("fake-transport", "auto")
        self.assertEqual(choice.transport, "process", "ambos disponíveis ⇒ o preferido")

    def test_connect_auto_usa_o_transporte_verificado_no_binding(self):
        adapter = VerifyingAdapter(
            {"process": (False, "binário ausente"), "session": (True, "ponte viva")}
        )
        registry = _registry(adapter)
        binding = registry.connect("fake-transport", transport="auto")
        self.addCleanup(registry.close, binding)
        self.assertEqual(binding.transport, "session")
        self.assertEqual(binding.transport_selection, A.SELECTION_VERIFIED)
        self.assertEqual(binding.to_dict()["transport_selection"], A.SELECTION_VERIFIED)

    def test_config_do_connect_chega_a_verificacao(self):
        """A verificação enxerga a MESMA configuração do connect (ex.: binary_path)."""

        class _ConfigAware(VerifyingAdapter):
            def _verify_transport(self, transport, config):
                if transport == "process" and config.get("binary_path") == "existe":
                    return True, "binário informado responde"
                return False, "binário informado não responde"

        registry = _registry(_ConfigAware())
        self.assertEqual(
            registry.resolve_transport("fake-transport", "auto", {"binary_path": "existe"}),
            "process",
        )
        with self.assertRaises(A.TransportUnavailable):
            registry.resolve_transport("fake-transport", "auto", {"binary_path": "sumiu"})


# --------------------------------------------------------------------------
# Bloqueio com instrução específica
# --------------------------------------------------------------------------


class BlockedWithInstructionTest(unittest.TestCase):
    def setUp(self):
        self.adapter = VerifyingAdapter(
            {
                "process": (False, "binário não encontrado no PATH"),
                "session": (False, "ponte não respondeu ao handshake"),
            }
        )
        self.registry = _registry(self.adapter)

    def test_nenhum_transporte_disponivel_bloqueia_com_setup_steps(self):
        with self.assertRaises(A.TransportUnavailable) as ctx:
            self.registry.resolve_transport_choice("fake-transport", "auto")
        exc = ctx.exception
        self.assertEqual(exc.agent_id, "fake-transport")
        self.assertEqual(exc.transport, "auto")
        # A instrução é a DAQUELE agente, com passo executável.
        self.assertEqual(
            [s.step_id for s in exc.setup_steps], ["install-bridge", "authenticate"]
        )
        self.assertIn("fake-agent install --bridge", str(exc))
        self.assertIn("binário não encontrado no PATH", str(exc))
        self.assertIn("ponte não respondeu ao handshake", str(exc))
        acoes = exc.next_actions()
        self.assertEqual([list(a["argv"]) for a in acoes][0], ["fake-agent", "install", "--bridge"])
        self.assertEqual(
            [list(a["argv"]) for a in acoes][1], ["fake-agent", "whoami"],
            "passo manual expõe o comando de verificação como argv acionável",
        )
        self.assertEqual(
            {o["transport"]: o["available"] for o in exc.to_dict()["transports"]},
            {"process": False, "session": False},
        )

    def test_bloqueio_e_handshake_failed_e_agent_unavailable(self):
        """Quem já distingue 'engine desconhecida' de 'despacho indisponível'
        continua classificando este caso como despacho indisponível."""
        self.assertTrue(issubclass(A.TransportUnavailable, A.HandshakeFailed))
        self.assertTrue(issubclass(A.TransportUnavailable, A.AgentUnavailableError))
        with self.assertRaises(A.HandshakeFailed):
            self.registry.connect("fake-transport", transport="auto")

    def test_connect_bloqueado_nao_tenta_o_adaptador(self):
        with self.assertRaises(A.TransportUnavailable):
            self.registry.connect("fake-transport", transport="auto")
        self.assertEqual(
            self.adapter._executors, {},
            "sem transporte disponível não se constrói executor nenhum",
        )
        self.assertEqual(self.adapter._bindings, {})


# --------------------------------------------------------------------------
# Transporte explícito
# --------------------------------------------------------------------------


class ExplicitTransportTest(unittest.TestCase):
    def setUp(self):
        self.adapter = VerifyingAdapter(
            {
                "process": (False, "binário não encontrado no PATH"),
                "session": (True, "ponte respondeu ao handshake"),
            }
        )
        self.registry = _registry(self.adapter)

    def test_explicito_indisponivel_falha_com_o_detalhe_daquele_transporte(self):
        with self.assertRaises(A.TransportUnavailable) as ctx:
            self.registry.resolve_transport_choice("fake-transport", "process")
        exc = ctx.exception
        self.assertEqual(exc.transport, "process")
        self.assertIn("binário não encontrado no PATH", str(exc))
        self.assertNotIn(
            "ponte respondeu", str(exc),
            "o erro é sobre o transporte pedido, não sobre o outro",
        )
        self.assertIn("fake-agent install --bridge", str(exc), "instrução específica presente")

    def test_explicito_indisponivel_nunca_cai_no_transporte_disponivel(self):
        with self.assertRaises(A.TransportUnavailable):
            self.registry.connect("fake-transport", transport="process")
        self.assertEqual(self.adapter._bindings, {}, "nenhum binding foi criado")

    def test_explicito_disponivel_conecta_marcado_como_verificado(self):
        binding = self.registry.connect("fake-transport", transport="session")
        self.addCleanup(self.registry.close, binding)
        self.assertEqual(binding.transport, "session")
        self.assertEqual(binding.transport_selection, A.SELECTION_VERIFIED)

    def test_transporte_nao_declarado_continua_recusado_antes_da_verificacao(self):
        registry = _registry(LocalAgentAdapter(max_workers=1))
        with self.assertRaises(A.AgentUnavailableError) as ctx:
            registry.resolve_transport_choice("local", "session")
        self.assertNotIsInstance(
            ctx.exception, A.TransportUnavailable,
            "transporte não oferecido é erro de oferta, não de disponibilidade",
        )
        self.assertIn("não oferece transporte", str(ctx.exception))


# --------------------------------------------------------------------------
# Compatibilidade: adaptador sem o método novo
# --------------------------------------------------------------------------


class LegacyAdapterTest(unittest.TestCase):
    def test_sem_available_transports_a_selecao_e_declarada(self):
        adapter = LegacyTransportAdapter()
        registry = _registry(adapter)
        self.assertIsNone(registry.transport_options("legacy-transport"))
        choice = registry.resolve_transport_choice("legacy-transport", "auto")
        self.assertEqual(choice.transport, "process", "comportamento histórico preservado")
        self.assertEqual(choice.selection, A.SELECTION_DECLARED)
        self.assertFalse(choice.verified)
        self.assertEqual(choice.options, ())

    def test_binding_legado_registra_declared(self):
        adapter = LegacyTransportAdapter()
        registry = _registry(adapter)
        binding = registry.connect("legacy-transport", transport="auto")
        self.addCleanup(registry.close, binding)
        self.assertEqual(adapter.connected_with, ["process"])
        self.assertEqual(binding.transport_selection, A.SELECTION_DECLARED)
        self.assertEqual(binding.to_dict()["transport_selection"], A.SELECTION_DECLARED)

    def test_registro_persistido_sem_o_campo_e_lido_como_declared(self):
        antigo = {
            "binding_id": "bind-legacy-1",
            "agent_id": "legacy-transport",
            "adapter_version": "legacy-transport/1",
            "transport": "process",
            "connected": True,
        }
        binding = A.AgentBinding.from_dict(antigo)
        self.assertEqual(binding.transport_selection, A.SELECTION_DECLARED)

    def test_transport_selection_invalido_e_recusado(self):
        with self.assertRaises(A.AgentContractError):
            A.AgentBinding(
                binding_id="b1",
                agent_id="legacy-transport",
                adapter_version="1",
                transport="process",
                transport_selection="talvez",
            )


# --------------------------------------------------------------------------
# Contrato de `available_transports`
# --------------------------------------------------------------------------


class TransportOptionsContractTest(unittest.TestCase):
    def test_ordem_do_descritor_prevalece_sobre_a_ordem_reportada(self):
        class _Invertido(VerifyingAdapter):
            def available_transports(self, config=None):
                return (("session", True, "ponte viva"), ("process", True, "binário vivo"))

        options = _registry(_Invertido()).transport_options("fake-transport")
        self.assertEqual([o.transport for o in options], ["process", "session"])

    def test_transporte_declarado_e_nao_reportado_conta_como_indisponivel(self):
        class _Parcial(VerifyingAdapter):
            def available_transports(self, config=None):
                return (("session", True, "ponte viva"),)

        options = _registry(_Parcial()).transport_options("fake-transport")
        self.assertEqual(
            {o.transport: o.available for o in options},
            {"process": False, "session": True},
            "omissão não é disponibilidade",
        )
        self.assertIn("não verificou", dict((o.transport, o.detail) for o in options)["process"])

    def test_tuplas_e_mapas_do_adaptador_de_terceiro_sao_aceitos(self):
        class _Mapas(VerifyingAdapter):
            def available_transports(self, config=None):
                return [
                    {"transport": "process", "available": False, "detail": "sem binário"},
                    ["session", True, "ponte viva"],
                ]

        registry = _registry(_Mapas())
        self.assertEqual(registry.resolve_transport("fake-transport", "auto"), "session")

    def test_retorno_fora_do_contrato_e_erro_de_contrato(self):
        class _Errado(VerifyingAdapter):
            def available_transports(self, config=None):
                return "process"

        with self.assertRaises(A.AgentContractError):
            _registry(_Errado()).transport_options("fake-transport")

    def test_excecao_do_adaptador_vira_erro_de_contrato(self):
        class _Explode(VerifyingAdapter):
            def available_transports(self, config=None):
                raise ZeroDivisionError("bug do adaptador")

        with self.assertRaises(A.AgentContractError):
            _registry(_Explode()).transport_options("fake-transport")

    def test_transporte_desconhecido_reportado_e_recusado(self):
        with self.assertRaises(A.AgentContractError):
            A.TransportAvailability("mcp", True, "inventado")

    def test_disponibilidade_e_desempacotavel_como_tupla(self):
        transport, available, detail = A.TransportAvailability("process", True, "ok")
        self.assertEqual((transport, available, detail), ("process", True, "ok"))


# --------------------------------------------------------------------------
# Adaptadores distribuídos e sonda
# --------------------------------------------------------------------------


class BundledAdaptersTest(unittest.TestCase):
    def test_local_verifica_o_proprio_transporte(self):
        adapter = LocalAgentAdapter(max_workers=1)
        options = adapter.available_transports({})
        self.assertEqual([o.transport for o in options], ["process"])
        self.assertTrue(options[0].available, options[0].detail)
        registry = _registry(adapter)
        binding = registry.connect("local", transport="auto")
        self.addCleanup(registry.close, binding)
        self.assertEqual(binding.transport_selection, A.SELECTION_VERIFIED)

    def test_claude_code_sem_binario_bloqueia_com_a_instrucao_do_agente(self):
        adapter = ClaudeCodeAgentAdapter(binary_path="binario-inexistente-wiki-ai-test")
        options = adapter.available_transports({})
        self.assertFalse(options[0].available)
        registry = _registry(adapter)
        with self.assertRaises(A.TransportUnavailable) as ctx:
            registry.connect("claude-code", transport="auto")
        self.assertIn("npm install -g @anthropic-ai/claude-code", str(ctx.exception))
        self.assertTrue(ctx.exception.next_actions())
        self.assertEqual(
            adapter._executors, {}, "bloqueio não pode deixar executor vivo"
        )

    def test_sonda_publica_o_regime_da_selecao(self):
        registry = _registry(VerifyingAdapter())
        binding = registry.connect("fake-transport", transport="auto")
        self.addCleanup(registry.close, binding)
        probe = registry.probe(binding)
        self.assertEqual(probe.transport_selection, A.SELECTION_VERIFIED)
        self.assertEqual(probe.to_dict()["transport_selection"], A.SELECTION_VERIFIED)
        self.assertEqual(
            {o["transport"] for o in probe.to_dict()["transports"]}, {"process", "session"}
        )
        self.assertTrue(probe.ok, probe.detail)

    def test_registro_persistido_guarda_o_regime_da_selecao(self):
        import tempfile

        from runtime.bindings import BindingStore

        registry = _registry(VerifyingAdapter())
        binding = registry.connect("fake-transport", transport="auto")
        self.addCleanup(registry.close, binding)
        with tempfile.TemporaryDirectory() as root:
            store = BindingStore(root)
            store.save_binding(binding)
            self.assertEqual(
                store.get_binding().transport_selection, A.SELECTION_VERIFIED
            )
            self.assertEqual(
                store.to_public_dict()["transport_selection"], A.SELECTION_VERIFIED
            )

    def test_sonda_de_adaptador_legado_declara_declared(self):
        adapter = LegacyTransportAdapter()
        registry = _registry(adapter)
        binding = registry.connect("legacy-transport", transport="auto")
        self.addCleanup(registry.close, binding)
        probe = registry.probe(binding)
        self.assertEqual(probe.transport_selection, A.SELECTION_DECLARED)
        self.assertEqual(probe.transports, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
