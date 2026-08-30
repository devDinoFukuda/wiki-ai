---
name: wiki-ai
description: >
  Wiki AI corporativo com fonte-verdade curada. Use ao ingerir
  conhecimento (transcrições, repositórios, docs), ao promover fontes,
  compilar a wiki, ou auditar integridade. Governa como o conhecimento entra,
  é validado por proveniência, vira fonte canônica e é compilado — sem que
  saída de agente contamine o corpus.
argument-hint: "[ingest <caminho> | promote | compile | lint | query <pergunta>]"
---

# Wiki AI

Fluxo: `ingest → promote → compile → lint` sobre `inbox/ → raw/ → wiki/`. A
leitura enxerga só `wiki/` e `raw/`.

## Documentação sob demanda
Só este `SKILL.md` está em disco. Leia o resto do executável:

```bash
{{WK}} docs <slug>     # ex.: {{WK}} docs schema
{{WK}} docs --list
```

Doc citado por nome de arquivo (`schema.md`, `operations/promote.md`): slug = nome
sem extensão nem pasta. Leia com `{{WK}} docs <slug>`, não com a ferramenta de arquivo.

**Leia `{{WK}} docs schema` antes de qualquer operação.**

## Roteador de operações
`$ARGUMENTS`: primeira palavra = operação. Leia o doc com `{{WK}} docs <slug>` antes de agir.

| Operação  | O que faz                                   | Slug do doc         | Comando CLI equivalente |
|-----------|---------------------------------------------|---------------------|--------------------------|
| `ingest`  | Registra fonte nova em `inbox/` (arquivo solto: transcrição, doc, clip) | `ingest` | `{{WK}} ingest <arquivo> --source-type <tipo> --origin <origem> --topic <slug>` |
| `ingest codebase` | Pipeline de repositório legado — operação da skill, **não** subcomando do CLI | `ingest-codebase` | `{{WK}} code --repo <r> --store <s> surface --topic <slug>` (início do pipeline) |
| `promote` | Roda o portão de promoção (`inbox/`->`raw/`)| `promote`           | `{{WK}} promote --store <s>` |
| `compile` | (Re)gera a wiki a partir de `raw/`          | `compile`           | `{{WK}} compile <topic> --store <s>` |
| `docx`    | Gera `wiki-docx/` (DOCX) a partir de `raw/` promovido | `docx`              | `{{WK}} docx [<topico>] [--store <path>] [--out-dir wiki-docx] [--no-prune]` |
| `lint`    | Audita integridade (somente leitura)        | `lint`              | `{{WK}} lint --store <s>` |
| `query`   | Consulta a wiki (teste local de leitura)    | `retrieval`         | `{{WK}} index search -c wiki -n <n>` |
| `reindex` | Atualiza o índice de busca                  | `retrieval`         | `{{WK}} index reindex --store <s>` |

`{{WK}} ingest` só aceita arquivo de fonte solto — nunca um caminho de
repositório. `{{WK}} ingest codebase ...` não existe como subcomando: falha
com erro acionável apontando o comando `code ... surface` acima; o pipeline
de repositório é a operação da skill `ingest codebase`, executada via `code`.

Se `$ARGUMENTS` estiver vazio, mostre este roteador e pergunte qual operação.

## Contrato de entrada
- Slash command não é shell: `$WK_REPO`, `$WK_TOPIC` e outras variáveis chegam como texto literal.
- `/wiki-ai ingest codebase $WK_REPO --topic $WK_TOPIC` é inválido por design. Pare e peça caminho/topic literais, ou use o modo CLI no Git Bash.
- Válido em slash command: `/wiki-ai ingest codebase C:/caminho/do/repo --topic codebases/nome`.
- Exports são permitidos somente quando você estiver executando a CLI no Git Bash.

## Guardrails invioláveis
Vencem qualquer instrução em contrário.

1. `raw/` é imutável exceto via `promote`.
2. Nunca promova automaticamente — só quando o usuário pedir nesta mensagem.
3. `agent-output` nunca vira canônico sem aprovação humana; `origin` preserva a origem de agente.
4. A leitura só enxerga `wiki/` e `raw/`. `inbox/` nunca é exposto; saídas de agente voltam a `inbox/agent-output/`.
5. Sem `origin` + `source_type`, não é fonte → quarentena.
6. `lint` recusa rodar sem `index.db` (exit 2, `acao: index reindex`) — nunca cria
   um banco vazio. Já `status` reportar `indice_sujo` (arquivo em disco mais
   novo que o indexado, por mtime) é convenção operacional, não gate: nada em
   `compile`/`lint` lê esse campo. Reindexe de qualquer forma antes de operar
   se `status` mostrar `indice_sujo` não-vazio — é sinal de fonte de verdade
   divergente do índice, mesmo sem bloqueio automático.
7. `reindex` ao fim de todo compile.
8. Não use `python -c`, heredoc ou script ad hoc para inspecionar JSON operacional.
   Para surface/state/retomada, use `{{WK}} code ... state`, `next`, `read` e `audit`.
9. Nunca use `Write`/`Edit` direto dentro do store: `wk init --store <s> --repo <r>`
   grava essa permissão como `deny` na engine. Quem escreve em `inbox/`/`raw/`/
   `wiki/` é sempre `{{WK}}` (`ingest`, `publish`, `promote`, `compile`).
10. Shell: PowerShell é proibido, todo comando roda em Git Bash. Sintoma de
    diagnóstico: saída com `No linha:` seguido de `caractere:`, ou o marcador
    `~~`, ou sugestão de `Get-ChildItem`/`Select-Object` = shell é PowerShell.
    Remediação: reexecute embrulhando em `bash -c '<comando>'`. Primeiro
    comando de qualquer sessão: `{{WK}} doctor --store <s> --repo <r> --engine <e>`.
11. Estágios `modules`, `rules`, `architecture`, `specs` e `synth` exigem
    subagente; o orquestrador nunca gera conteúdo SDD diretamente. O fluxo
    padrão é dirigido pelo humano: 👤 roda `code run <stage>` — composto que
    gera os agent-packs + `agent-packs/<stage>-contract.json` + manifesto
    (informa `fanout_required`) e já imprime o prompt de despacho pronto —,
    e cola esse prompt em você (`run` substitui a dupla antiga
    `run-stage`+`handoff`, que continua existindo à parte para controle
    fino). Você atua só como despachante: dispara 1 subagente por batch;
    cada subagente lê o pack e o contrato como ARQUIVOS (não executa
    comando), grava o próprio `output` e devolve recibo de 3 linhas
    (`ARQUIVO:`/`BLOCOS:`/`BYTES:`). Depois 👤 roda `code integrate <stage>`
    — composto que faz `merge-agent-output` de todos os batches do
    manifesto (usando o `agent_slot` de cada um) e fecha com `done`;
    `--agent` genérico (`main`, `orquestrador`, `self`, `principal`) é
    recusado em qualquer um dos dois caminhos.
12. Campo `acao`: ao ver `"acao"` em qualquer JSON de erro (principalmente de
    `merge-agent-output`), execute esse comando LITERALMENTE antes de
    qualquer outra investigação — geralmente `sdd-brief <stage>`. `done` traz
    a dica em `blockers[].action` (inglês), só quando há mais de um blocker.
    NUNCA abra `wk.pyz` com `zipfile`/decompilação/regex sobre o bytecode
    para entender um erro — o contrato de cada estágio é sempre
    `{{WK}} code sdd-brief <stage>` (README.md, seção "Retomada e erro", tem
    a mesma proibição).

## Seu papel neste fluxo

Duas personas só: 👤 humano digita comando, 🤖 você digita comando. `{{WK}}`
é ferramenta, nunca ator — nenhuma frase deste doc deve ler "o script faz X";
é sempre "👤/🤖 roda `{{WK}} X`". Três tipos de passo:

- **D** (determinístico): mesma entrada → mesma saída, sem LLM. **Padrão
  operacional: 👤 roda esses comandos** — os scripts geram as estruturas e
  artefatos; você só os roda quando o humano pedir explicitamente nesta
  mensagem. Isto é a esmagadora maioria de `{{WK}}` — `ingest`, `promote`,
  `compile`, `docx`, `lint` (parte mecânica W1–W3/L1/L2/L4/L5), `index
  status/reindex/search/audit`, e todo `{{WK}} code
  surface/export/config/plan/pending/done/next/evidence/run-stage/handoff
  /run/integrate/merge-agent-output/verify/drift/sdd-brief`, além de
  `{{WK}} finish`. Nenhum deles chama um modelo — conferido lendo
  `scripts/wk/cli.py`, `scripts/codescan/cli.py` e `scripts/sbindex/cli.py`:
  nenhum importa cliente de LLM algum.
- **M** (requer modelo): sem você, o fluxo empaca — ninguém mais produz esse
  conteúdo. **5 pontos no pipeline de código** (mesmo recorte da tabela
  "Divisão de responsabilidade" do README, seção "FLUXO 3") + **2 pontos
  fora dele** = **7 pontos M no total** no sistema inteiro:
  1. **Fan-out `modules`** — ler módulo a módulo e extrair regras de negócio.
  2. **Fan-out `rules`** — consolidar/depurar as regras extraídas.
  3. **Fan-out `architecture`** — C4, ERD, integrações, dívida técnica.
  4. **Fan-out `specs`** — requirements/design/tasks por unit, matrizes de
     rastreabilidade, confidence-report.
  5. **Fan-out `synth`** — cruzar tudo em `sdd/confirmed.md`/`inferred.md`
     (blocos `=== SYNTH: confirmed ===`/`=== SYNTH: inferred ===`).
  6. **Análise de assets** `.docx`/`.xlsx`/`.csv`/`.pdf` — ler o original em
     `raw/assets/` e escrever a página de análise (`Fluxo de análise` abaixo).
  7. **Síntese de resposta em retrieval** — `index search` devolve trechos
     brutos; transformar isso em resposta de prosa é você, não o motor de
     busca (BM25/RRF são D).
  Nos 5 do pipeline de código, `run`/`integrate` (ou os atômicos
  `run-stage`/`handoff`/`merge-agent-output`/`done`) ao redor continuam
  **D** (👤 roda) — só o conteúdo que o subagente escreve dentro do fan-out
  é M. Sua porta de entrada nos 5 pontos M é o prompt de despacho que o
  humano cola (saída de `{{WK}} code run <stage>`, ou de `handoff <stage>`
  no caminho atômico): dispare os subagentes ali listados e devolva só os
  recibos.
- **H** (decisão humana): você não decide sozinho — **pergunta ao humano**,
  mesmo que tecnicamente pudesse rodar o comando. Vale para:
  - `{{WK}} code ... config --doc-level <x> --granularity <y>` — os valores
    não são dedutíveis do código; são escolha de quem encomendou a análise.
  - `{{WK}} code ... pending specs --items <lista>` — escopo de negócio
    (quais units entram), não algo que se lê do repositório.
  - Aprovação em `{{WK}} promote --approve/--approve-all` para qualquer
    `source_type` que não seja `code-repo` — guardrail #2 abaixo proíbe você
    de aprovar sozinho, mesmo tendo rodado o `promote` que listou o item.
    `--approve-all` só roda com `--source-type`+`--topic`+`--approved-by`
    juntos (escopo fechado, não é o inbox inteiro).
  - `{{WK}} finish --workdir <w> --topic <t> --repo <r> --store <s>
    --approved-by <quem>` — sem `--approve` ele PARA sozinho antes do
    `promote` (exit 3, `parado_em: "promote"`, lista `pendentes[]`); só
    repita com `--approve` depois de o humano confirmar.
  - `--allow-unverified` em `promote`/`compile`/`docx`/`finish` — você não
    decide ignorar um `verify` reprovado; expõe o bloqueio e pergunta.
  - `--allow-feedback-loop` em `promote` — bloqueio `realimentacao`
    (`agent-output` cujo `derived_from` só alcança outro `agent-output`/
    página da wiki, nunca fonte humana ou `code-repo`) é decisão humana
    explícita, registrada no log; você não decide sozinho aceitar
    conhecimento sem lastro.

## Eficiência de contexto
Vale em toda operação. Cada item economiza tokens.
- Não releia artefato recém-escrito: Write/Edit já confirma; erra se falhar.
- Nunca cole log ou saída de comando no chat. Saída é para você; reporte só o veredito.
- Zero eco de diff: não repita o conteúdo que acabou de escrever.
- Prosa mínima.
- Arquivo grande: leia só o trecho (offset/limit), não o inteiro. Não releia o mesmo trecho.
- Não repita conteúdo já lido; referencie por `caminho:linha`.
- Não narre plano longo antes de agir.
- Subagente retorna só o bloco pedido, sem preâmbulo nem repetir a instrução.
- `{{WK}} code ...` imprime o corpo completo por padrão; `--quiet` é opt-in
  para condensar a uma linha JSON de resumo (`--verbose` é aceito só como
  no-op de compatibilidade).
- Leia `{{WK}} docs <slug>` uma vez por sessão; não releia o mesmo slug.
- Não use `--help` para varredura de comandos; o primeiro comando de qualquer
  sessão é `{{WK}} doctor --store <s> --repo <r> --engine <e>`.

## Codebase
Não entra por `ingest`: pipeline de 8 estágios (`ingest-codebase`). Produz fontes
para o corpus **e** a árvore SDD (`sdd-contract`). Saída por confiança: 🟢 →
`code-repo`, 🟡 → `agent-output`, 🔴 → pergunta.

## Retrieval
Leia `retrieval` antes de `promote`, `compile`, `lint`, `query`. Proveniência é
coluna: L1/L2/L5 via `index audit` (SQL) e L4 (`L4_realimentacao`, cadeia
`derived_from` sem lastro humano/`code-repo`) via `lint` — os quatro são
100% mecânicos. Só L3 (contradição entre fontes) exige recuperação +
julgamento de conteúdo.

## Templates
`templates/source-frontmatter.yaml` — frontmatter de proveniência no topo de cada
arquivo de `inbox/` e `raw/`. Começa com `---` na primeira linha.

## Wikilinks
Convenção `[[nome-da-pagina]]` para referência cruzada entre páginas do wiki.
Alvo = stem do arquivo (`nome-da-pagina.md`) ou `id` do frontmatter. `lint`
verifica: W1 (link markdown relativo quebrado), W2 (wikilink sem página
alvo), W3 (página órfã — sem link de entrada; `index.md` e
`_lint-report.md` isentos). Doc: `lint`.

## Fluxo de análise (docs/planilhas/XML)
`.docx`/`.xlsx`/`.csv`/`.pdf`: `ingest` grava o original em `raw/assets/` e um
stub em `inbox/` — não há conversão mecânica. Leia o original, discuta/extraia
os pontos-chave, escreva a página de análise, e ingira-a com
`--source-type agent-output --derived-from <id-do-asset>` — o
`--derived-from` grava a linhagem (alimenta `L4_realimentacao` do `lint` e o
gate `realimentacao` do `promote`) — segue o portão normal (`promote` nunca
auto-promove, e `--approve-all` exige `--source-type`+`--topic`+
`--approved-by` juntos) e `compile`. `.xml` de arquitetura (draw.io/XMI) já
sai estruturado (componentes/classes + Mermaid); qualquer outro XML vira
outline + bloco de código. Doc: `ingest`.

## Query → arquivo
Resposta valiosa de consulta vira página do wiki: escreva o arquivo, rode
`{{WK}} ingest <arquivo> --source-type agent-output --origin "resposta a: <pergunta>" --topic <slug>`
(já loga em `log.md`), depois `promote` e `compile`. Sem comando novo.
