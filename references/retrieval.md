# retrieval

## Quem executa o quê neste fluxo
| Tipo | Passos | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 4 — `reindex`, `search`, `get`, `audit`/`status` | 👤 ou 🤖, indiferente | **Não** |
| **M** | 2 — formular a query document (`intent`/`lex`/`vec`/`hyde`); sintetizar uma resposta a partir dos resultados | 🤖 obrigatório | Sim |
| **H** | 1 — aprovar a resposta virada página de wiki, em `promote` | 👤 obrigatório | Não, mas exige julgamento |

**Dá para completar sem modelo?** Rodar `search`/`get`/`reindex`/`audit`
sozinho: SIM — todos são busca/índice mecânico (BM25 + vetorial com fusão
RRF, ou SQL puro), prova abaixo por comando. **Obter uma resposta em
prosa a partir dos resultados: NÃO** — o índice devolve documentos/trechos
ranqueados, nunca uma resposta sintetizada; só um 🤖 lê os resultados e
escreve a resposta.

```bash
{{WK}} index <comando>
```

| Comando | Para quê | Tipo | Provado em |
|---|---|---|---|
| `reindex` | indexa o que mudou, poda o que sumiu | **D** | `scripts/sbindex/cli.py`, função `cmd_reindex` |
| `search` | híbrido BM25 + vetorial, fusão RRF | **D** — a *busca* em si; formular a query é **M** | `scripts/sbindex/cli.py`, função `cmd_search` |
| `get` | documento/trecho por `#docid` ou caminho | **D** | `scripts/sbindex/cli.py`, função `cmd_get` |
| `audit` | L1/L2/L5 em SQL | **D** | `scripts/sbindex/cli.py`, função `cmd_audit` |
| `status` | saúde e frescor (sai 1 se sujo) | **D** | `scripts/sbindex/cli.py`, função `cmd_status` |

`search` usa embeddings (Azure OpenAI ou modo léxico) para `vec:`/`hyde:`,
mas isso é geração determinística de vetor a partir de texto fixo — não é
julgamento de LLM, é uma função matemática do mesmo texto para o mesmo
vetor sempre. A parte que exige modelo é escolher **o que escrever** em
`intent:`/`lex:`/`vec:`/`hyde:` e depois **interpretar** os resultados —
isso sim é 🤖 M.

## Forçar modo léxico (`--lex-only`) — 👤 ou 🤖 **D**
`reindex --lex-only` é um atalho explícito para não gerar embeddings nesta
rodada — sem ele, `reindex` tenta embeddings automaticamente sempre que
`AZURE_OPENAI_*` está configurado, e cai em modo léxico silenciosamente (com
aviso) quando não está. `--lex-only` torna a escolha explícita mesmo com
`AZURE_OPENAI_*` configurado — útil para reindexar rápido sem custo/latência
de embedding, ou para testar o comportamento de busca só-BM25:
```bash
{{WK}} index reindex --store <store> --lex-only
```
Saída esperada: `{"documents": N, "changed": N, "pruned": 0, "embedded": 0,
"embedder": "none (lex-only)", "exact_tokens": false}` — sem o campo
`warning` que apareceria se embeddings estivessem apenas ausentes por falta
de configuração (`"embeddings não configurados: índice em modo LÉXICO ...
Defina AZURE_OPENAI_* ou use --lex-only"`). Como saber que deu certo:
`embedder` começa com `"none"`.

O que muda na qualidade da busca em modo léxico: `vec:`/`hyde:` na query
document deixam de funcionar (`search` recusa com `exit 2` e a mensagem
`"vec/hyde exigem embeddings; índice em modo léxico"`) — só `lex:` (BM25)
continua disponível. Buscas que dependem de sinônimo/paráfrase (a fonte usa
palavra diferente da consulta) perdem recall; buscas por termo exato, ID,
sigla ou nome continuam funcionando normalmente, já que são o caso de uso
de `lex:`.

## Query document (ordem importa; 1ª sub-query pesa 2x) — 🤖 M para formular
```
intent: o que procura
lex: "termo exato" -excluido
vec: descrição em linguagem natural
hyde: o parágrafo que espera encontrar
```
- `lex` → BM25: termo exato, símbolo, sigla, ID, nome. Aceita `"frase"`, `-negação`, `OR`. Insensível a acento.
- `vec` → vetorial: quando a fonte usa outras palavras.
- `hyde` → escreva a resposta hipotética, não a pergunta.

Modo léxico (sem embeddings): `lex` funciona; `vec`/`hyde` dão erro.

Exemplo concreto — rodar a busca é **D**, 👤 pode digitar isto sozinho
mesmo sem entender o resultado (mas interpretá-lo já é M). O `--format`
default é `text` (feito para leitura no terminal); para consumir por
script/agente, sempre passe `--format json` — sem essa flag a saída **não**
é JSON:
```bash
printf 'intent: politica de reembolso de despesas\nlex: "reembolso" OR "despesa"\n' | {{WK}} index search -c raw -n 5 --format json
```
Saída real (capturada rodando `sbindex.cli search --format json` contra um
store de teste, formato idêntico ao que `{{WK}} index search` produz —
note a chave de topo `results`, não uma lista solta, e o campo `docid`
(com `#` já embutido), não `id`):
```json
{
  "intent": "politica de reembolso de despesas",
  "searches": [
    {"type": "lex", "query": "\"reembolso\" OR \"despesa\"", "weight": 2.0}
  ],
  "filters": {"collection": "raw"},
  "results": [
    {
      "docid": "#c0abae",
      "path": "store/raw/human-doc/politica-reembolso.md",
      "collection": "raw",
      "heading": "",
      "source_type": "human-doc",
      "confidence": "reviewed",
      "score": 0.03279,
      "text": "# Politica de reembolso de despesas\n\nToda despesa de viagem precisa de nota fiscal e aprovacao do gestor direto\nantes do reembolso ser processado pelo financeiro."
    }
  ]
}
```
Como saber que deu certo: `results` não vazio, com `score` decrescente
entre itens. Campos disponíveis por resultado: `docid`, `path`,
`collection`, `heading`, `source_type`, `confidence`, `score`, `text`
(corpo truncado em 400 caracteres; use `--full` para o trecho inteiro). Se
vier `results: []`: refine `lex`/`vec` ou confirme que o índice está fresco
(`{{WK}} index status`; sujo → `{{WK}} index reindex --store <store>`).

Stdin ou argumento:
```bash
printf 'intent: ...\nlex: ...\n' | {{WK}} index search -n 10
```

## Filtros (chave desconhecida = erro)
```bash
--filter source_type=agent-output
--filter confidence=raw,reviewed     # vírgula = OR
--filter promoted=1
-c wiki
```
Válidos: `collection`, `source_type`, `confidence`, `topic`, `promoted`, `supersedes`, `source_id`.

## Receitas
promote — dedup:
```
intent: detectar duplicata degradada de fonte ja canonica
lex: <identificadores raros>
vec: <tese central, uma frase>
```
`-c raw --filter promoted=1`

compile — dois retrievals:
```
intent: paginas existentes afetadas por esta fonte
vec: <assunto>
lex: <entidades e simbolos>
```
`-c wiki`
```
intent: fontes relacionadas para cruzar
vec: <assunto>
hyde: <parágrafo esperado>
```
`-c raw --filter promoted=1`

lint L4:
```
intent: agent-output que parafraseia pagina derivada de agent-output
vec: <tese do candidato>
```
`--filter source_type=agent-output`

lint L3: por afirmação-chave, `vec:` com a afirmação, `-c raw --filter promoted=1`.

## Query → arquivo → wiki: a cadeia completa M → M → H
Uma consulta valiosa não termina no terminal — ela pode virar página
permanente da wiki. Isso é uma cadeia de 3 passos, dois deles obrigando
modelo e o último obrigando humano; nenhum comando único faz tudo:

1. **🤖 M** — rodar `search`/`get` (comandos acima, **D**) e depois
   sintetizar a resposta em prosa a partir dos resultados. Sem modelo aqui
   não existe resposta — só uma lista de trechos ranqueados.
2. **🤖 M** — escrever essa resposta num arquivo `.md` e ingerir como
   `agent-output`:
   ```bash
   {{WK}} ingest resposta-reembolso.md --source-type agent-output --origin "resposta a: qual a política de reembolso de despesas?" --topic financeiro/politicas
   ```
   Já loga em `log.md` (prova: função `cmd_ingest` em `scripts/wk/cli.py`); a
   ingestão em si é **D** mecanicamente, mas só existe porque o passo 1
   foi feito por um modelo — por isso a cadeia inteira é tratada como M até
   aqui.
3. **👤 H** — aprovar em `promote` (nunca auto-promove: `agent-output`
   exige `--approve`, ver `operations/promote.md`) e então `compile`
   (**D**). Sem essa aprovação humana explícita, a resposta fica presa em
   `inbox/` para sempre — nunca vira página canônica sozinha.

Sem comando novo: é a mesma sequência `ingest` → `promote` → `compile` de
sempre, só que a fonte da vez é uma resposta de retrieval em vez de uma
transcrição ou asset.
