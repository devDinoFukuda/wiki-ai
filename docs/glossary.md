# Glossário de domínio

Status: reconstruído; termos marcados como “a confirmar” dependem das respostas do grilling.

| Termo | Definição operacional |
|---|---|
| Fonte | Documento com `origin` e `source_type`; sem esses campos não participa do corpus canônico. |
| Candidato | Fonte em `inbox/`, ainda fora da fonte-verdade. |
| Promoção | Decisão que move um candidato elegível para `raw/` e atribui confiança. |
| Aprovação | Ato humano explícito via flag; não equivale a auto-promoção. |
| Fonte promovida | Documento em `raw/` com `promoted: true`. |
| Fonte-verdade | Conjunto imutável de documentos em `raw/`. |
| Página da wiki | Projeção derivada, uma por fonte promovida, gravada em `wiki/`. |
| Fonte canônica | Uso ambíguo: normalmente uma fonte promovida; `agent-output` permanece `unverified` mesmo após aprovação. A confirmar se “canônico” inclui esse caso. |
| Quarentena | Hoje, registro append-only em `quarantine.md`; o arquivo inválido permanece em `inbox/`. A confirmar se deveria ser um estado/local físico. |
| Proveniência | Metadados que ligam conteúdo a origem, tipo, captura, confiança e supersessão. |
| Supersessão | Relação em que uma fonte substitui outra; páginas não podem continuar citando a substituída. |
| Índice | Projeção SQLite reconstruível de `raw/` e `wiki/`, com documentos, chunks, FTS5, vetores e colunas de proveniência. |
| Índice sujo | Estado em que disco e banco divergem. A detecção atual cobre somente caminhos já indexados. |
| Surface | Inventário determinístico inicial de uma codebase: linguagens, módulos, manifests, entry points e sinais Git. |
| Workdir | Diretório externo à codebase analisada que armazena estado e artefatos do codescan. |
| Estágio | Fase do pipeline SDD com status e, em alguns casos, itens pendentes/concluídos. |
| Agent pack | Pacote compacto de evidências atribuído a um batch/subagente. |
| Agent output | Saída de agente; no corpus nunca alcança confiança `reviewed`. |
| Merge | Portão que valida formato, ruído, identidade do agente e proveniência antes de integrar artefatos SDD. |
| Artefato SDD | Documento de inventário, domínio, arquitetura, spec, síntese ou rastreabilidade produzido pelo pipeline. |
| Fan-out | Particionamento obrigatório de um estágio em batches atribuídos a subagentes distintos. |

