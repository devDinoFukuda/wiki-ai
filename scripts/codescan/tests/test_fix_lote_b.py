"""Lote B — FIX1 (diagnosticabilidade + falso positivo do gate Mermaid) e
FIX2 (artefatos "de diagrama" sem nenhum bloco ```mermaid```).

Reproduz o defeito relatado em produção: um label mermaid corretamente
quoted (`A["Quote Service (v2)"]`) era rejeitado pelo validador com uma
mensagem genérica sem linha/trecho, forçando o operador a fazer engenharia
reversa do `wk.pyz` e, no fim, apagar os diagramas do artefato para passar
no gate. Este arquivo cobre:

(a) mermaid válido com aspas/acentos/parênteses/dois-pontos/barras NÃO é
    rejeitado (falso positivo corrigido em MERMAID_NODE_ANY_RE);
(b) mermaid genuinamente frágil (label sem aspas) É rejeitado, e a mensagem
    de erro carrega linha do arquivo + trecho da linha ofensiva + nome do
    padrão que disparou;
(c) sdd/c4-context.md sem bloco mermaid reprova o gate (FIX2);
(d) sdd/c4-context.md com bloco mermaid válido passa o gate;
(e) sdd/domain.md (artefato de texto puro) sem mermaid continua passando —
    a exigência de diagrama não pode vazar para artefatos que nunca tiveram
    essa obrigação.
"""

from __future__ import annotations

import unittest

from codescan import sdd as sdd_mod


GREEN = "\U0001F7E2"


class MermaidFalsePositiveTest(unittest.TestCase):
    """(a) construções mermaid legítimas que o operador relatou como
    incorretamente rejeitadas — e algumas variações realistas adicionais
    (subgraph, rótulo de aresta, comentário `%%`) investigadas a pedido do
    diagnóstico."""

    def test_quoted_label_with_parentheses_is_not_rejected(self):
        text = (
            "```mermaid\nflowchart LR\n"
            '  A["Quote Service (v2)"] --> B["DynamoDB"]\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_quoted_label_with_colon_slash_and_accents_is_not_rejected(self):
        text = (
            "```mermaid\nflowchart LR\n"
            '  A["Serviço: Autenticação (OAuth2) - /login"] --> B["DynamoDB (região sa-east-1)"]\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_edge_inline_label_with_parentheses_is_not_rejected(self):
        """Investigação: `-->|texto|` com parênteses no rótulo da aresta
        (ex.: `"200 OK (retry)"`) era escaneado por MERMAID_NODE_ANY_RE como
        se fosse definição de nó, produzindo "label não quoted" a partir do
        texto do rótulo — falso positivo corrigido."""
        text = (
            "```mermaid\nflowchart LR\n"
            '  A["Início"] -->|"200 OK (retry)"| B["Fim"]\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_subgraph_with_quoted_label_is_not_rejected(self):
        text = (
            "```mermaid\nflowchart LR\n"
            '  subgraph SVC["Serviço (core)"]\n'
            '  A["Início"] --> B["Fim"]\n'
            "  end\n"
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_trailing_percent_comment_with_parentheses_is_not_rejected(self):
        """Investigação: comentário `%%` ao FINAL da linha (não a linha
        inteira) também podia colidir com o whitelist de aresta e com o
        scanner de nó quando continha parênteses — corrigido."""
        text = (
            "```mermaid\nflowchart LR\n"
            '  A["Início"] --> B["Fim"] %% nota operacional (v2)\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_full_line_percent_comment_was_already_ignored(self):
        text = (
            "```mermaid\nflowchart LR\n"
            "  %% comentário de linha inteira (ignorado)\n"
            '  A["Início"] --> B["Fim"]\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])


class MermaidLegitimateRejectionDiagnosabilityTest(unittest.TestCase):
    """(b) rejeição legítima continua acontecendo, e a mensagem passa a
    carregar linha do arquivo + trecho + nome do padrão — FIX1(a)."""

    def test_unquoted_label_is_rejected_with_line_and_trecho(self):
        text = (
            "# Doc\n"
            "\n"
            "texto de contexto antes do bloco\n"
            "\n"
            "```mermaid\n"
            "flowchart LR\n"
            '  A[SemAspas] --> B["Fim"]\n'
            "```\n"
        )
        offending_line = '  A[SemAspas] --> B["Fim"]'
        expected_line_no = text.splitlines().index(offending_line) + 1

        errors = sdd_mod._mermaid_errors(text)

        self.assertTrue(any("label" in e for e in errors), errors)
        label_errors = [e for e in errors if "label nao quoted" in e]
        self.assertTrue(label_errors, errors)
        msg = label_errors[0]
        self.assertIn(f"linha {expected_line_no}", msg)
        self.assertIn("SemAspas", msg)
        self.assertIn("padrão: label-nao-quoted", msg)

    def test_unbalanced_brackets_message_carries_line_and_trecho(self):
        text = (
            "```mermaid\n"
            "flowchart LR\n"
            '  A["Inicio"\n'
            '  A --> B["Fim"]\n'
            "```"
        )
        errors = sdd_mod._mermaid_errors(text)
        self.assertTrue(any("desbalanceados" in e for e in errors))
        match = [e for e in errors if "desbalanceados" in e][0]
        self.assertIn("linha 3", match)
        self.assertIn("A[\"Inicio\"", match)
        self.assertIn("padrão:", match)

    def test_fragile_character_message_carries_matched_snippet(self):
        text = (
            '```mermaid\nflowchart LR\n  A["Inicio"] --> B["Fim"]; C["Extra"]\n```'
        )
        errors = sdd_mod._mermaid_errors(text)
        frag = [e for e in errors if "caractere frágil" in e]
        self.assertTrue(frag, errors)
        self.assertIn("linha 3", frag[0])
        self.assertIn("padrão: caractere-fragil-fora-de-quote", frag[0])

    def test_error_payload_is_still_a_list_of_strings(self):
        """Compatibilidade: a estrutura de retorno continua lista de strings
        (não dict) — quem consome `_mermaid_errors` hoje não quebra."""
        text = '```mermaid\nflowchart LR\n  A[Ruim] --> B["Fim"]\n```'
        errors = sdd_mod._mermaid_errors(text)
        self.assertIsInstance(errors, list)
        self.assertTrue(all(isinstance(e, str) for e in errors))


class DiagramRequiredArtifactTest(unittest.TestCase):
    """(c)/(d)/(e) — FIX2: artefatos de diagrama exigem bloco ```mermaid```
    do tipo correto; artefatos de texto puro continuam sem essa exigência."""

    def _c4_context_rule(self) -> sdd_mod.ArtifactRule:
        rule = sdd_mod.rule_for_rel("sdd/c4-context.md")
        assert rule is not None
        return rule

    def _c4_context_text(self, *, with_diagram: bool) -> str:
        body = [
            "# C4 Contexto",
            "",
            "## Contexto",
            "",
            "- fluxo operacional descreve entrada do usuário, validação de estado e saída do sistema.",
            "- integração externa depende de gateway de pagamento e expõe critério de aceite do fluxo.",
            "- dependência entre serviços cobre erro operacional e reimplementação do teste de contrato.",
            "- rastreabilidade entre requisito e diagrama documenta entrada e saída de cada integração.",
            "- estado do pedido transita entre criado, confirmado e cancelado conforme regra de negócio.",
        ]
        if with_diagram:
            body += [
                "",
                "```mermaid",
                "flowchart LR",
                '  A["Usuário"] --> B["Sistema de Cotação"]',
                '  B --> C["Serviço de Pagamento (v2)"]',
                "```",
            ]
        else:
            body += [
                "",
                "- observação adicional: o contexto textual acima substitui o diagrama por enquanto.",
                "- pendência registrada para revisão futura sem uso do marcador de escape auditável.",
            ]
        return "\n".join(body) + "\n"

    def test_c4_context_without_mermaid_fails_gate(self):
        import os
        import tempfile

        rule = self._c4_context_rule()
        text = self._c4_context_text(with_diagram=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c4-context.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            result = sdd_mod._audit_file(path, rule, wd=tmp)

        self.assertNotEqual(result["status"], "pass")
        self.assertTrue(
            any("diagrama mermaid obrigatório ausente" in b for b in result["blockers"]),
            result["blockers"],
        )
        diagram_blocker = [b for b in result["blockers"] if "diagrama mermaid" in b][0]
        self.assertIn("flowchart", diagram_blocker)
        self.assertIn("C4Context", diagram_blocker)

    def test_c4_context_with_mermaid_passes_gate(self):
        import os
        import tempfile

        rule = self._c4_context_rule()
        text = self._c4_context_text(with_diagram=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c4-context.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            result = sdd_mod._audit_file(path, rule, wd=tmp)

        self.assertEqual(result["status"], "pass", result["blockers"])
        self.assertGreaterEqual(result["score"], sdd_mod.MIN_DONE_SCORE)
        self.assertFalse(any("diagrama mermaid" in b for b in result["blockers"]))

    def test_c4_context_without_mermaid_but_with_justified_escape_is_warning_not_blocker(self):
        import os
        import tempfile

        rule = self._c4_context_rule()
        text = self._c4_context_text(with_diagram=False)
        text += "\n<!-- no-diagram: sistema com um único ator, diagrama sem valor incremental -->\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c4-context.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            result = sdd_mod._audit_file(path, rule, wd=tmp)

        self.assertFalse(any("diagrama mermaid obrigatório ausente em" in b and "escape" not in b for b in result["blockers"]))
        self.assertTrue(any("no-diagram" in w for w in result["warnings"]), result["warnings"])

    def test_c4_context_empty_escape_marker_still_fails_gate(self):
        """Escape sem motivo real (`<!-- no-diagram: -->`) não é aceito: o
        mecanismo de escape exige justificativa auditável, não é um bypass
        silencioso."""
        import os
        import tempfile

        rule = self._c4_context_rule()
        text = self._c4_context_text(with_diagram=False) + "\n<!-- no-diagram: -->\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c4-context.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            result = sdd_mod._audit_file(path, rule, wd=tmp)

        self.assertNotEqual(result["status"], "pass")
        self.assertTrue(any("diagrama mermaid obrigatório ausente" in b for b in result["blockers"]))

    def test_architecture_c4_containers_c4_components_and_erd_require_diagram(self):
        expectations = {
            "sdd/architecture.md": ("flowchart", "graph"),
            "sdd/c4-containers.md": ("flowchart", "graph", "C4Context", "C4Container", "C4Component"),
            "sdd/c4-components.md": ("flowchart", "graph", "C4Context", "C4Container", "C4Component"),
            "sdd/erd-complete.md": ("erDiagram",),
        }
        for rel, expected_types in expectations.items():
            with self.subTest(rel=rel):
                rule = sdd_mod.rule_for_rel(rel)
                self.assertIsNotNone(rule)
                self.assertEqual(set(rule.require_diagram), set(expected_types))

    def test_domain_md_without_mermaid_still_passes(self):
        """(e) domain.md é artefato de texto puro: nunca exigiu mermaid e a
        introdução do FIX2 não pode fazer isso vazar para ele."""
        import os
        import tempfile

        rule = sdd_mod.rule_for_rel("sdd/domain.md")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.require_diagram, ())

        citation_lines = "\n".join(
            f"- {GREEN} Regra de negócio {i}: fluxo operacional valida entrada, aplica critério de "
            f"aceite, cobre estado do pedido e trata erro de dependência registrado em "
            f"src/quote/QuoteService.java:{10 + i}."
            for i in range(1, 7)
        )
        text = "\n".join(
            [
                "# Domínio",
                "",
                "## Glossário",
                "- Cotação: proposta de valor calculada para o segurado a partir da entrada informada.",
                "- Apólice: contrato formalizado após confirmação da cotação e integração com pagamento.",
                "- Sinistro: evento coberto que dispara o fluxo de reembolso e reimplementação do teste.",
                "",
                "## Regras de negócio",
                citation_lines,
                "",
                "## Lacunas",
                "- 🔴 Lacuna objetiva: reemissão automática não está coberta em src/quote/QuoteService.java:20.",
                "- 🟡 Lacuna moderada: dependência externa de reimplementação de teste carece de critério "
                "de aceite documentado em src/quote/QuoteService.java:21.",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "domain.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            result = sdd_mod._audit_file(path, rule, wd=tmp)

        self.assertEqual(result["status"], "pass", result["blockers"])
        self.assertFalse(any("diagrama" in b for b in result["blockers"]))
        self.assertFalse(any("diagrama" in w for w in result["warnings"]))

    def test_text_only_stages_have_no_diagram_requirement(self):
        """Nenhuma regra fora de c4-*/architecture/erd-complete exige
        diagrama: permissions, confirmed, inferred, gaps, code-analysis e
        specs/* continuam artefatos de texto puro."""
        text_only_rels = (
            "sdd/domain.md",
            "sdd/permissions.md",
            "sdd/confirmed.md",
            "sdd/inferred.md",
            "sdd/gaps.md",
            "sdd/code-analysis.md",
            "sdd/specs/*/requirements.md",
            "sdd/specs/*/design.md",
            "sdd/specs/*/tasks.md",
        )
        for stage_rules in sdd_mod.RULES.values():
            for rule in stage_rules:
                if rule.rel in text_only_rels:
                    self.assertEqual(rule.require_diagram, (), rule.rel)


class MermaidDiagramKindDispatchTest(unittest.TestCase):
    """BUG B1 (regressão do lote B): `_balanced_mermaid_line` e a checagem de
    caractere frágil rodavam INCONDICIONALMENTE em toda linha de QUALQUER
    bloco mermaid, antes do dispatch por `first.startswith(...)`. Isso trata
    o `{`/`}` da cardinalidade crow's-foot "muitos" (`o{`, `|{`, `}o`, `}|`)
    do `erDiagram` — e o corpo multi-linha de `classDiagram`/`stateDiagram`/
    `C4*` — como chave não fechada, reproduzindo o mesmo incidente que
    motivou o lote B: um `erDiagram` sintaticamente válido era rejeitado e o
    operador removia o diagrama para passar no gate.

    Cobre: dispatch por tipo acontece ANTES das regras de flowchart; toda a
    matriz de cardinalidade do erDiagram é aceita; bloco de atributos é
    aceito; o `erd-complete.md` real do incidente é aceito;
    `sequenceDiagram`/`stateDiagram-v2`/`classDiagram`/`C4Context` canônicos
    não são afetados pelo mesmo vazamento; a rejeição legítima de flowchart
    continua funcionando (sem regressão do FIX1)."""

    def test_er_diagram_accepts_full_crows_foot_cardinality_matrix(self):
        text = (
            "```mermaid\nerDiagram\n"
            '    A ||--|| B : "exactly-one to exactly-one"\n'
            '    A ||--o{ C : "exactly-one to zero-or-many"\n'
            '    A ||--|{ D : "exactly-one to one-or-many"\n'
            '    A }o--|| E : "zero-or-many to exactly-one"\n'
            '    A }|--|| F : "one-or-many to exactly-one"\n'
            '    A }o--o{ G : "zero-or-many to zero-or-many"\n'
            '    A }|--|{ H : "one-or-many to one-or-many"\n'
            '    A |o--o| I : "zero-or-one to zero-or-one"\n'
            '    A |o..o| J : rotulo nao quoted tracejado\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_er_diagram_accepts_attribute_block(self):
        text = (
            "```mermaid\nerDiagram\n"
            "    QUOTE {\n"
            "        string id PK\n"
            "        string status\n"
            "    }\n"
            '    QUOTE ||--o{ OUTBOX_ENTRY : "quoteId"\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_er_diagram_still_rejects_unbalanced_attribute_block(self):
        """A checagem de balanço de `{`/`}` continua existindo — só passou a
        rodar no BLOCO INTEIRO (multi-linha) em vez de por linha, e a
        cardinalidade crow's-foot é removida antes da contagem para não ser
        confundida com abertura/fechamento de bloco de atributos."""
        text = (
            "```mermaid\nerDiagram\n"
            "    QUOTE {\n"
            "        string id PK\n"
            '    QUOTE ||--o{ OUTBOX_ENTRY : "x"\n'
            "```"
        )
        errors = sdd_mod._mermaid_errors(text)
        self.assertTrue(any("bloco aberto sem fechamento" in e for e in errors), errors)

    def test_er_diagram_real_incident_fixture_from_erd_complete(self):
        """Bloco mermaid real copiado do incidente original (`erd-complete`
        do agent-output `architecture-batch-01.txt`, insurance-quote-service)
        que o operador acabou apagando por não conseguir passar no gate."""
        text = (
            "```mermaid\n"
            "erDiagram\n"
            '    QUOTE ||--o| INSURANCE_POLICY : "insurancePolicyId"\n'
            '    QUOTE ||--o{ OUTBOX_ENTRY : "quoteId"\n'
            '    PRODUCT ||--o{ OFFER : "oferece"\n'
            '    OFFER ||--|| MONTHLY_PREMIUM_AMOUNT : "faixa de prêmio"\n'
            '    OFFER ||--o{ COVERAGE : "coberturas"\n'
            '    OFFER ||--o{ ASSISTANCE : "assistências"\n'
            '    QUOTE ||--o{ QUOTE_COVERAGE : "coberturas solicitadas"\n'
            '    QUOTE ||--o{ QUOTE_ASSISTANCE : "assistências solicitadas"\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_er_diagram_bug_b1_minimal_repro_from_incident(self):
        """Entrada mínima exata reportada pelo incidente."""
        text = '```mermaid\nerDiagram\n    QUOTE ||--o{ OUTBOX_ENTRY : "quoteId"\n```'
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_sequence_diagram_canonical_is_not_rejected(self):
        text = (
            "```mermaid\nsequenceDiagram\n"
            "    participant C as Cliente\n"
            "    participant Q as QuoteService\n"
            "    C->>Q: POST /quotes\n"
            "    Q-->>C: 201 Created\n"
            "    Note over Q: valida regras (9 no total)\n"
            "    loop retry\n"
            "        Q->>Q: tenta novamente\n"
            "    end\n"
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_state_diagram_v2_with_composite_state_is_not_rejected(self):
        text = (
            "```mermaid\nstateDiagram-v2\n"
            "    [*] --> Created\n"
            "    state Created {\n"
            "        [*] --> Validated\n"
            "        Validated --> Confirmed\n"
            "    }\n"
            "    Created --> Cancelled\n"
            "    Cancelled --> [*]\n"
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_class_diagram_with_class_body_is_not_rejected(self):
        text = (
            "```mermaid\nclassDiagram\n"
            "    class Quote {\n"
            "        +Long id\n"
            "        +String status\n"
            "        +validate() bool\n"
            "    }\n"
            "    Quote --> Offer\n"
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_c4_context_canonical_with_boundary_block_is_not_rejected(self):
        text = (
            "```mermaid\nC4Context\n"
            "    title System Context diagram\n"
            '    Person(customer, "Customer", "A customer of the insurance system.")\n'
            '    System(quoteSystem, "Quote Service", "Handles quotes")\n'
            '    System_Boundary(b1, "Quote Boundary") {\n'
            '        System(inner, "Inner System", "desc")\n'
            "    }\n"
            '    Rel(customer, quoteSystem, "Uses", "HTTPS")\n'
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_c4_container_and_c4_component_canonical_are_not_rejected(self):
        for kind in ("C4Container", "C4Component"):
            with self.subTest(kind=kind):
                text = (
                    f"```mermaid\n{kind}\n"
                    '    Container(api, "API", "Spring MVC", "REST endpoints")\n'
                    '    Container(db, "DB", "DynamoDB", "Persistência")\n'
                    '    Rel(api, db, "lê/grava", "SDK")\n'
                    "```"
                )
                self.assertEqual(sdd_mod._mermaid_errors(text), [])

    def test_class_state_and_c4_still_reject_unbalanced_block(self):
        """O balanço multi-linha continua existindo para esses tipos — só a
        checagem por linha (que quebrava blocos legítimos) foi removida."""
        cases = {
            "classDiagram": "```mermaid\nclassDiagram\n    class Quote {\n        +Long id\n```",
            "stateDiagram-v2": "```mermaid\nstateDiagram-v2\n    state Created {\n        [*] --> Validated\n```",
            "C4Context": '```mermaid\nC4Context\n    System_Boundary(b1, "X") {\n        System(a, "A")\n```',
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                errors = sdd_mod._mermaid_errors(text)
                self.assertTrue(any("bloco aberto sem fechamento" in e for e in errors), errors)

    def test_flowchart_dispatch_still_happens_after_kind_detection(self):
        """`_mermaid_kind` roda ANTES das checagens de flowchart: o balanço
        de brackets/quotes e o caractere frágil só se aplicam quando o tipo
        detectado é flowchart/graph/quadrantChart."""
        self.assertEqual(sdd_mod._mermaid_kind("flowchart LR"), "flowchart")
        self.assertEqual(sdd_mod._mermaid_kind("graph TB"), "flowchart")
        self.assertEqual(sdd_mod._mermaid_kind("erDiagram"), "erDiagram")
        self.assertEqual(sdd_mod._mermaid_kind("sequenceDiagram"), "sequenceDiagram")
        self.assertEqual(sdd_mod._mermaid_kind("classDiagram"), "classDiagram")
        self.assertEqual(sdd_mod._mermaid_kind("stateDiagram-v2"), "stateDiagram")
        self.assertEqual(sdd_mod._mermaid_kind("C4Context"), "c4")
        self.assertEqual(sdd_mod._mermaid_kind("quadrantChart"), "quadrantChart")

    def test_flowchart_unquoted_label_regression_still_rejected_with_diagnostics(self):
        """Regressão: nada do BUG B1 pode enfraquecer a rejeição legítima de
        flowchart do FIX1 — label sem aspas continua reprovando, com linha,
        trecho e nome do padrão na mensagem."""
        text = (
            "# Doc\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            '  A[SemAspas] --> B["Fim"]\n'
            "```\n"
        )
        offending_line = '  A[SemAspas] --> B["Fim"]'
        expected_line_no = text.splitlines().index(offending_line) + 1

        errors = sdd_mod._mermaid_errors(text)
        label_errors = [e for e in errors if "label nao quoted" in e]

        self.assertTrue(label_errors, errors)
        self.assertIn(f"linha {expected_line_no}", label_errors[0])
        self.assertIn("SemAspas", label_errors[0])
        self.assertIn("padrão: label-nao-quoted", label_errors[0])

    def test_flowchart_still_rejects_unbalanced_brackets(self):
        text = '```mermaid\nflowchart LR\n  A["Inicio"\n  A --> B["Fim"]\n```'
        errors = sdd_mod._mermaid_errors(text)
        self.assertTrue(any("desbalanceados" in e for e in errors), errors)

    def test_quadrant_chart_still_validated_as_before(self):
        """quadrantChart continua sob as checagens de balanço/caractere
        frágil (não sofria do BUG B1 e não deve mudar de comportamento)."""
        text = (
            "```mermaid\nquadrantChart\n"
            "    title Reach vs Impact\n"
            "    x-axis Low Reach --> High Reach\n"
            "    y-axis Low Impact --> High Impact\n"
            "    P001: [0.5, 0.5]\n"
            "```"
        )
        self.assertEqual(sdd_mod._mermaid_errors(text), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
