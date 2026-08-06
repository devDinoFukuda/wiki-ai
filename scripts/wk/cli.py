"""CLI unificada do Wiki AI.

Um binário para as duas metades do sistema:

    wk code <sub>     pipeline de repositório (exige --repo)
    wk index <sub>    manutenção do índice (reindex, status)
    wk search|get|audit
    wk docs <nome>    documentação embutida no próprio executável
    wk init [dir]     materializa o mínimo em disco p/ o Claude Code achar a skill

Empacotado com `zipapp`: um arquivo, sem PYTHONPATH, sem instalação. Os
documentos da skill viajam dentro do zip — `wk docs` os imprime; só o
`SKILL.md` precisa existir em disco, porque é como o harness descobre a skill.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import io
import json
import os
import pkgutil
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

from . import __version__

DOCS_INDEX = "_docs/index.json"

# Preenchido nos documentos escritos pelo `init`: como invocar este executável.
WK_PLACEHOLDER = "{{WK}}"

# Onde cada engine descobre uma Agent Skill (verificado 2026-07, docs oficiais).
# `.agents/skills` é o padrão comum: Antigravity, Devin e Copilot leem os três.
# O `init` só materializa em disco; o payload pesado vive no .pyz (sb docs).
_AGENTS = ".agents/skills"
ENGINES = {
    "claude-code": ".claude/skills",
    "antigravity": _AGENTS,
    "devin": _AGENTS,     # também .devin/skills e .windsurf/skills
    "copilot": _AGENTS,   # também .github/skills e .claude/skills
}
SKILL_DIRNAME = "wiki-ai"


# ---------- documentos embutidos ----------


def _read_asset(name: str) -> bytes | None:
    try:
        return pkgutil.get_data(__package__, name)
    except (OSError, FileNotFoundError):
        return None


def _docs_manifest() -> dict:
    raw = _read_asset(DOCS_INDEX)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}


def _doc_text(slug: str) -> str | None:
    entry = _docs_manifest().get(slug)
    if not entry:
        return None
    raw = _read_asset("_docs/" + entry["asset"])
    return raw.decode("utf-8") if raw else None


def cmd_docs(a) -> int:
    manifest = _docs_manifest()
    if a.list or not a.name:
        if not manifest:
            print("nenhum documento embutido (build sem --docs?)", file=sys.stderr)
            return 1
        width = max(len(k) for k in manifest)
        for slug, meta in sorted(manifest.items()):
            print(f"{slug.ljust(width)}  {meta.get('title', '')}")
        return 0
    text = _doc_text(a.name)
    if text is None:
        print(
            json.dumps(
                {"error": f"documento desconhecido: {a.name}",
                 "validos": sorted(_docs_manifest())},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


# ---------- init ----------


def _self_invocation() -> str:
    """Como o usuário deve chamar este executável (vai para dentro dos docs)."""
    archive = getattr(sys.modules.get("__main__"), "__file__", None) or sys.argv[0]
    root = os.environ.get("WK_ARCHIVE") or archive
    # Rodando de dentro do zipapp, __file__ aponta para <arquivo.pyz>/__main__.py
    while root and not os.path.exists(root):
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return f'python "{os.path.abspath(root)}"' if root else "python wk.pyz"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _resolve_engines(raw: str) -> tuple[list[str], str | None]:
    """`--engine claude-code,copilot` ou `all` -> lista de engines válidas."""
    wanted = [e.strip() for e in (raw or "").split(",") if e.strip()]
    if wanted == ["all"]:
        return sorted(ENGINES), None
    unknown = [e for e in wanted if e not in ENGINES]
    if unknown:
        return [], f"engine(s) desconhecida(s): {unknown}. Válidas: {sorted(ENGINES)} ou 'all'"
    return wanted, None


def _skill_dirs(engines: list[str], base: str) -> dict[str, str]:
    """engine -> diretório da skill em disco. Deduplica `.agents/skills`."""
    dirs = {}
    for e in engines:
        dirs[e] = os.path.abspath(os.path.join(base, ENGINES[e], SKILL_DIRNAME))
    return dirs


# ---------- init: permissões da engine (store/repo) ----------


def _settings_targets(engines: list[str], base: str) -> list[str]:
    """engine -> settings.json físico único. claude-code tem o seu próprio;
    antigravity/devin/copilot compartilham `.agents/settings.json`, espelhando
    o compartilhamento de `.agents/skills` em ENGINES."""
    targets = set()
    for e in engines:
        rel = ".claude/settings.json" if e == "claude-code" else ".agents/settings.json"
        targets.add(os.path.abspath(os.path.join(base, *rel.split("/"))))
    return sorted(targets)


def _merge_settings_permissions(existing: dict, store_abs: str | None, repo_abs: str | None) -> dict:
    """Mescla `permissions` em cima do settings.json existente sem duplicar
    entradas e sem tocar em nenhuma outra chave (merge idempotente)."""
    out = dict(existing)
    perms = dict(out.get("permissions") or {})

    def _add_unique(key: str, new_items: list[str]) -> None:
        cur = list(perms.get(key) or [])
        for item in new_items:
            if item and item not in cur:
                cur.append(item)
        perms[key] = cur

    dirs = [d for d in (store_abs, repo_abs) if d]
    _add_unique("additionalDirectories", dirs)
    _add_unique("allow", [f"Read({d}/**)" for d in dirs] + (["Bash(wk *)"] if dirs else []))
    if store_abs:
        # A sessão principal não escreve SDD à mão: quem escreve é `wk` (promote/
        # compile/publish/ingest), nunca Write/Edit direto do agente no store.
        _add_unique("deny", [f"Write({store_abs}/**)", f"Edit({store_abs}/**)"])

    out["permissions"] = perms
    return out


def _write_permission_settings(path: str, store_abs: str | None, repo_abs: str | None) -> dict:
    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (ValueError, OSError):
            existing = {}
    merged = _merge_settings_permissions(existing, store_abs, repo_abs)
    _write(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    return {"path": path, "permissions": merged["permissions"]}


def _permission_format_status(path: str) -> tuple[str, str | None]:
    """`verificado` só para o settings.json real do claude-code (schema
    documentado e comprovadamente consumido pela ferramenta). Para o trio
    antigravity/devin/copilot, `.agents/settings.json` é um schema que este
    projeto inventou — gravamos o arquivo, mas nada garante que a engine o
    leia. `init`/`check`/`doctor` não podem afirmar garantia que não existe.
    """
    normalized = os.path.normpath(path)
    claude_suffix = os.path.normpath(".claude/settings.json")
    if normalized.endswith(claude_suffix):
        return "verificado", None
    return "best-effort", (
        "arquivo gravado, mas o schema de .agents/settings.json não é "
        "comprovadamente consumido por esta engine; trate como best-effort, "
        "não como permissão garantida"
    )


def _check_permission_settings(
    path: str, store_abs: str | None, repo_abs: str | None
) -> tuple[bool, list[str]]:
    if not os.path.exists(path):
        return False, ["settings ausente"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return False, ["settings ilegível (JSON inválido)"]
    perms = data.get("permissions") or {}
    add_dirs = perms.get("additionalDirectories") or []
    deny = perms.get("deny") or []
    problems = []
    if store_abs and store_abs not in add_dirs:
        problems.append("store ausente em additionalDirectories")
    if repo_abs and repo_abs not in add_dirs:
        problems.append("repo ausente em additionalDirectories")
    if not deny:
        problems.append("deny ausente")
    elif store_abs and not any(store_abs in d for d in deny):
        problems.append("deny não cobre o store")
    return (not problems), problems


def cmd_init(a) -> int:
    manifest = _docs_manifest()
    if "skill" not in manifest:
        print("SKILL.md não está embutido neste build", file=sys.stderr)
        return 1

    engines, err = _resolve_engines(a.engine)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False), file=sys.stderr)
        return 2

    invocation = a.invocation or _self_invocation()
    slugs = sorted(manifest) if a.all else ["skill"]
    # Diretórios físicos únicos (claude-code e o trio .agents podem coincidir).
    dirs = _skill_dirs(engines, a.base)
    result = {"engines": engines, "invocacao": invocation,
              "modo": "completo" if a.all else "minimo (SKILL.md)", "alvos": []}

    for target in sorted(set(dirs.values())):
        report = _write_skill_dir(target, manifest, slugs, invocation, a.force)
        if report is None:
            return 1  # conflito já reportado em stderr
        result["alvos"].append(report)

    if a.store or a.repo:
        store_abs = os.path.abspath(a.store) if a.store else None
        repo_abs = os.path.abspath(a.repo) if a.repo else None
        permissoes = []
        for target in _settings_targets(engines, a.base):
            report = _write_permission_settings(target, store_abs, repo_abs)
            formato, aviso = _permission_format_status(target)
            report["formato"] = formato
            if aviso:
                report["aviso"] = aviso
            permissoes.append(report)
        result["permissoes"] = permissoes

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _write_skill_dir(target, manifest, slugs, invocation, force) -> dict | None:
    """Escreve os docs num diretório. None se houver conflito sem --force."""
    written, skipped = [], []
    for slug in slugs:
        meta = manifest[slug]
        text = (_doc_text(slug) or "").replace(WK_PLACEHOLDER, invocation)
        dest = os.path.join(target, meta["file"])
        if os.path.exists(dest) and not force:
            if open(dest, encoding="utf-8").read() == text:
                skipped.append(meta["file"])
                continue
            print(
                json.dumps({"error": "existente e diferente; use --force",
                            "path": dest}, ensure_ascii=False),
                file=sys.stderr,
            )
            return None
        _write(dest, text)
        written.append(meta["file"])
    return {"dir": target, "comando": "/" + SKILL_DIRNAME,
            "escritos": written, "inalterados": skipped}


def cmd_check(a) -> int:
    """Compara o que está em disco com o que está embutido, por engine."""
    manifest = _docs_manifest()
    engines, err = _resolve_engines(a.engine)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False), file=sys.stderr)
        return 2

    invocation = a.invocation or _self_invocation()
    dirs = _skill_dirs(engines, a.base)
    slugs = sorted(manifest) if a.all else ["skill"]
    dirty = False
    out = {"versao": __version__, "alvos": []}
    for target in sorted(set(dirs.values())):
        diverged, missing, ok = [], [], []
        for slug in slugs:
            meta = manifest[slug]
            dest = os.path.join(target, meta["file"])
            expected = (_doc_text(slug) or "").replace(WK_PLACEHOLDER, invocation)
            if not os.path.exists(dest):
                missing.append(meta["file"])
                continue
            disk = open(dest, encoding="utf-8").read()
            same = hashlib.sha256(disk.encode()).hexdigest() == hashlib.sha256(
                expected.encode()
            ).hexdigest()
            (ok if same else diverged).append(meta["file"])
        if diverged:
            dirty = True
        out["alvos"].append(
            {"dir": target, "iguais": ok, "divergentes": diverged, "ausentes": missing}
        )

    if a.store or a.repo:
        store_abs = os.path.abspath(a.store) if a.store else None
        repo_abs = os.path.abspath(a.repo) if a.repo else None
        config_report = []
        for target in _settings_targets(engines, a.base):
            ok, problems = _check_permission_settings(target, store_abs, repo_abs)
            if not ok:
                dirty = True
            formato, aviso = _permission_format_status(target)
            entry = {"path": target, "ok": ok, "problemas": problems, "formato": formato}
            if aviso:
                entry["aviso"] = aviso
            config_report.append(entry)
        out["config_permissoes"] = config_report
    else:
        # Sem --store/--repo não há o que validar; a chave continua presente
        # (silêncio aqui é o que fazia gente achar que a permissão tinha sido
        # checada quando não foi) e o aviso diz o comando que checa de verdade.
        out["config_permissoes"] = {
            "estado": "nao_verificado",
            "aviso": (
                f"informe --store/--repo para validar: "
                f"wk check --engine {a.engine} --store <store> --repo <repo>"
            ),
        }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if dirty else 0


def cmd_engines(a) -> int:
    """Lista engines suportadas e quais já têm a skill em disco."""
    rows = []
    for e, rel in sorted(ENGINES.items()):
        d = os.path.abspath(os.path.join(a.base, rel, SKILL_DIRNAME, "SKILL.md"))
        rows.append({"engine": e, "diretorio": os.path.dirname(d),
                     "instalada": os.path.exists(d)})
    print(json.dumps({"engines": rows}, ensure_ascii=False, indent=2))
    return 0


def _detect_shell() -> dict:
    """Shell provável, a partir de variáveis de ambiente — sem chamar `ps`.

    Existe porque um teste real gastou ~10 chamadas descobrindo o ambiente
    (múltiplos --help, `ls`, `git log`) e ainda assim rodou comandos em
    PowerShell, que quebra a sintaxe que este CLI e seus docs assumem
    (Git Bash/POSIX sh). `powershell_provavel` é o sinal que os docs/skill
    devem checar antes de sugerir qualquer comando.
    """
    shell_env = os.environ.get("SHELL")
    msystem = os.environ.get("MSYSTEM")
    psmodulepath = os.environ.get("PSModulePath")
    comspec = os.environ.get("ComSpec")
    powershell_provavel = bool(psmodulepath) and not shell_env and not msystem

    if shell_env:
        detectado = f"bash/posix (SHELL={shell_env})"
    elif msystem:
        detectado = f"git-bash/msys ({msystem})"
    elif powershell_provavel:
        detectado = "powershell (provável)"
    elif comspec:
        detectado = f"cmd ({comspec})"
    else:
        detectado = "desconhecido"

    avisos = []
    if powershell_provavel:
        avisos.append(
            "ambiente parece PowerShell (sem SHELL/MSYSTEM, com PSModulePath); "
            "este CLI e seus docs assumem Git Bash/POSIX sh — embrulhe "
            "comandos em: bash -c '...'"
        )

    return {
        "detectado": detectado,
        "powershell_provavel": powershell_provavel,
        "SHELL": shell_env,
        "MSYSTEM": msystem,
        "PSModulePath_presente": bool(psmodulepath),
        "ComSpec": comspec,
        "avisos": avisos,
    }


def _index_doc_count(db_path: str) -> tuple[int | None, str | None]:
    """(contagem, erro). Não usa `sbindex.store.connect` direto: ela cria o
    schema/arquivo se faltar, e `doctor` só deve LER um índice que já existe."""
    try:
        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
            return (row[0] if row else 0), None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, str(exc)


def cmd_doctor(a) -> int:
    """Uma chamada só, no lugar da fase de descoberta (--help repetido, `ls`,
    `git log`, `jq --version`...) que um teste real de ponta a ponta gastou
    ~10 chamadas para reconstruir manualmente."""
    bloqueios: list[str] = []

    shell_info = _detect_shell()
    python_info = {"executavel": sys.executable, "versao": sys.version.split()[0]}
    wk_info = {"versao": __version__, "executavel": _self_invocation()}

    store_root = _store_root(a)
    store_existe = os.path.isdir(store_root)
    store_info: dict = {"caminho": store_root, "existe": store_existe}
    if store_existe:
        estrutura = {
            "inbox": os.path.isdir(os.path.join(store_root, "inbox")),
            "raw": os.path.isdir(os.path.join(store_root, "raw")),
            "wiki": os.path.isdir(os.path.join(store_root, "wiki")),
            "index.db": os.path.isfile(os.path.join(store_root, "index.db")),
        }
        store_info["estrutura"] = estrutura
        store_info["documentos_indexados"] = None
        if estrutura["index.db"]:
            n, idx_err = _index_doc_count(os.path.join(store_root, "index.db"))
            store_info["documentos_indexados"] = n
            if idx_err:
                store_info["index_erro"] = idx_err
    else:
        bloqueios.append("store")

    repo_arg = a.repo or os.environ.get("WK_REPO")
    repo_root = os.path.abspath(repo_arg) if repo_arg else None
    repo_existe = bool(repo_root and os.path.isdir(repo_root))
    repo_info = {
        "caminho": repo_root,
        "existe": repo_existe,
        "git": bool(repo_root and os.path.isdir(os.path.join(repo_root, ".git"))),
    }
    if repo_arg and not repo_existe:
        bloqueios.append("repo")

    engine = a.engine or "claude-code"
    engines_resolved, engine_err = _resolve_engines(engine)
    engine_info: dict = {"nome": engine}
    valid_engines = sorted(ENGINES)
    if engine_err:
        # Engine desconhecida é impeditiva: um diagnóstico que aprova este
        # estado (exit 0) e ainda sugere repetir a mesma engine inválida em
        # `proximo_passo` é pior que não existir. Ver DEFEITO 1.
        engine_info["erro"] = engine_err
        engine_info["engines_validas"] = valid_engines
        bloqueios.append("engine")
    else:
        dirs = _skill_dirs(engines_resolved, a.base)
        engine_info["skill_instalada"] = {
            e: os.path.isfile(os.path.join(dirs[e], "SKILL.md")) for e in engines_resolved
        }
        if a.store or a.repo:
            store_abs = os.path.abspath(a.store) if a.store else None
            repo_abs = os.path.abspath(a.repo) if a.repo else None
            perm_reports = []
            perm_ok_all = True
            formatos = set()
            for target in _settings_targets(engines_resolved, a.base):
                ok, problems = _check_permission_settings(target, store_abs, repo_abs)
                perm_ok_all = perm_ok_all and ok
                formato, aviso = _permission_format_status(target)
                formatos.add(formato)
                entry = {"path": target, "ok": ok, "problemas": problems, "formato": formato}
                if aviso:
                    entry["aviso"] = aviso
                perm_reports.append(entry)
            engine_info["config_permissoes"] = perm_reports
            if not perm_ok_all:
                bloqueios.append("permissao")
            # `permissao_garantida` só é True quando o arquivo está correto
            # E o formato é o `verificado` (claude-code). Best-effort nunca
            # vira garantia, mesmo que o arquivo em disco esteja ok.
            engine_info["permissao_garantida"] = perm_ok_all and formatos == {"verificado"}
            if perm_ok_all and formatos != {"verificado"}:
                engine_info["aviso_permissao"] = (
                    "arquivo(s) de permissão gravado(s) corretamente, mas o "
                    "schema usado por esta engine (.agents/settings.json) não "
                    "é comprovadamente consumido por ela; best-effort, não "
                    "garantia"
                )
        else:
            engine_info["config_permissoes"] = "nao_verificado (informe --store/--repo p/ validar)"
            engine_info["permissao_garantida"] = False

    skill_map = engine_info.get("skill_instalada")
    skill_completa = isinstance(skill_map, dict) and bool(skill_map) and all(skill_map.values())

    if not store_existe:
        proximo = f"wk store init {store_root}"
    elif "engine" in bloqueios:
        # Nunca ecoar a engine inválida informada pelo usuário: a forma
        # correta usa uma engine da lista de válidas.
        parts = ["wk", "init", "--engine", valid_engines[0]]
        if a.store:
            parts += ["--store", a.store]
        if a.repo:
            parts += ["--repo", a.repo]
        proximo = (
            f"engine inválida: {engine!r}. Válidas: {valid_engines}. "
            f"Ex.: {' '.join(parts)}"
        )
    elif "permissao" in bloqueios:
        parts = ["wk", "init", "--engine", engine]
        if a.store:
            parts += ["--store", a.store]
        if a.repo:
            parts += ["--repo", a.repo]
        proximo = " ".join(parts)
    elif "repo" in bloqueios:
        proximo = f"corrija --repo: caminho não existe: {repo_root}"
    elif not skill_completa:
        parts = ["wk", "init", "--engine", engine]
        if a.store:
            parts += ["--store", a.store]
        if a.repo:
            parts += ["--repo", a.repo]
        proximo = " ".join(parts)
    elif repo_root and repo_existe:
        proximo = f"wk code --repo {repo_root} --store {store_root} surface --topic <slug>"
    else:
        proximo = "ambiente ok — nenhuma ação necessária"

    out = {
        "shell": shell_info,
        "python": python_info,
        "wk": wk_info,
        "store": store_info,
        "repo": repo_info,
        "engine": engine_info,
        "bloqueios": bloqueios,
        "proximo_passo": proximo,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if bloqueios else 0


# Estrutura do store. inbox = entrada (nunca fonte); raw = fonte-verdade;
# wiki = gerado. Espelha schema.md §1.
STORE_TREE = (
    "inbox/transcripts", "inbox/agent-output", "inbox/code-notes", "inbox/clipped",
    "raw/transcripts", "raw/docs", "raw/code-notes", "raw/agent-output",
    "wiki",
)
STORE_FILES = ("log.md", "quarantine.md")


def cmd_store(a) -> int:
    """Cria a estrutura do store. Idempotente: não toca no que já existe.

    Substitui o loop de shell do INSTALL — um executável que não cria a própria
    pasta de dados é pedir para a pessoa errar a mão no `mkdir`.
    """
    if a.store_cmd != "init":
        print(json.dumps({"error": "uso: wk store init [caminho]"}), file=sys.stderr)
        return 2
    root = os.path.abspath(a.path)
    criados, existentes = [], []
    for d in STORE_TREE:
        p = os.path.join(root, d)
        (existentes if os.path.isdir(p) else criados).append(d)
        os.makedirs(p, exist_ok=True)
    for fn in STORE_FILES:
        p = os.path.join(root, fn)
        if os.path.exists(p):
            existentes.append(fn)
        else:
            open(p, "a", encoding="utf-8").close()
            criados.append(fn)
    print(
        json.dumps(
            {"store": root, "criados": criados, "ja_existiam": existentes,
             "dica": f"aponte o agente para este store, ou WK_STORE={root}"},
            ensure_ascii=False, indent=2,
        )
    )
    return 0


# ---------- fluxos reais do wiki ----------


RAW_DIR_BY_SOURCE_TYPE = {
    "code-repo": "code-notes",
    "human-transcript": "transcripts",
    "human-doc": "docs",
    "web-clip": "docs",
    "agent-output": "agent-output",
}

# Mapeamento equivalente para `inbox/` (usado por `ingest` e `publish`). Os
# quatro diretórios físicos de inbox/ (schema.md §1) não têm uma pasta 1:1 por
# source_type: human-doc entra em transcripts/ (matéria-prima humana, como
# human-transcript) e web-clip em clipped/. Os 5 tipos de VALID_SOURCE_TYPES
# (sbindex/frontmatter.py) ficam cobertos.
INBOX_DIR_BY_SOURCE_TYPE = {
    "code-repo": "code-notes",
    "agent-output": "agent-output",
    "human-transcript": "transcripts",
    "human-doc": "transcripts",
    "web-clip": "clipped",
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _store_root(a) -> str:
    return os.path.abspath(a.store or os.environ.get("WK_STORE") or "./store")


def _read_md(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _write_md(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def _walk_md(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.endswith(".md") and not fn.startswith("."):
                yield os.path.join(dirpath, fn)


def _inbox_candidates(store_root: str) -> list[str]:
    root = os.path.join(store_root, "inbox")
    return list(_walk_md(root)) if os.path.isdir(root) else []


def _resolve_promote_targets(store_root: str, target: str | None) -> tuple[list[str], str | None]:
    candidates = _inbox_candidates(store_root)
    if not target:
        return candidates, None

    direct = os.path.abspath(target)
    if os.path.isfile(direct):
        inbox = os.path.abspath(os.path.join(store_root, "inbox"))
        try:
            common = os.path.commonpath([inbox, direct])
        except ValueError:
            common = ""
        if common != inbox:
            return [], "promote só aceita arquivos dentro de inbox/"
        return [direct], None

    rel = os.path.abspath(os.path.join(store_root, "inbox", target))
    if os.path.isfile(rel):
        return [rel], None

    from sbindex.frontmatter import split

    hits = []
    for path in candidates:
        meta, _ = split(_read_md(path))
        if meta.get("id") == target:
            hits.append(path)
    if not hits:
        return [], f"candidato não encontrado em inbox/: {target}"
    return hits, None


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) and v in (0, 1):
        return "true" if v else "false"
    return json.dumps(str(v), ensure_ascii=False)


def _render_frontmatter(meta: dict, body: str) -> str:
    from sbindex.frontmatter import FIELDS

    lines = ["---"]
    for key in FIELDS:
        value = meta.get(key)
        if key == "sources":
            values = value or []
            if values:
                lines.append(
                    f"{key}: ["
                    + ", ".join(json.dumps(str(x), ensure_ascii=False) for x in values)
                    + "]"
                )
            continue
        if value is not None:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")


def _unique_dest(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = f"{base}-{i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def _append_log(store_root: str, line: str) -> None:
    path = os.path.join(store_root, "log.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line.rstrip() + "\n")


def _append_quarantine(store_root: str, items: list[dict]) -> None:
    if not items:
        return
    path = os.path.join(store_root, "quarantine.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"\n## {_utc_now()} promote\n")
        for item in items:
            f.write(
                f"- {item.get('id') or '(sem-id)'} | {item.get('path')} | "
                f"{item.get('motivo')}\n"
            )


def _run_reindex(store_root: str) -> tuple[bool, str | None]:
    from sbindex.cli import main as sbindex_main

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = sbindex_main(["--store", store_root, "reindex", "--lex-only"])
    if code:
        return False, (stderr.getvalue() or stdout.getvalue()).strip()
    return True, None


def _collect_approved_paths(
    store_root: str, targets: list[str], target_spec: str | None, source_type: str | None
) -> tuple[set[str], list[str], str | None]:
    """Resolve `--approve <alvo>` ou `--approve-all --source-type <t>`.

    Devolve (caminhos_aprovados, targets_atualizado, erro). Itens aprovados que
    não estavam no escopo original de `targets` (ex.: `--approve-all` sem alvo
    posicional) são adicionados — aprovação explícita amplia o escopo, nunca
    reduz o que já seria escaneado.
    """
    from sbindex.frontmatter import split

    approved: set[str] = set()
    existing = {os.path.abspath(t) for t in targets}

    if target_spec:
        resolved, err = _resolve_promote_targets(store_root, target_spec)
        if err:
            return set(), targets, err
        for p in resolved:
            ap = os.path.abspath(p)
            approved.add(ap)
            if ap not in existing:
                targets.append(p)
                existing.add(ap)

    if source_type:
        for p in _inbox_candidates(store_root):
            meta, _ = split(_read_md(p))
            if meta.get("source_type") != source_type:
                continue
            ap = os.path.abspath(p)
            approved.add(ap)
            if ap not in existing:
                targets.append(p)
                existing.add(ap)

    return approved, targets, None


def cmd_promote(a) -> int:
    from sbindex.frontmatter import provenance_gaps, split

    store_root = _store_root(a)
    targets, err = _resolve_promote_targets(store_root, a.target)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False), file=sys.stderr)
        return 2

    if a.approve_all and not a.approve_source_type:
        print(
            json.dumps({"error": "--approve-all exige --source-type"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    approved_paths, targets, err2 = _collect_approved_paths(
        store_root, targets, a.approve, a.approve_source_type if a.approve_all else None
    )
    if err2:
        print(json.dumps({"error": err2}, ensure_ascii=False), file=sys.stderr)
        return 2

    approved_by = a.approved_by or "humano"

    promoted: list[dict] = []
    human: list[dict] = []
    quarantine: list[dict] = []
    approval_lines: list[str] = []

    for path in targets:
        text = _read_md(path)
        meta, body = split(text)
        source_id = meta.get("id")
        source_type = meta.get("source_type")
        gaps = provenance_gaps(meta)
        rel = os.path.relpath(path, store_root).replace("\\", "/")

        if gaps:
            quarantine.append(
                {"id": source_id, "path": rel, "motivo": "; ".join(gaps)}
            )
            continue

        is_approved = os.path.abspath(path) in approved_paths

        if source_type == "code-repo":
            promoted_by, confidence = "wiki-ai", "reviewed"
        elif is_approved:
            promoted_by = approved_by
            confidence = "unverified" if source_type == "agent-output" else "reviewed"
        else:
            motivo = (
                "agent-output nunca é auto-promovido; use --approve para aprovação humana"
                if source_type == "agent-output"
                else "sem regra determinística segura para auto-promoção; use --approve para aprovação humana"
            )
            human.append(
                {"id": source_id, "path": rel, "source_type": source_type, "motivo": motivo}
            )
            continue

        meta.update(
            {
                "promoted": True,
                "promoted_by": promoted_by,
                "promoted_at": _utc_now(),
                "confidence": confidence,
            }
        )
        dest_dir = os.path.join(store_root, "raw", RAW_DIR_BY_SOURCE_TYPE[source_type])
        dest = _unique_dest(os.path.join(dest_dir, os.path.basename(path)))
        _write_md(dest, _render_frontmatter(meta, body))
        os.remove(path)
        promoted.append(
            {
                "id": source_id,
                "path": os.path.relpath(dest, store_root).replace("\\", "/"),
                "source_type": source_type,
                "confidence": confidence,
            }
        )
        if is_approved and source_type != "code-repo":
            approval_lines.append(
                f"- aprovado por {promoted_by}: {source_id or '(sem-id)'} "
                f"({source_type}) | {rel}"
            )

    _append_quarantine(store_root, quarantine)
    if promoted:
        _append_log(
            store_root,
            f"## [{_utc_now()}] promote | {len(promoted)} promovidos | wiki-ai",
        )
    if approval_lines:
        _append_log(
            store_root,
            f"## [{_utc_now()}] promote --approve | {len(approval_lines)} aprovados | {approved_by}",
        )
        for line in approval_lines:
            _append_log(store_root, line)
    reindexed = False
    reindex_error = None
    if promoted:
        reindexed, reindex_error = _run_reindex(store_root)

    out = {
        "promovidos": promoted,
        "decisao_humana": human,
        "quarentena": quarantine,
        "reindexed": reindexed,
    }
    if reindex_error:
        out["reindex_error"] = reindex_error
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if reindex_error else 0


def _slug(s: str) -> str:
    s = (s or "geral").strip().lower()
    s = re.sub(r"[^a-z0-9._/-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-/")
    return s or "geral"


def _promoted_raw_sources(store_root: str, topic: str | None) -> list[dict]:
    from sbindex.frontmatter import split

    out = []
    root = os.path.join(store_root, "raw")
    for path in _walk_md(root) if os.path.isdir(root) else []:
        meta, body = split(_read_md(path))
        if meta.get("promoted") != 1:
            continue
        if topic and meta.get("topic") != topic:
            continue
        out.append(
            {
                "id": meta.get("id") or os.path.splitext(os.path.basename(path))[0],
                "source_type": meta.get("source_type") or "sem-tipo",
                "topic": meta.get("topic") or "geral",
                "origin": meta.get("origin"),
                "confidence": meta.get("confidence"),
                "captured_at": meta.get("captured_at"),
                "path": path,
                "rel": os.path.relpath(path, store_root).replace("\\", "/"),
                "body": body.strip(),
            }
        )
    return out


def _frontmatter_for_wiki(page_id: str, topic: str, source_ids: list[str]) -> str:
    return _render_frontmatter(
        {"id": page_id, "topic": topic, "sources": source_ids},
        "",
    ).rstrip()


def cmd_compile(a) -> int:
    """Uma página por documento promovido: wiki/<topic>/<source_type>/<id>.md.

    Granularidade por grupo (topic, source_type) colapsava dezenas de módulos
    e specs numa página só; aqui cada fonte promovida vira sua própria página,
    e `wiki/index.md` agrega todas com link.
    """
    store_root = _store_root(a)
    sources = _promoted_raw_sources(store_root, a.topic)
    wiki_root = os.path.join(store_root, "wiki")
    os.makedirs(wiki_root, exist_ok=True)

    pages = []
    page_entries: list[tuple[dict, str]] = []

    for s in sources:
        topic_slug = _slug(s["topic"])
        type_slug = _slug(s["source_type"])
        id_slug = _slug(str(s["id"] or "sem-id"))
        page_id = f"wiki-{topic_slug}-{type_slug}-{id_slug}"
        path = os.path.join(wiki_root, topic_slug, type_slug, f"{id_slug}.md")
        body = [
            f"# {s['id']}",
            "",
            f"- topic: `{s['topic']}`",
            f"- source_type: `{s['source_type']}`",
            f"- confidence: `{s['confidence'] or ''}`",
            f"- origem: {s['origin'] or ''}",
            f"- fonte: `{s['rel']}`",
            "",
            "## Conteúdo",
            "",
            s["body"],
            "",
        ]
        text = _frontmatter_for_wiki(page_id, s["topic"], [str(s["id"])] if s.get("id") else [])
        text += "\n" + "\n".join(body).rstrip() + "\n"
        _write_md(path, text)
        rel_page = os.path.relpath(path, store_root).replace("\\", "/")
        pages.append(rel_page)
        page_entries.append((s, rel_page))

    source_ids = [str(s["id"]) for s in sources if s.get("id")]
    compile_ts = _utc_now()

    by_topic: dict[str, list[tuple[dict, str]]] = {}
    for s, rel_page in page_entries:
        by_topic.setdefault(s["topic"], []).append((s, rel_page))

    index_body = ["# Wiki Index", ""]
    if a.topic:
        index_body.extend([f"Tópico compilado: {a.topic}", ""])
    index_body.append(f"Total: {len(page_entries)} páginas em {len(by_topic)} tópico(s).")
    index_body.append("")
    for topic in sorted(by_topic):
        entries = by_topic[topic]
        index_body.append(f"## {topic} ({len(entries)})")
        index_body.append("")
        for s, rel_page in entries:
            atualizado = s.get("captured_at") or compile_ts
            index_body.append(f"- [{s['id']}]({rel_page}) | `{s['source_type']}` | atualizado {atualizado}")
        index_body.append("")
    index_text = _frontmatter_for_wiki("wiki-index", a.topic or "index", source_ids)
    index_text += "\n" + "\n".join(index_body).rstrip() + "\n"
    index_path = os.path.join(wiki_root, "index.md")
    _write_md(index_path, index_text)
    pages.insert(0, os.path.relpath(index_path, store_root).replace("\\", "/"))

    reindexed, reindex_error = _run_reindex(store_root)
    _append_log(
        store_root,
        f"## [{_utc_now()}] compile | {len(pages)} páginas | {len(sources)} fontes",
    )
    out = {
        "paginas": pages,
        "fontes": len(sources),
        "reindexed": reindexed,
    }
    if reindex_error:
        out["reindex_error"] = reindex_error
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if reindex_error else 0


def _audit_index(store_root: str, path_filter: str | None = None) -> dict:
    from sbindex import store
    from sbindex.cli import AUDIT_SQL

    conn = store.connect(os.path.join(store_root, "index.db"))
    rules = {}
    total = 0
    for rule, sql in AUDIT_SQL.items():
        rows = []
        for r in conn.execute(sql).fetchall():
            item = {"docid": "#" + r["docid"], "path": r["path"], "detalhe": r["detalhe"]}
            if path_filter and path_filter not in item["path"]:
                continue
            rows.append(item)
        rules[rule] = {"n": len(rows), "itens": rows}
        total += len(rows)
    conn.close()
    return {"achados": total, "regras": rules}


_WIKI_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_WIKI_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _audit_wiki_links(store_root: str, path_filter: str | None = None) -> dict:
    """W1/W2/W3: travessia determinística de `wiki/**/*.md`, sem SQL.

    W1 link markdown relativo cujo alvo não existe em disco; W2 wikilink
    `[[nome]]` sem página correspondente (por stem do arquivo ou `id` do
    frontmatter, case-insensitive); W3 página (fora de index.md/
    _lint-report.md) sem nenhum link de entrada (markdown ou wikilink) vindo
    de outra página.
    """
    from sbindex.frontmatter import split

    wiki_root = os.path.join(store_root, "wiki")
    paths = list(_walk_md(wiki_root)) if os.path.isdir(wiki_root) else []

    pages = []
    stems_lower: dict[str, str] = {}
    ids_lower: dict[str, str] = {}
    for path in paths:
        meta, body = split(_read_md(path))
        stem = os.path.splitext(os.path.basename(path))[0]
        rel = os.path.relpath(path, store_root).replace("\\", "/")
        pid = meta.get("id")
        pages.append({"path": path, "rel": rel, "body": body})
        stems_lower[stem.lower()] = path
        if pid:
            ids_lower[str(pid).lower()] = path

    incoming: set[str] = set()
    w1_items: list[dict] = []
    w2_items: list[dict] = []
    for page in pages:
        page_dir = os.path.dirname(page["path"])
        for raw in _WIKI_MD_LINK_RE.findall(page["body"]):
            target = raw.strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = target.split("#", 1)[0].strip()
            if not target_path:
                continue
            resolved = os.path.normpath(os.path.join(page_dir, target_path))
            if not os.path.exists(resolved):
                item = {"docid": "-", "path": page["rel"], "detalhe": f"link quebrado: {target}"}
                if not path_filter or path_filter in item["path"]:
                    w1_items.append(item)
            else:
                incoming.add(os.path.normcase(resolved))
        for name in _WIKI_WIKILINK_RE.findall(page["body"]):
            key = name.strip().lower()
            target_page = ids_lower.get(key) or stems_lower.get(key)
            if target_page is None:
                item = {
                    "docid": "-", "path": page["rel"],
                    "detalhe": f"wikilink sem página: [[{name.strip()}]]",
                }
                if not path_filter or path_filter in item["path"]:
                    w2_items.append(item)
            else:
                incoming.add(os.path.normcase(target_page))

    w3_items: list[dict] = []
    for page in pages:
        if os.path.basename(page["path"]) in ("index.md", "_lint-report.md"):
            continue
        if os.path.normcase(page["path"]) not in incoming:
            item = {"docid": "-", "path": page["rel"], "detalhe": "página órfã: sem link de entrada"}
            if not path_filter or path_filter in item["path"]:
                w3_items.append(item)

    regras = {
        "W1_link_quebrado": {"n": len(w1_items), "itens": w1_items},
        "W2_wikilink_sem_pagina": {"n": len(w2_items), "itens": w2_items},
        "W3_pagina_orfa": {"n": len(w3_items), "itens": w3_items},
    }
    return {"achados": len(w1_items) + len(w2_items) + len(w3_items), "regras": regras}


def _write_lint_report(store_root: str, report: dict) -> str:
    path = os.path.join(store_root, "wiki", "_lint-report.md")
    lines = [
        "---",
        'id: "wiki-lint-report"',
        'topic: "lint"',
        "---",
        "",
        "# Lint Report",
        "",
        f"Gerado em: {_utc_now()}",
        "",
        f"Achados: {report['achados']}",
        "",
    ]
    for rule, data in report["regras"].items():
        lines.extend([f"## {rule}", "", f"Total: {data['n']}", ""])
        for item in data["itens"]:
            lines.append(f"- {item['docid']} | `{item['path']}` | {item['detalhe']}")
        lines.append("")
    _write_md(path, "\n".join(lines).rstrip() + "\n")
    return os.path.relpath(path, store_root).replace("\\", "/")


def cmd_lint(a) -> int:
    store_root = _store_root(a)
    report = _audit_index(store_root, a.path)
    wiki_report = _audit_wiki_links(store_root, a.path)
    report["regras"].update(wiki_report["regras"])
    report["achados"] += wiki_report["achados"]
    report_path = _write_lint_report(store_root, report)
    out = {
        "achados": report["achados"],
        "regras": {k: v["n"] for k, v in report["regras"].items()},
        "report": report_path,
        "reindexed": False,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if report["achados"] else 0


# ---------- ingest: converte arquivo externo em inbox/ ----------


_INGEST_MD_EXTS = {".md"}
_INGEST_TEXT_EXTS = {".txt"}
_INGEST_SUBTITLE_EXTS = {".vtt", ".srt"}
_INGEST_HTML_EXTS = {".html", ".htm"}
_INGEST_CODEBLOCK_EXTS = {".xml", ".json"}
_INGEST_SUPPORTED_EXTS = (
    _INGEST_MD_EXTS | _INGEST_TEXT_EXTS | _INGEST_SUBTITLE_EXTS
    | _INGEST_HTML_EXTS | _INGEST_CODEBLOCK_EXTS
)

# Fontes binárias/planilha: não há conversão mecânica sensata (llm-wiki manda
# guardar o original imutável e deixar a análise para o LLM). Tratadas à parte
# em `_ingest_asset`, nunca passam por `_convert_to_markdown`.
_INGEST_ASSET_EXTS = {".docx", ".xlsx", ".csv", ".pdf"}

_SUBTITLE_TS_RE = re.compile(r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}")
_HTML_HEADING_RE = re.compile(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>")
_HTML_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_HTML_BR_RE = re.compile(r"(?i)<br\s*/?>")
_HTML_P_CLOSE_RE = re.compile(r"(?i)</p\s*>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _convert_subtitles(text: str) -> str:
    """.vtt/.srt -> markdown preservando os timestamps como texto literal."""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for ln in norm.split("\n"):
        if ln.strip() == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(ln)
    if current:
        blocks.append(current)

    out: list[str] = []
    for block in blocks:
        if block and block[0].strip().upper() == "WEBVTT":
            continue
        ts_idx = next((i for i, ln in enumerate(block) if _SUBTITLE_TS_RE.search(ln)), None)
        if ts_idx is None:
            out.append(" ".join(ln.strip() for ln in block).strip())
            out.append("")
            continue
        out.append(f"**{block[ts_idx].strip()}**")
        out.append("")
        out.extend(ln.strip() for ln in block[ts_idx + 1:])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _convert_html(text: str) -> str:
    """.html -> markdown: tags removidas, headings (h1-h6) preservados como `#`."""
    s = _HTML_SCRIPT_STYLE_RE.sub("", text)

    def _heading(m: re.Match) -> str:
        level = int(m.group(1))
        inner = _HTML_TAG_RE.sub("", m.group(2)).strip()
        return f"\n{'#' * level} {inner}\n"

    s = _HTML_HEADING_RE.sub(_heading, s)
    s = _HTML_BR_RE.sub("\n", s)
    s = _HTML_P_CLOSE_RE.sub("\n\n", s)
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _BLANK_RUN_RE.sub("\n\n", s)
    return s.strip() + "\n"


def _convert_codeblock(text: str, ext: str) -> str:
    """.xml/.json -> bloco de código com nota do formato original."""
    lang = ext.lstrip(".")
    note = f"> Conteúdo original em {lang.upper()}, preservado em bloco de código.\n\n"
    return note + f"```{lang}\n{text.rstrip()}\n```\n"


def _xml_local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_ns_attr(el: ET.Element, local_name: str) -> str | None:
    """Acha um atributo por nome local, ignorando prefixo/URI de namespace."""
    for k, v in el.attrib.items():
        if k == local_name or k.endswith("}" + local_name) or k.endswith(":" + local_name):
            return v
    return None


def _xml_ref_local(value: str | None) -> str:
    """`ns:id`/`#id`/`id` -> `id`, para exibir referências XMI de forma legível."""
    if not value:
        return value or ""
    if "#" in value:
        return value.rsplit("#", 1)[-1]
    if ":" in value:
        return value.rsplit(":", 1)[-1]
    return value


_XMI_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_ascii_ident(name: str, fallback: str) -> str:
    base = _XMI_IDENT_RE.sub("_", name or "").strip("_")
    if not base:
        base = _XMI_IDENT_RE.sub("_", fallback or "x").strip("_") or "x"
    if base[0].isdigit():
        base = f"c_{base}"
    return base


def _clean_drawio_label(value: str | None) -> str:
    if not value:
        return ""
    v = _HTML_BR_RE.sub(" ", value)
    v = _HTML_TAG_RE.sub("", v)
    v = html.unescape(v)
    return " ".join(v.split()).strip()


def _mermaid_flowchart_label(text: str) -> str:
    return text.replace('"', "'") if text else ""


def _convert_drawio(root: ET.Element) -> str:
    """draw.io (`mxfile`/`mxGraphModel`) -> componentes/relações + Mermaid flowchart."""
    vertices: list[tuple[str, str]] = []
    edges: list[tuple[str, str]] = []
    for cell in root.iter():
        if _xml_local_tag(cell.tag) != "mxCell":
            continue
        if cell.get("vertex") == "1":
            vertices.append((cell.get("id") or "", _clean_drawio_label(cell.get("value"))))
        elif cell.get("edge") == "1":
            edges.append((cell.get("source") or "", cell.get("target") or ""))

    id_to_label = {vid: (label or vid) for vid, label in vertices if vid}

    lines = ["# Arquitetura (draw.io)", "", "## Componentes", "", "| id | rótulo |", "|---|---|"]
    for vid, label in vertices:
        lines.append(f"| `{vid}` | {label or '(sem rótulo)'} |")
    lines.append("")

    lines.append("## Relações")
    lines.append("")
    lines.append("| origem | destino |")
    lines.append("|---|---|")
    for src, dst in edges:
        lines.append(f"| {id_to_label.get(src, src) or '(?)'} | {id_to_label.get(dst, dst) or '(?)'} |")
    lines.append("")

    valid_edges = [(s, d) for s, d in edges if s in id_to_label and d in id_to_label]
    if len(vertices) >= 2 and valid_edges:
        mermaid_id = {vid: f"n{i + 1}" for i, (vid, _label) in enumerate(vertices) if vid}
        lines.append("```mermaid")
        lines.append("flowchart LR")
        for vid, label in vertices:
            if vid not in mermaid_id:
                continue
            lbl = _mermaid_flowchart_label(label) or vid
            lines.append(f'    {mermaid_id[vid]}["{lbl}"]')
        for src, dst in valid_edges:
            lines.append(f"    {mermaid_id[src]} --> {mermaid_id[dst]}")
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _is_xmi(root: ET.Element) -> bool:
    if _xml_local_tag(root.tag).upper() == "XMI":
        return True
    return any(_xml_local_tag(el.tag) == "packagedElement" for el in root.iter())


def _convert_xmi(root: ET.Element) -> str:
    """XMI/UML (`XMI`/`packagedElement`) -> classes/associações + Mermaid classDiagram."""
    classes: list[tuple[str, str, list[tuple[str, str]]]] = []
    id_to_name: dict[str, str] = {}
    prop_type_by_id: dict[str, str] = {}
    assoc_elements: list[ET.Element] = []

    for el in root.iter():
        if _xml_local_tag(el.tag) != "packagedElement":
            continue
        etype = _xml_ref_local(_xml_ns_attr(el, "type") or "")
        eid = _xml_ns_attr(el, "id") or el.get("id") or ""
        if etype.endswith("Class"):
            name = el.get("name") or eid or "(sem nome)"
            attrs: list[tuple[str, str]] = []
            for child in el:
                if _xml_local_tag(child.tag) != "ownedAttribute":
                    continue
                aname = child.get("name") or "(sem nome)"
                atype_raw = child.get("type")
                if atype_raw:
                    atype = _xml_ref_local(atype_raw)
                else:
                    atype = ""
                    for sub in child:
                        if _xml_local_tag(sub.tag) == "type":
                            idref = _xml_ns_attr(sub, "idref") or sub.get("href") or ""
                            atype = _xml_ref_local(idref)
                            break
                attrs.append((aname, atype))
                cid = _xml_ns_attr(child, "id") or child.get("id")
                if cid and eid:
                    prop_type_by_id[cid] = eid
            classes.append((eid, name, attrs))
            if eid:
                id_to_name[eid] = name
        elif etype.endswith("Association"):
            assoc_elements.append(el)

    def resolve_end(idref: str) -> str:
        if idref in id_to_name:
            return id_to_name[idref]
        classid = prop_type_by_id.get(idref)
        if classid and classid in id_to_name:
            return id_to_name[classid]
        return idref

    associations: list[tuple[str, list[str]]] = []
    for el in assoc_elements:
        name = el.get("name") or ""
        ends: list[str] = []
        for idref in (el.get("memberEnd") or "").split():
            ends.append(resolve_end(idref))
        for child in el:
            ctag = _xml_local_tag(child.tag)
            if ctag == "memberEnd":
                idref = _xml_ns_attr(child, "idref")
                if idref:
                    ends.append(resolve_end(idref))
            elif ctag == "ownedEnd":
                atype_raw = child.get("type")
                if atype_raw:
                    ends.append(resolve_end(_xml_ref_local(atype_raw)))
        seen: set[str] = set()
        uniq_ends = [e for e in ends if e and not (e in seen or seen.add(e))]
        associations.append((name, uniq_ends))

    lines = ["# Arquitetura (XMI/UML)", "", "## Classes", "", "| classe | atributos |", "|---|---|"]
    for _eid, name, attrs in classes:
        attr_txt = "; ".join(f"{n}: {t}" if t else n for n, t in attrs) or "(sem atributos)"
        lines.append(f"| {name} | {attr_txt} |")
    lines.append("")

    lines.append("## Associações")
    lines.append("")
    lines.append("| associação | extremidades |")
    lines.append("|---|---|")
    for name, ends in associations:
        lines.append(f"| {name or '(sem nome)'} | {' — '.join(ends) if ends else '(indefinido)'} |")
    lines.append("")

    if classes:
        used_idents: set[str] = set()
        ident_by_name: dict[str, str] = {}
        mermaid_lines = ["```mermaid", "classDiagram"]
        for _eid, name, _attrs in classes:
            base = _sanitize_ascii_ident(name, "Classe")
            ident = base
            i = 2
            while ident in used_idents:
                ident = f"{base}_{i}"
                i += 1
            used_idents.add(ident)
            ident_by_name[name] = ident
            mermaid_lines.append(f"    class {ident}")
        for _name, ends in associations:
            for i in range(len(ends) - 1):
                a = ident_by_name.get(ends[i])
                b = ident_by_name.get(ends[i + 1])
                if a and b:
                    mermaid_lines.append(f"    {a} -- {b}")
        mermaid_lines.append("```")
        lines.extend(mermaid_lines)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _xml_outline(root: ET.Element, max_depth: int = 4, max_lines: int = 80) -> list[str]:
    lines: list[str] = []

    def walk(el: ET.Element, depth: int) -> None:
        if len(lines) >= max_lines or depth > max_depth:
            return
        attrs = list(el.attrib.items())[:3]
        attr_str = " ".join(f'{k}="{v}"' for k, v in attrs)
        tag = _xml_local_tag(el.tag)
        line = "  " * depth + f"- <{tag}"
        if attr_str:
            line += f" {attr_str}"
        line += ">"
        lines.append(line)
        for child in el:
            if len(lines) >= max_lines:
                return
            walk(child, depth + 1)

    walk(root, 0)
    return lines[:max_lines]


def _convert_xml_fallback(text: str, root: ET.Element) -> str:
    lines = ["# Estrutura XML", "", "## Outline", ""]
    lines.extend(_xml_outline(root))
    lines.append("")
    lines.append("## Original")
    lines.append("")
    lines.append("```xml")
    lines.append(text.rstrip())
    lines.append("```")
    return "\n".join(lines).rstrip() + "\n"


def _convert_xml_architecture(text: str) -> str:
    """.xml -> markdown estrutural (draw.io/XMI) ou outline (fallback).

    Em erro de parse, cai no comportamento antigo (`_convert_codeblock`): o
    original vira bloco de código literal, nada se perde.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return _convert_codeblock(text, ".xml")

    if _xml_local_tag(root.tag) == "mxfile" or any(
        _xml_local_tag(el.tag) == "mxGraphModel" for el in root.iter()
    ):
        return _convert_drawio(root)
    if _is_xmi(root):
        return _convert_xmi(root)
    return _convert_xml_fallback(text, root)


def _convert_to_markdown(path: str) -> tuple[str, str | None]:
    """(markdown, erro). `erro` != None => fora de escopo, nada deve ser escrito."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in _INGEST_SUPPORTED_EXTS:
        suportados = sorted(_INGEST_SUPPORTED_EXTS | _INGEST_ASSET_EXTS)
        return "", (
            f"formato fora de escopo: '{ext or '(sem extensão)'}'. "
            f"suportados (conversão): {sorted(_INGEST_SUPPORTED_EXTS)}; "
            f"suportados (asset imutável): {sorted(_INGEST_ASSET_EXTS)}; total: {suportados}"
        )
    text = _read_md(path)
    if ext in _INGEST_MD_EXTS or ext in _INGEST_TEXT_EXTS:
        return text, None
    if ext in _INGEST_SUBTITLE_EXTS:
        return _convert_subtitles(text), None
    if ext in _INGEST_HTML_EXTS:
        return _convert_html(text), None
    if ext == ".xml":
        return _convert_xml_architecture(text), None
    if ext in _INGEST_CODEBLOCK_EXTS:
        return _convert_codeblock(text, ext), None
    return "", f"formato fora de escopo: '{ext}'"  # pragma: no cover — defensivo


_INGEST_RESERVED_WORDS = {"codebase", "repo", "repositorio", "repository"}
_INGEST_FLAGS_WITH_VALUE = ("--source-type", "--origin", "--topic", "--captured-at", "--store")


def _ingest_first_positional(argv: list[str]) -> str | None:
    """Acha o primeiro positional de `ingest` (ignora flags e seus valores).

    `argv` aqui é o resto após o token `ingest` (ex.: ["codebase", "<repo>",
    "--topic", "demo"]). Roda ANTES do argparse de propósito: o cenário real
    que motivou a guarda é justamente argv incompleto (`--source-type`/
    `--origin` ausentes), que faria argparse abortar com "arguments required"
    antes de qualquer checagem de palavra reservada rodar.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _INGEST_FLAGS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("--"):
            i += 1  # --flag=valor ou flag desconhecida: pula só o token
            continue
        return tok
    return None


def _ingest_reserved_guard(argv: list[str]) -> dict | None:
    """Bloqueia `wk ingest codebase|repo|repositorio|repository ...`.

    Essas palavras são a operação `ingest codebase` da skill — o pipeline
    `wk code ... surface`, que não é (e não deve virar) subcomando deste CLI.
    Um arquivo real com um desses nomes (existe em disco) continua ingerível
    normalmente: a guarda só dispara quando o positional NÃO é um caminho
    existente.
    """
    target = _ingest_first_positional(argv)
    if target is None or target not in _INGEST_RESERVED_WORDS or os.path.isfile(target):
        return None
    return {
        "error": (
            f"'{target}' não é um subcomando de `wk ingest` — é a operação "
            "`ingest codebase` da skill (pipeline de repositório), que roda "
            "fora deste CLI, não como `wk ingest codebase ...`."
        ),
        "comando_correto": "wk code --repo <caminho> --store <store> surface --topic <slug>",
    }


def _ingest_asset(
    file_path: str, ext: str, store_root: str, source_type: str, origin: str,
    captured_at: str, topic, doc_id: str,
) -> tuple[str, str]:
    """Guarda o original imutável em `raw/assets/` e cria a página de fonte no inbox/.

    llm-wiki manda preservar a fonte binária/planilha tal como recebida — sem
    conversão mecânica. A análise é tarefa do LLM: lê o asset, escreve a
    página de análise e a ingere via `--source-type agent-output`.
    """
    assets_dir = os.path.join(store_root, "raw", "assets")
    os.makedirs(assets_dir, exist_ok=True)
    asset_dest = os.path.join(assets_dir, f"{doc_id}{ext}")
    shutil.copyfile(file_path, asset_dest)
    asset_rel = os.path.relpath(asset_dest, store_root).replace("\\", "/")

    filename = os.path.basename(file_path)
    meta = {
        "id": doc_id,
        "source_type": source_type,
        "origin": origin,
        "captured_at": captured_at,
        "promoted": False,
        "topic": topic,
    }
    body = "\n".join([
        f"# Fonte: {filename}",
        "",
        f"Original imutável: {asset_rel}",
        "",
        "## Análise pendente",
        "",
        "O agente deve ler o original acima, escrever a página de análise "
        "(pontos-chave, resumo, decisões) e ingeri-la via "
        "`wk ingest --source-type agent-output`.",
        "",
    ])
    dest_dir = os.path.join(store_root, "inbox", INBOX_DIR_BY_SOURCE_TYPE[source_type])
    dest = _unique_dest(os.path.join(dest_dir, f"{doc_id}.md"))
    _write_md(dest, _render_frontmatter(meta, body))
    return dest, asset_rel


def cmd_ingest(a) -> int:
    from sbindex.frontmatter import VALID_SOURCE_TYPES

    if not os.path.isfile(a.file):
        print(json.dumps({"error": f"arquivo não encontrado: {a.file}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if a.source_type not in VALID_SOURCE_TYPES:
        print(
            json.dumps(
                {"error": f"source_type inválido: {a.source_type}",
                 "validos": sorted(VALID_SOURCE_TYPES)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    origin = (a.origin or "").strip()
    if not origin:
        print(json.dumps({"error": "--origin não pode ser vazio"}, ensure_ascii=False), file=sys.stderr)
        return 2

    store_root = _store_root(a)
    captured_at = a.captured_at or _utc_now()
    stem = os.path.splitext(os.path.basename(a.file))[0]
    token = hashlib.sha1(
        f"{os.path.abspath(a.file)}|{captured_at}".encode("utf-8")
    ).hexdigest()[:8]
    doc_id = f"sb-ingest-{_slug(stem)}-{token}"

    ext = os.path.splitext(a.file)[1].lower()
    asset_rel = None
    if ext in _INGEST_ASSET_EXTS:
        dest, asset_rel = _ingest_asset(
            a.file, ext, store_root, a.source_type, origin, captured_at, a.topic, doc_id
        )
    else:
        body, conv_error = _convert_to_markdown(a.file)
        if conv_error:
            print(json.dumps({"error": conv_error}, ensure_ascii=False), file=sys.stderr)
            return 2
        meta = {
            "id": doc_id,
            "source_type": a.source_type,
            "origin": origin,
            "captured_at": captured_at,
            "promoted": False,
            "topic": a.topic,
        }
        dest_dir = os.path.join(store_root, "inbox", INBOX_DIR_BY_SOURCE_TYPE[a.source_type])
        dest = _unique_dest(os.path.join(dest_dir, f"{doc_id}.md"))
        _write_md(dest, _render_frontmatter(meta, body))

    _append_log(
        store_root,
        f"## [{_utc_now()}] ingest | {doc_id} | {a.source_type} | {a.file}",
    )
    out = {
        "id": doc_id,
        "path": os.path.relpath(dest, store_root).replace("\\", "/"),
        "source_type": a.source_type,
    }
    if asset_rel:
        out["asset"] = asset_rel
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------- publish: leva a árvore SDD de um workdir do codescan a inbox/ ----------


_PUBLISH_CODE_REPO_REL = {"sdd/inventory.md", "sdd/dependencies.md", "sdd/coupling.md"}
_PUBLISH_EXCLUDED_DIRNAMES = {"agent-packs", "agent-outputs", "agent-runs"}
_PUBLISH_EXCLUDED_FILENAMES = {"state.json", "surface.json"}


def _publish_candidates(workdir: str) -> list[str]:
    """Arquivos elegíveis: `sdd/**/*.md` e `modules/*.md`, mais confirmed.md/
    inferred.md na raiz do workdir. Nunca `*.py`, `*.txt`, agent-packs/,
    agent-outputs/, agent-runs/, state.json ou surface.json."""
    candidates: list[str] = []

    sdd_root = os.path.join(workdir, "sdd")
    if os.path.isdir(sdd_root):
        for dirpath, dirnames, filenames in os.walk(sdd_root):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in _PUBLISH_EXCLUDED_DIRNAMES
            )
            for fn in sorted(filenames):
                if fn in _PUBLISH_EXCLUDED_FILENAMES:
                    continue
                if os.path.splitext(fn)[1].lower() != ".md":
                    continue
                candidates.append(os.path.join(dirpath, fn))

    modules_root = os.path.join(workdir, "modules")
    if os.path.isdir(modules_root):
        for fn in sorted(os.listdir(modules_root)):
            full = os.path.join(modules_root, fn)
            if not os.path.isfile(full) or fn in _PUBLISH_EXCLUDED_FILENAMES:
                continue
            if os.path.splitext(fn)[1].lower() != ".md":
                continue
            candidates.append(full)

    for fn in ("confirmed.md", "inferred.md"):
        full = os.path.join(workdir, fn)
        if os.path.isfile(full):
            candidates.append(full)

    return candidates


def _publish_source_type(rel: str) -> str:
    return "code-repo" if rel in _PUBLISH_CODE_REPO_REL else "agent-output"


def _repo_name_from_workdir(workdir: str) -> str:
    """Nome do repo p/ origin: lê state.json do codescan; senão, deriva do
    nome do próprio workdir (`<nome>-<hash8>`, ver codescan/state.py:workdir)."""
    state_path = os.path.join(workdir, "state.json")
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                st = json.load(f)
            repo = st.get("repo")
            if repo:
                return os.path.basename(str(repo).rstrip("/\\")) or "repo"
        except (ValueError, OSError):
            pass
    base = os.path.basename(os.path.abspath(workdir).rstrip("/\\"))
    m = re.match(r"^(.*)-[0-9a-f]{8}$", base)
    return (m.group(1) if m else base) or "repo"


def cmd_publish(a) -> int:
    from sbindex.frontmatter import split

    workdir = os.path.abspath(a.workdir)
    if not os.path.isdir(workdir):
        print(json.dumps({"error": f"workdir não encontrado: {a.workdir}"}, ensure_ascii=False), file=sys.stderr)
        return 2

    store_root = _store_root(a)
    inbox_root = os.path.abspath(os.path.join(store_root, "inbox"))
    repo_name = _repo_name_from_workdir(workdir)
    candidates = _publish_candidates(workdir)

    published: list[dict] = []
    for path in candidates:
        rel = os.path.relpath(path, workdir).replace("\\", "/")
        source_type = _publish_source_type(rel)
        dest_dir = os.path.abspath(
            os.path.join(inbox_root, INBOX_DIR_BY_SOURCE_TYPE[source_type])
        )
        try:
            inside_inbox = os.path.commonpath([inbox_root, dest_dir]) == inbox_root
        except ValueError:
            inside_inbox = False
        if not inside_inbox:
            print(
                json.dumps({"error": f"destino fora de inbox/: {dest_dir}"}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2

        text = _read_md(path)
        _, body = split(text)  # descarta frontmatter que o artefato já tivesse
        stem_slug = _slug(os.path.splitext(rel)[0].replace("/", "-"))
        doc_id = f"sb-publish-{_slug(repo_name)}-{stem_slug}"
        meta = {
            "id": doc_id,
            "source_type": source_type,
            "origin": f"codescan {repo_name} — {rel}",
            "captured_at": _utc_now(),
            "promoted": False,
            "topic": a.topic,
        }
        dest = _unique_dest(os.path.join(dest_dir, f"{stem_slug}.md"))
        _write_md(dest, _render_frontmatter(meta, body))
        published.append(
            {
                "id": doc_id,
                "source_type": source_type,
                "path": os.path.relpath(dest, store_root).replace("\\", "/"),
                "origem": rel,
            }
        )

    _append_log(
        store_root,
        f"## [{_utc_now()}] publish | {len(published)} artefatos | workdir {workdir}",
    )
    out = {"workdir": workdir, "topic": a.topic, "publicados": published}
    if not candidates:
        out["aviso"] = "nenhum artefato elegível em sdd/ ou modules/"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------- despacho para as CLIs existentes ----------


def _split_globals(argv: list[str], names: tuple[str, ...]) -> tuple[list[str], dict]:
    """Extrai `--nome V` e `--nome=V` de qualquer posição. Devolve (resto, valores).

    Existe para que `--repo`/`--store` possam vir depois do subcomando, ao
    contrário das CLIs internas que os exigem antes. Ergonomia, não capricho:
    `sb code surface --repo X` é o que qualquer pessoa digita primeiro.
    """
    rest: list[str] = []
    found: dict[str, str] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        matched = False
        for n in names:
            if tok == f"--{n}" and i + 1 < len(argv):
                found[n] = argv[i + 1]
                i += 2
                matched = True
                break
            if tok.startswith(f"--{n}="):
                found[n] = tok.split("=", 1)[1]
                i += 1
                matched = True
                break
        if not matched:
            rest.append(tok)
            i += 1
    return rest, found


def _store(found: dict) -> str:
    return found.get("store") or os.environ.get("WK_STORE") or "./store"


def _is_help_request(argv: list[str]) -> bool:
    return any(tok in ("-h", "--help") for tok in argv)


def _run_code(argv: list[str]) -> int:
    from codescan.cli import main as codescan_main

    rest, g = _split_globals(argv, ("repo", "store"))
    if _is_help_request(rest):
        return codescan_main(["--store", _store(g), *rest])
    repo = g.get("repo") or os.environ.get("WK_REPO")
    if not repo:
        print(
            json.dumps({"error": "informe --repo <caminho> (ou WK_REPO)"}),
            file=sys.stderr,
        )
        return 2
    return codescan_main(["--store", _store(g), "--repo", repo, *rest])


def _run_index(argv: list[str]) -> int:
    from sbindex.cli import main as sbindex_main

    rest, g = _split_globals(argv, ("store",))
    return sbindex_main(["--store", _store(g), *rest])


# ---------- entrada ----------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wk",
        description="Wiki AI — pipeline de conhecimento e análise de codebase",
    )
    p.add_argument("--version", action="version", version=f"wiki-ai {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("docs", help="imprime documentação embutida")
    d.add_argument("name", nargs="?", help="slug do documento (vazio = lista)")
    d.add_argument("--list", action="store_true", help="lista os documentos")
    d.set_defaults(fn=cmd_docs)

    i = sub.add_parser("init", help="materializa a skill em disco por engine")
    i.add_argument("--engine", required=True,
                   help="claude-code | antigravity | devin | copilot | all (vírgula p/ vários)")
    i.add_argument("--base", default=".", help="raiz do projeto (default: .)")
    i.add_argument("--all", action="store_true", help="escreve todos os documentos, não só SKILL.md")
    i.add_argument("--force", action="store_true", help="sobrescreve divergentes")
    i.add_argument("--invocation", help="como chamar o executável (default: auto)")
    i.add_argument("--store", default=None, help="caminho do store; se informado, grava permissões da engine")
    i.add_argument("--repo", default=None, help="caminho do repo; se informado, grava permissões da engine")
    i.set_defaults(fn=cmd_init)

    c = sub.add_parser("check", help="compara disco vs. embutido, por engine")
    c.add_argument("--engine", required=True, help="engine(s) a verificar, ou 'all'")
    c.add_argument("--base", default=".")
    c.add_argument("--all", action="store_true")
    c.add_argument("--invocation")
    c.add_argument("--store", default=None, help="valida config de permissão do store, se informado")
    c.add_argument("--repo", default=None, help="valida config de permissão do repo, se informado")
    c.set_defaults(fn=cmd_check)

    e = sub.add_parser("engines", help="lista engines suportadas e o que já está instalado")
    e.add_argument("--base", default=".")
    e.set_defaults(fn=cmd_engines)

    dr = sub.add_parser(
        "doctor",
        help="diagnóstico único do ambiente (shell, python, wk, store, repo, engine)",
    )
    dr.add_argument("--store", default=None, help="caminho do store a diagnosticar")
    dr.add_argument("--repo", default=None, help="caminho do repo a diagnosticar")
    dr.add_argument("--engine", default=None,
                     help="engine a diagnosticar (default: claude-code)")
    dr.add_argument("--base", default=".", help="raiz do projeto (default: .)")
    dr.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("store", help="cria a estrutura do store (inbox/raw/wiki)")
    s.add_argument("store_cmd", choices=("init",), help="init")
    s.add_argument("path", nargs="?", default="./store", help="caminho (default: ./store)")
    s.set_defaults(fn=cmd_store)

    pr = sub.add_parser("promote", help="promove fontes seguras de inbox/ para raw/")
    pr.add_argument("target", nargs="?", help="id ou caminho em inbox/; vazio varre inbox/**/*.md")
    pr.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    pr.add_argument("--approve", help="id ou caminho em inbox/ a aprovar explicitamente (humano)")
    pr.add_argument("--approve-all", action="store_true", help="aprova todos os itens de --source-type")
    pr.add_argument("--source-type", dest="approve_source_type",
                    help="tipo exigido por --approve-all (human-transcript|human-doc|web-clip|agent-output)")
    pr.add_argument("--approved-by", default="humano", help="quem aprovou; vai para promoted_by")
    pr.set_defaults(fn=cmd_promote)

    co = sub.add_parser("compile", help="compila wiki/ a partir de raw/ promovido")
    co.add_argument("topic", nargs="?", help="tópico opcional")
    co.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    co.set_defaults(fn=cmd_compile)

    li = sub.add_parser("lint", help="audita o índice e escreve wiki/_lint-report.md")
    li.add_argument("path", nargs="?", help="filtro opcional por caminho")
    li.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    li.set_defaults(fn=cmd_lint)

    ig = sub.add_parser("ingest", help="converte arquivo externo em inbox/ com proveniência")
    ig.add_argument("file", help="arquivo a converter (.md/.txt/.vtt/.srt/.html/.xml/.json)")
    ig.add_argument("--source-type", dest="source_type", required=True,
                    help="human-transcript | human-doc | code-repo | agent-output | web-clip")
    ig.add_argument("--origin", required=True, help="proveniência concreta (pessoa, agente, link)")
    ig.add_argument("--topic", required=True, help="tópico do wiki-ai")
    ig.add_argument("--captured-at", dest="captured_at", help="ISO 8601 (default: agora, UTC)")
    ig.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    ig.set_defaults(fn=cmd_ingest)

    pu = sub.add_parser("publish", help="leva a árvore SDD de um workdir do codescan para inbox/")
    pu.add_argument("--workdir", required=True, help="workdir do codescan (.codescan/<repo>-<hash>)")
    pu.add_argument("--topic", required=True, help="tópico do wiki-ai")
    pu.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    pu.set_defaults(fn=cmd_publish)

    # Grupos repassados às CLIs internas: parsing fica com elas.
    for name, help_ in (
        ("code", "pipeline de repositório (exige --repo)"),
        ("index", "manutenção do índice: reindex, status"),
    ):
        g = sub.add_parser(name, help=help_, add_help=False)
        g.add_argument("args", nargs=argparse.REMAINDER)
    for name, help_ in (
        ("search", "busca híbrida no store"),
        ("get", "recupera documento ou trecho"),
        ("audit", "regras determinísticas L1/L2/L5"),
    ):
        g = sub.add_parser(name, help=help_, add_help=False)
        g.add_argument("args", nargs=argparse.REMAINDER)
    return p


_PASSTHROUGH_INDEX = ("search", "get", "audit")


def _force_utf8_stdout() -> None:
    """Docs e JSON carregam 🟢/🟡/🔴 e acento. O console Windows é cp1252 por
    padrão e quebra ao imprimir isso. `wk docs` é o mecanismo de leitura em
    runtime — não pode falhar por encoding do terminal."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            try:
                reconf(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv=None) -> int:
    _force_utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "code":
        return _run_code(argv[1:])
    if argv and argv[0] == "index":
        return _run_index(argv[1:])
    if argv and argv[0] in _PASSTHROUGH_INDEX:
        return _run_index(argv)
    if argv and argv[0] == "ingest":
        guard = _ingest_reserved_guard(argv[1:])
        if guard is not None:
            print(json.dumps(guard, ensure_ascii=False), file=sys.stderr)
            return 2
    a = _build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
