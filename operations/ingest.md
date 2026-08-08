# ingest

Uso: `ingest <caminho>`. Registra fonte em `inbox/`. Nunca escreve em `raw/`.

`ingest` é o único jeito de gravar em `inbox/`: a permissão da engine nega
`Write`/`Edit` direto do agente dentro do store (`wk init --store` grava
`deny: Write(<store>/**), Edit(<store>/**)`). Não escreva frontmatter à mão;
rode o comando.

## Quem executa o quê neste fluxo
Só duas personas existem: 👤 humano (digita o comando) e 🤖 modelo (LLM). O
`wk.pyz`/CLI é ferramenta, nunca ator.

| Tipo | Passos neste doc | Quem | Precisa de LLM? |
|---|---|---|---|
| **D** | 3 (ingerir original, reingerir análise, `promote`/`compile` depois) | 👤 ou 🤖, indiferente | Não |
| **M** | 2 (ler o asset binário, escrever a página de análise) | 🤖 obrigatório | Sim |
| **H** | 1 (escolher `source_type`/`origin` corretos; aprovar em `promote`, doc `promote`) | 👤 obrigatório | Não, mas exige julgamento |

**Dá para completar sem modelo?** Depende do formato. `.md`/`.txt`/`.vtt`/
`.srt`/`.html`/`.xml`/`.json`: SIM, 100% humano sozinho —
`cmd_ingest` (`scripts/wk/cli.py` (função `cmd_ingest`)) só chama conversores mecânicos
(`_convert_to_markdown`, regex/`xml.etree`), nenhum cliente de LLM é
importado no arquivo. `.docx`/`.xlsx`/`.csv`/`.pdf`: NÃO — o comando grava o
asset e um stub (prova: `_ingest_asset`, `scripts/wk/cli.py` (função `_ingest_asset`),
`shutil.copyfile` + template fixo), mas a leitura/análise do conteúdo do
asset é obrigatoriamente 🤖 (nenhum extrator de texto embutido nesse
caminho).

## `ingest` vs `ingest codebase`
`{{WK}} ingest <caminho>` só aceita arquivo de fonte solto (transcrição, doc,
clip — formatos abaixo). Repositório de código **não** usa `ingest`:
`{{WK}} ingest codebase ...` não é subcomando do CLI e falha com erro
acionável apontando o equivalente: `{{WK}} code --repo <repo> --store <store>
surface --topic <slug>` (início do pipeline de 8 estágios, doc
`ingest-codebase`).

```bash
{{WK}} ingest <caminho> --source-type <tipo> --origin "<proveniência concreta>" --topic <slug>
```

Os três parâmetros são obrigatórios. Sem `--origin` (não pode ser vazio) ou
`--source-type` válido, o comando recusa antes de escrever qualquer coisa
(validação na função `cmd_ingest` de `scripts/wk/cli.py`, checagem de
`--source-type`/`--origin` — puramente determinístico, **D**).

## Formatos aceitos

| Formato | Tratamento |
|---|---|
| `.md`, `.txt` | direto, sem transformação (passthrough) |
| `.vtt`, `.srt` | timestamps preservados como texto (`**00:00:01.000 --> 00:00:04.000**`), corpo em markdown |
| `.html`/`.htm` | tags removidas; `<h1>`–`<h6>` viram `#`–`######` |
| `.xml` | análise estrutural: draw.io (`mxGraphModel`) → componentes/relações + Mermaid `flowchart`; XMI/UML (`xmi:XMI`) → classes/associações + Mermaid `classDiagram`; qualquer outro XML → outline estrutural (elementos/atributos, profundidade limitada) + bloco de código preservado |
| `.json` | preservado como bloco de código anotado (não vira prosa) |
| `.docx`, `.xlsx`, `.csv`, `.pdf` | asset + análise (ver seção abaixo) — não há conversão mecânica |

Toda esta tabela é **D**: regex/parser fixo, sem chamada de modelo — prova
nas funções `_convert_subtitles`/`_convert_html`/`_convert_codeblock`/
`_convert_drawio` de `scripts/wk/cli.py` e no trecho de conversão XML
adiante no mesmo arquivo. 👤 humano executa sozinho.

Qualquer outro formato (áudio, imagem, `.pptx`, ...) continua **recusado** —
"formato fora de escopo". O comando não faz OCR nem transcrição.

## Docs e planilhas: asset + análise (`.docx`/`.xlsx`/`.csv`/`.pdf`) — a fronteira D→M

Estes formatos não são convertidos mecanicamente — não existe extrator de
texto embutido. Padrão llm-wiki: o comando grava o **original imutável** em
`raw/assets/<id><ext>` (fora do portão de `promote` — não é fonte-verdade
textual, é material de apoio) e cria em `inbox/` uma **página de fonte**
(stub) com frontmatter de proveniência e link para o asset. A partir daí o
fluxo muda de mãos: sem 🤖 nenhuma análise acontece, o stub fica marcado
"Análise pendente" para sempre.

### Passo 1 — 👤 D — ingerir o asset
Humano roda sozinho, sem modelo. Substitua `planilha-custos.xlsx`,
`--origin`, `--topic` pelos valores reais.

```bash
{{WK}} ingest planilha-custos.xlsx --source-type human-doc --origin "planilha enviada por Fulano em 2026-08-01, e-mail 'orçamento Q3'" --topic financeiro/custos-q3
```

Saída esperada (trecho):
```json
{
  "id": "sb-ingest-planilha-custos-a1b2c3d4",
  "path": "inbox/transcripts/sb-ingest-planilha-custos-a1b2c3d4.md",
  "source_type": "human-doc",
  "asset": "raw/assets/sb-ingest-planilha-custos-a1b2c3d4.xlsx"
}
```
Como saber que deu certo: campo `asset` presente no JSON e o arquivo existe
em `raw/assets/`. Se falhar: erro de `source_type inválido` ou `--origin não
pode ser vazio` (exit 2, campo `error` no JSON de stderr) — não há campo
`acao` neste comando; corrija o parâmetro apontado em `error` e rode de
novo.

### Passo 2 — 🤖 M — ler o asset e extrair os pontos-chave
**Sem modelo, o fluxo para aqui.** Não existe comando `wk` que leia
`.xlsx`/`.docx`/`.csv`/`.pdf` e produza texto — só um agente LLM (usando sua
ferramenta de leitura de arquivo) consegue abrir
`raw/assets/sb-ingest-planilha-custos-a1b2c3d4.xlsx`, entender a planilha e
decidir o que é relevante. O modelo LÊ o asset citado no stub (campo
"Original imutável" do arquivo gerado no Passo 1).

### Passo 3 — 🤖 M — escrever a página de análise
O modelo ESCREVE um arquivo `.md` novo (fora do store, ex.:
`analise-custos-q3.md`) com a leitura: pontos-chave, resumo, decisões,
tabelas relevantes reescritas em markdown. Este arquivo ainda não está no
`inbox/` — é só um arquivo comum no disco, escrito pelo modelo com
`Write`/ferramenta equivalente (fora do store, a permissão `deny` não se
aplica).

### Passo 4 — 👤 ou 🤖 D — reingerir a análise
Mecânico de novo — mesmo comando do Passo 1, mesma prova de código.
`--origin` deve referenciar o `id` do stub original para manter
rastreabilidade.

```bash
{{WK}} ingest analise-custos-q3.md --source-type agent-output --origin "análise de sb-ingest-planilha-custos-a1b2c3d4" --topic financeiro/custos-q3
```

Saída esperada: mesmo formato do Passo 1, sem campo `asset` (não é arquivo
binário), `path` dentro de `inbox/agent-output/`. Falha mais comum: `--topic`
divergente do Passo 1 — a análise fica sem ligação de tópico com o asset
original; confira antes de rodar.

### Passo 5 — 👤 H — aprovar em `promote`
`agent-output` nunca auto-promove (ver `operations/promote.md`). Humano
decide se a análise está correta e assina a aprovação — risco de aprovar
errado: conteúdo gerado por LLM, não revisado, vira fonte canônica da wiki.

```bash
{{WK}} promote --approve inbox/agent-output/sb-ingest-analise-custos-q3-<hash8>.md --approved-by "dev.dinofukuda@gmail.com" --store <store>
```

### Passo 6 — 👤 ou 🤖 D — `compile`
Gera a página final em `wiki/` a partir do que foi promovido (doc
`compile`). Nenhum modelo envolvido nesta etapa.

## Classificar `source_type` (schema §2) — 👤 H
5 valores válidos: `human-transcript`, `human-doc`, `code-repo`, `agent-output`,
`web-clip`. Dúvida humano/agente → `agent-output`. Áudio já transcrito por IA →
`human-transcript`, anote a transcrição automática em `--origin`. Esta
escolha é sempre **H**: o comando não valida a veracidade do valor informado
(só confere que está na lista, na função `cmd_ingest` de
`scripts/wk/cli.py`), então errar aqui não trava tecnicamente — mas
classifica errado a proveniência para sempre.

## O que o comando faz
1. Converte o arquivo (tabela acima) ou, para `.docx`/`.xlsx`/`.csv`/`.pdf`,
   grava o asset em `raw/assets/` e o stub em `inbox/` (ver seção acima).
   Falha de formato → erro, nada é escrito.
2. Gera `id` estável (`sb-ingest-<slug-do-nome>-<hash8>`).
3. Grava frontmatter: `id`, `source_type`, `origin`, `captured_at` (agora, UTC,
   ou `--captured-at` se informado), `promoted: false`, `topic`. Não grava
   `confidence` nem `source_link` neste passo — isso só existe a partir do
   `promote`.
4. Grava na subpasta de `inbox/` correspondente ao `source_type`:
   `transcripts/` (human-transcript, human-doc), `agent-output/`,
   `code-notes/` (code-repo), `clipped/` (web-clip).
5. Append em `log.md`: `## [<data>] ingest | <id> | <source_type> | <arquivo>`.

Passos 1-5 acima são **D** — todos dentro de `cmd_ingest`
(`scripts/wk/cli.py` (função `cmd_ingest`)), sem nenhuma chamada de modelo.

## Não faça
- Não compile, não promova, não deduplique contra `raw/`.
- Não preencha `--origin` por suposição.
- Não tente ingerir áudio/imagem direto — fora de escopo, sem OCR/transcrição.
- Não escreva o arquivo em `inbox/` com Write/Edit; a permissão da engine
  bloqueia isso e o comando é quem gera id/frontmatter corretos.
- Não trate o asset de `raw/assets/` como análise pronta: ele é só a fonte
  bruta; a página de análise (🤖, Passo 3 acima) é um ingest separado.
- Não pule o Passo 2/3 achando que o Passo 1 "já ingeriu o conteúdo": ele
  ingeriu só o arquivo original e um stub — o conteúdo textual só existe
  depois que um modelo o escreve.

## Saída
JSON com `id`, `path` (relativo ao store) e `source_type`.
