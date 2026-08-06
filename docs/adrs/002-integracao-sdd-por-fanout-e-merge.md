# ADR 002 — Integração SDD por fan-out e merge controlado

Status: aceito, retroativo.

## Contexto

Artefatos SDD gerados diretamente pelo orquestrador ou copiados do chat não provam autoria, batch, entrada usada nem aderência ao contrato. Execuções paralelas também precisam preservar checkpoints.

## Decisão

- Estágios geradores usam `run-stage` para emitir manifesto e batches.
- Cada batch é atribuído a um identificador de subagente.
- O subagente grava saída parseável no caminho previsto.
- `merge-agent-output` valida e registra a integração.
- `done` depende dos artefatos/manifestos e dos gates do estágio.
- Estado é persistido fora da codebase analisada.

## Evidência

- `SKILL.md:77-82`
- `operations/ingest-codebase.md:31-61`
- `scripts/codescan/cli.py:1217-1311`
- `scripts/codescan/cli.py:1315-1549`
- `scripts/codescan/state.py:48-132`

## Consequências

Positivas: retomada, rastreabilidade, validação determinística e redução de conteúdo não atribuível.

Negativas: protocolo operacional longo, dependência da engine realmente disparar subagentes e maior acoplamento entre CLI, manifests e convenções de arquivos.

