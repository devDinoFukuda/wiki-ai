# Wiki AI — Instalação e Execução

## Papéis: quem faz o quê

Existem **duas personas**. Só duas. `wk.pyz`, scripts e CLI são **ferramentas**
— nunca atores. Nenhum passo deste guia "roda sozinho": ou um humano digita o
comando, ou um agente LLM o digita por ele.

| Marcador | Persona | Significado |
|---|---|---|
| 👤 | **HUMANO** | uma pessoa digita o comando no terminal |
| 🤖 | **MODELO** | um agente LLM digita o comando (dentro de uma sessão de engine) |

Toda etapa deste guia carrega uma classificação:

| Tipo | Nome | Quem executa | Precisa de LLM? |
|---|---|---|---|
| **D** | Determinístico | 👤 ou 🤖 — indiferente | **Não.** Mesma entrada → mesma saída. Você mesmo roda, sem abrir sessão de agente nenhuma. |
| **M** | Requer modelo | 🤖 obrigatoriamente | **Sim.** Sem modelo o fluxo empaca — ninguém digita o conteúdo que falta. |
| **H** | Decisão humana | 👤 obrigatoriamente | **Não**, mas exige julgamento/autorização — o modelo pergunta, não decide sozinho. |

**Achado principal deste guia: a instalação inteira (§1–§3) é 100% D/H — zero
M.** Nenhum comando de `wk.pyz doctor|init|store|check|index status` chama um
LLM (conferido lendo `scripts/wk/cli.py` e `scripts/sbindex/cli.py`: nenhuma
dessas rotinas importa cliente de modelo algum). Você instala e configura o
Wiki AI sozinho, num terminal, sem abrir o Claude Code nem qualquer outra
engine.

### Resumo por fluxo

| Fluxo | Passos D | Passos M | Passos H | Dá para completar sem modelo? |
|---|---|---|---|---|
| Instalação (§1–§3) | doctor, init, store init, check, index status | nenhum | escolher engine/caminho do store e do repo | **Sim, integralmente.** |
| Fluxo A — Conhecimento (ingest→promote→compile→audit→query) | `wk ingest` (grava), `wk promote` (portão), `wk compile`, `wk index audit`, `wk index search` (busca) | análise de `.docx/.xlsx/.csv/.pdf`; lint semântico L3/L4; síntese da resposta em `query` | classificar `source_type/origin/topic`; aprovar item em `promote` | **Não** para análise de assets, lint semântico e síntese de resposta — o resto sim |
| Fluxo B — Codebase (surface→export→plan→cavar→evidence→publish) | `surface`, `export`, `config` (grava), `plan`, `pending`/`done` (gravam), `next`, `evidence`, `run-stage`, `merge-agent-output`, `verify`, `publish`, `promote` (código-fonte), `compile`, `docx` | conteúdo dos estágios `modules`/`rules`/`architecture`/`specs`/`synth` (o que o subagente escreve) | valores de `--doc-level`/`--granularity`; escopo de `pending --items`; `--allow-unverified`; aprovação em `promote` de itens não-`code-repo` | **Não** — cavar o repo e sintetizar `confirmed.md`/`inferred.md` exige LLM; o resto do runbook é D/H |

Runbook completo de codebase, com a tabela de responsáveis por cada um dos 32
subpassos, em [README.md §4](README.md#4-runbook-de-codebase).

---

## 0. Terminal — 👤 D

PowerShell é proibido; todo comando deste guia roda em Git Bash. Isto é uma
checagem de ambiente, não uma decisão: qualquer pessoa roda sozinha, sem
sessão de agente aberta.

| Sintoma de PowerShell | Remediação |
|---|---|
| Saída contém `No linha:` seguido de `caractere:` | reexecute embrulhando em `bash -c '<comando>'` |
| Saída contém o marcador `~~` | reexecute embrulhando em `bash -c '<comando>'` |
| Comando sugerido é `Get-ChildItem`/`Select-Object` | reexecute embrulhando em `bash -c '<comando>'` |

Primeiro comando de qualquer sessão — **D**, `wk doctor` só lê disco/ambiente
e imprime um diagnóstico; nenhuma chamada de modelo existe em `cmd_doctor`
(`scripts/wk/cli.py`). Substitua `/caminho/do/store` e `/caminho/do/legado`
pelos caminhos reais (ou omita `--repo` se ainda não tiver um legado):

```bash
python wk.pyz doctor --store /caminho/do/store --repo /caminho/do/legado --engine claude-code
```

Saída (trecho, `store`/`repo` ainda não existem neste exemplo):

```json
{
  "shell": { "detectado": "bash/posix (...)", "powershell_provavel": false },
  "wk": { "versao": "0.1.0", "executavel": "python \"...\\wk.pyz\"" },
  "store": { "caminho": "C:\\...\\store", "existe": false },
  "bloqueios": ["store"],
  "proximo_passo": "wk store init C:\\...\\store"
}
```

**Como saber que deu certo:** `"bloqueios": []` e `"proximo_passo": "ambiente
ok — nenhuma ação necessária"`. **Se falhar:** o próprio JSON já diz o que
rodar em `proximo_passo` — copie e cole; não adivinhe o comando.

## 1. Instalar — 👤 H (escolher engine) + 👤 D (executar)

Distribuição é **um arquivo**: `wk.pyz` (executável Python autocontido — código
e documentação embutidos). Sem unzip, sem PYTHONPATH, sem pip. Requer só
`python3` no PATH.

**H — decisão humana:** qual engine você usa (`claude-code`, `antigravity`,
`devin`, `copilot`) é você quem sabe, ninguém adivinha por você; não há
julgamento de risco aqui além de "qual ferramenta está instalada na sua
máquina".

**D — execução determinística.** Materialize a skill para a engine escolhida
(substitua `claude-code` se usar outra):

```bash
python wk.pyz init --engine claude-code
```

`--engine` aceita `claude-code`, `antigravity`, `devin`, `copilot`, lista
separada por vírgula, ou `all`. Cada engine descobre a skill no diretório que
ela lê (Claude Code em `.claude/skills/`, as demais em `.agents/skills/`, que
Antigravity/Devin/Copilot leem em comum). `init` escreve só o `SKILL.md`; o
resto da documentação fica dentro do `wk.pyz` e o agente a consulta com
`python wk.pyz docs <nome>`. `cmd_init` (`scripts/wk/cli.py`) só copia
texto embutido para disco — nenhuma chamada de modelo.

Saída esperada:

```json
{
  "engines": ["claude-code"],
  "invocacao": "python \"C:\\...\\wk.pyz\"",
  "modo": "minimo (SKILL.md)",
  "alvos": [
    { "dir": "C:\\...\\.claude\\skills\\wiki-ai", "comando": "/wiki-ai",
      "escritos": ["SKILL.md"], "inalterados": [] }
  ]
}
```

**Como saber que deu certo:** `SKILL.md` aparece em `"escritos"` (primeira vez)
ou `"inalterados"` (reexecução idempotente). **Se falhar:** `"error":
"existente e diferente; use --force"` — você editou o `SKILL.md` local à mão;
rode com `--force` para sobrescrever, ou copie sua edição antes.

```bash
python wk.pyz engines        # lista engines e o que já está instalado — D
python wk.pyz check --engine all   # disco vs. embutido (update seguro) — D
```

Reinicie a sessão da engine (**H** — ação humana na interface da engine, sem
comando de terminal). Digite `/` e confirme que `wiki-ai` aparece. **Se
falhar** (não aparece): a engine não leu o diretório novo — reinicie de novo;
se persistir, confira `python wk.pyz engines` para ver se `SKILL.md` está de
fato em disco no caminho que a engine lê.

> **Update:** troque o `wk.pyz` e rode `init` de novo (D). `check` mostra o que
> você editou à mão antes de sobrescrever (`--force` para aplicar).

## 2. Criar o store — 👤 D

O store é **separado** da skill: a skill é a ferramenta, o store são os dados.
Um comando cria a estrutura inteira (idempotente) — `cmd_store`
(`scripts/wk/cli.py`) só faz `os.makedirs`, sem LLM. Substitua o caminho
pelo que você quer usar:

```bash
python wk.pyz store init /caminho/do/store   # ou: store init (usa ./store)
```

Saída esperada:

```json
{
  "store": "C:\\caminho\\do\\store",
  "criados": ["inbox/transcripts", "inbox/agent-output", "inbox/code-notes",
              "inbox/clipped", "raw/transcripts", "raw/docs", "raw/code-notes",
              "raw/agent-output", "wiki", "log.md", "quarantine.md"],
  "ja_existiam": [],
  "dica": "aponte o agente para este store, ou WK_STORE=C:\\caminho\\do\\store"
}
```

**Como saber que deu certo:** `"criados"` lista as pastas na primeira
execução; rodando de novo, elas migram para `"ja_existiam"` (idempotente, não
duplica nem apaga nada). **Se falhar:** erro de permissão de disco — o comando
não trata isso, é `OSError` do Python; escolha um caminho onde você tem
permissão de escrita.

Na primeira conversa, diga ao agente onde o store está (👤 — você informa,
texto livre, não é comando):

```
O store do Wiki AI é ./store — inbox, raw e wiki ficam lá dentro.
```

`store init` não cria `wiki-docx/` — essa árvore só existe sob demanda: `wk
docx` a cria na primeira execução, em paralelo a `wiki/`, com os mesmos
documentos em formato `.docx` (para bibliotecas SharePoint/Copilot Studio,
que não aceitam `.md`). Ver `schema.md` §1 para a estrutura completa do store.

## 2.1. Permissão da engine sobre o store (obrigatório) — 👤 D

Sem este passo, o agente principal e os subagentes **não têm acesso** ao
store: `wk init` grava a permissão junto com a skill quando você passa
`--store`/`--repo`. Rode de novo (idempotente, mescla sem duplicar; mesmo
`cmd_init`, ainda D — grava JSON, não invoca modelo). Substitua os dois
caminhos pelos reais:

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

Confirme com (D — `cmd_check`, mesma natureza de leitura/comparação de disco):

```bash
python wk.pyz check --engine claude-code --store /caminho/do/store --repo /caminho/do/legado
```

Saída esperada (tudo certo, exit 0):

```json
{
  "alvos": [{ "dir": "...\\.claude\\skills\\wiki-ai", "iguais": ["SKILL.md"],
              "divergentes": [], "ausentes": [] }],
  "config_permissoes": [
    { "path": "...\\.claude\\settings.json", "ok": true, "problemas": [],
      "formato": "verificado" }
  ]
}
```

**Como saber que deu certo:** `"divergentes": []`, `"ausentes": []` e
`"problemas": []`; exit code `0`. **Se falhar:** `config_permissoes` reporta
`"ok": false` e a lista de problemas (`store ausente em
additionalDirectories`, `deny ausente`, `settings ausente`, etc.) — rode de
novo o `wk init --engine ... --store ... --repo ...` acima, ele mescla sem
duplicar. Se você pular este passo por completo, `run-stage` (Fluxo B) falha
rápido com `sem permissão de escrita em <workdir>/agent-outputs` — é o probe
determinístico que o `run-stage` roda antes de emitir qualquer manifesto para
subagente.

## 3. Testar — 👤 D

O `wk.pyz` é a única superfície de comando. Store default é `./store` (ou
`WK_STORE`). `index status` só lê `index.db` (SQLite) — sem LLM
(`scripts/sbindex/cli.py`, docstring do módulo linha 6: "audit — regras
DETERMINÍSTICAS ... sem LLM, sem vetor").

```bash
python wk.pyz index status
```

Saída esperada, store recém-criado:

```json
{
  "documentos": 0,
  "chunks": 0,
  "embedder": "none (lex-only)",
  "indice_sujo": []
}
```

**Como saber que deu certo:** o JSON responde sem erro e mostra
`"embedder": "none (lex-only)"` (modo padrão, sem Azure OpenAI configurado).
**Se falhar:** `ModuleNotFoundError: sbindex` — variável de ambiente/caminho
errado; confira que está rodando `python wk.pyz ...` a partir da raiz onde o
`.pyz` está, ou o caminho absoluto do `.pyz`.

> Nos documentos das operações, o comando aparece como `{{WK}}` — o `init`
> resolve esse marcador para a chamada real do executável na sua máquina.

---

# Fluxo A — Conhecimento (o principal)

## A1. Ingerir — 👤 H (classificar) + 🤖/👤 D (gravar)

```
/wiki-ai ingest ./transcricao.md
```

Isto dispara duas coisas de natureza diferente:

- **H — decisão humana:** `source_type`/`origin`/`topic` não têm resposta
  determinística; alguém (ou o agente perguntando a alguém) decide "isto é uma
  transcrição da Maria, sobre pagamentos". Dentro de uma sessão de engine, é o
  🤖 quem digita o comando abaixo, mas a classificação em si é julgamento
  humano — o agente propõe, você confirma.
- **D — execução:** uma vez decididos os três valores, `wk ingest`
  (`scripts/wk/cli.py`, função `cmd_ingest`) só converte e grava; não há
  chamada de modelo no caminho de código. Um humano roda exatamente o mesmo
  comando, sozinho, sem abrir engine nenhuma:

```bash
python wk.pyz ingest ./transcricao.md \
  --source-type human-transcript --origin "Maria, 1:1 26/07" --topic pagamentos
```

Saída esperada:

```json
{ "id": "sb-ingest-transcricao-a1b2c3d4", "path": "inbox/transcripts/sb-ingest-transcricao-a1b2c3d4.md", "source_type": "human-transcript" }
```

**Como saber que deu certo:** o arquivo aparece em `path` dentro do store, e
uma linha nova em `store/log.md`. **Se falhar:** `source_type inválido` lista
os valores válidos; `--origin não pode ser vazio` — preencha com quem/o quê é
a fonte.

`ingest` **é o único jeito** de gravar em `inbox/`: a permissão da engine (§2.1)
nega `Write`/`Edit` direto do agente dentro do store. O comando converte para
markdown e grava com proveniência (`source_type`, `origin`, `captured_at`,
`promoted: false`, `topic`), e registra em `log.md`.

Formatos aceitos: `.md`, `.txt` (direto), `.vtt`/`.srt` (timestamps viram
markdown), `.html`/`.htm` (tags removidas, headings preservados), `.xml`
(draw.io/XMI viram diagrama estruturado + Mermaid; outro XML vira outline +
bloco de código), `.json` (bloco de código anotado). Tudo isto é **D** — sem
LLM, texto entra e markdown sai, sempre igual para a mesma entrada.

`.docx`/`.xlsx`/`.csv`/`.pdf` **não são convertidos mecanicamente** — aqui
`ingest` ainda é D (grava o original em `raw/assets/<id><ext>` e um stub de
proveniência em `inbox/`), mas o passo seguinte é **M**: ler o conteúdo do
`.docx`/planilha e escrever a página de análise só um LLM faz (sem modelo, o
asset fica parado — nenhum comando "lê a planilha sozinho"). Exemplo do passo
M completo:

```
🤖 lê raw/assets/sb-ingest-relatorio-9f8e7d6c.xlsx, extrai os pontos-chave,
   escreve store/agent-analise-relatorio.md, então roda:
python wk.pyz ingest ./agent-analise-relatorio.md \
  --source-type agent-output --origin "análise de sb-ingest-relatorio-9f8e7d6c.xlsx" --topic pagamentos
```

Áudio, imagem e demais formatos seguem recusados como fora de escopo.

**`inbox/` não é indexado e não entra na wiki.** É estágio, não fonte.

## A2. Promover — 👤/🤖 D execução + 👤 H aprovação

```
/wiki-ai promote
```

Roda (**D** — `cmd_promote`, `scripts/wk/cli.py`, sem LLM: regras fixas de
auto-promoção/quarentena):

```bash
python wk.pyz promote --store "$WK_STORE"
```

Devolve três blocos: **Promovidos** · **Requer decisão humana** · **Quarentena**.
Só `code-repo` auto-promove — verificado em `cmd_promote`: `source_type ==
"code-repo"` é o único ramo que atribui `promoted_by`/`confidence` sem checar
aprovação. Os demais tipos ficam em "Requer decisão humana" até aprovação
explícita — **H, obrigatoriamente humana**: o guardrail #2 do `SKILL.md` proíbe
o agente de aprovar sozinho, mesmo que ele mesmo tenha rodado o `promote`.
Substitua `sb-2026-0142` pelo id real listado em "Requer decisão humana", e
`"Maria"` por quem está autorizando:

```bash
python wk.pyz promote --approve sb-2026-0142 --approved-by "Maria" --store "$WK_STORE"
# ou, para aprovar em lote por tipo:
python wk.pyz promote --approve-all --source-type human-transcript --approved-by "Maria" --store "$WK_STORE"
```

O arquivo migra para `store/raw/`, vira `promoted: true` /
`promoted_by: <quem aprovou>` / `confidence: reviewed` (`agent-output`
aprovado vira `confidence: unverified`, nunca `reviewed`, mesmo aprovado).

**Como saber que deu certo:** o item some de "Requer decisão humana" e
aparece com `promoted_by` preenchido; `store/log.md` ganha a linha de
aprovação. **Se falhar:** `verify_blocked` no JSON de saída — o `topic` desse
item tem um `verify` de codescan reprovado (ver B4); corrija ou decida
`--allow-unverified` (H, registrado no log).

## A3. Compilar — 👤/🤖 D

```
/wiki-ai compile
```

**D** — gera **uma página por documento promovido** em
`store/wiki/<topic>/<source_type>/<id>.md` (corpo verbatim, sem sintetizar
nem cruzar fontes) e atualiza `wiki/index.md` com a lista. Nenhuma síntese
aqui: é cópia de `raw/` formatada, mesma entrada sempre produz a mesma saída.
Cada página declara sua fonte única:

```yaml
sources: ["sb-001"]
```

Isso não é enfeite: é o que torna L1 e L5 verificáveis por SQL. Cruzamento
entre fontes acontece na leitura (`search`/`query`), não na compilação.

```bash
python wk.pyz compile --store "$WK_STORE"
```

**Como saber que deu certo:** o comando encerra com exit `0` e imprime a
contagem de páginas geradas; `wiki/index.md` lista o novo documento. **Se
falhar:** índice sujo bloqueia — rode `python wk.pyz index status` e, se
`indice_sujo` não estiver vazio, `python wk.pyz index reindex --store
"$WK_STORE"` antes.

O compile roda `reindex` sozinho ao final — não precisa rodar manualmente
depois.

## A4. Auditar — 👤/🤖 D (mecânico) + 🤖 M (semântico)

Barato, determinístico, roda sempre — **D**, `python wk.pyz index audit`
(`scripts/sbindex/cli.py`, "audit — regras DETERMINÍSTICAS ... sem LLM, sem
vetor"). Um humano roda isto sozinho, sem qualquer sessão de agente:

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

**Como saber que deu certo:** exit `0`, `"achados": 0`. **Se falhar:** exit
`1` e `report` aponta para `wiki/_lint-report.md` com a lista de linhas; cada
achado traz `ação sugerida`.

O lint semântico (L3 contradição, L4 realimentação) é **M** — julgar se duas
fontes se contradizem, ou se uma página resume outra sem citar (realimentação),
exige ler e comparar prosa; nenhum SQL faz isso. Sem modelo, L3/L4 não rodam —
não há substituto determinístico. Roda periodicamente (é caro):

```
/wiki-ai lint
```

🤖 lê as páginas candidatas, decide se há contradição/realimentação, e escreve
o achado em `wiki/_lint-report.md` via o mesmo comando `wk lint` (a gravação do
relatório continua D; o julgamento que preenche L3/L4 é M).

## A5. Consultar — 👤/🤖 D (busca) + 🤖 M (resposta)

```
/wiki-ai query como funciona o retry de pagamentos?
```

**D** — a busca em si é BM25 léxico determinístico
(`scripts/sbindex/store.py`, sem chamada de modelo). Um humano roda o mesmo
comando isolado, sem sessão de agente, para conferir o que está indexado:

Sem `--format json`, o comando devolve texto formatado para leitura humana, não
JSON (default de `--format` é `text`, no subparser `search` dentro de `main()`
em `scripts/sbindex/cli.py`). Para JSON, peça explicitamente:

```bash
printf 'intent: como funciona o retry\nlex: retry dead-letter\n' \
  | python wk.pyz index search -c wiki -n 5 --format json --store "$WK_STORE"
```

Saída real (capturada rodando o comando contra um store de teste):

```json
{
  "intent": "como funciona o retry",
  "searches": [{ "type": "lex", "query": "retry dead-letter", "weight": 2.0 }],
  "filters": { "collection": "wiki" },
  "results": [
    {
      "docid": "#946422",
      "path": "...\\wiki\\pagamentos\\human-doc\\sb-001.md",
      "collection": "wiki",
      "heading": "Retry de pagamentos",
      "source_type": "human-doc",
      "confidence": null,
      "score": 0.03279,
      "text": "[Retry de pagamentos]\n\nO sistema faz retry com dead-letter queue quando o pagamento falha 3 vezes."
    }
  ]
}
```

A chave de topo é `results` (não `hits`), e cada item traz `text` (não
`snippet`). Sem `--format json`, a mesma busca imprime texto:

```
#946422  0.0328  wiki  ...\wiki\pagamentos\human-doc\sb-001.md
    Retry de pagamentos
    [Retry de pagamentos]

    O sistema faz retry com dead-letter queue quando o pagamento falha 3 vezes.
```

**Como saber que deu certo:** `results` não vazio (modo JSON) ou pelo menos
uma linha com `#<docid>` (modo texto). **Se falhar (vazio, mas você sabe que
a página existe):** modo léxico casa string, não sentido — troque a palavra
ou use `OR`: `lex: "dead-letter" OR "DLQ" OR "fila de erro"`.

**M — a resposta em prosa que o slash command devolve** ("o retry funciona
assim: ...") é síntese: o LLM lê os `results` retornados pela busca D acima e
escreve a explicação. Sem modelo, você tem só os trechos brutos do `search` —
ninguém costura eles em resposta.

---

# Fluxo B — Codebase

> **Esta é a visão RESUMIDA.** O passo a passo completo de CLI — com
> `--store`, `run-stage`, `merge-agent-output`, `agent-runs` e a tabela de
> responsáveis (D/M/H, mesma taxonomia deste guia) de cada um dos 32
> subpassos — está em [README.md §4](README.md#4-runbook-de-codebase). Se for
> executar via CLI manual (fora do slash command `/wiki-ai ingest codebase`),
> comece por lá. Os comandos abaixo omitem `--store`/`$WK_STORE` por
> brevidade, mas ele é **obrigatório** em todo `wk code`/`wk publish` — sem
> `--store` e sem `WK_STORE` no ambiente, o comando recusa com `store não
> informado` (conferido em `scripts/codescan/cli.py`, função `main`,
> verificação que roda para *todo* subcomando de `wk code`, antes de
> despachar; README.md
> §11.2, "Erros comuns").

O legado é **READ-ONLY**. Nada é escrito dentro dele. Além das fontes para o
corpus, o pipeline gera a **árvore SDD** em
`store/.codescan/<repo>-<hash>/sdd/` — inventário, C4, ERD, specs por unit —
no contrato de `references/sdd-contract.md` (replica o Discovery do Reversa).

## B1. Surface + export — 👤/🤖 D (determinístico, sem LLM)

Substitua `/caminho/do/legado` pelo repositório real e `pagamentos` pelo
tópico. `surface`/`export` (`scripts/codescan/surface.py`,
`scripts/codescan/export.py`) só andam pelo filesystem/git do repo — nenhuma
chamada de modelo:

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  surface --topic pagamentos
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  export --topic pagamentos
```

`surface` devolve linguagens, LOC, manifests com dependências, entry points,
módulos, churn e autores. **Leia os `warnings`** — "sem git" significa que
você perdeu churn, que é o que aponta onde cavar. `export` materializa
`sdd/inventory.md`, `sdd/dependencies.md` e `sdd/coupling.md` (grafo de
acoplamento — motor Java especializado quando o repo tem Java, motor
genérico multi-linguagem caso contrário; com Java, também grava
`sdd/coupling.html` local para inspeção interativa), todos com proveniência
`code-repo`. `wk publish` leva também o `.html` para o corpus, como asset —
ver tabela de classificação abaixo.

**Como saber que deu certo:** `surface` termina com `"artifact":
".../.codescan/<repo>-<hash>/surface.json"` gravado (`cmd_surface`,
`scripts/codescan/cli.py`); `export` lista os três/quatro arquivos
gerados dentro de `sdd/`. **Se falhar:** `store não informado` — confira
`--store`/`$WK_STORE` (ver nota acima); repo inexistente — confira `--repo`.

Depois, registre as duas decisões de escopo — **H, decisão humana**
(`doc_level`/`granularity` não têm valor "correto" universal; trocam o
tamanho e a granularidade da árvore SDD gerada). O agente pergunta, não
decide sozinho; a gravação em si é **D** (`cmd_config`,
`scripts/codescan/cli.py`, só grava em `state.json`):

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  config --doc-level essencial --granularity module
```

`--doc-level` aceita `essencial`/`completo`; `--granularity` aceita
`module`/`file`. **Como saber que deu certo:** o JSON de saída ecoa os dois
valores gravados. **Se falhar:** `sem estado; rode surface primeiro` — rode
`surface` (acima) antes de `config`.

## B2. Planejar — 👤/🤖 D

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" plan --top 20
```

Ordena por LOC com churn e autores. **Churn alto + LOC alto = cave primeiro.**
`cmd_plan` (`scripts/codescan/cli.py`) só ordena dados que `surface` já
coletou — sem LLM. **Como saber que deu certo:** lista de módulos ordenada,
não vazia. **Se falhar:** `rode \`surface\` primeiro` — rode `surface` (B1)
antes.

## B3. Cavar — mistura D + M (aqui mora todo o M do Fluxo B)

```
/wiki-ai ingest codebase /caminho/do/legado --topic pagamentos
```

Este é o único passo do runbook onde `sem modelo o fluxo PARA` de verdade.
Decomposto:

| Sub-passo | Tipo | Quem | Por quê |
|---|---|---|---|
| `run-stage <stage>` — prepara manifesto de fan-out | **D** | 👤/🤖 | lê `state.json`/`surface.json`, monta lista de batches; sem LLM |
| conteúdo dos estágios `modules`, `rules`, `architecture`, `specs` (e `synth`, ver nota abaixo) | **M** | 🤖 obrigatório | é o subagente que lê o código-fonte e **escreve** a análise (regras de negócio, C4, ERD, requirements/design/tasks); nenhum comando gera essa prosa sozinho |
| `merge-agent-output <stage> --agent <id>` — integra a saída | **D** | 👤/🤖 | parseia/valida o texto que o subagente já escreveu; não gera conteúdo novo |
| `done <stage>` — marca concluído | **D** | 👤/🤖 | grava status em `state.json` |

O agente segue `operations/ingest-codebase.md`: lê `surface.json`, cava módulo
a módulo, extrai regras de negócio, e marca cada afirmação. Por baixo, esse
único slash command percorre a sequência inteira do README §4 —
`run-stage <stage>` (D) → subagentes 🤖 escrevem (M) →
`merge-agent-output <stage> --agent <id>` (D, um por batch) → `done <stage>`
(D) — para os estágios `modules`, `rules`, `architecture` e `specs`. Rodando
via CLI manual em vez do slash command, siga o README §4.5–4.18 passo a
passo; não existe atalho que pule `run-stage`/`merge-agent-output`/`done` por
estágio — e nenhum desses três comandos substitui o subagente escrevendo a
análise.

| Marca | Exige |
|---|---|
| 🟢 CONFIRMADO | `arquivo:linha` — sem exceção |
| 🟡 INFERIDO | justificativa |
| 🔴 GAP | pergunta ao humano (👤 H — o agente não resolve o gap sozinho) |

Na sequência, os estágios **architecture** (C4, ERD, integrações, dívida
técnica) e **specs** (requirements/design/tasks por unit, matrizes de
rastreabilidade, confidence-report) completam a árvore SDD — contrato e
templates em `references/sdd-contract.md`. Tracking por unit — **H** decide
quais units entram no escopo (`auth,orders,payments` é uma escolha de
negócio, não algo que se deduz do código sozinho); a gravação da lista é
**D** (`cmd_pending`, `scripts/codescan/cli.py`, só grava
`state.json`):

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  pending specs --items "auth,orders,payments"
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  done specs --item auth
```

**Como saber que deu certo:** o JSON de saída de `pending` ecoa a lista em
`items`; `done` move o item de `pending` para `done`. **Se falhar:**
`informe --items a,b,c` — a flag veio vazia.

Retomada após sessão morta — **D**, `cmd_next` só lê `state.json` e diz o
próximo comando, sem LLM:

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" next
```

## B3.5. Evidence pack — 👤/🤖 D

Antes da síntese, gere um pacote rastreável de evidências para o tópico — sem
subagente, sem fan-out (README §4.19):

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  evidence --topic pagamentos
```

`evidence` não tem `run-stage`/fan-out — roda direto, sem subagente.

O pacote fica em `store/.codescan/<repo>-<hash>/evidence-pagamentos.json`.
Ele é agnóstico: não usa parser nativo nem toolchain da linguagem. Serve para
dar contexto com `arquivo:linha` ao agente que vai escrever `sdd/confirmed.md`
e `sdd/inferred.md` (não existe `questions.md` no código; as perguntas ao
humano ficam dentro de `confidence-report.md`/`gaps.md` e da própria síntese).
**Como saber que deu certo:** o arquivo `evidence-<topic>.json` aparece no
workdir. **Se falhar:** "sem candidatos para o tópico" — confira `--topic`.

## B4. O resultado entra no fluxo A — 👤/🤖 D (publish/verify) + 👤 H (override)

`sdd/confirmed.md` e `sdd/inferred.md` são o local **canônico** da síntese
(nunca uma cópia solta na raiz do workdir — o `audit` reprova a divergência
como P0). `wk publish` (**D** — `cmd_publish`, `scripts/wk/cli.py`, move
arquivos e classifica por nome de caminho fixo; sem LLM) leva a árvore inteira
para `inbox/`, dividida por confiança:

```bash
python wk.pyz publish --workdir "$WK_STORE/.codescan/<repo>-<hash>" --topic pagamentos --store "$WK_STORE"
```

**Como saber que deu certo:** a saída lista os arquivos movidos para
`inbox/`, com `source_type` por arquivo (tabela abaixo). **Se falhar:**
`workdir não encontrado` — confira `--workdir` (o hash do nome de pasta vem
de `wk code ... state`).

| Arquivo | `source_type` (via `publish`) | Auto-promove no `promote`? |
|---|---|---|
| `sdd/confirmed.md` | `agent-output` | **não** — só `sdd/inventory.md`, `sdd/dependencies.md`, `sdd/coupling.md`, `sdd/coupling.html` viram `code-repo`; o resto da árvore, `confirmed.md` incluso, é `agent-output` |
| `sdd/inferred.md` | `agent-output` | **não** |
| `sdd/inventory.md`, `sdd/dependencies.md`, `sdd/coupling.md` | `code-repo` | **sim** — determinísticos, saem do `export` |
| `sdd/coupling.html` (só quando o motor Java roda) | `code-repo` | **sim** — publicado como **asset + stub**: bytes originais em `raw/assets/<doc_id>.html`, stub `.md` de proveniência (`doc_id` com sufixo `-html`, para não colidir com `coupling.md`). `wk docx` nunca converte este `.html` (pula, conta em `ignorados_asset_html`); `wk compile` copia o asset para `wiki/` e linka |
| demais artefatos `sdd/` (C4, ERD, specs...) e `modules/*.md` | `agent-output` | **não** — síntese de agente, mesmo citando código |

Não existe `questions.md` no código: as 🔴 (perguntas ao humano) ficam
registradas dentro de `confidence-report.md`/`gaps.md` (estágio specs) e do
próprio `sdd/confirmed.md`/`inferred.md` — não há artefato separado para elas.

`export --output` recusa gravar dentro de `store/raw` ou `store/wiki`
diretamente; o caminho para o corpus é sempre `publish` + `promote`, nunca
escrita manual nessas pastas.

Antes de publicar/promover `confirmed.md`, rode (**D** — `cmd_verify`,
`scripts/codescan/cli.py`: valida citação `arquivo:linha` contra o repo
real via regex/filesystem, nenhum LLM envolvido, docstring da própria função
confirma "não tenta provar semântica"):

```bash
python wk.pyz code --repo /caminho/do/legado --store "$WK_STORE" \
  verify --artifact "$WK_STORE/.codescan/<repo>-<hash>/sdd/confirmed.md"
```

Saída esperada (falha, para ilustrar): `{"ok": false, "claims": 4,
"green_claims": 3, "errors": [{"line": 12, "rule": "claim_sem_evidencia",
"detail": "claim verde em bullet sem citacao arquivo:linha valida"}]}`.
**Como saber que deu certo:** `"ok": true`, `"errors": []`, exit `0`. **Se
falhar:** exit `1` — rebaixe
a claim para `inferred.md` ou transforme em pergunta (🔴) antes de promover
como `code-repo`. `verify` não prova que a análise está semanticamente
perfeita; ele só bloqueia a classe mais perigosa de erro: afirmação 🟢 sem
evidência `arquivo:linha`.

**`verify` falho é gate real em `promote`/`compile`/`docx`** (não em
`publish`, que só anota `aviso_verify` — nada em `inbox/` é canônico
ainda). O bloqueio é por `topic`: em `promote`, item a item, cobrindo
também os artefatos `code-repo` do mesmo tópico (`inventory.md`/
`dependencies.md`/`coupling.md`/`coupling.html`); em `compile`/`docx`, pelo
`topic` do próprio comando. Para seguir mesmo assim: **H, decisão humana
explícita e obrigatória** — `--allow-unverified` (aceito por
`promote`/`compile`/`docx`, registrado em `log.md`). O agente **nunca** decide
isso sozinho; ele expõe o bloqueio e pergunta. Exemplo (substitua o topic):

```bash
python wk.pyz compile pagamentos --store "$WK_STORE" --allow-unverified
```

Detalhe completo: README.md §4.24 e §13 ("Gates").

Depois: `publish`, `promote` e `compile` normais (todos **D** na execução;
`promote` de itens não-`code-repo` continua exigindo aprovação **H**, igual
A2).

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

- `lex:` funciona. `vec:` e `hyde:` **dão erro explícito, com exit code `2`**
  — de propósito, nunca `results: []` silencioso. Comportamento real,
  confirmado rodando `index search` com `vec:`/`hyde:` sem
  `AZURE_OPENAI_*` configurado (`scripts/sbindex/cli.py`, função `cmd_search`:
  checa `emb.available` **antes** de tentar embedar; se `False`, nem chega a
  montar `results`):

  ```json
  {"error": "vec/hyde exigem embeddings; índice em modo léxico", "hint": "use apenas lex:, ou configure AZURE_OPENAI_*"}
  ```

  Falha silenciosa é o inimigo. O aviso que `index reindex` imprime nesse
  cenário (`cmd_reindex`, mesmo arquivo) concorda com isto: "vec/hyde exigem
  embeddings e falham com erro (exit 2) nesse modo; apenas lex funciona."
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
