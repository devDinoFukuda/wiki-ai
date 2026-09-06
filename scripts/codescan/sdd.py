"""Contrato operacional dos artefatos SDD do codescan."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass

from . import evidence as ev_mod
from . import state as st_mod


LEVELS = ("essencial", "completo", "detalhado")
PT_MARKERS = (
    "visão geral",
    "responsabilidades",
    "regras de negócio",
    "fluxo",
    "dependências",
    "rastreabilidade",
    "arquitetura",
    "lacunas",
)
EN_MARKERS = (
    "responsibility",
    "data structures",
    "dependencies",
    "overview",
    "requirements",
    "design",
)
MIN_DONE_SCORE = 90
CRITICAL_STAGES = ("modules", "rules", "architecture", "specs", "synth")
AGENT_OUTPUT_STAGES = CRITICAL_STAGES
BRIEF_STAGES = ("evidence",) + CRITICAL_STAGES
CONFIDENCE_MARKERS = ("\U0001F7E2", "\U0001F7E1", "\U0001F534")
FALLBACK_RE = re.compile(
    r"(unable to access|cannot access|permission restrictions|read failed|"
    r"nao consegui acessar|nao foi possivel ler|sem permissao|falha ao ler|"
    r"fallback operacional|generic output|shallow output)",
    re.I,
)
OPERATIONAL_MARKERS = (
    "fluxo",
    "erro",
    "dependencia",
    "integracao",
    "estado",
    "entrada",
    "saida",
    "criterio",
    "teste",
    "reimplementacao",
)
MATRIX_MARKERS = ("codigo", "regra", "requisito", "design", "tarefa")


@dataclass(frozen=True)
class ArtifactRule:
    """Regra única de qualidade por artefato SDD — fonte de verdade compartilhada
    por `done` (cli.py) e `audit` (sdd.py). Antes desta unificação existiam duas
    tabelas independentes (cli.py `_stage_artifact_quality` + este `RULES`) com
    limiares e seções divergentes para os mesmos 5 artefatos nomeados; ver
    inventário/decisões no relatório de merge dos gates.

    `sections` aceita, por posição, uma string (seção obrigatória única) OU uma
    tupla de aliases equivalentes — ex.: `("Decisões", "Decisões arquiteturais")`
    tolera sinônimo legítimo sem duplicar a regra. `min_bytes`, apesar do nome,
    sempre mediu contagem de caracteres (`len(text.strip())`), nunca bytes reais
    — mantido por compatibilidade com o campo já persistido/testado.
    """

    rel: str
    levels: tuple[str, ...] = LEVELS
    min_bytes: int = 600
    min_citations: int = 1
    sections: tuple[str | tuple[str, ...], ...] = ()
    glob: bool = False
    # Tipos de bloco ```mermaid``` aceitos como satisfazendo a exigência de
    # diagrama (ex.: ("flowchart", "graph")). Vazio = artefato de texto puro,
    # sem exigência de diagrama (default). Ver FIX2 (BQ-lote-B): artefatos
    # "de diagrama" (c4-*, architecture, erd-complete) eram aprovados sem
    # nenhum bloco mermaid porque `sections` só cobria prosa/tabelas.
    require_diagram: tuple[str, ...] = ()


RULES: dict[str, tuple[ArtifactRule, ...]] = {
    "modules": (
        ArtifactRule(
            "sdd/code-analysis.md",
            min_bytes=1800,
            min_citations=5,
            sections=("Visão geral", "Módulos", "Fluxos", "Riscos", "Rastreabilidade"),
        ),
        ArtifactRule(
            "sdd/data-dictionary.md",
            levels=("completo", "detalhado"),
            min_bytes=800,
            min_citations=2,
            sections=("Entidades", "Campos", "Origem"),
        ),
        ArtifactRule(
            "sdd/flowcharts/*.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            glob=True,
        ),
        ArtifactRule(
            "modules/*.md",
            min_bytes=600,
            min_citations=2,
            glob=True,
        ),
    ),
    "rules": (
        ArtifactRule(
            "sdd/domain.md",
            min_bytes=1400,
            min_citations=4,
            sections=("Glossário", "Regras de negócio", "Lacunas"),
        ),
        ArtifactRule(
            "sdd/state-machines.md",
            levels=("completo", "detalhado"),
            min_bytes=400,
            min_citations=1,
            sections=("Máquinas de estado",),
        ),
        ArtifactRule(
            "sdd/permissions.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Matriz", "Lacunas"),
        ),
        ArtifactRule(
            "sdd/adrs/*.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=1,
            glob=True,
        ),
    ),
    "architecture": (
        ArtifactRule(
            "sdd/architecture.md",
            min_bytes=1800,
            min_citations=5,
            sections=("Visão geral", "Containers", "Integrações", "Riscos", "Rastreabilidade"),
            require_diagram=("flowchart", "graph"),
        ),
        ArtifactRule(
            "sdd/c4-context.md",
            min_bytes=450,
            min_citations=0,
            sections=("Contexto",),
            require_diagram=("flowchart", "graph", "C4Context", "C4Container", "C4Component"),
        ),
        ArtifactRule(
            "sdd/c4-containers.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Containers",),
            require_diagram=("flowchart", "graph", "C4Context", "C4Container", "C4Component"),
        ),
        ArtifactRule(
            "sdd/c4-components.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Componentes",),
            require_diagram=("flowchart", "graph", "C4Context", "C4Container", "C4Component"),
        ),
        ArtifactRule(
            "sdd/coupling.md",
            min_bytes=500,
            min_citations=0,
            sections=("Métricas por módulo", "Plano Abstração"),
        ),
        ArtifactRule(
            "sdd/erd-complete.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Entidades",),
            require_diagram=("erDiagram",),
        ),
        ArtifactRule(
            "sdd/traceability/spec-impact-matrix.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Matriz",),
        ),
        ArtifactRule(
            "sdd/sequences/*.md",
            levels=("detalhado",),
            min_bytes=300,
            min_citations=0,
            glob=True,
        ),
    ),
    "specs": (
        ArtifactRule("sdd/specs/*/requirements.md", min_bytes=800, min_citations=2, glob=True),
        ArtifactRule("sdd/specs/*/design.md", min_bytes=800, min_citations=2, glob=True),
        ArtifactRule("sdd/specs/*/tasks.md", min_bytes=500, min_citations=1, glob=True),
        ArtifactRule(
            "sdd/confidence-report.md",
            min_bytes=700,
            min_citations=0,
            sections=("Contagem", "Rebaixados", "Lacunas"),
        ),
        ArtifactRule(
            "sdd/gaps.md",
            levels=("completo", "detalhado"),
            min_bytes=900,
            min_citations=0,
            sections=("Lacunas críticas", "Lacunas moderadas", "Perguntas abertas", "Impacto"),
        ),
        ArtifactRule(
            "sdd/traceability/code-spec-matrix.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Matriz",),
        ),
        ArtifactRule(
            "sdd/user-stories/*.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            glob=True,
        ),
        ArtifactRule(
            "sdd/specs/*/contracts.md",
            levels=("detalhado",),
            min_bytes=300,
            min_citations=1,
            glob=True,
        ),
        ArtifactRule(
            "sdd/specs/*/edge-cases.md",
            levels=("detalhado",),
            min_bytes=300,
            min_citations=1,
            glob=True,
        ),
    ),
    "synth": (
        ArtifactRule("sdd/confirmed.md", min_bytes=500, min_citations=3),
        ArtifactRule("sdd/inferred.md", min_bytes=300, min_citations=0),
    ),
}


# W8-T8.3 (plano-evolucao-wiki-ai.md §3.1 "Remoções obrigatórias"): a
# EXIGÊNCIA DE EXISTÊNCIA destes artefatos é cerimônia do legado, não
# checagem de integridade. Ausência de qualquer um deles deixa de ser
# `blocker`/zerar score de estágio e vira `avisos_cerimoniais` (informativo).
# Quando o artefato EXISTE, as checagens de integridade normais (citações,
# proveniência, placeholder, etc.) continuam valendo e bloqueando — só a
# obrigatoriedade de ele existir foi removida. Mapeamento (rel -> bullet de
# §3.1):
#   sdd/adrs/*.md          -> "ADR retroativo obrigatório apenas para
#                              satisfazer nível de documentação"
#   sdd/user-stories/*.md  -> "Histórias de usuário geradas automaticamente
#                              como comprovação de análise de código"
#   sdd/state-machines.md  -> "Máquina de estados ... vazio/genérico quando o
#                              sistema não contém informação que os justifique"
#   sdd/erd-complete.md    -> "... ERD ... vazio/genérico quando o sistema
#                              não contém informação que os justifique"
#   sdd/sequences/*.md     -> "... diagrama de sequência vazio/genérico
#                              quando o sistema não contém informação que os
#                              justifique"
#   modules/*.md           -> "Arquivo intermediário exigido exclusivamente
#                              porque um estágio anterior o produzia" (saída
#                              por-módulo, intermediária, consolidada em
#                              sdd/code-analysis.md pelo próprio estágio)
CEREMONIAL_RULES: frozenset[str] = frozenset({
    "sdd/adrs/*.md",
    "sdd/user-stories/*.md",
    "sdd/state-machines.md",
    "sdd/erd-complete.md",
    "sdd/sequences/*.md",
    "modules/*.md",
})


SCAFFOLDS: dict[str, str] = {
    "sdd/code-analysis.md": "# Análise de código\n\n## Visão geral\n\n## Módulos\n\n## Fluxos\n\n## Riscos\n",
    "sdd/data-dictionary.md": "# Dicionário de dados\n\n## Entidades\n\n## Campos\n\n## Origem\n",
    "sdd/domain.md": "# Domínio\n\n## Glossário\n\n## Regras de negócio\n\n## Lacunas\n",
    "sdd/state-machines.md": "# Máquinas de estado\n\n## Máquinas de estado\n",
    "sdd/permissions.md": "# Permissões\n\n## Matriz\n\n## Lacunas\n",
    "sdd/architecture.md": "# Arquitetura\n\n## Visão geral\n\n## Containers\n\n## Integrações\n\n## Riscos\n",
    "sdd/c4-context.md": "# C4 Contexto\n\n## Contexto\n\n```mermaid\nflowchart LR\n```\n",
    "sdd/c4-containers.md": "# C4 Containers\n\n## Containers\n\n```mermaid\nflowchart LR\n```\n",
    "sdd/c4-components.md": "# C4 Componentes\n\n## Componentes\n\n```mermaid\nflowchart LR\n```\n",
    "sdd/erd-complete.md": "# ERD\n\n## Entidades\n\n```mermaid\nerDiagram\n```\n",
    "sdd/traceability/spec-impact-matrix.md": "# Matriz de impacto\n\n## Matriz\n",
    "sdd/confidence-report.md": "# Relatório de confiança\n\n## Contagem\n\n## Rebaixados\n\n## Lacunas\n",
    "sdd/gaps.md": "# Lacunas\n\n## Lacunas críticas\n\n## Lacunas moderadas\n\n## Perguntas abertas\n\n## Impacto\n",
    "sdd/traceability/code-spec-matrix.md": "# Matriz código-spec\n\n## Matriz\n",
}


COMPACT_OUTPUT_CONTRACTS: dict[str, dict] = {
    "evidence": {
        "blocks": ("EVIDENCE", "FAILED EVIDENCE"),
        "max_lines_per_block": 50,
        "required_sections": ("Escopo", "Arquivos", "Citacoes", "Lacunas"),
    },
    "modules": {
        "blocks": ("MODULE", "FAILED MODULE"),
        "max_lines_per_block": 56,
        "required_sections": (
            "Responsabilidade",
            "Estruturas de dados",
            "Fluxos",
            "Dependencias",
            "Rastreabilidade",
            "Lacunas",
        ),
    },
    "rules": {
        "blocks": ("RULES", "FAILED RULES"),
        "max_lines_per_block": 80,
        "required_sections": (
            "Regras",
            "Estados",
            "Permissoes",
            "Contradicoes",
            "Lacunas",
        ),
    },
    "architecture": {
        "blocks": ("ARCHITECTURE", "FAILED ARCHITECTURE"),
        "max_lines_per_block": 90,
        "required_sections": (
            "Containers",
            "Integracoes",
            "Decisoes",
            "Riscos",
            "Rastreabilidade",
        ),
    },
    "specs": {
        "blocks": ("SPEC", "FAILED SPEC"),
        "max_lines_per_file": 70,
        "required_files": ("requirements.md", "design.md", "tasks.md"),
        "required_sections": (
            "Requisitos",
            "Criterios",
            "Design",
            "Tarefas",
            "Testes",
            "Rastreabilidade",
            "Lacunas",
        ),
    },
    "synth": {
        "max_lines_per_block": 80,
        "required_sections": ("Confirmados", "Inferidos", "Perguntas"),
    },
}


COMPACT_AGENT_RULES = (
    "saida_apenas_blocos_parseaveis",
    "pt_br_tecnico_sem_resumo",
    "nao_ecoar_codigo",
    "nao_repetir_contexto",
    "tabelas_densas_quando_comparar",
    "bullets_rastreaveis_com_marcador_confiana",
    "verde_exige_caminho_relativo_completo_da_raiz (ex. válido: `src/quote/DomainEvent.java:5`; "
    "ex. inválido: `DomainEvent.java:5` — basename sozinho não confere, use o mesmo formato de "
    "`evidence[].path` do pack)",
    "amarelo_exige_justificativa",
    "vermelho_exige_pergunta_objetiva",
    "entidades_tipos_entre_crases_em_estruturas_de_dados (ex.: `Quote`) — não é eco de código; "
    "a proibição de eco cobre trecho/linha de código, não identificador entre crases; "
    "alimenta sdd/data-dictionary.md",
)


def compact_contract(stage: str) -> dict:
    contract = COMPACT_OUTPUT_CONTRACTS.get(stage, {})
    return {
        "rules": list(COMPACT_AGENT_RULES),
        "format": {
            "start": "=== <BLOCK>: <id> ===",
            "end": "=== END ===",
            "no_preamble": True,
            "no_summary": True,
            "no_diff": True,
            "no_code_echo": True,
        },
        "limits": {
            "max_quote_lines": 0,
            "max_context_repetition": 0,
            "max_sections_per_artifact": 8,
            "max_bullets_per_section": 8,
        },
        "stage": contract,
    }


def doc_level(st: dict) -> str:
    level = (st.get("sdd") or {}).get("doc_level") or "essencial"
    return level if level in LEVELS else "essencial"


def stage_rules(stage: str, level: str) -> list[ArtifactRule]:
    return [r for r in RULES.get(stage, ()) if level in r.levels]


def rule_for_rel(rel: str) -> ArtifactRule | None:
    """Lookup por caminho relativo exato (não-glob) na tabela única `RULES`.

    Usado por `cli.py::_stage_artifact_quality` para consumir os mesmos
    limiares/seções que `audit` já aplica via `_audit_stage`, eliminando a
    tabela duplicada que existia só em cli.py."""
    for rules in RULES.values():
        for rule in rules:
            if not rule.glob and rule.rel == rel:
                return rule
    return None


def rel_path(wd: str, rel: str) -> str:
    return os.path.join(wd, *rel.split("/"))


def _glob(root: str, pattern: str) -> list[str]:
    base = rel_path(root, pattern.split("*", 1)[0].rstrip("/"))
    suffix = pattern.rsplit("*", 1)[-1]
    out: list[str] = []
    if not os.path.isdir(base):
        return out
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            if rel.startswith(pattern.split("*", 1)[0]) and rel.endswith(suffix):
                out.append(p)
    return sorted(out)


def paths_for_rule(wd: str, rule: ArtifactRule) -> list[str]:
    if rule.glob:
        return _glob(wd, rule.rel)
    return [rel_path(wd, rule.rel)]


def required_paths(wd: str, stage: str, st: dict) -> list[str]:
    out: list[str] = []
    for rule in stage_rules(stage, doc_level(st)):
        paths = paths_for_rule(wd, rule)
        out.extend(paths or [rel_path(wd, rule.rel.replace("*", "_index"))])
    return out


def scaffold(wd: str, stage: str, st: dict) -> dict:
    created: list[str] = []
    skipped: list[str] = []
    level = doc_level(st)
    for rule in stage_rules(stage, level):
        if rule.glob:
            if rule.rel == "sdd/flowcharts/*.md":
                targets = ["sdd/flowcharts/_index.md"]
            elif rule.rel == "sdd/adrs/*.md":
                targets = ["sdd/adrs/000-pendente.md"]
            elif rule.rel == "sdd/sequences/*.md":
                targets = ["sdd/sequences/_index.md"]
            elif rule.rel == "sdd/user-stories/*.md":
                targets = ["sdd/user-stories/_index.md"]
            else:
                targets = []
                units = sorted(set((st.get("stages", {}).get("specs", {}).get("pending") or []) +
                                   (st.get("stages", {}).get("specs", {}).get("done") or [])))
                for unit in units:
                    targets.append(rule.rel.replace("*", unit))
                if not targets and stage == "specs":
                    targets.append(rule.rel.replace("*", "_unit"))
        else:
            targets = [rule.rel]
        for rel in targets:
            path = rel_path(wd, rel)
            if os.path.exists(path):
                skipped.append(path)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            text = SCAFFOLDS.get(rel, f"# {os.path.basename(path)}\n\nPendente de análise.\n")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            created.append(path)
    return {"stage": stage, "doc_level": level, "created": created, "skipped": skipped}


def brief(wd: str, stage: str, st: dict) -> dict:
    level = doc_level(st)
    rules = stage_rules(stage, level)
    artifacts = [r.rel for r in rules]
    base = [
        "PT-BR técnico, denso e sem prosa decorativa.",
        "Use tabelas/listas curtas para reduzir tokens sem perder informação.",
        "Toda afirmação confirmada exige caminho relativo COMPLETO da raiz do repo, com '/', "
        "+ linha (mesmo formato de `evidence[].path` do pack) — ex. válido: "
        "`src/quote/DomainEvent.java:5`; ex. inválido: `DomainEvent.java:5` (basename não confere).",
        "Não substitua a árvore SDD por confirmed.md/inferred.md.",
        "Subagentes devem receber agent-pack; nunca copie o repo para store/.codescan.",
        "Se não cumprir score >=90, declare blocked/failed/degraded.",
    ]
    stage_notes = {
        "modules": [
            "Gerar agent-pack por batch antes de acionar subagentes.",
            "Consolidar modules/*.md em code-analysis.md.",
            "Extrair entidades para data-dictionary.md quando aplicável.",
            "Criar flowcharts Mermaid para fluxos principais quando doc_level >= completo.",
        ],
        "evidence": [
            "Gerar evidence-pack/agent-pack determinístico.",
            "Não produzir artefato final de wiki neste passo.",
            "Não copiar repositório para store/.codescan.",
        ],
        "rules": [
            "Ler modules/*.md e extrair regras de negócio, estados, permissões e ADRs.",
            "Bloco RULES aceita adrs/NNN-<slug> para ADRs retroativos (doc_level >= completo).",
            "Não reler repo salvo para citações pontuais via read.",
        ],
        "architecture": [
            "Ler modules/*.md e artefatos de rules.",
            "Produzir arquitetura, C4, ERD e rastreabilidade conforme doc_level.",
            "Bloco ARCHITECTURE aceita traceability/spec-impact-matrix e sequences/<slug> (detalhado).",
        ],
        "specs": [
            "Produzir requirements.md, design.md e tasks.md por unit.",
            "Em detalhado, incluir contracts.md e edge-cases.md no bloco SPEC da unit.",
            "Revisar confiança e gerar confidence-report/gaps/matrizes.",
            "Bloco SPEC também aceita confidence-report, gaps, traceability/code-spec-matrix, user-stories/<slug> e openapi/<slug>.",
        ],
        "synth": [
            "Preservar árvore SDD.",
            "Gerar confirmed/inferred como síntese para ingestão, não como wiki única.",
            "Artefatos canônicos: sdd/confirmed.md e sdd/inferred.md; não duplicar cópia na raiz do workdir.",
        ],
    }
    return {
        "stage": stage,
        "doc_level": level,
        "artifacts": artifacts,
        "instructions": base + stage_notes.get(stage, []),
        "compact_contract": compact_contract(stage),
    }


def audit(wd: str, stage: str | None, st: dict) -> dict:
    stages = [stage] if stage else ["modules", "rules", "architecture", "specs", "synth"]
    return audit_stages(wd, stages, st)


def audit_stages(wd: str, stages: list[str], st: dict) -> dict:
    """Agrega o resultado de `_audit_stage` por estágio.

    `min(scores, default=100)` sobre uma lista vazia (ou sobre estágios que
    não avaliaram nenhum artefato) transformaria "nada foi checado" em nota
    máxima — exatamente o defeito que fez `audit --strict` reportar
    `{"status":"pass","score":100}` num workdir onde só `surface` estava
    `done` (`_strict_audit_scope` devolve `stages=[]` nesse caso). Aqui isso é
    proibido por construção: sem evidência real (nenhum estágio com pelo
    menos 1 artefato verificado), o resultado é `sem_evidencia`/score `None`,
    nunca `pass`/100.
    """
    results = [_audit_stage(wd, s, st) for s in stages]
    artifacts_checked = sum(r.get("artifacts_checked", 0) for r in results)
    stages_evaluated = sum(1 for r in results if r.get("artifacts_checked", 0) > 0)
    vacuous_stages = [r["stage"] for r in results if r["status"] == "sem_evidencia"]

    real_scores = [r["score"] for r in results if r.get("score") is not None]
    score = min(real_scores) if real_scores else None
    if stages_evaluated == 0:
        status = "sem_evidencia"
    elif vacuous_stages:
        status = "sem_evidencia"
    else:
        status = "pass" if (
            score is not None and score >= MIN_DONE_SCORE and all(r["status"] == "pass" for r in results)
        ) else ("failed" if any(r["status"] == "failed" for r in results) else "degraded")

    # W8-T8.3 (§3.1): transparência da reclassificação — cerimônia removida
    # (ADR retroativo, histórias autogeradas, diagrama vazio, arquivo
    # intermediário, score por idioma/seções/tamanho) some da agregação
    # bloqueante, mas não fica invisível: consolidada aqui para quem lê o
    # relatório de auditoria completo, sem afetar `status`/`score`.
    avisos_cerimoniais = [a for r in results for a in r.get("avisos_cerimoniais", [])]
    report = {
        "status": status,
        "score": score,
        "threshold": MIN_DONE_SCORE,
        "criterio": "integridade",
        "stages": results,
        "stages_evaluated": stages_evaluated,
        "artifacts_checked": artifacts_checked,
        "avisos_cerimoniais": avisos_cerimoniais,
    }
    if status == "sem_evidencia":
        report["message"] = _no_evidence_message(
            st, results, stages_evaluated=stages_evaluated, artifacts_checked=artifacts_checked
        )
    return report


def _no_evidence_message(st: dict, results: list[dict], *, stages_evaluated: int, artifacts_checked: int) -> str:
    """Mensagem acionável para o caso `sem_evidencia`: quantos estágios/artefatos
    foram de fato verificados, quais estágios ainda faltam e qual comando roda
    o próximo passo — para que "nada foi auditado" nunca fique ambíguo com
    "tudo passou"."""
    total = len(results) if results else len(CRITICAL_STAGES)
    vacuous = [r["stage"] for r in results if r["status"] == "sem_evidencia"]
    pending = [
        s for s in CRITICAL_STAGES
        if (st.get("stages", {}) or {}).get(s, {}).get("status") != "done"
    ]
    nxt = pending[0] if pending else (vacuous[0] if vacuous else CRITICAL_STAGES[0])
    parts = [f"{stages_evaluated}/{total} estágios avaliados, {artifacts_checked} artefatos verificados."]
    if not results:
        parts.append("nenhum estágio elegível para auditoria no escopo atual.")
    elif vacuous:
        parts.append(f"sem evidência em: {', '.join(vacuous)}.")
    if pending:
        parts.append(f"estágios pendentes: {', '.join(pending)}.")
    parts.append(f"rode `sdd-brief {nxt}` e conclua o estágio antes de auditar novamente.")
    return " ".join(parts)


def _repo_root(st: dict) -> str | None:
    """Raiz do repositório auditado, conforme registrada por `state.init`.

    F-04: é a única fonte do caminho do repo no fluxo de auditoria (`audit`
    recebe `st`, não o repo). Devolve `None` quando o campo não existe ou já
    não aponta para um diretório — caso legítimo de workdir isolado/movido, em
    que `_audit_file` degrada para contagem por regex + warning em vez de
    reprovar citações que não tem como conferir.
    """
    repo = st.get("repo") if isinstance(st, dict) else None
    if isinstance(repo, str) and repo.strip() and os.path.isdir(repo):
        return repo
    return None


def _audit_stage(wd: str, stage: str, st: dict) -> dict:
    level = doc_level(st)
    repo = _repo_root(st)
    artifacts = []
    blockers: list[str] = []
    warnings: list[str] = []
    avisos_cerimoniais: list[str] = []
    source_tree_error = _source_tree_error(wd)
    if source_tree_error:
        blockers.append(source_tree_error)
    blockers.extend(_strict_workdir_blockers(wd, st))
    if stage in AGENT_OUTPUT_STAGES:
        blockers.extend(_agent_run_blockers(wd, stage, st))
    for rule in stage_rules(stage, level):
        paths = paths_for_rule(wd, rule)
        if not paths:
            if rule.rel in CEREMONIAL_RULES:
                # W8-T8.3 (§3.1): a existência deste artefato é cerimônia —
                # ver CEREMONIAL_RULES. Ausência vira aviso informativo,
                # nunca `blocker` nem entra no cálculo de `artifact_score`.
                avisos_cerimoniais.append(
                    f"ausente (informativo, não bloqueia — §3.1): {rule.rel}"
                )
                artifacts.append(
                    {"rule": rule.rel, "status": "ausente_cerimonial", "criterio": "cerimonia"}
                )
                continue
            blockers.append(f"ausente: {rule.rel}")
            artifacts.append({"rule": rule.rel, "status": "missing"})
            continue
        for path in paths:
            item = _audit_file(path, rule, wd=wd, repo=repo)
            # W8-T8.3 (§3.1): regra NÃO-glob (ex.: sdd/state-machines.md,
            # sdd/erd-complete.md) sempre devolve um candidato de
            # `paths_for_rule` mesmo quando o arquivo não existe no disco —
            # o `if not paths` acima só cobre regra glob (adrs/user-stories/
            # sequences). Arquivo ausente cai aqui, em `_audit_file`, que
            # devolve `status: "missing"`; para as regras em CEREMONIAL_RULES
            # essa ausência é a MESMA cerimônia (exigência de existência), só
            # que chega por outro caminho — tratamento idêntico ao `if not
            # paths` acima: aviso informativo, nunca `blocker`.
            if item.get("status") == "missing" and rule.rel in CEREMONIAL_RULES:
                avisos_cerimoniais.append(
                    f"ausente (informativo, não bloqueia — §3.1): {rule.rel}"
                )
                artifacts.append(
                    {"rule": rule.rel, "status": "ausente_cerimonial", "criterio": "cerimonia"}
                )
                continue
            artifacts.append(item)
            blockers.extend(item["blockers"])
            warnings.extend(item["warnings"])
            avisos_cerimoniais.extend(item.get("avisos_cerimoniais", []))
            if item["score"] < MIN_DONE_SCORE:
                blockers.append(f"{os.path.relpath(path, wd)} score {item['score']}/{MIN_DONE_SCORE}")

    if stage == "modules":
        s = st.get("stages", {}).get("modules", {})
        if s.get("pending") or s.get("failed") or s.get("blocked"):
            blockers.append("módulos não resolvidos")
        if not s.get("done"):
            blockers.append("nenhum módulo concluído")
    if stage == "specs":
        s = st.get("stages", {}).get("specs", {})
        if s.get("pending") or s.get("failed") or s.get("blocked"):
            blockers.append("specs não resolvidas")
        if not s.get("done"):
            blockers.append("nenhuma spec concluída")
        matrix_error = _matrix_error(wd)
        if matrix_error:
            blockers.append(matrix_error)
    if stage == "synth":
        blockers.extend(_synth_root_divergence_blockers(wd))

    # Dedup preservando a ordem de primeira ocorrência: a mesma fonte (ex.: um
    # artefato adulterado citado em mais de um `run` do manifesto) não pode
    # contar duas vezes e inflar o score negativamente por repetição.
    blockers = list(dict.fromkeys(blockers))

    artifacts_checked = len(artifacts)
    if artifacts_checked == 0:
        # Nenhuma regra elegível produziu artefato para checar neste estágio:
        # não existe score real para calcular. `min(scores, default=100)`
        # transformaria "nada verificado" em nota máxima — o mesmo defeito
        # corrigido em `audit_stages`, aqui replicado por estágio individual
        # para que um único estágio vazio não infle a média/mínimo agregado.
        # Sem artefato, não há aprovação: status dedicado e score nulo.
        blockers.append(
            f"nenhum artefato elegível para auditoria em {stage} (doc_level={level}); "
            "nada foi verificado neste estágio"
        )
        blockers = list(dict.fromkeys(blockers))
        return {
            "stage": stage,
            "status": "sem_evidencia",
            "score": None,
            "threshold": MIN_DONE_SCORE,
            "criterio": "integridade",
            "blockers": blockers,
            "warnings": warnings,
            "avisos_cerimoniais": avisos_cerimoniais,
            "artifacts": artifacts,
            "artifacts_checked": 0,
        }

    # W8-T8.3 (§3.1): artefatos "ausente_cerimonial" (ver CEREMONIAL_RULES)
    # não têm `score` real — são ausência tolerada, não avaliação. Antes desta
    # mudança, TODO artefato ausente (inclusive os cerimoniais) caía no
    # `a.get("score", 0)` default e zerava `artifact_score` do estágio
    # inteiro; artefatos legitimamente obrigatórios continuam zerando por
    # este mesmo mecanismo (não estão em CEREMONIAL_RULES, então permanecem
    # com status "missing" e são incluídos no cálculo).
    scoreable_artifacts = [a for a in artifacts if a.get("status") != "ausente_cerimonial"]
    artifact_score = min((a.get("score", 0) for a in scoreable_artifacts), default=100)
    score = min(artifact_score, max(0, 100 - 20 * len(blockers) - 5 * len(warnings)))
    if blockers:
        score = min(score, 60)
    if _has_p0_blocker(blockers):
        score = 0
    elif warnings:
        score = min(score, 99)
    status = "pass" if score >= MIN_DONE_SCORE and not blockers else ("degraded" if score >= 70 else "failed")
    return {
        "stage": stage,
        "status": status,
        "score": score,
        "threshold": MIN_DONE_SCORE,
        "criterio": "integridade",
        "blockers": blockers,
        "warnings": warnings,
        "avisos_cerimoniais": avisos_cerimoniais,
        "artifacts": artifacts,
        "artifacts_checked": artifacts_checked,
    }


def _source_tree_error(wd: str) -> str | None:
    src = os.path.join(wd, "src")
    if os.path.isdir(src):
        return "codebase copiado para workdir: remova src/ e use agent-pack/read"
    return None


AGENT_RUNS_SCHEMA = "wiki-ai.agent-runs.v2"
# Emitida por `redo` quando o manifesto passa a carregar status/supersedes
# explícitos por run (BQ4). v2 continua sendo o schema emitido normalmente
# por `merge-agent-output` (agentmerge.py) e segue válido/current mesmo sem
# esses campos — ver AGENT_RUNS_ACCEPTED_SCHEMAS e AGENT_RUN_STATUSES.
AGENT_RUNS_VERSIONED_SCHEMA = "wiki-ai.agent-runs.v3"
AGENT_RUNS_LEGACY_SCHEMA = "wiki-ai.agent-runs.v1"
AGENT_RUNS_ACCEPTED_SCHEMAS = (AGENT_RUNS_SCHEMA, AGENT_RUNS_VERSIONED_SCHEMA)
AGENT_RUN_STATUSES = ("current", "superseded")


def _run_status(run: dict) -> str:
    """Runs sem `status` (schema v2 legado/migrável ou runs nunca tocados por
    `redo`) são tratados como `current` — mesmo comportamento de sempre."""
    status = run.get("status")
    return status if status in AGENT_RUN_STATUSES else "current"


def _run_item_names(run: dict) -> set[str]:
    out: set[str] = set()
    for item in run.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("item") or "").strip().replace("\\", "/").strip("/")
        if name:
            out.add(name)
    return out


def _resolve_manifest_path(wd: str, value) -> str | None:
    """Resolve um caminho gravado no manifesto (absoluto ou relativo ao workdir)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if os.path.isabs(value):
        return value
    return os.path.join(wd, *value.replace("\\", "/").split("/"))


def _rel_to_wd(wd: str, path: str) -> str:
    try:
        return os.path.relpath(path, wd).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


def _agent_run_input_blockers(wd: str, stage: str, idx: int, run: dict) -> list[str]:
    raw = run.get("input")
    full = _resolve_manifest_path(wd, raw)
    label = raw if isinstance(raw, str) and raw.strip() else "(vazio)"
    if not full or not os.path.isfile(full):
        return [f"P0: agent-runs input ausente em {stage}#{idx}: {label}"]
    rel = _rel_to_wd(wd, full)
    expected = run.get("input_sha256")
    if not isinstance(expected, str) or not expected.strip():
        return [f"P0: agent-runs sem input_sha256 registrado em {stage}#{idx}: {rel}"]
    actual = st_mod.sha256_file(full)
    if actual != expected:
        return [
            f"P0: agent-runs input alterado apos registro em {stage}#{idx}: {rel} "
            f"(sha256 esperado {expected}, obtido {actual})"
        ]
    return []


def _agent_run_artifact_blockers(item_name: str, wd: str, artifact) -> list[str]:
    if not isinstance(artifact, dict):
        return [f"P0: agent-runs artefato inválido no item {item_name or '?'}"]
    raw = artifact.get("path")
    full = _resolve_manifest_path(wd, raw)
    label = _rel_to_wd(wd, full) if full else str(raw)
    if not full or not os.path.isfile(full):
        return [f"P0: artefato ausente após o merge: {label}"]
    expected = artifact.get("sha256")
    if not isinstance(expected, str) or not expected.strip():
        return [f"P0: artefato sem sha256 registrado no manifesto: {label}"]
    actual = st_mod.sha256_file(full)
    if actual != expected:
        return [
            f"P0: artefato alterado após o merge: {label} "
            f"(sha256 esperado {expected}, obtido {actual})"
        ]
    return []


def _agent_run_blockers(wd: str, stage: str, st: dict) -> list[str]:
    path = os.path.join(wd, "agent-runs", f"{stage}.json")
    if not os.path.isfile(path):
        return [f"P0: agent-runs obrigatório ausente para stage {stage}: agent-runs/{stage}.json"]
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        return [f"P0: agent-runs inválido para stage {stage}: {exc.__class__.__name__}"]

    schema = data.get("schema")
    if schema == AGENT_RUNS_LEGACY_SCHEMA:
        return [
            f"P0: agent-runs schema legado ({AGENT_RUNS_LEGACY_SCHEMA}) sem prova criptográfica para stage "
            f"{stage}: refaça o stage {stage} com o merge atualizado (schema {AGENT_RUNS_SCHEMA})"
        ]

    blockers: list[str] = []
    if schema not in AGENT_RUNS_ACCEPTED_SCHEMAS:
        blockers.append(
            f"P0: agent-runs schema inválido para stage {stage}: esperado "
            f"{AGENT_RUNS_SCHEMA} ou {AGENT_RUNS_VERSIONED_SCHEMA}, obtido {schema!r}"
        )
    if data.get("stage") != stage:
        blockers.append(f"P0: agent-runs stage incompatível: esperado {stage}")
    runs = data.get("runs")
    if not isinstance(runs, list) or not runs:
        blockers.append(f"P0: agent-runs sem registros para stage {stage}")
        return blockers

    covered: set[str] = set()
    has_payload = False
    for idx, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            blockers.append(f"P0: agent-runs registro inválido em {stage}#{idx}")
            continue
        if run.get("stage") != stage:
            blockers.append(f"P0: agent-runs registro com stage incompatível em {stage}#{idx}")

        # BQ4 (versionamento de runs): um run marcado `superseded` (via `redo`)
        # fica preservado na trilha para sempre, mas NUNCA é reconferido contra
        # o disco atual — só o run `current` de cada item é prova de
        # integridade. Sem isto, reescrever um artefato após `redo` reabria o
        # mesmo P0 "artefato alterado após o merge" que motivou o redo.
        if _run_status(run) != "current":
            continue

        blockers.extend(_agent_run_input_blockers(wd, stage, idx, run))

        items = run.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    blockers.append(f"P0: agent-runs item inválido em {stage}#{idx}")
                    continue
                name = str(item.get("item") or "").strip().replace("\\", "/").strip("/")
                artifacts = item.get("artifacts")
                if name:
                    covered.add(name)
                if isinstance(artifacts, list):
                    for artifact in artifacts:
                        blockers.extend(_agent_run_artifact_blockers(name, wd, artifact))
                        if name and isinstance(artifact, dict) and artifact.get("path"):
                            has_payload = True
        if int(run.get("items_count") or 0) > 0 or int(run.get("artifacts_count") or 0) > 0:
            has_payload = True

    if not has_payload:
        blockers.append(f"P0: agent-runs sem payload rastreável para stage {stage}")

    if stage in ("modules", "specs"):
        done = {
            str(item).strip().replace("\\", "/").strip("/")
            for item in ((st.get("stages") or {}).get(stage) or {}).get("done", [])
        }
        missing = sorted(item for item in done if item and item not in covered)
        if missing:
            blockers.append(f"P0: agent-runs não cobre itens done em {stage}: {', '.join(missing[:10])}")
    return blockers


def _agent_runs_path(wd: str, stage: str) -> str:
    return os.path.join(wd, "agent-runs", f"{stage}.json")


def _load_agent_runs(wd: str, stage: str) -> dict | None:
    path = _agent_runs_path(wd, stage)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def current_run_items(wd: str, stage: str) -> dict[str, int]:
    """Mapa item -> id (1-based) do run `current` mais recente que o cobre.

    Usado por `cli.py::cmd_merge_agent_output` para recusar re-merge de um
    item que já tem um run vigente (BQ4 ponto 5): sem isto, reescrever a
    resposta do subagente e rodar `merge-agent-output` de novo silenciosamente
    invalidava a trilha anterior em vez de apontar para `redo`."""
    data = _load_agent_runs(wd, stage)
    if not data:
        return {}
    runs = data.get("runs")
    if not isinstance(runs, list):
        return {}
    mapping: dict[str, int] = {}
    for idx, run in enumerate(runs, start=1):
        if not isinstance(run, dict) or _run_status(run) != "current":
            continue
        for name in _run_item_names(run):
            mapping[name] = idx
    return mapping


def _write_json_atomic(path: str, data) -> None:
    """Grava JSON via tmp + os.replace no MESMO diretório do destino.

    BUG F-22 (perda de manifesto): `open(path, "w")` trunca o arquivo ANTES
    de escrever — uma sessão morta (Ctrl-C, crash, disco cheio) no meio do
    `json.dump` deixava `agent-runs/<stage>.json` truncado/vazio. O manifesto
    é a única prova criptográfica do stage: sem ele, `_agent_run_blockers`
    emite `P0: agent-runs obrigatório ausente/inválido` e o stage inteiro
    precisa ser refeito. `os.replace` é atômico dentro do mesmo volume: ou o
    arquivo antigo continua íntegro, ou o novo já está completo — nunca um
    meio-termo. Mesmo padrão de `state.save`.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


SUPERSEDED_DIR = "superseded"


def _superseded_dest(wd: str, run_id: int, full: str) -> str:
    """Destino de arquivamento de um artefato superado: `agent-runs/superseded/<run-id>/<rel>`.

    `rel` é o caminho relativo ao workdir (preserva a hierarquia original,
    ex.: `sdd/modules/foo.md`). Se o artefato estiver FORA do workdir (o
    manifesto aceita caminho absoluto), cai para o basename — nunca deixamos
    um `..` compor o destino e escapar do diretório de arquivo.
    """
    rel = _rel_to_wd(wd, full)
    if rel.startswith("../") or rel == ".." or os.path.isabs(rel):
        rel = os.path.basename(full)
    base = os.path.join(wd, "agent-runs", SUPERSEDED_DIR, str(run_id))
    return os.path.join(base, *rel.split("/"))


def _archive_superseded_artifacts(wd: str, targets: list[tuple[int, str]]) -> list[dict]:
    """Move (não deleta) os artefatos dos itens reabertos para o arquivo morto.

    BUG F-23 (auditoria de conteúdo obsoleto): `redo` marcava o run como
    `superseded` e reabria o item, mas o .md antigo continuava no lugar de
    sempre. Como `_agent_run_blockers` deliberadamente NÃO reconfere o
    sha256 de run superado (BQ4), o artefato órfão deixava de ter qualquer
    prova de integridade — e `_audit_file` seguia lendo e PONTUANDO aquele
    conteúdo obsoleto como se fosse a entrega vigente do item reaberto. Um
    `redo` seguido de auditoria "passava" com o texto que o redo existe para
    substituir.

    Mover (em vez de apagar) preserva a trilha exigida pelo BQ4: o conteúdo
    exato que o run superado registrou continua no disco, endereçado pelo id
    do run, e o sha256 gravado no manifesto continua conferível manualmente
    contra o arquivo arquivado.
    """
    archived: list[dict] = []
    seen: set[str] = set()
    for run_id, full in targets:
        key = os.path.normcase(os.path.normpath(os.path.abspath(full)))
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isfile(full):
            continue
        dest = _superseded_dest(wd, run_id, full)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        os.replace(full, dest)
        archived.append({
            "run_id": run_id,
            "de": _rel_to_wd(wd, full),
            "para": _rel_to_wd(wd, dest),
        })
        _prune_empty_dirs(wd, os.path.dirname(full))
    return archived


def _prune_empty_dirs(wd: str, start: str) -> None:
    """Remove diretórios que ficaram VAZIOS após o arquivamento, subindo até o wd.

    `_strict_workdir_blockers` emite `P0: diretório vazio em modules/specs`. Sem
    esta poda, arquivar o único .md de `modules/<x>/` deixaria a pasta vazia
    para trás e o redo criaria, sozinho, um P0 novo no gate. Só apagamos
    diretório comprovadamente vazio (nenhum dado se perde) e nunca subimos
    acima do workdir.
    """
    wd_abs = os.path.abspath(wd)
    current = os.path.abspath(start or wd_abs)
    while current != wd_abs and current.startswith(wd_abs + os.sep):
        try:
            if os.listdir(current):
                return
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def _run_item_artifacts(wd: str, run: dict, names: set[str]) -> list[str]:
    """Caminhos absolutos dos artefatos dos itens de `run` cujo nome está em `names`."""
    out: list[str] = []
    for entry in run.get("items") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("item") or "").strip().replace("\\", "/").strip("/")
        if not name or name not in names:
            continue
        for artifact in entry.get("artifacts") or []:
            if isinstance(artifact, dict):
                raw = artifact.get("path")
            elif isinstance(artifact, str):
                raw = artifact
            else:
                raw = None
            full = _resolve_manifest_path(wd, raw)
            if full:
                out.append(full)
    return out


def redo_stage(wd: str, stage: str, item: str | None = None) -> dict:
    """Marca `superseded` os runs `current` do stage (ou só os que cobrem
    `item`, se informado) e reabre o(s) item(ns) correspondente(s) em
    `state.json` (done -> pending). Nunca remove/reescreve o conteúdo de um
    run antigo — só o campo `status` muda — preservando a trilha de auditoria
    por inteiro (BQ4). Determinístico e idempotente: sem run `current` a
    superar, é um no-op que devolve listas vazias, nunca erro.
    """
    path = _agent_runs_path(wd, stage)
    data = _load_agent_runs(wd, stage)
    if data is None:
        raise ValueError(
            f"agent-runs ausente ou ilegível para stage {stage}: rode run-stage/merge-agent-output antes de redo"
        )
    runs = data.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError(f"agent-runs sem registros para stage {stage}: nada para reabrir")

    target: str | None = None
    if item is not None:
        target = str(item).strip().replace("\\", "/").strip("/")
        if not target:
            raise ValueError("--item vazio")
        ever_covered = set()
        for run in runs:
            if isinstance(run, dict):
                ever_covered |= _run_item_names(run)
        if target not in ever_covered:
            raise ValueError(f"item nunca coberto por nenhum run de {stage}: {target}")

    superseded_ids: list[int] = []
    items_reopened: set[str] = set()
    archive_targets: list[tuple[int, str]] = []
    for idx, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            continue
        run.setdefault("id", idx)
        if _run_status(run) != "current":
            run["status"] = _run_status(run)
            continue
        run_items = _run_item_names(run)
        if target is not None and target not in run_items:
            continue
        run["status"] = "superseded"
        run.setdefault("supersedes", None)
        superseded_ids.append(idx)
        reopened_here = {target} if target is not None else run_items
        items_reopened.update(reopened_here)
        # F-23: só os artefatos dos itens efetivamente reabertos por ESTA
        # chamada saem do lugar. Com `--item`, os demais itens do mesmo run
        # continuam intactos no disco (o run vira superseded por inteiro,
        # mas só o item pedido foi invalidado).
        archive_targets.extend(
            (int(run.get("id") or idx), full)
            for full in _run_item_artifacts(wd, run, reopened_here)
        )

    # Normaliza id/status/supersedes explícitos em TODOS os runs (inclusive os
    # que já eram current e continuam current): a partir daqui o arquivo
    # carrega o schema versionado por inteiro, não só nos runs tocados agora.
    for idx, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            continue
        run.setdefault("id", idx)
        run.setdefault("status", "current")
        run.setdefault("supersedes", None)

    data["runs"] = runs
    data["stage"] = stage
    data["schema"] = AGENT_RUNS_VERSIONED_SCHEMA

    # BUG F-22 (escrita fora de trava): manifesto e state.json descrevem a
    # MESMA transação ("este run não vale mais / este item voltou a pending").
    # A gravação do manifesto acontecia solta, sem nenhuma trava: um
    # `merge-agent-output` concorrente (que serializa em `state._lock`, ver
    # `agentmerge._mark_items_done_transaction`) podia ler o manifesto antes
    # deste redo e reescrevê-lo depois, apagando o `superseded` recém-gravado
    # — o item ficava reaberto em state.json com o run antigo ainda `current`.
    # Segurar a MESMA trava de estado que os demais mutadores usam
    # (`state._lock`, o mecanismo que state expõe para transação de workdir,
    # já usado assim por agentmerge.py e cli.py) põe as duas escritas na
    # mesma fila. O arquivamento F-23 entra na trava pelo mesmo motivo.
    #
    # `state.mark_item` fica FORA da trava de propósito: ele adquire
    # `state._lock` internamente e a trava é por diretório (os.mkdir), não
    # reentrante — chamá-lo aqui dentro travaria o processo contra si mesmo
    # até o TimeoutError. Reimplementar a mutação de item aqui para caber na
    # trava seria duplicar a resolução de grafia de item que `mark_item` já
    # faz (BUG B3); o `mark_item` público, com a sua própria trava, é a API
    # correta.
    with st_mod._lock(wd):
        archived = _archive_superseded_artifacts(wd, archive_targets)
        _write_json_atomic(path, data)

    # BUG B3 (causa-raiz, TODO-1 resolvido): `name` aqui é SEMPRE a forma
    # canônica '/' (vem de `target`/`_run_item_names`, ambos normalizados
    # acima) — nunca resolvida contra a grafia '\\' que possa já estar
    # gravada em `state.json` (ex.: `done` populado por um merge anterior a
    # esta correção, ou por qualquer caminho que preserve separador nativo
    # do SO). Isso costumava exigir uma resolução própria aqui, no mesmo
    # padrão de `agentmerge._state_item_from_stage` (fora do escopo desta
    # correção), para não criar uma segunda entrada ao lado da já existente.
    #
    # Não é mais necessário: `state.mark_item` (state.py, dentro do escopo
    # desta correção) agora resolve `item` contra a grafia já existente em
    # done/pending/blocked/failed/degraded do stage ANTES de mutar — a
    # mesma garantia, agora na camada mais baixa (quem grava em disco),
    # independente de `redo_stage` (ou qualquer outro chamador) já ter
    # normalizado antes de chamar. `redo_stage` está correto por construção
    # sem replicar a resolução aqui; adicioná-la de novo seria redundante.
    for name in sorted(items_reopened):
        st_mod.mark_item(wd, stage, name, done=False)

    return {
        "stage": stage,
        "item": target,
        "itens_reabertos": sorted(items_reopened),
        "runs_superseded": {"quantidade": len(superseded_ids), "ids": superseded_ids},
        "arquivados": archived,
        "proximo_passo": f"run-stage {stage}",
    }


def merged_artifacts(wd: str, stage: str) -> set[str]:
    """Caminhos absolutos normalizados dos artefatos registrados no manifesto do stage."""
    path = os.path.join(wd, "agent-runs", f"{stage}.json")
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return set()
    out: set[str] = set()
    for run in data.get("runs") or []:
        if not isinstance(run, dict):
            continue
        for item in run.get("items") or []:
            if not isinstance(item, dict):
                continue
            for artifact in item.get("artifacts") or []:
                if isinstance(artifact, dict):
                    raw = artifact.get("path")
                elif isinstance(artifact, str):
                    raw = artifact
                else:
                    raw = None
                full = _resolve_manifest_path(wd, raw)
                if full:
                    out.add(os.path.normpath(os.path.abspath(full)))
    return out


def _has_p0_blocker(blockers: list[str]) -> bool:
    return any(blocker.startswith("P0:") for blocker in blockers)


def _strict_workdir_blockers(wd: str, st: dict) -> list[str]:
    blockers: list[str] = []
    for dirpath, dirnames, filenames in os.walk(wd):
        rel_dir = os.path.relpath(dirpath, wd).replace("\\", "/")
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for fn in filenames:
            rel = os.path.join(rel_dir, fn).replace("\\", "/") if rel_dir != "." else fn
            lower = rel.lower()
            if lower.endswith(".py"):
                blockers.append(f"P0: script python proibido no workdir: {rel}")
            if rel_dir == "." and lower.endswith(".txt"):
                blockers.append(f"P0: txt operacional solto no workdir: {rel}")

    modules_root = rel_path(wd, "modules")
    if os.path.isdir(modules_root):
        for dirpath, dirnames, filenames in os.walk(modules_root):
            if dirpath == modules_root:
                continue
            visible_dirs = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            visible_files = [f for f in filenames if not f.startswith(".")]
            if not visible_dirs and not visible_files:
                rel = os.path.relpath(dirpath, wd).replace("\\", "/")
                blockers.append(f"P0: diretório vazio em modules: {rel}")

    specs_root = rel_path(wd, "sdd/specs")
    if os.path.isdir(specs_root):
        allowed_units = _spec_units_from_state(st)
        for name in sorted(os.listdir(specs_root)):
            path = os.path.join(specs_root, name)
            if not os.path.isdir(path):
                continue
            rel = os.path.relpath(path, wd).replace("\\", "/")
            if name == "_unit":
                blockers.append(f"P0: spec placeholder proibida: {rel}")
            if name not in allowed_units:
                blockers.append(f"P0: spec órfã fora do estado: {rel}")
            if _dir_is_empty(path):
                blockers.append(f"P0: diretório vazio em specs: {rel}")
    return list(dict.fromkeys(blockers))


def _spec_units_from_state(st: dict) -> set[str]:
    stage = (st.get("stages") or {}).get("specs") or {}
    units: set[str] = set()
    for key in ("pending", "done", "blocked", "failed"):
        values = stage.get(key) or []
        units.update(str(value) for value in values)
    return units


def _dir_is_empty(path: str) -> bool:
    for _dirpath, dirnames, filenames in os.walk(path):
        visible_dirs = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        visible_files = [f for f in filenames if not f.startswith(".")]
        if visible_dirs or visible_files:
            return False
    return True


def _synth_root_divergence_blockers(wd: str) -> list[str]:
    """sdd/confirmed.md e sdd/inferred.md são canônicos; cópia na raiz diverge -> P0."""
    blockers: list[str] = []
    for name in ("confirmed.md", "inferred.md"):
        root_path = rel_path(wd, name)
        canonical_path = rel_path(wd, f"sdd/{name}")
        if not (os.path.isfile(root_path) and os.path.isfile(canonical_path)):
            continue
        with open(root_path, encoding="utf-8-sig", errors="replace") as f:
            root_text = f.read().strip()
        with open(canonical_path, encoding="utf-8-sig", errors="replace") as f:
            canonical_text = f.read().strip()
        if root_text != canonical_text:
            blockers.append(
                f"P0: cópia divergente do artefato canônico de synth: {name} (raiz) diverge de sdd/{name} "
                f"(canônico); use apenas sdd/{name} e remova {name} da raiz"
            )
    return blockers


MERMAID_START_RE = re.compile(r"```mermaid[^\n]*\n", re.I)
MERMAID_BLOCK_RE = re.compile(r"```mermaid[^\n]*\n(?P<body>.*?)\n```", re.I | re.S)
QUADRANT_POINT_RE = re.compile(r"^\s*P\d{3}:\s*\[\s*(?:0|1|0?\.\d+|1\.0+)\s*,\s*(?:0|1|0?\.\d+|1\.0+)\s*\]\s*$")
MERMAID_NODE_DEF_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:\[|\(|\{)")
# Label do nó: tenta primeiro o conteúdo integralmente entre aspas duplas —
# aceita parênteses, dois-pontos, barras e acentos dentro do label sem
# confundi-los com delimitadores de forma do nó — e só cai para o modo
# "sem aspas" (rejeitado depois) quando o label de fato não está quoted.
# Antes, o (.*?) genérico era ambíguo entre `]`/`)`/`}` de QUALQUER tipo de
# abertura, então um label quoted como `A["Quote Service (v2)"]` era truncado
# no primeiro ")" interno e reportado como "não quoted" — falso positivo
# investigado e corrigido no FIX1(b) do lote B.
MERMAID_NODE_ANY_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)\s*(\[\[?|\(\(?|\{)\s*(\"[^\"]*\"|[^\[\]\(\)\{\}]*)\s*(?:\]\]?|\)\)?|\})"
)
MERMAID_EDGE_RE = re.compile(r"(?:-->|---|-.->|==>)")
_MERMAID_EDGE_LABEL = r"(?:\s*\|(?:\"[^\"]*\"|[^|\n]*)\|)?"
MERMAID_EDGE_LINE_RE = re.compile(
    r"^\s*[A-Za-z][A-Za-z0-9_]*(?:\s*(?:\[\[?.*?\]\]?|\(\(?.*?\)\)?|\{.*?\}))?"
    r"(?:\s*(?:-->|---|-.->|==>)" + _MERMAID_EDGE_LABEL +
    r"\s*[A-Za-z][A-Za-z0-9_]*(?:\s*(?:\[\[?.*?\]\]?|\(\(?.*?\)\)?|\{.*?\}))?)+\s*$"
)
MERMAID_EDGE_LABEL_PREFIX_RE = re.compile(r"^\s*\|(?:\"[^\"]*\"|[^|\n]*)\|\s*")
# Rótulo inline de aresta (`-->|texto|`) não é uma definição de nó. Sem isto,
# um rótulo como `-->|"200 OK (retry)"|` era escaneado por MERMAID_NODE_ANY_RE
# e o token "OK (retry)" virava um falso "node/label não quoted" — investigado
# e corrigido no FIX1(b). Âncorado na seta para não colidir com um "|"
# legítimo dentro de outro label quoted na mesma linha.
MERMAID_EDGE_INLINE_LABEL_RE = re.compile(r"(-->|---|-\.->|==>)\s*\|(?:\"[^\"]*\"|[^|\n]*)\|")
MERMAID_BAD_LABEL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*\s*\[@")
MERMAID_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
MERMAID_FRAGILE_RE = re.compile(r"[`;]")
# Artefatos "de diagrama" cujo gate exige ao menos um bloco ```mermaid``` de um
# dos tipos aceitos abaixo (ver FIX2 do lote B). Chave = ArtifactRule.rel.
NO_DIAGRAM_ESCAPE_RE = re.compile(r"<!--\s*no-diagram:\s*(.+?)\s*-->", re.I)


def _balanced_mermaid_line(line: str) -> bool:
    pairs = {"]": "[", ")": "(", "}": "{"}
    stack: list[str] = []
    quote = False
    escaped = False
    for ch in line:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            quote = not quote
            continue
        if quote:
            continue
        if ch in "[({":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not quote and not stack


def _outside_quotes(line: str) -> str:
    out: list[str] = []
    quote = False
    escaped = False
    for ch in line:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            quote = not quote
            continue
        if not quote:
            out.append(ch)
    return "".join(out)


def _node_refs_in_endpoint(endpoint: str) -> tuple[str | None, bool]:
    endpoint = endpoint.strip()
    if not endpoint:
        return None, False
    # Um endpoint à direita de uma aresta com rótulo (`-->|texto|`) chega aqui como
    # `|texto| B`: o rótulo precisa ser descartado antes de extrair o node id.
    endpoint = MERMAID_EDGE_LABEL_PREFIX_RE.sub("", endpoint, count=1).strip()
    if not endpoint:
        return None, False
    match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(.*)$", endpoint)
    if not match:
        return None, False
    node_id, suffix = match.group(1), match.group(2).strip()
    return node_id, bool(suffix)


def _mmdc_error(body: str, idx: int) -> str | None:
    if os.environ.get("WK_MERMAID_VALIDATE_MMDC") != "1":
        return None
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return None
    with tempfile.TemporaryDirectory(prefix="wk-mmdc-") as tmp:
        src = os.path.join(tmp, "diagram.mmd")
        out = os.path.join(tmp, "diagram.svg")
        with open(src, "w", encoding="utf-8") as f:
            f.write(body)
        proc = subprocess.run(
            [mmdc, "-i", src, "-o", out],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    if proc.returncode == 0:
        return None
    msg = (proc.stderr or "").splitlines()[0:1]
    detail = f": {msg[0]}" if msg else ""
    return f"Mermaid mmdc reprovou bloco {idx}{detail}"


def _mermaid_trecho(text: str, limit: int = 120) -> str:
    """Trecho truncado (~120 chars) para anexar à mensagem de erro — ver
    FIX1(a): sem isto o operador não tinha como saber QUAL linha/label
    disparou a regra sem ler o arquivo inteiro e adivinhar."""
    t = text.strip()
    if len(t) > limit:
        return t[: limit - 1].rstrip() + "…"
    return t


def _strip_line_comment(line: str) -> str:
    """Remove um comentário `%% ...` fora de aspas, preservando o resto da
    linha intacto. Sem isto, uma linha como `A["X"] --> B["Y"] %% nota (v2)`
    tanto escapava do whitelist de aresta (que exige `$` no fim da linha)
    quanto contaminava o scanner de nós com o parêntese do comentário."""
    quote = False
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            quote = not quote
            continue
        if not quote and ch == "%" and line[i : i + 2] == "%%":
            return line[:i].rstrip()
    return line


def _strip_edge_inline_labels(line: str) -> str:
    """Remove o texto de rótulos inline de aresta (`-->|texto|`) antes do
    scanner de definição de nó — ver FIX1(b)/MERMAID_EDGE_INLINE_LABEL_RE."""
    return MERMAID_EDGE_INLINE_LABEL_RE.sub(lambda m: m.group(1), line)


# Tokens de cardinalidade crow's-foot do erDiagram (matriz completa aceita
# pelo Mermaid): zero/um/muitos de cada lado do relacionamento, incluindo a
# forma tracejada (`..`) — ver BUG B1. Cada token tem exatamente 2 chars e é
# removido do texto ANTES de contar `{`/`}`, porque `o{`/`|{`/`}o`/`}|` não
# são abertura/fechamento de bloco de atributos, são notação de "muitos".
ER_CARDINALITY_TOKEN_RE = re.compile(r"\|o|\|\||o\{|\|\{|\}o|\}\||o\|")

# Tipos de diagrama cuja sintaxe usa bloco multi-linha delimitado por `{`/`}`
# que NÃO fecha na mesma linha em que abre (atributos de entidade/classe,
# estado composto, boundary do C4). Balanço por linha (o que o validador
# fazia antes do dispatch por tipo) reprova todo bloco legítimo desse tipo.
_BRACE_BLOCK_KIND_LABELS = {
    "classDiagram": "classDiagram",
    "stateDiagram": "stateDiagram",
    "c4": "C4",
}


def _mermaid_kind(first_line: str) -> str:
    """Classifica o tipo de diagrama pela primeira linha do bloco — usado
    para decidir QUAIS checagens (sintaxe de flowchart, balanço de chaves
    multi-linha, ou nenhuma) se aplicam. Ver BUG B1: antes do dispatch por
    tipo, `_balanced_mermaid_line`/checagem de caractere frágil — semântica
    de flowchart — rodava incondicionalmente sobre QUALQUER bloco mermaid,
    reprovando `erDiagram` com cardinalidade `o{`/`}o`/`|{`/`}|` real."""
    if first_line.startswith("quadrantChart"):
        return "quadrantChart"
    if first_line.startswith(("flowchart", "graph")):
        return "flowchart"
    if first_line.startswith("erDiagram"):
        return "erDiagram"
    if first_line.startswith("classDiagram"):
        return "classDiagram"
    if first_line.startswith(("stateDiagram-v2", "stateDiagram")):
        return "stateDiagram"
    if first_line.startswith("sequenceDiagram"):
        return "sequenceDiagram"
    if first_line.startswith(("C4Context", "C4Container", "C4Component", "C4Dynamic", "C4Deployment")):
        return "c4"
    return "other"


def _multiline_brace_balance_errors(
    lines: list[tuple[int, str]],
    idx: int,
    kind_label: str,
    pattern_name: str,
    strip_re: re.Pattern | None = None,
) -> list[str]:
    """Balanço de `{`/`}` através do BLOCO INTEIRO (não por linha) — usado
    por tipos com bloco multi-linha (atributos de entidade/classe, estado
    composto, boundary do C4). Checar por linha, como o validador fazia
    incondicionalmente antes do dispatch por tipo, reprova toda entidade com
    atributos ou todo estado composto real — a mesma causa-raiz do BUG B1,
    replicada aqui para os demais tipos com sintaxe de bloco (item 4 do
    pedido de correção)."""
    errors: list[str] = []
    depth = 0
    for line_no, ln in lines:
        scan = strip_re.sub("", ln) if strip_re else ln
        depth += scan.count("{") - scan.count("}")
        if depth < 0:
            errors.append(
                f"Mermaid {kind_label} com bloco fechado sem abertura correspondente no bloco {idx}, "
                f"linha {line_no}: '{_mermaid_trecho(ln)}' (padrão: {pattern_name})"
            )
            depth = 0
    if depth:
        errors.append(
            f"Mermaid {kind_label} com bloco aberto sem fechamento no bloco {idx} (padrão: {pattern_name})"
        )
    return errors


def _er_diagram_errors(lines: list[tuple[int, str]], idx: int) -> list[str]:
    """Validação própria e mínima de `erDiagram` (BUG B1): aceita toda a
    matriz de cardinalidade crow's-foot (`||--||`, `||--o{`, `||--|{`,
    `}o--||`, `}|--||`, `}o--o{`, `}|--|{`, `|o--o|`), a variante tracejada
    (`..`), rótulo de relação quoted ou não-quoted, e bloco de atributos
    `ENTIDADE { tipo nome PK }` — sem aplicar as regras de flowchart."""
    body_lines = lines[1:]
    errors = _multiline_brace_balance_errors(
        body_lines, idx, "erDiagram", "erdiagram-bloco-atributos-desbalanceado", strip_re=ER_CARDINALITY_TOKEN_RE
    )
    body_norm = "\n".join(ln for _n, ln in body_lines)
    if "{" not in body_norm and "--" not in body_norm and ".." not in body_norm:
        errors.append(
            f"Mermaid erDiagram sem entidades ou relacionamentos no bloco {idx} (padrão: erdiagram-sem-entidades)"
        )
    return errors


def _mermaid_errors(text: str) -> list[str]:
    if not MERMAID_START_RE.search(text):
        return []
    errors: list[str] = []
    starts = list(MERMAID_START_RE.finditer(text))
    blocks = list(MERMAID_BLOCK_RE.finditer(text))
    if len(blocks) != len(starts):
        errors.append("Mermaid com fence sem fechamento")
    for idx, block in enumerate(blocks, start=1):
        body = block.group("body")
        # Linha absoluta (1-based) dentro do ARQUIVO inteiro, não do bloco —
        # FIX1(a): a mensagem precisa apontar direto pro editor do operador.
        body_start_line = text[: block.start("body")].count("\n") + 1
        lines: list[tuple[int, str]] = [
            (body_start_line + i, raw.rstrip())
            for i, raw in enumerate(body.splitlines())
            if raw.strip()
        ]
        if not lines:
            errors.append(f"Mermaid bloco {idx} vazio")
            continue
        first = lines[0][1].strip()
        kind = _mermaid_kind(first)
        # `_balanced_mermaid_line`/caractere frágil são semântica de
        # flowchart/graph/quadrantChart (label quoted vs. não-quoted). Rodar
        # isso incondicionalmente sobre QUALQUER bloco — como o validador
        # fazia antes do dispatch por tipo — reprova sintaxe legítima de
        # outros tipos (ex.: cardinalidade crow's-foot `o{`/`}o`/`|{`/`}|` de
        # `erDiagram`, que usa `{`/`}` para "muitos", não para bracket de
        # label). Ver BUG B1.
        if kind in ("flowchart", "quadrantChart"):
            for line_no, ln in lines:
                if not _balanced_mermaid_line(ln):
                    errors.append(
                        f"Mermaid com brackets/quotes desbalanceados no bloco {idx}, linha {line_no}: "
                        f"'{_mermaid_trecho(ln)}' (padrão: brackets-quotes-desbalanceados)"
                    )
                    break
                outside = _outside_quotes(ln)
                frag = MERMAID_FRAGILE_RE.search(outside)
                if frag or "@" in outside:
                    trecho_matched = frag.group(0) if frag else "@"
                    errors.append(
                        f"Mermaid com caractere frágil fora de label quoted no bloco {idx}, linha {line_no}: "
                        f"'{_mermaid_trecho(ln)}' -> trecho que casou '{trecho_matched}' "
                        "(padrão: caractere-fragil-fora-de-quote)"
                    )
                    break
        if kind == "quadrantChart":
            point_lines = [(n, ln) for n, ln in lines if re.match(r"^\s*P\d{3}:", ln)]
            for line_no, ln in point_lines:
                if re.search(r"\]\s+P\d{3}\s*:", ln):
                    errors.append(
                        f"Mermaid quadrantChart com multiplos pontos na mesma linha no bloco {idx}, "
                        f"linha {line_no}: '{_mermaid_trecho(ln)}' (padrão: quadrant-multiplos-pontos)"
                    )
                if not QUADRANT_POINT_RE.match(ln):
                    errors.append(
                        f"Mermaid quadrantChart com ponto invalido no bloco {idx}, linha {line_no}: "
                        f"'{_mermaid_trecho(ln)}' (padrão: quadrant-ponto-invalido)"
                    )
        elif kind == "flowchart":
            statements: list[tuple[int, str]] = [
                (line_no, _strip_line_comment(ln))
                for line_no, ln in lines[1:]
                if not ln.strip().startswith(("%%", "classDef", "class "))
            ]
            node_defs: list[str] = []
            defined_nodes: set[str] = set()
            edge_count = 0
            subgraph_depth = 0
            for line_no, ln in statements:
                stripped = ln.strip()
                if stripped.startswith("subgraph "):
                    subgraph_depth += 1
                    continue
                if stripped == "end":
                    subgraph_depth -= 1
                    if subgraph_depth < 0:
                        errors.append(
                            f"Mermaid flowchart com end sem subgraph no bloco {idx}, linha {line_no}: "
                            f"'{_mermaid_trecho(ln)}' (padrão: end-sem-subgraph)"
                        )
                        subgraph_depth = 0
                    continue
                if MERMAID_EDGE_RE.search(ln) and not MERMAID_EDGE_LINE_RE.match(ln):
                    errors.append(
                        f"Mermaid flowchart com sintaxe de aresta fora do whitelist no bloco {idx}, "
                        f"linha {line_no}: '{_mermaid_trecho(ln)}' (padrão: aresta-fora-whitelist)"
                    )
                # Rótulos inline de aresta (`-->|texto|`) não são definição de
                # nó: são removidos antes do scanner para não gerar falso
                # positivo de "label não quoted" a partir do texto do rótulo
                # da aresta (ex.: `-->|"200 OK (retry)"|`) — FIX1(b).
                node_scan_ln = _strip_edge_inline_labels(ln)
                for node_id, _shape, label in MERMAID_NODE_ANY_RE.findall(node_scan_ln):
                    if not MERMAID_ID_RE.match(node_id):
                        errors.append(
                            f"Mermaid flowchart com node id inválido no bloco {idx}, linha {line_no}: "
                            f"'{_mermaid_trecho(ln)}' -> node id '{node_id}' (padrão: node-id-invalido)"
                        )
                    node_defs.append(node_id)
                    defined_nodes.add(node_id)
                    label = label.strip()
                    if not (len(label) >= 2 and label.startswith('"') and label.endswith('"')):
                        errors.append(
                            f"Mermaid flowchart com label nao quoted/sanitizado no bloco {idx}, linha {line_no}: "
                            f"'{_mermaid_trecho(ln)}' -> label capturado '{label}' (padrão: label-nao-quoted)"
                        )
                if MERMAID_EDGE_RE.search(ln):
                    edge_count += len(MERMAID_EDGE_RE.findall(ln))
                    endpoints = [part for part in MERMAID_EDGE_RE.split(ln) if part.strip()]
                    for endpoint in endpoints:
                        node_id, has_inline_def = _node_refs_in_endpoint(endpoint)
                        if node_id and (has_inline_def or node_id in defined_nodes):
                            defined_nodes.add(node_id)
                            continue
                        if node_id:
                            errors.append(
                                f"Mermaid flowchart com node referenciado sem definição no bloco {idx}, "
                                f"linha {line_no}: '{_mermaid_trecho(ln)}' -> node '{node_id}' "
                                "(padrão: node-sem-definicao)"
                            )
            if subgraph_depth:
                errors.append(
                    f"Mermaid flowchart com subgraph/end desbalanceado no bloco {idx} "
                    "(padrão: subgraph-end-desbalanceado)"
                )
            if not node_defs or edge_count == 0:
                errors.append(
                    f"Mermaid flowchart sem nos/arestas no bloco {idx} (padrão: sem-nos-arestas)"
                )
            duplicates = sorted({node for node in node_defs if node_defs.count(node) > 1})
            if duplicates:
                errors.append(
                    f"Mermaid flowchart com node id duplicado no bloco {idx}: {', '.join(duplicates[:5])} "
                    "(padrão: node-id-duplicado)"
                )
            for line_no, ln in statements:
                if len(ln) > 140:
                    errors.append(
                        f"Mermaid flowchart com linha longa demais no bloco {idx}, linha {line_no}: "
                        f"'{_mermaid_trecho(ln)}' (padrão: linha-longa)"
                    )
                if MERMAID_BAD_LABEL_RE.search(ln):
                    errors.append(
                        f"Mermaid flowchart com label nao quoted/sanitizado no bloco {idx}, linha {line_no}: "
                        f"'{_mermaid_trecho(ln)}' (padrão: label-tecnica-invalida)"
                    )
                if ln.count("-->") + ln.count("---") + ln.count("-.->") > 2:
                    errors.append(
                        f"Mermaid flowchart denso demais em uma linha no bloco {idx}, linha {line_no}: "
                        f"'{_mermaid_trecho(ln)}' (padrão: aresta-densa)"
                    )
        elif kind == "erDiagram":
            errors.extend(_er_diagram_errors(lines, idx))
        elif kind in _BRACE_BLOCK_KIND_LABELS:
            # classDiagram (corpo de classe), stateDiagram/-v2 (estado
            # composto) e C4* (boundary) abrem bloco `{`/`}` multi-linha da
            # mesma forma que erDiagram — mesma causa-raiz do BUG B1,
            # corrigida aqui para os 3 tipos que o LOTE-F passou a gerar
            # (item 4 do pedido de correção).
            errors.extend(
                _multiline_brace_balance_errors(
                    lines[1:], idx, _BRACE_BLOCK_KIND_LABELS[kind], f"{kind}-bloco-desbalanceado"
                )
            )
        # sequenceDiagram e tipos não reconhecidos ("other"): nenhuma
        # checagem estrutural própria ainda — evita reintroduzir o mesmo
        # vazamento de regra de flowchart (BUG B1) para sintaxe que este
        # validador não modela. `_mmdc_error` (opt-in via
        # WK_MERMAID_VALIDATE_MMDC=1) continua sendo o validador real de
        # sintaxe quando o mmdc está disponível.
        mmdc_error = _mmdc_error(body, idx)
        if mmdc_error:
            errors.append(mmdc_error)
    return list(dict.fromkeys(errors))


def _mermaid_block_types(text: str) -> list[str]:
    """Tipo (primeiro token, ex.: `flowchart`, `erDiagram`, `C4Context`) de
    cada bloco ```mermaid``` presente em `text`, na ordem em que aparecem."""
    types: list[str] = []
    for match in MERMAID_BLOCK_RE.finditer(text):
        body_lines = [ln.strip() for ln in match.group("body").splitlines() if ln.strip()]
        if not body_lines:
            continue
        head = re.match(r"[A-Za-z][A-Za-z0-9_-]*", body_lines[0])
        if head:
            types.append(head.group(0))
    return types


def _no_diagram_escape_reason(text: str) -> str | None:
    """Motivo declarado no marcador de escape `<!-- no-diagram: <motivo> -->`,
    ou `None` se ausente/sem motivo real. Ver FIX2: escape auditável e
    explícito para não travar permanentemente um pipeline quando o diagrama
    for genuinamente impossível de produzir (ex.: entidade única sem
    relacionamentos ainda a mapear) — em vez de silenciosamente aceitar
    ausência, exige uma justificativa visível na revisão/auditoria."""
    match = NO_DIAGRAM_ESCAPE_RE.search(text)
    if not match:
        return None
    reason = match.group(1).strip()
    return reason or None


def _diagram_requirement_issue(text: str, rule: ArtifactRule) -> tuple[str | None, str | None]:
    """Verifica a exigência de diagrama de `rule.require_diagram`.

    Retorna `(blocker, warning)` — no máximo um dos dois é não-`None`:
    - nenhum bloco mermaid do tipo aceito e sem escape -> `blocker` (P1: gate
      reprova, mensagem cita o(s) tipo(s) esperado(s) por artefato).
    - nenhum bloco mermaid do tipo aceito mas com `<!-- no-diagram: motivo -->`
      justificado -> `warning` (auditável, não bloqueia o `done`/`pass`).
    - tipo aceito encontrado -> `(None, None)`.
    """
    if not rule.require_diagram:
        return None, None
    found = _mermaid_block_types(text)
    if any(t in rule.require_diagram for t in found):
        return None, None
    expected = "/".join(rule.require_diagram)
    reason = _no_diagram_escape_reason(text)
    if reason:
        return None, (
            f"diagrama mermaid obrigatório ausente em {rule.rel}, com escape auditável declarado "
            f"(<!-- no-diagram: {reason} -->); tipo esperado seria {expected}; revisar na próxima "
            "iteração se a ausência ainda é genuinamente inevitável"
        )
    if found:
        observado = "/".join(sorted(set(found)))
        detail = f"observado bloco(s) de tipo incompatível: {observado}"
    else:
        detail = "nenhum bloco ```mermaid``` encontrado"
    return (
        f"diagrama mermaid obrigatório ausente em {rule.rel}: esperado bloco ```mermaid``` do tipo "
        f"{expected}; {detail}. Se o diagrama for genuinamente impossível, documente o motivo com "
        "`<!-- no-diagram: <motivo> -->` no artefato"
    ), None


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

CITATION_SAMPLE_SIZE = 5


def _mask_code_fences(text: str) -> str:
    """Zera o conteúdo dentro de blocos de código, preservando a contagem de linhas.

    BUG F-21 (falso positivo de placeholder): `evidence.find_boilerplate_markers`
    procura TODO/FIXME/TBD/XXX em todo o texto. Um artefato que faz exatamente o
    que o contrato pede — citar o legado com fidelidade — inclui trechos do
    código-fonte dentro de ```java ... ```; se o legado tem um `// TODO`, o
    artefato era reprovado por "placeholder pendente" por ter sido FIEL. O
    marcador dentro da fence é evidência do repositório, não pendência do
    artefato; fora da fence (prosa do agente) continua sendo pendência.

    A correção fica aqui, no ponto de consumo: `find_boilerplate_markers`
    continua com a semântica literal "procure nestes bytes" e quem tem contexto
    de markdown decide quais bytes valem. Fences não fechadas mascaram até o fim
    do arquivo (é o que um leitor de markdown também faz).
    """
    lines = (text or "").splitlines(keepends=True)
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE_RE.match(line)
        marker = match.group(1)[0] * 3 if match else None
        if fence is None:
            if marker is not None:
                fence = marker
                out.append(line)  # a linha de abertura (```java) não carrega marcador
                continue
            out.append(line)
        else:
            out.append("\n" if line.endswith("\n") else "")
            if marker == fence:
                fence = None
    return "".join(out)


_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# Caminho/arquivo técnico (com ou sem citação `:linha`), a mesma forma que
# `evidence.citations` reconhece — mascarado aqui para não contar como
# prosa em inglês (F-30).
_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+"
    r"|\b[A-Za-z0-9_-]+\.[A-Za-z0-9_]{1,8}(?::\d+(?:-\d+)?)?\b"
)


def _mask_for_lang_detection(text: str) -> str:
    """Mascara crases/fences e caminhos antes de contar marcadores EN/PT (F-30).

    O detector de "provável inglês" era manipulável: um artefato honesto em
    PT-BR mas rico em citações/caminhos técnicos (`RequirementsService.java:42`,
    trechos de código entre crases) inflava `en_hits` com tokens que não são
    prosa — não é o agente escrevendo em inglês, é o repositório citado
    fielmente. Mascarar (zerando o conteúdo, preservando o resto do texto)
    antes de contar os marcadores tira essa alavanca de falso positivo/forja.
    """
    masked = _mask_code_fences(text)
    masked = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), masked)
    masked = _PATH_TOKEN_RE.sub(lambda m: " " * len(m.group(0)), masked)
    return masked


def _citation_sample(citations: list, text: str, size: int = CITATION_SAMPLE_SIZE) -> list:
    """Amostra determinística de até `size` citações distintas.

    A ordem é dada por um hash estável de (conteúdo do artefato + citação):
    o mesmo artefato sempre sorteia a MESMA amostra, em qualquer máquina e
    qualquer execução (nada de `hash()` builtin, que varia com PYTHONHASHSEED),
    e artefatos diferentes sorteiam amostras diferentes — quem tenta forjar
    não consegue prever quais das suas citações serão conferidas sem já ter o
    texto final.
    """
    seed = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
    unique: dict[str, object] = {}
    for c in citations:
        unique.setdefault(f"{c.path}:{c.line_start}-{c.line_end}", c)
    ordered = sorted(
        unique.items(),
        key=lambda kv: hashlib.sha256(f"{seed}|{kv[0]}".encode("utf-8")).hexdigest(),
    )
    return [c for _key, c in ordered[:size]]


def _audit_file(
    path: str, rule: ArtifactRule, wd: str | None = None, repo: str | None = None
) -> dict:
    blockers: list[str] = []
    warnings: list[str] = []
    avisos_cerimoniais: list[str] = []
    rel = _rel_to_wd(wd, path) if wd else os.path.basename(path)
    if not os.path.isfile(path):
        return {
            "path": path,
            "status": "missing",
            "score": 0,
            "blockers": [f"arquivo ausente: {rel}"],
            "warnings": [],
            "avisos_cerimoniais": [],
            "criterio": "integridade",
        }
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    lower = text.lower()
    norm = _norm(text)
    score = 100
    checks: dict[str, bool] = {}
    content_bytes = len(text.strip())
    if content_bytes < rule.min_bytes:
        # W8-T8.3 (§3.1): "tamanho de texto apresentado como medida de
        # entendimento" é cerimônia — informativo, não reprova nem reduz
        # score. Integridade (citações/paths/proveniência) segue abaixo,
        # intacta e bloqueante.
        avisos_cerimoniais.append(
            f"tamanho abaixo do sugerido em {rel}: medido {content_bytes} bytes, sugerido >= {rule.min_bytes} "
            "bytes (informativo, não bloqueia — §3.1)"
        )
    else:
        checks["profundidade"] = True
    # F-21: só a prosa fora de ``` fences conta como placeholder pendente.
    found_boilerplate = ev_mod.find_boilerplate_markers(_mask_code_fences(text))
    if found_boilerplate:
        blockers.append(
            f"placeholder pendente em {rel}: marcador(es) observado(s) {', '.join(found_boilerplate[:3])} "
            "(esperado nenhum placeholder)"
        )
        score -= 15
    for section in rule.sections:
        # `section` é uma string (seção única) ou uma tupla de aliases
        # sinônimos — ex.: ("Decisões", "Decisões arquiteturais"). Qualquer
        # alias presente satisfaz a regra; a mensagem cita todos os aliases.
        #
        # W8-T8.3 (§3.1): "quantidade de seções ... apresentado como medida
        # de entendimento" é cerimônia — seção ausente vira aviso
        # informativo (não mais `warnings`, que reduzia score em
        # `_audit_stage`), nunca reprova nem reduz score.
        aliases = section if isinstance(section, tuple) else (section,)
        if not any(f"## {alias}".lower() in lower or f"# {alias}".lower() in lower for alias in aliases):
            label = "/".join(aliases)
            avisos_cerimoniais.append(
                f"seção sugerida ausente: {label} em {rel} (informativo, não bloqueia — §3.1)"
            )
    if rule.sections and not any(a.startswith("seção sugerida ausente") for a in avisos_cerimoniais):
        checks["secoes"] = True
    citations = ev_mod.citations(text)
    if len(citations) < rule.min_citations:
        blockers.append(
            f"citações insuficientes em {rel}: medido {len(citations)}, esperado >= {rule.min_citations}"
        )
        score -= 20
    else:
        checks["rastreabilidade"] = True
    # BUG F-04 (CRÍTICO, gate forjável): até aqui a "rastreabilidade" era
    # contagem de regex — `ev_mod.citations` só reconhece a FORMA
    # `arquivo.ext:linha`. Um artefato inteiramente inventado passava no gate
    # escrevendo `Fake.java:1` as vezes que `rule.min_citations` exigisse: zero
    # relação com o repositório auditado, score cheio em rastreabilidade. O gate
    # que existe para provar procedência aceitava a prova mais barata possível
    # de fabricar.
    #
    # Agora uma AMOSTRA determinística (até CITATION_SAMPLE_SIZE por artefato) é
    # conferida contra o disco do repo com a MESMA lógica de
    # `evidence._citation_error` (arquivo existe, caminho relativo completo,
    # dentro do repo, linha dentro do arquivo) — a mesma regra já aplicada em
    # `evidence.verify_markdown`, não uma segunda implementação que possa
    # divergir. Amostra, e não verificação total, porque um artefato de specs
    # traz dezenas/centenas de citações e cada uma custa uma leitura de arquivo;
    # 5 sorteadas por hash do próprio conteúdo já tornam a forja não-confiável
    # (quem inventa não sabe quais serão conferidas) a custo fixo.
    if citations:
        if repo and os.path.isdir(repo):
            for c in _citation_sample(citations, text):
                err = ev_mod._citation_error(repo, c)
                if not err:
                    continue
                blockers.append(
                    f"citação não confere com o repositório em {rel}: "
                    f"citacao_invalida: {err['citation']} ({err['rule']}: {err['detail']})"
                )
                score -= 20
        else:
            # Sem repo no contexto (ex.: auditoria de workdir isolado, sem o
            # código-fonte por perto) não há como conferir nada: mantemos o
            # comportamento histórico por regex, mas registramos que a
            # rastreabilidade deste artefato NÃO foi provada — para que
            # "não verificado" nunca se confunda com "verificado e válido".
            warnings.append(
                f"citacoes_nao_verificadas: {len(citations)} citação(ões) em {rel} contadas por forma "
                "(arquivo:linha) sem conferência contra o repositório: repo indisponível no contexto "
                f"da auditoria ({repo!r})"
            )
    # F-30: mascara crases/fences e caminhos antes de contar — um `norm`
    # separado só para esta checagem, sem afetar FALLBACK_RE/operational_hits/
    # _generic_artifact mais abaixo, que continuam sobre o `norm` original.
    # Ocorrências (não só presença/ausência): com só 6 marcadores EN
    # cadastrados, contar presença travaria o piso em 6 e `en_hits >= 8`
    # nunca disparia nem para um artefato 100% em inglês — a calibração do
    # contrato pressupõe contagem de ocorrência (prosa real repete "overview",
    # "requirements" etc. várias vezes ao longo do artefato).
    lang_norm = _norm(_mask_for_lang_detection(text))
    pt_hits = sum(lang_norm.count(_norm(marker)) for marker in PT_MARKERS)
    en_hits = sum(lang_norm.count(marker) for marker in EN_MARKERS)
    # Recalibrado (igual ao contrato/evidence): heading isolada em inglês não
    # acusa mais sozinha — só um punhado de termos técnicos em EN (comum em
    # prosa PT-BR real) não é "provável inglês". Só acusa quando o inglês é
    # dominante E robusto: pelo menos o dobro dos marcadores PT-BR, E pelo
    # menos 8 marcadores EN no total — um artefato majoritariamente em
    # inglês, não um artefato PT-BR com jargão técnico.
    # W8-T8.3 (§3.1): "score baseado em idioma ... apresentado como medida de
    # entendimento" é cerimônia — informativo, não reprova nem reduz score.
    if en_hits >= 2 * pt_hits and en_hits >= 8:
        avisos_cerimoniais.append(
            f"provável saída em inglês em {rel} (informativo, não bloqueia — §3.1): "
            f"marcadores pt-br={pt_hits}, en={en_hits}"
        )
    elif pt_hits >= 2 or rule.min_citations == 0:
        checks["ptbr"] = True
    if rule.min_citations > 0 and not any(marker in text for marker in CONFIDENCE_MARKERS):
        blockers.append(
            f"escala de confiança ausente em {rel}: medido 0 marcador(es) \U0001F7E2\U0001F7E1\U0001F534, "
            "esperado >= 1"
        )
        score -= 10
    else:
        checks["confianca"] = True
    if FALLBACK_RE.search(norm):
        blockers.append(f"fallback/falha operacional no artefato em {rel}")
        score -= 20
    bullet_count = len(re.findall(r"(?m)^\s*(?:[-*]|\d+\.|\|)\s*", text))
    operational_hits = sum(1 for marker in OPERATIONAL_MARKERS if marker in norm)
    if bullet_count < 4 or operational_hits < 2:
        blockers.append(
            f"detalhamento operacional insuficiente em {rel}: bullets/tabela medido {bullet_count} "
            f"(esperado >= 4), marcadores operacionais medido {operational_hits} (esperado >= 2)"
        )
        score -= 10
    else:
        checks["operacional"] = True
    if _generic_artifact(norm):
        blockers.append(f"artefato genérico em {rel}: conteúdo sem substância verificável além de frases padrão")
        score -= 20
    if "```mermaid" in lower:
        mermaid_errors = _mermaid_errors(text)
        if mermaid_errors:
            blockers.extend(mermaid_errors)
            score -= min(40, 15 * len(mermaid_errors))
        if re.search(r"^\s*m_[A-Za-z0-9_]+:", text, re.M):
            blockers.append("Mermaid quadrantChart com rótulo técnico frágil")
            score -= 20
    diagram_blocker, diagram_warning = _diagram_requirement_issue(text, rule)
    is_ceremonial_diagram = rule.rel in CEREMONIAL_RULES
    if diagram_blocker and is_ceremonial_diagram:
        # W8-T8.3 (§3.1): "máquina de estados/ERD/diagrama de sequência
        # vazio/genérico ... quando o sistema não contém informação que os
        # justifique" — para os artefatos em CEREMONIAL_RULES a exigência de
        # o diagrama existir vira aviso, não reprova. C4/architecture.md
        # (fora de CEREMONIAL_RULES) continuam bloqueando normalmente abaixo.
        avisos_cerimoniais.append(diagram_blocker + " (informativo, não bloqueia — §3.1)")
    elif diagram_blocker:
        blockers.append(diagram_blocker)
        score -= 25
    elif diagram_warning and is_ceremonial_diagram:
        avisos_cerimoniais.append(diagram_warning)
    elif diagram_warning:
        warnings.append(diagram_warning)
        score -= 5
    elif rule.require_diagram:
        checks["diagrama"] = True
    score = max(0, min(100, score))
    status = "pass" if score >= MIN_DONE_SCORE and not blockers else ("failed" if blockers else "degraded")
    return {
        "path": path,
        "status": status,
        "score": score,
        "bytes": len(text.strip()),
        "citations": len(citations),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "avisos_cerimoniais": avisos_cerimoniais,
        "criterio": "integridade",
    }


def _norm(text: str) -> str:
    raw = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")


def _generic_artifact(norm: str) -> bool:
    generic = ("este modulo", "este arquivo", "responsavel por", "sem informacoes suficientes")
    lines = [ln.strip() for ln in norm.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return len(lines) < 5 or sum(1 for marker in generic if marker in norm) >= 2


def _matrix_error(wd: str) -> str | None:
    root = rel_path(wd, "sdd/specs")
    if not os.path.isdir(root):
        return "matriz codigo->regra->requisito->design->tarefa ausente"
    chunks: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn in ("requirements.md", "design.md", "tasks.md"):
                with open(os.path.join(dirpath, fn), encoding="utf-8-sig", errors="replace") as f:
                    chunks.append(f.read())
    text = _norm("\n".join(chunks))
    missing = [m for m in MATRIX_MARKERS if m not in text]
    if missing:
        return "matriz codigo->regra->requisito->design->tarefa incompleta: " + ",".join(missing)
    if "|" not in text:
        return "matriz codigo->regra->requisito->design->tarefa sem tabela"
    return None
