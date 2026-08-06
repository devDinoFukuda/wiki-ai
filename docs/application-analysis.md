# Análise da aplicação Wiki AI

Status: diagnóstico inicial, reconstruído do código e da documentação em 2026-08-02.

## Resumo executivo

A aplicação é um zipapp Python local (`wk.pyz`) com dois contextos principais:

1. Governança do corpus: `ingest -> promote -> compile -> lint`, com proveniência em frontmatter e índice SQLite/FTS5.
2. Engenharia reversa de codebases: `surface -> ... -> verify`, com checkpoints, fan-out de subagentes e integração controlada de artefatos SDD.

O desenho de confiança é coerente e os 293 testes passam. A cobertura da produção medida nesta análise foi 84%, e o conteúdo empacotado em `wk.pyz` corresponde ao fonte atual. Os riscos mais importantes estão nas fronteiras de filesystem, na reconciliação incremental da wiki e em diagnósticos que podem declarar um estado saudável incorretamente.

## Contextos e fluxo de domínio

### Governança do corpus

```text
Fonte externa
  -> Candidato (inbox, não canônico)
  -> Decisão de promoção
      -> Fonte promovida (raw, imutável)
      -> Decisão humana pendente
      -> Registro de quarentena
  -> Página derivada (wiki)
  -> Documento/chunks do índice
  -> Busca e auditoria
```

Invariantes confirmadas:

- `raw/` é a fonte-verdade imutável (`schema.md:3`).
- leitura usa somente `raw/` e `wiki/` (`schema.md:29`).
- `agent-output` promovido continua `unverified` (`schema.md:43`).
- uma página da wiki declara as fontes que a originaram, habilitando L1/L5 por SQL (`operations/compile.md:25-32`).

### Engenharia reversa de codebases

```text
Codebase
  -> Surface determinística
  -> Estado e plano de estágio
  -> Batch atribuído a subagente
  -> Saída parseável
  -> Merge com manifesto/proveniência
  -> Artefato SDD validado
  -> Publish como candidato do corpus
```

O orquestrador não deve produzir SDD diretamente; `run-stage` registra o fan-out e `merge-agent-output` é o portão de integração (`SKILL.md:77-82`, `scripts/codescan/cli.py:1501-1520`).

## Achados priorizados

### Crítico — tópico pode escapar de `wiki/`

`_slug` preserva `.` e `/` (`scripts/wk/cli.py:943-947`) e seu resultado entra diretamente em `os.path.join(wiki_root, ...)` (`scripts/wk/cli.py:998-1003`). Um tópico como `../../destino` pode resolver fora de `wiki/`. Isso viola o confinamento do store mesmo em uma ferramenta local.

Decisão recomendada: aceitar segmentos hierárquicos, mas rejeitar segmento vazio, `.` ou `..`; depois validar o caminho final com `commonpath` antes de escrever.

### Alto — `compile` não reconcilia o estado gerado

O contrato diz que `compile` regenera a wiki, que edição manual desaparece e que `wiki/index.md` lista todas as páginas (`operations/compile.md:11-18`, `operations/compile.md:37`). A implementação apenas sobrescreve páginas presentes; não remove páginas antigas. Com filtro por tópico, ainda substitui o índice global por um índice contendo somente aquele tópico (`scripts/wk/cli.py:991-1035`).

Consequências: páginas órfãs permanecem pesquisáveis, o índice Markdown pode ocultar outros tópicos e `raw/`, `wiki/` e índice SQLite deixam de representar a mesma visão lógica.

### Alto — `publish` não é idempotente e permite `source_id` duplicado

O ID publicado é determinístico (`scripts/wk/cli.py:1415`), mas uma repetição usa `_unique_dest` e cria outro arquivo (`scripts/wk/cli.py:1424`) com o mesmo ID. O banco impõe unicidade apenas a `path`, não a `source_id` (`scripts/sbindex/store.py:16-39`).

Consequências: uma retomada/reexecução acumula fontes duplicadas, as junções de L1/L5 ficam ambíguas e o compile pode sobrescrever a mesma página mais de uma vez.

### Alto — saúde operacional pode dar falso positivo

`doctor` considera Git válido apenas porque existe uma entrada `.git` (`scripts/wk/cli.py:471-475`). Nesta cópia, `.git` está vazio e `git status` recusa o repositório, mas `doctor` reporta `git: true`.

O mesmo comando lista `inbox/`, `raw/` e `wiki/` ausentes, mas não os inclui em `bloqueios` (`scripts/wk/cli.py:448-463`). `index status` também examina somente caminhos já registrados no banco (`scripts/sbindex/cli.py:348-376`), portanto não detecta arquivos novos não indexados nem um store estruturalmente incompleto.

### Médio — a documentação embarcada contradiz o CLI sobre `synth`

`operations/ingest-codebase.md:393-397` afirma que `run-stage synth` e `merge-agent-output synth` são inválidos. O parser atual aceita `synth` nos dois comandos (`scripts/codescan/cli.py:2038-2047`) e o papel existe em `RUN_STAGE_ROLES` (`scripts/codescan/cli.py:1315-1321`). Como o documento é empacotado no `wk.pyz`, o runbook distribui uma instrução obsoleta.

### Médio — não há migração/versionamento do schema SQLite

A tabela `meta` existe (`scripts/sbindex/store.py:93`), mas não é usada para versão de schema. `CREATE TABLE IF NOT EXISTS` não adiciona colunas a bancos antigos. Uma evolução do modelo pode exigir apagar/recriar manualmente um índice que a própria documentação apresenta como derivado e reconstruível.

### Baixo — descritores de arquivo não são sempre fechados explicitamente

O scanner usa `json.load(open(...))` (`scripts/codescan/surface.py:188`), e a suíte emitiu `ResourceWarning`. Há padrões semelhantes em leitores do índice e em testes. O impacto atual é baixo, mas pode aparecer ao varrer monorepos grandes no Windows.

## Dívida arquitetural

- Os dois adaptadores CLI concentram muita responsabilidade: `scripts/wk/cli.py` tem 1.654 linhas e `scripts/codescan/cli.py`, 2.139. Regras de domínio, I/O e apresentação JSON coexistem, elevando o custo de testar invariantes de ponta a ponta.
- Estados importantes são dicionários livres: frontmatter, `state.json`, manifests e payloads. Existem gates fortes, mas faltam tipos/versionamento nas fronteiras persistidas.
- O índice é corretamente tratado como derivado, porém o contrato de frescor não compara o filesystem completo com o banco.

## Perguntas em aberto para o grilling

1. `compile <topic>` deve preservar um índice global ou produzir um índice estritamente tópico? O contrato atual diz “todas as páginas”, mas o código implementa a segunda opção.
2. Reexecutar `publish` para o mesmo workdir deve ser idempotente, substituir a versão anterior ou criar uma nova revisão explícita?
3. “Quarentena” significa apenas registrar o erro em `quarantine.md`, ou retirar o candidato inválido de `inbox/`? Hoje ele permanece e pode gerar o mesmo registro em toda promoção.
4. O CLI deve tratar todos os argumentos como confiáveis por ser local, ou os guardrails de filesystem são uma fronteira de segurança real?
5. O produto será sempre single-user/local ou precisa suportar múltiplos processos/agentes operando o mesmo store?

