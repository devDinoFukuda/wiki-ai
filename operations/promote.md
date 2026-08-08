# promote

Uso: `promote` | `promote <id-ou-caminho>`. Portão `inbox/`→`raw/` (schema §3).
Só rode quando o usuário pedir. Não compile aqui.

## Quem executa o quê neste fluxo
| Tipo | Passos | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 2 — promover `code-repo` (auto-promoção); rejeitar para `quarantine.md` por proveniência ausente (L2, automático, sem decisão humana) | 👤 ou 🤖, indiferente | Não |
| **H** | 2 — aprovar `human-transcript`/`human-doc`/`web-clip`/`agent-output` (`--approve`/`--approve-all`); decidir `--allow-unverified` | 👤 obrigatório | Não, mas exige julgamento e responsabilidade |
| **M** | 0 | — | Não há nenhum passo que exija modelo neste comando |

**Dá para completar sem modelo?** SIM, sempre — a função `cmd_promote` em
`scripts/wk/cli.py` é 100% regra determinística (`if source_type ==
"code-repo": ...` na mesma função, bloco da regra `code-repo`); nenhuma
chamada de LLM existe no caminho. O que **não** dá para pular é o humano:
promover qualquer
coisa além de `code-repo` exige uma pessoa nomeada em `--approved-by`
decidindo conscientemente — essa decisão é o único "não determinístico"
do fluxo, e é humana, não de modelo.

```bash
{{WK}} promote [<id-ou-caminho>] --store <store> [--allow-unverified]
```

Sem alvo posicional, varre `inbox/**/*.md` inteiro. Com alvo, resolve por
caminho dentro de `inbox/` ou por `id` no frontmatter.

## Risco de promover sem revisar
`agent-output` é sempre saída de um modelo LLM, nunca verificada
mecanicamente quanto ao conteúdo (só quanto à forma). Promover sem ler o
arquivo primeiro coloca texto potencialmente incorreto, inventado ou
desatualizado no corpus canônico (`raw/`) — de onde `compile` e `docx`
alimentam a wiki e o SharePoint sem nenhuma outra checagem de conteúdo.
Reverter depois exige achar e "despromover" manualmente; não há comando de
rollback. **Leia o arquivo antes de aprovar.**

## Passo D — auto-promoção de `code-repo` (👤 ou 🤖, sem aprovação)
Saída determinística de `wk code` (inventário, dependências, acoplamento)
promove sozinha, sem flag nenhuma:

```bash
{{WK}} promote --store <store>
```

Saída esperada (trecho, com um item `code-repo` na fila):
```json
{
  "promovidos": [{"id": "sb-publish-meurepo-sdd-inventory", "path": "raw/code-notes/...", "source_type": "code-repo", "confidence": "reviewed"}],
  "decisao_humana": [],
  "quarentena": [],
  "bloqueados_verify": [],
  "reindexed": true
}
```
Como saber que deu certo: item aparece em `promovidos`, nunca em
`decisao_humana`. Se um item `code-repo` aparecer em `bloqueados_verify` em
vez de `promovidos`: leia o campo `acao` desse item — normalmente aponta
`wk code --repo <repo> verify --artifact <workdir>/sdd/confirmed.md` (ver
"Gate de `verify`" abaixo).

## Passo H — aprovação (obrigatória para tudo exceto `code-repo`)
Não existe aprovação por linguagem natural no chat — o portão só reconhece as
flags abaixo. Substitua `<id-ou-caminho>` e `<quem aprovou>` pelos valores
reais; use o e-mail ou nome de quem está de fato assumindo a responsabilidade
pela promoção.

```bash
{{WK}} promote --approve inbox/agent-output/sb-ingest-analise-<hash8>.md --approved-by "dev.dinofukuda@gmail.com" --store <store>
{{WK}} promote --approve-all --source-type human-transcript --approved-by "dev.dinofukuda@gmail.com" --store <store>
```

`--approve-all` exige `--source-type`; sem ele, o comando recusa antes de
tocar em qualquer arquivo (prova: função `cmd_promote` em
`scripts/wk/cli.py`, bloco de validação de `--approve-all`/`--source-type`).
`--approved-by` default é `"humano"` — sempre prefira passar um nome/e-mail
real, o default não identifica ninguém. Aprovação amplia o escopo — mesmo um
item fora do `target` original é promovido se aprovado explicitamente.

Como saber que deu certo: o `id` aprovado aparece em `promovidos` com
`confidence: "unverified"` (se era `agent-output`) ou `"reviewed"` (demais
tipos) e `promoted_by` igual ao valor passado em `--approved-by` — confira
que bate com quem de fato decidiu.

## Regra por `source_type`
- `code-repo` → auto-promove sempre, sem `--approve`. `confidence: reviewed`,
  `promoted_by: wiki-ai`. **D.**
- `human-transcript` / `human-doc` / `web-clip` → nunca auto-promove. Só com
  `--approve`/`--approve-all` explícito. `confidence: reviewed`,
  `promoted_by: <--approved-by>`. **H.**
- `agent-output` → nunca auto-promove, **mesmo aprovado**. Aprovação move o
  arquivo, mas grava `confidence: unverified` (nunca `reviewed`) — o
  registro de proveniência deixa rastro permanente de que a origem foi um
  modelo, mesmo depois de promovido. **H.**
- Sem `origin`/`source_type` válido (L2) → direto para `quarantine.md`, nem
  entra na decisão de aprovação. **D** (rejeição automática, sem decisão
  humana envolvida).

## Gate de `verify` (codebase) — 👤 H para destravar
Para fontes de origem `codescan` (`origin` no formato `"codescan <repo> —
<caminho>"`), `promote` bloqueia **por item**, escopado pelo `topic` de
cada fonte: se o workdir de codescan de origem tem
`state.json.stages.verify.status == "failed"`, o item some da lista
de promoção e entra no bloco `bloqueados_verify` do JSON de saída, cada item
já trazendo `acao` com o comando exato de correção (prova: função
`cmd_promote` em `scripts/wk/cli.py`, bloco do gate de `verify`). Fontes de
outros tópicos, no mesmo lote, não
são penalizadas — o escopo é sempre o `topic` da própria fonte. O bloqueio
cobre também os artefatos `code-repo` do tópico
(`inventory.md`/`dependencies.md`/`coupling.md`/`coupling.html`), não só
`agent-output`.

Para promover mesmo assim — decisão humana explícita, registrada:
```bash
{{WK}} promote --store <store> --allow-unverified
```
Isso é **H de alto risco**: força para dentro do corpus canônico algo que a
verificação mecânica já reprovou (citação `arquivo:linha` inválida ou
ausente). Fica gravado em `log.md` e ecoado no JSON de saída
(`verify_override`) — decisão auditável, não anônima. Fontes que não vêm de
`codescan` (transcrição, doc, clip) não passam por esse gate — seguem só a
"Regra por `source_type`" acima.

## Passos (o que o comando faz) — todos **D**
1. Lista candidatos, lê frontmatter. Gap de proveniência (L2) →
   `quarantine.md` com motivo, pula.
2. Aplica a regra acima por `source_type` + aprovação.
3. Promovido: atualiza frontmatter (`promoted: true`, `promoted_by`,
   `promoted_at`, `confidence`), move para a subpasta de `raw/`
   correspondente, remove de `inbox/`, append em `log.md`.
4. Roda `reindex` sozinho se promoveu algo (reporta `reindexed`/`reindex_error`).

Prova: `cmd_promote` inteiro (`scripts/wk/cli.py` (função `cmd_promote`)) não importa nem
chama nenhum cliente de LLM — é leitura de frontmatter, comparação de
string e I/O de arquivo.

## Saída
JSON com `promovidos` (id, path, source_type, confidence), `decisao_humana`
(id, path, source_type, motivo), `quarentena` (id, path, motivo),
`bloqueados_verify` (itens retidos pelo gate de `verify` — sempre presente,
lista vazia quando nada foi bloqueado, cada item com `acao`) e `reindexed`.
Com `--allow-unverified`, soma-se `verify_override` (itens promovidos
apesar do `verify` falho). Em dúvida, não use `--approve` nem
`--allow-unverified` — pare e pergunte ao humano responsável.
