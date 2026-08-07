# Wiki AI — Instalação e Execução

## 0. Terminal

PowerShell é proibido; todo comando deste guia roda em Git Bash.

| Sintoma de PowerShell | Remediação |
|---|---|
| Saída contém `No linha:` seguido de `caractere:` | reexecute embrulhando em `bash -c '<comando>'` |
| Saída contém o marcador `~~` | reexecute embrulhando em `bash -c '<comando>'` |
| Comando sugerido é `Get-ChildItem`/`Select-Object` | reexecute embrulhando em `bash -c '<comando>'` |

Primeiro comando de qualquer sessão:

```bash
python wk.pyz doctor --store /caminho/do/store --repo /caminho/do/legado --engine claude-code
```

## 1. Instalar

Distribuição é **um arquivo**: `wk.pyz` (executável Python autocontido — código
e documentação embutidos). Sem unzip, sem PYTHONPATH, sem pip. Requer só
`python3` no PATH.

Materialize a skill para a(s) engine(s) que você usa:

```bash
python wk.pyz init --engine claude-code
```

`--engine` aceita `claude-code`, `antigravity`, `devin`, `copilot`, lista
separada por vírgula, ou `all`. Cada engine descobre a skill no diretório que
ela lê (Claude Code em `.claude/skills/`, as demais em `.agents/skills/`, que
Antigravity/Devin/Copilot leem em comum). `init` escreve só o `SKILL.md`; o
resto da documentação fica dentro do `wk.pyz` e o agente a consulta com
`python wk.pyz docs <nome>`.

```bash
python wk.pyz engines        # lista engines e o que já está instalado
python wk.pyz check --engine all   # disco vs. embutido (update seguro)
```

Reinicie a sessão da engine. Digite `/` e confirme que `wiki-ai` aparece.

> **Update:** troque o `wk.pyz` e rode `init` de novo. `check` mostra o que você
> editou à mão antes de sobrescrever (`--force` para aplicar).

## 2. Criar o store

O store é **separado** da skill: a skill é a ferramenta, o store são os dados.
Um comando cria a estrutura inteira (idempotente):

```bash
python wk.pyz store init /caminho/do/store   # ou: store init (usa ./store)
```

Na primeira conversa, diga ao agente onde o store está:

```
O store do Wiki AI é ./store — inbox, raw e wiki ficam lá dentro.
```

`store init` não cria `wiki-docx/` — essa árvore só existe sob demanda: `wk
docx` a cria na primeira execução, em paralelo a `wiki/`, com os mesmos
documentos em formato `.docx` (para bibliotecas SharePoint/Copilot Studio,
que não aceitam `.md`). Ver `schema.md` §1 para a estrutura completa do store.

## 2.1. Permissão da engine sobre o store (obrigatório)

Sem este passo, o agente principal e os subagentes **não têm acesso** ao
store: `wk init` grava a permissão junto com a skill quando você passa
`--store`/`--repo`. Rode de novo (idempotente, mescla sem duplicar):

```bash
python wk.pyz init --engine claude-code --store /caminho/do/store --repo /caminho/do/legado
```

Isto grava, no `settings.json` da engine (`.claude/settings.json` para
`claude-code`; `.agents/settings.json` para antigravity/devin/copilot):
- `additionalDirectories`: store e repo, para o agente enxergar as pastas;
- `allow`: `Read(<store>/**)`, `Read(<repo>/**)`, `Bash(wk *)`;
- `deny`: `Write(<store>/**)`, `Edit(<store>/**)` — a sessão principal **nunca**
  escreve dentro do store à mão; quem escreve é sempre o `wk` (`ingest`,
  `publish`, `promote`, `compile`), nunca `Write`/`Edit` direto do agente.

Cada item de `"permissoes"` (`init`) e `config_permissoes` (`check`/`doctor`)
traz `"formato"`: `verificado` só para `claude-code` — schema real de
`.claude/settings.json`, documentado e comprovadamente consumido pela engine.
Para antigravity/devin/copilot, `"formato": "best-effort"` —
`.agents/settings.json` é gravado corretamente, mas nada garante que essas
engines de fato o consumam; trate como best-effort, nunca como garantia.

Confirme com:

```bash
python wk.pyz check --engine claude-code --store /caminho/do/store --repo /caminho/do/legado
```

`config_permissoes` reporta `ok: false` e a lista de problemas se faltar algo
(`store ausente em additionalDirectories`, `deny ausente`, etc.). Se você pular
este passo, `run-stage` falha rápido com `sem permissão de escrita em
<workdir>/agent-outputs` — é o probe determinístico que o `run-stage` roda
antes de emitir qualquer manifesto para subagente.

## 3. Testar

O `wk.pyz` é a única superfície de comando. Store default é `./store` (ou
`SB_STORE`).

```bash
python wk.pyz index status
```

Deve responder JSON com `"embedder": "none (lex-only)"`.

> Nos documentos das operações, o comando aparece como `{{WK}}` — o `init`
> resolve esse marcador para a chamada real do executável na sua máquina.

---

# Fluxo A — Conhecimento (o principal)

## A1. Ingerir

```
/wiki-ai ingest ./transcricao.md
```

O agente decide `source_type`/`origin`/`topic` e roda:

```bash
python wk.pyz ingest ./transcricao.md \
  --source-type human-transcript --origin "Maria, 1:1 26/07" --topic pagamentos
```

`ingest` **é o único jeito** de gravar em `inbox/`: a permissão da engine (§2.1)
nega `Write`/`Edit` direto do agente dentro do store. O comando converte para
markdown e grava com proveniência (`source_type`, `origin`, `captured_at`,
`promoted: false`, `topic`), e registra em `log.md`.

Formatos aceitos: `.md`, `.txt` (direto), `.vtt`/`.srt` (timestamps viram
markdown), `.html`/`.htm` (tags removidas, headings preservados), `.xml`
(draw.io/XMI viram diagrama estruturado + Mermaid; outro XML vira outline +
bloco de código), `.json` (bloco de código anotado). `.docx`/`.xlsx`/`.csv`/
`.pdf` **não são convertidos mecanicamente**: o comando grava o original em
`raw/assets/<id><ext>` e um stub de proveniência em `inbox/`; a análise
(sempre do LLM) entra depois como `agent-output` (`operations/ingest.md`).
Áudio, imagem e demais formatos seguem recusados como fora de escopo.

**`inbox/` não é indexado e não entra na wiki.** É estágio, não fonte.

## A2. Promover

```
/wiki-ai promote
```

Roda:

```bash
python wk.pyz promote --store "$WK_STORE"
```

Devolve três blocos: **Promovidos** · **Requer decisão humana** · **Quarentena**.
Só `code-repo` auto-promove. Os demais tipos ficam em "Requer decisão humana"
até aprovação explícita:

```bash
python wk.pyz promote sb-2026-0142 --approve sb-2026-0142 --approved-by "Maria" --store "$WK_STORE"
# ou, para aprovar em lote por tipo:
python wk.pyz promote --approve-all --source-type human-transcript --approved-by "Maria" --store "$WK_STORE"
```

O arquivo migra para `store/raw/`, vira `promoted: true` /
`promoted_by: <quem aprovou>` / `confidence: reviewed` (`agent-output`
aprovado vira `confidence: unverified`, nunca `reviewed`, mesmo aprovado).

## A3. Compilar

```
/wiki-ai compile
```

Gera **uma página por documento promovido** em
`store/wiki/<topic>/<source_type>/<id>.md` (corpo verbatim, sem sintetizar
nem cruzar fontes) e atualiza `wiki/index.md` com a lista. Cada página declara
sua fonte única:

```yaml
sources: ["sb-001"]
```

Isso não é enfeite: é o que torna L1 e L5 verificáveis por SQL. Cruzamento
entre fontes acontece na leitura (`search`/`query`), não na compilação.

O compile roda `reindex` sozinho ao final — não precisa rodar manualmente
depois.

## A4. Auditar

Barato, determinístico, roda sempre:

```bash
python wk.pyz index audit
```

| Regra | Pega |
|---|---|
| `L2_proveniencia_ausente` | fonte sem `origin` ou `source_type` |
| `L1_canonico_so_de_agente` | página cujas fontes são todas `agent-output` |
| `L5_supersedida_ainda_citada` | página citando fonte já substituída |
| `wiki_sem_fontes_declaradas` | página sem `sources:` |
| `fonte_orfa` | fonte promovida que ninguém cita |

Sai com código 1 se houver achado.

O lint semântico (L3 contradição, L4 realimentação) é caro e roda periodicamente:

```
/wiki-ai lint
```

## A5. Consultar

```
/wiki-ai query como funciona o retry de pagamentos?
```

Ou direto:

```bash
printf 'intent: como funciona o retry\nlex: retry dead-letter\n' \
  | python wk.pyz index search -c wiki -n 5
```

---

# Fluxo B — Codebase

O legado é **READ-ONLY**. Nada é escrito dentro dele. Além das fontes para o
corpus, o pipeline gera a **árvore SDD** em
`store/.codescan/<repo>-<hash>/sdd/` — inventário, C4, ERD, specs por unit —
no contrato de `references/sdd-contract.md` (replica o Discovery do Reversa).

## B1. Surface + export (determinístico, sem LLM)

```bash
python wk.pyz code --repo /caminho/do/legado \
  surface --topic pagamentos
python wk.pyz code --repo /caminho/do/legado \
  export --topic pagamentos
```

`surface` devolve linguagens, LOC, manifests com dependências, entry points,
módulos, churn e autores. **Leia os `warnings`** — "sem git" significa que
você perdeu churn, que é o que aponta onde cavar. `export` materializa
`sdd/inventory.md`, `sdd/dependencies.md` e `sdd/coupling.md` (grafo de
acoplamento — motor Java especializado quando o repo tem Java, motor
genérico multi-linguagem caso contrário; com Java, também grava
`sdd/coupling.html` local para inspeção interativa), todos com proveniência
`code-repo` (o `.html` não conta — é local, não publica).

Depois, registre as duas decisões de escopo (o agente pergunta):

```bash
python wk.pyz code --repo /caminho/do/legado \
  config --doc-level essencial --granularity module
```

## B2. Planejar

```bash
python wk.pyz code --repo /caminho/do/legado plan --top 20
```

Ordena por LOC com churn e autores. **Churn alto + LOC alto = cave primeiro.**

## B3. Cavar

```
/wiki-ai ingest codebase /caminho/do/legado --topic pagamentos
```

O agente segue `operations/ingest-codebase.md`: lê `surface.json`, cava módulo
a módulo, extrai regras de negócio, e marca cada afirmação.

| Marca | Exige |
|---|---|
| 🟢 CONFIRMADO | `arquivo:linha` — sem exceção |
| 🟡 INFERIDO | justificativa |
| 🔴 GAP | pergunta ao humano |

Na sequência, os estágios **architecture** (C4, ERD, integrações, dívida
técnica) e **specs** (requirements/design/tasks por unit, matrizes de
rastreabilidade, confidence-report) completam a árvore SDD — contrato e
templates em `references/sdd-contract.md`. Tracking por unit:

```bash
python wk.pyz code --repo /caminho/do/legado \
  pending specs --items "auth,orders,payments"
python wk.pyz code --repo /caminho/do/legado \
  done specs --item auth
```

Retomada após sessão morta:

```bash
python wk.pyz code --repo /caminho/do/legado next
```

## B3.5. Evidence pack

Antes da síntese, gere um pacote rastreável de evidências para o tópico:

```bash
python wk.pyz code --repo /caminho/do/legado \
  evidence --topic pagamentos
```

O pacote fica em `store/.codescan/<repo>-<hash>/evidence-pagamentos.json`.
Ele é agnóstico: não usa parser nativo nem toolchain da linguagem. Serve para
dar contexto com `arquivo:linha` ao agente que vai escrever `sdd/confirmed.md`
e `sdd/inferred.md` (não existe `questions.md` no código; as perguntas ao
humano ficam dentro de `confidence-report.md`/`gaps.md` e da própria síntese).

## B4. O resultado entra no fluxo A

`sdd/confirmed.md` e `sdd/inferred.md` são o local **canônico** da síntese
(nunca uma cópia solta na raiz do workdir — o `audit` reprova a divergência
como P0). `wk publish` leva a árvore inteira para `inbox/`, dividida por
confiança:

```bash
python wk.pyz publish --workdir "$WK_STORE/.codescan/<repo>-<hash>" --topic pagamentos --store "$WK_STORE"
```

| Arquivo | `source_type` (via `publish`) | Auto-promove no `promote`? |
|---|---|---|
| `sdd/confirmed.md` | `agent-output` | **não** — só `sdd/inventory.md`, `sdd/dependencies.md`, `sdd/coupling.md` viram `code-repo`; o resto da árvore, `confirmed.md` incluso, é `agent-output` |
| `sdd/inferred.md` | `agent-output` | **não** |
| `sdd/inventory.md`, `sdd/dependencies.md`, `sdd/coupling.md` | `code-repo` | **sim** — determinísticos, saem do `export` |
| `sdd/coupling.html` (só quando o motor Java roda) | — | **não é artefato SDD** — não entra em `written`, não vai para `inbox/`, `publish` não o leva; é só inspeção local |
| demais artefatos `sdd/` (C4, ERD, specs...) e `modules/*.md` | `agent-output` | **não** — síntese de agente, mesmo citando código |

Não existe `questions.md` no código: as 🔴 (perguntas ao humano) ficam
registradas dentro de `confidence-report.md`/`gaps.md` (estágio specs) e do
próprio `sdd/confirmed.md`/`inferred.md` — não há artefato separado para elas.

`export --output` recusa gravar dentro de `store/raw` ou `store/wiki`
diretamente; o caminho para o corpus é sempre `publish` + `promote`, nunca
escrita manual nessas pastas.

Antes de publicar/promover `confirmed.md`, rode:

```bash
python wk.pyz code --repo /caminho/do/legado \
  verify --artifact "$WK_STORE/.codescan/<repo>-<hash>/sdd/confirmed.md"
```

Se falhar, não promova como `code-repo`: rebaixe a claim para `inferred.md` ou
transforme em pergunta. `verify` não prova que a análise está semanticamente
perfeita; ele só bloqueia a classe mais perigosa de erro: afirmação 🟢 sem
evidência `arquivo:linha`.

Depois: `publish`, `promote` e `compile` normais.

> **Estágio `synth` no pipeline de código:** `run-stage synth` e
> `merge-agent-output synth` são aceitos normalmente pelo `codescan` atual —
> os blocos esperados são `=== SYNTH: confirmed ===` / `=== SYNTH: inferred
> ===`, gravados em `sdd/confirmed.md` / `sdd/inferred.md`. Completar `done
> synth` exige `agent-runs/synth.json` (mesma prova de proveniência dos
> demais estágios).

**É aqui que o valor aparece.** A spec diz `MAX_RETRIES = 5` em
`PaymentProcessor.cs:3` 🟢. A Maria disse "3x" na transferência. Nenhuma das
duas fontes contém a contradição — ela nasce do cruzamento, e o L3 registra.

---

# Ordem recomendada

1. Uma transcrição → `ingest` → `promote` → aprove → `compile`. Olhe a wiki.
2. Uma segunda fonte → repita. **Agora a wiki compõe** — cruza as duas.
3. `audit`. Deve vir quase limpo.
4. Só então o Fluxo B, num repo pequeno.

Não comece pelo codebase. Valide o portão com o caminho simples primeiro.

---

# Estado atual: modo léxico

Você optou por não integrar embeddings. Consequências:

- `lex:` funciona. `vec:` e `hyde:` **dão erro** — de propósito, não retornam
  vazio. Falha silenciosa é o inimigo.
- BM25 casa string, não sentido. Se a fonte diz "dead-letter queue" e você
  busca "fila de erro", não acha. Use `OR`:

```
lex: "dead-letter" OR "DLQ" OR "fila de erro"
```

Se um dia o léxico falhar — a página existe mas com outras palavras — três
variáveis destravam:

```bash
export AZURE_OPENAI_ENDPOINT=https://<recurso>.openai.azure.com
export AZURE_OPENAI_API_KEY=...
export AZURE_OPENAI_EMBED_DEPLOY=text-embedding-3-small
python wk.pyz index reindex
```

Confira `chunks_sem_embedding: 0` no `status`. **Este caminho nunca foi testado
de verdade** — o que quebra primeiro costuma ser o nome do deployment, que é o
que *você* nomeou no Azure, não o nome do modelo.

---

# Quando algo der errado

| Sintoma | Causa provável |
|---|---|
| `ModuleNotFoundError: sbindex` | PYTHONPATH (seção 3) |
| Busca não acha o que existe | modo léxico: a palavra é outra. Use `OR`. |
| `filtro desconhecido: 'collectio'` | typo — o erro lista os válidos |
| Busca não acha página que o compile acabou de gerar | índice sujo. `status` → `reindex` (o `compile` já roda sozinho, mas se você editou `raw/`/`wiki/` por fora, rode manualmente). |
| Resultado fora de escopo | faltou `-c wiki` ou `--filter topic=` |
| Página some após compile | correto: a wiki é derivada, não edite à mão |

---

# O que não está pronto

- **Estágio `synth` do pipeline de código tem `run-stage`/`merge-agent-output`
  no CLI atual** (ver nota em B4). Blocos `=== SYNTH: confirmed ===` /
  `=== SYNTH: inferred ===` gravam `sdd/confirmed.md` / `sdd/inferred.md`.
  `agent-runs/synth.json` é exigido pelo `audit`/`done synth`, como nos
  demais estágios.
- **Azure OpenAI não validado.** Ver acima.
- **Estágio 4 do codebase divide por confiança, não por tipo.** Num rescan você
  vê uma reescrita, não um diff. Dói quando o repo for reanalisado.
