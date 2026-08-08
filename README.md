# Wiki AI

## Sumário — comece aqui

Este documento tem 17 seções numeradas + material de referência. Ache seu
caso abaixo em vez de rolar a página inteira.

**Ainda não instalei nada:**
[0. Terminal](#sec-0) · [1. Setup](#sec-1)

**Quero ingerir conhecimento novo:**
- Transcrição, ata, nota de texto → [2. Ingestão de transcrição/nota](#sec-2)
- Documento, planilha ou PDF (`.docx`/`.xlsx`/`.csv`/`.pdf`) → [3. Ingestão de doc/planilha/PDF](#sec-3)
- Repositório de código (pipeline completo) → [4. Runbook de codebase](#sec-4)

**Já ingeri, quero publicar/governar:**
- Aprovar o que entra na fonte-verdade → [5. Promoção / governança](#sec-5)
- Gerar a wiki em Markdown → [6. Compilação da wiki](#sec-6)
- Gerar `.docx` para bibliotecas SharePoint → [7. Exportação `.docx`](#sec-7)
- Auditar integridade (links, proveniência) → [8. Lint e auditoria](#sec-8)

**Quero consultar o que já existe:**
[9. Indexação e busca (retrieval)](#sec-9)

**🚨 Travei / deu erro / perdi o fio no meio de um fluxo:**
[11. Retomada de fluxo interrompido e recuperação de erro](#sec-11) — leia
isto primeiro se algo já rodou e parou.

**Uso `/wiki-ai ...` (slash command), não a CLI direto:**
[10. Modo skill (slash commands)](#sec-10)

**Referência / consulta pontual, não é passo a passo:**
[12. Contratos de bloco por estágio](#sec-12) ·
[13. Gates](#sec-13) ·
[14. Proibido](#sec-14) ·
[15. Referência rápida de comandos](#sec-15) ·
[16. Glossário](#sec-16) ·
[17. Limitações conhecidas](#sec-17)

**Quero entender o sistema antes de rodar qualquer comando:**
[O que é](#sec-oque-e) · [Arquitetura e fluxo](#sec-arquitetura) ·
[Mapa de papéis do sistema](#sec-mapa-papeis) (a taxonomia D/M/H usada em
todo o documento — leia uma vez; se já conhece, [pule direto para o
Setup](#sec-1)).

<a id="sec-oque-e"></a>
## O que é

Wiki AI é a ferramenta que transforma fontes soltas — transcrições,
documentos, planilhas, XML de arquitetura e repositórios de código legado —
numa wiki corporativa curada, com proveniência rastreável por fonte. Serve
times que precisam manter conhecimento de negócio e de sistemas legados
navegável e citável, sem deixar saída de LLM virar fato sem revisão. Entrega
três coisas: páginas Markdown em `wiki/` (fonte-verdade legível), a mesma
wiki em `.docx` (`wiki-docx/`) para bibliotecas SharePoint consumidas pelo
Copilot Studio, e, quando a fonte é um repositório, uma árvore de artefatos
SDD (inventário, C4, ERD, specs por unit) sob `.codescan/`.

<a id="sec-arquitetura"></a>
## Arquitetura e fluxo

```
fonte externa ──ingest/publish──> inbox/ (não-verificado)
                                     │
                                  promote
                                     ▼
                                   raw/ (fonte-verdade; confidence/promoted_by)
                                     │
                         ┌───────────┴───────────┐
                      compile                   docx
                         ▼                         ▼
                      wiki/                   wiki-docx/
                (gerado, .md, reproduzível)  (gerado, .docx, p/ SharePoint)
```

- **Store**: raiz de dados do Wiki AI, separada da skill (`INSTALL.md` §2).
  Contém `inbox/`, `raw/`, `wiki/`, `wiki-docx/`, `log.md`, `quarantine.md`.
  Estrutura normativa completa em [schema.md](schema.md) §1.
- **Proveniência**: todo arquivo em `inbox/`/`raw/` carrega `source_type`,
  `confidence` e `origin` no frontmatter ([schema.md](schema.md) §2).
  `source_type: agent-output` nunca vira `confidence: reviewed`, nem
  aprovado manualmente — vira `unverified` para sempre, e `origin` preserva
  a origem de agente (`schema.md` §2 "Proveniência (frontmatter
  obrigatório)"). É o que impede
  saída de LLM de virar fato canônico sem revisão humana.
- **Promoção**: `inbox/` é entrada não-verificada; `promote` é o único
  portão para `raw/`. `code-repo` auto-promove; os demais tipos exigem
  `--approve`/`--approve-all` explícito ([schema.md](schema.md) §3).
- **Gerado vs. fonte-verdade**: `wiki/` e `wiki-docx/` são derivados de
  `raw/` — nunca edite à mão (`INSTALL.md`: "Página some após compile:
  correto: a wiki é derivada"). `compile` reescreve cada página a partir de
  `raw/` a cada rodada; `wk docx` além disso poda `.docx` órfão por padrão.
- **`wiki-docx/`**: existe porque o conector SharePoint do Copilot Studio
  não aceita `.md` como formato de biblioteca de documentos — só
  DOC/DOCX, PPT/PPTX e PDF (`operations/docx.md` §"Por que este comando
  existe"). `wk store init` não cria essa árvore; ela só existe sob demanda,
  na primeira execução de `wk docx` (`INSTALL.md` §2 "Criar o store").
- **Índice (`index.db`)**: derivado de `raw/` + `wiki/`, reconstruível e
  deletável ([schema.md](schema.md) §7). `status` sujo bloqueia `compile` e
  `lint`; `reindex` destrava.
- **Fan-out de subagentes**: os estágios `modules`, `rules`, `architecture`,
  `specs` e `synth` do pipeline de código exigem subagente — o orquestrador
  nunca gera conteúdo SDD diretamente. Existe por dois motivos: limite de
  contexto (cada subagente lê só o material do próprio batch, não o
  workdir inteiro) e isolamento da leitura do repo (cada batch grava o
  próprio arquivo e devolve só um recibo de 3 linhas, nunca o conteúdo).
  `run-stage` informa `fanout_required`; `merge-agent-output` recusa
  `--agent` ausente ou genérico (`SKILL.md` §"Guardrails invioláveis").
- **codescan/SDD**: ao rodar o pipeline sobre um repositório, o Wiki AI cria
  um workdir isolado em `store/.codescan/<repo>-<hash>` (hash derivado do
  caminho ou URL do repo). Os estágios seguem a ordem
  `surface → modules → rules → architecture → specs → evidence → synth →
  verify`; ao final, `publish` leva a árvore `sdd/**` e `modules/*.md` para
  `inbox/`.

Visão geral em 5 linhas: uma fonte entra por `ingest` (arquivo solto, seções
2/3) ou `publish` (saída do pipeline de código, seção 4) em `inbox/`;
`promote` (seção 5) decide o que vira fonte-verdade em `raw/`; `compile`
(seção 6) gera `wiki/` e `docx` (seção 7) gera `wiki-docx/`, os dois a
partir de `raw/`; `lint` (seção 8) fecha o runbook auditando o índice.

<a id="sec-como-usar"></a>
## Como usar este documento

Runbook de execução. Leia de cima para baixo e execute na ordem — cada passo
diz **quem** executa, o comando, o que esperar e o que fazer se falhar. Não
pule passos.

**Dois modos de uso.** Seções 1–9 são o modo CLI (você digita cada comando,
com ou sem um modelo, dependendo da classificação de cada passo — ver
[Mapa de papéis do sistema](#sec-mapa-papeis) logo abaixo). A **seção 10** é
o modo skill (`/wiki-ai ingest codebase ...`), em que um agente dispara o
mesmo pipeline por você. Se seu caminho é o slash command, vá direto para
[10. Modo skill](#sec-10) — ela mapeia cada operação da skill ao comando
CLI equivalente. (A seção 10 fica fisicamente perto do fim do documento, de
propósito — a ordem numérica 1→17 segue a ordem de leitura linear do
runbook CLI; mover §10 para cima quebraria essa ordem sem necessidade,
já que o link acima leva lá em um clique.)

Regra de contexto: `wk code` imprime o corpo completo por padrão. `--quiet` é
opt-in: condensa a saída a uma única linha JSON de resumo
(`{"ok":true,"cmd":"<subcomando>", ...campos condensados...}`) — listas viram
`<chave>_count`, dicts viram `<chave>_keys`. `--verbose` é aceito só por
compatibilidade (no-op explícito; o corpo completo já é o padrão). Ambas as
flags valem antes ou depois do subcomando. O manifesto completo sempre é
gravado em disco antes da impressão. A economia de contexto real é outra: o
subagente grava o próprio arquivo de saída e devolve só o recibo de 3 linhas
(`ARQUIVO:`/`BLOCOS:`/`BYTES:`).

<a id="sec-mapa-papeis"></a>
## Mapa de papéis do sistema

> **Já conhece a taxonomia D/M/H?** Pule direto para [1. Setup](#sec-1) — o
> resto desta seção é referência, não é um passo que se executa.

Leia esta seção antes de qualquer runbook. Ela responde, de relance: "o que
eu consigo fazer sozinho, e onde eu preciso de um modelo?"

### Só existem duas personas

`wk`, `wk.pyz`, scripts em `scripts/`, `codescan` — nenhum deles é um ator.
São **ferramentas**: alguém as invoca. Só existem duas personas neste
sistema, e toda linha de "quem executa" deste documento usa exatamente um
destes dois marcadores (ou os dois, quando o passo é misto):

| Marcador | Persona | Significado |
|---|---|---|
| 👤 | **HUMANO** | uma pessoa digita o comando no terminal (ou aprova/decide algo que um agente executa por ela) |
| 🤖 | **MODELO** | um agente LLM — subagente de fan-out, ou o orquestrador do modo skill |

Nunca escreva "o script faz X" ou "responsável: script". Um comando não age
sozinho — sempre há um 👤 ou um 🤖 do outro lado do terminal disparando-o.
Onde este documento antes escrevia "🔧 script" como sujeito da frase
("script materializa a skill", "script fecha o gate"), a formulação correta
é: "👤 humano executa `wk init` — a ferramenta materializa a skill." A partir
daqui, todo passo é classificado por **quem pode/deve executá-lo** e **se
precisa de LLM**, não pelo nome do comando.

### Classificação obrigatória de todo passo

| Tipo | Nome | Quem executa | Precisa de LLM? |
|---|---|---|---|
| **D** | Determinístico | 👤 ou 🤖 — indiferente | **NÃO.** Mesma entrada → mesma saída. O humano faz sozinho, sem depender de nenhum modelo. |
| **M** | Requer modelo | 🤖 obrigatoriamente | **SIM.** Só um LLM produz a saída. Sem modelo, o fluxo PARA nesse ponto. |
| **H** | Decisão humana | 👤 obrigatoriamente | **NÃO**, mas exige julgamento, autorização ou responsabilidade — não é técnico, é governança. |

Regras de leitura que valem para o documento inteiro:

1. Todo passo **D** declara explicitamente, no próprio passo, que o humano
   pode executá-lo sozinho, sem modelo — e aponta o arquivo/linha do código
   que prova a ausência de chamada a LLM.
2. Todo passo **M** declara o que o modelo LÊ e o que ESCREVE, e por que um
   humano não faz isso mecanicamente — quando um humano *conseguiria*
   produzir o mesmo resultado escrevendo à mão, isto é dito explicitamente
   (a diferença entre "impossível sem modelo" e "possível, mas manual e
   lento").
3. Todo passo **H** declara o que está sendo decidido e o risco concreto de
   decidir errado.
4. Passo misto (ex.: `promote` durante o pipeline de código) traz os dois
   marcadores, na ordem em que as sub-ações acontecem.

### Mapa de papéis por fluxo

| Fluxo | Passos D (humano sozinho) | Passos M (exige modelo) | Passos H (decisão) | Dá para completar sem modelo? |
|---|---|---|---|---|
| 1. Setup e instalação (§1) | 5/5 | 0 | 0 | **Sim, sempre.** |
| 2. Ingestão de transcrição/nota (§2) | 5/6 (ingest, promote, compile, docx, lint) | 0 | 1/6 (aprovar promoção) | **Sim, sempre.** |
| 3. Ingestão de doc/planilha/PDF (§3) | 6/8 (ingest asset, ingest análise, promote, compile, docx, lint) | 1/8 (escrever a análise do asset) | 1/8 (aprovar promoção) | **Parcial** — publica o stub sem modelo; a leitura/análise do conteúdo do asset exige M. |
| 4. Ingestão de codebase (§4) | 31/32 subpassos | 5/32 (os 5 pontos de fan-out) | 4/32 (config, pending specs, redo, promote não-`code-repo`) | **Não.** Os 5 pontos de fan-out são obrigatórios — sem modelo o pipeline trava a partir de `run-stage modules`. |
| 5. Promoção/governança (§5) | 2-3 (varredura, execução da promoção, reindex) | 0 | 1-2 (aprovar, tratar quarentena) | **Sim, sempre.** `promote` nunca chama LLM. |
| 6. Compilação da wiki (§6) | 1 (+ reindex automático) | 0 | 0-1 (`--allow-unverified`) | **Sim, sempre.** |
| 7. Exportação `.docx` (§7) | 1 | 0 | 0-1 (`--allow-unverified`) | **Sim, sempre.** |
| 8. Lint e auditoria (§8) | Nível 1 + grafo (W1-W3): sempre D | 0 | Nível 2 semântico: leitura/decisão do candidato | **Sim, sempre.** Nível 2 é tedioso sem ajuda, mas não é bloqueado por ausência de modelo. |
| 9. Indexação e busca (§9) | reindex, status, get, `search` em modo `lex:` | 0 (retrieval, não geração) | 0 | **Sim**, no modo padrão `lex:`. `vec:`/`hyde:` dependem de um serviço de embeddings externo, não validado neste projeto (§17) — não confundir com "exige LLM escrevendo conteúdo": é apenas indisponível hoje. |
| 10. Modo skill (§10) | comandos `wk` disparados pelo agente | o agente inteiro é 🤖 disparando o pipeline | aprovação de `promote` continua humana mesmo aqui | **Não, por definição** — é o modo em que um modelo conduz. Para 100% sem modelo, use o modo CLI (seções 1-9). |
| 11. Retomada/erro (§11) | `state`/`next`/`read`/`audit`, a maioria das correções | correção quando a causa é saída de subagente inválida | decidir `redo` | **Sim, na maioria dos casos.** Só exige M quando o erro está no conteúdo que o subagente escreveu. |

### O que é 100% humano — sem nenhum modelo, do início ao fim

Verificado por leitura de código (`scripts/wk/cli.py`, `scripts/sbindex/cli.py`):
nenhum destes comandos chama uma API de LLM, `subprocess`, ou qualquer
dependência de rede para produzir conteúdo. Você pode rodar cada um destes
fluxos inteiros sozinho, no terminal, sem abrir um agente:

- **Setup completo** (§1): `wk init` → `wk store init` → `wk doctor`.
- **Ingestão + publicação de uma transcrição ou nota de texto** (§2):
  `wk ingest` → `wk promote --approve` → `wk compile` → `wk docx` →
  `wk lint`. Nenhum desses 5 comandos gera conteúdo por LLM — o corpo da
  página final é o texto que você já ingeriu, verbatim.
- **Governança de conteúdo já ingerido** (§5): decidir o que promove, o que
  fica em quarentena, o que exige `--allow-unverified` — tudo isso é
  leitura de JSON e julgamento humano, sem LLM.
- **Compilar, exportar e auditar qualquer conteúdo já promovido** (§6, §7,
  §8): `wk compile` + `wk docx` + `wk lint`, em qualquer ordem, quantas
  vezes quiser — determinístico, idempotente, sem custo de modelo.
- **Buscar na wiki** (§9): `wk search "<consulta>"` sem prefixo cai
  automaticamente em `lex:` (BM25 léxico), sem embeddings, sem LLM.
- **Diagnosticar e retomar um pipeline parado** (§11): `state`/`next`/
  `read`/`audit` são sempre D — só a correção do *conteúdo* de um estágio
  específico (quando o problema é o texto que um subagente escreveu) exige
  voltar a chamar um modelo.

O único fluxo que **não tem** caminho 100% humano é a **ingestão de
codebase** (§4): os 5 pontos de fan-out (`modules`, `rules`, `architecture`,
`specs`, `synth`) exigem um LLM lendo o pacote determinístico e escrevendo o
artefato — não existe atalho determinístico para gerar a leitura
operacional de um módulo de código. A ingestão de **doc/planilha/PDF** (§3)
é parcial: o asset em si sobe sem modelo, mas a *análise legível* do
conteúdo exige um LLM (ou um humano disposto a escrever a análise à mão,
o que o comando não impede, só não automatiza).

<a id="sec-0"></a>
## 0. Terminal

Use Git Bash. Não use PowerShell — a sintaxe deste runbook (`export`, aspas,
`&&`) quebra em PowerShell e o erro aparece truncado/deformado. Se você
suspeita estar em PowerShell, embrulhe o comando: `bash -c '...'`.

<a id="sec-1"></a>
## 1. Setup (uma vez por máquina)

**Classificação da seção: 100% D.** Nenhum dos 5 passos abaixo chama um LLM
— `cmd_init`, `cmd_check`, `cmd_doctor` (`scripts/wk/cli.py`)
só leem/escrevem arquivos e validam argumentos. Um humano faz o setup
inteiro sozinho, sem abrir nenhum agente.

> 🚨 Travou em algum passo desta seção? [11. Retomada de fluxo interrompido
> e recuperação de erro](#sec-11) tem o diagnóstico geral.

1.1. Abra um terminal Git Bash.

Quem executa: 👤 humano. Tipo **D** — não há comando `wk` aqui, é ação do
sistema operacional.

1.2. Defina as variáveis de execução:

Quem executa: 👤 humano. Tipo **D** — `export` é do shell, não do `wk`.

Os quatro caminhos abaixo são **exemplo** — troque pelos do seu ambiente
(onde está o `python.exe`, onde está o `wk.pyz`, onde fica o seu store e o
seu repositório):

```bash
export WKPY='/c/Users/User/AppData/Local/Programs/Python/Python313/python.exe'
export WK='C:/Users/User/Downloads/files/second-brain/wk.pyz'
export WK_STORE='C:/Users/User/OneDrive/Documentos/projetos/wiki/store'
export WK_REPO='C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service'
```

`WK_STORE`/`WK_REPO` já vão aqui porque os passos 1.3 e 1.5, logo abaixo,
dependem deles — defini-los só na seção 4 deixa `--store`/`--repo` vazios
neste ponto do runbook.

Esperado: nenhuma saída (só `export`). Como saber que deu certo: rode
`echo "$WK_STORE"` — se ecoar o caminho, a variável está definida na sessão.
Se falhar: `WKPY`/`WK`/`WK_STORE`/`WK_REPO` apontam para caminho errado —
confira com `ls "$WKPY"`, `ls "$WK"`, `ls "$WK_STORE"` e `ls "$WK_REPO"`.

1.3. Materialize a skill e grave as permissões da engine sobre store/repo:

Quem executa: 👤 humano digita o comando. Tipo **D** — `cmd_init`
(`scripts/wk/cli.py`) copia arquivos embutidos e escreve JSON de
permissão; não há chamada de rede nem de LLM em nenhum ramo da função.

```bash
$WKPY "$WK" init --engine devin --store "$WK_STORE" --repo "$WK_REPO"
```

Esperado: JSON com `"engines"`, `"alvos"` (diretórios escritos) e
`"permissoes"` (settings.json atualizado com `additionalDirectories`/`deny`),
um item por arquivo, com `"formato"`: `verificado` só para `claude-code`
(schema real de `.claude/settings.json`, comprovadamente consumido pela
engine); `best-effort` para antigravity/devin/copilot (`.agents/settings.json`
é gravado, mas nada garante que a engine o consuma).

Exemplo de saída (resumida):
```json
{"ok": true, "cmd": "init", "engines": ["devin"],
 "alvos": [".agents/skills/wiki-ai/SKILL.md"],
 "permissoes": [{"engine": "devin", "formato": "best-effort"}]}
```

Como saber que deu certo: `"ok": true` e a lista `"alvos"` não vazia.

Se falhar: erro `existente e diferente; use --force` — repita com `--force`
só se a divergência for esperada (ex.: versão nova do `wk`).

1.4. Crie a estrutura do store:

Quem executa: 👤 humano. Tipo **D** — só cria diretórios (`STORE_TREE`,
`scripts/wk/cli.py`); idempotente.

```bash
$WKPY "$WK" store init "$WK_STORE"
```

Esperado: JSON com `"criados"`/`"ja_existiam"` listando `inbox/`, `raw/`,
`wiki/`, `log.md`, `quarantine.md`. Idempotente — rodar de novo não apaga
nada. Não cria `wiki-docx/` — essa árvore só existe depois do primeiro
`wk docx` (seção 7), sob demanda.

Como saber que deu certo: `ls "$WK_STORE"` mostra `inbox raw wiki log.md
quarantine.md`.

Se falhar: caminho de `$WK_STORE` inválido (ex.: unidade não montada).

1.5. Verificação final — rode `wk doctor` antes de qualquer outra coisa:

Quem executa: 👤 humano. Tipo **D** — `cmd_doctor` (`scripts/wk/cli.py`)
só lê ambiente/arquivos e monta um relatório; nenhuma escrita além de
diagnóstico.

```bash
$WKPY "$WK" doctor --store "$WK_STORE" --repo "$WK_REPO" --engine devin
```

Esperado: `"bloqueios": []` e `"proximo_passo": "ambiente ok — nenhuma ação
necessária"` (ou já apontando `wk code --repo ... --store ... surface
--topic ...`, o que também é sucesso) — exit `0`. Invariante: exit `0` se e
somente se `"bloqueios"` estiver vazio. Engine desconhecida entra em
`"bloqueios"` (`"engine"`) e força exit != 0; `proximo_passo` nunca ecoa a
engine inválida de volta — só lista as engines válidas e o `init` corrigido.
Com `--store`/`--repo`, `engine.permissao_garantida` só é `true` para
`claude-code` (formato `verificado`); para antigravity/devin/copilot fica
`false` mesmo com o arquivo gravado corretamente (formato `best-effort` —
`aviso_permissao` explica).

Como saber que deu certo: `echo $?` depois do comando mostra `0`.

Se falhar: `"bloqueios"` não vazio — `proximo_passo` traz o comando exato de
correção (reexecutar `init`, corrigir `--repo`, ou `wk store init`). Não use
`--help` para descobrir o ambiente; `doctor` substitui essa fase.

`wk.pyz` desatualizado (diagnóstico, não portão): quando o código-fonte
(`scripts/`) está disponível ao lado do `.pyz`, `doctor` compara o
`source_sha256` embutido em `wk/_build_manifest.json` contra um hash
recalculado do `scripts/` em disco e reporta `wk.pyz.pyz_desatualizado`
(`_check_pyz_freshness`, `scripts/wk/cli.py`). `true` → `"acao": "rode
python scripts/build_pyz.py"`. Isso **nunca** entra em `"bloqueios"` nem
muda o exit code — é só um aviso; degrada em silêncio (sem erro) quando não
há `.pyz` (código-fonte solto) ou não há `scripts/` ao lado do `.pyz`. A
recompilação (`python scripts/build_pyz.py`) é responsabilidade de quem
mantém o `wk.pyz`, não deste README.

<a id="sec-2"></a>
## 2. Ingestão de transcrição / nota em texto

**Classificação da seção: praticamente 100% D+H — nenhum passo exige LLM.**
Fluxo completo: `ingest` (D) → decisão de promover (H) → `promote` (D) →
`compile` (D) → `docx` (D) → `lint` (D). Cobre `.md`, `.txt`, `.vtt`, `.srt`
(transcrição com timestamps preservados) e `.html`/`.htm`. Verificado em
`cmd_ingest` (`scripts/wk/cli.py`) — a função só valida argumentos,
converte o arquivo com `_convert_to_markdown` (troca de formato de texto,
sem chamada de rede) e grava frontmatter; nenhum ramo chama LLM.

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 2.1. `ingest` — registra a transcrição/nota em `inbox/`

Quem executa: 👤 humano digita o comando. Tipo **D** — comprovado: nenhuma
importação de cliente HTTP/LLM em `cmd_ingest`; a conversão de `.vtt`/`.srt`
é troca de string (preserva `**HH:MM:SS.mmm --> HH:MM:SS.mmm**` como texto).

```bash
$WKPY "$WK" ingest "C:/Users/User/Downloads/reuniao-2026-08-05.vtt" \
  --source-type human-transcript \
  --origin "reunião de status — squad Faturamento, 05/08/2026" \
  --topic "projetos/migracao-erp"
```

Esperado (JSON):
```json
{"id": "sb-ingest-reuniao-2026-08-05-a1b2c3d4",
 "path": "inbox/transcripts/sb-ingest-reuniao-2026-08-05-a1b2c3d4.md",
 "source_type": "human-transcript"}
```

Como saber que deu certo: o arquivo existe em
`$WK_STORE/inbox/transcripts/`, com frontmatter `promoted: false`.

Se falhar: sem `--origin` (vazio) ou `--source-type` inválido, o comando
recusa **antes de escrever qualquer coisa** e imprime `"error"` em stderr
com `"validos"` listando os 5 valores aceitos (`human-transcript`,
`human-doc`, `code-repo`, `agent-output`, `web-clip`). Formato fora de
escopo (áudio, imagem, `.pptx`) → `"formato fora de escopo"`; não há OCR
nem transcrição automática neste comando (ver §17, fluxos ausentes).

### 2.2. Promover, compilar, exportar, auditar

Quem decide: 👤 humano. Tipo **H** — `human-transcript` nunca auto-promove
(regra de `source_type`, `operations/promote.md`); decisão: aprovar esta
transcrição como fonte-verdade. Risco de decidir errado: promover uma
transcrição com erro de transcrição, participante mal identificado ou
trecho fora de contexto grava `confidence: reviewed` — vira citável na wiki
como se fosse fato revisado.

```bash
$WKPY "$WK" promote --approve "sb-ingest-reuniao-2026-08-05-a1b2c3d4" \
  --approved-by "Maria" --store "$WK_STORE"
$WKPY "$WK" compile "projetos/migracao-erp" --store "$WK_STORE"
$WKPY "$WK" docx "projetos/migracao-erp" --store "$WK_STORE"
$WKPY "$WK" lint --store "$WK_STORE"
```

Os quatro comandos acima são **D** — detalhe completo, exemplos e critérios
de sucesso/falha em §5 (promote), §6 (compile), §7 (docx) e §8 (lint); esta
seção só amarra a sequência específica de transcrição/nota.

<a id="sec-3"></a>
## 3. Ingestão de documento, planilha ou PDF (`.docx`/`.xlsx`/`.csv`/`.pdf`)

**A fronteira D/M mais importante deste fluxo:** o comando `wk ingest`
sobe o arquivo (D, sem LLM). A **análise do conteúdo** — abrir a planilha,
extrair os pontos-chave, redigir uma página legível — é **M**, obrigatória
para o conteúdo virar prosa navegável na wiki. Sem esse passo M, você
publica o **asset original** (arquivo bruto navegável, com link de
proveniência), não um resumo.

Verificado em `cmd_ingest` (`scripts/wk/cli.py`, ramo `_INGEST_ASSET_EXTS`)
e em `operations/ingest.md` (§ "Docs e planilhas: asset + análise"): não há
extrator de texto embutido para esses 4 formatos — nenhuma biblioteca de
parsing de `.xlsx`/`.docx`/`.pdf`/`.csv` é chamada dentro de `cmd_ingest`
para produzir prosa; o comando só copia bytes.

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 3.1. `ingest` do asset — grava o original + a página de fonte (stub)

Quem executa: 👤 humano. Tipo **D** — `_ingest_asset` grava o binário
original em `raw/assets/<id><ext>` (fora do portão de `promote`, é material
de apoio) e cria em `inbox/` um stub com frontmatter de proveniência,
apontando para o asset. Nenhuma conversão de conteúdo acontece aqui.

```bash
$WKPY "$WK" ingest "C:/Users/User/Downloads/planilha-custos-2026.xlsx" \
  --source-type human-doc \
  --origin "planilha de custos consolidados — Financeiro, T2 2026" \
  --topic "financeiro/custos-2026"
```

Esperado:
```json
{"id": "sb-ingest-planilha-custos-2026-e5f6a7b8",
 "path": "inbox/transcripts/sb-ingest-planilha-custos-2026-e5f6a7b8.md",
 "source_type": "human-doc"}
```

Como saber que deu certo: dois arquivos existem —
`$WK_STORE/raw/assets/sb-ingest-planilha-custos-2026-e5f6a7b8.xlsx`
(o original, intocado) e o stub em `inbox/transcripts/`.

Se falhar: mesmos casos da seção 2.1 (source-type/origin/formato). Um
detalhe específico deste fluxo: `wk ingest` recusa `.docx` marcado
`dc:identifier = wk-docx-gerado` — não deixa reingerir um `.docx` que o
próprio `wk docx` gerou (proveniência circular); a mensagem aponta para o
`.md` correspondente em `raw/`.

**Ponto de decisão sem modelo:** se você só precisa publicar o arquivo
original navegável (sem resumo em prosa), pode pular direto para §5
(promote do stub) — `human-doc` exige `--approve`, mas nenhum modelo é
necessário. Você só perde a página de análise legível.

### 3.2. Análise do asset — obrigatoriamente 🤖

Quem executa: 🤖 modelo. Tipo **M** — sem um LLM aqui, não existe versão em
prosa do conteúdo da planilha/PDF/docx na wiki, só o stub linkando o
arquivo bruto.

- Modelo LÊ: o asset original em `raw/assets/<id>.xlsx` (ou `.pdf`/`.docx`/
  `.csv`), e o stub em `inbox/` para saber `id`/`topic`/`origin`.
- Modelo ESCREVE: um arquivo Markdown novo, em disco, com os pontos-chave
  extraídos — tabelas resumidas, números relevantes, achados. Sem template
  fixo imposto pelo `wk` (o comando de destino, 3.3, só valida
  `source_type`/`origin`, não a qualidade da análise).

Por que um humano não faz isso mecanicamente: ler uma planilha de custos e
redigir prosa estruturada exige interpretação — mesma limitação de "resumir
um documento" em qualquer contexto. Um humano **consegue** fazer à mão
(abrir o Excel, escrever a página), só não é automatizável sem LLM; o `wk`
não tenta.

### 3.3. `ingest` da análise — registra o output do modelo como `agent-output`

Quem executa: 👤 humano (ou o agente, no modo skill — seção 10) digita o
comando. Tipo **D**.

```bash
$WKPY "$WK" ingest "./analise-planilha-custos-2026.md" \
  --source-type agent-output \
  --origin "análise de sb-ingest-planilha-custos-2026-e5f6a7b8" \
  --topic "financeiro/custos-2026"
```

Esperado: mesmo formato do passo 2.1, com `"source_type": "agent-output"`.

Como saber que deu certo: novo arquivo em `inbox/agent-output/`.

Se falhar: mesmos casos gerais de `ingest`. Um erro comum aqui é esquecer
de referenciar o `id` do stub original em `--origin` — não é validado pelo
comando, mas quebra a rastreabilidade entre asset e análise; corrija antes
de prosseguir.

### 3.4. Promover, compilar, exportar, auditar

Quem decide: 👤 humano. Tipo **H** — `agent-output` **nunca** auto-promove,
mesmo aprovado (regra de `source_type`); aprovar move o arquivo, mas grava
`confidence: unverified` para sempre. Risco de decidir errado: promover uma
análise que interpretou mal a planilha propaga um número errado para a
wiki citável — por isso `confidence` nunca vira `reviewed` aqui, mas o
arquivo ainda passa a existir em `raw/` como se fosse fonte aceita.

```bash
$WKPY "$WK" promote --approve "sb-ingest-analise-planilha-..." \
  --approved-by "Carlos" --store "$WK_STORE"
$WKPY "$WK" compile "financeiro/custos-2026" --store "$WK_STORE"
$WKPY "$WK" docx "financeiro/custos-2026" --store "$WK_STORE"
$WKPY "$WK" lint --store "$WK_STORE"
```

Detalhe completo em §5–§8. O stub original (3.1) e a análise (3.3) são
promovidos e compilados **separadamente** — 1 fonte promovida = 1 página,
sem cruzamento automático (§6).

<a id="sec-4"></a>
## 4. Runbook de codebase

**Quem faz cada passo.** Cada subseção declara a classificação **D**/**M**/
**H** (ver [Mapa de papéis do sistema](#sec-mapa-papeis), no topo do
documento, para a definição completa dos três tipos e das duas personas
👤/🤖).

> 🚨 Esta é a seção mais longa do runbook (32 subpassos). Se travar em
> qualquer ponto — comando recusado, `merge-agent-output` rejeitado,
> `audit`/`verify` reprovando — vá direto para [11. Retomada de fluxo
> interrompido e recuperação de erro](#sec-11); não tente adivinhar. Este
> aviso se repete no meio e no fim da seção.

**Resumo desta seção** (4.1–4.31, com o 4.7-bis, 32 subseções ao todo —
passos mistos contam em mais de uma linha):

| Classificação | Aparece em |
|---|---|
| **D** | 31 dos 32 passos |
| **M** | 5 dos 32 passos — os 5 pontos de fan-out |
| **H** | 4 dos 32 passos — `config` (4.3), `pending specs` (4.15), `redo` (4.7-bis), `promote` de item não-`code-repo` (4.28) |

Até `agent-pack`/`sdd-brief` (contratos na seção 12) tudo é **D**; o
conteúdo do SDD em si (módulos, regras, arquitetura, specs, synth) só
existe via **M**; `promote` de fonte que não é `code-repo` só avança com
**H**. **Sem modelo, este fluxo não é completável** — ver "Mapa de papéis
por fluxo", no topo: os 5 pontos de fan-out são obrigatórios, não
contornáveis.

Pré-requisito: seção 1 completa. `WK_STORE`/`WK_REPO` já foram exportados no
passo 1.2 — o bloco abaixo repete os dois (idempotente) para quem retoma o
runbook direto nesta seção, e acrescenta `WK_TOPIC`, usado a partir daqui
(os três valores abaixo são exemplo — troque pelos seus):

```bash
export WK_STORE='C:/Users/User/OneDrive/Documentos/projetos/wiki/store'
export WK_REPO='C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service'
export WK_TOPIC='codebases/insurance-quote-service'
```

Sequência completa, sem pular etapas (D/M/H de cada elo):
`surface` D → `export` D → `config` H+D → `plan` D → `run-stage modules` D →
fan-out M → `merge-agent-output modules` D (um por batch) → `done modules` D →
`run-stage rules` D → merge M+D → `done rules` D →
`run-stage architecture` D → merge M+D → `done architecture` D →
`pending specs` H+D → `run-stage specs` D → merge M+D →
`done specs` D → `evidence` D → `done evidence` D →
`run-stage synth` D → merge M+D → `done synth` D → `verify` D →
`done verify` D → `audit --strict` D → `publish` D →
`promote` D+H → `compile` D → `docx` D → `lint` D.

`redo` não entra nessa cadeia — é fora-de-banda, só quando um estágio já
`done` precisa ser refeito (H+D, ver 4.7-bis).

### Onde chamar o modelo — os 5 pontos de fan-out

32 subpassos, 36 comandos `wk` determinísticos, e só **5 pontos** em que um
LLM precisa efetivamente escrever conteúdo — todos classificados **M**. É a
única tabela que importa se você só quer saber "onde eu preciso pensar, e
onde é só rodar o comando":

| # | Comando que ABRE o ponto | Modelo LÊ | Modelo ESCREVE | Formato do bloco | Comandos que FECHAM |
|---|---|---|---|---|---|
| 1 | `wk code run-stage modules` | `agent-packs/modules-batch-NN.json` | `agent-outputs/modules-batch-NN.txt` | `=== MODULE: <path> === … === END ===` | `merge-agent-output modules` → `done modules` |
| 2 | `wk code run-stage rules` | `agent-packs/rules-batch-NN.json` | `agent-outputs/rules-batch-NN.txt` | `=== RULES: domain\|state-machines\|permissions\|adrs/NNN-<slug> ===` | `merge-agent-output rules` → `done rules` |
| 3 | `wk code run-stage architecture` | `agent-packs/architecture-batch-NN.json` | `agent-outputs/architecture-batch-NN.txt` | `=== ARCHITECTURE: architecture\|c4-*\|erd-complete\|traceability/spec-impact-matrix\|sequences/<slug> ===` | `merge-agent-output architecture` → `done architecture` |
| 4 | `wk code run-stage specs` | `agent-packs/specs-batch-NN.json` | `agent-outputs/specs-batch-NN.txt` | `=== SPEC: <unit> ===` **SINGULAR** + `--- requirements.md ---` / `design.md` / `tasks.md` | `merge-agent-output specs` → `done specs` |
| 5 | `wk code run-stage synth` | `sdd/*.md` de 1º nível (via `agent-packs/synth-batch-01.json`) | `agent-outputs/synth-batch-01.txt` | `=== SYNTH: confirmed\|inferred ===` | `merge-agent-output synth` → `done synth` |

> **Regra derivada:** `run-stage` **NUNCA** gera conteúdo — só prepara o
> manifesto (`cmd_run_stage` grava `"generates_sdd_content": false` no
> próprio JSON de retorno, `scripts/codescan/cli.py`). Todo output com
> `fanout_required` ≥ 1 é um ponto **M**, obrigatório — sem exceção, sem
> fallback do orquestrador. Por que um humano não substitui o modelo aqui:
> cada bloco exige ler e sintetizar código-fonte real (módulos, regras
> implícitas, diagramas C4/ERD) — um humano até conseguiria escrever module
> a module à mão, mas é exatamente o trabalho manual que este pipeline
> existe para evitar; o `wk` não tem heurística determinística para gerar
> esse texto. Fora desses 5 pontos (mais a análise de asset da seção 3),
> todo comando `wk` é **D**: mesma entrada, mesma saída, sem LLM.

**Como disparar o fan-out difere por engine.** O "dispare o subagente" do
passo 4.6 (e equivalentes 4.10/4.13/4.17/4.22) não é um comando `wk` — é
mecanismo nativo da engine, um subagente por batch, listas disjuntas:

| Engine | Mecanismo de subagente |
|---|---|
| Claude Code | `Task` |
| Devin | subagentes (spawn nativo) |
| Antigravity | `start_subagent` |
| Copilot | sessões de subagente |

(`operations/ingest-codebase.md`.) Em qualquer engine, o contrato é o
mesmo: o subagente **grava o próprio arquivo** no caminho `output` do batch
e devolve só o recibo de 3 linhas (`ARQUIVO:`/`BLOCOS:`/`BYTES:`) — nunca o
conteúdo do bloco na mensagem de chat.

### Regra do campo `acao`

Todo erro de `merge-agent-output` (e o guard de estágio inválido, ex.
`run-stage evidence`) traz um campo JSON `"acao"` com o comando corretivo
exato — em geral `"rode \`sdd-brief <stage>\` para o contrato canônico do
estágio"`. **Ao ver `acao` em qualquer JSON de erro, execute esse comando
LITERALMENTE antes de qualquer outra investigação.** Não interprete, não
adivinhe, não decompile — rode. Quem age aqui pode ser 👤 ou 🤖 — o comando
corretivo continua **D**, não muda a classificação do passo original.

> ⚠️ `done` é a exceção: a dica corretiva vem na chave `action` (inglês, não
> `acao`), aninhada em `blockers[].action`, e só aparece quando há **mais de
> um** blocker no mesmo `done`. Com um único blocker, o JSON de erro de
> `done` traz só `"error"` (e, quando aplicável, `"artifact"`) — sem
> `acao`/`action` no nível superior. Nesse caso, o próprio texto de
> `"error"` já nomeia o estágio/artefato a corrigir (seção 11.2, "Erros
> comuns"); não é motivo para abrir o `.pyz`.

**NUNCA abra `wk.pyz` com `zipfile`/decompilação, regex sobre o bytecode ou
qualquer script ad hoc para entender um erro.** O contrato de cada estágio —
seções obrigatórias, limites de linha, regra de "sem prosa" — está em `wk
code sdd-brief <stage>` (seção 12). É proibição explícita (seção 14.2,
"`python -c`, heredoc ou script ad hoc"; `SKILL.md` §"Guardrails
invioláveis") — decompilar o
`.pyz` em vez de rodar `sdd-brief`/seguir `acao` já custou 14 rodadas de
tentativa e erro numa execução real, sem nunca corrigir o problema de fato
(o erro era formato de bloco, não lógica escondida no bytecode).

### 4.1. `surface` — estágio 1: mapa determinístico

Quem executa: 👤 humano. Tipo **D** — `cmd_surface` (`scripts/codescan/cli.py`)
só varre o repo (leitura de arquivos) e grava JSON; sem LLM.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" surface --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"surface"`, com `arquivos`, `loc`, `modulos`,
`entry_points` e `artifact` (caminho de `surface.json`). Sem `error`.

Se falhar: `--repo` não existe → corrija o caminho. `store não informado` →
falta `--store` explícito ou `WK_STORE` no ambiente.

### 4.2. `export` — artefatos SDD determinísticos (sem LLM)

Quem executa: 👤 humano. Tipo **D** — motores de análise estática
(Java/genérico), sem chamada a LLM.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" export --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"export"`; grava `sdd/inventory.md`,
`sdd/dependencies.md`, `sdd/coupling.md` no workdir. Passo obrigatório do
estágio 1, não opcional. `coupling.md` usa o motor Java (classe e pacote,
ciclos, classe-deus, hotspot de Ca, abstração especulativa; limiares por
outlier IQR) quando o repo tem Java, e o motor genérico multi-linguagem
(Ce/Ca/I por módulo) caso contrário — quando o motor Java roda, o export
também grava `sdd/coupling.html` (mapa interativo, artefato LOCAL de
inspeção; não entra em `inbox/`, não publica).

Se falhar: erro `rode surface primeiro` → repita 4.1. Nunca use `--output`
apontando para `raw/`/`wiki/` do store (bloqueado — use o fluxo de
`publish` na seção 4.27).

### 4.3. `config` — decisões do SDD

Obrigatório antes de `plan` e de `run-stage modules`.

Quem decide: 👤 humano decide `--doc-level`/`--granularity`. Tipo **H** —
decisão: quanto detalhe o pipeline vai produzir (`essencial`/`completo`/
`detalhado`) e como particionar o trabalho. Risco de decidir errado: um
`doc_level` raso demais para um sistema crítico deixa lacunas de
rastreabilidade (sem ADRs, sem ERD completo); um nível alto demais em
repositório pequeno gasta fan-out (custo de modelo) sem ganho proporcional.
| Quem grava a decisão: 👤 ou 🤖 (indiferente). Tipo **D** — depois de
decidido, gravar o valor no `state.json` é mecânico.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" config --doc-level detalhado --granularity hybrid
```

Esperado: `"ok":true`, `"cmd":"config"`, ecoa `doc_level`/`granularity`
gravados no estado.

Se falhar: `--doc-level`/`--granularity` fora das opções válidas
→ argparse recusa antes de chamar o comando; corrija o valor. Válidos:

- `--doc-level`: `essencial` · `completo` · `detalhado`
- `--granularity`: `module` · `endpoint` · `use-case` · `hybrid` ·
  `feature` · `custom`

### 4.4. `plan` — lista de módulos a cavar (LOC, main primeiro)

Quem executa: 👤 humano. Tipo **D** — heurística de particionamento por LOC,
sem LLM.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" plan
```

Esperado: `"ok":true`, `"cmd":"plan"`, lista de módulos condensada em
`*_count`. Não fecha nenhum estágio — só planeja.

Se falhar: erro `config SDD obrigatória antes de plan/run-stage modules`
com campo `"acao"` trazendo o comando `config` exato → rode 4.3 antes.

### 4.5. `run-stage modules` — manifesto de fan-out

Quem executa: 👤 humano. Tipo **D** — só prepara o manifesto; não gera
conteúdo (comprovado por `"generates_sdd_content": false` no retorno).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage modules
```

Esperado: `fanout_required` (inteiro, N subagentes exigidos), `next_action` e,
no corpo completo (padrão), `batches[].output`, `batches[].agent_slot` e
`batches[].merge_command` de cada batch. Use `--quiet` só para condensar a
uma linha (`fanout_required`/`next_action` sobrevivem por serem escalares;
`batches` vira `batches_count`). O manifesto completo é sempre gravado em
`$WK_STORE/.codescan/<repo>-<hash>/agent-runs/modules-plan.json`.

Se falhar: erro `sem módulos pendentes; rode plan` → repita 4.4. Erro de
permissão em `agent-outputs/` → refaça 1.3 (`wk init --store ... --repo ...`).

### 4.6. Fan-out — dispare os subagentes

Quem executa: 🤖 modelo — cada subagente escreve o próprio arquivo de saída.
Tipo **M** — obrigatório: sem subagente, `fanout_required` nunca chega a 0
e `merge-agent-output`/`done modules` não têm o que integrar.

- Modelo LÊ: `agent-packs/modules-batch-NN.json` (o pacote determinístico do
  batch, gerado por `run-stage`/`agent-pack`).
- Modelo ESCREVE: `agent-outputs/modules-batch-NN.txt`, no formato
  `=== MODULE: <path> === ... === END ===`.

Não é comando `wk`. Dispare **exatamente `fanout_required` subagentes**, um
por batch, com listas de itens disjuntas (cada módulo cai em um único
batch). Para cada batch `N`:

- o caminho de saída é o campo `output` do batch (corpo completo do passo
  4.5, padrão, ou o manifesto em disco):
  `.../agent-outputs/modules-batch-NN.txt`;
- o subagente **grava o próprio arquivo** nesse caminho, no formato
  `=== MODULE: <path> === ... === END ===` (contrato completo em
  `sdd-brief modules`, seção 12);
- o subagente devolve só o recibo de 3 linhas — `ARQUIVO:`/`BLOCOS:`/`BYTES:`
  — nunca o conteúdo do artefato, log, diff ou eco de comando na mensagem.

Por que um humano não substitui: analisar cada módulo do repositório e
produzir leitura operacional estruturada (responsabilidade, fluxos,
dependências, riscos, rastreabilidade por `arquivo:linha`) é o próprio
trabalho de síntese que o LLM faz — um humano consegue fazer manualmente,
só que é exatamente o esforço que este pipeline substitui.

### 4.7. `merge-agent-output modules` — um por batch

Quem executa: 👤 humano (ou o agente orquestrador, no modo skill). Tipo
**D** — valida formato e integra a saída do 🤖 (passo 4.6); não reescreve
nem interpreta o conteúdo.

Comando (repita para cada batch, trocando `NN` e `--agent`):

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output modules --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/modules-batch-01.txt" --agent modules-b01
```

Esperado: `"ok":true`, `"cmd":"merge-agent-output"`, artefatos gravados em
`modules/<slug>.md` (um por módulo do batch).

Se falhar: `--agent` ausente → obrigatório em todo `merge-agent-output`.
`--agent` genérico (`main`/`orquestrador`/`orchestrator`/`self`/`principal`)
→ use o `agent_slot` real do batch (ex.: `modules-b01`). `input` com `mtime`
anterior ao plano → o arquivo é anterior ao `run-stage`; grave o output de
novo, depois do passo 4.5.

### 4.7-bis. `redo` — refaz um estágio já mergeado

Quem decide: 👤 humano decide refazer. Tipo **H** — decisão: reabrir um
estágio/item já fechado. Risco de decidir errado: refazer sem necessidade
consome fan-out (custo de modelo) à toa; **não** refazer quando o conteúdo
está genuinamente errado deixa erro propagar para `publish`/`promote`.
Quem executa depois de decidido: 👤 ou 🤖 (indiferente). Tipo **D**.

Não é um passo obrigatório da sequência linear (ver nota no fim da lista
acima). Use quando um estágio já `done` — ou um item específico dele —
precisa ser reaberto e reescrito, sem apagar o histórico de runs
anteriores. `redo` marca os runs `current` cobertos como `superseded`
(nunca apaga conteúdo) e devolve o(s) item(ns) a `pending` em
`state.json`. Vale para `modules`, `rules`, `architecture`, `specs` e
`synth`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" redo modules --item nome-do-modulo
```

Sem `--item`, reabre todos os itens `current` do estágio.

Esperado: `"itens_reabertos"`, `"runs_superseded"` (quantidade + ids) e
`"proximo_passo"` apontando `run-stage <stage>` — volte ao `run-stage`
do estágio (4.5/4.9/4.12/4.16/4.21, conforme o caso) para gerar um novo
manifesto de fan-out dos itens reabertos (**M** de novo a partir daqui). A
partir daqui o manifesto do estágio passa a usar o schema
`wiki-ai.agent-runs.v3` (carrega `status`/`supersedes` por run; `audit`
aceita `v2` e `v3` — ver 4.26).

Se falhar: `agent-runs ausente ou ilegível` → rode `run-stage`/
`merge-agent-output` do estágio antes de `redo`. `item nunca coberto por
nenhum run` → confira o nome do item (o mesmo usado no
`merge-agent-output` original).

### 4.8. `done modules`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `modules` por
verificação de artefatos/manifesto; não escreve conteúdo SDD.

Pré-requisito não óbvio: `done modules` exige `sdd/code-analysis.md` gravado
e consolidado — não basta todo `modules/<slug>.md` existir; esses arquivos
são insumo intermediário, não wiki final. `sdd/code-analysis.md` é regra
obrigatória do estágio `modules` (`RULES["modules"]` em
`scripts/codescan/sdd.py`: mínimo de 1800 bytes, 5 citações e as seções
`Visão geral`/`Módulos`/`Fluxos`/`Riscos`/`Rastreabilidade`). Escrever esse
consolidado é tipo **M** (o mesmo subagente do fan-out redige, ou um novo
turno de 🤖) — em `doc_level` completo/detalhado, consolide também
`sdd/data-dictionary.md` e `sdd/flowcharts/<modulo>.md`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done modules
```

Esperado: `"ok":true`, estado do estágio `modules` com `"status":"done"`.

Se falhar: `fan-out obrigatório não cumprido` → todos os batches foram
registrados com o mesmo `--agent`; refaça o `merge-agent-output` dos batches
que faltam com um `--agent` realmente distinto. Itens `pending`/`blocked`/
`failed`/`degraded` → resolva-os (repita 4.6–4.7 para o batch faltante, ou
use `blocked`/`failed`/`degraded <stage> --item <item>` para registrar o
problema). `sdd/code-analysis.md` ausente/raso (`acao` aponta o artefato) →
grave-o antes de repetir `done modules` (ver pré-requisito acima).

> 🚨 Metade do estágio `modules` (o mais longo) fica para trás daqui.
> Perdeu o fio ou o erro não bate com nada listado acima? [11. Retomada de
> fluxo interrompido e recuperação de erro](#sec-11) — comece por `state`/
> `next`, não por tentativa e erro.

### 4.9. `run-stage rules`

Quem executa: 👤 humano. Tipo **D** — prepara o manifesto de fan-out de
`rules`; não gera conteúdo.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage rules
```

Esperado: igual a 4.5, mas para `rules` — normalmente 1 batch (`agent_slot`
tipo `rules-b01`). Sem fan-out múltiplo obrigatório quando `fanout_required
<= 1`.

Se falhar: mesmos casos de 4.5, adaptados ao estágio `rules`.

### 4.10. Subagente + `merge-agent-output rules`

Quem escreve as regras: 🤖 modelo. Tipo **M** — mesmo raciocínio de 4.6:
modelo LÊ `agent-packs/rules-batch-NN.json`, ESCREVE
`agent-outputs/rules-batch-NN.txt`. Quem integra: 👤 ou 🤖. Tipo **D**.

O subagente grava o recibo em `output` do batch (4.9), com blocos
`=== RULES: domain ===` (e `=== RULES: state-machines ===` /
`=== RULES: permissions ===` / `=== RULES: adrs/NNN-<slug> ===` para ADRs
retroativos em `doc_level` completo/detalhado — ver tabela da seção 12).
Depois:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output rules --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/rules-batch-01.txt" --agent rules-b01
```

Esperado/Se falhar: mesmos casos de 4.7, estágio `rules`.

### 4.11. `done rules`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `rules`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done rules
```

Esperado/Se falhar: mesmos casos de 4.8, estágio `rules`. Em `doc_level`
completo/detalhado, `done rules` também exige ao menos um ADR retroativo em
`sdd/adrs/`.

### 4.12. `run-stage architecture`

Quem executa: 👤 humano. Tipo **D** — prepara o manifesto de fan-out do
estágio `architecture`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage architecture
```

Esperado/Se falhar: mesmos casos de 4.9, estágio `architecture`.

### 4.13. Subagente + `merge-agent-output architecture`

Quem escreve os diagramas: 🤖 modelo. Tipo **M** — LÊ material consolidado
dos estágios anteriores (não o repo), ESCREVE
`agent-outputs/architecture-batch-NN.txt`. Quem integra: 👤 ou 🤖. Tipo **D**.

Blocos aceitos: `=== ARCHITECTURE: architecture ===` / `c4-context` /
`c4-containers` / `c4-components` / `erd-complete` /
`traceability/spec-impact-matrix` / `sequences/<slug>` (os últimos por
`doc_level` — ver seção 12; `sequences/` só em `detalhado`).

**Diagrama Mermaid é obrigatório nestes 5 artefatos** — `audit` reprova sem
ele (`RULES["architecture"]`, `scripts/codescan/sdd.py`):

| Artefato | Tipo de bloco ```mermaid``` exigido |
|---|---|
| `sdd/architecture.md` | `flowchart` ou `graph` |
| `sdd/c4-context.md` | `flowchart` \| `graph` \| `C4Context` \| `C4Container` \| `C4Component` |
| `sdd/c4-containers.md` | idem `c4-context` |
| `sdd/c4-components.md` | idem `c4-context` |
| `sdd/erd-complete.md` | `erDiagram` |

Ausência sem escape → **blocker**, `score -25`. Escape auditável —
`<!-- no-diagram: <motivo> -->` no corpo do artefato — vira **warning**,
`score -5`, nunca bypass silencioso: o motivo é obrigatório e não-vazio
(`<!-- no-diagram: -->` sem motivo continua reprovando como se não houvesse
escape) e o aviso fica registrado em `warnings[]` no relatório de `audit`.
Erro de sintaxe Mermaid (brackets/quotes desbalanceados, aresta fora da
whitelist, node sem definição, label não citado etc.) sempre traz linha
absoluta do arquivo + trecho ofensivo truncado + nome do padrão violado,
ex.: `Mermaid ... linha 42: '...' (padrão: label-nao-quoted)` — nunca uma
mensagem genérica de "mermaid inválido".

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output architecture --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/architecture-batch-01.txt" --agent architecture-b01
```

Esperado/Se falhar: mesmos casos de 4.7, estágio `architecture`. Diagrama
ausente/inválido nos 5 artefatos acima → corrija o bloco ```mermaid``` (ou
documente `<!-- no-diagram: <motivo> -->` se genuinamente não houver
diagrama a desenhar) e repita o merge — correção de conteúdo Mermaid é
**M** de novo (o subagente reescreve o bloco).

### 4.14. `done architecture`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `architecture`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done architecture
```

Esperado/Se falhar: mesmos casos de 4.8, estágio `architecture`. Em
`doc_level` detalhado, `done architecture` também exige ao menos um diagrama
de sequência em `sdd/sequences/`.

### 4.15. `pending specs` — registra as units a especificar

Quem decide: 👤 humano decide a lista de units. Tipo **H** — decisão: quais
unidades funcionais viram spec. Risco de decidir errado: esquecer uma unit
crítica deixa a wiki sem requirements/design/tasks daquele fluxo; incluir
units triviais demais gasta fan-out à toa. Quem grava: 👤 ou 🤖. Tipo **D**.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" pending specs --items "criar-cotacao,emitir-apolice,invalidar-cache"
```

Esperado: `"ok":true`, `"cmd":"pending"`, estágio `specs` com `pending_count`
igual ao número de itens informados.

Se falhar: `stage` inválido → só os estágios de `STAGES` são aceitos; confira
grafia (`specs`, não `spec`).

### 4.16. `run-stage specs`

Quem executa: 👤 humano. Tipo **D** — prepara o manifesto de fan-out de
`specs`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage specs
```

Esperado/Se falhar: mesmos casos de 4.9, estágio `specs`, batch com os itens
registrados em 4.15.

### 4.17. Subagente + `merge-agent-output specs`

Quem escreve as specs: 🤖 modelo. Tipo **M**. Quem integra: 👤 ou 🤖. Tipo **D**.

> ⚠️ **`SPEC` é SINGULAR — `SPECS` (plural) é o erro mais comum aqui.** O
> parser só aceita `=== SPEC: <unit> ===`; `=== SPECS: ... ===` falha com
> `prosa fora de arquivo SPEC: <unit>` e descarta a rodada de fan-out
> inteira. Errado ❌ `=== SPECS: functional ===` · Certo ✅
> `=== SPEC: functional ===`.

Bloco por unit: `=== SPEC: <unit> ===`, com três sub-blocos obrigatórios
dentro — `--- requirements.md ---`, `--- design.md ---`, `--- tasks.md ---` —
e os opcionais `--- contracts.md ---` / `--- edge-cases.md ---`. Em
`doc_level` detalhado o `sdd-brief` pede os dois, mas `done specs <unit>`
não os exige; a auditoria de `done specs` (sem `--item`) só bloqueia se
nenhum contracts.md/edge-cases.md existir em todo o estágio. O mesmo
cabeçalho `=== SPEC: ... ===` também aceita documentos nomeados de revisão
final: `confidence-report`, `gaps`, `traceability/code-spec-matrix`,
`user-stories/<slug>` (grava `.md`) e `openapi/<slug>` (grava `.yaml`) —
corpo livre, sem sub-blocos (ver seção 12).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output specs --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/specs-batch-01.txt" --agent specs-b01
```

Esperado/Se falhar: mesmos casos de 4.7, estágio `specs`. Artefatos vão para
`sdd/specs/<slug-da-unit>/{requirements,design,tasks}.md`.

### 4.18. `done specs`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `specs`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done specs
```

Esperado/Se falhar: mesmos casos de 4.8; exige ao menos uma unit concluída,
`sdd/confidence-report.md` não vazio e, em `doc_level` >= completo,
`sdd/gaps.md`, `sdd/traceability/code-spec-matrix.md` e ao menos uma user
story em `sdd/user-stories/`.

### 4.19. `evidence` — evidence-pack por tópico

Quem executa: 👤 humano. Tipo **D** — monta o evidence-pack do tópico.

**`evidence` NÃO aceita `run-stage` nem fan-out** — diferente dos 5 estágios
vizinhos (`modules`/`rules`/`architecture`/`specs`/`synth`), roda 100% via
`evidence --topic <t>`, sem manifesto, sem subagente e sem
`merge-agent-output`. `run-stage`/`merge-agent-output`/`agent-pack`/`redo`
com `stage=evidence` falham com JSON acionável em stderr (não mais o
`argparse: invalid choice` cru) e **exit 2** — `{"error": "'evidence' não é
um estágio válido para '<comando>' (aceita apenas: modules, rules,
architecture, specs, synth)", "acao": "evidence é 100% determinístico e não
tem fan-out por subagente; rode \`wk code evidence --topic <topico>\`
diretamente"}` (`_evidence_stage_hint`, `scripts/codescan/cli.py`) —
`evidence` não está em `CRITICAL_STAGES` (`scripts/codescan/sdd.py`).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" evidence --topic "$WK_TOPIC"
```

Esperado: `"ok":true`, `"cmd":"evidence"`, `artifact` apontando para
`evidence-<topic>.json` no workdir.

Se falhar: sem candidatos para o tópico → confira `--topic`/`--top`; nenhum
match não é erro fatal, mas o artefato sai vazio (revise antes de prosseguir).
JSON com `"acao"` mencionando `evidence` → você tentou `run-stage`/
`merge-agent-output`/`agent-pack`/`redo evidence`; rode `evidence --topic
<t>` direto (comando acima) — a própria mensagem já diz isso.

### 4.20. `done evidence`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `evidence`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done evidence
```

Esperado/Se falhar: mesmos casos de 4.8, estágio `evidence`.

### 4.21. `run-stage synth`

Quem executa: 👤 humano. Tipo **D** — prepara o manifesto de fan-out de
`synth`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" run-stage synth
```

Esperado/Se falhar: mesmos casos de 4.9, estágio `synth`. `run-stage synth`
recusa se `modules`/`rules`/`architecture`/`specs` não estiverem `done`.

### 4.22. Subagente + `merge-agent-output synth`

Quem escreve confirmed/inferred: 🤖 modelo. Tipo **M** — LÊ `sdd/*.md` de
1º nível, ESCREVE `agent-outputs/synth-batch-01.txt`. Quem integra: 👤 ou
🤖. Tipo **D**.

Blocos: `=== SYNTH: confirmed ===` / `=== SYNTH: inferred ===`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" merge-agent-output synth --input "$WK_STORE/.codescan/<repo>-<hash>/agent-outputs/synth-batch-01.txt" --agent synth-b01
```

Esperado/Se falhar: mesmos casos de 4.7. Grava `sdd/confirmed.md` e/ou
`sdd/inferred.md` — local canônico; nunca uma cópia na raiz do workdir. O
manifesto `agent-runs/synth.json` exigido pelo `audit` só é gravado por este
merge.

### 4.23. `done synth`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `synth`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done synth
```

Esperado/Se falhar: mesmos casos de 4.8, estágio `synth`.

### 4.24. `verify` — valida Markdown confirmado contra arquivo:linha

Quem executa: 👤 humano. Tipo **D** — valida citações `arquivo:linha`
contra o repo (comparação textual, sem LLM).

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" verify --artifact "$WK_STORE/.codescan/<repo>-<hash>/sdd/confirmed.md"
```

Esperado: `"ok":true`, `"cmd":"verify"`, relatório com citações
`arquivo:linha` validadas contra o repo.

**Gate real.** `verify` grava o resultado em `state.json.stages.verify`
(`cmd_verify`, `scripts/codescan/cli.py`). Quando
`stages.verify.status == "failed"`, `_failed_verify_workdirs`
(`scripts/wk/cli.py`) passa a bloquear os comandos a jusante,
escopado por `topic` — nunca globalmente:

| Comando | Escopo do bloqueio | Bloqueia mesmo? |
|---|---|---|
| `publish` | — | **Não.** É staging em `inbox/`; nada vira canônico ali. Só anota `"aviso_verify"` no JSON de saída (`cmd_publish`, `scripts/wk/cli.py`), avisando para não promover sem corrigir/`--allow-unverified`. |
| `promote` | por **item**, pelo `topic` de cada fonte (`_verify_gate_scope`, `scripts/wk/cli.py`) | **Sim** — o gate real está aqui. Fontes de outros tópicos no mesmo lote não são penalizadas. Cobre também os artefatos `code-repo` do tópico (`inventory.md`/`dependencies.md`/`coupling.md`/`coupling.html`), não só `agent-output`. |
| `compile` / `docx` | pelo `topic` do próprio comando (ou todos os tópicos, se o comando não filtrar) | **Sim.** Sai com `exit 3` e o bloqueio em stderr. |

Override explícito: `--allow-unverified` (aceito por `promote`/`compile`/
`docx`) libera mesmo com `verify` falho — fica registrado em `log.md`
(`## [...] compile --allow-unverified | N workdir(s) com verify falhado...`)
e ecoado no JSON de saída (`verify_override`/`decisao_humana`, conforme o
comando). É tipo **H**: decisão explícita de aceitar risco. Risco de
decidir errado: publicar/promover conteúdo cujas citações `arquivo:linha`
não batem com o repo — a wiki passa a citar evidência que não existe
naquele ponto do código. Não existe `--allow-unverified` em `publish` —
não precisa, porque `publish` não bloqueia.

Se falhar: citação não bate com o repo → corrija `confirmed.md` (via novo
merge de `synth`, **M**) e repita `verify`. Se o objetivo é promover/
compilar/gerar docx mesmo assim, decida conscientemente com
`--allow-unverified` (fica na trilha de auditoria) — não é um workaround
silencioso.

### 4.25. `done verify`

Quem executa: 👤 humano. Tipo **D** — fecha o gate do estágio `verify`.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" done verify
```

Esperado/Se falhar: mesmos casos de 4.8, estágio `verify`.

### 4.26. `audit --strict`

Quem executa: 👤 humano. Tipo **D** — audita os artefatos de todos os
estágios; não escreve conteúdo, só avalia.

Nota: `--strict` é mantido por compatibilidade — os gates P0 já são padrão
em `audit` mesmo sem a flag; ela não liga nada adicional. O comando continua
válido no runbook.

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" audit --strict
```

Esperado (corpo completo, padrão): `"status":"pass"`, `score >= threshold`,
`stages_evaluated` igual ao nº de estágios do escopo estrito e
`artifacts_checked > 0`. Sem estágio elegível (nenhum artefato verificado), o
resultado **não** é `pass`: é `"status":"sem_evidencia"`, `"score":null`, com
`stages_evaluated`/`artifacts_checked` (podendo ser `0`) e `"message"`
apontando os estágios pendentes e o `sdd-brief` a rodar — `--strict` sai com
código != 0 nesse caso. `score: 100` só significa aprovação real quando
`stages_evaluated > 0`; nenhum score aparece por ausência de evidência. Use
`--quiet` para condensar a uma linha (`blockers_count` soma os blockers de
todos os estágios/artefatos).

Se falhar: blockers em `stages[].blockers`/`stages[].artifacts[].blockers`
(ou `blockers_count` > 0 sob `--quiet`) → cada blocker cita estágio +
artefato + causa; corrija na origem (reabra o estágio com
`merge-agent-output`, **M**) e repita o `audit`. `"status":"sem_evidencia"` →
nenhum estágio do escopo tinha artefato para checar; siga o `message` do
próprio relatório. `P0: agent-runs schema legado (wiki-ai.agent-runs.v1)` →
refaça o estágio citado com o merge atual (schema `wiki-ai.agent-runs.v2`;
`redo` grava `v3`, também aceito — ver 4.7-bis).
`P0: artefato alterado após o merge` → não editar artefato SDD à mão; refaça
`merge-agent-output` do estágio.

### 4.27. `publish` — leva a árvore SDD do workdir para `inbox/`

Quem executa: 👤 humano. Tipo **D** — copia a árvore SDD do workdir para
`inbox/`, classificação mecânica por caminho, sem LLM.

```bash
$WKPY "$WK" publish --workdir "$WK_STORE/.codescan/<repo>-<hash>" --topic "$WK_TOPIC" --store "$WK_STORE"
```

Esperado: `"publicados"` com um item por arquivo elegível (`sdd/**/*.md`,
`modules/*.md`, `confirmed.md`/`inferred.md`). Único caminho válido de
workdir do codescan para `inbox/` — nunca escreva ali manualmente.

Se falhar: `workdir não encontrado` → confira `<repo>-<hash>`; rode `wk code
--repo "$WK_REPO" --store "$WK_STORE" state` para achar o workdir certo.
`aviso: nenhum artefato elegível` → estágios anteriores não geraram `.md` em
`sdd/`/`modules/` — não é erro fatal, mas revise antes de prosseguir.

**`coupling.html` é publicado como asset + stub.** `_publish_candidates`
(`scripts/wk/cli.py`) aceita `.md` **e** `.html` (`_PUBLISH_SDD_EXTS`);
`sdd/coupling.html` (mapa interativo do motor Java, seção 4.2) é candidato e
classificado `code-repo` (`_PUBLISH_CODE_REPO_REL`). `cmd_publish`
(`scripts/wk/cli.py`) copia os bytes originais para
`raw/assets/<doc_id>.html` e grava um stub `.md` de proveniência apontando
para esse asset; `doc_id` recebe o sufixo `-html` para não colidir com o
`doc_id` de `coupling.md`. `wk compile` copia o asset para
`wiki/<topic>/<source_type>/<id>.html` e linka a partir da página da fonte e
da página de overview (seção 6); `wk docx` **nunca** converte esse `.html`
— pula explicitamente e conta em `ignorados_asset_html`.

### 4.28. `promote` — move fontes seguras de `inbox/` para `raw/`

Quem executa a auto-promoção: 👤 ou 🤖 dispara. Tipo **D** — `code-repo`
auto-promove sozinho, sem revisão humana adicional. Quem aprova o resto:
👤 humano. Tipo **H** — decisão: aceitar `agent-output` (síntese do
pipeline) como digno de virar `raw/`. Risco de decidir errado: mesmo
aprovado, `agent-output` fica `confidence: unverified` para sempre — o
risco real é aprovar `human-transcript`/`human-doc` de baixa qualidade, que
sim vira `confidence: reviewed`.

Comando resumido — detalhe completo, exemplos e critérios de sucesso/falha
na seção 5 (esta subseção só amarra o passo dentro do pipeline de código):

```bash
$WKPY "$WK" promote --store "$WK_STORE"
```

**Gate de `verify`.** Cada item é bloqueado individualmente, pelo `topic`
daquela fonte, se o workdir de codescan de origem tem
`state.json.stages.verify.status == "failed"` (`_verify_gate_scope`,
`scripts/wk/cli.py`) — inclui os artefatos `code-repo` do tópico
(`inventory.md`/`dependencies.md`/`coupling.md`/`coupling.html`), não só
`agent-output`. Fontes de outros tópicos no mesmo lote não são penalizadas.
Detalhe completo do gate: seção 4.24.

### 4.29. `compile` — gera `wiki/` a partir de `raw/` promovido

Quem executa: 👤 humano. Tipo **D**.

Comando resumido — detalhe completo na seção 6:

```bash
$WKPY "$WK" compile "$WK_TOPIC" --store "$WK_STORE"
```

**Página de overview por tópico.** Além da página por fonte (1 fonte
promovida = 1 página, sem cruzamento — comportamento inalterado),
`compile` gera `wiki/<topic>/overview.md` (`_build_topic_overview`,
`scripts/wk/cli.py`, chamada em `cmd_compile`) para todo tópico
que tenha **ao menos uma fonte cuja `origin` venha de `wk publish`**
(regex `_ORIGIN_CODESCAN_RE`, `scripts/wk/cli.py` — casa
`"codescan <repo> — <caminho>"`; tópicos 100% manuais, só transcrições/docs
ingeridos à mão, **não ganham overview**). Seções, nesta ordem:

| Seção | Conteúdo | Corpo completo ou índice? |
|---|---|---|
| Arquitetura | `sdd/architecture.md` + os 3 C4 (`c4-context`/`c4-containers`/`c4-components`) | completo |
| Decisões | tabela de ADRs — nº+título / status / decisão em 1 linha, linkando o ADR completo | resumo (tabela) |
| Análise de código | `sdd/code-analysis.md` | **índice**: contagem + nomes de módulo (via headings `### Módulo:`) + link para o artefato completo — nunca o corpo inteiro |
| Edge cases | `sdd/specs/*/edge-cases.md`, por unit | completo |
| Diagramas | `sdd/flowcharts/_index.md` embutido; demais flowcharts e `sdd/sequences/*.md` linkados; `coupling.html` (asset) linkado | misto |
| Confiança | `confidence-report.md` + `gaps.md` embutidos; `confirmed.md`/`inferred.md` linkados com contagem de linhas | misto |
| Lacunas de síntese | só aparece se houver artefato esperado ausente | condicional |

Tolerante a ausência: artefato que falta faz a subseção ser pulada, não a
overview inteira falhar — a lacuna vai para "Lacunas de síntese". A
identificação de fonte-do-codescan é sempre por caminho relativo
(`_ORIGIN_CODESCAN_RE`), nunca por nome de tópico. `wiki/index.md` linka
cada overview em destaque, na seção "Visão geral por tópico", no topo do
arquivo, antes da lista de páginas — com contagem de lacunas quando houver.

**Gate de `verify`.** `compile` recusa (`exit 3`) tópicos cujo workdir de
origem tem `verify` falho, salvo com `--allow-unverified` (**H**) — ver
seção 4.24.

### 4.30. `docx` — gera `wiki-docx/` (DOCX) a partir de `raw/` promovido

Quem executa: 👤 humano. Tipo **D**.

Comando resumido — detalhe completo na seção 7:

```bash
$WKPY "$WK" docx "$WK_TOPIC" --store "$WK_STORE"
```

**`.docx` agregador.** Além do `.docx` por fonte (granularidade inalterada:
1 fonte promovida = 1 documento), `docx` gera
`wiki-docx/<topic>/index.docx` (`cmd_docx`, `scripts/wk/cli.py`) para
todo tópico elegível a overview — mesmo critério de `compile` (seção 4.29):
reusa `_build_topic_overview` e converte o texto resultante para OOXML pelo
mesmo `docxgen.build_document` dos demais documentos. Não substitui os
`.docx` individuais, que continuam sendo gerados normalmente.

`coupling.html` **nunca** é convertido para `.docx` — `docx` pula esse
asset explicitamente e conta em `ignorados_asset_html` (ele já é HTML
navegável em `raw/assets/`; converter perderia a interatividade).

**Diagramas Mermaid em `.docx`: degradação textual, nunca imagem
renderizada.** Ver tabela completa na seção 7.

**Gate de `verify`.** `docx` recusa (`exit 3`) tópicos cujo workdir de
origem tem `verify` falho, salvo com `--allow-unverified` (**H**) — ver
seção 4.24.

Se falhar: `pulados` não vazio → exit `1`; cada item lista `id` e motivo.

### 4.31. `lint` — audita o índice e fecha o runbook

Quem executa: 👤 humano. Tipo **D**.

Comando resumido — detalhe completo na seção 8:

```bash
$WKPY "$WK" lint --store "$WK_STORE"
```

Esperado: `"achados":0` e `"report"` apontando para
`wiki/_lint-report.md`. Runbook concluído quando `achados` chega a `0`.

> 🚨 Se, em qualquer ponto da seção 4, você perdeu o fio ou um erro não
> bateu com o "Se falhar" do passo: [11. Retomada de fluxo interrompido e
> recuperação de erro](#sec-11).

<a id="sec-5"></a>
## 5. Promoção / governança

**Classificação da seção: 100% D+H — `promote` nunca chama LLM.** Este é o
portão único `inbox/` → `raw/`, aplicável a qualquer fonte, não só
codebase: transcrições (seção 2), docs/planilhas (seção 3) e artefatos SDD
publicados (seção 4.27). Verificado em `cmd_promote`
(`scripts/wk/cli.py`): a função lê frontmatter, aplica regra por
`source_type`, move arquivo e atualiza índice — nenhuma chamada de LLM em
nenhum ramo.

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 5.1. Varredura — o que está pendente de decisão

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" promote --store "$WK_STORE"
```

Sem alvo posicional, varre `inbox/**/*.md` inteiro. Esperado (JSON):
`"promovidos"` (auto-promovidos, ex. `source_type: code-repo`),
`"decisao_humana"` (exige `--approve`/`--approve-all`), `"quarentena"`
(proveniência incompleta), `"bloqueados_verify"` (retidos pelo gate de
`verify`, sempre presente, vazio se nada foi bloqueado) e
`"reindexed":true`.

Exemplo de saída (resumida):
```json
{"promovidos": [{"id": "sb-ingest-inventory-...", "source_type": "code-repo"}],
 "decisao_humana": [{"id": "sb-ingest-reuniao-...", "source_type": "human-transcript",
                       "motivo": "exige --approve"}],
 "quarentena": [], "bloqueados_verify": [], "reindexed": true}
```

Como saber que deu certo: `"reindexed": true` e nenhuma exceção em stderr.
Itens em `"decisao_humana"` **não são erro** — são o portão fazendo o
trabalho dele.

Se falhar: `reindex_error` no corpo → índice não atualizou; rode `wk index
reindex --store "$WK_STORE"` manualmente (seção 9) e investigue o erro.

### 5.2. Regra por `source_type` — o que precisa de aprovação

| `source_type` | Auto-promove? | `confidence` resultante |
|---|---|---|
| `code-repo` | **Sim**, sempre, sem `--approve` | `reviewed`, `promoted_by: wiki-ai` |
| `human-transcript` / `human-doc` / `web-clip` | Não — só com `--approve`/`--approve-all` | `reviewed`, `promoted_by: <--approved-by>` |
| `agent-output` | Não — **mesmo aprovado** | `unverified` (nunca `reviewed`) |
| Sem `origin`/`source_type` válido (L2) | Direto para `quarantine.md`, nem entra na decisão | — |

### 5.3. Aprovar — a decisão humana

Quem decide: 👤 humano. Tipo **H** — decisão: esta fonte (transcrição, doc,
ou síntese de agente) pode virar fonte-verdade citável? Risco de decidir
errado: para `human-*`/`web-clip`, aprovar grava `confidence: reviewed` —
a wiki passa a tratar aquele conteúdo como fato revisado; um erro de
transcrição ou uma nota mal contextualizada vira citação confiável.

```bash
$WKPY "$WK" promote --approve "sb-ingest-reuniao-2026-08-05-a1b2c3d4" \
  --approved-by "Maria" --store "$WK_STORE"
```

ou, em lote por tipo:

```bash
$WKPY "$WK" promote --approve-all --source-type human-transcript \
  --approved-by "Maria" --store "$WK_STORE"
```

`--approve-all` exige `--source-type`; sem ele, o comando recusa antes de
tocar em qualquer arquivo. `--approved-by` default é `"humano"` — sempre
informe um nome real para manter a trilha de auditoria. Aprovação amplia o
escopo: mesmo um item fora da varredura original é promovido se aprovado
explicitamente.

Como saber que deu certo: o item some de `"decisao_humana"` na próxima
varredura e aparece em `"promovidos"` com `"confidence": "reviewed"` (ou
`"unverified"` se `agent-output`).

Se falhar: `--approve-all` sem `--source-type` → recusa antes de tocar em
qualquer arquivo, corrija o comando. Item não encontrado → confira `id`/
caminho dentro de `inbox/`.

### 5.4. Quarentena — proveniência incompleta

Quem decide: 👤 humano. Tipo **H** — decisão: corrigir a fonte na origem
(reingerir com `--origin`/`--source-type` corretos) ou descartar. Não há
comando `wk` para "aprovar quarentena" — a única saída é corrigir e
reingerir; ler `quarantine.md` e decidir o quê fazer é sempre humano.

Como saber que caiu em quarentena: item aparece em `"quarentena"` no JSON
de saída de `promote`, e uma linha é anexada a `$WK_STORE/quarantine.md`
com o motivo.

Se falhar (ficou preso em quarentena por engano): confira o frontmatter do
arquivo em `inbox/` — `origin` vazio ou `source_type` fora dos 5 valores
válidos é a causa mais comum; rode `wk ingest` de novo com os valores
corretos (o arquivo antigo em `inbox/` não é sobrescrito automaticamente —
remova-o manualmente antes de reingerir, se necessário).

### 5.5. Gate de `verify` (só fontes de `codescan`)

Quem decide o override: 👤 humano. Tipo **H** — ver critério completo e
risco na seção 4.24.

```bash
$WKPY "$WK" promote --allow-unverified --store "$WK_STORE"
```

Fontes que não vêm de `codescan` (transcrição, doc, clip) não passam por
esse gate — seguem só a regra de `source_type` (5.2).

<a id="sec-6"></a>
## 6. Compilação da wiki

**Classificação da seção: 100% D.** Verificado em `cmd_compile`
(`scripts/wk/cli.py`) e `operations/compile.md`: lê `raw/` com
`promoted: true`, escreve uma página por documento verbatim, sem
sintetizar/cruzar fontes, sem chamada a LLM.

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 6.1. `compile` — gera `wiki/` a partir de `raw/`

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" compile "projetos/migracao-erp" --store "$WK_STORE"
```

Sem `<topico>`, compila todos os tópicos promovidos.

Esperado: JSON com `"paginas"` (lista de caminhos gerados, index primeiro),
`"fontes"` (contagem) e `"reindexed"`.

Exemplo:
```json
{"paginas": ["wiki/index.md",
              "wiki/projetos/migracao-erp/human-transcript/sb-ingest-....md"],
 "fontes": 1, "reindexed": true}
```

Como saber que deu certo: `wiki/<topic>/<source_type>/<id>.md` existe e o
corpo bate com a fonte em `raw/` (verbatim + cabeçalho de proveniência).

Se falhar: `"fontes":0` → nada foi promovido para esse tópico; volte à
seção 5.

Não existe merge de múltiplas fontes numa página, não existe "sintetize" ou
"cruze com fontes relacionadas": a granularidade é 1 fonte promovida = 1
página. Cruzamento de conhecimento acontece na leitura (seção 9), não na
compilação.

### 6.2. Página de overview por tópico

Ver seção 4.29 para a tabela completa de seções da overview — regra
idêntica, chamada a partir daqui ou de dentro do pipeline de código.

### 6.3. Gate de `verify` e `--allow-unverified`

Quem decide: 👤 humano. Tipo **H** — mesmo critério da seção 4.24. `compile`
recusa (`exit 3`) tópicos cujo workdir de origem tem `verify` falho.

```bash
$WKPY "$WK" compile "projetos/migracao-erp" --allow-unverified --store "$WK_STORE"
```

<a id="sec-7"></a>
## 7. Exportação `.docx`

**Classificação da seção: 100% D.** Verificado em `cmd_docx`
(`scripts/wk/cli.py`) e `operations/docx.md`: lê `raw/` promovido
(mesma leitura de `compile`, não exige `compile` prévio), converte
markdown → OOXML mecanicamente, sem LLM.

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 7.1. `docx` — gera `wiki-docx/` a partir de `raw/`

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" docx "projetos/migracao-erp" --store "$WK_STORE"
```

Esperado: JSON com `documentos` (um por fonte promovida, com `path`, `id`,
`source_type`, `avisos`), `fontes`, `removidos` e `pulados`. Gera
`wiki-docx/<topic>/<source_type>/<arquivo>.docx`.

Como saber que deu certo: o `.docx` existe em `wiki-docx/` e abre no Word
com título/resumo derivados do corpo + seção de proveniência.

Se falhar: `pulados` não vazio → exit `1`; cada item lista `id` e motivo.

Idempotente: rodar de novo sobrescreve no mesmo caminho, nunca gera
`-2.docx`. Sem filtro de topic, poda toda a árvore `wiki-docx/` (remove
`.docx` órfão). Com `<topic>`, a poda fica restrita à subárvore do topic;
`--no-prune` desliga tudo.

### 7.2. `.docx` agregador por tópico

Igual à overview de `compile` (seção 4.29/6.2): `docx` gera
`wiki-docx/<topic>/index.docx` para todo tópico elegível, reusando
`_build_topic_overview`.

### 7.3. Diagramas Mermaid no `.docx`: degradação textual, nunca imagem

Não há rasterização nem dependência externa (mermaid-cli/puppeteer) — todo
diagrama vira texto/tabela estruturado:

| Tipo Mermaid | Vira no `.docx` |
|---|---|
| `flowchart` / `graph` | lista de nós/dependências |
| `erDiagram` | lista de entidades/relações |
| `sequenceDiagram` | lista ordenada Ator→Ator, com `alt`/`else`/`opt`/`loop`/`par` e `Note` refletidos como itens |
| `stateDiagram-v2` | tabela de transições (Origem / Evento-Condição / Destino) |
| `C4Context` / `C4Container` / `C4Component` | tabela de elementos + tabela de relações |
| `classDiagram` | tabela de classes + tabela de relações |
| outros (`pie`/`gantt`/`journey`/`mindmap`) ou Mermaid malformado | legenda + bloco de código bruto + nota apontando para a versão Markdown da wiki |

`coupling.html` **nunca** é convertido — pulado, contado em
`ignorados_asset_html`.

### 7.4. Gate de `verify` e `--allow-unverified`

Quem decide: 👤 humano. Tipo **H** — mesmo critério da seção 4.24.

```bash
$WKPY "$WK" docx "projetos/migracao-erp" --allow-unverified --store "$WK_STORE"
```

### 7.5. Transporte ao SharePoint: fora de escopo desta v1

`wk docx` **não** sobe nada para o SharePoint — não há, no projeto, nenhuma
chamada de rede, Graph API/REST ou dependência de biblioteca de upload.
Levar `wiki-docx/**/*.docx` até a biblioteca de documentos do SharePoint é
etapa operacional separada, humana (👤), fora do escopo v1 — ver seção 17.

<a id="sec-8"></a>
## 8. Lint e auditoria de integridade

**Classificação da seção: D (Nível 1 + grafo) e D+H (Nível 2).** Somente
leitura: não promove, não compila, não edita. Verificado em `cmd_lint`
(`scripts/wk/cli.py`).

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 8.1. Nível 1 — determinístico, sempre

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" lint --store "$WK_STORE"
```

Escreve `wiki/_lint-report.md` (sobrescreve) e devolve JSON com contagem por
regra:

| Regra | Pega |
|---|---|
| `L2_proveniencia_ausente` | fonte em `raw/` sem `origin`/`source_type` |
| `L1_canonico_so_de_agente` | página com fontes todas `agent-output` |
| `L5_supersedida_ainda_citada` | página que cita fonte substituída |
| `wiki_sem_fontes_declaradas` | página sem `sources:` |
| `fonte_orfa` | fonte promovida que ninguém cita |

Esperado: `"achados":0` para runbook concluído.

Exemplo de saída com achado:
```json
{"achados": 1, "report": "wiki/_lint-report.md",
 "regras": {"fonte_orfa": [{"id": "sb-ingest-...", "path": "raw/..."}]}}
```

Como saber que deu certo: `"achados": 0` e `wiki/_lint-report.md` gravado.

Se falhar (achados > 0): cada regra em `"regras"` lista os documentos
problemáticos; corrija na fonte (`raw/`/`wiki/` conforme a regra) e repita.
Sai `1` se houver achado.

### 8.2. Grafo — wikilinks e páginas órfãs

Quem executa: 👤 humano. Tipo **D** — travessia determinística de `wiki/`
(não usa o índice SQL).

| Regra | Pega |
|---|---|
| `W1_link_markdown_quebrado` | link markdown relativo cujo arquivo alvo não existe |
| `W2_wikilink_sem_alvo` | `[[wikilink]]` sem página correspondente (nem por stem, nem por `id`) |
| `W3_pagina_orfa` | página sem nenhum link de entrada (`index.md` e `_lint-report.md` isentos) |

Achados de W1/W2/W3 também geram exit 1. Já incluídos na mesma chamada de
8.1 — não é um comando separado.

### 8.3. Nível 2 — semântico, periódico

Quem executa a consulta: 👤 humano (ou 🤖, indiferente — é retrieval, não
geração). Tipo **D** para a execução da busca. Quem interpreta e decide o
que fazer com os candidatos: 👤 humano. Tipo **H** — decisão: o candidato
retornado é de fato uma paráfrase/contradição, e o que fazer a respeito.
Risco de decidir errado: ignorar uma contradição real deixa a wiki com
afirmações conflitantes sem sinalização; agir sobre um falso positivo
remove/rebaixa conteúdo válido. Não é **M**: nenhum passo aqui exige um
LLM — a busca é BM25/vetorial determinística, e a leitura dos resultados
pode ser feita por um humano sozinho (ainda que ajuda de um modelo torne a
triagem mais rápida — conveniência, não requisito).

Não leia o corpus inteiro; gere candidatos por recuperação (ver seção 9).

L4 — realimentação:
```
$WKPY "$WK" search "intent: agent-output que parafraseia pagina derivada de agent-output
vec: <tese do candidato, uma frase>" --filter source_type=agent-output --store "$WK_STORE"
```
Profundidade > 1 → BLOQUEIA promoção (decisão humana subsequente).

L3 — contradição (por afirmação-chave):
```
$WKPY "$WK" search "intent: fontes que afirmam algo diferente sobre este ponto
vec: <a afirmação>" -c raw --filter promoted=1 --store "$WK_STORE"
```
Divergência → registre, prefira maior `confidence` / origem humana. Não
resolva em silêncio — decisão explícita e registrada (👤, **H**).

Como saber que deu certo: não há critério binário aqui (é revisão
periódica, não gate); "concluído" = você revisou os candidatos retornados e
registrou a decisão (corrigir fonte, ignorar, ou escalar).

Se falhar (`vec:`/`hyde:` sem embeddings configurados): erro `"vec/hyde
exigem embeddings; índice em modo léxico"` — use somente `lex:`, ou
configure `AZURE_OPENAI_*` (não validado neste projeto, seção 17).

<a id="sec-9"></a>
## 9. Indexação e busca (retrieval)

**Classificação da seção: 100% D no modo padrão.** Verificado em
`cmd_search`/`cmd_get` (`scripts/sbindex/cli.py`): fusão RRF
determinística sobre listas de resultados léxicos/vetoriais — nenhuma
geração de prosa por LLM em nenhum dos dois comandos. `wk search` no modo
padrão (`lex:`) não depende de nenhum serviço externo.

> 🚨 Travou neste fluxo? [11. Retomada de fluxo interrompido e recuperação
> de erro](#sec-11).

### 9.1. `wk index reindex` — reconstrói o índice

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" index reindex --store "$WK_STORE"
```

Esperado: JSON com contagem de documentos indexados/removidos. Roda
automaticamente ao final de `promote`/`compile` — chamar manualmente serve
para depurar um índice suspeito de estar dessincronizado.

Como saber que deu certo: `wk index status` (9.2) reporta `"sujo": false`.

Se falhar: caminho de `--store` inválido, ou `index.db` corrompido —
delete `$WK_STORE/index.db` e rode `reindex` de novo (é reconstruível e
deletável, seção "Arquitetura e fluxo" no topo).

### 9.2. `wk index status` — saúde do índice

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" index status --store "$WK_STORE"
```

Esperado: JSON com `"sujo"` (bool) e contagens. `"sujo": true` bloqueia
`compile`/`lint` — rode `reindex` (9.1) antes de continuar.

### 9.3. `wk search` — busca híbrida

Quem executa: 👤 humano. Tipo **D**. Query de uma linha sem prefixo vira
`lex:` automaticamente (`_default_search_query`, `scripts/wk/cli.py`)
— o modo padrão nunca depende de embeddings.

```bash
$WKPY "$WK" search "prazo de cancelamento de apólice" --store "$WK_STORE"
```

Esperado:
```
#a1b2c3d4  0.0842  raw  raw/transcripts/sb-ingest-reuniao-....md [!unverified-source]
    Prazo de cancelamento
    O cliente pode cancelar em até 7 dias corridos após a contratação...
```

`[!unverified-source]` sinaliza `source_type: agent-output` no resultado —
leia com ressalva.

Como saber que deu certo: retorno com pelo menos um `#docid`; `(sem
resultados)` não é erro, é ausência de match.

Se falhar: `vec:`/`hyde:` sem embeddings configurados → erro explícito
pedindo `AZURE_OPENAI_*` ou uso de `lex:` (ver seção 17 — não validado
nesta v1). Query multi-linha exige prefixo explícito em cada linha
(`lex:`/`vec:`/`hyde:`) — sem prefixo numa linha de documento multi-linha,
o motor recusa com "linha inválida no query document".

### 9.4. `wk get` — recupera um documento

Quem executa: 👤 humano. Tipo **D**.

```bash
$WKPY "$WK" get "a1b2c3d4" --store "$WK_STORE"
```

Se falhar: `"não encontrado"` → o JSON de erro já traz `"sugestoes"` com
até 5 candidatos por `LIKE` no caminho; use um deles.

### 9.5. `wk audit` (índice) — regras L1/L2/L5 isoladas

Quem executa: 👤 humano. Tipo **D**. Mesmo motor de `wk lint` (seção 8.1),
sem filtro de caminho e sem gravar relatório em disco — só o JSON.

```bash
$WKPY "$WK" audit --store "$WK_STORE"
```

<a id="sec-10"></a>
## 10. Modo skill (slash commands)

Alternativa ao runbook manual: em vez de digitar a sequência das seções
1–9, você invoca a operação e o agente (🤖) executa o pipeline por você.
Mesmo aqui, todo passo continua com sua classificação D/M/H de origem — o
agente só está disparando, em sequência, os mesmos comandos `wk` que você
digitaria à mão. As oito operações existentes:

| Slash command | O que faz | Equivale, na CLI, a | D/M/H |
|---|---|---|---|
| `/wiki-ai ingest <arquivo>` | registra fonte solta (transcrição, doc, planilha, XML, clip) em `inbox/`; docx/xlsx/csv/pdf viram asset imutável em `raw/assets/` + página de fonte, e o agente escreve a análise (portão agent-output) | `wk ingest <arquivo> --source-type <t> --origin "<origem>" --topic <slug>` (seções 2/3) | D (ingest) + M (análise de asset, seção 3.2) |
| `/wiki-ai ingest codebase <caminho> --topic <slug>` | pipeline de repositório inteiro (seção 4, do `surface` ao `lint`) | `wk code --repo <r> --store <s> surface --topic <slug>` e o restante da seção 4 | D + M (5 pontos de fan-out) + H (config/pending/promote não-`code-repo`) |
| `/wiki-ai promote` | portão de promoção `inbox/` → `raw/` (seção 5) | `wk promote --store <s>` | D + H (aprovação não-`code-repo`) |
| `/wiki-ai compile` | (re)gera `wiki/` a partir de `raw/` promovido (seção 6) | `wk compile <topic> --store <s>` | D |
| `/wiki-ai docx` | (re)gera `wiki-docx/` (DOCX) a partir de `raw/` promovido, para bibliotecas SharePoint (seção 7) | `wk docx <topic> --store <s>` | D |
| `/wiki-ai lint` | audita integridade, somente leitura (seção 8) | `wk lint --store <s>` | D (+ H no nível 2) |
| `/wiki-ai query <pergunta>` | consulta a wiki (seção 9) | `wk search "<pergunta>" --store <s>` | D |
| `/wiki-ai reindex` | atualiza o índice de busca (seção 9) | `wk index reindex --store <s>` | D |

No modo skill, o agente dispara os comandos `wk` (D) e também escreve o
conteúdo quando o passo exige (M — ex.: a análise de um `ingest`, ou os
blocos SDD do fan-out). Mas isso não elimina o portão humano: `promote` de
uma fonte com `source_type` diferente de `code-repo` continua exigindo
`--approve`/`--approve-all` explícito, decisão **H**, mesmo disparado de
dentro da skill (`scripts/wk/cli.py`). O modo skill acelera o disparo,
não substitui a aprovação humana onde ela é exigida.

### 10.1. Regra de entrada do slash command

Slash command **não é shell**: recebe texto literal e não expande variáveis do
Git Bash. Use caminho e topic literais.

Válido:

```text
/wiki-ai ingest codebase C:/Users/User/projetos/desafio-tech-lead/insurance-quote-service --topic codebases/insurance-quote-service
```

Inválido por design (chega como texto, não como valor):

```text
/wiki-ai ingest codebase $WK_REPO --topic $WK_TOPIC
```

Inválido (mistura operação da skill com invocação do executável):

```text
/wiki-ai ingest python 'C:/.../wk.pyz' index status codebase ...
```

### 10.2. Quando usar cada modo

| Situação | Modo |
|---|---|
| Repositório novo, ponta a ponta, com fan-out de subagentes | `/wiki-ai ingest codebase ...` (§4) |
| Retomar pipeline parado no meio | CLI, seção 11 (`state` → `next`) |
| Depurar um estágio específico | CLI, o passo correspondente da seção 4 |
| Arquivo solto (transcrição, doc, planilha, PDF, XML, VTT) | `/wiki-ai ingest <arquivo>` ou `wk ingest` (§2/§3) |
| Publicar para bibliotecas SharePoint/Copilot Studio | `/wiki-ai docx` ou `wk docx <topic> --store <s>` (§7) |
| Auditar sem alterar nada | `/wiki-ai lint` ou `wk audit --store <s>` (§8/§9) |
| Quero fazer tudo sozinho, sem modelo | CLI puro, seções 1, 2, 5, 6, 7, 8, 9 — nunca a seção 10 |

### 10.3. Pré-requisito

O slash command só existe depois de `wk init --engine <e>` (passo 1.3), que
materializa a skill no diretório da engine (`.claude/skills/wiki-ai` no Claude
Code, `.agents/skills/wiki-ai` nas demais). Verifique com:

```bash
$WKPY "$WK" check --engine devin --store "$WK_STORE" --repo "$WK_REPO"
```

<a id="sec-11"></a>
## 11. Retomada de fluxo interrompido e recuperação de erro

<a id="sec-11-1"></a>
### 11.1. Retomada

Perdeu o fio? Nesta ordem, sem adivinhar — todos os quatro comandos são
**D**, 👤 ou 🤖 indiferente:

```bash
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" state
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" next
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" read <arquivo-do-repo> --from <linha> --count <n>
$WKPY "$WK" code --repo "$WK_REPO" --store "$WK_STORE" audit --strict
```

- `state`: estado completo do workdir por padrão; `--quiet` condensa a
  uma linha (`estagio_atual` sobrevive à condensação por ser escalar).
- `next`: o que fazer agora, sem reconstruir o histórico à mão.
- `read`: leitura confinada ao repo declarado, numerada — nunca leia o
  repo por fora do `wk` (sem `cat`, sem editor, sem IDE apontando pra fora
  do contrato).
- `audit --strict`: mede o que já está bloqueando antes de continuar;
  se nada foi checado ainda, reporta `"status":"sem_evidencia"` em vez de
  `pass` (seção 4.26) — nunca trate ausência de evidência como aprovação.

Achou o estágio/item a corrigir? `redo <stage> [--item <item>]` reabre:
marca os runs `current` cobertos como `superseded` (preserva a trilha) e
devolve o(s) item(ns) de `done` para `pending`. Não apaga nem reescreve o
run anterior — só destrava para um novo `merge-agent-output` do mesmo
item. `redo` é **H** decide · **D** executa: a ferramenta nunca reabre um
estágio sozinha; só o operador (👤) — ou o orquestrador da skill relatando
a decisão a um humano — tem esse critério.

Nunca use `python -c`, heredoc ou script ad hoc para ler JSON de
surface/state — use os comandos acima.

<a id="sec-11-2"></a>
### 11.2. Erros comuns

| Sintoma | Causa | Correção | Quem corrige |
|---|---|---|---|
| `wk ingest codebase ...` recusado | `ingest codebase` não é subcomando do CLI — é a operação da skill | rode o fluxo da seção 4, a partir de `code ... surface` | D · 👤 |
| saída truncada/estranha, algo como `No linha:N caractere:` ou cheia de `~~` | comando rodou em PowerShell, não Git Bash | reexecute em Git Bash; se preso em PowerShell, embrulhe: `bash -c '...'` | D · 👤 |
| `store não informado: sem --store e sem WK_STORE, nada seria gravado no lugar certo` | faltou `--store` e `WK_STORE` não está no ambiente | passe `--store "$WK_STORE"` ou `export WK_STORE=...` (seção 1) | D · 👤 |
| `flag(s) --topic pertence(m) ao subcomando 'surface', não ao nível superior` | flag do subcomando escrita antes do subcomando | mova a flag para depois do subcomando — a própria mensagem traz o comando corrigido em `"acao"` | D · 👤 |
| `merge-agent-output exige --agent <identificador do subagente>` | `--agent` ausente | repita com `--agent <agent_slot-do-batch>` | D · 👤 |
| `--agent '...' é genérico demais` | usou `main`/`orquestrador`/`orchestrator`/`self`/`principal` | use o `agent_slot` real do batch (ex.: `modules-b01`) | D · 👤 |
| `input ... tem mtime anterior ao plano de fan-out` | `--input` é arquivo velho (sobra de tentativa anterior) | grave o output de novo, depois de rodar `run-stage` | M · 🤖 (reescrever o output) |
| `fan-out obrigatório não cumprido em <stage>` | `done` bloqueado: todos os batches vieram do mesmo `--agent` | refaça `merge-agent-output` do(s) batch(es) faltante(s) com `--agent` realmente distinto | M · 🤖 (novo batch) + D · 👤 (merge) |
| `item já mergeado; rode wk code ... redo <stage> --item <item> antes de refazer` | reenvio de `merge-agent-output` sobre item que já tem run `current` registrado (BQ4) | rode `redo <stage> --item <item>` primeiro (reabre o item), depois refaça o `merge-agent-output` | H · 👤 (`redo`) + M · 🤖 (refaz) |
| `P0: agent-runs schema legado (wiki-ai.agent-runs.v1) sem prova criptográfica` | manifesto `agent-runs` antigo (v1), sem hash | refaça o estágio citado com o merge atual (schema v2) | M · 🤖 |
| `P0: artefato alterado após o merge` | artefato SDD editado à mão depois do merge (hash não bate) | não edite artefato manualmente; refaça `merge-agent-output` do estágio | H · 👤 (não edita) + D · 🤖/👤 reroda |
| `P0: artefato ausente/alterado após o merge` com o arquivo existindo em disco | `--store` relativo causava duplicação de path — **corrigido**: `main()` normaliza `a.store` com `os.path.abspath` (`scripts/codescan/cli.py`) antes de qualquer dispatch | atualize o `wk.pyz`; se persistir, confirme que está passando `--store` (relativo já é seguro agora) | D · 👤 |
| `ruído rejeitado: <regras>` (ex.: `agent_output_too_long, edit_echo`) | saída do subagente passou de 220 linhas ou ecoou uma edição | a 1ª linha é o resumo legado; logo abaixo vêm até 10 violações detalhadas (`[regra] linha N: trecho (padrão: ...)`, com "+N outra(s), total M" quando há mais de 10) — corrija só os trechos apontados, não reescreva o artefato inteiro | M · 🤖 |
| `Mermaid ... label nao quoted/sanitizado` (ou qualquer outro `(padrão: ...)`) | sintaxe Mermaid inválida dentro de um bloco `mermaid`, ou diagrama obrigatório ausente num artefato de `architecture` (seção 4.13) | o erro traz linha absoluta do arquivo + trecho ofensivo + nome do padrão; corrija o trecho — **não remova o diagrama**. Se genuinamente não há diagrama a desenhar, use `<!-- no-diagram: <motivo> -->` (vira warning, não blocker) | M · 🤖 |
| `run-stage`/`merge-agent-output`/`agent-pack`/`redo` com `stage=evidence` | `evidence` não tem fan-out (não está em `CRITICAL_STAGES`) — o guard dedicado (`_evidence_stage_hint`, `scripts/codescan/cli.py`) devolve JSON `{"error":..., "acao":...}` em stderr com **exit 2**, não mais o `argparse: invalid choice` cru | rode `wk code evidence --topic <t>` direto (seção 4.19) — a própria mensagem já indica isso em `"acao"` | D · 👤 |
| pack JSON em linha única, parecendo truncado | serialização compacta em versões antigas | **corrigido**: packs gravam com `indent=2` em disco (`dump_compact_json`), o orçamento de tamanho (45 KB/batch) continua medido pela forma compacta (`compact_json_size`) — não é o mesmo texto que você lê; se ainda vir linha única, atualize o `wk.pyz` | D · 👤 |
| `promote`/`compile`/`docx` recusam com menção a `verify` | gate real: `state.json.stages.verify.status == "failed"` para o `topic` em questão (seção 4.24) | corrija a citação e refaça `verify`, ou decida conscientemente com `--allow-unverified` | H · 👤 |
| `wk doctor` reporta `wk.pyz.pyz_desatualizado: true` | o `.pyz` em uso é mais antigo que o `scripts/` ao lado dele | rode `python scripts/build_pyz.py` (é diagnóstico, não bloqueia — `"bloqueios"` continua vazio) | D · 👤 |

Versionamento do manifesto: `merge-agent-output` continua gravando
`wiki-ai.agent-runs.v2`, como sempre. `redo` é o único comando que
reescreve o manifesto para `wiki-ai.agent-runs.v3` (mesmo schema, agora
com `status`/`supersedes` explícitos por run) — `audit` aceita v2 e v3
como válidos; só v1 (legado, sem hash) continua bloqueado
(`scripts/codescan/sdd.py`).

<a id="sec-12"></a>
## 12. Contratos de bloco por estágio

| Estágio | Cabeçalho aceito | Artefato gerado | Quem escreve |
|---|---|---|---|
| modules | `=== MODULE: <path> === ... === END ===` | `modules/<slug-do-path>.md` | M · 🤖 escreve o bloco · D · a ferramenta grava |
| rules | `=== RULES: domain \| state-machines \| permissions \| adrs/NNN-<slug> ===` (state-machines/permissions/adrs só em `doc_level` completo/detalhado) | `sdd/<nome>.md` / `sdd/adrs/NNN-<slug>.md` | M · 🤖 escreve o bloco · D · a ferramenta grava |
| architecture | `=== ARCHITECTURE: architecture \| c4-context \| c4-containers \| c4-components \| erd-complete \| traceability/spec-impact-matrix \| sequences/<slug> ===` (por `doc_level`; `sequences/` só em detalhado) | `sdd/<nome>.md` | M · 🤖 escreve o bloco · D · a ferramenta grava |
| specs (unit) | `=== SPEC: <unit> ===` com sub-blocos `--- requirements.md ---` / `--- design.md ---` / `--- tasks.md ---` (+ opcionais `--- contracts.md ---` / `--- edge-cases.md ---`; pedidos pelo `sdd-brief` em `doc_level` detalhado, não exigidos por `done specs <unit>`) | `sdd/specs/<slug-da-unit>/*.md` | M · 🤖 escreve o bloco · D · a ferramenta grava |
| specs (doc nomeado) | `=== SPEC: confidence-report \| gaps \| traceability/code-spec-matrix \| user-stories/<slug> \| openapi/<slug> ===` (corpo livre) | `sdd/<nome>.md` (`openapi/` grava `.yaml`) | M · 🤖 escreve o bloco · D · a ferramenta grava |
| synth | `=== SYNTH: confirmed \| inferred ===` | `sdd/confirmed.md` / `sdd/inferred.md` | M · 🤖 escreve o bloco · D · a ferramenta grava |

Contrato compacto e exato de cada estágio (seções obrigatórias, limites de
linha, regras de "sem prosa"): `wk code --repo "$WK_REPO" --store
"$WK_STORE" sdd-brief <stage>` (`stage` em `evidence, modules, rules,
architecture, specs, synth`) — rode antes de escrever o subagente.

`sdd-brief` é **D**: gera o contrato, não o cumpre — é puramente
determinístico, sem chamar LLM. Quem cumpre o contrato — escreve os
blocos `=== ... ===` no formato exigido — é sempre **M** (🤖, subagente); a
ferramenta nunca gera esse conteúdo, só valida a forma e grava o artefato
via `merge-agent-output` (**D**).

<a id="sec-13"></a>
## 13. Gates

| Gate | Bloqueia | Quem é impedido |
|---|---|---|
| `audit` (gates P0; `--strict` é só compatibilidade) | falso positivo | M · 🤖 |
| `agent-pack v2` | leitura ampla do repo | M · 🤖 |
| `merge-agent-output` | escrita manual de SDD; `--agent` ausente/genérico; input com `mtime` anterior ao plano do stage; item já coberto por run `current` sem `redo` prévio | H · 👤 / M · 🤖 |
| `agent-runs` (schema `wiki-ai.agent-runs.v2`; vira `v3` depois de `redo`) | stage `done` sem subagente; manifesto v1 (legado) ou artefato/input alterado após o merge (hash `sha256` recomputado pelo audit) | M · 🤖 |
| `done --artifact` | fechar `done` com artefato que nunca passou por `merge-agent-output`; ou todos os batches registrados pelo mesmo `--agent` | M · 🤖 |
| `export --output` | gravar direto em `raw/`/`wiki/` do store (use `publish`) | D · 👤 |
| `code` sem store | execução sem `--store` explícito e sem `WK_STORE` no ambiente | D · 👤 |
| `ingest codebase` (CLI) | subcomando inexistente — erro aponta `code ... surface` | D · 👤 |
| `verify` (ver 4.24) | `promote` (por item, escopado por `topic`), `compile`/`docx` (escopados pelo `topic` do comando) recusam quando `state.json.stages.verify.status == "failed"`; `publish` NÃO bloqueia, só anota `aviso_verify`. Override: `--allow-unverified` (H) | D (execução) · H (override) |
| workdir hygiene | `*.py`, `*.txt`, `src/`, specs vazias | M · 🤖 |
| Mermaid strict | diagrama vazio, denso, sem aresta, label inválido | M · 🤖 |
| noise | log, diff, `Ran command`, `Edited`, `Wrote` | M · 🤖 |

<a id="sec-14"></a>
## 14. Proibido

### 14.1. Bloqueado por código

- script auxiliar no workdir — vale para M · 🤖.
- copiar repo para `store/.codescan` — vale para H · 👤 (o subagente não tem
  como copiar o repo; é erro de quem monta o ambiente).
- pasta vazia em `sdd/specs` — vale para M · 🤖.
- pasta vazia em `modules` — vale para M · 🤖.
- output fora dos blocos parseáveis — vale para M · 🤖.

### 14.2. Convenção (não bloqueado — disciplina do operador)

- PowerShell — vale para D · 👤.
- misturar `/wiki-ai ingest` com comando `python` — vale para D · 👤.
- log colado no chat — vale para M · 🤖.
- diff colado no chat — vale para M · 🤖.
- artefato recém-escrito colado no chat — vale para M · 🤖.
- `python -c`, heredoc ou script ad hoc para ler JSON de surface/state; use
  `state`, `next`, `read` e `audit` — vale para D · 👤 e D · 🤖 (CLI manual e
  orquestrador da skill).

<a id="sec-15"></a>
## 15. Referência rápida de comandos

| Comando | Para quê | D/M/H |
|---|---|---|
| `wk doctor` | diagnóstico único do ambiente (shell, python, store, repo, engine); também reporta `wk.pyz.pyz_desatualizado` (diagnóstico, não bloqueia) | D |
| `wk init --engine <e> [--store --repo]` | materializa a skill + grava permissões | D |
| `wk check --engine <e> [--store --repo]` | compara disco vs. embutido; valida permissões | D |
| `wk engines [--base]` | lista engines suportadas e o que já está instalado | D |
| `wk store init [caminho]` | cria `inbox/`/`raw/`/`wiki/` do store | D |
| `wk code ... surface --topic <slug>` | estágio 1: mapa determinístico do repo | D |
| `wk code ... export --topic <slug>` | inventory/dependencies/coupling — sem LLM; coupling usa motor Java ou genérico conforme o repo, e grava `coupling.html` local quando Java | D |
| `wk code ... config --doc-level --granularity` | grava decisões do SDD (obrigatório antes de plan) | H decide · D grava |
| `wk code ... plan` | lista módulos a cavar, por LOC | D |
| `wk code ... pending <stage> --items <lista>` | registra pendências de um estágio | H decide · D grava |
| `wk code ... run-stage <stage>` | manifesto de fan-out para subagentes — prepara, não gera conteúdo | D |
| `wk code ... sdd-brief <stage>` | contrato compacto do estágio para o subagente — prepara, não gera conteúdo | D |
| `wk code ... sdd-scaffold <stage>` | cria esqueleto dos artefatos SDD do estágio — prepara, não gera conteúdo. **Candidato a remoção**: fora do happy path do runbook (seção 4 não o usa); decisão registrada em [docs/implementacao-conhecimento-codebase.md](docs/implementacao-conhecimento-codebase.md) §9.7 ("Remover `sdd-scaffold` do happy path; avaliar sua remoção definitiva") | D |
| `wk code ... agent-pack <stage> --batch N` | pacote determinístico de um batch (todos os estágios; `modules` lê o repo, os demais leem o material do workdir) — prepara, não gera conteúdo | D |
| `wk code ... merge-agent-output <stage> --input --agent` | integra a saída de um subagente | D |
| `wk code ... redo <stage> [--item]` | reabre stage/item: runs `current` viram `superseded`, `done` volta a `pending` | H decide · D executa |
| `wk code ... evidence --topic <slug>` | evidence-pack por tópico | D |
| `wk code ... verify --artifact <md>` | valida citações `arquivo:linha` | D |
| `wk code ... done <stage> [--item --artifact]` | marca estágio/item concluído | D |
| `wk code ... blocked\|failed\|degraded <stage>` | registra item com problema | D |
| `wk code ... state` | estado completo do workdir | D |
| `wk code ... next` | o que fazer agora | D |
| `wk code ... read <path> --from --count` | lê arquivo do repo, numerado | D |
| `wk code ... audit [--strict]` | mede qualidade SDD sem alterar estado | D |
| `wk code ... cleanup` | apaga o clone de um repo remoto (após o término) | D |
| `wk publish --workdir --topic` | leva a árvore SDD do workdir para `inbox/`, incluindo `coupling.html` como asset (nunca bloqueado por `verify`, só anota `aviso_verify`) | D |
| `wk promote [--approve \| --approve-all --source-type] [--allow-unverified]` | move fontes seguras de `inbox/` para `raw/`; bloqueia por item/`topic` se `verify` falhou (seção 4.24) | D (`code-repo`) · H (demais) |
| `wk compile [topic] [--allow-unverified]` | gera `wiki/` a partir de `raw/` promovido, com `wiki/<topic>/overview.md` quando elegível (seção 4.29) | D |
| `wk docx [topic] [--out-dir --no-prune] [--allow-unverified]` | gera `wiki-docx/` (DOCX) a partir de `raw/` promovido, com `wiki-docx/<topic>/index.docx` agregador quando elegível (seção 4.30) | D |
| `wk lint [path]` | audita o índice, escreve `wiki/_lint-report.md` | D |
| `wk ingest <arquivo> --source-type --origin --topic` | registra fonte em `inbox/` com proveniência: md/txt passthrough; vtt/srt transcrição; html; xml → análise estrutural (draw.io/XMI/outline); json → bloco de código; docx/xlsx/csv/pdf → original imutável em `raw/assets/` + página de fonte para análise (M) | D (ingest) — a análise subsequente de docx/xlsx/csv/pdf é M, seção 3 |
| `wk index reindex\|status` | manutenção do índice | D |
| `wk search "<query>"` / `wk get <ref>` | busca híbrida / recupera documento — query de 1 linha sem prefixo vira `lex:` automaticamente | D |
| `wk audit` | regras determinísticas L1/L2/L5 do índice | D |
| `wk docs [nome] [--list]` | documentação embutida no executável | D |

<a id="sec-16"></a>
## 16. Glossário

| Termo | Significado |
|---|---|
| store | raiz de dados do Wiki AI (`inbox/`, `raw/`, `wiki/`, `wiki-docx/`); separada da skill |
| workdir | diretório de trabalho do pipeline de código, `store/.codescan/<repo>-<hash>` |
| topic | slug que agrupa fontes/páginas por assunto (ex. `codebases/insurance-quote-service`) |
| estágio (stage) | fase do pipeline de código (`surface`, `modules`, `rules`, `architecture`, `specs`, `evidence`, `synth`, `verify`) |
| D — determinístico | passo `wk` que roda sozinho, sem LLM — a mesma entrada sempre produz a mesma saída (ex. `surface`, `export`, `run-stage`, `merge-agent-output`); pode ser disparado por 👤 ou 🤖, indiferente |
| M — requer modelo | passo que só um LLM (🤖) pode cumprir — sem ele, o fluxo para (ex. os 5 pontos de fan-out do pipeline de código, a análise de asset da seção 3) |
| H — decisão humana | passo que exige julgamento/autorização de 👤 — não é técnico, é governança (ex. aprovar `promote`, decidir `doc_level`, decidir `redo`) |
| subagente | processo de LLM (🤖) disparado pelo fan-out para escrever o conteúdo de um batch; nunca é o orquestrador principal, nunca lê o repo por fora do pacote que recebeu |
| fan-out | disparo de N subagentes, um por batch, exigido nos estágios `modules`/`rules`/`architecture`/`specs`/`synth` |
| batch | fatia disjunta de um estágio, com `output` e `agent_slot` próprios, gerada por `run-stage` |
| agent_slot | identificador único do subagente de um batch (ex. `modules-b01`); exigido em `--agent`, recusa valor genérico |
| SDD | árvore de artefatos determinísticos/sintetizados do pipeline de código (`inventory`, C4, ERD, specs por unit) sob `sdd/` no workdir |
| proveniência | metadados obrigatórios de origem de um documento: `source_type`, `confidence`, `origin` |
| promoção | passagem de um arquivo de `inbox/` para `raw/` via `promote`, único portão para fonte-verdade |
| asset imutável | original de `.docx`/`.xlsx`/`.csv`/`.pdf` gravado direto em `raw/assets/`, fora do fluxo de promoção |
| gate | verificação que bloqueia uma ação até uma condição ser satisfeita (ex. índice sujo bloqueia `compile`/`lint`) |
| evidence-pack | pacote de evidências `arquivo:linha` por tópico, gerado por `evidence`, usado para escrever `confirmed.md`/`inferred.md` |
| doc_level | granularidade de conteúdo do SDD (`essencial`/`completo`/`detalhado`), decisão obrigatória via `config` (H) |
| granularity | unidade de particionamento do plano do SDD (`module`/`endpoint`/`use-case`/`hybrid`/`feature`/`custom`) |
| agent-runs | manifesto de proveniência de um estágio (schema `wiki-ai.agent-runs.v2`; vira `v3` depois de `redo`), exigido por `done` e checado por `audit` |
| redo | comando que reabre stage/item: marca runs `current` como `superseded` e devolve `done` para `pending`, sem apagar a trilha anterior |
| superseded | status de um run de `agent-runs` que foi substituído por `redo`; continua no manifesto, só deixa de contar como cobertura vigente do item |

<a id="sec-17"></a>
## 17. Limitações conhecidas

- `wk docx` não transporta nada para o SharePoint — não há chamada de
  rede, Graph API/REST ou biblioteca de upload no projeto; é decisão
  deliberada, fora de escopo desta v1. Alternativa até existir automação
  própria: sincronização de pasta via cliente OneDrive/SharePoint apontado
  para `wiki-docx/`, ou upload manual periódico pela interface web do
  SharePoint ([operations/docx.md](operations/docx.md)) — sempre D · 👤.
- `wiki/` e `wiki-docx/` podem divergir entre si: não há gate de
  consistência entre as duas árvores, e elas nem se comportam igual —
  `compile` reescreve páginas a partir de `raw/` mas não poda página órfã,
  enquanto `wk docx` poda `.docx` órfão por padrão (`scripts/wk/cli.py`,
  `cmd_compile` vs. `cmd_docx`).
- Permissões de engine: só `claude-code` tem `"formato": "verificado"`
  (schema real de `.claude/settings.json`, comprovadamente consumido pela
  engine); `antigravity`/`devin`/`copilot` são `"formato": "best-effort"` —
  o arquivo é gravado, mas nada garante que a engine o consuma
  (`README.md` §1.3).
- `README.md` não é embutido no `.pyz`: não consta em `DOCS` de
  `scripts/build_pyz.py`. `schema.md`, `INSTALL.md` e `operations/*.md`
  constam e são lidos via `wk docs <slug>` em runtime — uma mudança neles
  exige rebuild do `.pyz` e `wk init --force` para propagar aos diretórios
  de skill das engines.
- `store init` não cria `wiki-docx/`: essa árvore só existe sob demanda,
  criada na primeira execução de `wk docx` (`INSTALL.md`;
  `STORE_TREE` em `scripts/wk/cli.py` não lista `wiki-docx`).
- Azure OpenAI (embeddings) não validado: `vec:`/`hyde:` dão erro de
  propósito em vez de retornar vazio; só o modo léxico (`lex:`) é testado
  (`INSTALL.md`). Mitigação: uma query de texto livre em uma linha, sem
  prefixo, vira `lex:` automaticamente — reduz o risco de cair sem querer
  em `vec:`/`hyde:` sem embeddings configurados (`scripts/wk/cli.py`,
  `_SEARCH_MODE_PREFIXES`).
- Rescan de codebase divide artefatos por confiança, não por tipo: uma
  reanálise (`surface`/`export` de novo) produz reescrita completa da
  árvore SDD, não um diff (`INSTALL.md`). Mitigação parcial: para
  corrigir só um item, sem reescrever o estágio inteiro, use
  `redo <stage> --item <item>` (seção 11.1) — reabre aquele item, preserva
  o resto da árvore.
- Resumo (`dc:description`) de alguns `.docx` sai como placeholder
  genérico ("Artefato \<source_type\> do tópico ..."), não como texto
  real: `derive_summary` só extrai resumo de um parágrafo corrido sob
  heading em `SUMMARY_SECTIONS`, ou do primeiro parágrafo não-boilerplate
  do corpo. Fontes compostas só por tabela/lista — como `inventory.md`
  ou `coupling.md`, gerados por `codescan export` — não têm parágrafo
  corrido e caem no placeholder (`scripts/wk/docx_meta.py`,
  `derive_summary`, `BOILERPLATE_PREFIXES`, `SUMMARY_SECTIONS`).
- **Fluxos ausentes** (verificado por ausência de subcomando em
  `scripts/wk/cli.py`, `scripts/codescan/cli.py` e `scripts/sbindex/cli.py`
  — não existe `add_parser` correspondente):

| Fluxo | Por que não existe |
|---|---|
| Publicação automática no SharePoint | `wk docx` só escreve `.docx` local; nenhuma chamada de rede/Graph API no projeto (ver acima) |
| Transcrição automática de áudio/vídeo (OCR/ASR) | `wk ingest` recusa explicitamente áudio/imagem — "formato fora de escopo", sem OCR/transcrição embutidos (`operations/ingest.md`) |
| Remover/despromover uma fonte (`unpromote`/`delete`) | não há `add_parser("unpromote")`/`("remove")`/`("delete")` em `scripts/wk/cli.py`; a única forma de tirar algo de `raw/`/`wiki/` é editar/mover manualmente fora do fluxo normativo |
| Rescan incremental (diff) de um repositório já processado | reanálise reescreve a árvore SDD inteira, não gera diff; mitigação parcial via `redo --item` (acima) |
| Deduplicação automática de fontes semelhantes | não existe comando `dedupe`; a seção 8.3 (lint L4, `agent-output` que parafraseia) é o mecanismo mais próximo, e é semiautomático (retrieval + decisão humana) |
