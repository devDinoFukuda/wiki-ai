# lint

Uso: `lint` | `lint <caminho>`. Somente leitura: não promove, não compila, não edita.

## Quem executa o quê neste fluxo
`lint` em si **detecta** — isso é sempre **D**. **Corrigir** o que ele
encontra é uma etapa separada, fora do comando `lint`, e varia por achado.

| Tipo | Passos | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 1 — rodar `lint` (Nível 1 + grafo W1/W2/W3) e ler o relatório | 👤 ou 🤖, indiferente | **Não** |
| **M** | variável — corrigir achados que exigem reescrever/reclassificar texto (ex.: L1, L4) | 🤖 obrigatório | Sim, para gerar a correção |
| **H** | variável — corrigir achados que exigem decisão sem reescrita (ex.: aprovar/rejeitar em `promote`, decidir qual fonte tem razão em L3) | 👤 obrigatório | Não, mas exige julgamento |

**Dá para completar a detecção sem modelo? SIM, 100%** para o Nível 1 e o
grafo de wikilinks — `cmd_lint` (`scripts/wk/cli.py` (função `cmd_lint`)) roda só SQL
(`_audit_index`) e travessia de arquivo (`_audit_wiki_links`), nenhuma
chamada de LLM. **Corrigir os achados já é outra história** — ver tabela
"Detectar vs. corrigir" abaixo.

## Nível 1 — determinístico, sempre (👤 ou 🤖 D)
```bash
{{WK}} lint --store <store>
```
Exemplo concreto:
```bash
{{WK}} lint --store <store>
```
Saída esperada (trecho):
```json
{
  "achados": 2,
  "regras": {"L2_proveniencia_ausente": 0, "wiki_sem_fontes_declaradas": 1, "fonte_orfa": 1},
  "report": "wiki/_lint-report.md",
  "reindexed": false
}
```
Como saber que deu certo: comando roda (exit 0 se `achados: 0`, exit 1 se
houver achados — isso **não é falha de execução**, é o resultado normal de
uma varredura com problemas). Leia `wiki/_lint-report.md` para a lista
item a item. Escreve `wiki/_lint-report.md` (sobrescreve) e devolve JSON com
contagem por regra. Mesmas 5 regras SQL de `{{WK}} index audit` (sem filtro
de caminho e sem gravar relatório, se preferir só o JSON):

| Regra | Pega |
|---|---|
| `L2_proveniencia_ausente` | fonte em `raw/` sem `origin`/`source_type` |
| `L1_canonico_so_de_agente` | página com fontes todas `agent-output` |
| `L5_supersedida_ainda_citada` | página que cita fonte substituída |
| `wiki_sem_fontes_declaradas` | página sem `sources:` |
| `fonte_orfa` | fonte promovida que ninguém cita |

Sai 1 se houver achado.

## Nível 2 — semântico, periódico (🤖 M — geração da consulta e julgamento do resultado)
Não leia o corpus inteiro; gere candidatos por recuperação. Diferente do
Nível 1, aqui não existe um comando `wk` que "rode e decida" sozinho: o
modelo formula a consulta de recuperação, lê os candidatos devolvidos por
`{{WK}} index search` e julga se procede — sem modelo, este nível
simplesmente não roda (não existe SQL equivalente para "parafraseia" ou
"contradiz").

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
origem humana. Não resolva em silêncio — decidir qual fonte "vale" mais é
sempre **👤 H**, mesmo que o modelo tenha detectado a divergência.

## Grafo — wikilinks e órfãs (👤 ou 🤖 D)
Travessia determinística de `wiki/` (não usa o índice SQL): resolve
`[[nome-da-pagina]]` (alvo = stem do arquivo ou `id` do frontmatter) e links
markdown relativos (`[texto](caminho.md)`).

| Regra | Pega |
|---|---|
| `W1_link_markdown_quebrado` | link markdown relativo cujo arquivo alvo não existe |
| `W2_wikilink_sem_alvo` | `[[wikilink]]` sem página correspondente (nem por stem, nem por `id`) |
| `W3_pagina_orfa` | página sem nenhum link de entrada (`index.md` e `_lint-report.md` isentos) |

Achados de W1/W2/W3 também geram exit 1.

## Detectar vs. corrigir — a fronteira D→M/H

| Achado | Detectar | Corrigir |
|---|---|---|
| `L2_proveniencia_ausente` | 👤/🤖 D (`lint`) | 👤 H — decide se a fonte vai para `quarantine.md` definitivamente ou se alguém reingere com `--origin`/`--source-type` corretos (`operations/ingest.md`) |
| `L1_canonico_so_de_agente` | 👤/🤖 D (`lint`) | 👤 H — decide se busca fonte humana adicional ou aceita o risco; não é reescrita de texto |
| `L5_supersedida_ainda_citada` | 👤/🤖 D (`lint`) | 🤖 M — reescrever a página citante para referenciar a versão vigente exige leitura/edição de texto |
| `wiki_sem_fontes_declaradas` | 👤/🤖 D (`lint`) | 👤 H — investigar por que `compile` gerou página sem `sources:` (provável bug ou fonte corrompida), não é correção de conteúdo |
| `fonte_orfa` | 👤/🤖 D (`lint`) | 👤 H — decide se a fonte deve ganhar um link de outra página ou se é lixo a arquivar |
| `W1`/`W2` (link/wikilink quebrado) | 👤/🤖 D (`lint`) | 🤖 M — corrigir o link exige reescrever a página com o alvo certo (ou 👤 H, se for decisão de "não deveria existir esse link") |
| `W3_pagina_orfa` | 👤/🤖 D (`lint`) | 👤 H — decide se cria um link de entrada ou se a página é descartável |
| `L4`/`L3` (Nível 2) | 🤖 M (a busca já exige modelo) | 👤 H — a resolução (bloquear promoção, escolher fonte) é sempre humana |

Em nenhum caso `lint` corrige sozinho — ele é **somente leitura** por
design (linha 3 acima). Qualquer correção é um comando separado
(`ingest`, `promote`, ou edição de página seguida de novo `compile`).

## Saída
`wiki/_lint-report.md` (sobrescreve) + contagem por regra. Ordem: L4, L1, depois
L2/L3/L5, depois grafo (W1/W2/W3). Cada achado: id/caminho, regra, ação sugerida.
