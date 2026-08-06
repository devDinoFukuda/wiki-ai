# schema

`raw/` é fonte-verdade imutável. Nada vira canônico sem passar pelo portão de promoção.

## 1. Estrutura de pastas

```
wiki-ai/
├── inbox/                 # ENTRADA. Nada aqui é fonte-verdade. Estágio obrigatório.
│   ├── transcripts/
│   ├── agent-output/      # saídas do Copilot e outros agentes VOLTAM aqui
│   ├── code-notes/
│   └── clipped/
│
├── raw/                   # FONTE-VERDADE. Imutável. Só entra via promote.
│   ├── transcripts/
│   ├── docs/
│   ├── code-notes/
│   ├── agent-output/
│   └── assets/             # originais imutáveis de docx/xlsx/csv/pdf; ingest
│                           # grava aqui direto (exceção: não passa por
│                           # promote — não é fonte-verdade textual, é apoio)
│
├── wiki/                  # GERADO por `compile`. Uma página por documento promovido.
│   ├── <topic>/<source_type>/<id>.md
│   └── index.md
│
├── log.md                 # log append-only de OPERAÇÕES (não de interações)
└── quarantine.md          # itens rejeitados na promoção + motivo
```

Leitura enxerga só `wiki/` e `raw/`. Saídas de agente voltam a `inbox/agent-output/`.

## 2. Proveniência (frontmatter obrigatório)

Todo arquivo em `inbox/` ou `raw/` carrega o frontmatter de
`templates/source-frontmatter.yaml`. Sem proveniência, não é fonte.

`source_type`:

| source_type       | descrição                              | auto-promoção? |
|-------------------|----------------------------------------|----------------|
| human-transcript  | fala de pessoa (reunião, transferência)| não (exige `--approve`) |
| human-doc         | documento escrito por pessoa           | não (exige `--approve`) |
| code-repo         | extração determinística de repositório | sim            |
| agent-output      | saída de LLM / Copilot / agente        | **NUNCA** (mesmo aprovado, vira `unverified`) |
| web-clip          | conteúdo externo capturado             | não (exige `--approve`) |

Páginas de `wiki/` declaram no frontmatter a fonte que as gerou (uma por
página — `compile` não funde múltiplas fontes numa página):

```yaml
sources: ["sb-001"]
```

Página sem `sources` é reportada pelo `audit`.

## 3. Portão de promoção (`promote`)

- `code-repo` → auto-promove sempre, `confidence: reviewed`.
- `human-transcript` / `human-doc` / `web-clip` → nunca auto-promove. Exige
  `promote --approve <id-ou-caminho>` ou `promote --approve-all --source-type <t>`.
  Promove `confidence: reviewed`, `promoted_by: <--approved-by, default "humano">`.
- `agent-output` → nunca auto-promove, mesmo com `--approve`/`--approve-all`
  explícito: aprovação move o arquivo, mas grava `confidence: unverified`
  (nunca `reviewed`); `origin` preserva a origem de agente.
- Sem `origin`/`source_type` válido (L2) → `quarantine.md`, nunca chega a
  decisão de auto-promoção.

Rejeitados → `quarantine.md` com motivo.

## 4. Regras de lint (`lint`)

- **L1 — Sem canônico só de agente:** página de `wiki/` cuja única fonte é
  `agent-output` → marcar `unverified`.
- **L2 — Proveniência obrigatória:** arquivo em `raw/` sem `origin`/`source_type`
  → quarentena.
- **L3 — Contradição com proveniência:** preferir maior `confidence`/origem
  humana; REGISTRAR a contradição, não escolher em silêncio.
- **L4 — Detector de realimentação:** `agent-output` que parafraseia página da
  wiki derivada de outro `agent-output` (profundidade > 1) → BLOQUEAR promoção.
- **L5 — Supersessão:** fonte com `supersedes` invalida a antiga; a wiki não
  pode mais citar a substituída.

Wikilinks (`[[nome-da-pagina]]`, alvo = stem do arquivo ou `id` do
frontmatter) ganham checagem própria, travessia de `wiki/` à parte do SQL:

- **W1 — Link markdown quebrado:** link relativo markdown cujo arquivo alvo
  não existe.
- **W2 — Wikilink sem alvo:** `[[...]]` sem página correspondente.
- **W3 — Página órfã:** página sem link de entrada (`index.md` e
  `_lint-report.md` isentos).

---

## 5. Operações

- **ingest `<arquivo> --source-type <t> --origin <o> --topic <t>`** — converte
  o arquivo para markdown, atribui proveniência, grava em `inbox/` (nunca em
  `raw/`, exceto o caso de asset abaixo). Registra em `log.md`. Formatos
  aceitos: `.md`, `.txt`, `.vtt`, `.srt`, `.html`/`.htm`, `.xml` (draw.io/XMI
  viram diagrama estruturado + Mermaid; outro XML vira outline + código),
  `.json`. `.docx`/`.xlsx`/`.csv`/`.pdf` gravam o original em
  `raw/assets/<id><ext>` e um stub de proveniência em `inbox/` — a análise
  (LLM) é um ingest posterior via `agent-output` (`operations/ingest.md`).
  Qualquer outro formato (áudio, imagem, ...) segue recusado como fora de
  escopo.
- **publish `--workdir <wd> --topic <t>`** — leva a árvore SDD de um workdir do
  codescan para `inbox/`: `sdd/inventory.md`, `sdd/dependencies.md`,
  `sdd/coupling.md` como `code-repo` em `inbox/code-notes/`; o resto de
  `sdd/**/*.md` e `modules/*.md` como `agent-output` em `inbox/agent-output/`.
  Único caminho suportado para popular `inbox/` a partir do pipeline de código.
- **promote** — roda o portão da seção 3. Auto-promove o permitido, lista o
  resto para decisão humana (a menos que `--approve`/`--approve-all` explícito),
  move rejeitados para `quarantine.md`.
- **compile `[topic]`** — (re)gera `wiki/` a partir de `raw/` **apenas**, uma
  página por documento promovido (`wiki/<topic>/<source_type>/<id>.md`) mais
  `wiki/index.md` agrupado por `topic` (contagem de páginas e data de
  atualização por página). Não sintetiza, não cruza fontes, não sumariza: o
  corpo da página é o corpo do documento promovido, verbatim. Ignora
  `inbox/`. Roda `reindex` automaticamente ao final.
- **lint** — audita o índice (`audit` SQL: L1/L2/L5, sempre) e escreve
  `wiki/_lint-report.md`. Julgamento semântico (L3/L4) é tarefa do agente via
  recuperação, fora do `wk lint` determinístico.
- **reindex** — atualiza o índice. `compile` e `promote` (quando promovem algo)
  já rodam sozinhos; rode manualmente quando desconfiar do índice.
- **query** — consulta a wiki. Em produção este papel é do Copilot; local serve
  para teste.

`export --output <dir>` (pipeline de código) recusa gravar dentro de
`<store>/raw` ou `<store>/wiki`: o único caminho para levar artefatos ao
corpus é `publish` (ou `promote` depois de `publish`).

---

## 6. Loop fechado
```
fonte ──ingest──> inbox/ ──promote──> raw/ ──compile──> wiki/
                                                          │
                          leitura ──> saída volta como agent-output
                          ──> inbox/ ──> promote §3 (não-canônica)
```

## 7. Índice
`store/index.db` é derivado de `raw/` + `wiki/`; reconstruível, deletável.
- `inbox/` NÃO é indexado (no dedup, o inbox é a query; `raw/` é o índice).
- `status` sai 1 se sujo. Compile e lint não rodam sujos.
- `reindex` ao fim do compile.
- `source_type`, `confidence`, `promoted`, `topic`, `supersedes` são colunas.

Detalhes: `retrieval`.

## 8. Notas
- `promote` e `compile` em cadência fixa; entre rodadas a leitura é eventualmente consistente.
- Transcrição = matéria-prima (o que a pessoa diz, não o que sabe fazer).
