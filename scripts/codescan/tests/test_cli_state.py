from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from codescan import sdd as sdd_mod
from codescan import state as st_mod
from codescan.cli import main


GREEN = "\U0001F7E2"
YELLOW = "\U0001F7E1"
RED = "\U0001F534"


def _write(path: str, text: str = "x\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _artifact_paths_for_item(wd: str, stage: str, item: str) -> list[str]:
    if stage == "modules":
        slug = item.replace("\\", "/").replace("/", "-")
        return [os.path.join(wd, "modules", f"{slug}.md")]
    if stage == "specs":
        root = os.path.join(wd, "sdd", "specs", item)
        return [os.path.join(root, name) for name in ("requirements.md", "design.md", "tasks.md")]
    return [os.path.join(wd, "sdd", f"{item}.md")]


def _write_agent_runs(wd: str, stage: str, items: list[str] | None = None) -> None:
    """Grava agent-runs/<stage>.json no schema wiki-ai.agent-runs.v2, com
    input_sha256/artefatos reais em disco (o audit/done validam esses hashes
    contra o arquivo de verdade, não aceitam mais o schema v1 legado)."""
    items = items or [stage]
    input_path = os.path.join(wd, "agent-packs", f"{stage}.json")
    _write(input_path, json.dumps({"stage": stage, "items": items}, ensure_ascii=False))
    items_payload = []
    for item in items:
        artifact_paths = _artifact_paths_for_item(wd, stage, item)
        items_payload.append(
            {
                "item": item,
                "artifacts": [
                    {
                        "path": path,
                        "sha256": st_mod.sha256_file(path),
                        "bytes": os.path.getsize(path),
                    }
                    for path in artifact_paths
                ],
            }
        )
    runs = [
        {
            "stage": stage,
            "input": input_path,
            "input_sha256": st_mod.sha256_file(input_path),
            "input_bytes": os.path.getsize(input_path),
            "agent": "test-agent",
            "items": items_payload,
            "items_count": len(items),
            "artifacts_count": len(items),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    _write(
        os.path.join(wd, "agent-runs", f"{stage}.json"),
        json.dumps({"schema": "wiki-ai.agent-runs.v2", "stage": stage, "runs": runs}, ensure_ascii=False),
    )


def _module_doc() -> str:
    return "\n".join(
        [
            "# Módulo de pagamentos",
            "",
            "## Responsabilidade",
            f"- O módulo coordena tentativas de pagamento, valida entrada nula e encerra o fluxo com retorno booleano. {GREEN} `src/payments.py:1`",
            f"- A política de retry é centralizada para evitar divergência entre chamadas internas. {GREEN} `src/payments.py:2`",
            "",
            "## Estruturas de dados",
            f"- A entidade operacional principal é o identificador de pagamento recebido pelo fluxo. {GREEN} `src/payments.py:3`",
            f"- Não há persistência inferível no recorte analisado; qualquer schema precisa de confirmação. {YELLOW}",
            "",
            "## Fluxos",
            f"- Fluxo principal: receber o identificador, validar ausência de valor, executar tentativa e retornar sucesso. {GREEN} `src/payments.py:4`",
            f"- Fluxo de erro: identificador ausente retorna falha sem acionar integração externa. {GREEN} `src/payments.py:5`",
            "",
            "## Dependências",
            f"- O módulo depende apenas da política local de limite de tentativas no trecho analisado. {GREEN} `src/payments.py:1`",
            "",
            "## Rastreabilidade",
            "| Arquivo | Função / Classe | Cobertura |",
            "|---|---|---|",
            f"| `src/payments.py` | `retry_payment` | responsabilidade, fluxo principal e erro {GREEN} `src/payments.py:2` |",
            "",
            "## Lacunas",
            f"- Confirmar se a integração externa fica em outro módulo ou foi omitida do recorte. {RED}",
        ]
    )


def _code_analysis_doc() -> str:
    return "\n".join(
        [
            "# Análise de código",
            "",
            "## Visão geral",
            f"- O serviço separa a lógica de pagamento em módulo próprio e usa retorno explícito para sucesso/falha. {GREEN} `src/payments.py:1`",
            f"- O comportamento confirmado cobre retry, validação de entrada e fronteira de falha local. {GREEN} `src/payments.py:2`",
            "",
            "## Módulos",
            f"- `src/payments` concentra regras de tentativa e validação do identificador. {GREEN} `src/payments.py:3`",
            f"- A ausência de persistência no módulo indica que o estado transacional está fora deste recorte. {YELLOW}",
            "",
            "## Fluxos",
            f"- Fluxo principal: receber requisição, validar identificador, aplicar política de retry e retornar resultado. {GREEN} `src/payments.py:4`",
            f"- Fluxo alternativo: identificador ausente retorna falha local sem prosseguir. {GREEN} `src/payments.py:5`",
            "",
            "## Riscos",
            f"- Não foi identificado tratamento diferenciado para timeout de integração externa. {YELLOW}",
            f"- Falta confirmação humana sobre SLA e idempotência de chamadas duplicadas. {RED}",
            "",
            "## Rastreabilidade",
            "| Arquivo | Responsabilidade | Evidência |",
            "|---|---|---|",
            f"| `src/payments.py` | retry e validação | {GREEN} `src/payments.py:1` |",
            f"| `src/payments.py` | retorno de falha | {GREEN} `src/payments.py:5` |",
            "",
            "## Decisões",
            f"- A decisão observável é manter a validação antes de qualquer efeito externo. {GREEN} `src/payments.py:4`",
            f"- A análise preserva lacunas como lacunas, sem promover suposição para confirmado. {YELLOW}",
            "",
            "## Detalhamento operacional",
            *[
                f"- Item {i}: descreve comportamento preservável, dependência, erro e implicação para reimplementação. {YELLOW}"
                for i in range(18)
            ],
        ]
    )


def _generic_code_analysis_doc() -> str:
    return "\n".join(
        [
            "# Análise de código",
            "",
            "## Visão geral",
            f"- Este módulo é responsável por funcionalidades do sistema e este arquivo descreve uma visão geral genérica. {GREEN} `src/payments.py:1`",
            f"- Este módulo também é responsável por organizar responsabilidades sem detalhar comportamento preservável. {GREEN} `src/payments.py:2`",
            "",
            "## Módulos",
            f"- `src/payments` aparece no recorte analisado e contém código relacionado ao domínio. {GREEN} `src/payments.py:3`",
            "",
            "## Fluxos",
            f"- Fluxo principal genérico: receber entrada, processar e retornar saída. {GREEN} `src/payments.py:4`",
            f"- Fluxo de erro genérico: retornar falha quando houver erro. {GREEN} `src/payments.py:5`",
            "",
            "## Riscos",
            f"- Lacuna: confirmar integrações externas e dependência operacional. {YELLOW}",
            "",
            "## Rastreabilidade",
            "| Código | Regra | Evidência |",
            "|---|---|---|",
            f"| `src/payments.py` | regra genérica | {GREEN} `src/payments.py:1` |",
            "",
            "## Detalhamento operacional",
            *[
                f"- Item {i}: fluxo, erro, dependência, entrada, saída, teste e reimplementação descritos de forma genérica. {YELLOW}"
                for i in range(18)
            ],
        ]
    )


def _requirements_doc() -> str:
    return "\n".join(
        [
            "# Checkout",
            "",
            "## Visão geral",
            f"- A unit cobre o recebimento de pedido, cálculo de pagamento e retorno do status operacional. {GREEN} `src/payments.py:1`",
            "",
            "## Responsabilidades",
            f"- Validar entrada obrigatória antes de iniciar pagamento. {GREEN} `src/payments.py:2`",
            f"- Aplicar política de tentativa controlada. {GREEN} `src/payments.py:3`",
            "",
            "## Regras de negócio",
            f"- Pedido sem identificador válido deve falhar sem integração externa. {GREEN} `src/payments.py:4`",
            f"- Limite de retry maior que zero é necessário para concluir pagamento. {GREEN} `src/payments.py:1`",
            "",
            "## Requisitos funcionais",
            "| ID | Requisito | Prioridade | Critério de aceite | Confiança |",
            "|---|---|---|---|---|",
            f"| RF-01 | Validar identificador | Must | Retorna falha quando ausente | {GREEN} `src/payments.py:4` |",
            f"| RF-02 | Executar tentativa | Must | Retorna sucesso após tentativa válida | {GREEN} `src/payments.py:5` |",
            "",
            "## Critérios de aceitação",
            f"- Dado identificador nulo, quando processar pagamento, então retornar falha sem efeitos externos. {GREEN} `src/payments.py:4`",
            f"- Dado identificador válido, quando processar pagamento, então retornar resultado operacional. {GREEN} `src/payments.py:5`",
            "",
            "## Rastreabilidade de código",
            "| Arquivo | Função / Classe | Cobertura |",
            "|---|---|---|",
            f"| `src/payments.py` | `retry_payment` | RF-01, RF-02 {GREEN} `src/payments.py:2` |",
        ]
    )


def _design_doc() -> str:
    return "\n".join(
        [
            "# Checkout - Design técnico",
            "",
            "## Interface",
            f"- Símbolo principal: `retry_payment(payment_id)` recebe identificador e retorna booleano. {GREEN} `src/payments.py:2`",
            "",
            "## Fluxo principal",
            f"1. Receber `payment_id` e validar presença antes da tentativa. {GREEN} `src/payments.py:3`",
            f"2. Aplicar regra local de tentativa e retornar sucesso operacional. {GREEN} `src/payments.py:4`",
            "",
            "## Fluxos alternativos",
            f"- Identificador ausente retorna falha imediatamente. {GREEN} `src/payments.py:3`",
            f"- Erros externos não aparecem no trecho e devem ser tratados como lacuna. {YELLOW}",
            "",
            "## Dependências",
            f"- A constante local de retry controla o comportamento do fluxo. {GREEN} `src/payments.py:1`",
            "",
            "## Decisões de design identificadas",
            f"- Validação ocorre antes de qualquer operação potencialmente externa. {GREEN} `src/payments.py:3`",
            "",
            "## Riscos e lacunas",
            f"- Falta confirmar observabilidade, idempotência e política de timeout. {RED}",
            "",
            "## Rastreabilidade",
            "| Elemento | Evidência |",
            "|---|---|",
            f"| `retry_payment` | {GREEN} `src/payments.py:2` |",
            "",
            "## Detalhamento operacional",
            *[
                f"- Item {i}: mantém contrato de entrada, retorno, falha e dependência observada no legado. {YELLOW}"
                for i in range(8)
            ],
        ]
    )


def _tasks_doc() -> str:
    return "\n".join(
        [
            "# Checkout - Tarefas de implementação",
            "",
            "## Pré-requisitos",
            f"- [ ] Definir contrato de entrada compatível com `payment_id`. {GREEN} `src/payments.py:2`",
            f"- [ ] Confirmar política de retry e limites operacionais. {GREEN} `src/payments.py:1`",
            "",
            "## Tarefas",
            f"- [ ] T-01 Implementar validação de identificador obrigatório. Origem no legado: `src/payments.py:3`. Confiança: {GREEN}",
            f"- [ ] T-02 Implementar retorno booleano para sucesso/falha do pagamento. Origem no legado: `src/payments.py:4`. Confiança: {GREEN}",
            "",
            "## Tarefas de teste",
            f"- [ ] TT-01 Cobrir happy path com identificador válido. {GREEN} `src/payments.py:4`",
            f"- [ ] TT-02 Cobrir erro com identificador ausente. {GREEN} `src/payments.py:3`",
            "",
            "## Ordem sugerida",
            f"- Primeiro validar entrada, depois implementar retry, depois testes de regressão. {GREEN} `src/payments.py:2`",
            "",
            "## Lacunas pendentes",
            f"- Confirmar integração externa, idempotência e SLA antes da reimplementação final. {RED}",
        ]
    )


def _confidence_doc() -> str:
    extra = [
        f"- Registrar decisor de timeout e compensação transacional antes da reimplementação. {RED}",
        f"- Validar se ausência de persistência no recorte é decisão arquitetural ou lacuna. {YELLOW}",
        f"- Cada afirmação confirmada preserva citação local ao legado quando disponível. {GREEN}",
        f"- Afirmações sem linha permanecem como lacuna ou inferência, sem promoção automática. {YELLOW}",
    ]
    return "\n".join(
        [
            "# Relatório de confiança",
            "",
            "## Contagem",
            f"- requirements.md: 8 confirmações, 0 inferências, 0 gaps. {GREEN}",
            f"- design.md: 7 confirmações, 1 inferência, 1 gap. {YELLOW}",
            f"- tasks.md: 6 confirmações, 0 inferências, 1 gap. {YELLOW}",
            "",
            "## Rebaixados",
            f"- Nenhuma afirmação verde foi rebaixada após revisão das citações. {GREEN}",
            f"- A política de timeout permanece inferida e não entrou em confirmado. {YELLOW}",
            "",
            "## Lacunas",
            f"- Confirmar SLA, integração externa e idempotência antes de reimplementação. {RED}",
            "",
            "## Rastreabilidade",
            f"- A contagem foi baseada nos artefatos canônicos de checkout e nos itens verificados. {GREEN}",
            "",
            "## Revisão operacional",
            *[
                f"- Revisão {i}: confirma coerência entre requisitos, design, tarefas, lacunas e rastreabilidade. {YELLOW}"
                for i in range(8)
            ],
        ] + extra
    )


def _traceability_matrix_doc() -> str:
    rows = [
        f"| `src/payments.py:{i}` | regra de validação {i} | requisito RF-{i:02d} | design de fluxo {i} | tarefa T-{i:02d} |"
        for i in range(1, 8)
    ]
    return "\n".join(
        [
            "# Matriz código regra requisito design tarefa",
            "",
            "## Matriz",
            "",
            "| Código | Regra | Requisito | Design | Tarefa |",
            "|---|---|---|---|---|",
            *rows,
            "",
            "- A matriz preserva fluxo, erro, dependencia, entrada e saida para reimplementacao.",
            "- Cada linha conecta codigo analisado, regra operacional, requisito, design e tarefa.",
            "- O objetivo é impedir perda de rastreabilidade entre artefatos técnicos.",
            "- A revisão confirma coerência mínima entre teste, criterio e implementação planejada.",
        ]
    )


def _incomplete_gaps_doc() -> str:
    return "\n".join(
        [
            "# Lacunas",
            "",
            "## Lacunas",
            "",
            "- Confirmar SLA operacional antes da reimplementacao.",
            "- Confirmar comportamento de erro da integracao externa.",
            "- Confirmar dependencia de infraestrutura para entrada e saida do fluxo.",
            "- Confirmar criterio de teste para falhas transitórias.",
            "- Confirmar estado persistido em caso de retry parcial.",
            "- Confirmar regra de compensação transacional.",
        ]
    )


def _bad_quadrant_chart_doc() -> str:
    return "\n".join(
        [
            "# Acoplamento",
            "",
            "## Plano Abstração Instabilidade",
            "",
            "```mermaid",
            "quadrantChart",
            "  title Zonas de design",
            "  x-axis Estavel --> Instavel",
            "  y-axis Concreto --> Abstrato",
            "  quadrant-1 Inutilidade",
            "  quadrant-2 Abstrato-estavel",
            "  quadrant-3 Dor",
            "  quadrant-4 Concreto-instavel",
            "  P015: [0.31, 1.00]  P016: [1.00, 0.00]",
            "```",
            "",
            "- O diagrama descreve fluxo, erro, dependencia, entrada e saida de forma rastreavel.",
            "- A falha esperada é sintática: múltiplos pontos Mermaid na mesma linha.",
            "- O audit precisa reprovar esse padrão antes de permitir done.",
            "- Sem esse bloqueio, artefato quebrado pode receber score alto indevido.",
        ]
    )


def _synth_confirmed_doc() -> str:
    return "\n".join(
        [
            "# Confirmados",
            "",
            "## Visão geral",
            f"- Fluxo principal confirmado: entrada validada, tentativa aplicada e saída retornada em src/quote/QuoteService.java:10. {GREEN}",
            f"- As regras de negócio confirmadas garantem que entrada nula retorna falha sem integração externa em src/quote/QuoteService.java:11. {GREEN}",
            f"- Dependências confirmadas incluem o repositório que persiste a decisão em src/quote/QuoteRepository.java:4. {GREEN}",
            "",
            "## Rastreabilidade",
            f"- Critério de aceite confirmado cobre entrada, fluxo e saída rastreados em src/quote/QuoteService.java:12. {GREEN}",
            f"- O fluxo de erro trata a integração sem persistir estado parcial, com saída consistente e sem teste adicional pendente. {GREEN}",
            "",
            "## Lacunas",
            f"- O estado de retry parcial permanece como lacuna, pronto para reimplementação futura. {YELLOW}",
        ]
    )


def _synth_inferred_doc() -> str:
    return "\n".join(
        [
            "# Inferidos",
            "",
            "## Inferências",
            f"- Inferência: o serviço provavelmente aplica retry limitado mesmo sem citação direta no recorte revisado. {YELLOW}",
            f"- Inferência: a integração externa provavelmente ocorre fora do módulo analisado, sem evidência direta no recorte. {YELLOW}",
            f"- Inferência: o fluxo de erro provavelmente não persiste estado parcial, mas isso não foi confirmado no recorte. {YELLOW}",
            f"- Inferência: o tempo de resposta observado sugere ausência de cache local, sem confirmação direta no recorte. {YELLOW}",
            f"- Inferência: a validação de entrada provavelmente é reaproveitada por outros fluxos do mesmo serviço. {YELLOW}",
            "",
            "## Lacunas",
            f"- Pergunta objetiva: qual componente trata o timeout da integração externa? {RED}",
        ]
    )


class CliStateContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        self.wd = st_mod.workdir(self.store, self.repo)
        st_mod.init(self.wd, self.repo, topic=None)

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        # --verbose: no-op explícito. O corpo completo já é o padrão do
        # `wk code` (--quiet é que é opt-in); mantemos a flag aqui só para
        # deixar claro que este arquivo depende do contrato de corpo
        # completo (JSON inteiro por comando) que os testes abaixo inspecionam
        # campo a campo, e não do resumo condensado do --quiet.
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def test_agent_pack_gera_contexto_sem_copiar_repo_para_workdir_src(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "\n".join([
            "package app.payments;",
            "class PaymentService {",
            "  boolean quote(String id) { return id != null; }",
            "}",
        ]))
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "package app.payments;\nclass PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("agent-pack", "modules", "--batch", "1", "--batches", "1"))

        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertGreaterEqual(report["modules"], 1)
        self.assertGreaterEqual(report["files"], 1)
        self.assertFalse(os.path.isdir(os.path.join(self.wd, "src")))
        with open(report["artifact"], encoding="utf-8") as f:
            pack = json.load(f)
        self.assertEqual(pack["schema"], "wiki-ai.agent-pack.v2")
        self.assertIn("nao copie o repo", " ".join(pack["rules"]).lower())
        self.assertIn("evidence", pack["modules"][0])

    def test_done_modules_rejeita_artifact_ausente_sem_alterar_estado(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])

        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 2)
        self.assertIn("artifact de módulo não encontrado", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")
        self.assertEqual(stage["pending"], ["src/payments"])
        self.assertEqual(stage["done"], [])

    def test_done_modules_rejeita_artifact_sem_evidencia(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), "- Sem citação.\n")

        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 2)
        self.assertIn("rastreabilidade insuficiente", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")
        self.assertEqual(stage["done"], [])

    def test_done_modules_rejeita_boilerplate_em_ingles(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(
            os.path.join(self.wd, "modules", "src-payments.md"),
            "\n".join(
                [
                    "# Payments",
                    "",
                    "## Overview",
                    f"- This module handles payments. {GREEN} `src/payments.py:1`",
                    "",
                    "## Responsibility",
                    f"- Responsibility: process checkout. {GREEN} `src/payments.py:2`",
                    "",
                    "## Dependencies",
                    f"- Dependencies: repository and gateway. {GREEN} `src/payments.py:3`",
                ]
            ),
        )

        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 2)
        self.assertIn("PT-BR", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")
        self.assertEqual(stage["done"], [])

    def test_done_modules_aceita_artifact_com_profundidade_e_evidencia(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())

        code, out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "in_progress")
        self.assertTrue(stage["items_complete"])
        self.assertEqual(stage["done"], ["src/payments"])
        self.assertEqual(stage["pending"], [])

        code, _out, err = _run(self._argv("done", "modules"))
        self.assertEqual(code, 2)
        self.assertIn("code-analysis.md", err)

        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), "# Análise curta\n")
        code, _out, err = _run(self._argv("done", "modules"))
        self.assertEqual(code, 2)
        self.assertIn("profundidade insuficiente", err)

        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())
        _write_agent_runs(self.wd, "modules", ["src/payments"])
        code, out, err = _run(self._argv("audit", "--stage", "modules"))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["status"], "pass")
        self.assertGreaterEqual(report["score"], 90)

        code, out, err = _run(self._argv("audit", "--stage", "modules", "--strict"))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["status"], "pass")
        self.assertGreaterEqual(report["score"], 90)

        code, out, err = _run(self._argv("done", "modules"))
        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "done")
        self.assertTrue(stage["finalized"])

    def test_done_modules_com_artifact_exige_registro_no_merge(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())
        _write_agent_runs(self.wd, "modules", ["src/payments"])

        # sdd/code-analysis.md já existe com boa qualidade (passa nos outros
        # gates), mas nunca foi registrado como artifact merged em nenhum
        # item do manifesto de modules — é o candidato certo para provar que
        # `done --artifact` recusa caminho fora do merge sem se confundir com
        # os gates de qualidade/rastreabilidade já cobertos por outro teste.
        nao_merged = os.path.join(self.wd, "sdd", "code-analysis.md")
        code, _out, err = _run(self._argv("done", "modules", "--artifact", nao_merged))

        self.assertEqual(code, 2)
        self.assertIn("merge-agent-output modules", err)
        payload = json.loads(err)
        self.assertIn(nao_merged, payload["error"])
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")

        merged_artifact = os.path.join(self.wd, "modules", "src-payments.md")
        code, out, err = _run(self._argv("done", "modules", "--artifact", merged_artifact))

        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "done")
        self.assertTrue(stage["finalized"])

    def test_done_modules_aceita_palavras_ptbr_todo_todos_metodos_e_camelcase_sem_placeholder(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        doc = (
            _module_doc()
            + f"\n- todo comportamento confirmado preserva todos os métodos, toResponse e toCommand já citados para reimplementação. {YELLOW}\n"
        )
        _write(os.path.join(self.wd, "modules", "src-payments.md"), doc)

        code, out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["done"], ["src/payments"])

    def test_done_modules_aceita_generics_java_sem_placeholder(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        doc = (
            _module_doc()
            + "\n"
            + f"- Tipos Java legítimos aparecem como Optional<RuleViolation>, List<Violation>, Map<String,Object> e ResponseEntity<QuoteResponse>. {YELLOW}\n"
        )
        _write(os.path.join(self.wd, "modules", "src-payments.md"), doc)

        code, out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["done"], ["src/payments"])

    def test_done_modules_rejeita_todo_tecnico_isolado(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        doc = _module_doc().replace("Confirmar se a integração externa", "TODO Confirmar se a integração externa")
        _write(os.path.join(self.wd, "modules", "src-payments.md"), doc)

        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))

        self.assertEqual(code, 2)
        self.assertIn("placeholder/boilerplate", err)

    def test_done_modules_rejeita_placeholders_reais(self):
        placeholders = (
            "TODO",
            "TBD",
            "FIXME",
            "XXX",
            "<arquivo:linha>",
            "<descrição>",
            "<preencher>",
        )
        for placeholder in placeholders:
            with self.subTest(placeholder=placeholder), tempfile.TemporaryDirectory() as tmp:
                repo = os.path.join(tmp, "repo")
                store = os.path.join(tmp, "store")
                os.makedirs(repo, exist_ok=True)
                wd = st_mod.workdir(store, repo)
                st_mod.init(wd, repo, topic=None)
                st_mod.set_pending(wd, "modules", ["src/payments"])
                doc = _module_doc().replace(
                    "Confirmar se a integração externa",
                    f"{placeholder} Confirmar se a integração externa",
                )
                _write(os.path.join(wd, "modules", "src-payments.md"), doc)

                code, _out, err = _run(["--store", store, "--repo", repo, "--verbose", "done", "modules", "--item", "src/payments"])

                self.assertEqual(code, 2)
                self.assertIn("placeholder/boilerplate", err)

    def test_done_modules_rejeita_stage_generico_com_score_abaixo_de_90(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)

        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _generic_code_analysis_doc())
        _write_agent_runs(self.wd, "modules", ["src/payments"])
        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        self.assertIn("artefato genérico", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")

    def test_done_specs_exige_triplo_artifact_e_qualidade_minima(self):
        st_mod.set_pending(self.wd, "specs", ["checkout"])
        root = os.path.join(self.wd, "sdd", "specs", "checkout")
        _write(os.path.join(root, "requirements.md"))
        _write(os.path.join(root, "design.md"))

        code, _out, err = _run(self._argv("done", "specs", "--item", "checkout"))

        self.assertEqual(code, 2)
        self.assertIn("tasks.md", err)
        stage = st_mod.load(self.wd)["stages"]["specs"]
        self.assertEqual(stage["status"], "in_progress")
        self.assertEqual(stage["done"], [])

        _write(os.path.join(root, "tasks.md"), "- tarefa curta\n")
        code, _out, err = _run(self._argv("done", "specs", "--item", "checkout"))
        self.assertEqual(code, 2)
        self.assertIn("requirements.md inválido", err)

        _write(os.path.join(root, "requirements.md"), _requirements_doc())
        _write(os.path.join(root, "design.md"), _design_doc())
        _write(os.path.join(root, "tasks.md"), _tasks_doc())
        code, out, err = _run(self._argv("done", "specs", "--item", "checkout"))

        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "in_progress")
        self.assertTrue(stage["items_complete"])
        self.assertEqual(stage["done"], ["checkout"])

        code, _out, err = _run(self._argv("done", "specs"))
        self.assertEqual(code, 2)
        self.assertIn("confidence-report.md", err)

        _write(os.path.join(self.wd, "sdd", "confidence-report.md"), _confidence_doc())
        _write_agent_runs(self.wd, "specs", ["checkout"])
        code, out, err = _run(self._argv("done", "specs"))
        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "done")

    def test_done_stage_agent_output_sem_agent_runs_falha(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())

        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        self.assertIn("agent-runs obrigatório ausente", err)

    def test_audit_strict_agent_output_sem_agent_runs_falha(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())

        code, out, err = _run(self._argv("audit", "--stage", "modules", "--strict"))

        self.assertEqual(code, 1, err)
        report = json.loads(out)
        blockers = "\n".join(blocker for stage in report["stages"] for blocker in stage["blockers"])
        self.assertIn("agent-runs obrigatório ausente", blockers)

    def test_done_modules_falha_persiste_last_error_e_next_aponta_acao_especifica(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        self.assertIn("code-analysis.md", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")
        last_error = stage.get("last_error")
        if isinstance(last_error, dict):
            self.assertEqual(last_error.get("command"), "done")
            self.assertEqual(last_error.get("stage"), "modules")
            self.assertEqual(last_error.get("exit_code"), 2)
            last_error_text = last_error.get("error", "")
        else:
            self.assertIsInstance(last_error, str)
            last_error_text = last_error
        self.assertIn("code-analysis.md", last_error_text)

        code, out, err = _run(self._argv("next"))
        self.assertEqual(code, 0, err)
        nxt = json.loads(out)
        self.assertEqual(nxt["proximo"], "modules")
        next_last_error = nxt["last_error"]
        next_error_text = next_last_error.get("error", "") if isinstance(next_last_error, dict) else next_last_error
        self.assertIn("code-analysis.md", next_error_text)
        self.assertIn("acao", nxt)
        self.assertIn("code-analysis.md", nxt["acao"])

    def test_done_modules_sucesso_limpa_last_error(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("done", "modules"))
        self.assertEqual(code, 2)

        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())
        _write_agent_runs(self.wd, "modules", ["src/payments"])
        code, out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "done")
        self.assertNotIn("last_error", stage)
        self.assertNotIn("last_artifact", stage)
        self.assertNotIn("last_blockers", stage)

    def test_done_modules_agrega_multiplos_blockers_no_json_de_erro(self):
        st = st_mod.load(self.wd)
        st["sdd"] = {"doc_level": "completo"}
        st_mod.save(self.wd, st)
        st_mod.set_pending(self.wd, "modules", ["src/payments"])

        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertIn("blockers", payload)
        errors = "\n".join(blocker["error"] for blocker in payload["blockers"])
        actions = "\n".join(blocker["action"] for blocker in payload["blockers"])
        self.assertIn("itens pending", errors)
        self.assertIn("data-dictionary.md", errors)
        self.assertIn("flowchart", errors)
        self.assertIn("gerar sdd/data-dictionary.md", actions)
        self.assertIn("gerar sdd/flowcharts/*.md", actions)

    def test_audit_strict_parcial_nao_exige_agent_runs_de_rules_antes_do_stage_iniciar(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("audit", "--strict"))

        self.assertEqual(code, 1, err)
        report = json.loads(out)
        blockers = "\n".join(
            blocker
            for stage in report["stages"]
            for blocker in stage["blockers"]
        )
        self.assertNotIn("agent-runs/rules.json", blockers)

    def test_audit_strict_sem_stage_modules_done_nao_audita_rules_pending(self):
        st_mod.mark(self.wd, "surface", "done", artifact=os.path.join(self.wd, "surface.json"))
        st = st_mod.load(self.wd)
        st["sdd"] = {"doc_level": "essencial", "granularity": "module"}
        st_mod.save(self.wd, st)
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())
        _write_agent_runs(self.wd, "modules", ["src/payments"])
        code, _out, err = _run(self._argv("done", "modules"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("next"))
        self.assertEqual(code, 0, err)
        nxt = json.loads(out)
        self.assertEqual(nxt["proximo"], "rules")
        self.assertEqual(nxt["status"], "pending")

        code, out, err = _run(self._argv("audit", "--strict"))

        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual([stage["stage"] for stage in report["stages"]], ["modules"])
        blockers = "\n".join(
            blocker
            for stage in report["stages"]
            for blocker in stage["blockers"]
        )
        self.assertNotIn("stage rules", blockers)

    def test_done_rules_e_architecture_exigem_arvore_sdd(self):
        code, _out, err = _run(self._argv("done", "rules", "--artifact", os.path.join(self.wd, "inferred.md")))
        self.assertEqual(code, 2)
        self.assertIn("domain.md", err)

        _write(os.path.join(self.wd, "sdd", "domain.md"), "# Domínio\n")
        code, _out, err = _run(self._argv("done", "rules"))
        self.assertEqual(code, 2)
        self.assertIn("profundidade insuficiente", err)

        code, _out, err = _run(self._argv("done", "architecture", "--artifact", os.path.join(self.wd, "inferred.md")))
        self.assertEqual(code, 2)
        self.assertIn("architecture.md", err)

    def test_problem_statuses_e_next_sao_explicitos(self):
        st_mod.mark(self.wd, "surface", "done", artifact=os.path.join(self.wd, "surface.json"))
        st = st_mod.load(self.wd)
        st["sdd"] = {"doc_level": "essencial", "granularity": "module"}
        st_mod.save(self.wd, st)
        st_mod.set_pending(self.wd, "modules", ["src/payments"])

        code, out, err = _run(self._argv("blocked", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        stage = json.loads(out)
        self.assertEqual(stage["status"], "blocked")
        self.assertEqual(stage["blocked"], ["src/payments"])
        self.assertEqual(stage["pending"], [])

        code, out, err = _run(self._argv("next"))
        self.assertEqual(code, 0, err)
        nxt = json.loads(out)
        self.assertEqual(nxt["proximo"], "modules")
        self.assertEqual(nxt["status"], "blocked")
        self.assertEqual(nxt["blocked"], ["src/payments"])
        self.assertEqual(nxt["item"], "src/payments")

    def test_sdd_brief_expoe_contrato_compacto_por_stage(self):
        code, out, err = _run(self._argv("sdd-brief", "modules"))

        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["stage"], "modules")
        self.assertEqual(report["doc_level"], "essencial")
        self.assertIn("sdd/code-analysis.md", report["artifacts"])
        self.assertTrue(any("score >=90" in item for item in report["instructions"]))

    def test_sdd_scaffold_cria_artefatos_do_stage(self):
        code, out, err = _run(self._argv("sdd-scaffold", "modules"))

        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["stage"], "modules")
        self.assertEqual(report["doc_level"], "essencial")
        self.assertTrue(any(path.endswith(os.path.join("sdd", "code-analysis.md")) for path in report["created"]))
        self.assertTrue(os.path.isfile(os.path.join(self.wd, "sdd", "code-analysis.md")))

    def test_plan_falha_sem_config_sdd_e_passa_apos_config(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, _out, err = _run(self._argv("plan", "--batches", "1"))

        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertEqual(error["error"], "config SDD obrigatória antes de plan/run-stage modules")
        self.assertEqual(error["missing"], ["sdd.doc_level", "sdd.granularity"])
        self.assertIn("config --doc-level", error["acao"])
        self.assertIn("--granularity", error["acao"])

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["subagentes"], 1)

    def test_next_aponta_config_apos_surface_e_export_sem_config(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("next"))
        self.assertEqual(code, 0, err)
        nxt = json.loads(out)
        self.assertEqual(nxt["proximo"], "config")
        self.assertIn("config --doc-level", nxt["acao"])

        code, _out, err = _run(self._argv("export", "--topic", "codebases/test"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("next"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["proximo"], "config")

        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("next"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["proximo"], "modules")

    def test_audit_modules_reprova_workdir_com_src_copiado(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())
        _write(os.path.join(self.wd, "src", "main", "java", "app", "Payment.java"), "class Payment {}\n")
        _write(os.path.join(self.wd, "src", "main", "java", "app", "pom.xml"), "<project />\n")

        code, out, err = _run(self._argv("audit", "--stage", "modules"))

        self.assertNotEqual(code, 0)
        report = json.loads(out)
        blockers = "\n".join(
            blocker
            for stage in report["stages"]
            for blocker in stage["blockers"]
        )
        self.assertNotEqual(report["status"], "pass")
        self.assertIn("src", blockers)
        self.assertRegex(blockers, r"copiad|codebase|workdir|store")

    def test_done_modules_usa_strict_por_padrao_e_reprova_scripts_py_e_txt_no_workdir(self):
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())
        _write(os.path.join(self.wd, "parse_modules.py"), "print('manual')\n")
        _write(os.path.join(self.wd, "todo_matches.txt"), "TODO manual\n")

        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        self.assertIn("script python proibido", err)
        self.assertIn("txt operacional solto", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")

    def test_done_modules_doc_level_completo_agrega_data_dictionary_e_flowcharts_sem_marcar_done(self):
        st = st_mod.load(self.wd)
        st["sdd"] = {"doc_level": "completo"}
        st_mod.save(self.wd, st)
        st_mod.set_pending(self.wd, "modules", ["src/payments"])
        _write(os.path.join(self.wd, "modules", "src-payments.md"), _module_doc())
        code, _out, err = _run(self._argv("done", "modules", "--item", "src/payments"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), _code_analysis_doc())

        code, _out, err = _run(self._argv("done", "modules"))

        self.assertEqual(code, 2)
        self.assertIn("data-dictionary.md", err)
        self.assertIn("flowcharts", err)
        stage = st_mod.load(self.wd)["stages"]["modules"]
        self.assertEqual(stage["status"], "in_progress")
        self.assertNotEqual(stage.get("status"), "done")

    def test_audit_reprova_quadrantchart_com_multiplos_pontos_na_mesma_linha(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = os.path.join(tmp, "coupling.md")
            _write(artifact, _bad_quadrant_chart_doc())

            report = sdd_mod._audit_file(
                artifact,
                sdd_mod.ArtifactRule(
                    "sdd/coupling.md",
                    min_bytes=200,
                    min_citations=0,
                    sections=("Plano Abstração Instabilidade",),
                ),
            )

        self.assertEqual(report["status"], "failed")
        self.assertLess(report["score"], 90)
        self.assertTrue(any("Mermaid" in blocker for blocker in report["blockers"]))
        self.assertTrue(any("quadrantChart" in blocker or "pontos" in blocker for blocker in report["blockers"]))

    def test_audit_specs_reprova_gaps_incompleto_em_doc_level_completo_e_detalhado(self):
        for level in ("completo", "detalhado"):
            with self.subTest(level=level):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = os.path.join(tmp, "repo")
                    store = os.path.join(tmp, "store")
                    os.makedirs(repo, exist_ok=True)
                    wd = st_mod.workdir(store, repo)
                    st_mod.init(wd, repo, topic=None)
                    st = st_mod.load(wd)
                    st["sdd"] = {"doc_level": level}
                    st["stages"]["specs"] = {
                        "status": "in_progress",
                        "pending": [],
                        "done": ["checkout"],
                        "blocked": [],
                        "failed": [],
                    }
                    st_mod.save(wd, st)
                    root = os.path.join(wd, "sdd", "specs", "checkout")
                    _write(os.path.join(root, "requirements.md"), _requirements_doc())
                    _write(os.path.join(root, "design.md"), _design_doc())
                    _write(os.path.join(root, "tasks.md"), _tasks_doc())
                    _write(os.path.join(wd, "sdd", "confidence-report.md"), _confidence_doc())
                    _write(os.path.join(wd, "sdd", "traceability", "code-spec-matrix.md"), _traceability_matrix_doc())
                    _write(os.path.join(wd, "sdd", "gaps.md"), _incomplete_gaps_doc())

                    report = sdd_mod.audit(wd, "specs", st_mod.load(wd))

                blockers = "\n".join(
                    blocker
                    for stage in report["stages"]
                    for blocker in stage["blockers"]
                )
                self.assertNotEqual(report["status"], "pass")
                self.assertIn("gaps.md", blockers)
                self.assertRegex(blockers, r"gaps\.md|conte.do insuficiente|incomplet")

class RunStageContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.store = os.path.join(self.tmp.name, "store")
        os.makedirs(self.repo, exist_ok=True)
        self.wd = st_mod.workdir(self.store, self.repo)
        st_mod.init(self.wd, self.repo, topic="codebases/test")

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *args: str) -> list[str]:
        # --verbose: no-op explícito. O corpo completo já é o padrão do
        # `wk code` (--quiet é que é opt-in); mantemos a flag aqui só para
        # deixar claro que este arquivo depende do contrato de corpo
        # completo (JSON inteiro por comando) que os testes abaixo inspecionam
        # campo a campo, e não do resumo condensado do --quiet.
        return ["--store", self.store, "--repo", self.repo, "--verbose", *args]

    def test_run_stage_modules_prepara_manifesto_pack_e_comando_merge_sem_gerar_sdd(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "\n".join([
            "package app.payments;",
            "class PaymentService {",
            "  boolean quote(String id) { return id != null; }",
            "}",
        ]))
        _write(os.path.join(self.repo, "src", "payments", "PaymentPolicy.java"), "package app.payments;\nclass PaymentPolicy {}\n")

        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)

        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "1"))

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["schema"], "wiki-ai.run-stage-plan.v1")
        self.assertEqual(result["stage"], "modules")
        self.assertEqual(len(result["batches"]), 1)
        batch = result["batches"][0]
        self.assertEqual(batch["role"], "Module Archaeologist")
        self.assertTrue(os.path.isfile(batch["agent_pack"]))
        self.assertIn("merge-agent-output modules --input", batch["merge_command"])
        self.assertTrue(os.path.isfile(os.path.join(self.wd, "agent-runs", "modules-plan.json")))
        self.assertFalse(os.path.isdir(os.path.join(self.wd, "modules")))
        self.assertFalse(os.path.isdir(os.path.join(self.wd, "sdd", "specs")))

    def test_run_stage_modules_falha_sem_config_mesmo_com_pendencia_existente(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        st_mod.set_pending(self.wd, "modules", ["src/payments"])

        code, _out, err = _run(self._argv("run-stage", "modules", "--batches", "1"))

        self.assertEqual(code, 2)
        payload = next((line for line in reversed(err.splitlines()) if line.lstrip().startswith("{")), "")
        if payload:
            error = json.loads(payload)
            self.assertEqual(error["missing"], ["sdd.doc_level", "sdd.granularity"])
            self.assertIn("config --doc-level", error["acao"])
        self.assertFalse(os.path.isfile(os.path.join(self.wd, "agent-runs", "modules-plan.json")))

    def test_run_stage_suporta_rules_architecture_specs_sem_spawnar_agente(self):
        for stage, role in (
            ("rules", "Business Rules Detective"),
            ("architecture", "Architecture Reviewer"),
            ("specs", "Specification Writer"),
        ):
            with self.subTest(stage=stage):
                st_mod.set_pending(self.wd, stage, [f"{stage}/item"])

                code, out, err = _run(self._argv("run-stage", stage))

                self.assertEqual(code, 0, err)
                result = json.loads(out)
                self.assertEqual(result["stage"], stage)
                self.assertEqual(result["batches"][0]["role"], role)
                self.assertTrue(os.path.isfile(result["batches"][0]["agent_pack"]))
                self.assertIn(f"merge-agent-output {stage} --input", result["batches"][0]["merge_command"])
                self.assertTrue(os.path.isfile(os.path.join(self.wd, "agent-runs", f"{stage}-plan.json")))

    def test_run_stage_probe_falha_quando_agent_outputs_e_arquivo_invalido(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "detalhado", "--granularity", "hybrid"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)

        # simula falta de acesso: um ARQUIVO no lugar de agent-outputs/ faz
        # os.makedirs falhar de forma determinística, em qualquer SO.
        bogus = os.path.join(self.wd, "agent-outputs")
        _write(bogus, "arquivo no lugar do diretorio\n")

        code, _out, err = _run(self._argv("run-stage", "modules", "--batches", "1"))

        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertIn(bogus.replace("\\", "/"), error["error"].replace("\\", "/"))
        self.assertIn("wk init --store", error["acao"])
        self.assertIn("--repo", error["acao"])
        self.assertFalse(os.path.isfile(os.path.join(self.wd, "agent-runs", "modules-plan.json")))

    def test_run_stage_probe_passa_e_consegue_ler_sdd_existente(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)
        _write(os.path.join(self.wd, "sdd", "code-analysis.md"), "# Análise\n")

        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "1"))

        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isdir(os.path.join(self.wd, "agent-outputs")))

    def test_run_stage_manifesto_contem_contrato_de_recibo(self):
        st_mod.set_pending(self.wd, "rules", ["rules/item"])

        code, _out, err = _run(self._argv("run-stage", "rules"))

        self.assertEqual(code, 0, err)
        manifest_path = os.path.join(self.wd, "agent-runs", "rules-plan.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        contract = manifest["receipt_contract"]
        self.assertTrue(contract["subagent_writes_output"])
        self.assertEqual(contract["receipt_format"], ["ARQUIVO:", "BLOCOS:", "BYTES:"])
        self.assertEqual(contract["max_status_lines"], 3)
        self.assertEqual(contract["max_error_lines"], 8)
        for flag in ("no_command_echo", "no_tool_output_echo", "no_diff_echo", "no_written_artifact_echo"):
            self.assertIn(flag, contract["flags"])
        rules_text = " ".join(manifest["rules"]).lower()
        self.assertNotIn("subagente retorna somente blocos parseaveis", rules_text)
        self.assertIn("recibo", rules_text)

    def test_merge_agent_output_aceita_flag_agent_e_registra_no_manifesto(self):
        _write(os.path.join(self.repo, "src", "payments", "PaymentService.java"), "class PaymentService {}\n")
        code, _out, err = _run(self._argv("surface", "--module-min-files", "1"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("config", "--doc-level", "essencial", "--granularity", "module"))
        self.assertEqual(code, 0, err)
        code, _out, err = _run(self._argv("plan", "--batches", "1"))
        self.assertEqual(code, 0, err)
        code, out, err = _run(self._argv("run-stage", "modules", "--batches", "1"))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        items = result["batches"][0]["items"]
        response = os.path.join(self.wd, "agent-outputs", "modules-batch-01.txt")
        _write(
            response,
            "\n".join(
                "\n".join(["=== MODULE: {} ===".format(item), "Conteúdo mínimo do módulo para o merge.", "=== END ==="])
                for item in items
            ),
        )

        code, _out, err = _run(self._argv("merge-agent-output", "modules", "--input", response, "--agent", "sub-3"))
        self.assertEqual(code, 0, err)

        manifest_path = os.path.join(self.wd, "agent-runs", "modules.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["runs"][0]["agent"], "sub-3")

    def test_run_stage_synth_emite_manifesto_com_recibo_e_agent_pack(self):
        code, out, err = _run(self._argv("run-stage", "synth"))

        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["stage"], "synth")
        batch = result["batches"][0]
        self.assertEqual(batch["role"], "Synthesis Writer")
        self.assertTrue(os.path.isfile(batch["agent_pack"]))
        self.assertTrue(batch["output"].replace("\\", "/").endswith("agent-outputs/synth-batch-01.txt"))
        self.assertIn("merge-agent-output synth --input", batch["merge_command"])

        manifest_path = os.path.join(self.wd, "agent-runs", "synth-plan.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        contract = manifest["receipt_contract"]
        self.assertEqual(contract["receipt_format"], ["ARQUIVO:", "BLOCOS:", "BYTES:"])
        self.assertEqual(contract["max_status_lines"], 3)
        self.assertEqual(contract["max_error_lines"], 8)

    def test_merge_agent_output_synth_rejeita_bloco_com_nome_invalido(self):
        response = os.path.join(self.wd, "agent-outputs", "synth-bad.txt")
        _write(
            response,
            "\n".join(
                [
                    "=== SYNTH: nome-invalido ===",
                    "Conteúdo qualquer que nunca deveria ser aceito.",
                    "=== END ===",
                    "",
                ]
            ),
        )

        code, _out, err = _run(self._argv("merge-agent-output", "synth", "--input", response, "--agent", "synth-b01"))

        self.assertEqual(code, 2)
        self.assertIn("nome de artefato não aceito", err)
        self.assertFalse(os.path.isfile(os.path.join(self.wd, "sdd", "confirmed.md")))

    def test_synth_fecha_ponta_a_ponta_via_run_stage_merge_e_done(self):
        for stage in ("modules", "rules", "architecture", "specs"):
            st_mod.mark(self.wd, stage, "done")

        code, out, err = _run(self._argv("run-stage", "synth"))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        batch = result["batches"][0]
        response = batch["output"]

        _write(
            response,
            "\n".join(
                [
                    "=== SYNTH: confirmed ===",
                    _synth_confirmed_doc(),
                    "=== END ===",
                    "=== SYNTH: inferred ===",
                    _synth_inferred_doc(),
                    "=== END ===",
                    "",
                ]
            ),
        )

        code, out, err = _run(self._argv("merge-agent-output", "synth", "--input", response, "--agent", "synth-b01"))
        self.assertEqual(code, 0, err)
        merge_result = json.loads(out)
        self.assertEqual(merge_result["items"], 2)

        manifest_path = os.path.join(self.wd, "agent-runs", "synth.json")
        with open(manifest_path, encoding="utf-8") as f:
            run_manifest = json.load(f)
        self.assertEqual(run_manifest["schema"], "wiki-ai.agent-runs.v2")
        artifact_count = 0
        for item in run_manifest["runs"][0]["items"]:
            for artifact in item["artifacts"]:
                self.assertEqual(len(artifact["sha256"]), 64)
                self.assertGreater(artifact["bytes"], 0)
                artifact_count += 1
        self.assertEqual(artifact_count, 2)

        self.assertTrue(os.path.isfile(os.path.join(self.wd, "sdd", "confirmed.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.wd, "sdd", "inferred.md")))

        code, out, err = _run(self._argv("done", "synth"))
        self.assertEqual(code, 0, err)
        stage_state = json.loads(out)
        self.assertEqual(stage_state["status"], "done")
        self.assertTrue(stage_state["finalized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
