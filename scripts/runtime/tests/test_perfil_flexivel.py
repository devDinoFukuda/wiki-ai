"""Flexibilização por sistema SEM abrir a porta de controle.

Cada teste aqui cobre uma capacidade nova do runtime — vocabulário de
resultado ampliável por perfil, progresso por campo de contrato, política de
execução do operador chegando à negociação e orçamento efetivo da cadeia — e,
em par com ela, a garantia que continua fechada: `CONTROL_FIELDS` recusa, e
schema sem `with_extra` recusa.
"""

import os
import shutil
import tempfile
import unittest

from runtime import agents as A
from runtime import coordinator as C
from runtime import state as S
from runtime import tasks as T
from runtime.executors.local_thread import LocalThreadExecutor


# --------------------------------------------------------------------------
# 1. ResultSchema.with_extra
# --------------------------------------------------------------------------


class WithExtraTest(unittest.TestCase):
    def test_chave_extra_sem_with_extra_continua_recusada(self):
        """A porta só abre por decisão do operador — nunca por iniciativa do worker."""
        problemas = C.DEFAULT_SCHEMA.validate(
            {"objective_id": "obj-1", "risco_operacional": "alto"}
        )
        self.assertEqual(
            [reason for reason, _ in problemas], [C.RejectionReason.SCHEMA_INVALID]
        )
        self.assertIn("risco_operacional", problemas[0][1])

    def test_chave_extra_declarada_passa_e_strict_continua_verdadeiro(self):
        schema = C.DEFAULT_SCHEMA.with_extra(["risco_operacional"])
        self.assertTrue(schema.strict)
        self.assertEqual(schema.extra, ("risco_operacional",))
        self.assertEqual(
            schema.validate({"objective_id": "obj-1", "risco_operacional": "alto"}), []
        )
        # Outra chave, não declarada, continua recusada pelo MESMO schema.
        self.assertTrue(schema.validate({"objective_id": "obj-1", "outra": 1}))

    def test_chave_extra_colidindo_com_control_field_e_recusada(self):
        for nome in ("budget", "tools", "permissions", "write_path"):
            with self.subTest(campo=nome):
                with self.assertRaises(ValueError) as ctx:
                    C.DEFAULT_SCHEMA.with_extra([nome])
                self.assertIn("CONTROLE", str(ctx.exception))

    def test_control_field_declarado_como_extra_nunca_passa_a_validar(self):
        """Mesmo forjando `optional`, `validate` recusa campo de controle."""
        forjado = C.ResultSchema(optional=("budget",))
        problemas = forjado.validate({"objective_id": "obj-1", "budget": {"max_tokens": 9}})
        self.assertEqual(
            [reason for reason, _ in problemas], [C.RejectionReason.CONTROL_FIELD]
        )

    def test_chave_extra_ja_declarada_e_recusada(self):
        """`profile.result_extra_keys` é ADIÇÃO: não redeclara o que já existe.

        `matrix` está entre as opcionais do núcleo (`coordinator.py:198`) — um
        perfil que a listasse como "extra" estaria dizendo que ela significa
        outra coisa, e o resultado passaria por duas validações incompatíveis.
        """
        for nome in ("contract", "objective_id", "matrix", "reading_needs", "state"):
            with self.subTest(campo=nome):
                self.assertIn(nome, C.DEFAULT_SCHEMA.declared)
                with self.assertRaises(ValueError) as ctx:
                    C.DEFAULT_SCHEMA.with_extra([nome])
                self.assertIn("já é campo declarado", str(ctx.exception))

    def test_with_extra_vazio_devolve_o_mesmo_schema(self):
        """Compatibilidade: sem chave extra, nada muda — nem o objeto."""
        self.assertIs(C.DEFAULT_SCHEMA.with_extra(()), C.DEFAULT_SCHEMA)
        self.assertNotIn("extra", C.DEFAULT_SCHEMA.to_dict())

    def test_to_dict_anuncia_as_extras_ao_worker(self):
        payload = C.DEFAULT_SCHEMA.with_extra(["risco_operacional"]).to_dict()
        self.assertIn("risco_operacional", payload["optional"])
        self.assertEqual(payload["extra"], ["risco_operacional"])
        self.assertIn("budget", payload["forbidden"])


# --------------------------------------------------------------------------
# Harness comum: cadeia real com executor determinístico em processo
# --------------------------------------------------------------------------


class _Engine(LocalThreadExecutor):
    """Executor local que declara `deepening` e limites de contexto.

    Os limites existem para que `AgentCapabilities.negotiate` tenha o que
    CLAMPAR: sem teto declarado, "mínimo entre pedido e declarado" seria só o
    pedido e o teste não provaria negociação nenhuma.
    """

    def capabilities(self):
        caps = dict(super().capabilities())
        caps.update(
            {
                "deepening": True,
                "max_context_bytes": 50_000,
                "max_context_tokens": 8_000,
                "max_duration_s": 120,
            }
        )
        return caps

    def submit(self, task_id, objective, references=None, schema=None, policy=None):
        # A política EFETIVA (já negociada pelo adapter) é registrada para o
        # teste ler: é o único ponto onde se observa o que a engine recebeu.
        self.politicas_recebidas.append(dict(policy or {}))
        self.schemas_recebidos.append(dict(schema or {}))
        return super().submit(task_id, objective, references, schema, policy)


class _ChainCase(unittest.TestCase):
    #: saída do worker; sobrescrita por subclasse
    saida = {"objective_id": "obj-1", "state": "partial"}

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wk-perfil-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = T.TaskStore.open(os.path.join(self.root, "runtime.db"))
        self.addCleanup(self.store.close)
        self.inputs = {"snapshot_id": "snap-1", "source_version_ids": []}
        self.executor = _Engine(registry={"investigation": self._callable}, max_workers=2)
        self.executor.politicas_recebidas = []
        self.executor.schemas_recebidos = []
        self.addCleanup(self.executor.shutdown)
        self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1", "kind": "investigation"},
            self.inputs,
            # Sem teto declarado o `context.Budget` sai zerado e NENHUM pacote
            # cabe: a tarefa falharia em montagem de contexto antes de chegar
            # ao executor, e nada nestes testes seria observado.
            budget={"max_bytes": 200_000, "max_tokens": 50_000},
        )
        self.store.refresh_states()

    @classmethod
    def _callable(cls, objective=None, references=None, schema=None, cancel_event=None):
        return dict(cls.saida)

    def _ultima_tarefa(self):
        return sorted(self.store.all_tasks(), key=lambda t: (t.created_at, t.task_id))[-1]

    def _run_chain(self, outcomes_fn, **kwargs):
        return C.run_chain(
            self.store,
            objective_ids=["obj-1"],
            input_versions=self.inputs,
            outcomes_fn=outcomes_fn,
            repo_id="repo-1",
            executor=self.executor,
            engine_capabilities={"deepening": True},
            **kwargs,
        )

    def _uma_rodada(self):
        """`outcomes_fn` que encerra a cadeia na primeira rodada."""

        def outcomes_fn(round_no):
            return [
                {
                    "objective_id": "obj-1",
                    "state": "complete",
                    "task_id": self._ultima_tarefa().task_id,
                }
            ]

        return outcomes_fn


# --------------------------------------------------------------------------
# 1b. As extras atravessam `run`/`run_chain` até o resultado persistido
# --------------------------------------------------------------------------


class ExtraKeysNoResultadoTest(_ChainCase):
    saida = {
        "objective_id": "obj-1",
        "state": "partial",
        "risco_operacional": {"nivel": "alto", "motivo": "sem rollback"},
    }

    def test_sem_result_extra_keys_a_chave_derruba_o_resultado(self):
        relatorio = self._run_chain(self._uma_rodada())
        self.assertIsNone(self._ultima_tarefa().result)
        recusas = relatorio.round_reports[0].run.rejections
        self.assertTrue(
            any("risco_operacional" in detalhe for _, _, detalhe in recusas), recusas
        )

    def test_com_result_extra_keys_a_chave_e_preservada_no_resultado(self):
        self._run_chain(self._uma_rodada(), result_extra_keys=["risco_operacional"])
        tarefa = self._ultima_tarefa()
        self.assertIsNotNone(tarefa.result)
        self.assertEqual(
            tarefa.result["risco_operacional"], {"nivel": "alto", "motivo": "sem rollback"}
        )
        # O worker foi AVISADO da extensão antes de responder.
        self.assertIn("risco_operacional", self.executor.schemas_recebidos[0]["optional"])
        self.assertEqual(self.executor.schemas_recebidos[0]["extra"], ["risco_operacional"])

    def test_result_extra_keys_nao_abre_campo_de_controle(self):
        with self.assertRaises(ValueError):
            self._run_chain(self._uma_rodada(), result_extra_keys=["budget"])

    def test_chave_invalida_recusa_antes_de_persistir_teto_da_cadeia(self):
        """`set_chain_limits` NÃO roda com configuração inválida.

        `run_chain` gravava teto e orçamento antes da primeira rodada; só
        dentro de `run()` a chave era validada. Uma cadeia ficava com limites
        persistidos para trabalho que nunca ia despachar — e a próxima
        invocação herdava esses limites sem nunca ter rodado.
        """
        antes = self.store.chain_status("obj-1")
        for chave in ("budget", "policy", "matrix", "contract"):
            with self.subTest(chave=chave):
                with self.assertRaises(ValueError):
                    self._run_chain(
                        self._uma_rodada(),
                        result_extra_keys=[chave],
                        budget={"max_tokens": 123},
                        max_rounds=9,
                    )
        depois = self.store.chain_status("obj-1")
        self.assertEqual(depois["budget"], {})
        self.assertEqual(depois["budget"], antes["budget"])
        self.assertEqual(depois["max_rounds"], antes["max_rounds"])
        self.assertEqual(depois["rounds_used"], antes["rounds_used"])
        self.assertEqual(self.executor.politicas_recebidas, [])


# --------------------------------------------------------------------------
# 4. `policy` do operador chega a `negotiate`
# --------------------------------------------------------------------------


class PolicyNegociadaTest(_ChainCase):
    def test_policy_do_operador_chega_negociada_ao_executor(self):
        pedido = {"max_bytes": 999_999, "max_tokens": 4_000, "timeout_s": 30}
        self._run_chain(self._uma_rodada(), policy=pedido)
        recebida = self.executor.politicas_recebidas[0]
        # min(pedido, declarado): bytes clampados pelo teto da engine; tokens e
        # timeout preservados porque cabem.
        self.assertEqual(recebida["max_bytes"], 50_000)
        self.assertEqual(recebida["max_tokens"], 4_000)
        self.assertEqual(recebida["timeout_s"], 30)

    def test_negociacao_e_a_mesma_de_agent_capabilities(self):
        """O que a engine recebe é exatamente `capabilities.negotiate(pedido)`."""
        pedido = {"max_bytes": 999_999, "max_tokens": 4_000, "timeout_s": 30}
        self._run_chain(self._uma_rodada(), policy=pedido)
        caps = A.AgentCapabilities.from_executor(self.executor.capabilities())
        self.assertEqual(self.executor.politicas_recebidas[0], caps.negotiate(pedido))

    def test_sem_policy_o_pedido_negociado_e_o_vazio(self):
        """Compatibilidade: sem `policy`, `negotiate` recebe `{}` como antes.

        O que a engine vê são os limites que ELA declarou (é o que
        `negotiate({})` devolve); nenhum limite novo é inventado pelo laço.
        """
        self._run_chain(self._uma_rodada())
        caps = A.AgentCapabilities.from_executor(self.executor.capabilities())
        self.assertEqual(self.executor.politicas_recebidas[0], caps.negotiate({}))
        self.assertNotIn("max_tokens_pedido", self.executor.politicas_recebidas[0])

    def test_policy_viaja_no_envelope_em_vendor(self):
        envelope = C.envelope_for_task(self._ultima_tarefa(), policy={"max_tokens": 7})
        self.assertEqual(envelope.vendor["policy"], {"max_tokens": 7})


# --------------------------------------------------------------------------
# 5. `budget` da cadeia chega a `chain_status`
# --------------------------------------------------------------------------


class BudgetDaCadeiaTest(_ChainCase):
    def test_budget_e_max_rounds_ficam_visiveis_em_chain_status(self):
        relatorio = self._run_chain(
            self._uma_rodada(), budget={"max_tokens": 120_000}, max_rounds=7
        )
        status = self.store.chain_status("obj-1")
        self.assertEqual(status["budget"], {"max_tokens": 120_000})
        self.assertEqual(status["max_rounds"], 7)
        # É por `ChainReport.chain` que o CLI mostra o envelope da cadeia.
        do_relatorio = relatorio.to_dict()["chain"]["obj-1"]
        self.assertEqual(do_relatorio["budget"], {"max_tokens": 120_000})
        self.assertEqual(do_relatorio["max_rounds"], 7)
        self.assertIn("consumed", do_relatorio)

    def test_budget_efetivo_da_cadeia_governa_budget_exhausted(self):
        self._run_chain(self._uma_rodada(), budget={"max_tokens": 100})
        self.store.record_chain_round("obj-1", usage={"tokens": 100}, counts_round=False)
        status = self.store.chain_status("obj-1")
        esgotado, detalhe = S.budget_exhausted(status["budget"], status["consumed"])
        self.assertTrue(esgotado, status)
        self.assertIn("max_tokens", detalhe)

    def test_sem_budget_a_cadeia_nao_ganha_teto_inventado(self):
        self._run_chain(self._uma_rodada())
        status = self.store.chain_status("obj-1")
        self.assertEqual(status["budget"], {})
        self.assertFalse(S.budget_exhausted(status["budget"], {"tokens": 10**9})[0])


# --------------------------------------------------------------------------
# 3. Progresso por campo de contrato preenchido
# --------------------------------------------------------------------------


class ProgressoPorContratoTest(unittest.TestCase):
    def test_campo_de_contrato_preenchido_conta_como_progresso(self):
        estado = S.new_state("repo-1", "obj-1", "rev-1")
        _, progresso = S.apply_result(
            estado,
            {"result_hash": "h1", "contract": {"regra": {"content": "limite e 1000"}}},
            round_no=1,
        )
        self.assertEqual(progresso.contract_fields_filled, ("regra",))
        self.assertTrue(progresso.has_progress)
        self.assertTrue(progresso.to_dict()["has_progress"])

    def test_reescrita_de_campo_ja_preenchido_continua_sem_progresso(self):
        """A garantia que fica: mudança textual não é progresso."""
        estado = S.new_state("repo-1", "obj-1", "rev-1")
        base, _ = S.apply_result(
            estado, {"result_hash": "h1", "contract": {"regra": {"content": "x"}}}
        )
        _, progresso = S.apply_result(
            base, {"result_hash": "h2", "contract": {"regra": {"content": "x reescrito"}}}
        )
        self.assertEqual(progresso.contract_fields_filled, ())
        self.assertFalse(progresso.has_progress)


#: Campos do §6.3 usados um por rodada: cada rodada PREENCHE um campo que
#: estava vazio, que é exatamente o movimento que passou a contar.
_CAMPOS = ("identidade", "gatilho", "precondicoes", "dados", "decisoes")


class ContinuacaoPorContratoTest(_ChainCase):
    def _outcomes(self, alvo_por_rodada):
        """Cada rodada preenche um campo NOVO do contrato e nada mais.

        Sem leitura fechada, sem evidência nova, sem conflito resolvido: é o
        caso que antes era classificado `no_progress` em cima de trabalho real.
        """
        passo = {"n": 0}

        def outcomes_fn(round_no):
            n = passo["n"]
            passo["n"] += 1
            alvo = alvo_por_rodada(n)
            return [
                {
                    "objective_id": "obj-1",
                    "task_id": self._ultima_tarefa().task_id,
                    "state": "partial",
                    "unmet_needs": [
                        {
                            "kind": "code",
                            "target": alvo,
                            "motivo": "ler",
                            "need_id": "need:" + alvo,
                        }
                    ],
                    "contract_state": {
                        _CAMPOS[n % len(_CAMPOS)]: {"content": "apuracao {}".format(n)}
                    },
                }
            ]

        return outcomes_fn

    def test_rodada_so_com_campo_de_contrato_nao_gera_no_progress(self):
        """Antes: preencher um campo do contrato sem fechar leitura parava a cadeia."""
        relatorio = self._run_chain(
            self._outcomes(lambda n: "f{}.py".format(n)), max_rounds=3
        )
        primeira = relatorio.progress_by_round[0]["progress"]["obj-1"]
        self.assertEqual(primeira["contract_fields_filled"], ["identidade"])
        self.assertTrue(primeira["has_progress"])
        # A rodada 0 GEROU continuação: a cadeia não morreu por falta de progresso.
        self.assertTrue(relatorio.progress_by_round[0]["planned"]["criadas"])
        self.assertEqual(relatorio.stop_reason, S.STOP_BUDGET_EXHAUSTED)
        self.assertGreaterEqual(relatorio.rounds, 2)

    def test_pacote_identico_continua_parando_a_cadeia(self):
        """A guarda de `package_hash` repetido FICA — progresso não a revoga."""
        relatorio = self._run_chain(self._outcomes(lambda n: "mesmo.py"), max_rounds=5)
        # Há progresso semântico em toda rodada (campo novo do contrato) e,
        # ainda assim, a cadeia para: o PACOTE de necessidades se repetiu.
        self.assertEqual(relatorio.stop_reason, S.STOP_NO_PROGRESS)
        self.assertTrue(
            any("já despachado" in d for d in relatorio.diagnostics), relatorio.diagnostics
        )


if __name__ == "__main__":
    unittest.main()
