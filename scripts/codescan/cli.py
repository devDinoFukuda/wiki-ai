"""CLI do pipeline de codebase.

  surface   estágio 1: varre o repo, produz surface.json (determinístico)
  export    gera inventory.md + dependencies.md do surface.json (determinístico)
  plan      lista os módulos a cavar, em ordem, marca como pendentes
  pending   registra itens pendentes de um estágio (units de specs etc.)
  config    persiste decisões do SDD (doc_level, granularity) no estado
  next      diz o que fazer agora (retomada após sessão morta)
  done      marca item concluído
  blocked   marca estágio/item bloqueado
  failed    marca estágio/item com falha
  degraded  marca estágio/item degradado
  state     mostra o estado
  read      lê arquivo do repo com limite de linhas (leitura confinada)
  evidence  gera pacote JSON de evidências por tópico, sem parser nativo
  run-stage prepara manifesto determinístico para subagentes, sem gerar SDD
  verify    valida Markdown confirmado contra citações arquivo:linha

O legado é READ-ONLY: nada é escrito dentro do repositório analisado.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from . import state as st_mod
from . import evidence as ev_mod
from . import export as ex_mod
from . import coupling as cp_mod
from . import sdd as sdd_mod
from . import agentpack as agentpack_mod
from . import agentmerge as agentmerge_mod
from . import noise as noise_mod
from .surface import scan, to_dict


def _wd(a) -> str:
    return st_mod.workdir(a.store, a.repo)


def _clone_dir(a) -> str:
    """Onde um repo remoto e clonado: fora do workdir/wiki."""
    root = os.environ.get("WK_CLONE_ROOT") or os.path.join(tempfile.gettempdir(), "wiki-ai-code-clones")
    digest = hashlib.sha1(a.repo.encode("utf-8", errors="replace")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", a.repo.rstrip("/\\").split("/")[-1] or "repo")
    return os.path.join(root, f"{name}-{digest}")


def _ensure_repo(a) -> tuple[str, int]:
    """Resolve `--repo` para um caminho local. Clona se for URL.

    Devolve (caminho_local, código). Código != 0 = erro já reportado.
    O clone é blobless (`--filter=blob:none`): histórico completo p/ churn, sem
    baixar todos os blobs. `cleanup` apaga depois.
    """
    if not st_mod.is_url(a.repo):
        p = os.path.abspath(a.repo)
        if not os.path.isdir(p):
            print(json.dumps({"error": f"repo não encontrado: {a.repo}"}), file=sys.stderr)
            return "", 2
        return p, 0

    dest = _clone_dir(a)
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest, 0  # já clonado; reusa entre estágios
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for args in (
        ["git", "clone", "--filter=blob:none", a.repo, dest],
        ["git", "clone", a.repo, dest],  # fallback: git sem partial clone
    ):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                return dest, 0
        except (OSError, subprocess.SubprocessError):
            continue
        shutil.rmtree(dest, ignore_errors=True)
    print(json.dumps({"error": f"falha ao clonar {a.repo}"}), file=sys.stderr)
    return "", 2


def _rm_readonly(func, path, _exc):
    """onerror do rmtree: git deixa arquivos read-only no Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def cmd_cleanup(a) -> int:
    """Apaga o clone de um repo remoto (mantém a análise/SDD do workdir)."""
    if not st_mod.is_url(a.repo):
        print(json.dumps({"skipped": "repo local não é apagado"}, ensure_ascii=False))
        return 0
    dest = _clone_dir(a)
    if os.path.isdir(dest):
        shutil.rmtree(dest, onerror=_rm_readonly)
        print(json.dumps({"removido": dest}, ensure_ascii=False))
    else:
        print(json.dumps({"nada_a_remover": dest}, ensure_ascii=False))
    return 0


def cmd_surface(a) -> int:
    repo_path, code = _ensure_repo(a)
    if code:
        return code
    wd = _wd(a)
    st = st_mod.load(wd) or st_mod.init(wd, a.repo, a.topic)
    if a.topic:
        st["topic"] = a.topic
        st_mod.save(wd, st)

    s = scan(repo_path, module_min_files=a.module_min_files, since=a.since)
    art = os.path.join(wd, "surface.json")
    os.makedirs(wd, exist_ok=True)
    with open(art, "w", encoding="utf-8") as f:
        json.dump(to_dict(s), f, ensure_ascii=False, indent=2)
    st_mod.mark(wd, "surface", "done", art)

    print(
        json.dumps(
            {
                "workdir": wd,
                "artifact": art,
                "arquivos": s.total_files,
                "loc": s.total_loc,
                "linguagens": {k: v["files"] for k, v in sorted(
                    s.languages.items(), key=lambda x: -x[1]["loc"])},
                "modulos": len(s.modules),
                "manifests": len(s.manifests),
                "entry_points": len(s.entry_points),
                "git": s.git.get("available", False),
                "ignorados": s.skipped,
                "warnings": s.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# Parâmetros do auto-dimensionamento do fan-out. O humano NÃO escolhe quantos
# subagentes: colisão (grupo vazio, agente ocioso, partição inválida) nasce de
# um N escolhido a dedo. O CLI decide, com limites seguros.
TARGET_LOC_PER_AGENT = 2000   # fallback quando não houver estimativa de bytes
MAX_AGENTS = 8                # teto: além disso o custo de coordenação supera o ganho


def _auto_batches(mods: list[dict], max_bytes: int = agentpack_mod.DEFAULT_MAX_BYTES) -> int:
    """Decide quantos subagentes usar. Nunca > nº de módulos (sem grupo vazio),
    nunca > MAX_AGENTS, sempre >= 1. Dimensiona por bytes estimados do pack."""
    n_mods = len(mods)
    if n_mods <= 1:
        return 1
    by_bytes = agentpack_mod.choose_batch_count(
        mods,
        agentpack_mod.PackLimits(max_bytes=max_bytes),
        max_agents=MAX_AGENTS,
    )
    total_loc = sum(m.get("loc", 0) for m in mods)
    by_loc = -(-total_loc // TARGET_LOC_PER_AGENT)  # ceil
    return max(1, min(MAX_AGENTS, n_mods, max(by_bytes, by_loc)))


def _balance_batches(mods: list[dict], n: int) -> list[list[dict]]:
    """Particiona módulos em n grupos balanceados por LOC (LPT scheduling).

    Balancear por LOC — não por contagem — evita que um agente pegue os módulos
    pesados e vire o gargalo. Listas disjuntas por construção: cada módulo cai em
    exatamente um grupo, então dois subagentes nunca colidem no mesmo módulo.
    """
    batches: list[list[dict]] = [[] for _ in range(n)]
    loads = [0] * n
    for m in sorted(mods, key=lambda x: -x.get("loc", 0)):
        i = loads.index(min(loads))
        batches[i].append(m)
        loads[i] += m.get("loc", 0)
    return [b for b in batches if b]


def _plan_groups(
    st: dict,
    surface: dict,
    batches: int | None = None,
    max_bytes: int = agentpack_mod.DEFAULT_MAX_BYTES,
) -> list[list[dict]]:
    pending_raw = st.get("stages", {}).get("modules", {}).get("pending") or []
    # BUG B3: compara pela forma CANÔNICA nos dois lados, sem alterar o que
    # fica gravado em `pending` (que precisa continuar batendo literalmente
    # com `Module.path` de surface.json — nativo do SO, '\\' no Windows para
    # diretórios aninhados; ver nota completa perto de `_normalize_item`).
    # Sem isto, um `pending` reescrito em '/' pela normalização de
    # `sdd.redo_stage` (fora do meu escopo) tornaria o módulo reaberto
    # invisível para `_plan_groups` no próximo `run-stage`/`agent-pack`,
    # mesmo com o path correto em surface.json.
    pending = {_normalize_item(p) for p in pending_raw}
    mods = [m for m in surface.get("modules", []) if not pending or _normalize_item(m.get("path", "")) in pending]
    if batches:
        n = max(1, min(batches, len(mods) or 1))
    else:
        n = _auto_batches(mods, max_bytes=max_bytes)
    return _balance_batches(mods, n)


SDD_CONFIG_ACTION = (
    "config --doc-level <essencial|completo|detalhado> "
    "--granularity <module|endpoint|use-case|hybrid|feature>"
)


def _sdd_brief_hint(stage: str) -> str:
    """`sdd-brief <stage>` é a fonte canônica do contrato do estágio — citar
    isso nos erros de run-stage/merge-agent-output/agent-pack tira o incentivo
    de o agente ir ler o código-fonte do produto para entender o contrato."""
    return f"rode `sdd-brief {stage}` para o contrato canônico do estágio"


def _surface_done(st: dict) -> bool:
    return st.get("stages", {}).get("surface", {}).get("status") == "done"


def _missing_sdd_config(st: dict) -> list[str]:
    sdd = st.get("sdd") or {}
    return [f"sdd.{key}" for key in ("doc_level", "granularity") if not sdd.get(key)]


def _sdd_config_gate_payload(missing: list[str], stage: str | None = None) -> dict:
    acao = SDD_CONFIG_ACTION
    if stage:
        acao = f"{_sdd_brief_hint(stage)}; {acao}"
    return {
        "error": "config SDD obrigatória antes de plan/run-stage modules",
        "missing": missing,
        "acao": acao,
    }


def _fail_if_missing_sdd_config(st: dict, stage: str | None = None) -> bool:
    missing = _missing_sdd_config(st)
    if not missing:
        return False
    print(json.dumps(_sdd_config_gate_payload(missing, stage), ensure_ascii=False), file=sys.stderr)
    return True


def cmd_plan(a) -> int:
    wd = _wd(a)
    st = st_mod.load(wd)
    if not st or st["stages"]["surface"]["status"] != "done":
        print(json.dumps({"error": "rode `surface` primeiro"}), file=sys.stderr)
        return 2
    if _fail_if_missing_sdd_config(st):
        return 2
    with open(st["stages"]["surface"]["artifact"], encoding="utf-8") as f:
        s = json.load(f)
    all_mods = s["modules"]

    # Teste fica fora do alvo de escavação por padrão (é cobertura, não regra de
    # negócio). Continua no inventário do surface; --include-tests o traz de volta.
    excluded = 0
    if a.include_tests:
        mods = all_mods
    else:
        mods = [m for m in all_mods if m.get("role", "main") == "main"]
        excluded = len(all_mods) - len(mods)
    if a.top:
        mods = mods[: a.top]

    paths = [m["path"] for m in mods]
    st_mod.set_pending(wd, "modules", paths)

    # N é decidido pelo CLI. --batches existe só como override consciente e, mesmo
    # assim, é limitado a [1, nº de módulos] para nunca gerar grupo vazio.
    if a.batches:
        n = max(1, min(a.batches, len(mods) or 1))
        criterio = f"override do usuário (limitado a {n} p/ não haver grupo vazio)"
    else:
        n = _auto_batches(mods, max_bytes=a.agent_pack_max_bytes)
        criterio = (
            f"auto: min(MAX_AGENTS={MAX_AGENTS}, modulos={len(mods)}, "
            f"ceil(pack_bytes/{a.agent_pack_max_bytes}), ceil(LOC/{TARGET_LOC_PER_AGENT}))"
        )

    groups = _balance_batches(mods, n)
    out = {
        "modulos": len(paths),
        "testes_excluidos": excluded,
        "subagentes": len(groups),
        "criterio": criterio,
        "batches": [
            {"agente": i + 1, "modulos": [m["path"] for m in g],
             "loc_total": sum(m["loc"] for m in g),
             "agent_pack": f"agent-pack modules --batch {i + 1}"}
            for i, g in enumerate(groups)
        ],
        "agent_pack_budget": {
            "max_bytes": a.agent_pack_max_bytes,
            "max_files_per_module": agentpack_mod.DEFAULT_MAX_FILES_PER_MODULE,
            "max_lines_per_file": agentpack_mod.DEFAULT_MAX_LINES_PER_FILE,
        },
        "nota": (
            "Fan-out obrigatório: um subagente por grupo, listas disjuntas. "
            "O `done` de módulos é concorrência-seguro."
        ),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_next(a) -> int:
    wd = _wd(a)
    st = st_mod.load(wd)
    if not st:
        print(json.dumps({"proximo": "surface", "motivo": "nada iniciado"}))
        return 0
    if _surface_done(st):
        missing = _missing_sdd_config(st)
        if missing:
            out = {
                "proximo": "config",
                "status": "pending",
                "motivo": "config SDD obrigatória antes de plan/run-stage modules",
                "missing": missing,
                "acao": SDD_CONFIG_ACTION,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
    stage = None
    for candidate in st_mod.STAGES:
        s_candidate = st.get("stages", {}).get(candidate, {})
        if s_candidate.get("last_error") and s_candidate.get("status") != "done":
            stage = candidate
            break
    if stage is None:
        stage = st_mod.next_stage(st)
    if stage is None:
        print(json.dumps({"proximo": None, "motivo": "pipeline completo"}))
        return 0
    s = st["stages"][stage]
    out = {"proximo": stage, "status": s.get("status")}
    if s.get("last_error"):
        last_error = s.get("last_error")
        error_text = last_error.get("error") if isinstance(last_error, dict) else str(last_error)
        out["motivo"] = "último done falhou"
        out["last_error"] = last_error
        if s.get("last_artifact") is not None:
            out["last_artifact"] = s.get("last_artifact")
        if s.get("last_blockers"):
            out["blockers"] = s.get("last_blockers")
            first = s["last_blockers"][0]
            out["acao"] = first.get("action")
            if first.get("item"):
                out["item"] = first.get("item")
        else:
            out["acao"] = _action_for_blocker(wd, stage, s.get("last_artifact"), error_text)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for problem in st_mod.ITEM_PROBLEM_STATUSES:
        if s.get(problem):
            out[problem] = s[problem]
            if s.get("status") == problem:
                out["item"] = s[problem][0]
    if s.get("status") in st_mod.ITEM_PROBLEM_STATUSES:
        out["motivo"] = f"estágio {stage} está {s.get('status')}"
    if s.get("pending"):
        out["pendentes"] = s["pending"]
        out.setdefault("item", s["pending"][0])
        out["concluidos"] = len(s.get("done") or [])
    elif s.get("items_complete") and s.get("status") == "in_progress":
        out["concluidos"] = len(s.get("done") or [])
        out["motivo"] = "itens concluídos; falta consolidar artefatos SDD e rodar done do estágio"
        out["acao"] = f"gerar artefatos SDD de {stage} e rodar done {stage}"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _err(message: str, **extra) -> None:
    out = {"error": message}
    out.update(extra)
    print(json.dumps(out, ensure_ascii=False), file=sys.stderr)


def _rel_to_wd(wd: str, artifact) -> str | list[str] | None:
    if artifact is None:
        return None
    if isinstance(artifact, list):
        return [_rel_to_wd(wd, item) for item in artifact]
    if not os.path.isabs(str(artifact)):
        return str(artifact).replace("\\", "/")
    try:
        return os.path.relpath(str(artifact), wd).replace("\\", "/")
    except ValueError:
        return str(artifact)


def _action_for_blocker(wd: str, stage: str, artifact, error: str) -> str:
    rel = _rel_to_wd(wd, artifact)
    rels = rel if isinstance(rel, list) else [rel]
    joined_rel = " ".join(str(item) for item in rels if item)
    text = f"{joined_rel} {error}".lower()
    if "data-dictionary.md" in text:
        return "gerar sdd/data-dictionary.md"
    if "flowchart" in text or "sdd/flowcharts" in text:
        return "gerar sdd/flowcharts/*.md"
    if "code-analysis.md" in text:
        return "gerar ou corrigir sdd/code-analysis.md"
    if "confidence-report.md" in text:
        return "gerar ou corrigir sdd/confidence-report.md"
    if "requirements.md" in text or "design.md" in text or "tasks.md" in text:
        return "corrigir artifacts de spec em sdd/specs/<unit>/"
    if "agent-runs" in text:
        return f"registrar agent-runs/{stage}.json via run-stage/merge-agent-output"
    if "artifact de módulo" in text or joined_rel.startswith("modules/"):
        verb = "gerar" if "não encontrado" in error or "nao encontrado" in error else "corrigir"
        return f"{verb} artifact de item: {joined_rel or 'modules/<item>.md'}"
    if "pendente" in text or "não resolvido" in text or "nao resolvido" in text:
        return f"resolver itens pendentes/bloqueados antes de done {stage}"
    return f"corrigir blocker e rodar done {stage}"


def _blocker(wd: str, stage: str, artifact, error: str, item: str | None = None) -> dict:
    out = {
        "error": error,
        "action": _action_for_blocker(wd, stage, artifact, error),
    }
    if item:
        out["item"] = item
    rel = _rel_to_wd(wd, artifact)
    if rel is not None:
        out["artifact"] = rel
    return out


def _dedupe_blockers(blockers: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for blocker in blockers:
        key = json.dumps(blocker, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(blocker)
    return out


def _primary_blocker(blockers: list[dict]) -> tuple[str | list[str] | None, str | None]:
    if not blockers:
        return None, None
    first = blockers[0]
    return first.get("artifact"), first.get("error")


def _module_artifact(wd: str, item: str) -> str:
    slug = ex_mod._slug(item.replace("\\", "/"))
    return os.path.join(wd, "modules", f"{slug}.md")


# --- BUG B3: identificador de item de stage — forma canônica única ---------
#
# Escolha de canônico: '/' (não '\\'). Justificativa:
#   - `sdd.redo_stage`/`sdd._run_item_names` (sdd.py, fora do meu escopo de
#     edição) já normalizam item para '/' incondicionalmente antes de tocar
#     em state.json — se cli.py canonizasse para '\\' os dois lados nunca
#     bateriam de jeito nenhum.
#   - `agentmerge._normalize_item` (agentmerge.py, idem) também usa '/' como
#     alvo quando não há grafia prévia em state.json para reconciliar.
#   - `_stage_pending_items` (abaixo, já existente neste arquivo) já fazia
#     `.replace("\\", "/")` para os stages não-`modules`; '/' já era o padrão
#     de fato em metade do arquivo.
#   - É a única forma portável entre SOs (citações `arquivo:linha`, agent-pack
#     etc. já usam '/'); '\\' quebraria em qualquer runner não-Windows.
#
# `pending` do stage `modules` é o ÚNICO identificador que NÃO é canonizado
# na gravação: ele precisa continuar batendo, caractere a caractere, com
# `Module.path` de surface.json (surface.py, nativo do SO — '\\' no Windows
# para diretórios aninhados), porque `_plan_groups` seleciona módulos por
# esse valor. Em vez de reescrever `pending` (o que dessincronizaria de
# surface.json), `_plan_groups` foi ajustado para comparar as DUAS formas já
# normalizadas — sem alterar o que fica gravado.
ITEM_LIST_KEYS = ("pending", "done", *st_mod.ITEM_PROBLEM_STATUSES)
_ITEM_LIST_PRECEDENCE = ("pending", *st_mod.ITEM_PROBLEM_STATUSES, "done")


def _normalize_item(value) -> str:
    """Forma canônica de um identificador de item: separador '/', sem barras
    duplicadas nem barra de borda. Aceita entrada com '\\' (colada de um
    caminho Windows) ou '/' indistintamente."""
    text = str(value).strip().replace("\\", "/")
    return re.sub(r"/+", "/", text).strip("/")


def _resolve_stored_item(stage_state: dict, item: str) -> str:
    """Resolve `item` (aceita '\\' ou '/') para a grafia JÁ EXISTENTE em
    qualquer lista de item do stage (done/pending/blocked/failed/degraded),
    comparando pela forma canônica — sem isso, `done --item <com \\>` sobre
    um item cuja grafia gravada é '/' (ou vice-versa) cria uma SEGUNDA
    entrada em vez de casar com a existente (a própria causa-raiz do BUG B3
    quando aplicada sem essa resolução). Sem grafia existente, devolve a
    forma canônica ('/') — mesmo padrão que `agentmerge._state_item_from_stage`
    já aplica no caminho de merge-agent-output (agentmerge.py, fora do meu
    escopo), replicado aqui para os comandos que não passam por ele
    (done/blocked/failed/degraded/redo)."""
    wanted = _normalize_item(item)
    if not wanted:
        return wanted
    for key in ITEM_LIST_KEYS:
        for candidate in stage_state.get(key) or []:
            if _normalize_item(candidate) == wanted:
                return str(candidate)
    return wanted


def _reconcile_stage_items(wd: str, stage: str) -> bool:
    """Deduplica identificadores de item que hoje coexistem sob grafias
    diferentes ('\\' vs '/', ou duplicadas dentro da mesma lista) nas listas
    de item de um stage (done/pending/blocked/failed/degraded) em
    state.json.

    BUG B3: `sdd.redo_stage` (sdd.py, fora do meu escopo) sempre normaliza o
    item para '/' antes de chamar `state.mark_item(done=False)`. Se `done`
    já tinha a grafia '\\' (gravada por um `done <stage> --item <com \\>`
    anterior a este fix, ou por qualquer outra via fora do meu controle), o
    `discard('/...')` de `mark_item` não casa com a entrada '\\...' — ela
    fica órfã em `done` enquanto a forma '/' passa a existir em `pending`. Um
    novo merge completa o item de novo (grafia '/', casando com o `pending`
    reaberto) e `done` termina com as DUAS grafias para o mesmo módulo.

    Resolução por PRECEDÊNCIA entre listas quando a MESMA forma canônica
    aparece em mais de uma lista: pending > blocked > failed > degraded >
    done. Um item ainda pendente ou com problema não pode ficar registrado
    como `done` ao mesmo tempo — mantém a leitura "não terminou" em vez de
    aceitar dois estados conflitantes em silêncio. Dentro da MESMA lista,
    colapsa grafias duplicadas do mesmo item para a forma canônica.

    DECISÃO: só reescreve state.json quando encontra divergência real
    (grafias diferentes para o mesmo item, ou o mesmo item em mais de uma
    lista) — nunca em stages já canônicos/sem duplicata. Chamada em todo
    `done`/`blocked`/`failed`/`degraded`/`redo` (idempotente e barata: um
    `state.json` já canônico não sofre nenhuma escrita), não em comandos só
    de leitura (`state`, `next`, `audit`) — evita reescrever o arquivo "sem
    necessidade" fora dos pontos de mutação, mantendo o histórico de mtime
    do arquivo limpo quando nada mudou. Devolve True se persistiu mudança."""
    with st_mod._lock(wd):
        st = st_mod.load(wd)
        if not st:
            return False
        s = st.get("stages", {}).get(stage)
        if not isinstance(s, dict):
            return False

        by_canon: dict[str, dict[str, list[str]]] = {}
        for key in ITEM_LIST_KEYS:
            for raw in s.get(key) or []:
                canon = _normalize_item(raw)
                if not canon:
                    continue
                by_canon.setdefault(canon, {}).setdefault(key, []).append(str(raw))

        new_lists = {key: list(dict.fromkeys(s.get(key) or [])) for key in ITEM_LIST_KEYS}
        artifacts = s.get("artifacts") if isinstance(s.get("artifacts"), dict) else None
        reasons = s.get("reasons") if isinstance(s.get("reasons"), dict) else None
        changed = False

        for canon, occurrences in by_canon.items():
            all_raw = [raw for raws in occurrences.values() for raw in raws]
            if len(occurrences) == 1 and len(set(all_raw)) == 1:
                continue  # já canônico e único: nada a fazer
            changed = True
            winning_key = next(k for k in _ITEM_LIST_PRECEDENCE if k in occurrences)
            winning_raw = occurrences[winning_key][0]
            for key, raws in occurrences.items():
                for raw in set(raws):
                    if key == winning_key and raw == winning_raw:
                        continue
                    if raw in new_lists[key]:
                        new_lists[key].remove(raw)
                    for mapping in (artifacts, reasons):
                        if mapping and raw in mapping:
                            mapping.setdefault(winning_raw, mapping.pop(raw))
            if winning_raw not in new_lists[winning_key]:
                new_lists[winning_key].append(winning_raw)

        if not changed:
            return False

        for key in ITEM_LIST_KEYS:
            s[key] = sorted(new_lists[key])
        s["items_complete"] = bool(
            s.get("done") and not s.get("pending")
            and not any(s.get(k) for k in st_mod.ITEM_PROBLEM_STATUSES)
        )
        if not s["items_complete"]:
            s.pop("finalized", None)
        s["status"] = st_mod._derive_item_stage_status(s, stage)
        st_mod.save(wd, st)
        return True


CONFIDENCE_MARKERS = ("\U0001F7E2", "\U0001F7E1", "\U0001F534")
PT_BR_MARKERS = (
    "visão geral",
    "responsabilidade",
    "responsabilidades",
    "regra de negócio",
    "regras de negócio",
    "requisito",
    "critérios de aceitação",
    "fluxo",
    "fluxos",
    "dependência",
    "dependências",
    "entidade",
    "entidades",
    "função",
    "funções",
    "rastreabilidade",
    "evidência",
    "lacuna",
    "lacunas",
    "tarefa",
    "tarefas",
    "decisão",
    "decisões",
)
EN_MARKERS = (
    "responsibility",
    "responsibilities",
    "data structures",
    "dependencies",
    "overview",
    "business rules",
    "requirements",
    "acceptance criteria",
    "technical design",
    "implementation tasks",
    "main flow",
    "alternative flows",
)
BOILERPLATE_TEXT_MARKERS = (
    "lorem ipsum",
    "as an ai",
    "as a language model",
    "not enough information",
    "no information available",
    "pendente de análise",
    "preencher",
    "<Unit>",
    "<unit>",
    "<descrição>",
    "<descricao>",
    "<arquivo:linha>",
)
ENGLISH_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?"
    r"(Responsibility|Responsibilities|Data Structures(?:/Entities)?|Dependencies|"
    r"Overview|Business Rules|Functional Requirements|Non-Functional Requirements|"
    r"Acceptance Criteria|Technical Design|Main Flow|Alternative Flows|"
    r"Risks and Gaps|Implementation Tasks|Traceability)\s*:?\s*$"
    r"|^\s*\*\*(Responsibility|Data Structures(?:/Entities)?|Dependencies|Overview|Main Flow):\*\*"
)
FAILED_ANALYSIS_RE = re.compile(
    r"(?i)(unable to access|cannot access|permission restrictions|read \(failed\)|"
    r"failed module|n[aã]o consegui acessar|sem permiss[aã]o|falha ao ler|"
    r"n[aã]o foi poss[ií]vel ler)"
)


def _has_section(text: str, aliases: tuple[str, ...]) -> bool:
    for alias in aliases:
        pattern = r"(?im)^\s{0,3}#{1,4}\s+" + re.escape(alias) + r"\s*$"
        if re.search(pattern, text):
            return True
    return False


def _quality_errors(
    text: str,
    *,
    kind: str,
    min_chars: int,
    min_citations: int,
    required_sections: tuple[tuple[str, ...], ...],
    min_pt_markers: int = 3,
    require_confidence: bool = True,
) -> list[str]:
    stripped = text.strip()
    lower = stripped.lower()
    errors: list[str] = []
    if len(stripped) < min_chars:
        errors.append(f"{kind} com profundidade insuficiente: {len(stripped)}/{min_chars} caracteres")
    citation_count = len(ev_mod.citations(text))
    if citation_count < min_citations:
        errors.append(f"{kind} com rastreabilidade insuficiente: {citation_count}/{min_citations} citações")
    if require_confidence and not any(marker in text for marker in CONFIDENCE_MARKERS):
        errors.append(f"{kind} sem escala de confiança 🟢🟡🔴")
    pt_hits = sum(1 for marker in PT_BR_MARKERS if marker in lower)
    en_hits = sum(1 for marker in EN_MARKERS if marker in lower)
    if ENGLISH_HEADING_RE.search(text):
        errors.append(f"{kind} contém cabeçalho/template em inglês; contrato exige PT-BR")
    if pt_hits < min_pt_markers or en_hits > pt_hits:
        errors.append(f"{kind} não demonstra idioma PT-BR técnico suficiente")
    if FAILED_ANALYSIS_RE.search(text):
        errors.append(f"{kind} contém falha de leitura/fallback; marque blocked/failed/degraded")
    found_boilerplate = ev_mod.find_boilerplate_markers(text, BOILERPLATE_TEXT_MARKERS)
    if found_boilerplate:
        errors.append(f"{kind} contém placeholder/boilerplate: {', '.join(found_boilerplate[:3])}")
    missing_sections = ["/".join(section[0] for section in (aliases,)) for aliases in required_sections if not _has_section(text, aliases)]
    if missing_sections:
        errors.append(f"{kind} sem seções obrigatórias: {', '.join(missing_sections)}")
    bullet_count = len(re.findall(r"(?m)^\s*(?:[-*]|\d+\.)\s+", text))
    table_count = len(re.findall(r"(?m)^\s*\|.+\|\s*$", text))
    if bullet_count + table_count < 4:
        errors.append(f"{kind} sem detalhamento operacional mínimo")
    return errors


def _validate_module_done(wd: str, item: str) -> tuple[str | None, str | None]:
    artifact = _module_artifact(wd, item)
    if not os.path.isfile(artifact):
        return artifact, "artifact de módulo não encontrado"
    with open(artifact, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    if not text.strip():
        return artifact, "artifact de módulo vazio"
    errors = _quality_errors(
        text,
        kind="artifact de módulo",
        min_chars=900,
        min_citations=3,
        required_sections=(
            ("Responsabilidade", "Responsabilidades"),
            ("Estruturas de dados", "Entidades"),
            ("Fluxos", "Fluxo principal"),
            ("Dependências", "Dependencias"),
            ("Rastreabilidade", "Evidências"),
        ),
    )
    if errors:
        return artifact, "; ".join(errors)
    return artifact, None


def _spec_dir(wd: str, unit: str) -> str:
    base = os.path.abspath(os.path.join(wd, "sdd", "specs"))
    full = os.path.abspath(os.path.join(base, unit))
    if full != base and full.startswith(base + os.sep):
        return full
    raise ValueError("unit de spec fora de sdd/specs")


def _validate_spec_done(wd: str, unit: str) -> tuple[list[str], str | None]:
    try:
        root = _spec_dir(wd, unit)
    except ValueError as e:
        return [], str(e)
    required = [
        os.path.join(root, "requirements.md"),
        os.path.join(root, "design.md"),
        os.path.join(root, "tasks.md"),
    ]
    missing = [p for p in required if not os.path.isfile(p)]
    if missing:
        return required, "arquivos de spec ausentes: " + ", ".join(os.path.basename(p) for p in missing)
    rules = {
        "requirements.md": {
            "kind": "requirements.md",
            "min_chars": 1100,
            "min_citations": 3,
            "sections": (
                ("Visão geral",),
                ("Responsabilidades",),
                ("Regras de negócio",),
                ("Requisitos funcionais",),
                ("Critérios de aceitação",),
                ("Rastreabilidade de código", "Rastreabilidade"),
            ),
        },
        "design.md": {
            "kind": "design.md",
            "min_chars": 1100,
            "min_citations": 3,
            "sections": (
                ("Interface",),
                ("Fluxo principal",),
                ("Fluxos alternativos",),
                ("Dependências", "Dependencias"),
                ("Decisões de design identificadas", "Decisões de design"),
                ("Riscos e lacunas", "Lacunas"),
            ),
        },
        "tasks.md": {
            "kind": "tasks.md",
            "min_chars": 800,
            "min_citations": 2,
            "sections": (
                ("Pré-requisitos", "Pre-requisitos"),
                ("Tarefas",),
                ("Tarefas de teste",),
                ("Ordem sugerida",),
                ("Lacunas pendentes", "Lacunas"),
            ),
        },
    }
    file_errors: list[str] = []
    for path in required:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        rule = rules[os.path.basename(path)]
        errors = _quality_errors(
            text,
            kind=rule["kind"],
            min_chars=rule["min_chars"],
            min_citations=rule["min_citations"],
            required_sections=rule["sections"],
        )
        if errors:
            file_errors.append(f"{os.path.basename(path)} inválido: " + "; ".join(errors))
    if file_errors:
        return required, "; ".join(file_errors)
    return required, None


def _validate_item_done(wd: str, stage: str, item: str) -> tuple[str | list[str] | None, str | None]:
    if stage == "modules":
        return _validate_module_done(wd, item)
    if stage == "specs":
        return _validate_spec_done(wd, item)
    return None, None


def _doc_level(st: dict) -> str:
    return (st.get("sdd") or {}).get("doc_level") or "essencial"


def _nonempty(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _sdd(wd: str, rel: str) -> str:
    return os.path.join(wd, "sdd", *rel.split("/"))


def _glob_nonempty(root: str, suffix: str = ".md") -> bool:
    if not os.path.isdir(root):
        return False
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(suffix) and os.path.getsize(os.path.join(dirpath, fn)) > 0:
                return True
    return False


def _required_stage_artifacts(wd: str, stage: str, st: dict) -> list[str]:
    level = _doc_level(st)
    required: list[str] = []
    if stage == "modules":
        required.append(_sdd(wd, "code-analysis.md"))
        if level in ("completo", "detalhado"):
            required.append(_sdd(wd, "data-dictionary.md"))
    elif stage == "rules":
        required.append(_sdd(wd, "domain.md"))
        if level in ("completo", "detalhado"):
            required += [
                _sdd(wd, "state-machines.md"),
                _sdd(wd, "permissions.md"),
            ]
    elif stage == "architecture":
        required += [
            _sdd(wd, "architecture.md"),
            _sdd(wd, "c4-context.md"),
        ]
        if level in ("completo", "detalhado"):
            required += [
                _sdd(wd, "c4-containers.md"),
                _sdd(wd, "c4-components.md"),
                _sdd(wd, "erd-complete.md"),
                _sdd(wd, "traceability/spec-impact-matrix.md"),
            ]
    elif stage == "specs":
        required.append(_sdd(wd, "confidence-report.md"))
        if level in ("completo", "detalhado"):
            required += [
                _sdd(wd, "gaps.md"),
                _sdd(wd, "traceability/code-spec-matrix.md"),
            ]
    elif stage == "synth":
        required += [
            _sdd(wd, "confirmed.md"),
            _sdd(wd, "inferred.md"),
        ]
    return required


def _validate_required_artifacts(required: list[str]) -> tuple[list[str], str | None]:
    missing = [p for p in required if not _nonempty(p)]
    if missing:
        return required, "artefatos SDD obrigatórios ausentes ou vazios: " + ", ".join(
            os.path.relpath(p, os.path.commonpath(required)) if len(required) > 1 else p
            for p in missing
        )
    return required, None


def _audit_error(report: dict) -> str | None:
    threshold = report.get("threshold", sdd_mod.MIN_DONE_SCORE)
    score = report.get("score")
    if report.get("status") == "pass" and score is not None and score >= threshold:
        return None
    if report.get("status") == "sem_evidencia":
        # Score nulo por desenho (ver sdd.audit_stages): não formatar como
        # "None/90", que leria como falha numérica em vez de "nada checado".
        return report.get("message") or (
            "auditoria sem evidência: nenhum artefato elegível foi verificado neste escopo"
        )
    reasons: list[str] = []
    for stage in report.get("stages") or []:
        reasons.extend(stage.get("blockers") or [])
        reasons.extend(stage.get("warnings") or [])
        for artifact in stage.get("artifacts") or []:
            reasons.extend(artifact.get("blockers") or [])
            reasons.extend(artifact.get("warnings") or [])
    compact = "; ".join(dict.fromkeys(reasons[:5]))
    score_display = score if score is not None else "sem_evidencia"
    return f"score SDD insuficiente: {score_display}/{threshold}" + (
        f"; {compact}" if compact else ""
    )


def _stage_artifact_quality(wd: str, stage: str, artifact: str) -> str | None:
    """Gate de qualidade de `done <stage>` para os artefatos SDD nomeados.

    Antes desta unificação (BQ5) esta função mantinha uma tabela própria de
    limiares/seções, duplicada e divergente da tabela usada por `audit`
    (`sdd_mod.RULES`/`ArtifactRule`) — um artefato podia passar em `audit` e
    falhar em `done`, ou vice-versa, sem que a mensagem de erro explicasse que
    havia duas regras concorrentes para o mesmo caminho. Agora os limiares e
    seções vêm de `sdd_mod.rule_for_rel`, a MESMA tabela que `audit` consulta
    via `_audit_stage`; só a formatação da mensagem (`_quality_errors`, usada
    também pelos gates por item de módulos/specs) permanece local a `done`.
    """
    rel = os.path.relpath(artifact, wd).replace("\\", "/")
    rule = sdd_mod.rule_for_rel(rel)
    if not rule:
        return None
    with open(artifact, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    errors = _quality_errors(
        text,
        kind=os.path.basename(rel),
        min_chars=rule.min_bytes,
        min_citations=rule.min_citations,
        required_sections=tuple(
            section if isinstance(section, tuple) else (section,) for section in rule.sections
        ),
        require_confidence=rule.min_citations > 0,
    )
    if errors:
        return "; ".join(errors)
    return None


def _audit_blockers(wd: str, stage: str, report: dict) -> list[dict]:
    blockers: list[dict] = []
    for stage_report in report.get("stages") or []:
        for error in (stage_report.get("blockers") or []) + (stage_report.get("warnings") or []):
            blockers.append(_blocker(wd, stage, None, error))
        for artifact in stage_report.get("artifacts") or []:
            path = artifact.get("path") or artifact.get("rule")
            for error in (artifact.get("blockers") or []) + (artifact.get("warnings") or []):
                blockers.append(_blocker(wd, stage, path, error))
    if not blockers:
        error = _audit_error(report)
        if error:
            blockers.append(_blocker(wd, stage, stage, error))
    return blockers


def _plan_fanout_required(wd: str, stage: str) -> int | None:
    plan = _load_json_or_none(_plan_manifest_path(wd, stage))
    if not plan:
        return None
    value = plan.get("fanout_required")
    return value if isinstance(value, int) else None


def _registered_agents(wd: str, stage: str) -> list[str]:
    runs = _load_json_or_none(os.path.join(_agent_runs_dir(wd), f"{stage}.json"))
    if not runs:
        return []
    return [r.get("agent") for r in (runs.get("runs") or []) if r.get("agent")]


def _fanout_gate_error(wd: str, stage: str) -> str | None:
    """`done <stage>` recusa fechar quando o plano exigia mais de um batch mas
    todo agent-run registrado veio do mesmo `agent` — sinal direto de que o
    fan-out não aconteceu (um único agente fez o trabalho de N subagentes)."""
    required = _plan_fanout_required(wd, stage)
    if not required or required <= 1:
        return None
    agents = _registered_agents(wd, stage)
    if not agents:
        return None
    distinct = sorted(set(agents))
    if len(distinct) <= 1:
        return (
            f"fan-out obrigatório não cumprido em {stage}: eram esperados {required} batch(es), "
            f"um subagente por batch, mas os agent-runs registrados usam só {len(distinct)} "
            f"agente(s) distinto(s): {', '.join(distinct)}"
        )
    return None


def _validate_stage_done_blockers(wd: str, stage: str) -> list[dict]:
    st = st_mod.load(wd) or {}
    s = st.get("stages", {}).get(stage, {})
    blockers: list[dict] = []
    fanout_error = _fanout_gate_error(wd, stage)
    if fanout_error:
        blockers.append(_blocker(wd, stage, os.path.join(_agent_runs_dir(wd), f"{stage}.json"), fanout_error))
    if stage in ("modules", "specs"):
        done = s.get("done") or []
        if not done:
            blockers.append(_blocker(wd, stage, None, f"estágio {stage} não tem itens concluídos"))
        for problem in ("pending", "blocked", "failed", "degraded"):
            items = s.get(problem) or []
            if items:
                blockers.append(_blocker(
                    wd,
                    stage,
                    None,
                    f"estágio {stage} ainda tem itens {problem}: {', '.join(items[:10])}",
                ))
        for item in done:
            artifact, error = _validate_item_done(wd, stage, item)
            if error:
                blockers.append(_blocker(wd, stage, artifact, f"{item}: {error}", item=item))

    required = _required_stage_artifacts(wd, stage, st)
    artifact, error = _validate_required_artifacts(required)
    if error:
        blockers.append(_blocker(wd, stage, artifact, error))
    for path in required:
        if not _nonempty(path):
            continue
        quality_error = _stage_artifact_quality(wd, stage, path)
        if quality_error:
            blockers.append(_blocker(wd, stage, path, quality_error))

    if stage == "modules" and _doc_level(st) in ("completo", "detalhado"):
        root = _sdd(wd, "flowcharts")
        if not _glob_nonempty(root):
            blockers.append(_blocker(wd, stage, root, "doc_level completo exige ao menos um flowchart em sdd/flowcharts/"))
    if stage == "rules" and _doc_level(st) in ("completo", "detalhado"):
        root = _sdd(wd, "adrs")
        if not _glob_nonempty(root):
            blockers.append(_blocker(wd, stage, root, "doc_level completo exige ADRs retroativos em sdd/adrs/"))
    if stage == "architecture" and _doc_level(st) == "detalhado":
        root = _sdd(wd, "sequences")
        if not _glob_nonempty(root):
            blockers.append(_blocker(wd, stage, root, "doc_level detalhado exige diagramas de sequência em sdd/sequences/"))
    if stage == "specs" and _doc_level(st) in ("completo", "detalhado"):
        root = _sdd(wd, "user-stories")
        if not _glob_nonempty(root):
            blockers.append(_blocker(wd, stage, root, "doc_level completo exige user stories em sdd/user-stories/"))
    if stage == "specs":
        units = s.get("done") or []
        if not units:
            blockers.append(_blocker(wd, stage, _sdd(wd, "specs"), "estágio specs exige units concluídas"))
    if stage == "synth":
        for prior in ("modules", "rules", "architecture", "specs"):
            if st.get("stages", {}).get(prior, {}).get("status") != "done":
                blockers.append(_blocker(wd, stage, None, f"synth não pode concluir antes de {prior}"))
    if stage in sdd_mod.CRITICAL_STAGES:
        report = sdd_mod.audit(wd, stage, st)
        error = _audit_error(report)
        if error:
            blockers.extend(_audit_blockers(wd, stage, report))
    return _dedupe_blockers(blockers)


def _validate_stage_done(wd: str, stage: str) -> tuple[str | list[str] | None, str | None]:
    return _primary_blocker(_validate_stage_done_blockers(wd, stage))


def _validate_artifact_was_merged(wd: str, stage: str, artifact: str) -> str | None:
    """Recusa `done <stage> --artifact <path>` quando o artefato nunca passou
    por `merge-agent-output`: sem isso, um item fecha sem manifesto/proveniência."""
    merged = sdd_mod.merged_artifacts(wd, stage)
    candidate = os.path.normpath(os.path.abspath(artifact))
    if candidate in merged:
        return None
    return (
        f"artefato não passou pelo merge-agent-output do stage {stage}: {artifact}; "
        f"rode merge-agent-output {stage} --input <resposta-do-subagente> antes de done {stage}"
    )


def cmd_done(a) -> int:
    wd = _wd(a)
    # BUG B3: migra/deduplica grafias '\\'/'/' já gravadas para este stage
    # antes de validar/mutar, e resolve --item (aceita as duas formas) para
    # a grafia já existente em state.json — sem isso, `done --item` com
    # separador diferente do gravado cria uma segunda entrada em vez de
    # casar com a existente.
    _reconcile_stage_items(wd, a.stage)
    if a.item:
        st_now = st_mod.load(wd) or {}
        a.item = _resolve_stored_item(st_now.get("stages", {}).get(a.stage, {}), a.item)
    if a.item:
        artifact, error = _validate_item_done(wd, a.stage, a.item)
        blockers = [_blocker(wd, a.stage, artifact, f"{a.item}: {error}", item=a.item)] if error else []
    else:
        blockers = _validate_stage_done_blockers(wd, a.stage)
        artifact, error = _primary_blocker(blockers)
    if error:
        artifact_out = _rel_to_wd(wd, artifact)
        st_mod.record_stage_error(wd, a.stage, error, artifact_out, blockers)
        extra = {"artifact": artifact_out}
        if len(blockers) > 1:
            extra["blockers"] = blockers
        _err(error, **extra)
        return 2
    if a.item:
        st = st_mod.mark_item(wd, a.stage, a.item, done=True)
    else:
        if a.artifact:
            merge_error = _validate_artifact_was_merged(wd, a.stage, a.artifact)
            if merge_error:
                blockers = [_blocker(wd, a.stage, a.artifact, merge_error)]
                st_mod.record_stage_error(wd, a.stage, merge_error, a.artifact, blockers)
                _err(merge_error, artifact=a.artifact)
                return 2
        try:
            st = st_mod.mark(wd, a.stage, "done", a.artifact)
        except ValueError as e:
            error = str(e)
            blockers = [_blocker(wd, a.stage, None, error)]
            st_mod.record_stage_error(wd, a.stage, error, None, blockers)
            _err(error)
            return 2
    st = st_mod.clear_stage_error(wd, a.stage)
    print(json.dumps(st["stages"][a.stage], ensure_ascii=False, indent=2))
    return 0


def cmd_problem_status(a) -> int:
    wd = _wd(a)
    # BUG B3: mesma migração/resolução de grafia aplicada em cmd_done.
    _reconcile_stage_items(wd, a.stage)
    if a.item:
        st_now = st_mod.load(wd) or {}
        a.item = _resolve_stored_item(st_now.get("stages", {}).get(a.stage, {}), a.item)
    try:
        if a.item:
            st = st_mod.mark_item_status(wd, a.stage, a.item, a.status, artifact=a.artifact)
        else:
            st = st_mod.mark(wd, a.stage, a.status, a.artifact)
    except ValueError as e:
        _err(str(e))
        return 2
    print(json.dumps(st["stages"][a.stage], ensure_ascii=False, indent=2))
    return 0


def cmd_state(a) -> int:
    wd = _wd(a)
    st = st_mod.load(wd)
    if not st:
        print(json.dumps({"error": "sem estado; rode `surface`"}), file=sys.stderr)
        return 1
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_read(a) -> int:
    """Leitura confinada ao repo, numerada, com limite.

    Existe para que o agente cite `arquivo:linha` sem carregar arquivo inteiro
    no contexto — e para impedir leitura fora do repositório declarado.
    """
    repo, code = _ensure_repo(a)
    if code:
        return code
    full = os.path.abspath(os.path.join(repo, a.path))
    if not full.startswith(repo + os.sep):
        print(json.dumps({"error": "caminho fora do repositório"}), file=sys.stderr)
        return 2
    if not os.path.isfile(full):
        print(json.dumps({"error": f"não é arquivo: {a.path}"}), file=sys.stderr)
        return 2
    lines = open(full, encoding="utf-8-sig", errors="replace").read().splitlines()
    start = max(0, a.line_from - 1) if a.line_from else 0
    end = min(len(lines), start + a.count) if a.count else len(lines)
    w = len(str(end))
    print(f"# {a.path}  (linhas {start+1}-{end} de {len(lines)})")
    for i, ln in enumerate(lines[start:end], start=start + 1):
        print(f"{str(i).rjust(w)}\t{ln}")
    return 0


def _load_surface_or_error(wd: str, stage_hint: str | None = None) -> tuple[dict | None, int]:
    st = st_mod.load(wd)
    if not st or st.get("stages", {}).get("surface", {}).get("status") != "done":
        payload = {"error": "rode `surface` primeiro"}
        if stage_hint:
            payload["acao"] = f"{_sdd_brief_hint(stage_hint)}; rode surface antes de {stage_hint}"
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return None, 2
    art = st["stages"]["surface"].get("artifact")
    if not art or not os.path.isfile(art):
        payload = {"error": "surface.json não encontrado", "artifact": art}
        if stage_hint:
            payload["acao"] = f"{_sdd_brief_hint(stage_hint)}; rode surface antes de {stage_hint}"
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return None, 2
    with open(art, encoding="utf-8") as f:
        return json.load(f), 0


def cmd_evidence(a) -> int:
    """Gera evidence-pack JSON a partir de surface + busca lexical no repo.

    Este estágio preserva o agnosticismo: não usa parser, AST nem toolchain de
    linguagem. O pacote resultante é contexto rastreável para o agente sintetizar
    `confirmed.md`/`inferred.md` sem carregar o repo inteiro.
    """
    wd = _wd(a)
    surface, code = _load_surface_or_error(wd)
    if surface is None:
        return code
    repo_path, code = _ensure_repo(a)
    if code:
        return code
    st = st_mod.load(wd) or {}
    topic = a.topic or st.get("topic")
    if not topic:
        print(json.dumps({"error": "informe --topic ou rode surface --topic"}), file=sys.stderr)
        return 2
    artifact = a.output or os.path.join(wd, f"evidence-{topic}.json")
    try:
        pack = ev_mod.build_evidence_pack(
            repo_path,
            surface,
            topic,
            top=a.top,
            context=a.context,
            max_lines=a.max_lines,
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    ev_mod.write_json(artifact, pack)
    st_mod.mark(wd, "evidence", "done", artifact)
    print(
        json.dumps(
            {
                "artifact": artifact,
                "topic": topic,
                "items": len(pack["items"]),
                "warnings": pack.get("warnings", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_agent_pack(a) -> int:
    """Gera contexto rastreavel para subagentes sem copiar a arvore do repo."""
    wd = _wd(a)
    st = st_mod.load(wd) or {}
    limits = agentpack_mod.PackLimits(
        max_bytes=a.max_bytes,
        max_files_per_module=a.max_files,
        max_lines_per_file=a.max_lines,
    )
    if a.stage == "modules":
        surface, code = _load_surface_or_error(wd, stage_hint=a.stage)
        if surface is None:
            return code
        repo_path, code = _ensure_repo(a)
        if code:
            return code
        groups = _plan_groups(st, surface, a.batches, max_bytes=a.max_bytes)
        if not groups:
            print(json.dumps({
                "error": "sem modulos pendentes; rode plan",
                "acao": f"{_sdd_brief_hint(a.stage)}; rode plan antes de agent-pack",
            }, ensure_ascii=False), file=sys.stderr)
            return 2
        if a.batch < 1 or a.batch > len(groups):
            print(json.dumps({
                "error": f"batch invalido: 1..{len(groups)}",
                "acao": _sdd_brief_hint(a.stage),
            }, ensure_ascii=False), file=sys.stderr)
            return 2
        total_batches = len(groups)
        pack = agentpack_mod.build_agent_pack(
            repo=repo_path,
            repo_label=a.repo,
            stage=a.stage,
            batch=a.batch,
            total_batches=total_batches,
            modules=groups[a.batch - 1],
            limits=limits,
        )
    else:
        if a.batch != 1:
            print(json.dumps({
                "error": "batch invalido: 1..1",
                "acao": _sdd_brief_hint(a.stage),
            }, ensure_ascii=False), file=sys.stderr)
            return 2
        total_batches = 1
        items = _stage_pending_items(st, a.stage)
        if not items:
            items = [a.stage]
        pack = agentpack_mod.build_stage_pack(
            wd=wd,
            repo_label=a.repo,
            stage=a.stage,
            batch=1,
            total_batches=total_batches,
            items=items,
            limits=limits,
        )

    outdir = os.path.join(wd, "agent-packs")
    os.makedirs(outdir, exist_ok=True)
    artifact = a.output or os.path.join(outdir, f"{a.stage}-batch-{a.batch:02d}.json")
    agentpack_mod.dump_compact_json(artifact, pack)
    print(json.dumps({
        "artifact": artifact,
        "stage": a.stage,
        "batch": a.batch,
        "total_batches": total_batches,
        "schema": pack["schema"],
        "bytes": pack["metrics"]["bytes"],
        "max_bytes": a.max_bytes,
        "modules": pack["metrics"].get("modules", pack["metrics"].get("sources", 0)),
        "files": pack["metrics"]["files"],
        "dropped": pack["metrics"]["dropped"],
    }, ensure_ascii=False, indent=2))
    return 0


GENERIC_AGENT_VALUES = {"main", "orquestrador", "orchestrator", "self", "principal"}


def _validate_agent_id(agent: str | None) -> str | None:
    """`--agent` é obrigatório em merge-agent-output: sem ele, a ausência de
    fan-out (um único agente "principal" fazendo tudo) não fica registrada no
    manifesto e passa despercebida até o `done` do estágio."""
    if agent is None or not agent.strip():
        return "merge-agent-output exige --agent <identificador do subagente>; sem isso não dá para provar fan-out"
    if agent.strip().lower() in GENERIC_AGENT_VALUES:
        return (
            f"--agent {agent!r} é genérico demais; use o identificador real do subagente "
            "(ex.: o agent_slot do batch, como 'modules-b01')"
        )
    return None


def _plan_manifest_path(wd: str, stage: str) -> str:
    return os.path.join(_agent_runs_dir(wd), f"{stage}-plan.json")


def _load_json_or_none(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _plan_created_at_epoch(wd: str, stage: str) -> float | None:
    plan = _load_json_or_none(_plan_manifest_path(wd, stage))
    if not plan:
        return None
    created_at = plan.get("created_at")
    if not created_at:
        return None
    try:
        return calendar.timegm(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _validate_input_freshness(wd: str, stage: str, input_path: str) -> str | None:
    """Recusa `--input` mais antigo que o plano de fan-out do mesmo estágio.

    Sem isto, um arquivo pré-existente (ex.: sobra de uma tentativa anterior,
    ou artefato copiado à mão) pode ser passado como se fosse resposta fresca
    de subagente. Quando não existe `agent-runs/<stage>-plan.json`, não há
    como comparar — não bloqueia (compat com fluxos sem run-stage)."""
    plan_epoch = _plan_created_at_epoch(wd, stage)
    if plan_epoch is None:
        return None
    try:
        input_mtime = os.path.getmtime(input_path)
    except OSError:
        return None
    if input_mtime < plan_epoch:
        return (
            f"input {input_path} tem mtime anterior ao plano de fan-out "
            f"(agent-runs/{stage}-plan.json); arquivo pré-existente não pode ser "
            "passado como resposta de subagente — gere/grave o output depois de `run-stage`"
        )
    return None


def _merge_input_items(stage: str, text: str) -> list[str]:
    """Extrai os nomes de item que `agentmerge_mod.merge_agent_output` vai
    gravar a partir de `text`, reusando o parser interno de cada stage
    (read-only: nenhum arquivo é escrito aqui). Best-effort: entrada malformada
    devolve lista vazia — o próprio `merge_agent_output` reporta o erro real."""
    try:
        if stage == "modules":
            return [item for item, _content in agentmerge_mod._parse_modules(text)]
        if stage == "specs":
            units, named_docs = agentmerge_mod._parse_specs(text)
            return [item for item, _files in units] + [name for name, _content in named_docs]
        if stage in agentmerge_mod.NAMED_STAGE_HEADER_RE:
            return [name for name, _content in agentmerge_mod._parse_named_blocks(stage, text)]
    except agentmerge_mod.MergeError:
        return []
    return []


def _merge_redo_conflict_error(wd: str, stage: str, input_path: str) -> str | None:
    """Recusa re-merge de item(ns) que já têm um run `current` registrado
    (BQ4 ponto 5): sem isto, reescrever a resposta do subagente e rodar
    `merge-agent-output` de novo invalidava silenciosamente a trilha anterior
    em vez de passar pelo caminho suportado (`redo`)."""
    try:
        with open(input_path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    items = _merge_input_items(stage, text)
    if not items:
        return None
    current = sdd_mod.current_run_items(wd, stage)
    conflicts = sorted({item for item in items if item in current})
    if not conflicts:
        return None
    sample = ", ".join(conflicts[:5])
    return (
        f"item já mergeado; rode `wk code ... redo {stage} --item <item>` antes de refazer "
        f"(itens em conflito: {sample})"
    )


def cmd_merge_agent_output(a) -> int:
    wd = _wd(a)
    if not os.path.isfile(a.input):
        print(json.dumps({
            "error": f"input não encontrado: {a.input}",
            "acao": _sdd_brief_hint(a.stage),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    agent_error = _validate_agent_id(getattr(a, "agent", None))
    if agent_error:
        print(json.dumps({
            "error": agent_error,
            "acao": _sdd_brief_hint(a.stage),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    freshness_error = _validate_input_freshness(wd, a.stage, a.input)
    if freshness_error:
        print(json.dumps({
            "error": freshness_error,
            "acao": _sdd_brief_hint(a.stage),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    redo_conflict = _merge_redo_conflict_error(wd, a.stage, a.input)
    if redo_conflict:
        print(json.dumps({
            "error": redo_conflict,
            "acao": _sdd_brief_hint(a.stage),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        result = agentmerge_mod.merge_agent_output(wd, a.stage, a.input, agent=a.agent)
    except agentmerge_mod.MergeError as e:
        print(json.dumps({
            "error": str(e),
            "acao": _sdd_brief_hint(a.stage),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_redo(a) -> int:
    """Reabre um stage/item para refazer o merge sem apagar a trilha de
    auditoria: marca `superseded` os runs `current` cobertos e devolve o(s)
    item(ns) para `pending` em state.json (BQ4)."""
    wd = _wd(a)
    # BUG B3: migra grafias divergentes já gravadas ANTES de redo — reduz o
    # risco de sdd.redo_stage (que sempre normaliza `item` para '/' e chama
    # state.mark_item com essa forma) falhar em casar contra uma entrada
    # '\\' legada.
    _reconcile_stage_items(wd, a.stage)
    try:
        result = sdd_mod.redo_stage(wd, a.stage, item=a.item)
    except ValueError as e:
        print(json.dumps({
            "error": str(e),
            "acao": _sdd_brief_hint(a.stage),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    # sdd.redo_stage (sdd.py, fora do meu escopo) normaliza `item` para '/'
    # incondicionalmente antes de chamar state.mark_item(done=False): se
    # `done` só tinha a grafia '\\' para o mesmo módulo, o discard() interno
    # não casa e o item reaberto acaba coexistindo como '\\' em `done` e '/'
    # em `pending`. Reconciliar de novo AQUI, logo após o redo, é o único
    # ponto em que cli.py consegue fechar essa lacuna sem editar sdd.py.
    _reconcile_stage_items(wd, a.stage)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


RUN_STAGE_ROLES = {
    "modules": "Module Archaeologist",
    "rules": "Business Rules Detective",
    "architecture": "Architecture Reviewer",
    "specs": "Specification Writer",
    "synth": "Synthesis Writer",
}


def _agent_runs_dir(wd: str) -> str:
    return os.path.join(wd, "agent-runs")


def _stage_pending_items(st: dict, stage: str) -> list[str]:
    items = st.get("stages", {}).get(stage, {}).get("pending") or []
    return [str(item).replace("\\", "/").strip("/") for item in items if str(item).strip()]


def _write_compact_manifest(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def _run_stage_batch_output(wd: str, stage: str, index: int) -> str:
    return os.path.join(wd, "agent-outputs", f"{stage}-batch-{index:02d}.txt")


def _run_stage_agent_slot(stage: str, index: int) -> str:
    return f"{stage}-b{index:02d}"


def _run_stage_merge_command(stage: str, output_path: str, agent_slot: str) -> str:
    return f"merge-agent-output {stage} --input {output_path} --agent {agent_slot}"


def _run_stage_next_action(stage: str, batches: list[dict]) -> str:
    """Instrução literal de fan-out: quantos subagentes disparar (um por batch,
    listas disjuntas) e onde cada um grava a saída. Precisa sobreviver ao modo
    quiet (é campo escalar) — é o principal remédio contra o orquestrador que
    nunca dispara subagente depois de `run-stage`."""
    steps = " ".join(
        f"[{b['agent_slot']}] itens={b['items']} -> grava o recibo em {b['output']} "
        f"e rode `{b['merge_command']}`."
        for b in batches
    )
    return (
        f"FAN-OUT OBRIGATORIO: dispare agora exatamente {len(batches)} subagente(s) para o "
        f"estágio {stage}, um por batch, listas de itens disjuntas. {steps}"
    )


def _stage_contract_path(wd: str, stage: str) -> str:
    return os.path.join(wd, "agent-packs", f"{stage}-contract.json")


def _write_stage_contract(wd: str, stage: str, st: dict) -> str:
    """Materializa o contrato do estágio (mesmo payload do sdd-brief) em
    arquivo ao lado dos agent-packs. O subagente lê pack + contrato como
    arquivos — nenhum comando precisa ser executado por LLM no fan-out."""
    path = _stage_contract_path(wd, stage)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(sdd_mod.brief(wd, stage, st), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _handoff_prompt(stage: str, batches: list[dict], contract_path: str) -> str:
    """Prompt de despacho pronto para colar na LLM: despachante dispara um
    subagente por batch; cada subagente lê 2 arquivos e grava 1."""
    lines = [
        f"Fan-out do estágio {stage}. Você é despachante: NÃO analise o repositório você mesmo,",
        "NÃO gere conteúdo você mesmo, NÃO execute comandos, NÃO faça merge.",
        "",
        f"Dispare exatamente {len(batches)} subagente(s), um por batch, listas de itens disjuntas:",
        "",
    ]
    for b in batches:
        items = ", ".join(str(i) for i in (b.get("items") or []))
        lines += [
            f"[batch {int(b['batch']):02d} — subagente {b['agent_slot']}]",
            f"  LEIA:    {b['agent_pack']}",
            f"  LEIA:    {contract_path}",
            f"  ESCREVA: {b['output']}",
            f"  ITENS:   {items}",
            "",
        ]
    lines += [
        "Instrução para cada subagente:",
        "  Analise SOMENTE os itens do seu pack.",
        "  Escreva no caminho ESCREVA só blocos `=== <BLOCO>: <id> ===` … `=== END ===`, conforme o contrato.",
        "  Item impossível: bloco FAILED conforme o contrato.",
        "  MARCADORES: 🟢 confirmado (exige arquivo:linha) · 🟡 inferido (justificativa) · 🔴 desconhecido (pergunta objetiva).",
        '  PROIBIDO no arquivo: código, diff, log, saída de comando, prosa fora de bloco, "Ran command", "Edited", "Wrote".',
        "  PT-BR técnico, sem preâmbulo, sem resumo, sem conclusão.",
        "  Devolva só: ARQUIVO: <caminho> / BLOCOS: <n> / BYTES: <n>",
        "",
        "AO FINAL, devolva SOMENTE a lista de recibos (ARQUIVO/BLOCOS/BYTES por batch).",
    ]
    return "\n".join(lines)


def cmd_handoff(a) -> int:
    """Imprime o prompt de despacho do fan-out — o único texto que o humano
    entrega à LLM. Exige manifesto de run-stage; regrava o contrato para
    garantir frescor com o doc_level atual."""
    wd = _wd(a)
    st = st_mod.load(wd)
    manifest_path = os.path.join(_agent_runs_dir(wd), f"{a.stage}-plan.json")
    if not st or not os.path.isfile(manifest_path):
        print(json.dumps({
            "error": f"manifesto ausente para {a.stage}; rode run-stage {a.stage} antes de handoff",
            "acao": f"{_sdd_brief_hint(a.stage)}; rode run-stage {a.stage} e depois handoff {a.stage}",
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    batches = manifest.get("batches") or []
    if not batches:
        print(json.dumps({
            "error": f"manifesto de {a.stage} sem batches",
            "acao": f"rode run-stage {a.stage} novamente",
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    contract_path = _write_stage_contract(wd, a.stage, st)
    print(_handoff_prompt(a.stage, batches, contract_path))
    return 0


def _run_stage_permission_probe(wd: str) -> str | None:
    """Prova determinística de permissão antes de emitir o manifesto do run-stage.

    Verifica que o processo consegue escrever+apagar um sentinela em
    `<workdir>/agent-outputs/` (destino do recibo do subagente) e ler um
    arquivo de `<workdir>/sdd/` quando essa árvore já existir. Sem isso, um
    subagente sem acesso ao store improvisa lendo o repositório — violação
    de contrato observada em execução real.

    Devolve None quando o probe passa, ou uma mensagem de erro citando o
    caminho exato sem acesso.
    """
    outputs_dir = os.path.join(wd, "agent-outputs")
    try:
        os.makedirs(outputs_dir, exist_ok=True)
        sentinel = os.path.join(outputs_dir, ".permission-probe")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(sentinel)
    except OSError as exc:
        return f"sem permissão de escrita em {outputs_dir}: {exc}"

    sdd_dir = os.path.join(wd, "sdd")
    if os.path.isdir(sdd_dir):
        try:
            for dirpath, _dirnames, filenames in os.walk(sdd_dir):
                for filename in filenames:
                    path = os.path.join(dirpath, filename)
                    with open(path, "rb") as f:
                        f.read(1)
                    return None
        except OSError as exc:
            return f"sem permissão de leitura em {sdd_dir}: {exc}"
    return None


def cmd_run_stage(a) -> int:
    """Prepara execução por subagentes. Não gera conteúdo SDD."""
    wd = _wd(a)
    st = st_mod.load(wd)
    if not st:
        print(json.dumps({
            "error": "rode surface/config/pending antes de run-stage",
            "acao": f"{_sdd_brief_hint(a.stage)}; rode surface/config/pending antes de run-stage {a.stage}",
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    probe_error = _run_stage_permission_probe(wd)
    if probe_error:
        print(
            json.dumps(
                {
                    "error": probe_error,
                    "acao": (
                        f"{_sdd_brief_hint(a.stage)}; corrija a permissão: "
                        f"wk init --store {a.store} --repo {a.repo}"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    stage = a.stage
    role = RUN_STAGE_ROLES[stage]
    batches: list[dict] = []
    packs: list[str] = []

    if stage == "modules":
        surface, code = _load_surface_or_error(wd, stage_hint=stage)
        if surface is None:
            return code
        if _fail_if_missing_sdd_config(st, stage=stage):
            return 2
        repo_path, code = _ensure_repo(a)
        if code:
            return code
        groups = _plan_groups(st, surface, a.batches, max_bytes=a.max_bytes)
        if not groups:
            print(json.dumps({
                "error": "sem módulos pendentes; rode plan",
                "acao": f"{_sdd_brief_hint(stage)}; rode plan antes de run-stage {stage}",
            }, ensure_ascii=False), file=sys.stderr)
            return 2
        limits = agentpack_mod.PackLimits(
            max_bytes=a.max_bytes,
            max_files_per_module=a.max_files,
            max_lines_per_file=a.max_lines,
        )
        outdir = os.path.join(wd, "agent-packs")
        os.makedirs(outdir, exist_ok=True)
        for idx, group in enumerate(groups, start=1):
            pack = agentpack_mod.build_agent_pack(
                repo=repo_path,
                repo_label=a.repo,
                stage=stage,
                batch=idx,
                total_batches=len(groups),
                modules=group,
                limits=limits,
            )
            artifact = os.path.join(outdir, f"{stage}-batch-{idx:02d}.json")
            agentpack_mod.dump_compact_json(artifact, pack)
            packs.append(artifact)
            output = _run_stage_batch_output(wd, stage, idx)
            slot = _run_stage_agent_slot(stage, idx)
            batches.append({
                "batch": idx,
                "agent_slot": slot,
                "role": role,
                "items": [m.get("path") for m in group],
                "agent_pack": artifact,
                "output": output,
                "merge_command": _run_stage_merge_command(stage, output, slot),
                "pack_bytes": pack.get("metrics", {}).get("bytes", 0),
            })
    else:
        items = _stage_pending_items(st, stage)
        if not items:
            items = [stage]
        limits = agentpack_mod.PackLimits(
            max_bytes=a.max_bytes,
            max_files_per_module=a.max_files,
            max_lines_per_file=a.max_lines,
        )
        outdir = os.path.join(wd, "agent-packs")
        os.makedirs(outdir, exist_ok=True)
        pack = agentpack_mod.build_stage_pack(
            wd=wd,
            repo_label=a.repo,
            stage=stage,
            batch=1,
            total_batches=1,
            items=items,
            limits=limits,
        )
        artifact = os.path.join(outdir, f"{stage}-batch-01.json")
        agentpack_mod.dump_compact_json(artifact, pack)
        packs.append(artifact)
        output = _run_stage_batch_output(wd, stage, 1)
        slot = _run_stage_agent_slot(stage, 1)
        batches.append({
            "batch": 1,
            "agent_slot": slot,
            "role": role,
            "items": items,
            "agent_pack": artifact,
            "output": output,
            "merge_command": _run_stage_merge_command(stage, output, slot),
            "pack_bytes": pack.get("metrics", {}).get("bytes", 0),
        })

    fanout_required = len(batches)
    next_action = _run_stage_next_action(stage, batches)
    contract_path = _write_stage_contract(wd, stage, st)

    manifest = {
        "schema": "wiki-ai.run-stage-plan.v1",
        "stage": stage,
        "workdir": wd,
        "contract": contract_path,
        "role": role,
        "requires_subagents": True,
        "spawns_agents": False,
        "generates_sdd_content": False,
        "fanout_required": fanout_required,
        "next_action": next_action,
        "rules": [
            "orquestrador nao gera conteudo SDD",
            "subagente grava o artefato ele mesmo no caminho `output` do batch",
            "subagente retorna somente o recibo de 3 linhas (ARQUIVO/BLOCOS/BYTES)",
            "sem eco de comando, log, diff, saida de ferramenta ou conteudo do artefato escrito",
            "merge-agent-output integra o artefato gravado pelo subagente",
            "cada subagente usa o proprio agent_slot como --agent no merge-agent-output",
        ],
        "receipt_contract": noise_mod.receipt_contract(),
        "batches": batches,
        "packs": packs,
        "created_at": st_mod._now(),
    }
    manifest_path = os.path.join(_agent_runs_dir(wd), f"{stage}-plan.json")
    _write_compact_manifest(manifest_path, manifest)
    print(json.dumps({
        "schema": manifest["schema"],
        "stage": stage,
        "manifest": manifest_path,
        "contract": contract_path,
        "fanout_required": fanout_required,
        "next_action": next_action,
        "batches": [
            {
                "batch": b["batch"],
                "agent_slot": b["agent_slot"],
                "role": b["role"],
                "items": b["items"],
                "agent_pack": b["agent_pack"],
                "output": b["output"],
                "merge_command": b["merge_command"],
            }
            for b in batches
        ],
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


_EXPORT_GUARDED_STORE_SUBDIRS = ("raw", "wiki")


def _is_under_dir(path: str, base: str) -> bool:
    path_n = os.path.normpath(os.path.abspath(path))
    base_n = os.path.normpath(os.path.abspath(base))
    return path_n == base_n or path_n.startswith(base_n + os.sep)


def _export_guardrail_error(store: str, outdir: str) -> str | None:
    """`export --output` não pode escrever direto em <store>/raw ou <store>/wiki:
    esses diretórios só mudam via `wk publish`/promote. Saída manual ali furou
    o guardrail antes; o destino correto para exportar é inbox/."""
    store_abs = os.path.abspath(store)
    for sub in _EXPORT_GUARDED_STORE_SUBDIRS:
        guarded = os.path.join(store_abs, sub)
        if _is_under_dir(outdir, guarded):
            return (
                f"export --output não pode escrever dentro de {sub}/ do store "
                f"({guarded}): grave em inbox/ e promova com `wk publish --workdir <workdir> --topic <topic>` "
                "(ou `wk store` + promote), nunca escreva direto em raw/ ou wiki/"
            )
    return None


def cmd_export(a) -> int:
    """Todos os artefatos SDD determinísticos: inventory, dependencies, coupling.

    Sem LLM. inventory/dependencies vêm do surface.json; coupling caminha o repo
    para o grafo de imports (Ce/Ca/I 🟢) + zonas de design (🟡) — motor Java
    (classe e pacote, ciclos, classe-deus, hotspot de Ca, abstração
    especulativa) quando o repo tem Java, motor genérico multi-linguagem
    caso contrário (ver `coupling.py`). Os três (`inventory.md`,
    `dependencies.md`, `coupling.md`) saem `source_type: code-repo`, prontos
    para o inbox/. Obrigatório no estágio 1 — não é passo opcional. Quando o
    motor Java roda, também é escrito `coupling.html` — um artefato LOCAL de
    inspeção interativa (não entra em `written`/inbox, não publica).
    """
    import time

    wd = _wd(a)
    surface, code = _load_surface_or_error(wd)
    if surface is None:
        return code
    st = st_mod.load(wd) or {}
    topic = a.topic or st.get("topic")
    outdir = a.output or os.path.join(wd, "sdd")
    if a.output:
        guard_error = _export_guardrail_error(a.store, outdir)
        if guard_error:
            print(json.dumps({"error": guard_error, "output": outdir}, ensure_ascii=False), file=sys.stderr)
            return 2
    written = ex_mod.export_deterministic(surface, outdir, topic=topic)

    # coupling: zonas de design. Determinístico, mas caminha o repo.
    coupling_info: dict = {}
    repo_path, code = _ensure_repo(a)
    if code == 0:
        analysis = cp_mod.analyze(repo_path, surface)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        os.makedirs(outdir, exist_ok=True)
        art = os.path.join(outdir, "coupling.md")
        with open(art, "w", encoding="utf-8", newline="\n") as f:
            f.write(cp_mod.render(surface, analysis, topic, now))
        written.append(art)
        zones: dict = {}
        for r in analysis["modules"]:
            zones[r["zone"]] = zones.get(r["zone"], 0) + 1
        coupling_info = {
            "engine": analysis.get("engine"),
            "edges": len(analysis["edges"]),
            "zonas": zones,
        }
        summary = analysis.get("summary")
        if analysis.get("engine") == "java" and summary:
            coupling_info.update({
                "ciclos_pacotes": summary.get("cycles_packages"),
                "ciclos_classes": summary.get("cycles_classes"),
                "classes_deus": summary.get("god_classes"),
                "hotspots": summary.get("concrete_hotspots"),
                "especulativas": summary.get("speculative_abstractions"),
            })
        # coupling.html: artefato LOCAL de inspeção (não publicável, não
        # entra em `written`/SDD) — só existe para o motor Java.
        html_out = cp_mod.render_html(surface, analysis, now)
        if html_out is not None:
            html_art = os.path.join(outdir, "coupling.html")
            with open(html_art, "w", encoding="utf-8", newline="\n") as f:
                f.write(html_out)
            coupling_info["html"] = html_art
    else:
        coupling_info = {"skipped": "repo indisponível para coupling"}

    print(
        json.dumps(
            {"outdir": outdir, "artifacts": written, "topic": topic,
             "coupling": coupling_info},
            ensure_ascii=False, indent=2,
        )
    )
    return 0


def cmd_config(a) -> int:
    """Persiste decisões do SDD (doc_level, granularity) no estado.

    Não use o campo `artifact` do estágio surface para isso: ele aponta para o
    surface.json e `plan`/`export`/`evidence` dependem dele.
    """
    wd = _wd(a)
    with st_mod._lock(wd):
        st = st_mod.load(wd)
        if not st:
            print(json.dumps({"error": "sem estado; rode `surface` primeiro"}), file=sys.stderr)
            return 2
        sdd = st.setdefault("sdd", {})
        if a.doc_level:
            sdd["doc_level"] = a.doc_level
        if a.granularity:
            sdd["granularity"] = a.granularity
        st_mod.save(wd, st)
    print(json.dumps(sdd, ensure_ascii=False, indent=2))
    return 0


def cmd_pending(a) -> int:
    """Registra a lista de pendências de um estágio (ex.: units do `specs`).

    O `plan` faz isso para `modules`; os estágios novos (architecture, specs)
    precisam do mesmo tracking para sobreviver a sessão morta.
    """
    # BUG B3: aceita '\\' ou '/' na entrada e grava sempre a forma canônica
    # ('/'). Seguro mesmo para o stage `modules` (normalmente povoado por
    # `plan`, não por este comando): `_plan_groups` compara pending contra
    # surface.json já pela forma normalizada dos dois lados (ver
    # `_plan_groups`), então gravar '/' aqui não desincroniza a seleção de
    # módulos do `run-stage`/`agent-pack`.
    items = [_normalize_item(s) for s in (a.items or "").split(",") if s.strip()]
    items = [i for i in items if i]
    if not items:
        print(json.dumps({"error": "informe --items a,b,c"}), file=sys.stderr)
        return 2
    st = st_mod.set_pending(_wd(a), a.stage, items)
    print(json.dumps(st["stages"][a.stage], ensure_ascii=False, indent=2))
    return 0


def _stage_has_existing_artifact(wd: str, stage: str, st: dict) -> bool:
    paths = list(_required_stage_artifacts(wd, stage, st))
    s = st.get("stages", {}).get(stage, {})
    if stage == "modules":
        paths.extend(_module_artifact(wd, item) for item in (s.get("done") or []))
    elif stage == "specs":
        for unit in (s.get("done") or []) + (s.get("pending") or []):
            try:
                root = _spec_dir(wd, unit)
            except ValueError:
                continue
            paths.extend(
                os.path.join(root, name)
                for name in ("requirements.md", "design.md", "tasks.md")
            )
    return any(_nonempty(path) for path in paths)


def _strict_audit_scope(wd: str, st: dict) -> list[str]:
    stages = list(sdd_mod.CRITICAL_STAGES)
    state_stages = st.get("stages") or {}
    scope: list[str] = []
    for index, stage in enumerate(stages):
        s = state_stages.get(stage, {})
        if s.get("status") == "done":
            scope.append(stage)
            continue
        current = s.get("status") == "in_progress" or bool(s.get("items_complete"))
        if current and _stage_has_existing_artifact(wd, stage, st):
            scope.append(stage)
        break
    return scope


def cmd_audit(a) -> int:
    wd = _wd(a)
    st = st_mod.load(wd)
    if not st:
        print(json.dumps({"error": "sem estado; rode `surface`"}), file=sys.stderr)
        return 2
    if getattr(a, "stage", None) and getattr(a, "stage_pos", None) and a.stage != a.stage_pos:
        print(json.dumps({"error": "stage conflitante"}), file=sys.stderr)
        return 2
    stage = a.stage or getattr(a, "stage_pos", None)
    _strict = getattr(a, "strict", False)
    if _strict and not stage:
        report = sdd_mod.audit_stages(wd, _strict_audit_scope(wd, st), st)
        report["scope"] = "strict-current"
    else:
        report = sdd_mod.audit(wd, stage, st)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    score = report.get("score")
    ok = report["status"] == "pass" and score is not None and score >= report["threshold"]
    return 0 if ok else 1


def _load_sdd_state(a) -> tuple[str, dict]:
    wd = _wd(a)
    st = st_mod.load(wd) or {"sdd": {}, "stages": {}}
    return wd, st


def cmd_sdd_brief(a) -> int:
    wd, st = _load_sdd_state(a)
    report = sdd_mod.brief(wd, a.stage, st)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_sdd_scaffold(a) -> int:
    wd, st = _load_sdd_state(a)
    report = sdd_mod.scaffold(wd, a.stage, st)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# --- F-02: `verify` passa a ser estado POR COBERTURA, não por execução ------
#
# Antes, `verify --artifact <qualquer.md>` chamava
# `st_mod.mark(wd, "verify", "done")` — um único artefato trivial (até um .md
# sem nenhuma claim) marcava o STAGE INTEIRO como aprovado, e o portão de
# promote/compile/docx em `wk/cli.py` (que lê `stages.verify.status`) liberava
# o workdir todo. Agora cada execução registra o resultado do SEU artefato em
# `stages.verify.artifacts` (mapa acumulado entre execuções) e o `status` do
# stage é DERIVADO da cobertura dos artefatos obrigatórios.
_VERIFY_REQUIRED_ALWAYS = ("sdd/confirmed.md",)
_VERIFY_REQUIRED_IF_PRESENT = ("sdd/inferred.md",)


def _verify_artifact_key(wd: str, artifact: str) -> str:
    """Chave estável do artefato no mapa de cobertura: caminho relativo ao
    workdir, com '/'. Artefato fora do workdir (caso de uso avulso) fica com o
    caminho absoluto normalizado — nunca colide com um obrigatório."""
    wd_abs = os.path.abspath(wd)
    art_abs = os.path.abspath(artifact)
    try:
        rel = os.path.relpath(art_abs, wd_abs)
    except ValueError:  # drives distintos no Windows
        return art_abs.replace("\\", "/")
    if rel.startswith(os.pardir) or os.path.isabs(rel):
        return art_abs.replace("\\", "/")
    return rel.replace("\\", "/")


def _verify_required_rels(wd: str) -> list[str]:
    """Artefatos que precisam estar `done` para o stage virar `done`.

    `sdd/confirmed.md` é sempre exigido (é o artefato que o portão protege);
    `sdd/inferred.md` só entra se existir no workdir — workdir que nunca
    produziu inferred não fica travado por um arquivo que não deveria existir.
    """
    rels = list(_VERIFY_REQUIRED_ALWAYS)
    for rel in _VERIFY_REQUIRED_IF_PRESENT:
        if os.path.isfile(_sdd(wd, rel.split("/", 1)[1])):
            rels.append(rel)
    return rels


def _verify_coverage(wd: str, artifacts: dict) -> dict:
    """Deriva status do stage + relatório de cobertura a partir do mapa."""
    required = _verify_required_rels(wd)
    verificados = sorted(k for k, v in artifacts.items() if v == "done")
    reprovados = sorted(k for k, v in artifacts.items() if v == "failed")
    faltantes = [rel for rel in required if artifacts.get(rel) != "done"]
    if reprovados:
        status = "failed"
    elif not faltantes:
        status = "done"
    else:
        status = "in_progress"
    return {
        "status": status,
        "obrigatorios": required,
        "verificados": verificados,
        "reprovados": reprovados,
        "faltantes_obrigatorios": faltantes,
    }


def _verify_next_action(wd: str, cobertura: dict) -> str:
    """Próximo `verify` a rodar para fechar o portão — nunca deixa o agente
    adivinhar por que `status` não virou `done`."""
    alvo = None
    if cobertura["reprovados"]:
        alvo = cobertura["reprovados"][0]
        motivo = "corrija as citações reprovadas e revalide"
    elif cobertura["faltantes_obrigatorios"]:
        alvo = cobertura["faltantes_obrigatorios"][0]
        motivo = "artefato obrigatório ainda não verificado"
    if not alvo:
        return "cobertura completa: stage verify aprovado para promote/compile/docx"
    caminho = os.path.join(wd, *alvo.split("/")) if not os.path.isabs(alvo) else alvo
    return f"{motivo}: rode `verify --artifact {caminho}`"


def cmd_verify(a) -> int:
    """Valida Markdown confirmado.

    O verificador não tenta provar semântica; ele garante o contrato mínimo:
    claim verde em bullet precisa ter citação `arquivo:linha`, a citação precisa
    apontar para arquivo/linha existentes dentro do repo declarado, e o
    documento inteiro precisa ter ao menos uma citação válida se tiver claims.

    O resultado é gravado POR ARTEFATO em `stages.verify.artifacts`; o
    `status` do stage é a cobertura dos obrigatórios (ver `_verify_coverage`).
    """
    if not os.path.isfile(a.artifact):
        print(json.dumps({"error": f"artifact não encontrado: {a.artifact}"}), file=sys.stderr)
        return 2
    repo_path, code = _ensure_repo(a)
    if code:
        return code
    md = open(a.artifact, encoding="utf-8-sig", errors="replace").read()
    report = ev_mod.verify_markdown(repo_path, md)
    out = a.output
    if out:
        ev_mod.write_json(out, report)

    wd = _wd(a)
    key = _verify_artifact_key(wd, a.artifact)
    st = st_mod.load(wd) or {}
    stage = st.setdefault("stages", {}).setdefault("verify", {})
    artifacts = stage.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    artifacts[key] = "done" if report["ok"] else "failed"  # merge com execuções anteriores
    stage["artifacts"] = artifacts
    st_mod.save(wd, st)

    cobertura = _verify_coverage(wd, artifacts)
    cobertura["artefato_atual"] = key
    # `mark` faz seu próprio load/save sob trava e é a única via pública que
    # grava status/artifact do stage — chamado DEPOIS do merge do mapa.
    st_mod.mark(wd, "verify", cobertura["status"], out or a.artifact)

    payload = dict(report)
    payload["cobertura"] = cobertura
    payload["stage_status"] = cobertura["status"]
    payload["acao"] = _verify_next_action(wd, cobertura)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _quiet_condense(payload: dict) -> dict:
    """Reduz um payload JSON a campos escalares + contagens de listas/dicts.

    Existe para que `--quiet` nunca reimprima o corpo inteiro de um comando
    (plan/run-stage/state/audit/export despejavam JSON completo repetidas
    vezes no orquestrador, custando contexto). Listas viram `<chave>_count` e
    dicts aninhados viram `<chave>_keys`; escalares passam direto.
    """
    out: dict = {}
    for key, value in payload.items():
        if key == "error":
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            out[f"{key}_keys"] = len(value)
    return out


def _quiet_extra_fields(cmd: str, payload: dict) -> dict:
    """Campos essenciais que a condensação genérica perderia, por comando —
    o que o resumo de uma linha precisa carregar (ex.: `audit` precisa do
    total de blockers, não só da contagem de stages)."""
    extra: dict = {}
    if cmd == "audit":
        stages = payload.get("stages") or []
        total = 0
        for stage_report in stages:
            total += len(stage_report.get("blockers") or [])
            for art in stage_report.get("artifacts") or []:
                total += len(art.get("blockers") or [])
        extra["blockers_count"] = total
    elif cmd == "state":
        stages = payload.get("stages") or {}
        extra["estagio_atual"] = st_mod.next_stage({"stages": stages})
    return extra


def _quiet_summary(cmd: str, code: int, out_text: str, err_text: str) -> dict:
    raw = out_text.strip() or err_text.strip()
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
    summary: dict = {"ok": code == 0, "cmd": cmd}
    if isinstance(payload, dict):
        if isinstance(payload.get("error"), str):
            summary["error"] = payload["error"][:300]
        summary.update(_quiet_condense(payload))
        summary.update(_quiet_extra_fields(cmd, payload))
    elif raw:
        summary["output_bytes"] = len(raw)
    return summary


def _run_quiet(a) -> int:
    """Executa `a.fn(a)` capturando toda a saída. `--quiet` é opt-in (o corpo
    completo é o padrão do CLI): o STDOUT vira uma única linha JSON de
    resultado, para os casos em que o orquestrador só precisa do resumo.

    O STDERR só é repassado quando carrega algo que o resumo não capturou
    (ex.: saída que não era JSON). Quando o comando já falhou com um payload
    `{"error": ...}` em JSON, esse erro já está condensado no resumo de uma
    linha (`ok`, `error`, `acao`, ...) — reemitir o STDERR bruto duplicaria a
    mesma mensagem de erro em dois formatos diferentes."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        code = a.fn(a)
    err_text = err_buf.getvalue()
    summary = _quiet_summary(getattr(a, "cmd", "?"), code, out_buf.getvalue(), err_text)
    if err_text and "error" not in summary:
        sys.stderr.write(err_text)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return code


_TOP_LEVEL_FLAGS_WITH_VALUE = ("--store", "--repo")
_TOP_LEVEL_FLAGS_BOOL = ("--quiet", "--verbose")


def _flag_reorder_hint(argv: list[str], subcommands: set[str]) -> dict | None:
    """Detecta flag de subcomando (ex.: `--topic`) escrita antes do nome do
    subcomando — `--repo X --topic Y surface` produz hoje `invalid choice: 'Y'`
    do argparse, que não diz o que está errado. Devolve None quando a ordem
    já está correta (ou quando nenhum subcomando reconhecido aparece)."""
    leftover_idx: list[int] = []
    subcommand_index: int | None = None
    subcommand_name: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _TOP_LEVEL_FLAGS_WITH_VALUE and i + 1 < len(argv):
            i += 2
            continue
        if any(tok.startswith(f"{f}=") for f in _TOP_LEVEL_FLAGS_WITH_VALUE):
            i += 1
            continue
        if tok in _TOP_LEVEL_FLAGS_BOOL:
            i += 1
            continue
        if subcommand_index is None and tok in subcommands:
            subcommand_index = i
            subcommand_name = tok
            i += 1
            continue
        if subcommand_index is None:
            leftover_idx.append(i)
        i += 1
    if subcommand_index is None or not leftover_idx:
        return None
    moved = [argv[idx] for idx in leftover_idx]
    kept = [tok for idx, tok in enumerate(argv) if idx not in leftover_idx]
    corrected: list[str] = []
    inserted = False
    for tok in kept:
        corrected.append(tok)
        if not inserted and tok == subcommand_name:
            corrected.extend(moved)
            inserted = True
    flags = sorted({tok for tok in moved if tok.startswith("--")})
    return {"flags": flags, "subcommand": subcommand_name, "corrected": corrected}


# FIX 2 (lote A): `evidence` é um estágio válido em `sdd-brief` (via
# `sdd_mod.BRIEF_STAGES = ("evidence",) + CRITICAL_STAGES`) mas NÃO nos quatro
# subcomandos abaixo, que só operam sobre `sdd_mod.CRITICAL_STAGES` — evidence
# é 100% determinístico (busca lexical, sem parser/AST) e não tem fan-out por
# subagente, então não faz sentido em run-stage/merge-agent-output/agent-pack/
# redo. Sem esta checagem, `argparse` só devolve `invalid choice: 'evidence'`,
# sem indicar que o comando certo é `evidence --topic <topico>`.
_STAGE_CMD_VALUE_FLAGS = {
    "run-stage": ("--batches", "--max-bytes", "--max-files", "--max-lines"),
    "merge-agent-output": ("--input", "--agent"),
    "agent-pack": ("--batch", "--batches", "--max-bytes", "--max-files", "--max-lines", "--output"),
    "redo": ("--item",),
}


def _evidence_stage_hint(argv: list[str]) -> dict | None:
    """Detecta `evidence` usado como `stage` posicional de run-stage/
    merge-agent-output/agent-pack/redo e devolve um erro acionável. Devolve
    None quando não se aplica (deixa o argparse validar normalmente)."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _TOP_LEVEL_FLAGS_WITH_VALUE and i + 1 < len(argv):
            i += 2
            continue
        if tok in _TOP_LEVEL_FLAGS_BOOL:
            i += 1
            continue
        if tok in _STAGE_CMD_VALUE_FLAGS:
            value_flags = _STAGE_CMD_VALUE_FLAGS[tok]
            j = i + 1
            while j < len(argv):
                t = argv[j]
                if t in value_flags and j + 1 < len(argv):
                    j += 2
                    continue
                if t in _TOP_LEVEL_FLAGS_BOOL:
                    j += 1
                    continue
                if t == "evidence":
                    return {
                        "error": (
                            f"'evidence' não é um estágio válido para '{tok}' "
                            f"(aceita apenas: {', '.join(sdd_mod.CRITICAL_STAGES)})"
                        ),
                        "acao": (
                            "evidence é 100% determinístico e não tem fan-out por subagente; "
                            "rode `wk code evidence --topic <topico>` diretamente"
                        ),
                    }
                break  # primeiro token não reconhecido como flag = o `stage` real
            i += 1
            continue
        i += 1
    return None


class _CliArgError(Exception):
    """Carrega a mensagem crua do argparse + o `prog` do parser que a gerou,
    para que `main()` converta em JSON acionável em vez de deixar o argparse
    imprimir usage cru e chamar sys.exit direto (ver `_JsonArgumentParser`)."""

    def __init__(self, message: str, prog: str) -> None:
        super().__init__(message)
        self.message = message
        self.prog = prog


class _JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser que nunca deixa escapar o dump cru de usage+error do
    argparse. Qualquer erro de parsing (flag desconhecida em posição errada,
    escolha inválida, argumento obrigatório ausente etc.) vira `_CliArgError`,
    capturada em `main()` e reemitida como JSON com `error`/`acao`.

    `add_subparsers()` propaga esta classe para todo subparser (usa
    `type(self)` como default de `parser_class`), então o mesmo contrato vale
    tanto para erros no nível superior quanto dentro de qualquer subcomando."""

    def error(self, message: str) -> None:
        raise _CliArgError(message, self.prog)


def _prog_to_wk(prog: str) -> str:
    if prog.startswith("codescan"):
        return "wk code" + prog[len("codescan"):]
    return prog


def _argparse_error_payload(message: str, prog: str, argv_list: list[str] | None = None) -> dict:
    wk_prog = _prog_to_wk(prog)
    payload = {"error": f"argumentos inválidos: {message}"}
    if message.startswith("unrecognized arguments:") and argv_list is not None:
        extra_tokens = message.split(":", 1)[1].strip().split()
        remaining = list(extra_tokens)
        corrected: list[str] = []
        for tok in argv_list:
            if tok in remaining:
                remaining.remove(tok)
                continue
            corrected.append(tok)
        payload["acao"] = (
            f"argumento(s) não reconhecido(s) removido(s) ({' '.join(extra_tokens)}); "
            f"rode: wk code {' '.join(corrected)}"
        )
    else:
        payload["acao"] = f"rode `{wk_prog} -h` para ver os argumentos aceitos"
    return payload


def main(argv=None) -> int:
    argv_list = list(sys.argv[1:]) if argv is None else list(argv)
    p = _JsonArgumentParser(prog="codescan", description="Pipeline de codebase")
    p.add_argument("--store", default=None, help="raiz do store (ou defina WK_STORE)")
    p.add_argument("--repo", required=True, help="raiz local OU URL do repositório (READ-ONLY)")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="opt-in: condensa a saída a uma única linha JSON de resumo (o corpo completo é o padrão)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="mantido por compatibilidade; o corpo completo já é o padrão (no-op explícito)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    cl = sub.add_parser("cleanup", help="apaga o clone de um repo remoto (após o término)")
    cl.set_defaults(fn=cmd_cleanup)

    s = sub.add_parser("surface", help="estágio 1: mapa determinístico")
    s.add_argument("--topic", help="tópico do wiki-ai a que este repo pertence")
    s.add_argument("--module-min-files", type=int, default=3)
    s.add_argument("--since", help="janela p/ churn (ex.: '12 months ago')")
    s.set_defaults(fn=cmd_surface)

    ex = sub.add_parser("export", help="artefatos SDD determinísticos: inventory, dependencies, coupling")
    ex.add_argument("--topic", help="tópico do wiki-ai (vai para o frontmatter)")
    ex.add_argument("--output", help="pasta de saída (default: workdir/sdd)")
    ex.set_defaults(fn=cmd_export)

    pl = sub.add_parser("plan", help="lista módulos a cavar, por LOC (main primeiro)")
    pl.add_argument("--top", type=int, help="só os N maiores")
    pl.add_argument("--batches", type=int, help="OVERRIDE do N automático (raro; limitado a nº de módulos)")
    pl.add_argument("--agent-pack-max-bytes", type=int, default=agentpack_mod.DEFAULT_MAX_BYTES)
    pl.add_argument("--include-tests", action="store_true", help="inclui módulos de teste no alvo")
    pl.set_defaults(fn=cmd_plan)

    pd = sub.add_parser("pending", help="registra pendências de um estágio")
    pd.add_argument("stage", choices=st_mod.STAGES)
    pd.add_argument("--items", required=True, help="lista separada por vírgula")
    pd.set_defaults(fn=cmd_pending)

    cf = sub.add_parser("config", help="persiste decisões do SDD no estado")
    cf.add_argument("--doc-level", choices=("essencial", "completo", "detalhado"))
    cf.add_argument(
        "--granularity",
        choices=("module", "endpoint", "use-case", "hybrid", "feature", "custom"),
    )
    cf.set_defaults(fn=cmd_config)

    n = sub.add_parser("next", help="o que fazer agora")
    n.set_defaults(fn=cmd_next)

    d = sub.add_parser("done", help="marca concluído")
    d.add_argument("stage", choices=st_mod.STAGES)
    d.add_argument("--item")
    d.add_argument("--artifact")
    d.set_defaults(fn=cmd_done)

    for status in ("blocked", "failed", "degraded"):
        ps = sub.add_parser(status, help=f"marca {status}")
        ps.add_argument("stage", choices=st_mod.STAGES)
        ps.add_argument("--item")
        ps.add_argument("--artifact")
        ps.set_defaults(fn=cmd_problem_status, status=status)

    stt = sub.add_parser("state", help="estado completo")
    stt.set_defaults(fn=cmd_state)

    r = sub.add_parser("read", help="lê arquivo do repo, numerado")
    r.add_argument("path")
    r.add_argument("--from", dest="line_from", type=int, default=0)
    r.add_argument("--count", type=int, default=0)
    r.set_defaults(fn=cmd_read)

    ev = sub.add_parser("evidence", help="gera evidence-pack por tópico")
    ev.add_argument("--topic", help="tópico/slice lexical")
    ev.add_argument("--top", type=int, default=20, help="máximo de arquivos candidatos")
    ev.add_argument("--context", type=int, default=ev_mod.DEFAULT_CONTEXT, help="linhas de contexto por match")
    ev.add_argument("--max-lines", type=int, default=ev_mod.DEFAULT_MAX_LINES, help="máximo de linhas por evidência")
    ev.add_argument("--output", help="caminho do JSON (default: workdir/evidence-<topic>.json)")
    ev.set_defaults(fn=cmd_evidence)

    ap = sub.add_parser("agent-pack", help="gera pacote deterministico para subagente sem copiar repo")
    ap.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    ap.add_argument("--batch", type=int, required=True, help="batch 1..N emitido por plan")
    ap.add_argument("--batches", type=int, help="mesmo override usado no plan")
    ap.add_argument("--max-bytes", type=int, default=agentpack_mod.DEFAULT_MAX_BYTES, help="bytes máximos por pack")
    ap.add_argument("--max-files", type=int, default=agentpack_mod.DEFAULT_MAX_FILES_PER_MODULE, help="arquivos por modulo no pacote")
    ap.add_argument("--max-lines", type=int, default=agentpack_mod.DEFAULT_MAX_LINES_PER_FILE, help="linhas por arquivo no pacote")
    ap.add_argument("--output", help="caminho do JSON (default: workdir/agent-packs)")
    ap.set_defaults(fn=cmd_agent_pack)

    mo = sub.add_parser("merge-agent-output", help="integra saída parseável de subagente")
    mo.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    mo.add_argument("--input", required=True, help="arquivo de resposta do subagente")
    mo.add_argument("--agent", help="identificador do subagente que gerou o input")
    mo.set_defaults(fn=cmd_merge_agent_output)

    rd = sub.add_parser(
        "redo",
        help="reabre stage/item: marca runs current como superseded (preserva trilha) e devolve done->pending",
    )
    rd.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    rd.add_argument("--item", help="reabre só este item; sem --item, reabre todos os itens current do stage")
    rd.set_defaults(fn=cmd_redo)

    rs = sub.add_parser("run-stage", help="prepara manifesto determinístico para subagentes")
    rs.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    rs.add_argument("--batches", type=int, help="override de batches para modules")
    rs.add_argument("--max-bytes", type=int, default=agentpack_mod.DEFAULT_MAX_BYTES)
    rs.add_argument("--max-files", type=int, default=agentpack_mod.DEFAULT_MAX_FILES_PER_MODULE)
    rs.add_argument("--max-lines", type=int, default=agentpack_mod.DEFAULT_MAX_LINES_PER_FILE)
    rs.set_defaults(fn=cmd_run_stage)

    ho = sub.add_parser("handoff", help="imprime o prompt de despacho do fan-out, pronto para colar na LLM")
    ho.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    ho.set_defaults(fn=cmd_handoff)

    au = sub.add_parser("audit", help="mede qualidade SDD sem alterar estado")
    au.add_argument("stage_pos", nargs="?", choices=sdd_mod.CRITICAL_STAGES)
    au.add_argument("--stage", choices=sdd_mod.CRITICAL_STAGES)
    au.add_argument("--strict", action="store_true", help="mantido por compatibilidade; gates P0 já são padrão")
    au.set_defaults(fn=cmd_audit)

    sb = sub.add_parser("sdd-brief", help="emite contrato compacto SDD para um estágio")
    sb.add_argument("stage", choices=sdd_mod.BRIEF_STAGES)
    sb.set_defaults(fn=cmd_sdd_brief)

    ss = sub.add_parser("sdd-scaffold", help="cria esqueleto dos artefatos SDD do estágio")
    ss.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    ss.set_defaults(fn=cmd_sdd_scaffold)

    vf = sub.add_parser("verify", help="valida Markdown confirmado contra arquivo:linha")
    vf.add_argument("--artifact", required=True, help="Markdown a verificar")
    vf.add_argument("--output", help="salva relatório JSON")
    vf.set_defaults(fn=cmd_verify)

    # `--quiet`/`--verbose` precisam funcionar em QUALQUER posição (antes ou
    # depois do subcomando) com efeito idêntico. Sem isto, algo como
    # `run-stage modules --verbose` batia em "unrecognized arguments" porque
    # só o parser de nível superior conhecia essas flags — causa raiz de um
    # agente real ter ido ler o código-fonte do produto para se orientar.
    #
    # default=SUPPRESS é essencial aqui, não cosmético: `_SubParsersAction`
    # parseia o subcomando com um namespace NOVO e depois copia todos os seus
    # `vars()` por cima do namespace externo — com default=False (comum) isso
    # zeraria de volta um `--quiet`/`--verbose` já setado antes do subcomando,
    # sempre que o usuário não repetir a flag depois dele. Com SUPPRESS, a
    # dest só entra no namespace do subparser quando a flag for realmente
    # digitada ali, então o valor herdado do nível superior sobrevive.
    for _sp in sub.choices.values():
        _sp.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        _sp.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    if not any(tok in ("-h", "--help") for tok in argv_list):
        evidence_hint = _evidence_stage_hint(argv_list)
        if evidence_hint:
            print(json.dumps(evidence_hint, ensure_ascii=False), file=sys.stderr)
            return 2
        hint = _flag_reorder_hint(argv_list, set(sub.choices.keys()))
        if hint:
            corrected_cmd = "wk code " + " ".join(hint["corrected"])
            print(
                json.dumps(
                    {
                        "error": (
                            f"flag(s) {', '.join(hint['flags'])} pertence(m) ao subcomando "
                            f"'{hint['subcommand']}', não ao nível superior (antes do subcomando)"
                        ),
                        "acao": f"rode: {corrected_cmd}",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2

    try:
        a = p.parse_args(argv_list)
    except _CliArgError as e:
        payload = _argparse_error_payload(e.message, e.prog, argv_list)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2

    store_value = a.store or os.environ.get("WK_STORE")
    if not store_value:
        print(
            json.dumps(
                {
                    "error": "store não informado: sem --store e sem WK_STORE, nada seria gravado no lugar certo",
                    "acao": "informe --store <caminho> OU defina a env var WK_STORE=<caminho>",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    # FIX 1 (lote A): `--store` relativo deixava `wd` (workdir) relativo, e todo
    # caminho gravado em agent-runs/<stage>.json a partir de `wd` (ex.: output de
    # run-stage, artifact de merge-agent-output) também ficava relativo. Ao ler de
    # volta, sdd._resolve_manifest_path() só reconhece um valor como "já absoluto"
    # via os.path.isabs(); um valor relativo é tratado como relativo-a-wd e
    # rejuntado com wd — duplicando o prefixo (ex.:
    # "store/.codescan/<h>/store/.codescan/<h>/modules/<slug>.md"), path
    # inexistente. Tornar `a.store` sempre absoluto aqui (única linha que
    # resolve o argumento antes do dispatch) faz `wd` ser sempre absoluto, então
    # todo caminho derivado de `os.path.join(wd, ...)` já nasce absoluto e o
    # isabs() de _resolve_manifest_path para de reinterpretar como relativo.
    # `--repo` NÃO tem o mesmo problema: `_ensure_repo`/`state.workdir` já
    # aplicam `os.path.abspath` internamente antes de usar o valor para
    # resolver caminho ou montar a chave de hash do workdir; `a.repo` só
    # aparece cru como rótulo (`repo_label`) ou texto de mensagem, nunca é
    # rejuntado como se fosse relativo a outra base. Não alteramos `a.repo`
    # aqui para não arriscar mudar o rótulo exibido em saídas legíveis.
    a.store = os.path.abspath(store_value)

    # Corpo completo é o padrão do `wk code` (--verbose é aceito só como
    # no-op de compatibilidade). `--quiet` é opt-in explícito para o resumo
    # de uma linha; funciona em qualquer posição (flags injetadas em todo
    # subparser acima) com efeito idêntico ao da flag global.
    if a.quiet:
        return _run_quiet(a)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
