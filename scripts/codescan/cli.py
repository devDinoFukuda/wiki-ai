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
  run       composto: run-stage + handoff (prepara o fan-out e entrega o prompt)
  integrate composto: merge de todos os batches do manifesto + done do estágio
  auto      laço: encadeia as ações determinísticas até a próxima parada real
  pilot     imprime o prompt-mestre do modo piloto (a LLM roda o laço sozinha)
  verify    valida Markdown confirmado contra citações arquivo:linha
  drift     compara o commit pinado no surface com o HEAD atual do repo

`next --run` executa a próxima ação quando ela é determinística; decisão
humana (config/pending) e passo de LLM continuam pedindo o comando explícito.

`auto` vai além: encadeia TODAS as ações determinísticas (surface, export,
config/pending quando as decisões vêm nas flags, plan, run, integrate,
evidence, done) e só devolve o controle ao humano em três situações —
decisão-chave (`decisao_humana`), colar o prompt na LLM (`fanout:<stage>`) e
loop de erro (`intervencao`, mesma falha 2x seguidas na mesma etapa).
Todo payload de `next`/`state`/`run`/`integrate`/`done` carrega `progresso`.

`pilot` fecha a última lacuna: emite o prompt-mestre que faz a própria LLM
rodar o laço do `auto`, despachar os subagentes de cada `fanout:<stage>`,
integrar e reexecutar — de modo que o humano só reapareça em `decisao_humana`
ou em falha. É só texto: nada é executado nem escrito por `pilot`.

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
from . import surface as surface_mod
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


def _next_payload(a) -> dict:
    """Payload puro do `next` (mesmos campos de sempre, sem imprimir).

    Extraído de `cmd_next` para que os compostos (`next --run`) possam decidir
    a partir do MESMO cálculo de `acao` que o humano vê — sem uma segunda
    implementação da máquina de estados que pudesse divergir dos gates.
    """
    wd = _wd(a)
    st = st_mod.load(wd)
    if not st:
        return {"proximo": "surface", "motivo": "nada iniciado"}
    if _surface_done(st):
        missing = _missing_sdd_config(st)
        if missing:
            return {
                "proximo": "config",
                "status": "pending",
                "motivo": "config SDD obrigatória antes de plan/run-stage modules",
                "missing": missing,
                "acao": SDD_CONFIG_ACTION,
            }
    stage = None
    for candidate in st_mod.STAGES:
        s_candidate = st.get("stages", {}).get(candidate, {})
        if s_candidate.get("last_error") and s_candidate.get("status") != "done":
            stage = candidate
            break
    if stage is None:
        stage = st_mod.next_stage(st)
    if stage is None:
        return {"proximo": None, "motivo": "pipeline completo"}
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
        return out
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
    return out


def cmd_next(a) -> int:
    wd = _wd(a)
    payload = _next_payload(a)
    payload["progresso"] = _progresso(wd, st_mod.load(wd), payload.get("proximo"))
    if getattr(a, "run", False):
        return _next_run(a, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
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


def _cmd_done_verified_stage(wd: str, a) -> int:
    """`done` para `verify`/`evidence` (F01, W0): os dois só têm `status`
    fechado por um vínculo de verificação real (`state.record_verification`,
    gravado por `cmd_evidence`/`cmd_verify`), nunca por mutação direta de
    `done`. Isola este caminho de `cmd_done` porque nenhum dos dois usa
    `--item`, `merge-agent-output` ou o gate genérico de blockers — a
    validação é 100% o hash de `state.verification_ok`.
    """
    if a.item:
        _err(f"done {a.stage} não aceita --item: {a.stage} não tem itens")
        return 2
    if a.stage == "verify":
        # `verify` nunca fecha por `done`: o status é DERIVADO da cobertura
        # dos artefatos obrigatórios (`_verify_coverage`, computado só por
        # `cmd_verify`) — permitir `done verify` aqui destrancaria o estágio
        # inteiro com uma única citação válida, exatamente o bug que F-02 já
        # fechou para `verify --artifact`. Sem exceção: mesmo com um
        # `--artifact` já verificado com sucesso, a cobertura pode faltar
        # outro artefato obrigatório (ex.: inferred.md) — só `cmd_verify`
        # sabe recalcular isso.
        error = (
            "done verify não é permitido: o status de verify é derivado da "
            "cobertura de artefatos obrigatórios, não de um done manual"
        )
        st_mod.record_stage_error(wd, "verify", error, a.artifact, [])
        _err(error, acao="rode `verify --artifact <sdd/confirmed.md>` (e inferred.md, se existir) até a cobertura fechar sozinha")
        return 2
    # stage == "evidence": único artefato; `done` só reafirma um resultado já
    # produzido por `cmd_evidence` E ainda íntegro (hash bate com o disco).
    ok, reason = st_mod.verification_ok(wd, "evidence")
    if not ok:
        error = f"done evidence recusado: {reason}"
        st_mod.record_stage_error(wd, "evidence", error, a.artifact, [])
        _err(error, acao="rode `evidence --topic <topico>` — ela mesma fecha o estágio ao terminar, sem precisar de done depois")
        return 2
    st = st_mod.mark(wd, "evidence", "done", a.artifact or None)
    st = st_mod.clear_stage_error(wd, "evidence")
    out = dict(st["stages"]["evidence"])
    out["progresso"] = _progresso(wd, st, "evidence")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_done(a) -> int:
    wd = _wd(a)
    # F01 (W0): `evidence`/`verify` não têm itens nem gate de auditoria
    # (sdd.CRITICAL_STAGES) — sem este bloco, `done <stage>` genérico abaixo
    # não tinha NENHUM blocker próprio para os dois, e um `--artifact`
    # qualquer (ou nenhum) fechava o estágio sem que a execução real
    # (`evidence`/`verify --artifact`) tivesse rodado. Tratado ANTES de
    # qualquer outra coisa (inclusive `--item`, que estes estágios não usam).
    if a.stage in st_mod.VERIFIED_STAGES:
        return _cmd_done_verified_stage(wd, a)
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
    # `progresso` só no payload IMPRESSO: cópia rasa para não vazar o campo
    # derivado para dentro de state.json em um save posterior.
    out = dict(st["stages"][a.stage])
    out["progresso"] = _progresso(wd, st, a.stage)
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
    out = dict(st)
    out["progresso"] = _progresso(wd, st)
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
    # F01 (W0): grava o hash do pacote recém-escrito ANTES de marcar `done` —
    # é este registro que `done evidence` (cmd_done) passa a exigir para
    # aceitar reafirmar o estágio, e que fica inválido sozinho (ver
    # `state.revalidate_verified_stages`) se o artefato for editado depois.
    st_mod.record_verification(
        wd, "evidence", "evidence", artifact,
        scope={"topic": topic, "commit": pack.get("commit")},
    )
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


PROGRESSO_FASES = ("preparacao", "fanout", "fechamento")


def _stage_phase(wd: str, stage: str, st: dict) -> str:
    """Fase do estágio dentro do ciclo `preparar -> fan-out -> fechar`.

    Determinística e derivada só de disco + state.json: `preparacao` enquanto
    não existe manifesto de fan-out (`agent-runs/<stage>-plan.json`), `fanout`
    entre o `run-stage` e o fechamento dos itens, `fechamento` quando os itens
    já estão completos (falta só o `done`) ou o estágio fechou.
    """
    s = (st.get("stages") or {}).get(stage) or {}
    if s.get("status") == "done" or s.get("items_complete"):
        return "fechamento"
    if os.path.isfile(_plan_manifest_path(wd, stage)):
        return "fanout"
    return "preparacao"


def _progresso(wd: str, st: dict | None, stage: str | None = None) -> str:
    """Campo `progresso`: `"etapa <i> de <total> — fase <...>"`.

    Um único escalar que responde "onde eu estou" sem que o orquestrador
    precise gastar uma invocação (e o corpo inteiro do `state`) só para
    descobrir isso — é o campo que aparece em `next`, `state`, `run`,
    `integrate` e `done`. Escalar de propósito: sobrevive intacto ao
    `--quiet` (`_quiet_condense` só colapsa listas/dicts).

    `stage` fora de `state.STAGES` (ex.: o pseudo-passo `config` do `next`)
    cai no estágio real corrente; pipeline completo devolve a última etapa.
    """
    total = len(st_mod.STAGES)
    if not st:
        return f"etapa 1 de {total} — fase preparacao"
    if stage not in st_mod.STAGES:
        stage = st_mod.next_stage(st)
    if stage is None:
        return f"etapa {total} de {total} — fase fechamento"
    return (
        f"etapa {st_mod.STAGES.index(stage) + 1} de {total} "
        f"— fase {_stage_phase(wd, stage, st)}"
    )


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


def _redo_command(a, stage: str, item: str) -> str:
    """Comando `redo` LITERAL, com os valores reais deste contexto (F-08).

    A mensagem antiga (`wk code ... redo <stage> --item <item>`) tinha três
    placeholders: o operador/agente tinha que adivinhar o binário, o `--repo`,
    o `--store` E o item. Na prática ninguém rodava o `redo` — o merge era
    refeito por cima, o run antigo continuava `current` com o sha velho e a
    auditoria travava em `P0: artefato alterado após o merge` para sempre.
    Aqui o comando sai pronto para colar."""
    return (
        f'$WKPY "$WK" code --repo "{a.repo}" --store "{a.store}" '
        f'redo {stage} --item "{item}"'
    )


MAX_REPORTED_REDO_CONFLICTS = 10


def _merge_redo_conflict_error(a, wd: str, stage: str, input_path: str) -> dict | None:
    """Recusa re-merge de item(ns) que já têm um run `current` registrado
    (BQ4 ponto 5): sem isto, reescrever a resposta do subagente e rodar
    `merge-agent-output` de novo invalidava silenciosamente a trilha anterior
    em vez de passar pelo caminho suportado (`redo`).

    F-08: o bloqueio já existia, mas a mensagem não ENSINAVA a saída — o
    resultado prático era o operador re-mergear por outro caminho e deixar o
    run antigo `current` apontando para um sha que não existe mais
    (`P0: artefato alterado após o merge`, permanente). Agora o payload traz,
    para cada item em conflito, o run id e o agent que o cobrem, e um `acao`
    com o(s) comando(s) `redo` literais, um por item, prontos para colar.

    Devolve o payload de erro (dict pronto para `json.dumps`) ou `None`."""
    try:
        with open(input_path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    items = _merge_input_items(stage, text)
    if not items:
        return None
    owners = agentmerge_mod.current_run_owners(wd, stage)
    conflicts = sorted({item for item in items if item in owners})
    if not conflicts:
        return None
    shown = conflicts[:MAX_REPORTED_REDO_CONFLICTS]
    detalhes = [
        {
            "item": item,
            "run": owners[item].get("run"),
            "agent": owners[item].get("agent"),
        }
        for item in conflicts
    ]
    listagem = "; ".join(
        f"`{d['item']}` (run #{d['run']}, agent {d['agent'] or 'não informado'})"
        for d in detalhes[:len(shown)]
    )
    if len(conflicts) > len(shown):
        listagem += f"; ... +{len(conflicts) - len(shown)} outro(s) (total {len(conflicts)})"
    comandos = [_redo_command(a, stage, item) for item in shown]
    acao = (
        "rode o(s) redo abaixo (um por item em conflito) e só então refaça o merge: "
        + " ; ".join(comandos)
    )
    if len(conflicts) > len(shown):
        acao += (
            f" ; (+{len(conflicts) - len(shown)} item(ns) em conflito não listado(s): "
            "repita o mesmo comando trocando --item)"
        )
    return {
        "error": (
            f"item já mergeado: re-merge sem `redo` deixaria o run anterior `current` com "
            f"sha desatualizado (P0: artefato alterado após o merge). "
            f"Itens em conflito: {listagem}"
        ),
        "acao": acao,
        "itens_em_conflito": detalhes,
        "comandos_redo": comandos,
    }


def _merge_error_payload(a, e: Exception) -> dict:
    """Payload de um `MergeError`. Usa o `acao` estruturado da exceção quando
    existe (ruído do lote D, F-24) e, no caso F-24, promove a ação genérica
    (`redo <stage> --item ...`) para o comando literal com --repo/--store
    reais — mesma cura do F-08, aplicada ao conflito de dono de artefato."""
    payload: dict = {"error": str(e)}
    violacoes = list(getattr(e, "violacoes", None) or [])
    donos: list[str] = []
    for v in violacoes:
        dono = v.get("dono_item") if isinstance(v, dict) else None
        if v.get("tipo") == "artefato_de_outro_batch" and dono and dono not in donos:
            donos.append(dono)
    if donos:
        comandos = [_redo_command(a, a.stage, item) for item in donos]
        payload["acao"] = (
            "supere o run dono antes de re-mergear: " + " ; ".join(comandos)
            + " — OU corrija o output deste batch para não reivindicar o artefato de outro batch"
        )
        payload["comandos_redo"] = comandos
    else:
        payload["acao"] = getattr(e, "acao", None) or _sdd_brief_hint(a.stage)
    if violacoes:
        payload["violacoes"] = violacoes
        payload["violacoes_total"] = getattr(e, "violacoes_total", len(violacoes))
    return payload


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
    redo_conflict = _merge_redo_conflict_error(a, wd, a.stage, a.input)
    if redo_conflict:
        print(json.dumps(redo_conflict, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        result = agentmerge_mod.merge_agent_output(wd, a.stage, a.input, agent=a.agent)
    except agentmerge_mod.MergeError as e:
        print(json.dumps(_merge_error_payload(a, e), ensure_ascii=False), file=sys.stderr)
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


# Exigência de citação, LITERAL e com exemplo dos dois lados (válido/inválido).
# O texto antigo ("🟢 confirmado (exige arquivo:linha)") era ambíguo: o
# subagente lia "arquivo" como o nome do arquivo e escrevia `DomainEvent.java:5`
# — basename que o gate de `verify`/`audit` reprova, porque não resolve para um
# caminho real do repo. A regra canônica já está em
# `sdd.COMPACT_AGENT_RULES`/`sdd` (`verde_exige_caminho_relativo_completo_da_raiz`);
# aqui ela aparece na MESMA forma, no único texto que a LLM despachante lê.
HANDOFF_CITACAO_REGRA = (
    "CITAÇÃO: 🟢 confirmado exige citação com caminho relativo COMPLETO a partir da raiz do "
    "repo, com /, exatamente como em evidence[].path do pack, seguido de :linha. "
    "Exemplo VÁLIDO: "
    "quote-service/src/main/java/br/com/acme/insurance/quote/domain/event/DomainEvent.java:5 "
    "— INVÁLIDO: DomainEvent.java:5 (basename é reprovado no gate)."
)

# Falha real observada em execução: subagentes do fan-out tiveram `write`
# negado pela engine e devolveram o conteúdo na resposta; o despachante não
# tinha instrução de fallback e o fluxo morreu com 0 outputs gravados. Este
# parágrafo é o remédio — aparece uma vez no corpo do prompt do despachante e
# fecha a cadeia: subagente sem permissão devolve o conteúdo, despachante
# grava; se o despachante também não tiver permissão, nada é descartado.
HANDOFF_FALLBACK_ESCRITA = (
    "FALLBACK DE ESCRITA: Se um subagente não conseguir gravar o arquivo (permissão negada), "
    "ele deve devolver o conteúdo COMPLETO na resposta; nesse caso VOCÊ (despachante) grava o "
    "conteúdo, byte a byte, no caminho ESCREVA correspondente. Se você também não tiver "
    "permissão de escrita: NÃO descarte o conteúdo — grave cada um em "
    "./wk-agent-outputs/<mesmo-nome>.txt no diretório atual da sessão e informe o operador para "
    "movê-los (comando `move`/`cp` literal por arquivo, origem→destino)."
)


def _handoff_prompt(stage: str, batches: list[dict], contract_path: str) -> str:
    """Prompt de despacho pronto para colar na LLM: despachante dispara um
    subagente por batch; cada subagente lê 2 arquivos e grava 1."""
    lines = [
        f"Fan-out do estágio {stage}. Você é despachante: NÃO analise o repositório você mesmo,",
        "NÃO gere conteúdo você mesmo, NÃO execute comandos, NÃO faça merge.",
        "",
        HANDOFF_CITACAO_REGRA,
        "",
        HANDOFF_FALLBACK_ESCRITA,
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
        "  MARCADORES: 🟢 confirmado · 🟡 inferido (justificativa) · 🔴 desconhecido (pergunta objetiva).",
        f"  {HANDOFF_CITACAO_REGRA}",
        "  Se a escrita falhar, devolva o conteúdo completo entre os marcadores === ... === no "
        "corpo da resposta (o despachante grava).",
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


# ---------------------------------------------------------------------------
# Compostos (`run`, `integrate`, `next --run`).
#
# FLUXO 3 gastava ~40 invocações porque cada passo determinístico da máquina de
# estados era um `wk code ...` separado (run-stage, handoff, N merges, done,
# next entre cada um). Os compostos abaixo NÃO reimplementam nem afrouxam nada:
# chamam exatamente a mesma `fn` que o argparse chamaria, com o mesmo namespace,
# e só reempacotam a saída. Todo gate (freshness do input, --agent obrigatório,
# conflito de re-merge, artefato de outro batch, ruído, blockers do done, ...)
# continua valendo porque é literalmente o mesmo código executando.
# ---------------------------------------------------------------------------


def _capture(fn, args) -> tuple[int, str, str]:
    """Roda um subcomando existente capturando stdout/stderr (mesmo mecanismo
    do `--quiet`; redirects aninham sem problema)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        code = fn(args)
    return code, out_buf.getvalue(), err_buf.getvalue()


def _split_json_prefix(text: str) -> tuple[dict | None, str]:
    """Separa o payload JSON do resto da saída.

    `run` imprime uma linha JSON seguida do prompt de despacho em texto cru —
    o composto precisa recuperar o dict sem perder o prompt (que é o produto
    entregue ao humano). Devolve `(payload_ou_None, cauda_em_texto)`.
    """
    raw = text.strip("\n")
    if not raw.strip():
        return None, ""
    try:
        value = json.loads(raw)
        return (value if isinstance(value, dict) else None), ""
    except ValueError:
        pass
    lines = raw.split("\n")
    try:
        value = json.loads(lines[0])
    except ValueError:
        return None, text
    tail = "\n".join(lines[1:]).strip("\n")
    return (value if isinstance(value, dict) else None), tail


def _derived_args(a, **overrides):
    """Namespace irmão do atual com os defaults do subcomando alvo aplicados.

    Os compostos não passam pelo argparse do subcomando chamado, então os
    defaults que aquele subparser aplicaria precisam ser explicitados aqui.
    """
    ns = argparse.Namespace(**vars(a))
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def cmd_run(a) -> int:
    """`run <stage>` = `run-stage <stage>` + `handoff <stage>`.

    Um comando prepara o fan-out E entrega o prompt de despacho. Falha do
    `run-stage` aborta antes do handoff e propaga a saída original intacta
    (mesmo `error`/`acao`, mesmo exit code).

    STDOUT: uma linha JSON (manifesto/batches/`progresso`/`acao`) e, abaixo
    dela, o prompt cru pronto para colar.
    """
    stage = a.stage
    args = _derived_args(a, stage=stage)
    code, out_text, err_text = _capture(cmd_run_stage, args)
    if code != 0:
        sys.stdout.write(out_text)
        sys.stderr.write(err_text)
        return code
    payload, _tail = _split_json_prefix(out_text)
    payload = payload if payload is not None else {"stage": stage}
    hcode, hout, herr = _capture(cmd_handoff, args)
    if hcode != 0:
        sys.stdout.write(out_text)
        sys.stderr.write(herr)
        return hcode
    wd = _wd(a)
    payload["progresso"] = _progresso(wd, st_mod.load(wd), stage)
    payload["acao"] = (
        f"cole na LLM despachante o prompt impresso abaixo desta linha JSON "
        f"({payload.get('fanout_required', len(payload.get('batches') or []))} subagente(s), "
        f"cada um grava o próprio output); quando os outputs existirem, rode "
        f"`integrate {stage}` — um único comando faz todos os merges e o done {stage}; "
        f"se a engine negar escrita no store, rode `wk init` novamente para migrar as "
        f"permissões (allow em agent-outputs)"
    )
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write(hout if hout.endswith("\n") else hout + "\n")
    return 0


def _integrate_batch_id(batch: dict, fallback: int) -> int:
    try:
        return int(batch.get("batch"))
    except (TypeError, ValueError):
        return fallback


def cmd_integrate(a) -> int:
    """`integrate <stage>`: merge de TODOS os batches do manifesto + `done`.

    Lê `agent-runs/<stage>-plan.json` e, na ordem dos batches, roda o fluxo
    real de `merge-agent-output` com o `--agent <agent_slot>` do próprio
    manifesto (nada de `--agent main`: o slot vem do plano, então o fan-out
    continua provável). Todos os gates de merge continuam ativos.

    Nada é mergeado parcialmente por acidente: se faltar QUALQUER
    `agent-outputs/<stage>-batch-NN.txt`, o comando falha antes do primeiro
    merge listando os `faltantes[]`. `--partial` é o opt-in explícito para
    integrar só o que já chegou.

    Primeiro erro para a execução e propaga o payload ORIGINAL do subcomando
    (com `comandos_redo`, `violacoes`, ...), acrescido do contexto do batch.
    """
    wd = _wd(a)
    stage = a.stage
    st = st_mod.load(wd)
    manifest = _load_json_or_none(_plan_manifest_path(wd, stage))
    if manifest is None:
        _err(
            f"manifesto ausente para {stage}; rode `run {stage}` antes de integrate",
            acao=(
                f"{_sdd_brief_hint(stage)}; rode `run {stage}`, cole o prompt de handoff "
                f"e só então `integrate {stage}`"
            ),
            progresso=_progresso(wd, st, stage),
        )
        return 2
    batches = manifest.get("batches") or []
    if not batches:
        _err(
            f"manifesto de {stage} sem batches",
            acao=f"rode `run {stage}` novamente",
            progresso=_progresso(wd, st, stage),
        )
        return 2

    faltantes = [
        {
            "batch": _integrate_batch_id(b, i),
            "agent": b.get("agent_slot"),
            "output": b.get("output"),
        }
        for i, b in enumerate(batches, start=1)
        if not os.path.isfile(str(b.get("output") or ""))
    ]
    if faltantes and not getattr(a, "partial", False):
        _err(
            f"output(s) de subagente ausente(s) para {stage}: "
            + ", ".join(str(f["output"]) for f in faltantes),
            faltantes=faltantes,
            acao=(
                "cada subagente precisa GRAVAR o próprio recibo antes do merge; falta(m) "
                + ", ".join(f"{f['agent']} -> {f['output']}" for f in faltantes)
                + f"; rode `handoff {stage}` para reemitir o prompt, ou "
                f"`integrate {stage} --partial` para integrar só os batches já entregues"
            ),
            progresso=_progresso(wd, st, stage),
        )
        return 2

    merges: list[dict] = []
    for i, batch in enumerate(batches, start=1):
        batch_id = _integrate_batch_id(batch, i)
        slot = batch.get("agent_slot")
        output = str(batch.get("output") or "")
        if not os.path.isfile(output):  # só alcançável com --partial
            merges.append({"batch": batch_id, "agent": slot, "status": "ausente", "output": output})
            continue
        code, out_text, err_text = _capture(
            cmd_merge_agent_output,
            _derived_args(a, stage=stage, input=output, agent=slot),
        )
        if code != 0:
            payload, _tail = _split_json_prefix(err_text or out_text)
            if payload is None:
                payload = {"error": (err_text or out_text).strip() or f"falha no merge do batch {batch_id}"}
            payload.setdefault("acao", _sdd_brief_hint(stage))
            payload.setdefault("stage", stage)
            payload["batch"] = batch_id
            payload["agent"] = slot
            payload["input"] = output
            payload["merges"] = merges
            payload["progresso"] = _progresso(wd, st_mod.load(wd), stage)
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return code
        result, _tail = _split_json_prefix(out_text)
        entry = {"batch": batch_id, "agent": slot, "status": "ok"}
        if isinstance(result, dict):
            entry["items"] = result.get("items")
            entry["artifacts"] = result.get("artifacts")
            if result.get("blockers"):
                entry["blockers"] = result["blockers"]
            if result.get("warnings"):
                entry["warnings"] = result["warnings"]
        merges.append(entry)

    dcode, dout, derr = _capture(
        cmd_done, _derived_args(a, stage=stage, item=None, artifact=None)
    )
    if dcode != 0:
        payload, _tail = _split_json_prefix(derr or dout)
        if payload is None:
            payload = {"error": (derr or dout).strip() or f"falha no done {stage}"}
        payload.setdefault("acao", f"corrija o blocker e rode `done {stage}`")
        payload.setdefault("stage", stage)
        payload["merges"] = merges
        payload["progresso"] = _progresso(wd, st_mod.load(wd), stage)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return dcode
    done_payload, _tail = _split_json_prefix(dout)
    print(json.dumps(
        {
            "stage": stage,
            "merges": merges,
            "done": done_payload if done_payload is not None else dout.strip(),
            "progresso": _progresso(wd, st_mod.load(wd), stage),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


# Subcomandos que `next --run` pode disparar sozinho: 100% determinísticos,
# sem decisão humana e sem passo de LLM no meio.
NEXT_RUNNABLE_CMDS = (
    "run-stage", "handoff", "run", "integrate", "done", "evidence", "export", "plan",
)


def _next_command(a, name: str, stage: str | None = None):
    """(nome, fn, namespace) de um subcomando determinístico, já com os
    defaults que o subparser correspondente aplicaria."""
    if name == "plan":
        return name, cmd_plan, _derived_args(
            a, top=None, batches=None,
            agent_pack_max_bytes=agentpack_mod.DEFAULT_MAX_BYTES, include_tests=False,
        )
    if name == "export":
        return name, cmd_export, _derived_args(a, topic=None, output=None)
    if name == "evidence":
        return name, cmd_evidence, _derived_args(
            a, topic=None, top=20, context=ev_mod.DEFAULT_CONTEXT,
            max_lines=ev_mod.DEFAULT_MAX_LINES, output=None,
        )
    if name in ("run", "run-stage"):
        fn = cmd_run if name == "run" else cmd_run_stage
        return name, fn, _derived_args(
            a, stage=stage, batches=None,
            max_bytes=agentpack_mod.DEFAULT_MAX_BYTES,
            max_files=agentpack_mod.DEFAULT_MAX_FILES_PER_MODULE,
            max_lines=agentpack_mod.DEFAULT_MAX_LINES_PER_FILE,
        )
    if name == "handoff":
        return name, cmd_handoff, _derived_args(a, stage=stage)
    if name == "integrate":
        return name, cmd_integrate, _derived_args(a, stage=stage, partial=False)
    if name == "done":
        return name, cmd_done, _derived_args(a, stage=stage, item=None, artifact=None)
    raise KeyError(name)


def _next_dispatch(a, payload: dict):
    """Resolve o passo do `next` em UM subcomando determinístico, ou explica
    por que não dá para executá-lo sozinho.

    Devolve `(nome, fn, args)` para executar, ou um dict
    `{bloqueado_em, acao}` quando o próximo passo é decisão humana (`config`,
    `pending`, parâmetros de `surface`/`verify`) ou passo de LLM (colar o
    handoff, gerar os outputs dos subagentes, escrever artefato SDD que falta).
    Uma ação por chamada — nunca encadeia, nunca recorre.
    """
    wd = _wd(a)
    st = st_mod.load(wd)
    stage = payload.get("proximo")
    acao = str(payload.get("acao") or "")

    if stage is None:
        return {"bloqueado_em": "pipeline_completo", "acao": "nada a executar"}
    if not st:
        return {
            "bloqueado_em": "passo_manual",
            "acao": "rode `surface --topic <topico>` (parâmetros de varredura não são dedutíveis do estado)",
        }
    # `export` é 100% determinístico e obrigatório no estágio 1, mas nunca é o
    # `proximo` da máquina de estados (não é um stage) — sem este ramo ele
    # ficaria inalcançável por `next --run`.
    if _surface_done(st) and not _nonempty(_sdd(wd, "inventory.md")):
        return _next_command(a, "export")
    if stage == "config":
        return {"bloqueado_em": "decisao_humana", "acao": acao or SDD_CONFIG_ACTION}
    if stage == "surface":
        return {
            "bloqueado_em": "passo_manual",
            "acao": acao or "rode `surface --topic <topico>` (parâmetros de varredura não são dedutíveis do estado)",
        }

    s = (st.get("stages") or {}).get(stage) or {}
    if s.get("last_error"):
        return {
            "bloqueado_em": "blocker_do_done",
            "acao": acao or f"corrija o blocker e rode `done {stage}`",
        }
    if s.get("status") in st_mod.ITEM_PROBLEM_STATUSES:
        return {
            "bloqueado_em": f"estagio_{s.get('status')}",
            "acao": acao or f"resolva o item {s.get('status')} e rode `done {stage}`",
        }
    if stage == "evidence":
        return _next_command(a, "evidence")
    if stage == "verify":
        return {
            "bloqueado_em": "passo_manual",
            "acao": "rode `verify --artifact <markdown>` (o artefato a verificar não sai do estado)",
        }

    # Estágios de fan-out: plan -> run -> (LLM grava outputs) -> integrate.
    if stage == "modules" and not s.get("pending") and not (s.get("done") or []):
        return _next_command(a, "plan")
    if (
        stage in st_mod.ITEM_STAGES_REQUIRE_FINALIZE
        and stage != "modules"
        and not s.get("pending")
        and not (s.get("done") or [])
    ):
        return {
            "bloqueado_em": "decisao_humana",
            "acao": f"escolha as unidades do estágio e rode `pending {stage} --items a,b,c`",
        }
    if s.get("items_complete") and s.get("status") != "done":
        return _next_command(a, "done", stage)
    manifest = _load_json_or_none(_plan_manifest_path(wd, stage))
    batches = (manifest or {}).get("batches") or []
    if not batches:
        return _next_command(a, "run", stage)
    faltantes = [
        str(b.get("output")) for b in batches if not os.path.isfile(str(b.get("output") or ""))
    ]
    if faltantes:
        return {
            "bloqueado_em": "outputs_de_subagente_ausentes",
            "acao": (
                f"passo de LLM: cole o prompt de `handoff {stage}` na LLM despachante e faça cada "
                f"subagente GRAVAR o próprio output; falta(m) " + ", ".join(faltantes)
                + f"; depois rode `integrate {stage}`"
            ),
            "faltantes": faltantes,
        }
    return _next_command(a, "integrate", stage)


def _next_run(a, payload: dict) -> int:
    """`next --run`: no máximo UMA ação determinística por invocação."""
    plan = _next_dispatch(a, payload)
    if isinstance(plan, dict):
        out = dict(payload)
        out.update(plan)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    name, fn, args = plan
    code, out_text, err_text = _capture(fn, args)
    result, tail = _split_json_prefix(out_text if code == 0 else (err_text or out_text))
    wd = _wd(a)
    envelope: dict = {
        "proximo": payload.get("proximo"),
        "executado": name,
        "exit_code": code,
        "progresso": _progresso(wd, st_mod.load(wd)),
    }
    if result is not None:
        envelope["resultado"] = result
    else:
        envelope["saida"] = (out_text or err_text).strip()
    stream = sys.stdout if code == 0 else sys.stderr
    print(json.dumps(envelope, ensure_ascii=False, indent=2), file=stream)
    if tail:
        sys.stdout.write(tail if tail.endswith("\n") else tail + "\n")
    return code


# ---------------------------------------------------------------------------
# `auto`: o laço determinístico do pipeline.
#
# `next --run` executa NO MÁXIMO uma ação por invocação — o humano volta ao
# teclado depois de cada `export`, `plan`, `done`... `auto` encadeia todas as
# ações determinísticas seguidas e só devolve o controle nas três situações em
# que o humano é REALMENTE necessário:
#
#   1. decisão-chave (`decisao_humana`): doc_level/granularity, unidades de
#      `specs`. Quando essas decisões vêm nas flags (`--doc-level`,
#      `--granularity`, `--specs-items`), `auto` grava a config/pendência ele
#      mesmo e NÃO para — cada flag elimina uma parada;
#   2. colar o prompt na LLM (`fanout:<stage>`): o único passo que o CLI não
#      pode executar. `auto` imprime o prompt e para; na reinvocação, se os
#      outputs do manifesto já existem, ele retoma do `integrate`;
#   3. loop de erro (`intervencao`): a MESMA falha duas vezes seguidas na mesma
#      etapa. Aí insistir é desperdício — devolve o payload de erro original
#      completo (com `comandos_redo` quando existe) e para.
#
# Nada aqui reimplementa a máquina de estados: o próximo passo sai de
# `_next_payload`/`_next_dispatch`, e cada ação é a MESMA `fn` que o argparse
# chamaria, com o mesmo namespace — todo gate continua valendo porque é
# literalmente o mesmo código executando.
# ---------------------------------------------------------------------------

# Teto duro de ações por invocação: nenhuma recursão, nenhum laço infinito.
# Alto o suficiente para um pipeline inteiro entre dois fan-outs, baixo o
# suficiente para que um ciclo patológico morra em segundos.
AUTO_MAX_ACOES = 30

# Quantas vezes a MESMA assinatura de erro pode aparecer na mesma etapa antes
# de o laço parar e chamar o humano. 2 = "errou, tentou de novo, errou igual".
AUTO_MAX_TENTATIVAS = 2

# Quantas execuções BEM-SUCEDIDAS e idênticas seguidas contam como laço parado
# (a ação sai 0 mas o pipeline não anda). Ver o guarda em `cmd_auto`.
AUTO_MAX_REPETICOES = 3


def _auto_error_signature(stage: str | None, error: str) -> str:
    """Assinatura estável de um erro: hash de (etapa + mensagem).

    É o que distingue "erro novo" (o pipeline andou, achou outro problema) de
    "mesmo erro de novo" (o laço está batendo na mesma parede). Só a mensagem
    entra no hash — nunca timestamps ou caminhos de tmp, que mudariam a
    assinatura a cada execução e neutralizariam a guarda.
    """
    base = f"{stage or '-'}\n{(error or '').strip()}"
    return hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()[:16]


def _auto_reset_tentativas(wd: str) -> None:
    """`--retry`: zera o contador de tentativas (o humano interveio)."""
    with st_mod._lock(wd):
        st = st_mod.load(wd)
        if not st:
            return
        st["auto"] = {"erros": {}, "reset_em": st_mod._now()}
        st_mod.save(wd, st)


def _auto_clear_error(wd: str, stage: str | None) -> None:
    """Ação da etapa passou: a assinatura anterior não conta mais como
    'seguida'. Sem isto, um erro resolvido continuaria armado e a próxima
    falha diferente já cairia direto em `intervencao`."""
    if not stage:
        return
    with st_mod._lock(wd):
        st = st_mod.load(wd)
        if not st:
            return
        erros = ((st.get("auto") or {}).get("erros") or {})
        if stage in erros:
            erros.pop(stage)
            st.setdefault("auto", {})["erros"] = erros
            st_mod.save(wd, st)


def _auto_record_error(wd: str, stage: str | None, error: str) -> dict:
    """Registra a falha em `state.json` (`auto.erros[<stage>]`) e devolve
    `{assinatura, tentativas}`. Persistir é o ponto: a 2ª tentativa quase
    sempre acontece em OUTRA invocação do CLI (o humano roda `auto` de novo),
    então um contador em memória nunca veria a repetição."""
    key = stage or "-"
    assinatura = _auto_error_signature(stage, error)
    tentativas = 1
    with st_mod._lock(wd):
        st = st_mod.load(wd)
        if not st:
            return {"assinatura": assinatura, "tentativas": tentativas}
        auto = st.setdefault("auto", {})
        erros = auto.setdefault("erros", {})
        anterior = erros.get(key) or {}
        if anterior.get("assinatura") == assinatura:
            tentativas = int(anterior.get("tentativas") or 0) + 1
        erros[key] = {
            "assinatura": assinatura,
            "tentativas": tentativas,
            "mensagem": (error or "")[:500],
            "em": st_mod._now(),
        }
        st_mod.save(wd, st)
    return {"assinatura": assinatura, "tentativas": tentativas}


def _auto_finish_action(a, wd: str, st: dict | None) -> str:
    topic = (st or {}).get("topic") or getattr(a, "topic", None) or "<topic>"
    return (
        "pipeline do `wk code` completo (synth fechado): rode "
        f'`wk finish --workdir "{wd}" --topic "{topic}" --repo "{a.repo}" '
        f'--store "{a.store}" --approved-by <voce> --approve` '
        "para verify+audit+publish+promote+compile+index"
    )


AUTO_FANOUT_ACAO = (
    "cole o prompt na LLM; quando os outputs existirem, rode `wk code auto` de novo "
    "(ele continua do integrate); se a engine negar escrita no store, rode `wk init` "
    "novamente para migrar as permissões (allow em agent-outputs)"
)


def _auto_stage_state(st: dict | None, stage: str | None) -> dict:
    return ((st or {}).get("stages") or {}).get(stage or "") or {}


def _auto_plan(a, payload: dict, st: dict | None, wd: str):
    """Próxima ação do laço: `(rotulo, fn, args, stage)` para executar, ou um
    dict de PARADA (`parado_em`/`motivo`/`acao`).

    A ordem espelha `_next_dispatch` (mesma máquina de estados); as únicas
    diferenças são os pontos onde `auto` pode agir sozinho: roda `surface`
    (o tópico vem de `--topic`), grava `config`/`pending` quando as flags
    trazem a decisão, e reexecuta o `done` de um estágio com erro registrado
    em vez de só reportá-lo.
    """
    stage = payload.get("proximo")

    # 1) Estágio 1: sem estado ou surface ainda aberto.
    if not st or not _surface_done(st):
        if not (getattr(a, "topic", None) or (st or {}).get("topic")):
            return {
                "parado_em": "decisao_humana",
                "motivo": "o tópico do wiki-ai não é dedutível do repositório",
                "acao": "rode `wk code auto --topic <slug>` (o auto varre o repo e segue sozinho)",
            }
        return ("surface", cmd_surface, _derived_args(
            a, topic=getattr(a, "topic", None), module_min_files=3, since=None,
        ), "surface")

    # 2) Fim do pipeline do `wk code`: `synth` fechado. `verify` fica de fora
    #    de propósito — ele roda dentro do `wk finish`, com o artefato certo.
    if stage is None or _auto_stage_state(st, "synth").get("status") == "done":
        return {
            "parado_em": "pipeline_completo",
            "motivo": "todos os estágios SDD fechados",
            "acao": _auto_finish_action(a, wd, st),
        }

    # 3) `export` é determinístico e obrigatório no estágio 1, mas nunca é o
    #    `proximo` da máquina de estados (não é stage) — mesmo ramo do
    #    `_next_dispatch`, replicado aqui para vir ANTES do gate de config.
    if not _nonempty(_sdd(wd, "inventory.md")):
        name, fn, args = _next_command(a, "export")
        args.topic = getattr(a, "topic", None)
        return (name, fn, args, None)

    # 4) Decisão-chave nº 1: doc_level/granularity. Com as flags, o auto grava.
    if stage == "config":
        missing = _missing_sdd_config(st)
        supplied = set()
        if getattr(a, "doc_level", None):
            supplied.add("sdd.doc_level")
        if getattr(a, "granularity", None):
            supplied.add("sdd.granularity")
        if set(missing) <= supplied:
            return ("config", cmd_config, _derived_args(
                a, doc_level=a.doc_level, granularity=a.granularity,
            ), "config")
        return {
            "parado_em": "decisao_humana",
            "motivo": "config SDD é decisão-chave: nível de documentação e granularidade",
            "missing": missing,
            "acao": (
                "rode `wk code auto --doc-level <essencial|completo|detalhado> "
                "--granularity <module|endpoint|use-case|hybrid|feature>` — "
                "o auto grava a config e segue sem parar de novo aqui"
            ),
        }

    s = _auto_stage_state(st, stage)

    # 5) Erro já registrado no estado: REEXECUTA o `done` do estágio. Só
    #    reportar o blocker travaria o laço para sempre depois que o humano
    #    corrigisse o artefato; reexecutando, o gate decide de novo, e se
    #    falhar igual a guarda de erro conta a repetição.
    if s.get("last_error") and stage in sdd_mod.CRITICAL_STAGES:
        return ("done " + stage, cmd_done, _derived_args(
            a, stage=stage, item=None, artifact=None,
        ), stage)

    # 6) Decisão-chave nº 2: as unidades de `specs`. Com --specs-items, grava.
    if (
        stage in st_mod.ITEM_STAGES_REQUIRE_FINALIZE
        and stage != "modules"
        and not s.get("pending")
        and not (s.get("done") or [])
    ):
        items = (getattr(a, "specs_items", None) or "").strip()
        if items:
            return ("pending " + stage, cmd_pending, _derived_args(
                a, stage=stage, items=items,
            ), stage)
        return {
            "parado_em": "decisao_humana",
            "motivo": f"as unidades do estágio {stage} são decisão-chave do humano",
            "acao": (
                f'rode `wk code auto --specs-items "a,b"` (o auto registra as pendências '
                f"de {stage} e segue) — ou `pending {stage} --items a,b`"
            ),
        }

    # 7) `evidence`: gera o pacote e fecha o estágio.
    if stage == "evidence" and s.get("status") != "done":
        artifact = str(s.get("artifact") or "")
        if artifact and _nonempty(artifact):
            return ("done evidence", cmd_done, _derived_args(
                a, stage="evidence", item=None, artifact=None,
            ), "evidence")
        name, fn, args = _next_command(a, "evidence")
        args.topic = getattr(a, "topic", None)
        return (name, fn, args, "evidence")

    # 8) Resto (plan / run / integrate / done): a máquina de estados canônica.
    plan = _next_dispatch(a, payload)
    if isinstance(plan, dict):
        bloqueado = plan.get("bloqueado_em")
        if bloqueado == "pipeline_completo":
            return {
                "parado_em": "pipeline_completo",
                "motivo": "todos os estágios SDD fechados",
                "acao": _auto_finish_action(a, wd, st),
            }
        if bloqueado == "outputs_de_subagente_ausentes":
            # Fan-out já preparado numa invocação anterior e ainda sem outputs:
            # mesma parada do `run`, com o prompt reimpresso.
            return {
                "parado_em": f"fanout:{stage}",
                "motivo": f"os outputs dos subagentes de {stage} ainda não existem",
                "acao": AUTO_FANOUT_ACAO,
                "faltantes": plan.get("faltantes") or [],
                "_handoff": stage,
            }
        return {
            "parado_em": "decisao_humana",
            "motivo": plan.get("bloqueado_em") or "passo não determinístico",
            "acao": plan.get("acao") or "",
        }
    name, fn, args = plan
    rotulo = f"{name} {stage}" if name in ("run", "integrate", "done") else name
    return (rotulo, fn, args, stage)


def _auto_envelope(wd: str, base: dict, executados: list[str], stage: str | None) -> dict:
    """Toda saída do `auto` carrega `executados[]` (o que ESTA invocação rodou)
    e `progresso` — os dois campos que respondem "o que aconteceu" e "onde eu
    estou" sem uma segunda invocação para descobrir."""
    out = {k: v for k, v in base.items() if not k.startswith("_")}
    out["executados"] = list(executados)
    out["progresso"] = _progresso(wd, st_mod.load(wd), stage)
    return out


def _auto_print(payload: dict, prompt: str = "", stream=None) -> None:
    """Uma linha JSON compacta e, abaixo dela, o prompt cru quando existe
    (mesmo contrato de saída do `run`: `head -1` sempre parseia)."""
    stream = stream or sys.stdout
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=stream)
    if prompt:
        sys.stdout.write(prompt if prompt.endswith("\n") else prompt + "\n")


def _auto_handoff_prompt(a, stage: str) -> str:
    """Reimprime o prompt de despacho de um fan-out já preparado."""
    code, out_text, _err = _capture(cmd_handoff, _derived_args(a, stage=stage))
    return out_text if code == 0 else ""


def _auto_error_stop(a, wd: str, stage: str | None, rotulo: str,
                     code: int, out_text: str, err_text: str,
                     executados: list[str]) -> int:
    """Falha de uma ação: conta a repetição e decide entre 'tenta de novo na
    próxima invocação' e 'chama o humano'."""
    original, _tail = _split_json_prefix(err_text or out_text)
    if original is None:
        original = {"error": (err_text or out_text).strip() or f"falha em `{rotulo}`"}
    mensagem = str(original.get("error") or "")
    guarda = _auto_record_error(wd, stage or rotulo, mensagem)
    tentativas = guarda["tentativas"]
    comandos_redo = original.get("comandos_redo") or []

    if tentativas >= AUTO_MAX_TENTATIVAS:
        acao = (
            f"INTERVENÇÃO HUMANA: o mesmo erro se repetiu {tentativas}x na etapa "
            f"{stage or rotulo} — o laço parou em vez de insistir. "
        )
        if comandos_redo:
            acao += "Rode o(s) redo e refaça o passo: " + " ; ".join(comandos_redo) + ". "
        acao += (
            f"Ou corrija o que `erro` aponta (ex.: reescrever o output do subagente) "
            f"e rode `wk code auto --retry` para zerar o contador. "
            f"Ação original do passo: {original.get('acao') or 'ver `erro`'}"
        )
        payload = {
            "parado_em": "intervencao",
            "motivo": f"mesmo erro {tentativas}x seguidas em `{rotulo}`",
            "erro": original,
            "tentativas": tentativas,
            "assinatura": guarda["assinatura"],
            "acao": acao,
        }
    else:
        payload = {
            "parado_em": "erro",
            "motivo": f"`{rotulo}` falhou (1ª vez com esta assinatura)",
            "erro": original,
            "tentativas": tentativas,
            "assinatura": guarda["assinatura"],
            "acao": (
                (original.get("acao") or "corrija o erro e rode `wk code auto` de novo")
                + " — na próxima repetição idêntica o auto para em `intervencao`"
            ),
        }
    _auto_print(_auto_envelope(wd, payload, executados, stage), stream=sys.stderr)
    return code or 2


def cmd_auto(a) -> int:
    """Encadeia as ações determinísticas até bater numa parada real.

    Exit 0 nas paradas previstas (`decisao_humana`, `fanout:<stage>`,
    `pipeline_completo`, `limite_de_acoes`); exit 2 quando uma ação falhou
    (`erro`), quando a falha se repetiu e exige humano (`intervencao`) ou
    quando o laço deixou de andar (`sem_progresso`).
    """
    wd = _wd(a)
    if getattr(a, "retry", False):
        _auto_reset_tentativas(wd)
    executados: list[str] = []

    for _ in range(AUTO_MAX_ACOES):
        st = st_mod.load(wd)
        payload = _next_payload(a)
        plano = _auto_plan(a, payload, st, wd)

        if isinstance(plano, dict):
            stage_hint = plano.get("_handoff") or payload.get("proximo")
            prompt = _auto_handoff_prompt(a, plano["_handoff"]) if plano.get("_handoff") else ""
            _auto_print(_auto_envelope(wd, plano, executados, stage_hint), prompt)
            return 0

        rotulo, fn, args, stage = plano
        code, out_text, err_text = _capture(fn, args)
        if code != 0:
            return _auto_error_stop(a, wd, stage, rotulo, code, out_text, err_text, executados)

        executados.append(rotulo)
        _auto_clear_error(wd, stage)

        # Ação que "passa" mas não move a máquina de estados (ex.: um `export`
        # que sai 0 sem produzir `inventory.md`) repetiria para sempre até o
        # teto — e cada repetição custa uma varredura do repo. Três iguais
        # seguidas já provam que o laço não está andando.
        if len(executados) >= AUTO_MAX_REPETICOES and len(set(executados[-AUTO_MAX_REPETICOES:])) == 1:
            parada = {
                "parado_em": "sem_progresso",
                "motivo": (
                    f"`{rotulo}` rodou {AUTO_MAX_REPETICOES}x seguidas com exit 0 sem "
                    "mudar o próximo passo do pipeline"
                ),
                "acao": (
                    f"rode `state` e `next` para ver por que `{rotulo}` não avança "
                    "(artefato esperado não foi escrito?); corrija e rode `wk code auto` de novo"
                ),
            }
            _auto_print(_auto_envelope(wd, parada, executados, stage), stream=sys.stderr)
            return 2

        # `run <stage>` preparou o fan-out: aqui o CLI acaba e a LLM começa.
        if rotulo.startswith("run "):
            _prep, prompt = _split_json_prefix(out_text)
            parada = {
                "parado_em": f"fanout:{stage}",
                "motivo": f"fan-out de {stage} preparado; o passo seguinte é de LLM",
                "acao": AUTO_FANOUT_ACAO,
            }
            _auto_print(_auto_envelope(wd, parada, executados, stage), prompt)
            return 0

    parada = {
        "parado_em": "limite_de_acoes",
        "motivo": f"teto de {AUTO_MAX_ACOES} ações por invocação atingido (proteção contra laço)",
        "acao": "rode `wk code auto` de novo; se o teto voltar a bater sem progresso, rode `state` e investigue",
    }
    _auto_print(_auto_envelope(wd, parada, executados, None))
    return 0


def cmd_pilot(a) -> int:
    """Imprime o prompt-mestre do modo piloto (ou o slash command resolvido).

    O `auto` já para sozinho em `fanout:<stage>` e o humano faz a ponte à mão:
    copiar o prompt, colar numa sessão de LLM, esperar os recibos, rodar
    `integrate`, rodar `auto` de novo — a cada estágio. `pilot` emite o texto
    que transfere essa ponte para a própria LLM: ela roda o laço, despacha os
    subagentes, integra e só devolve o controle em decisão-chave ou falha.

    Não executa nada e não escreve nada: só resolve caminhos (python, wk.pyz,
    --store, --repo, workdir) e imprime texto. Todo o trabalho continua nos
    subcomandos existentes, com os mesmos gates.

    O import de `pilot` é local de propósito: `pilot` importa deste módulo as
    constantes de contrato (`HANDOFF_CITACAO_REGRA`, `HANDOFF_FALLBACK_ESCRITA`,
    `AUTO_MAX_ACOES`) para não duplicar regra divergente — importá-lo no topo
    daqui fecharia o ciclo.
    """
    from . import pilot as pilot_mod

    if getattr(a, "command_file", False):
        sys.stdout.write(pilot_mod.render_wk_flow_command(
            python=sys.executable or "python",
            pyz=pilot_mod._resolve_pyz(),
            store=a.store,
            repo=a.repo,
        ))
        return 0

    print(pilot_mod.pilot_prompt(
        store=a.store,
        repo=a.repo,
        topic=getattr(a, "topic", None),
        doc_level=getattr(a, "doc_level", None),
        granularity=getattr(a, "granularity", None),
        specs_items=getattr(a, "specs_items", None),
    ))
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


# --- F-25: pinagem de commit + detecção de drift --------------------------
#
# `surface` grava `git.head` em surface.json e `evidence` copia esse sha para
# `commit` de cada pack — mas NADA nunca comparava esse valor com o repo. O
# efeito prático: depois de qualquer `git pull` no legado analisado, todo o
# SDD (e todas as citações `arquivo:linha` que o `verify` aprova) passava a
# falar de um commit que não é mais o do disco, sem UM aviso. Uma citação
# `Foo.java:120` continuava "válida" porque a linha 120 existe — só que agora
# é outro código. As funções abaixo fecham esse buraco em dois lugares:
# `verify` (reprova a citação afetada) e o subcomando `drift` (relatório).


def _pinned_head(wd: str) -> tuple[str | None, str | None]:
    """(head_pinado, motivo_indisponível) lido do surface.json do workdir."""
    st = st_mod.load(wd) or {}
    art = ((st.get("stages") or {}).get("surface") or {}).get("artifact")
    if not art or not os.path.isfile(art):
        return None, "surface.json ausente: rode `surface` para pinar o commit analisado"
    data = _load_json_or_none(art)
    if not isinstance(data, dict):
        return None, f"surface.json ilegível ({art}): commit pinado indisponível"
    head = (data.get("git") or {}).get("head")
    if not head:
        return None, "surface.json sem `git.head`: o repo não tinha git quando foi varrido"
    return str(head), None


def _drift_state(wd: str, repo_path: str) -> dict:
    """Estado de drift do workdir. NUNCA levanta: indisponível vira warning."""
    pinned, motivo = _pinned_head(wd)
    if pinned is None:
        return {
            "disponivel": False,
            "drift": False,
            "head_pinado": None,
            "head_atual": surface_mod.current_head(repo_path),
            "arquivos_alterados": [],
            "warnings": [motivo],
        }
    return surface_mod.drift_report(repo_path, pinned)


def _module_item_from_slug(wd: str, slug: str) -> str:
    """Volta de `modules/<slug>.md` para o item real gravado no state.

    O slug é lossy (`src/app/Pagamentos` -> `src-app-pagamentos`); reconstruir
    por string daria um `--item` que `redo` não encontra. Aqui o slug é
    comparado contra os itens que já existem no stage.
    """
    stage_state = ((st_mod.load(wd) or {}).get("stages") or {}).get("modules") or {}
    for key in ITEM_LIST_KEYS:
        for cand in stage_state.get(key) or []:
            if ex_mod._slug(str(cand).replace("\\", "/")) == slug:
                return str(cand)
    return slug


def _artifact_stage_item(wd: str, path: str) -> tuple[str | None, str | None]:
    """Mapeia um artefato do workdir de volta para (stage, item) de `redo`.

    O layout é o mesmo que `agentmerge` usa para GRAVAR: `modules/<slug>.md`,
    `sdd/specs/<unit>/*.md` e `sdd/<nome>.md` para os stages nomeados.
    """
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(wd)).replace("\\", "/")
    except ValueError:
        return None, None
    if rel.startswith("../"):
        return None, None
    if rel.startswith("modules/") and rel.endswith(".md"):
        return "modules", _module_item_from_slug(wd, rel[len("modules/"):-len(".md")])
    if not rel.startswith("sdd/"):
        return None, None
    name = rel[len("sdd/"):]
    name = name[:-len(".md")] if name.endswith(".md") else name
    if name.startswith("specs/"):
        parts = name.split("/")
        return "specs", (parts[1] if len(parts) >= 3 else None)
    for stage, allowed in agentmerge_mod.NAMED_STAGE_ALLOWED.items():
        prefixes = agentmerge_mod.NAMED_STAGE_PREFIX_RES.get(stage, ())
        if name in allowed or any(p.match(name) for p in prefixes):
            return stage, name
    if agentmerge_mod._is_specs_doc(name):
        return "specs", name
    return None, None


_DRIFT_ARTIFACT_ROOTS = ("modules", "sdd")
MAX_DRIFT_CITATIONS_REPORTED = 20


def _artifacts_citing(a, wd: str, changed: set[str]) -> list[dict]:
    """Artefatos SDD (.md do workdir) que citam algum arquivo alterado.

    Varre as citações `arquivo:linha` de cada .md — a mesma extração que o
    `verify` usa — e devolve, por artefato, o(s) arquivo(s) afetado(s) e o
    comando `redo` literal do item correspondente.
    """
    afetados: list[dict] = []
    for root_name in _DRIFT_ARTIFACT_ROOTS:
        root = os.path.join(wd, root_name)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                if not fn.endswith(".md"):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    with open(full, encoding="utf-8-sig", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                hits = [
                    c for c in ev_mod.citations(text)
                    if surface_mod.normalize_repo_path(c.path) in changed
                ]
                if not hits:
                    continue
                stage, item = _artifact_stage_item(wd, full)
                entry = {
                    "artefato": os.path.relpath(full, wd).replace("\\", "/"),
                    "stage": stage,
                    "item": item,
                    "arquivos": sorted({surface_mod.normalize_repo_path(c.path) for c in hits}),
                    "citacoes": [
                        f"{c.path}:{c.line_start}" for c in hits[:MAX_DRIFT_CITATIONS_REPORTED]
                    ],
                    "citacoes_afetadas": len(hits),
                }
                if stage in sdd_mod.CRITICAL_STAGES and item:
                    entry["redo"] = _redo_command(a, stage, item)
                afetados.append(entry)
    afetados.sort(key=lambda e: e["artefato"])
    return afetados


def cmd_drift(a) -> int:
    """Compara o commit pinado no surface.json com o HEAD atual do repo.

    Sem drift: `{"drift": false}` — os artefatos continuam falando do commit
    que foi analisado. Com drift: lista COMPLETA de arquivos alterados, quais
    artefatos SDD citam esses arquivos e o `redo` de cada item afetado.
    """
    repo_path, code = _ensure_repo(a)
    if code:
        return code
    wd = _wd(a)
    drift = _drift_state(wd, repo_path)

    payload = {
        "drift": bool(drift.get("drift")),
        "disponivel": bool(drift.get("disponivel")),
        "head_pinado": drift.get("head_pinado"),
        "head_atual": drift.get("head_atual"),
        "arquivos_alterados": list(drift.get("arquivos_alterados") or []),
        "artefatos_afetados": [],
        "warnings": list(drift.get("warnings") or []),
    }

    if not payload["drift"]:
        payload["acao"] = (
            "sem drift: o repo ainda está no commit pinado; artefatos e citações seguem válidos"
            if payload["disponivel"]
            else "drift indeterminado: rode `surface` para (re)pinar o commit deste repo"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    changed = set(payload["arquivos_alterados"])
    afetados = _artifacts_citing(a, wd, changed)
    payload["artefatos_afetados"] = afetados

    redos = []
    for entry in afetados:
        cmd = entry.get("redo")
        if cmd and cmd not in redos:
            redos.append(cmd)
    if redos:
        payload["acao"] = (
            f"{len(afetados)} artefato(s) citam arquivo alterado entre "
            f"{payload['head_pinado']} e {payload['head_atual']}: refaça os itens afetados — "
            + "; ".join(redos[:5])
            + ("; ..." if len(redos) > 5 else "")
            + " e rode `surface` de novo para repinar o commit"
        )
    elif afetados:
        payload["acao"] = (
            "artefatos citam arquivo alterado, mas o item de origem não é remapeável para "
            "`redo`: revise os artefatos listados e rode `surface` de novo"
        )
    else:
        payload["acao"] = (
            "drift sem impacto: nenhum artefato SDD cita arquivo alterado; "
            "rode `surface` de novo para repinar o commit"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _apply_drift_to_report(a, wd: str, repo_path: str, md: str, report: dict) -> dict | None:
    """Aplica o contrato de drift ao relatório do `verify` (mutando-o).

    Devolve o bloco `drift` do payload quando o repo divergiu do commit
    pinado, ou `None`. Regra: citação para arquivo alterado desde o commit
    pinado vira ERRO `drift_detectado` (o artefato precisa ser refeito, não
    "revalidado"); drift que não toca nenhum arquivo citado é só warning.
    """
    drift = _drift_state(wd, repo_path)
    warnings = report.setdefault("warnings", [])
    for w in drift.get("warnings") or []:
        if w not in warnings:
            warnings.append(w)
    if not drift.get("drift"):
        return None

    changed = set(drift.get("arquivos_alterados") or [])
    afetadas = [c for c in ev_mod.citations(md) if surface_mod.normalize_repo_path(c.path) in changed]
    arquivos_afetados = sorted({surface_mod.normalize_repo_path(c.path) for c in afetadas})

    if afetadas:
        for c in afetadas:
            report["errors"].append(
                {
                    "citation": f"{c.path}:{c.line_start}",
                    "rule": "drift_detectado",
                    "detail": (
                        f"{c.path} mudou entre o commit pinado {drift.get('head_pinado')} e o "
                        f"HEAD atual {drift.get('head_atual')}: a citação não prova mais nada"
                    ),
                }
            )
        report["ok"] = not report["errors"]
    else:
        warnings.append(
            f"drift: {len(changed)} arquivo(s) mudaram entre {drift.get('head_pinado')} e "
            f"{drift.get('head_atual')}, nenhum deles citado neste artefato"
        )

    return {
        "head_pinado": drift.get("head_pinado"),
        "head_atual": drift.get("head_atual"),
        "arquivos_alterados": arquivos_afetados,
    }


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

    wd = _wd(a)
    # F-25: o drift entra ANTES da gravação do relatório e do estado — um
    # artefato cujas citações apontam para arquivo já alterado não pode ser
    # gravado como `done` nem sair com exit code 0.
    drift_bloco = _apply_drift_to_report(a, wd, repo_path, md, report)

    out = a.output
    if out:
        ev_mod.write_json(out, report)

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
    # F01 (W0): vincula ESTA aprovação ao hash do artefato + escopo checado
    # (commit pinado + arquivos citados) — sem isto, `done evidence`/`done
    # verify` (cmd_done) não tinha como distinguir "artefato de fato passou
    # por `verify`" de "alguém setou o campo à mão". Só quando o relatório
    # aprova: um artefato reprovado não deve virar um registro "verificado"
    # que `cmd_done` (se algum dia relaxar o bloqueio de `verify`) aceitaria.
    if report["ok"]:
        pinned_head, _motivo = _pinned_head(wd)
        st_mod.record_verification(
            wd, "verify", key, a.artifact,
            scope={
                "repo_head": pinned_head,
                "citations": sorted({c.path for c in ev_mod.citations(md)}),
            },
        )
    # `mark` faz seu próprio load/save sob trava e é a única via pública que
    # grava status/artifact do stage — chamado DEPOIS do merge do mapa.
    st_mod.mark(wd, "verify", cobertura["status"], out or a.artifact)

    payload = dict(report)
    if drift_bloco:
        payload["drift"] = drift_bloco
    payload["cobertura"] = cobertura
    payload["stage_status"] = cobertura["status"]
    payload["acao"] = _verify_next_action(wd, cobertura)
    if drift_bloco and drift_bloco["arquivos_alterados"]:
        payload["acao"] = (
            "drift: o repo saiu do commit pinado e arquivos citados mudaram — rode "
            "`surface` para repinar e `drift` para ver os itens a refazer, depois revalide"
        )
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
    # `_split_json_prefix` (e não `json.loads` direto) porque `run` imprime uma
    # linha JSON seguida do prompt de despacho: sem isso o resumo de `--quiet`
    # degradaria para `output_bytes` justamente no composto principal.
    payload, _tail = _split_json_prefix(raw) if raw else (None, "")
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
    # `pilot` é o único comando cujo PRODUTO é texto cru (o prompt-mestre) e
    # não um payload JSON: condensá-lo devolveria `{"output_bytes": N}` e
    # descartaria exatamente aquilo que foi pedido. `--quiet` aqui não tem
    # resumo possível, então é no-op em vez de armadilha silenciosa.
    if getattr(a, "cmd", None) == "pilot":
        return a.fn(a)

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
    "run": ("--batches", "--max-bytes", "--max-files", "--max-lines"),
    "integrate": (),
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
    n.add_argument(
        "--run",
        action="store_true",
        help=(
            "executa a próxima ação quando ela é um subcomando determinístico "
            f"({'/'.join(NEXT_RUNNABLE_CMDS)}); decisão humana ou passo de LLM "
            "devolve {bloqueado_em, acao} sem executar nada"
        ),
    )
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

    rn = sub.add_parser("run", help="composto: run-stage + handoff (prepara o fan-out e entrega o prompt)")
    rn.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    rn.add_argument("--batches", type=int, help="override de batches para modules")
    rn.add_argument("--max-bytes", type=int, default=agentpack_mod.DEFAULT_MAX_BYTES)
    rn.add_argument("--max-files", type=int, default=agentpack_mod.DEFAULT_MAX_FILES_PER_MODULE)
    rn.add_argument("--max-lines", type=int, default=agentpack_mod.DEFAULT_MAX_LINES_PER_FILE)
    rn.set_defaults(fn=cmd_run)

    ig = sub.add_parser(
        "integrate",
        help="composto: merge de todos os batches do manifesto (com o agent_slot de cada um) + done do estágio",
    )
    ig.add_argument("stage", choices=sdd_mod.CRITICAL_STAGES)
    ig.add_argument(
        "--partial",
        action="store_true",
        help="opt-in: integra só os batches cujo output já existe (sem isso, output faltando aborta antes do 1º merge)",
    )
    ig.set_defaults(fn=cmd_integrate)

    au_to = sub.add_parser(
        "auto",
        help=(
            "laço: encadeia as ações determinísticas até a próxima parada real "
            "(decisão-chave, colar o prompt na LLM, ou loop de erro)"
        ),
    )
    au_to.add_argument(
        "--topic", help="tópico do wiki-ai; permite ao auto rodar o `surface` sozinho",
    )
    au_to.add_argument(
        "--doc-level", choices=("essencial", "completo", "detalhado"),
        help="decisão de config antecipada: com ela (e --granularity) o auto grava a config e não para",
    )
    au_to.add_argument(
        "--granularity",
        choices=("module", "endpoint", "use-case", "hybrid", "feature", "custom"),
        help="decisão de config antecipada: com ela (e --doc-level) o auto grava a config e não para",
    )
    au_to.add_argument(
        "--specs-items",
        help='unidades do estágio specs ("a,b"): com elas o auto registra as pendências e não para',
    )
    au_to.add_argument(
        "--retry", action="store_true",
        help="zera o contador de tentativas da guarda de loop de erro (o humano interveio)",
    )
    au_to.set_defaults(fn=cmd_auto)

    pt = sub.add_parser(
        "pilot",
        help=(
            "imprime o prompt-mestre do modo piloto: a LLM roda o `auto` em laço, "
            "despacha os subagentes do fan-out, integra e só para em decisão humana ou falha"
        ),
    )
    pt.add_argument(
        "--command-file",
        action="store_true",
        help=(
            "imprime o conteúdo do slash command (.claude/commands/wk-flow.md) "
            "com os caminhos já resolvidos, em vez do prompt cru"
        ),
    )
    pt.add_argument("--topic", help="tópico do wiki-ai; embutido no comando AUTO do prompt")
    pt.add_argument(
        "--doc-level", choices=("essencial", "completo", "detalhado"),
        help="decisão de config antecipada: embutida no comando AUTO (o piloto não para nela)",
    )
    pt.add_argument(
        "--granularity",
        choices=("module", "endpoint", "use-case", "hybrid", "feature", "custom"),
        help="decisão de config antecipada: embutida no comando AUTO (o piloto não para nela)",
    )
    pt.add_argument(
        "--specs-items",
        help='unidades do estágio specs ("a,b"): embutidas no comando AUTO do prompt',
    )
    pt.set_defaults(fn=cmd_pilot)

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

    df = sub.add_parser(
        "drift",
        help="compara o commit pinado no surface.json com o HEAD atual e lista artefatos SDD afetados",
    )
    df.set_defaults(fn=cmd_drift)

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

    # F01 (W0): auto-cura ANTES de qualquer subcomando rodar — se o artefato
    # que embasou um `done` de `evidence`/`verify` mudou de conteúdo (ou
    # sumiu) desde então, `status` volta pra `failed` aqui, nesta própria
    # invocação. Sem isto, editar `sdd/confirmed.md` depois do `verify`
    # deixaria `stages.verify.status` mentindo "done" indefinidamente —
    # nenhum comando reconferia o hash. Best-effort (nunca levanta: ver
    # `state.revalidate_verified_stages`) e silencioso quando não há nada a
    # revalidar, então roda em toda invocação sem custo perceptível.
    st_mod.revalidate_verified_stages(st_mod.workdir(a.store, a.repo))

    # Corpo completo é o padrão do `wk code` (--verbose é aceito só como
    # no-op de compatibilidade). `--quiet` é opt-in explícito para o resumo
    # de uma linha; funciona em qualquer posição (flags injetadas em todo
    # subparser acima) com efeito idêntico ao da flag global.
    if a.quiet:
        return _run_quiet(a)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
