# lint

Uso: `lint` | `lint <caminho>`. Somente leitura: não promove, não compila, não edita.

## Nível 1 — determinístico, sempre
```bash
{{WK}} lint [<caminho>] --store <store>
```
Escreve `wiki/_lint-report.md` (sobrescreve) e devolve JSON com contagem por
regra. Mesmas 5 regras SQL de `{{WK}} index audit` (sem filtro de caminho e
sem gravar relatório, se preferir só o JSON):

| Regra | Pega |
|---|---|
| `L2_proveniencia_ausente` | fonte em `raw/` sem `origin`/`source_type` |
| `L1_canonico_so_de_agente` | página com fontes todas `agent-output` |
| `L5_supersedida_ainda_citada` | página que cita fonte substituída |
| `wiki_sem_fontes_declaradas` | página sem `sources:` |
| `fonte_orfa` | fonte promovida que ninguém cita |

Sai 1 se houver achado.

## Nível 2 — semântico, periódico
Não leia o corpus inteiro; gere candidatos por recuperação.

L4 — realimentação:
```
intent: agent-output que parafraseia pagina derivada de agent-output
vec: <tese do candidato, uma frase>
```
`--filter source_type=agent-output`. Profundidade > 1 → BLOQUEIA promoção.

L3 — contradição (por afirmação-chave):
```
intent: fontes que afirmam algo diferente sobre este ponto
vec: <a afirmação>
```
`-c raw --filter promoted=1`. Divergência → registre, prefira maior `confidence` /
origem humana. Não resolva em silêncio.

## Grafo — wikilinks e órfãs
Travessia determinística de `wiki/` (não usa o índice SQL): resolve
`[[nome-da-pagina]]` (alvo = stem do arquivo ou `id` do frontmatter) e links
markdown relativos (`[texto](caminho.md)`).

| Regra | Pega |
|---|---|
| `W1_link_markdown_quebrado` | link markdown relativo cujo arquivo alvo não existe |
| `W2_wikilink_sem_alvo` | `[[wikilink]]` sem página correspondente (nem por stem, nem por `id`) |
| `W3_pagina_orfa` | página sem nenhum link de entrada (`index.md` e `_lint-report.md` isentos) |

Achados de W1/W2/W3 também geram exit 1.

## Saída
`wiki/_lint-report.md` (sobrescreve) + contagem por regra. Ordem: L4, L1, depois
L2/L3/L5, depois grafo (W1/W2/W3). Cada achado: id/caminho, regra, ação sugerida.
