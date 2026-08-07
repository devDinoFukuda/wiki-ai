"""Contrato operacional dos artefatos SDD do codescan."""

from __future__ import annotations

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
EN_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}#{1,6}\s+"
    r"(overview|responsibility|responsibilities|business rules|requirements|"
    r"technical design|implementation tasks|dependencies|data structures)\s*$"
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
        ),
        ArtifactRule(
            "sdd/c4-context.md",
            min_bytes=450,
            min_citations=0,
            sections=("Contexto",),
        ),
        ArtifactRule(
            "sdd/c4-containers.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Containers",),
        ),
        ArtifactRule(
            "sdd/c4-components.md",
            levels=("completo", "detalhado"),
            min_bytes=300,
            min_citations=0,
            sections=("Componentes",),
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
        "blocks": ("CONFIRMED", "INFERRED", "QUESTIONS"),
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
    "verde_exige_arquivo_linha",
    "amarelo_exige_justificativa",
    "vermelho_exige_pergunta_objetiva",
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
        "Toda afirmação confirmada exige arquivo:linha.",
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

    report = {
        "status": status,
        "score": score,
        "threshold": MIN_DONE_SCORE,
        "stages": results,
        "stages_evaluated": stages_evaluated,
        "artifacts_checked": artifacts_checked,
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


def _audit_stage(wd: str, stage: str, st: dict) -> dict:
    level = doc_level(st)
    artifacts = []
    blockers: list[str] = []
    warnings: list[str] = []
    source_tree_error = _source_tree_error(wd)
    if source_tree_error:
        blockers.append(source_tree_error)
    blockers.extend(_strict_workdir_blockers(wd, st))
    if stage in AGENT_OUTPUT_STAGES:
        blockers.extend(_agent_run_blockers(wd, stage, st))
    for rule in stage_rules(stage, level):
        paths = paths_for_rule(wd, rule)
        if not paths:
            blockers.append(f"ausente: {rule.rel}")
            artifacts.append({"rule": rule.rel, "status": "missing"})
            continue
        for path in paths:
            item = _audit_file(path, rule, wd=wd)
            artifacts.append(item)
            blockers.extend(item["blockers"])
            warnings.extend(item["warnings"])
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
            "blockers": blockers,
            "warnings": warnings,
            "artifacts": artifacts,
            "artifacts_checked": 0,
        }

    artifact_score = min(a.get("score", 0) for a in artifacts)
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
        "blockers": blockers,
        "warnings": warnings,
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
        items_reopened.update({target} if target is not None else run_items)

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
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    for name in sorted(items_reopened):
        st_mod.mark_item(wd, stage, name, done=False)

    return {
        "stage": stage,
        "item": target,
        "itens_reabertos": sorted(items_reopened),
        "runs_superseded": {"quantidade": len(superseded_ids), "ids": superseded_ids},
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
MERMAID_NODE_ANY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\s*(\[\[?|\(\(?|\{)\s*(.*?)\s*(?:\]\]?|\)\)?|\})")
MERMAID_EDGE_RE = re.compile(r"(?:-->|---|-.->|==>)")
_MERMAID_EDGE_LABEL = r"(?:\s*\|(?:\"[^\"]*\"|[^|\n]*)\|)?"
MERMAID_EDGE_LINE_RE = re.compile(
    r"^\s*[A-Za-z][A-Za-z0-9_]*(?:\s*(?:\[\[?.*?\]\]?|\(\(?.*?\)\)?|\{.*?\}))?"
    r"(?:\s*(?:-->|---|-.->|==>)" + _MERMAID_EDGE_LABEL +
    r"\s*[A-Za-z][A-Za-z0-9_]*(?:\s*(?:\[\[?.*?\]\]?|\(\(?.*?\)\)?|\{.*?\}))?)+\s*$"
)
MERMAID_EDGE_LABEL_PREFIX_RE = re.compile(r"^\s*\|(?:\"[^\"]*\"|[^|\n]*)\|\s*")
MERMAID_BAD_LABEL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*\s*\[@")
MERMAID_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
MERMAID_FRAGILE_RE = re.compile(r"[`;]")


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
        lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
        if not lines:
            errors.append(f"Mermaid bloco {idx} vazio")
            continue
        for ln in lines:
            if not _balanced_mermaid_line(ln):
                errors.append(f"Mermaid com brackets/quotes desbalanceados no bloco {idx}")
                break
            outside = _outside_quotes(ln)
            if MERMAID_FRAGILE_RE.search(outside) or "@" in outside:
                errors.append(f"Mermaid com caractere frágil fora de label quoted no bloco {idx}")
                break
        first = lines[0].strip()
        if first.startswith("quadrantChart"):
            point_lines = [ln for ln in lines if re.match(r"^\s*P\d{3}:", ln)]
            for ln in point_lines:
                if re.search(r"\]\s+P\d{3}\s*:", ln):
                    errors.append(f"Mermaid quadrantChart com multiplos pontos na mesma linha no bloco {idx}")
                if not QUADRANT_POINT_RE.match(ln):
                    errors.append(f"Mermaid quadrantChart com ponto invalido no bloco {idx}")
        elif first.startswith(("flowchart", "graph")):
            statements = [
                ln for ln in lines[1:]
                if not ln.strip().startswith(("%%", "classDef", "class "))
            ]
            node_defs: list[str] = []
            defined_nodes: set[str] = set()
            edge_count = 0
            subgraph_depth = 0
            for ln in statements:
                stripped = ln.strip()
                if stripped.startswith("subgraph "):
                    subgraph_depth += 1
                    continue
                if stripped == "end":
                    subgraph_depth -= 1
                    if subgraph_depth < 0:
                        errors.append(f"Mermaid flowchart com end sem subgraph no bloco {idx}")
                        subgraph_depth = 0
                    continue
                if MERMAID_EDGE_RE.search(ln) and not MERMAID_EDGE_LINE_RE.match(ln):
                    errors.append(f"Mermaid flowchart com sintaxe de aresta fora do whitelist no bloco {idx}")
                for node_id, _shape, label in MERMAID_NODE_ANY_RE.findall(ln):
                    if not MERMAID_ID_RE.match(node_id):
                        errors.append(f"Mermaid flowchart com node id inválido no bloco {idx}: {node_id}")
                    node_defs.append(node_id)
                    defined_nodes.add(node_id)
                    label = label.strip()
                    if not (len(label) >= 2 and label.startswith('"') and label.endswith('"')):
                        errors.append(f"Mermaid flowchart com label nao quoted/sanitizado no bloco {idx}")
                if MERMAID_EDGE_RE.search(ln):
                    edge_count += len(MERMAID_EDGE_RE.findall(ln))
                    endpoints = [part for part in MERMAID_EDGE_RE.split(ln) if part.strip()]
                    for endpoint in endpoints:
                        node_id, has_inline_def = _node_refs_in_endpoint(endpoint)
                        if node_id and (has_inline_def or node_id in defined_nodes):
                            defined_nodes.add(node_id)
                            continue
                        if node_id:
                            errors.append(f"Mermaid flowchart com node referenciado sem definição no bloco {idx}: {node_id}")
            if subgraph_depth:
                errors.append(f"Mermaid flowchart com subgraph/end desbalanceado no bloco {idx}")
            if not node_defs or edge_count == 0:
                errors.append(f"Mermaid flowchart sem nos/arestas no bloco {idx}")
            duplicates = sorted({node for node in node_defs if node_defs.count(node) > 1})
            if duplicates:
                errors.append(f"Mermaid flowchart com node id duplicado no bloco {idx}: {', '.join(duplicates[:5])}")
            for ln in statements:
                if len(ln) > 140:
                    errors.append(f"Mermaid flowchart com linha longa demais no bloco {idx}")
                if MERMAID_BAD_LABEL_RE.search(ln):
                    errors.append(f"Mermaid flowchart com label nao quoted/sanitizado no bloco {idx}")
                if ln.count("-->") + ln.count("---") + ln.count("-.->") > 2:
                    errors.append(f"Mermaid flowchart denso demais em uma linha no bloco {idx}")
        elif first.startswith("erDiagram"):
            body_norm = "\n".join(lines[1:])
            if "{" not in body_norm and not re.search(r"\|\|--|--\|\{|\}\|--|--o\{", body_norm):
                errors.append(f"Mermaid erDiagram sem entidades ou relacionamentos no bloco {idx}")
        mmdc_error = _mmdc_error(body, idx)
        if mmdc_error:
            errors.append(mmdc_error)
    return list(dict.fromkeys(errors))


def _audit_file(path: str, rule: ArtifactRule, wd: str | None = None) -> dict:
    blockers: list[str] = []
    warnings: list[str] = []
    rel = _rel_to_wd(wd, path) if wd else os.path.basename(path)
    if not os.path.isfile(path):
        return {"path": path, "status": "missing", "score": 0, "blockers": [f"arquivo ausente: {rel}"], "warnings": []}
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    lower = text.lower()
    norm = _norm(text)
    score = 100
    checks: dict[str, bool] = {}
    content_bytes = len(text.strip())
    if content_bytes < rule.min_bytes:
        blockers.append(
            f"conteúdo insuficiente em {rel}: medido {content_bytes} bytes, esperado >= {rule.min_bytes} bytes"
        )
        score -= 15
    else:
        checks["profundidade"] = True
    found_boilerplate = ev_mod.find_boilerplate_markers(text)
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
        aliases = section if isinstance(section, tuple) else (section,)
        if not any(f"## {alias}".lower() in lower or f"# {alias}".lower() in lower for alias in aliases):
            label = "/".join(aliases)
            warnings.append(
                f"seção ausente: {label} em {rel} (obrigatória; observado ausente, esperado presente)"
            )
            score -= 5
    if rule.sections and not any(w.startswith("seção ausente") for w in warnings):
        checks["secoes"] = True
    citations = ev_mod.citations(text)
    if len(citations) < rule.min_citations:
        blockers.append(
            f"citações insuficientes em {rel}: medido {len(citations)}, esperado >= {rule.min_citations}"
        )
        score -= 20
    else:
        checks["rastreabilidade"] = True
    pt_hits = sum(1 for marker in PT_MARKERS if _norm(marker) in norm)
    en_hits = sum(1 for marker in EN_MARKERS if marker in norm)
    if en_hits > pt_hits or EN_HEADING_RE.search(text):
        blockers.append(
            f"provável saída em inglês em {rel}: não demonstra idioma PT-BR técnico suficiente "
            f"(marcadores pt-br={pt_hits}, en={en_hits}; esperado pt-br > en)"
        )
        score -= 20
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
