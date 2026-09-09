"""Profundidade da cadeia: sem teto de rodadas e leitura satisfeita É progresso.

| Aceite                                                                   | Teste |
|--------------------------------------------------------------------------|-------|
| Sem teto por padrão: a cadeia não para por CONTAGEM de rodadas            | `SemTetoDeRodadasTest.test_cadeia_passa_de_tres_rodadas_*` |
| `chain_status` não finge um teto que não existe                          | `test_chain_status_sem_teto_*` |
| `--max-rounds N` continua sendo teto EXPLÍCITO                            | `test_teto_explicito_continua_recusando` |
| `max_rounds=0` continua proibindo qualquer continuação                    | `test_teto_zero_continua_proibindo` |
| Rodada que só satisfez leitura (sem claim novo) NÃO é `no_progress`       | `LeituraSatisfeitaEProgressoTest` |
| Objetivo `discovery` REAL de `analysis.investigation` vira pacote com os  | `DescobertaRealTest` |
| arquivos inteiros e carrega `analysis_directives`                        |       |
"""

import os
import shutil
import tempfile
import unittest

from analysis import capabilities as cap_mod
from analysis import inventory as inv_mod
from analysis import snapshot as snap_mod
from analysis.extractors import registry as ext_registry
from analysis.extractors.base import SourceFile
from analysis.investigation import ObjectiveKind, ReadingTrigger, plan
from analysis.snapshot import resolve_evidence
from runtime import context as CTX
from runtime import coordinator as C
from runtime import state as S
from runtime import tasks as T


GO_FILES = {
    "billing/main.go": (
        "package billing\n\nfunc Total(a int, b int) int {\n\treturn a + b\n}\n"
    ),
    "billing/limite.go": (
        "package billing\n\nimport \"errors\"\n\n"
        "var ErrLimite = errors.New(\"limite\")\n\n"
        "func Aprovar(total int) error {\n"
        "\tif total > 1000 {\n\t\treturn ErrLimite\n\t}\n\treturn nil\n}\n"
    ),
}


class _StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cadeia-profunda-")
        self.store = T.TaskStore.open(os.path.join(self.tmpdir, "runtime.db"))
        self.inputs = {"snapshot_id": "snap-1", "source_version_ids": []}

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _tarefa_base(self):
        return self.store.create_task(
            T.TaskKind.INVESTIGATION,
            {"objective_id": "obj-1", "task_kind": "investigation"},
            self.inputs,
        )

    def _fecha(self, task, rodada):
        """Fecha a tarefa como `done/partial` — como o coordenador faria."""
        lease = self.store.acquire_lease(task.task_id, "owner")
        execution_id = f"exec-{rodada}"
        self.store.start_attempt(task.task_id, lease.lease_id, execution_id)
        self.store.record_result(
            task.task_id,
            lease.lease_id,
            execution_id,
            state=T.TaskState.DONE,
            outcome=T.AttemptOutcome.SUCCEEDED,
            result={"objective_id": "obj-1", "state": "partial"},
            termination_reason="completed",
        )

    @staticmethod
    def _outcome_partial(task_id, rodada):
        """Outcome `partial` com necessidade NOVA a cada rodada (pacote distinto)."""
        return {
            "objective_id": "obj-1",
            "task_id": task_id,
            "state": "partial",
            "unmet_needs": [
                {
                    "kind": "range",
                    "target": f"arquivo_{rodada}.go:1-40",
                    "motivo": f"leitura pendente da rodada {rodada}",
                    "trigger": ReadingTrigger.SOURCE_FILE.value,
                }
            ],
        }

    @staticmethod
    def _progresso(rodada):
        return S.Progress(evidence_accepted=(f"arquivo_{rodada}.go:1-40",)).to_dict()


class SemTetoDeRodadasTest(_StoreTest):
    """`DEFAULT_MAX_CONTINUATION_ROUNDS is None`: parada é material, não aritmética."""

    def test_default_e_sem_teto(self):
        self.assertIsNone(T.DEFAULT_MAX_CONTINUATION_ROUNDS)
        self.assertIsNone(T.get_max_continuation_rounds(self.store))

    def test_cadeia_passa_de_tres_rodadas_sem_recusa_por_contagem(self):
        """ACEITE: 6 rodadas seguidas, nenhuma recusada por 'excede o teto'."""
        task = self._tarefa_base()
        self._fecha(task, 0)
        atual = task.task_id
        for rodada in range(1, 7):
            resultado = C.plan_continuations(
                self.store,
                [self._outcome_partial(atual, rodada)],
                input_versions=self.inputs,
                progress=self._progresso(rodada),
            )
            self.assertEqual(
                resultado["recusadas"],
                [],
                f"rodada {rodada} recusada sem teto declarado: {resultado['recusadas']}",
            )
            self.assertEqual(len(resultado["criadas"]), 1, resultado)
            atual = resultado["criadas"][0]
            self._fecha(self.store.get(atual), rodada)
        self.assertGreaterEqual(self.store.chain_status("obj-1")["rounds_used"], 6)

    def test_chain_status_sem_teto_nao_finge_limite(self):
        """`max_rounds`/`rounds_left` são `None` — nunca um número inventado."""
        status = self.store.chain_status("obj-1")
        self.assertIsNone(status["max_rounds"])
        self.assertIsNone(status["rounds_left"])
        self.store.set_chain_limits("obj-1", max_rounds=4)
        com_teto = self.store.chain_status("obj-1")
        self.assertEqual(com_teto["max_rounds"], 4)
        self.assertEqual(com_teto["rounds_left"], 4)

    def test_teto_explicito_continua_recusando(self):
        """`--max-rounds N` (perfil/CLI) segue sendo teto DAQUELA cadeia."""
        task = self._tarefa_base()
        self._fecha(task, 0)
        primeira = C.plan_continuations(
            self.store,
            [self._outcome_partial(task.task_id, 1)],
            input_versions=self.inputs,
            max_rounds=1,
            progress=self._progresso(1),
        )
        self.assertEqual(len(primeira["criadas"]), 1, primeira)
        cont = self.store.get(primeira["criadas"][0])
        self._fecha(cont, 1)
        segunda = C.plan_continuations(
            self.store,
            [self._outcome_partial(cont.task_id, 2)],
            input_versions=self.inputs,
            max_rounds=1,
            progress=self._progresso(2),
        )
        self.assertEqual(segunda["criadas"], [])
        self.assertEqual(segunda["recusadas"][0]["stop_reason"], S.STOP_BUDGET_EXHAUSTED)

    def test_teto_zero_continua_proibindo(self):
        """`0` é teto ZERO, não ausência de teto."""
        task = self._tarefa_base()
        self._fecha(task, 0)
        resultado = C.plan_continuations(
            self.store,
            [self._outcome_partial(task.task_id, 1)],
            input_versions=self.inputs,
            max_rounds=0,
            progress=self._progresso(1),
        )
        self.assertEqual(resultado["criadas"], [])
        self.assertTrue(resultado["recusadas"])

    def test_politica_persistida_continua_sendo_teto(self):
        """`set_max_continuation_rounds` grava teto; `None` grava 'sem teto'."""
        self.assertEqual(T.set_max_continuation_rounds(self.store, 2), 2)
        self.assertEqual(T.get_max_continuation_rounds(self.store), 2)
        self.assertIsNone(T.set_max_continuation_rounds(self.store, None))
        self.assertIsNone(T.get_max_continuation_rounds(self.store))


class LeituraSatisfeitaEProgressoTest(_StoreTest):
    """Ler N arquivos e devolver `reading_satisfied` É progresso (§7.3)."""

    def _estado(self):
        return S.new_state(
            repo_id="code/billing", objective_id="obj-1", input_revision="rev-1"
        )

    def test_reading_satisfied_sem_claim_novo_tem_progresso(self):
        delta = {
            "result_hash": "h1",
            "reading_satisfied": [
                {"need_id": "n1", "target": "a.go:1-40", "evidence": [
                    {"path": "a.go", "line_start": 1, "line_end": 40}
                ]},
                {"need_id": "n2", "target": "b.go:1-30", "evidence": [
                    {"path": "b.go", "line_start": 1, "line_end": 30}
                ]},
            ],
            "contract": {},
            "claims": {},
        }
        _, progresso = S.apply_result(self._estado(), delta)
        self.assertEqual(sorted(progresso.obligations_closed), ["n1", "n2"])
        self.assertTrue(
            progresso.has_progress,
            "leu 2 arquivos e fechou 2 obrigações: isso é progresso",
        )

    def test_declarar_leitura_sem_citacao_nao_fecha_obrigacao(self):
        """Controle: 'li' sem path/linhas nunca fecha leitura."""
        delta = {
            "result_hash": "h2",
            "reading_satisfied": [{"need_id": "n1", "target": "a.go:1-40"}],
        }
        _, progresso = S.apply_result(self._estado(), delta)
        self.assertEqual(progresso.obligations_closed, ())
        self.assertFalse(progresso.has_progress)

    def test_cadeia_continua_quando_a_rodada_so_satisfez_leitura(self):
        """`plan_continuations` não recusa por `no_progress` nesse caso."""
        task = self._tarefa_base()
        self._fecha(task, 0)
        _, progresso = S.apply_result(
            self._estado(),
            {
                "result_hash": "h3",
                "reading_satisfied": [
                    {"need_id": "n1", "target": "a.go:1-40", "evidence": [
                        {"path": "a.go", "line_start": 1, "line_end": 40}
                    ]}
                ],
            },
        )
        resultado = C.plan_continuations(
            self.store,
            [self._outcome_partial(task.task_id, 1)],
            input_versions=self.inputs,
            progress=progresso,
        )
        self.assertEqual(resultado["recusadas"], [], resultado)
        self.assertEqual(len(resultado["criadas"]), 1)


class DescobertaRealTest(unittest.TestCase):
    """Objetivo `discovery` REAL de `analysis.investigation` -> pacote de contexto.

    Roda o pipeline inteiro (`capture` -> `inventory.build` -> `extract_all` ->
    `capabilities.discover` -> `plan`) sobre um repositório Go em disco: é o
    contrato entre os dois módulos que este teste fixa, não uma fixture.
    """

    KEEP = None

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="descoberta-real-")
        for rel, body in GO_FILES.items():
            full = os.path.join(self.tmpdir, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(body)
        self.snapshot = snap_mod.capture(self.tmpdir)
        inventory = inv_mod.build(self.snapshot)
        keep = {
            inv_mod.FileClass.CODE,
            inv_mod.FileClass.CONFIG,
            inv_mod.FileClass.MANIFEST,
            inv_mod.FileClass.TEST,
            inv_mod.FileClass.MIGRATION,
        }
        sources = []
        for fc in inventory.files:
            if fc.file_class not in keep:
                continue
            full = os.path.join(self.tmpdir, *fc.path.split("/"))
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                sources.append(
                    SourceFile(
                        path=fc.path,
                        content=fh.read(),
                        language=(fc.language.value if fc.language else ""),
                    )
                )
        extraction = ext_registry.default_registry().extract_all(sources)
        cmap = cap_mod.discover(extraction, inventory, snapshot=self.snapshot)
        self.objectives = plan(
            cmap, extraction, inventory=inventory, snapshot=self.snapshot
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _discovery(self):
        alvos = [o for o in self.objectives if o.kind is ObjectiveKind.DISCOVERY]
        self.assertTrue(alvos, "repo Go deveria gerar objetivo de descoberta")
        return alvos[0]

    def test_pacote_traz_o_conteudo_integral_dos_arquivos_do_modulo(self):
        objective = self._discovery()
        alvos = {
            n.target.rsplit(":", 1)[0]
            for n in objective.reading_needs
            if n.trigger is ReadingTrigger.SOURCE_FILE
        }
        self.assertEqual(alvos, set(GO_FILES), "os 2 arquivos Go viram obrigação")

        package = CTX.build_package(
            objective.to_dict(),
            CTX.Budget(max_bytes=500_000, max_tokens=200_000),
            lambda p, s, e: resolve_evidence(self.snapshot, p, s, e),
        )
        por_path = {p.path: p.snippet for p in package.parts}
        self.assertEqual(set(por_path), set(GO_FILES))
        for rel, body in GO_FILES.items():
            self.assertEqual(por_path[rel], body, f"{rel} tem de vir INTEIRO")
        self.assertEqual(package.remaining_needs, [])

    def test_pacote_carrega_as_diretivas_de_analise_do_objetivo(self):
        objective = self._discovery()
        package = CTX.build_package(
            objective.to_dict(),
            CTX.Budget(max_bytes=500_000, max_tokens=200_000),
            lambda p, s, e: resolve_evidence(self.snapshot, p, s, e),
        )
        directives = package.refs[0]["analysis_directives"]
        self.assertEqual(directives, list(objective.analysis_directives))
        self.assertTrue(directives, "descoberta sem diretiva nenhuma seria regressão")

    def test_envelope_de_tarefa_leva_as_diretivas_ao_agente(self):
        objective = self._discovery()
        store = T.TaskStore.open(os.path.join(self.tmpdir, "runtime.db"))
        try:
            task = store.create_task(
                T.TaskKind.INVESTIGATION,
                objective.to_dict(),
                {"snapshot_id": self.snapshot.snapshot_id, "source_version_ids": []},
            )
            envelope = C.envelope_for_task(task, execution_id="exec-1")
            self.assertEqual(
                envelope.to_dict()["objective"]["analysis_directives"],
                list(objective.analysis_directives),
            )
        finally:
            store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
