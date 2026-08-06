# ADR 001 — Corpus curado e wiki derivada

Status: aceito, retroativo.

## Contexto

Conteúdo humano, código e saídas de agentes têm níveis de confiança diferentes. Permitir que uma saída de agente se torne fonte-verdade silenciosamente cria ciclos de realimentação e degrada a proveniência.

## Decisão

- Toda entrada passa por `inbox/`.
- `raw/` é a fonte-verdade imutável e só recebe conteúdo por promoção.
- `wiki/` é uma projeção reconstruível de `raw/`, uma página por fonte, sem síntese no compile.
- `agent-output` aprovado permanece `unverified`.
- A leitura consulta apenas `raw/` e `wiki/`.

## Evidência

- `schema.md:3-4`
- `schema.md:29`
- `schema.md:38-44`
- `operations/compile.md:11-23`
- `scripts/wk/cli.py:825-940`
- `scripts/wk/cli.py:983-1051`

## Consequências

Positivas: proveniência auditável, corpus reconstruível e barreira explícita contra contaminação por agente.

Negativas: consistência eventual entre promoção e compile; custo operacional de aprovação; necessidade de reconciliação correta das páginas obsoletas.

