# compile

Uso: `compile` | `compile <tópico>`. (Re)gera `wiki/` a partir de `raw/`
**apenas**. Ignora `inbox/`.

```bash
{{WK}} compile [<topico>] --store <store>
```

## O que o comando faz (determinístico, sem LLM)
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
página. Cruzamento de conhecimento acontece na leitura (retrieval), não na
compilação.

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

## Saída
JSON com `paginas` (lista de caminhos gerados, index primeiro), `fontes`
(contagem) e `reindexed`.
