# docx

Uso: `docx` | `docx <tópico>`. Gera `wiki-docx/` (DOCX) a partir de `raw/`
promovido, para publicação em bibliotecas SharePoint consumidas pelo
Copilot Studio.

## Quem executa o quê neste fluxo
| Tipo | Passos | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 1 — rodar `docx` inteiro | 👤 ou 🤖, indiferente | **Não** |
| **M** | 0 | — | — |
| **H** | 0 | — | — |

**Dá para completar sem modelo? SIM, 100%.** `cmd_docx`
(`scripts/wk/cli.py` (função `cmd_docx`) em diante) lê `raw/` (a mesma leitura de
`compile`) e escreve `.docx` via `docxgen`/OOXML determinístico — nenhuma
chamada de LLM. 👤 humano exporta a wiki para `.docx` sozinho, sobre
conteúdo **já promovido** (a etapa que precisou de humano/modelo já
aconteceu em `ingest`/`promote` — ver esses docs).

```bash
{{WK}} docx [<topico>] [--store <path>] [--out-dir wiki-docx] [--no-prune] [--allow-unverified]
```

Exemplo concreto:
```bash
{{WK}} docx financeiro/custos-q3 --store <store>
```
Saída esperada (trecho):
```json
{
  "documentos": [{"path": "wiki-docx/financeiro/custos-q3/human-doc/sb-ingest-...docx", "id": "sb-ingest-...", "source_type": "human-doc", "avisos": []}],
  "fontes": 3,
  "removidos": [],
  "pulados": []
}
```
Como saber que deu certo: `pulados` vazio e a contagem de `documentos` bate
com `fontes`. Se algum item aparecer em `pulados`, leia o `motivo` desse
item — é a única forma de diagnóstico deste comando (não há campo `acao`
aqui). Exit `1` só ocorre se algum `.docx` falhar ao ser escrito.

## O que o comando faz (determinístico, sem LLM) — tudo **D**
1. Lê toda fonte em `raw/` com `promoted: true` (filtra por `topic` se
   `<topico>` for passado) — a mesma leitura que `wk compile` faz. **Não
   exige `wk compile` prévio**: lê `raw/` diretamente, não `wiki/`.
2. Converte cada fonte promovida em **um `.docx`**: markdown → OOXML, com
   título e resumo derivados heuristicamente do corpo, mais uma seção de
   proveniência (`topic`, `source_type`, `confidence`, `origin`,
   `captured_at`, fonte em `raw/`). Caminho:
   `wiki-docx/<topic>/<source_type>/<arquivo>.docx`.
3. Poda `.docx` órfão: todo arquivo em `wiki-docx/` que não corresponda a
   uma fonte gerada **nesta execução** é removido (`--no-prune` desliga).
4. Roda sozinho, sem depender de `reindex`; grava uma linha em `log.md` ao
   final, no formato:
   ```
   ## [{timestamp}] docx | {n_documentos} documentos | {n_fontes} fontes | {n_removidos} removidos
   ```

Não existe merge de múltiplas fontes num único `.docx` **por fonte**: a
granularidade é a mesma de `compile` — 1 fonte promovida = 1 documento
gerado. Exceção deliberada: o agregador `wiki-docx/<topic>/index.docx`
("`.docx` agregador por tópico" abaixo), que consolida a mesma overview do
`compile` — não é gerado a partir de fontes individuais, mas do texto da
overview. `coupling.html` nunca é convertido — pulado explicitamente
(`ignorados_asset_html`).

## O que o comando NÃO faz
- **Não toca `wiki/`** — árvore independente, gerada só a partir de `raw/`.
  Rodar `docx` antes, depois, ou sem nunca rodar `compile` não altera
  `wiki/` de nenhuma forma.
- **Não publica nem sobe nada para o SharePoint.** Produz arquivos em disco
  local, dentro do store; o transporte é responsabilidade de outra etapa,
  fora do escopo deste comando (ver seção abaixo). Esse transporte, quando
  existir, é sempre **👤 H** — nenhum agente deste projeto sobe arquivo
  para o SharePoint.
- Não sintetiza nem cruza fontes — cada `.docx` é o corpo de uma única
  fonte promovida, verbatim, convertido para OOXML.
- Não chama nenhum modelo para "melhorar" o resumo ou o título — ambos são
  derivados por heurística de texto (primeira frase/heading), não por LLM.

## Pré-condição
Fontes promovidas em `raw/` — as mesmas que `compile` consome. **Não requer
`wk compile` prévio nem que `wiki/` exista.** Sem fontes promovidas, o
comando roda e sai `0` com a árvore de saída vazia.

## Saída
Árvore: `wiki-docx/<topic>/<source_type>/<arquivo>.docx`.

JSON:
```json
{
  "documentos": [{"path": "wiki-docx/...", "id": "...", "source_type": "...", "avisos": []}],
  "fontes": 12,
  "removidos": [{"path": "wiki-docx/...", "motivo": "orfao"}],
  "pulados": [{"id": "...", "motivo": "..."}]
}
```
Exit `1` só se algum `.docx` falhar ao ser escrito (listado em `pulados`).

## Idempotência
Rodar `docx` duas vezes seguidas sobre o mesmo `raw/` produz **a mesma
árvore**: mesmos caminhos, mesmo conteúdo byte a byte. Nomes de arquivo
nunca carregam sufixo de execução — **nunca aparece `-2.docx`**. Uma
colisão real de nome (dois `id`s diferentes que sanitizam para o mesmo
stem) é resolvida por sufixo determinístico derivado do próprio `id` (8
caracteres de `sha1(id)`), nunca por contador dependente do estado do
disco ou da ordem de processamento.

## Poda de órfãos
Ao final de cada execução, o comando varre `wiki-docx/` e remove todo
`.docx` cujo caminho não esteja entre os gerados **nesta execução** — caso
típico: fonte removida, renomeada ou retopicada em `raw/` desde a última
rodada. Diretórios que ficam vazios após a poda também são removidos. Cada
arquivo removido entra em `"removidos"` no JSON de saída. `--no-prune`
desliga a varredura inteira: órfãos permanecem no disco e `"removidos"`
sai vazio.

## Transporte ao SharePoint: fora de escopo desta v1 — sempre 👤 H se acontecer
Este comando **não** sobe nada para o SharePoint — não há, no projeto,
nenhuma chamada de rede, Graph API/REST ou dependência de biblioteca de
upload. `wk docx` só escreve `wiki-docx/**/*.docx` em disco local; levar
esses arquivos até a biblioteca de documentos do SharePoint é uma etapa
operacional separada, deliberadamente fora do escopo v1, e é **sempre
humana** quando acontece — não existe "modelo que publica no SharePoint"
neste projeto. Alternativa até existir automação própria: sincronização de
pasta via cliente OneDrive/SharePoint sync apontado para `wiki-docx/`, ou
upload manual periódico pela interface web do SharePoint.

## Por que este comando existe
O caminho de ingestão adotado pelo Copilot Studio para este corpus é o
conector SharePoint (`Add knowledge → SharePoint`), que **não aceita
`.md`** como formato de biblioteca de documentos — só DOC/DOCX, PPT/PPTX e
PDF. `wk compile` produz `wiki/**/*.md`, que segue sendo a fonte-verdade
legível por humanos e por outras ferramentas, mas não é publicável direto
nessa biblioteca. `wk docx` existe só para cobrir essa lacuna: converte as
mesmas fontes promovidas para o único formato de documento que o conector
aceita. Detalhes e evidência oficial:
[../references/copilot-studio-sharepoint-kb.md](../references/copilot-studio-sharepoint-kb.md)
(§1 "Decisão preliminar: qual caminho de ingestão"; §2 "Formatos", tabela
2.1 — linha `.md`: aceito **só** no caminho de upload direto de arquivo,
ausente dos dois caminhos que envolvem SharePoint).

## Rotas alternativas avaliadas
- **Lista SharePoint** — a KB recomenda literalmente lista, não documento,
  para dados tabulares (matrizes, métricas por classe). Avaliada e
  descartada **só para esta v1**: exige API de criação de lista com
  colunas tipadas, incompatível com geração de arquivo estático offline
  por `wk.pyz`. Registrada como **trabalho futuro**.
- **PDF** — descartado: geração from-scratch sem biblioteca externa tem
  custo desproporcional ao ganho; DOCX já é editável in-place no
  SharePoint, PDF exigiria regeração completa a cada revisão.
- **Página moderna SharePoint (`.aspx`)** — descartado: exigiria Graph API
  ou SharePoint REST API ao vivo para criar/editar página, incompatível
  com o modelo offline (zero dependências externas, sem chamada de rede)
  de `wk.pyz`.

## Consequência no caminho de ingestão (`wk ingest`)
Todo `.docx` gerado por este comando é marcado em `docProps/core.xml` com
`dc:identifier = wk-docx-gerado`. `wk ingest` recusa qualquer asset `.docx`
que carregue essa marcação — a fonte-verdade continua sendo o `.md`
correspondente em `raw/`, nunca o `.docx` derivado. Reingerir um `.docx` de
`wiki-docx/` criaria proveniência falsa (um artefato gerado virando "fonte
original"); a mensagem de recusa aponta para o `.md` em `raw/` e para
regenerar via `wk docx`, não reingerir. Essa checagem também é **D** —
comparação de string no XML, sem modelo (função `_docx_gerado_por_wk` em
`scripts/wk/cli.py`, chamada pela função `cmd_ingest` no mesmo arquivo).

## Não faça
- Não edite `wiki-docx/` à mão — é regenerado (e podado) a cada `docx`;
  qualquer edição manual some ou é removida na próxima rodada.
- Não aponte `wk ingest` para um `.docx` de `wiki-docx/` esperando
  reingerir conteúdo — a fonte é o `.md` em `raw/`.
- Não trate a ausência de upload automático para o SharePoint como bug: é
  comportamento fora de escopo desta v1 (ver "Transporte ao SharePoint"
  acima).
- Não invoque um modelo para "gerar" ou "melhorar" o `.docx` — o comando
  inteiro é determinístico; se o conteúdo estiver errado, o problema está
  na fonte em `raw/`, corrija na origem (`ingest`/`promote`) e rode `docx`
  de novo.

## `.docx` agregador por tópico
Além de "1 fonte promovida = 1 `.docx`" (ver "O que o comando faz" acima,
inalterado), `docx` gera `wiki-docx/<topic>/index.docx` para todo tópico
elegível a overview — mesmo critério de `compile` (`operations/compile.md`,
"Página de overview por tópico"): reusa `_build_topic_overview` e converte
o texto resultante pelo mesmo `docxgen.build_document` dos demais
documentos. Documento consolidado adicional; não substitui os `.docx`
individuais, que continuam sendo gerados.

`coupling.html` nunca é convertido para `.docx` — é pulado explicitamente
(contado em `ignorados_asset_html`); já é HTML navegável em
`raw/assets/`, converter perderia a interatividade.

## Diagramas Mermaid no `.docx`: degradação textual, nunca imagem
Não há rasterização nem dependência externa (mermaid-cli/puppeteer/
headless browser) — todo diagrama vira texto/tabela estruturado no OOXML,
por regra fixa (**D**, não heurística de modelo):

| Tipo Mermaid | Vira no `.docx` |
|---|---|
| `flowchart` / `graph` | lista de nós/dependências (inalterado) |
| `erDiagram` | lista de entidades/relações (inalterado) |
| `sequenceDiagram` | lista ordenada Ator→Ator; `alt`/`else`/`opt`/`loop`/`par` e `Note` viram itens da lista |
| `stateDiagram-v2` | tabela de transições (Origem / Evento-Condição / Destino) |
| `C4Context` / `C4Container` / `C4Component` | tabela de elementos + tabela de relações |
| `classDiagram` | tabela de classes + tabela de relações |
| outros (`pie`/`gantt`/`journey`/`mindmap`) ou Mermaid malformado | legenda em itálico + bloco de código bruto + nota apontando para a versão Markdown da wiki |

Não prometa imagem renderizada em nenhum ponto de contato com o usuário —
é degradação textual estruturada, por design, sem dependência externa.

## Gate de `verify` — 👤 H para destravar
`docx` recusa (`exit 3`) tópicos cujo workdir de origem tem
`state.json.stages.verify.status == "failed"` — escopado pelo `topic` do
comando (ou todos os tópicos, se não filtrar). Override consciente:
```bash
{{WK}} docx financeiro/custos-q3 --store <store> --allow-unverified
```
(registrado em `log.md` e no JSON de saída). Detalhe do gate:
`operations/promote.md`.
