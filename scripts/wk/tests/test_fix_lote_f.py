# -*- coding: utf-8 -*-
"""Testes do FIX (P1) — diagramas Mermaid perdidos na conversão para docx.

Cobre sequenceDiagram, stateDiagram-v2, C4Context, regressão de flowchart e
robustez a mermaid malformado em scripts/wk/docx_md.py.

Os fixtures de sequenceDiagram/flowchart abaixo são cópias literais de arquivos
reais gerados pelo codescan (não lidos em runtime, conforme obrigação #2):
  C:\\Users\\User\\projetos\\teste_wk\\store\\.codescan\\insurance-quote-service-efb62c18\\
      sdd\\sequences\\create-quote.md
      sdd\\sequences\\policy-issued.md
      sdd\\flowcharts\\_index.md
"""

import unittest

from wk import docx_md


# ----------------------------------------------------------------------
# Fixtures reais (copiados de C:\Users\User\projetos\teste_wk\store\.codescan\
# insurance-quote-service-efb62c18\sdd\...), embutidos como constantes.
# ----------------------------------------------------------------------

CREATE_QUOTE_MD = """# Sequência — Criação de Cotação

## Visão geral
Diagrama de sequência do fluxo de criação de cotação, desde a requisição do cliente até a publicação do evento via outbox.

## Fluxo de criação
```mermaid
sequenceDiagram
    participant C as Cliente
    participant QC as QuoteController
    participant CQU as CreateQuoteUseCase
    participant CL as CatalogLookup
    participant VR as QuoteValidationRules
    participant QR as QuoteRepository
    participant POJ as PublishQuoteOutboxEventsJob
    participant K as Kafka

    C->>QC: POST /quotes
    QC->>CQU: create(command)
    CQU->>CL: findProductAndOffer(productId, offerId)
    CL-->>CQU: Product, Offer
    CQU->>VR: validate(quote, product, offer)
    VR-->>CQU: List<RuleViolation>
    alt violacoes vazias
        CQU->>QR: save(quote)
        CQU->>QR: saveOutboxEntry(quoteId)
        CQU-->>QC: QuoteResponse (201)
        QC-->>C: 201 Created + Location
    else violacoes presentes
        CQU->>CQU: recordQuoteRejected
        CQU-->>QC: QuoteValidationException
        QC-->>C: 422 + ProblemDetail
    end
    POJ->>QR: findPending
    POJ->>QR: claim(quoteId)
    POJ->>K: publish(QuoteReceivedMessage)
    POJ->>QR: markPublished(quoteId)
```

## Detalhamento por etapa
- Entrada: POST /quotes com body contendo customerId, productId, offerId, coverages, assistences, totalCoverageAmount, totalMonthlyPremium 🟢 QuoteController.java:32
- Consulta ao catálogo: virtual threads em paralelo para produto e oferta 🟢 CatalogLookup.java:24
- Validacao: 9 regras executadas; primeira violacao reportada por regra de colecao 🟢 QuoteValidationRules.java:23
- Persistencia: Quote salva com ID gerado; entrada outbox criada como pending 🟢 CreateQuoteUseCase.java:44
- Erro: QuoteValidationException convertida em ProblemDetail RFC 7807 com violations 🟢 ApiExceptionHandler.java:53
- Outbox: job agendado @Scheduled(5s) reclama entradas pending, publica no Kafka e marca published 🟢 PublishQuoteOutboxEventsJob.java:65

## Riscos
- 🔴 Se markPublished falhar apos publish bem-sucedido, evento pode ser duplicado na proxima reclamacao 🟡 PublishQuoteOutboxEventsJob.java:100
- 🟡 Regras de colecao reportam apenas primeira violacao (findFirst) 🟢 CoveragesMustBeAllowedRule.java:15
"""

POLICY_ISSUED_MD = """# Sequencia — Emissao de Apolice

## Visao geral
Diagrama de sequencia do fluxo de emissao de apolice, desde o consumo de QuoteReceivedMessage pelo policy-service ate o processamento de PolicyIssuedMessage pelo quote-service.

## Fluxo de emissao
```mermaid
sequenceDiagram
    participant K as Kafka
    participant QRC as QuoteReceivedConsumer
    participant IPU as IssuePolicyUseCase
    participant PIP as PolicyIssuedPublisher
    participant PIC as PolicyIssuedConsumer
    participant PPIU as ProcessPolicyIssuedUseCase
    participant QR as QuoteRepository

    K->>QRC: QuoteReceivedMessage
    alt quoteId nulo
        QRC->>QRC: log e skip
    else quoteId valido
        QRC->>IPU: issue(quoteId)
        IPU-->>QRC: PolicyIssued
        QRC->>PIP: publish(PolicyIssuedMessage)
        PIP->>K: send(topic, key, message).get(10s)
        K->>PIC: PolicyIssuedMessage
        PIC->>PPIU: process(event)
        PPIU->>QR: updateInsurancePolicy(quoteId, policyId)
        alt UPDATED
            PPIU->>PPIU: recordPolicyUpdated
        else DUPLICATE
            PPIU->>PPIU: recordPolicyDuplicate
        else CONFLICT ou NOT_FOUND
            PPIU-->>PIC: FunctionalPolicyIssuedException
        end
    end
```

## Detalhamento por etapa
- Entrada: QuoteReceivedMessage consumida do topico quote-received 🟢 QuoteReceivedConsumer.java:33
- Skip: se quoteId nulo, loga e retorna sem processar 🟢 QuoteReceivedConsumer.java:45
- Erro: RuntimeException rethrow para acionar handler do container Kafka 🟢 QuoteReceivedConsumer.java:63
- Publicacao: PolicyIssuedMessage enviada com timeout sincrono de 10s 🟢 PolicyIssuedPublisher.java:42
- Estado: outcome UPDATED/DUPLICATE/CONFLICT/NOT_FOUND ramifica o processamento 🟢 ProcessPolicyIssuedUseCase.java:33
- Dependencia: PolicyIssuedPublisher trata InterruptedException, ExecutionException, TimeoutException 🟢 PolicyIssuedPublisher.java:42

## Riscos
- 🔴 Timeout de 10s no publisher pode bloquear o consumer; nao ha retry no publisher 🟢 PolicyIssuedPublisher.java:42
- 🟡 Consumer com auto-commit false e offset reset earliest pode reprocessar mensagens 🟢 PolicyKafkaConfiguration.java:39
"""

FLOWCHART_INDEX_MD = """# Fluxos de módulos

## Fluxo consolidado

- Entrada: resultados de análise dos módulos em `modules/*.md`.
- Processo: cada módulo alimenta a consolidação de SDD do estágio `modules`.
- Saída: `code-analysis.md`, `data-dictionary.md` quando há entidades, e este índice Mermaid.
- Estado: os itens só são marcados como concluídos depois que os artefatos são gravados.
- Rastreabilidade: detalhes e citações permanecem nos módulos individuais.

```mermaid
flowchart LR
  inicio["Entrada de módulos"]
  m001["policy-service-src-main-java-br-com-acme-insurance-..."]
  m002["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m003["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m004["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m005["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m006["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m007["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m008["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m009["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m010["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m011["quote-service-src-main-java-br-com-acme-insurance-q..."]
  m012["quote-service-src-main-java-br-com-acme-insurance-q..."]
  fim["Artefatos SDD"]
  inicio --> m001
  m001 --> m002
  m002 --> m003
  m003 --> m004
  m004 --> m005
  m005 --> m006
  m006 --> m007
  m007 --> m008
  m008 --> m009
  m009 --> m010
  m010 --> m011
  m011 --> m012
  m012 --> fim
```
"""


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _texto(block):
    return "".join(r["text"] for r in block["runs"])


def _all_text(blocks):
    partes = []
    for b in blocks:
        if b["kind"] in ("paragraph", "list_item"):
            partes.append(_texto(b))
        elif b["kind"] == "table":
            for cell in b.get("header") or []:
                partes.append("".join(r["text"] for r in cell))
            for row in b.get("rows") or []:
                for cell in row:
                    partes.append("".join(r["text"] for r in cell))
    return "\n".join(partes)


class TestSequenceDiagramReal(unittest.TestCase):
    """(a) sequenceDiagram real produz lista/tabela de mensagens e NÃO cai no fallback."""

    def test_create_quote_nao_cai_em_fallback(self):
        blocks, warnings = docx_md.parse(CREATE_QUOTE_MD)
        texto = _all_text(blocks)
        self.assertNotIn("não renderizado", texto)
        self.assertNotIn("nao renderizado", texto)
        # nenhum warning de tipo mermaid nao suportado
        self.assertFalse(any("nao suportado" in w for w in warnings))

    def test_create_quote_produz_mensagens(self):
        blocks, _ = docx_md.parse(CREATE_QUOTE_MD)
        itens = [_texto(b) for b in blocks if b["kind"] == "list_item"]
        self.assertIn("Cliente → QuoteController: POST /quotes", itens)
        self.assertIn("QuoteController → CreateQuoteUseCase: create(command)", itens)
        # participantes resolvidos via alias (nao aparece o id bruto "CQU")
        self.assertTrue(any("CreateQuoteUseCase" in i for i in itens))
        self.assertFalse(any(i.startswith("CQU ") or " CQU " in i for i in itens))

    def test_policy_issued_nao_cai_em_fallback(self):
        blocks, warnings = docx_md.parse(POLICY_ISSUED_MD)
        texto = _all_text(blocks)
        self.assertNotIn("não renderizado", texto)
        self.assertFalse(any("nao suportado" in w for w in warnings))
        itens = [_texto(b) for b in blocks if b["kind"] == "list_item"]
        self.assertIn("Kafka → QuoteReceivedConsumer: QuoteReceivedMessage", itens)


class TestSequenceDiagramAgrupamentoENotas(unittest.TestCase):
    """(b) alt/else/loop/Note over são representados."""

    def test_alt_else_indentacao(self):
        blocks, _ = docx_md.parse(CREATE_QUOTE_MD)
        itens = [(b["level"], _texto(b)) for b in blocks if b["kind"] == "list_item"]
        niveis = dict(itens)
        self.assertIn((0, "Alternativa: violacoes vazias"), itens)
        self.assertIn((0, "Senão: violacoes presentes"), itens)
        # mensagens dentro do alt/else ficam indentadas (nivel > 0)
        self.assertIn((1, "CreateQuoteUseCase → QuoteRepository: save(quote)"), itens)
        self.assertIn((1, "CreateQuoteUseCase → CreateQuoteUseCase: recordQuoteRejected"), itens)
        # depois do 'end' volta ao nivel 0
        self.assertIn((0, "PublishQuoteOutboxEventsJob → QuoteRepository: findPending"), itens)

    def test_alt_aninhado_policy_issued(self):
        blocks, _ = docx_md.parse(POLICY_ISSUED_MD)
        itens = [(b["level"], _texto(b)) for b in blocks if b["kind"] == "list_item"]
        # alt externo (quoteId nulo/valido) aninha um segundo alt (UPDATED/DUPLICATE/...)
        self.assertIn((0, "Alternativa: quoteId nulo"), itens)
        self.assertIn((0, "Senão: quoteId valido"), itens)
        self.assertIn((1, "Alternativa: UPDATED"), itens)
        self.assertIn((1, "Senão: DUPLICATE"), itens)
        self.assertIn((1, "Senão: CONFLICT ou NOT_FOUND"), itens)
        self.assertIn((2, "ProcessPolicyIssuedUseCase → ProcessPolicyIssuedUseCase: recordPolicyUpdated"), itens)

    def test_loop_e_note_over(self):
        md = """```mermaid
sequenceDiagram
    participant A as Alfa
    participant B as Beta
    Note over A,B: início da rotina
    loop a cada 5s
        A->>B: heartbeat
    end
    Note left of A: observação lateral
```"""
        blocks, warnings = docx_md.parse(md)
        texto = _all_text(blocks)
        self.assertNotIn("não renderizado", texto)
        itens = [(b["level"], _texto(b)) for b in blocks if b["kind"] == "list_item"]
        self.assertIn((0, "Nota (sobre Alfa, Beta): início da rotina"), itens)
        self.assertIn((0, "Repetição: a cada 5s"), itens)
        self.assertIn((1, "Alfa → Beta: heartbeat"), itens)
        self.assertIn((0, "Nota (à esquerda de Alfa): observação lateral"), itens)

    def test_activate_deactivate_explicito_e_atalho(self):
        md = """```mermaid
sequenceDiagram
    participant A
    participant B
    activate A
    A->>+B: chama
    B-->>-A: responde
    deactivate A
```"""
        blocks, _ = docx_md.parse(md)
        itens = [_texto(b) for b in blocks if b["kind"] == "list_item"]
        self.assertIn("Ativa A", itens)
        self.assertIn("Desativa A", itens)
        self.assertTrue(any("(ativa)" in i for i in itens))
        self.assertTrue(any("(desativa)" in i for i in itens))


class TestStateDiagram(unittest.TestCase):
    """(c) stateDiagram-v2 produz tabela de transições."""

    def test_transicoes_com_inicio_e_fim(self):
        md = """```mermaid
stateDiagram-v2
    [*] --> Pendente
    Pendente --> Aprovada: aprovar
    Pendente --> Rejeitada: rejeitar
    Aprovada --> [*]
    Rejeitada --> [*]
```"""
        blocks, warnings = docx_md.parse(md)
        texto = _all_text(blocks)
        self.assertNotIn("não renderizado", texto)
        tabelas = [b for b in blocks if b["kind"] == "table"]
        self.assertEqual(len(tabelas), 1)
        header = ["".join(r["text"] for r in c) for c in tabelas[0]["header"]]
        self.assertEqual(header, ["Origem", "Evento/Condição", "Destino"])
        rows = [
            tuple("".join(r["text"] for r in c) for c in row)
            for row in tabelas[0]["rows"]
        ]
        self.assertIn(("[*] (estado inicial)", "", "Pendente"), rows)
        self.assertIn(("Pendente", "aprovar", "Aprovada"), rows)
        self.assertIn(("Aprovada", "", "[*] (estado final)"), rows)

    def test_alias_de_estado_composto(self):
        md = """```mermaid
stateDiagram-v2
    state "Em Análise" as EA
    [*] --> EA
    EA --> [*]
```"""
        blocks, _ = docx_md.parse(md)
        tabelas = [b for b in blocks if b["kind"] == "table"]
        rows = [
            tuple("".join(r["text"] for r in c) for c in row)
            for row in tabelas[0]["rows"]
        ]
        self.assertIn(("[*] (estado inicial)", "", "Em Análise"), rows)


class TestC4Context(unittest.TestCase):
    """(d) C4Context produz tabelas de elementos e relações."""

    def test_elementos_e_relacoes(self):
        md = """```mermaid
C4Context
    title Contexto do sistema de cotação
    Person(customer, "Cliente", "Compra seguros online")
    System(quoteService, "Quote Service", "Gera cotações")
    System_Ext(kafka, "Kafka", "Message broker")
    Rel(customer, quoteService, "Usa", "HTTPS")
    Rel(quoteService, kafka, "Publica eventos", "Kafka Protocol")
```"""
        blocks, warnings = docx_md.parse(md)
        texto = _all_text(blocks)
        self.assertNotIn("não renderizado", texto)
        tabelas = [b for b in blocks if b["kind"] == "table"]
        self.assertEqual(len(tabelas), 2, "esperado tabela de elementos + tabela de relações")

        elementos = [
            tuple("".join(r["text"] for r in c) for c in row)
            for row in tabelas[0]["rows"]
        ]
        self.assertIn(("Person", "Cliente", "Compra seguros online", ""), elementos)
        self.assertIn(("System", "Quote Service", "Gera cotações", ""), elementos)
        self.assertIn(("System_Ext", "Kafka", "Message broker", ""), elementos)

        relacoes = [
            tuple("".join(r["text"] for r in c) for c in row)
            for row in tabelas[1]["rows"]
        ]
        self.assertIn(("Cliente", "Quote Service", "Usa", "HTTPS"), relacoes)
        self.assertIn(("Quote Service", "Kafka", "Publica eventos", "Kafka Protocol"), relacoes)

    def test_container_com_tecnologia(self):
        md = """```mermaid
C4Container
    Container(api, "API", "Spring Boot", "Expõe endpoints REST")
```"""
        blocks, _ = docx_md.parse(md)
        tabelas = [b for b in blocks if b["kind"] == "table"]
        elementos = [
            tuple("".join(r["text"] for r in c) for c in row)
            for row in tabelas[0]["rows"]
        ]
        self.assertIn(("Container", "API", "Expõe endpoints REST", "Spring Boot"), elementos)


class TestFlowchartRegressao(unittest.TestCase):
    """(e) flowchart mantém comportamento anterior (regressão)."""

    def test_flowchart_index_real_vira_lista_de_dependencias(self):
        blocks, warnings = docx_md.parse(FLOWCHART_INDEX_MD)
        legendas = [_texto(b) for b in blocks if b["kind"] == "paragraph"]
        self.assertIn("Dependências entre módulos:", legendas)
        itens = [_texto(b) for b in blocks if b["kind"] == "list_item"]
        self.assertTrue(any("depende de" in i for i in itens))
        self.assertTrue(any(i.startswith("Entrada de módulos depende de") for i in itens))
        texto = _all_text(blocks)
        self.assertNotIn("não renderizado", texto)

    def test_flowchart_com_subgraph_agrupamento_preservado(self):
        md = """```mermaid
flowchart TD
  subgraph Zona1["Zona Um"]
    a["Nó A"]
    b["Nó B"]
    a --> b
  end
```"""
        blocks, _ = docx_md.parse(md)
        legendas = [_texto(b) for b in blocks if b["kind"] == "paragraph"]
        self.assertIn("Agrupamento por zona:", legendas)
        itens = [_texto(b) for b in blocks if b["kind"] == "list_item"]
        self.assertIn("Nó B está em Zona Um", itens)

    def test_erdiagram_preservado(self):
        md = """```mermaid
erDiagram
    CLIENTE ||--o{ PEDIDO : "realiza"
```"""
        blocks, _ = docx_md.parse(md)
        legendas = [_texto(b) for b in blocks if b["kind"] == "paragraph"]
        self.assertIn("Entidades e relacionamentos:", legendas)
        itens = [_texto(b) for b in blocks if b["kind"] == "list_item"]
        self.assertIn("CLIENTE realiza PEDIDO (cardinalidade ||--o{)", itens)


class TestMermaidMalformadoNaoLevantaExcecao(unittest.TestCase):
    """(f) mermaid malformado não levanta exceção e degrada para o fallback melhorado."""

    def test_sequence_diagram_lixo_nao_quebra(self):
        md = """```mermaid
sequenceDiagram
    isto nao eh uma linha valida ->>> :::
    ((( colchetes desbalanceados [[[
    -->> sem origem
```"""
        try:
            blocks, warnings = docx_md.parse(md)
        except Exception as e:  # pragma: no cover - a asserção abaixo já cobre a falha
            self.fail(f"parse() levantou excecao com mermaid malformado: {e!r}")
        texto = _all_text(blocks)
        self.assertIn("não renderizado", texto)
        self.assertTrue(any("mermaid" in w for w in warnings))

    def test_bloco_mermaid_vazio_nao_quebra(self):
        md = "```mermaid\n```"
        blocks, warnings = docx_md.parse(md)
        self.assertIsInstance(blocks, list)
        texto = _all_text(blocks)
        self.assertIn("desconhecido", texto)

    def test_tipo_totalmente_desconhecido_cai_no_fallback_melhorado(self):
        md = """```mermaid
pie title Distribuição
    "A" : 40
    "B" : 60
```"""
        blocks, warnings = docx_md.parse(md)
        texto = _all_text(blocks)
        self.assertIn("não renderizado", texto)
        self.assertIn("disponível na versão Markdown da wiki", texto)
        # o codigo bruto continua presente, em paragrafo CodeBlock monoespacado
        code_blocks = [b for b in blocks if b.get("style") == "CodeBlock"]
        self.assertTrue(len(code_blocks) >= 2)

    def test_c4_sem_argumentos_nao_quebra(self):
        md = """```mermaid
C4Context
    Rel()
    Person()
```"""
        try:
            blocks, warnings = docx_md.parse(md)
        except Exception as e:  # pragma: no cover
            self.fail(f"parse() levantou excecao com C4 malformado: {e!r}")
        self.assertIsInstance(blocks, list)

    def test_state_diagram_sem_transicoes_reconheciveis_cai_no_fallback(self):
        md = """```mermaid
stateDiagram-v2
    isto nao eh uma transicao valida
```"""
        blocks, warnings = docx_md.parse(md)
        texto = _all_text(blocks)
        self.assertIn("não renderizado", texto)


if __name__ == "__main__":
    unittest.main()
