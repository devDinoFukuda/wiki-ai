# promote

Uso: `promote` | `promote <id-ou-caminho>`. Portão `inbox/`→`raw/` (schema §3).
Só rode quando o usuário pedir. Não compile aqui.

```bash
{{WK}} promote [<id-ou-caminho>] --store <store>
```

Sem alvo posicional, varre `inbox/**/*.md` inteiro. Com alvo, resolve por
caminho dentro de `inbox/` ou por `id` no frontmatter.

## Aprovação (obrigatória para tudo exceto `code-repo`)
Não existe aprovação por linguagem natural no chat — o portão só reconhece as
flags abaixo:

```bash
{{WK}} promote --approve <id-ou-caminho> --approved-by "<quem aprovou>" --store <store>
{{WK}} promote --approve-all --source-type <tipo> --approved-by "<quem aprovou>" --store <store>
```

`--approve-all` exige `--source-type`; sem ele, o comando recusa antes de
tocar em qualquer arquivo. `--approved-by` default é `"humano"`. Aprovação
amplia o escopo — mesmo um item fora do `target` original é promovido se
aprovado explicitamente.

## Regra por `source_type`
- `code-repo` → auto-promove sempre, sem `--approve`. `confidence: reviewed`,
  `promoted_by: wiki-ai`.
- `human-transcript` / `human-doc` / `web-clip` → nunca auto-promove. Só com
  `--approve`/`--approve-all` explícito. `confidence: reviewed`,
  `promoted_by: <--approved-by>`.
- `agent-output` → nunca auto-promove, **mesmo aprovado**. Aprovação move o
  arquivo, mas grava `confidence: unverified` (nunca `reviewed`).
- Sem `origin`/`source_type` válido (L2) → direto para `quarantine.md`, nem
  entra na decisão de aprovação.

## Passos (o que o comando faz)
1. Lista candidatos, lê frontmatter. Gap de proveniência (L2) →
   `quarantine.md` com motivo, pula.
2. Aplica a regra acima por `source_type` + aprovação.
3. Promovido: atualiza frontmatter (`promoted: true`, `promoted_by`,
   `promoted_at`, `confidence`), move para a subpasta de `raw/`
   correspondente, remove de `inbox/`, append em `log.md`.
4. Roda `reindex` sozinho se promoveu algo (reporta `reindexed`/`reindex_error`).

## Saída
JSON com `promovidos` (id, path, source_type, confidence), `decisao_humana`
(id, path, source_type, motivo), `quarentena` (id, path, motivo) e
`reindexed`. Em dúvida, não use `--approve`.
