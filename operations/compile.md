# compile

Uso: `compile` | `compile <tópico>`. (Re)gera `wiki/` a partir de `raw/`
**apenas**. Ignora `inbox/`.

## Quem executa o quê neste fluxo
| Tipo | Passos | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 1 — rodar `compile` inteiro | 👤 ou 🤖, indiferente | **Não** |
| **M** | 0 | — | — |
| **H** | 0 | — | — |

**Dá para completar sem modelo? SIM, 100%.** `cmd_compile`
(`scripts/wk/cli.py` (função `cmd_compile`) em diante) lê `raw/` e escreve `wiki/**/*.md` por
template fixo — nenhuma chamada de LLM, nenhuma síntese, nenhuma decisão de
conteúdo. 👤 humano roda o comando sozinho sobre conteúdo **já promovido**
(ver `operations/promote.md` — a etapa que exige humano é lá, não aqui).

```bash
{{WK}} compile [<topico>] --store <store> [--allow-unverified]
```

Exemplo concreto — compilar só o tópico `financeiro/custos-q3`:
```bash
{{WK}} compile financeiro/custos-q3 --store <store>
```
Saída esperada (trecho):
```json
{
  "paginas": ["wiki/index.md", "wiki/financeiro/custos-q3/overview.md", "wiki/financeiro/custos-q3/human-doc/sb-ingest-...md"],
  "fontes": 3,
  "reindexed": true
}
```
Como saber que deu certo: `reindexed: true` e o número em `fontes` bate com
o que você esperava promover para aquele tópico. Se `fontes: 0`, nada foi
promovido ainda nesse tópico — rode `promote` primeiro (`operations/promote.md`).

## O que o comando faz (determinístico, sem LLM) — tudo **D**
1. Lê todo documento em `raw/` com `promoted: true` (filtra por `topic` se
   `<topico>` foi passado).
2. Escreve **uma página por documento**, sem sintetizar nem cruzar fontes: o
   corpo da página é o corpo do documento promovido, verbatim, mais um
   cabeçalho com `topic`, `source_type`, `confidence`, `origin` e o caminho da
   fonte em `raw/`. Caminho: `wiki/<topic>/<source_type>/<id>.md`.
3. Escreve `wiki/index.md` **agrupado por `topic`**: uma seção por tópico
   (contagem de páginas do tópico no cabeçalho da seção) e, por página, link +
   id + `source_type` + data de atualização.
4. Roda `reindex` sozinho ao final — não precisa rodar manualmente depois.

Não existe merge de múltiplas fontes numa página, não existe "sintetize",
"cruze com fontes relacionadas" ou "chunker por `##`": isso descreve uma
versão anterior do compile. A granularidade agora é 1 fonte promovida = 1
página. Cruzamento de conhecimento acontece na leitura (retrieval, sempre
🤖 — ver `references/retrieval.md`), não na compilação.

## Frontmatter da página gerada
```yaml
id: wiki-<topic-slug>-<source_type-slug>-<id-slug>
topic: <topic>
sources: ["<id da fonte>"]
```
Isso é o que torna L1/L5 verificáveis por SQL (`audit`).

## Não faça
- Não edite `raw/` nem páginas de `wiki/` à mão — elas são regeneradas a cada
  `compile` e qualquer edição manual some na próxima rodada.
- Não promova aqui.
- Não pule `compile` esperando que `promote` já tenha atualizado a wiki —
  `promote` só move para `raw/`; `compile` é quem gera as páginas.
- Não invoque um modelo "para revisar" o conteúdo aqui: se o conteúdo precisa
  de revisão de um LLM, isso acontece antes, em `ingest`/`promote`; `compile`
  é só cópia estruturada.

## Saída
JSON com `paginas` (lista de caminhos gerados, index primeiro), `fontes`
(contagem) e `reindexed`.

## Página de overview por tópico — também **D**
Além de "1 fonte promovida = 1 página" (acima, inalterado), `compile` gera
`wiki/<topic>/overview.md` (`_build_topic_overview`) para todo `topic` que
tenha ao menos uma fonte publicada por `wk publish` (identificada pelo
`origin` no formato `"codescan <repo> — <caminho>"`; tópicos 100% manuais —
só transcrição/doc ingerido à mão — não ganham overview). Seções, nesta
ordem, com corpo completo salvo indicação contrária:
- **Arquitetura** — `sdd/architecture.md` + os 3 C4 (completo);
- **Decisões** — tabela de ADRs (nº+título / status / decisão em 1 linha,
  linkando o ADR completo);
- **Análise de código** — **índice** de `sdd/code-analysis.md` (contagem +
  nomes de módulo via headings `### Módulo:` + link para o artefato
  completo, nunca o corpo inteiro);
- **Edge cases** — `sdd/specs/*/edge-cases.md` por unit (completo);
- **Diagramas** — `sdd/flowcharts/_index.md` embutido; demais flowcharts,
  `sdd/sequences/*.md` e o asset `coupling.html` linkados;
- **Confiança** — `confidence-report.md`/`gaps.md` embutidos;
  `confirmed.md`/`inferred.md` linkados com contagem de linhas;
- **Lacunas de síntese** — só aparece se faltar artefato esperado.

Artefato ausente pula a subseção (registrada em "Lacunas de síntese"), não
derruba a overview inteira. `wiki/index.md` linka cada overview em
destaque, na seção "Visão geral por tópico", antes da lista de páginas.
Overview é material **adicional** — não substitui as páginas individuais
por artefato SDD (`sdd/architecture.md`, `sdd/adrs/*.md` etc. continuam 1
página cada). O conteúdo dessas seções veio de `🤖 M` em algum passo
anterior do pipeline `ingest-codebase` (estágios `architecture`/`specs`/
`synth`) — `compile` só copia e organiza, não gera nada novo aqui.

## Gate de `verify` — 👤 H para destravar
`compile` recusa (`exit 3`) tópicos cujo workdir de origem tem
`state.json.stages.verify.status == "failed"` — escopado pelo `topic` do
comando (ou todos os tópicos, se não filtrar). Isso é a única bifurcação não
puramente mecânica do comando, e mesmo assim não exige modelo: exige uma
decisão humana. Override consciente:
```bash
{{WK}} compile financeiro/custos-q3 --store <store> --allow-unverified
```
(registrado em `log.md` e no JSON de saída, campo `verify_override`).
Detalhe do gate: `operations/promote.md`.
