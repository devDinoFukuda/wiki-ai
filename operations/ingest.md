# ingest

Uso: `ingest <caminho>`. Registra fonte em `inbox/`. Nunca escreve em `raw/`.

`ingest` é o único jeito de gravar em `inbox/`: a permissão da engine nega
`Write`/`Edit` direto do agente dentro do store (`wk init --store` grava
`deny: Write(<store>/**), Edit(<store>/**)`). Não escreva frontmatter à mão;
rode o comando.

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
`--source-type` válido, o comando recusa antes de escrever qualquer coisa.

## Formatos aceitos

| Formato | Tratamento |
|---|---|
| `.md`, `.txt` | direto, sem transformação (passthrough) |
| `.vtt`, `.srt` | timestamps preservados como texto (`**00:00:01.000 --> 00:00:04.000**`), corpo em markdown |
| `.html`/`.htm` | tags removidas; `<h1>`–`<h6>` viram `#`–`######` |
| `.xml` | análise estrutural: draw.io (`mxGraphModel`) → componentes/relações + Mermaid `flowchart`; XMI/UML (`xmi:XMI`) → classes/associações + Mermaid `classDiagram`; qualquer outro XML → outline estrutural (elementos/atributos, profundidade limitada) + bloco de código preservado |
| `.json` | preservado como bloco de código anotado (não vira prosa) |
| `.docx`, `.xlsx`, `.csv`, `.pdf` | asset + análise (ver seção abaixo) — não há conversão mecânica |

Qualquer outro formato (áudio, imagem, `.pptx`, ...) continua **recusado** —
"formato fora de escopo". O comando não faz OCR nem transcrição.

## Docs e planilhas: asset + análise (`.docx`/`.xlsx`/`.csv`/`.pdf`)
Estes formatos não são convertidos mecanicamente — não existe extrator de
texto embutido. Padrão llm-wiki: o comando grava o **original imutável** em
`raw/assets/<id><ext>` (fora do portão de `promote` — não é fonte-verdade
textual, é material de apoio) e cria em `inbox/` uma **página de fonte**
(stub) com frontmatter de proveniência e link para o asset.

A análise é sempre do LLM, nunca mecânica. Fluxo:
1. `wk ingest planilha.xlsx --source-type human-doc --origin "<proveniência>" --topic <slug>`
   grava o asset e o stub.
2. O agente lê o original em `raw/assets/`, discute/extrai os pontos-chave.
3. O agente escreve a página de análise em markdown.
4. `wk ingest analise.md --source-type agent-output --origin "análise de <id do stub>" --topic <slug>`
   registra a análise em `inbox/agent-output/` (mesmo portão de sempre).
5. `promote` decide (agent-output nunca auto-promove) → `compile` gera a página.

## Classificar `source_type` (schema §2)
5 valores válidos: `human-transcript`, `human-doc`, `code-repo`, `agent-output`,
`web-clip`. Dúvida humano/agente → `agent-output`. Áudio já transcrito por IA →
`human-transcript`, anote a transcrição automática em `--origin`.

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

## Não faça
- Não compile, não promova, não deduplique contra `raw/`.
- Não preencha `--origin` por suposição.
- Não tente ingerir áudio/imagem direto — fora de escopo, sem OCR/transcrição.
- Não escreva o arquivo em `inbox/` com Write/Edit; a permissão da engine
  bloqueia isso e o comando é quem gera id/frontmatter corretos.
- Não trate o asset de `raw/assets/` como análise pronta: ele é só a fonte
  bruta; a página de análise (LLM) é um ingest separado.

## Saída
JSON com `id`, `path` (relativo ao store) e `source_type`.
