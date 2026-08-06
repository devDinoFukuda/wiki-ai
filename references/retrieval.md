# retrieval

```bash
{{WK}} index <comando>
```

| Comando | Para quê |
|---|---|
| `reindex` | indexa o que mudou, poda o que sumiu |
| `search` | híbrido BM25 + vetorial, fusão RRF |
| `get` | documento/trecho por `#docid` ou caminho |
| `audit` | L1/L2/L5 em SQL |
| `status` | saúde e frescor (sai 1 se sujo) |

## Query document (ordem importa; 1ª sub-query pesa 2x)
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
