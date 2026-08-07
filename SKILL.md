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
6. Índice sujo bloqueia compile e lint: `status` antes; sujo → `reindex`.
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
    subagente; o orquestrador nunca gera conteúdo SDD diretamente. `run-stage`
    informa `fanout_required` (nº de batches) e `next_action`. Cada subagente
    grava o próprio `output` e devolve recibo de 3 linhas
    (`ARQUIVO:`/`BLOCOS:`/`BYTES:`). `merge-agent-output` exige `--agent`
    distinto por batch; valor genérico (`main`, `orquestrador`, `self`,
    `principal`) é recusado.

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
coluna: L1/L2/L5 via `audit` (SQL); L3/L4 por recuperação + julgamento.

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
`--source-type agent-output` — segue o portão normal (`promote` nunca
auto-promove) e `compile`. `.xml` de arquitetura (draw.io/XMI) já sai
estruturado (componentes/classes + Mermaid); qualquer outro XML vira outline +
bloco de código. Doc: `ingest`.

## Query → arquivo
Resposta valiosa de consulta vira página do wiki: escreva o arquivo, rode
`{{WK}} ingest <arquivo> --source-type agent-output --origin "resposta a: <pergunta>" --topic <slug>`
(já loga em `log.md`), depois `promote` e `compile`. Sem comando novo.
