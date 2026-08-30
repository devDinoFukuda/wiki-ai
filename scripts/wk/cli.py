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
import zipfile

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


# ---------- FIX 5: detecta `wk.pyz` desatualizado em relação ao `scripts/` ----------

_BUILD_MANIFEST_NAME = "_build_manifest.json"


def _source_sha256(scripts_dir: str) -> str:
    """Hash agregado determinístico de `wk/`, `codescan/`, `sbindex/` sob
    `scripts_dir` — mesmos pacotes/exclusões (`__pycache__`, `tests`) que
    `build_pyz.py:stage()` empacota no `.pyz`.

    Duplicado de propósito a partir de `build_pyz.py:_source_sha256`: o
    `.pyz` não empacota `build_pyz.py` (só `wk/`, `codescan/`, `sbindex/`),
    então este arquivo — rodando OU de dentro do `.pyz` OU de um `scripts/`
    solto — não pode importá-lo. Qualquer mudança aqui exige a mesma mudança
    lá, senão o comparador de `doctor` nunca bate mesmo com fonte idêntica.
    """
    entries = []
    for pkg in ("wk", "codescan", "sbindex"):
        pkg_dir = os.path.join(scripts_dir, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(pkg_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "tests"))
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, scripts_dir).replace("\\", "/")
                with open(full, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
                entries.append(f"{rel}:{digest}")
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _archive_path() -> str | None:
    """Caminho absoluto do `.pyz` em execução, ou None se rodando de
    código-fonte solto (não de um zipapp) — mesma lógica de descoberta de
    `_self_invocation` (sobe diretórios até achar um caminho existente),
    diferenciando pelo resultado ser ou não um arquivo zip de verdade."""
    archive = getattr(sys.modules.get("__main__"), "__file__", None) or sys.argv[0]
    root = os.environ.get("WK_ARCHIVE") or archive
    while root and not os.path.exists(root):
        parent = os.path.dirname(root)
        if parent == root:
            return None
        root = parent
    if root and os.path.isfile(root) and zipfile.is_zipfile(root):
        return os.path.abspath(root)
    return None


def _check_pyz_freshness(archive: str) -> dict:
    """Compara o `source_sha256` embutido no `.pyz` (`_build_manifest.json`,
    gravado por `build_pyz.py`) com o hash do `scripts/` irmão do arquivo
    `.pyz` em disco (layout padrão do repo: `wk.pyz` na raiz, `scripts/` ao
    lado). Degrada sem alarme falso quando não há fonte ao lado do `.pyz`
    (instalação só-`.pyz`, sem o repo) ou o manifesto não existe (build
    anterior ao FIX 5).
    """
    manifest_raw = _read_asset(_BUILD_MANIFEST_NAME)
    if not manifest_raw:
        return {"pyz": archive, "fonte_disponivel": False, "manifesto_ausente": True}
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except ValueError:
        return {"pyz": archive, "fonte_disponivel": False, "manifesto_ausente": True}
    embedded_hash = manifest.get("source_sha256")
    candidate_scripts = os.path.join(os.path.dirname(archive), "scripts")
    if not embedded_hash or not os.path.isdir(os.path.join(candidate_scripts, "wk")):
        return {"pyz": archive, "fonte_disponivel": False}
    current_hash = _source_sha256(candidate_scripts)
    desatualizado = current_hash != embedded_hash
    out = {
        "pyz": archive,
        "built_at": manifest.get("built_at"),
        "fonte_disponivel": True,
        "fonte_dir": candidate_scripts,
        "pyz_desatualizado": desatualizado,
    }
    if desatualizado:
        out["acao"] = "rode python scripts/build_pyz.py"
    return out


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

    # FIX 5: só é possível checar quando rodando de um `.pyz`; de código-fonte
    # solto não há artefato para desatualizar. Nunca vira `bloqueio`/afeta o
    # exit code — é diagnóstico, não portão; ver DECISÕES no relatório.
    pyz_archive = _archive_path()
    wk_info["pyz"] = _check_pyz_freshness(pyz_archive) if pyz_archive else {
        "fonte_disponivel": False,
        "nota": "não está rodando a partir de um .pyz (código-fonte solto)",
    }

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
    # F-41: em Python `isinstance(True, int)` é verdadeiro, então o teste de
    # bool vem SEMPRE antes do de int. Só bool REAL vira true/false; inteiro
    # continua inteiro — 0 e 1 são números, não booleanos disfarçados, e a
    # conversão antiga corrompia qualquer campo numérico legítimo cujo valor
    # calhasse de ser 0 ou 1 (inclusive no round-trip ler->regravar, já que
    # `frontmatter._coerce` devolve int para `promoted`).
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
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


def _publish_existing_dest(dest_dir: str, doc_id: str) -> str | None:
    """Idempotência do publish: se este `doc_id` já está em staging no
    inbox, devolve o arquivo existente para ser regravado (refresh) — sem
    isso, reexecutar `publish` criava duplicata via `_unique_dest` com o
    MESMO doc_id (achado 'Alto' de docs/application-analysis.md)."""
    if not os.path.isdir(dest_dir):
        return None
    pattern = re.compile(rf"^id:\s*[\"']?{re.escape(doc_id)}[\"']?\s*$", re.M)
    for name in sorted(os.listdir(dest_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(dest_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(4096)
        except OSError:
            continue
        if pattern.search(head):
            return path
    return None


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
    store_root: str,
    targets: list[str],
    target_spec: str | None,
    source_type: str | None,
    topic: str | None = None,
) -> tuple[set[str], set[str], list[str], str | None]:
    """Resolve `--approve <alvo>` ou `--approve-all --source-type <t> --topic <x>`.

    Devolve (caminhos_aprovados, aprovados_em_massa, targets_atualizado, erro).
    Itens aprovados que não estavam no escopo original de `targets` (ex.:
    `--approve-all` sem alvo posicional) são adicionados — aprovação explícita
    amplia o escopo, nunca reduz o que já seria escaneado.

    F-11: a aprovação em massa cruza DOIS filtros — `source_type` E `topic`
    (ambos lidos do frontmatter do item). Antes bastava o source_type, e um
    único `--approve-all --source-type agent-output` aprovava todo o inbox,
    de qualquer tópico, de uma vez.
    """
    from sbindex.frontmatter import split

    approved: set[str] = set()
    em_massa: set[str] = set()
    existing = {os.path.abspath(t) for t in targets}

    if target_spec:
        resolved, err = _resolve_promote_targets(store_root, target_spec)
        if err:
            return set(), set(), targets, err
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
            if topic is not None and meta.get("topic") != topic:
                continue
            ap = os.path.abspath(p)
            approved.add(ap)
            em_massa.add(ap)
            if ap not in existing:
                targets.append(p)
                existing.add(ap)

    return approved, em_massa, targets, None


def cmd_promote(a) -> int:
    from sbindex.frontmatter import VALID_SOURCE_TYPES, provenance_gaps, split

    store_root = _store_root(a)
    targets, err = _resolve_promote_targets(store_root, a.target)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False), file=sys.stderr)
        return 2

    # F-11: aprovação em massa é o ponto de maior alavancagem do sistema —
    # exige escopo fechado (source_type VÁLIDO + topic) e aprovador nomeado.
    if a.approve_all and not a.approve_source_type:
        print(
            json.dumps(
                {
                    "error": "--approve-all exige --source-type",
                    "acao": (
                        "repita com `--approve-all --source-type <tipo> --topic <topic> "
                        "--approved-by <pessoa>`; sem os dois filtros a aprovação varreria "
                        "todo o inbox de uma vez"
                    ),
                    "validos": sorted(VALID_SOURCE_TYPES),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    if a.approve_all and not (a.approve_topic or "").strip():
        print(
            json.dumps(
                {
                    "error": "--approve-all exige --topic",
                    "acao": (
                        "repita com `--approve-all --source-type <tipo> --topic <topic> "
                        "--approved-by <pessoa>`; o topic fecha o escopo da aprovação em "
                        "massa (só itens cujo `topic` do frontmatter bate são aprovados)"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    if a.approve_source_type and a.approve_source_type not in VALID_SOURCE_TYPES:
        print(
            json.dumps(
                {
                    "error": f"source_type inválido: {a.approve_source_type}",
                    "acao": "use um dos tipos do schema (sbindex/frontmatter.py:VALID_SOURCE_TYPES)",
                    "validos": sorted(VALID_SOURCE_TYPES),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    approved_by = (a.approved_by or "").strip()
    if (a.approve or a.approve_all) and not approved_by:
        print(
            json.dumps(
                {
                    "error": "--approve/--approve-all exigem --approved-by",
                    "acao": (
                        "repita informando quem assume a aprovação: `--approved-by "
                        "\"<pessoa>\"`. O valor vai para `promoted_by` no frontmatter e "
                        "para o log.md — não há mais aprovador genérico default"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    approve_topic = (a.approve_topic or "").strip() or None
    approved_paths, aprovados_em_massa, targets, err2 = _collect_approved_paths(
        store_root,
        targets,
        a.approve,
        a.approve_source_type if a.approve_all else None,
        approve_topic if a.approve_all else None,
    )
    if err2:
        print(json.dumps({"error": err2}, ensure_ascii=False), file=sys.stderr)
        return 2

    # Ator para as linhas de log que não são aprovação humana (o
    # `--allow-unverified` sem `--approve` continua sendo ação do próprio wk).
    log_actor = approved_by or "wiki-ai"

    promoted: list[dict] = []
    human: list[dict] = []
    quarantine: list[dict] = []
    approval_lines: list[str] = []
    # F-11(d): itens efetivamente promovidos pela aprovação em massa — a
    # contagem vai para o log junto com o escopo (topic + source_type).
    massa_promovidos: list[str] = []
    verify_blocked: list[dict] = []
    verify_overridden: list[dict] = []

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

        # FIX 3: bloqueia por item (não pelo comando inteiro) — cada fonte
        # carrega seu próprio `topic`; só as que vêm de um workdir de codescan
        # com verify falhado são afetadas. Fontes não-codescan (transcrições,
        # docs, ou sem topic) nunca passam por aqui.
        item_topic = meta.get("topic")
        if item_topic:
            vfailed = _failed_verify_workdirs(store_root, item_topic)
            if vfailed:
                if not a.allow_unverified:
                    verify_blocked.append(
                        {
                            "id": source_id,
                            "path": rel,
                            "topic": item_topic,
                            "motivo": "verify falhou para o workdir de codescan deste topic",
                            "workdirs": vfailed,
                            "acao": (
                                "rode `wk code --repo <repo> verify --artifact "
                                "<workdir>/sdd/confirmed.md` e corrija as citações "
                                "reprovadas; ou promova mesmo assim com "
                                "--allow-unverified (decisão humana explícita, "
                                "registrada no log)"
                            ),
                        }
                    )
                    continue
                verify_overridden.append(
                    {"id": source_id, "path": rel, "topic": item_topic, "workdirs": vfailed}
                )

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
            em_massa = os.path.abspath(path) in aprovados_em_massa
            approval_lines.append(
                f"- aprovado por {promoted_by}"
                f"{' (--approve-all)' if em_massa else ''}: {source_id or '(sem-id)'} "
                f"({source_type}) | {rel}"
            )
            if em_massa:
                massa_promovidos.append(rel)

    _append_quarantine(store_root, quarantine)
    if promoted:
        _append_log(
            store_root,
            f"## [{_utc_now()}] promote | {len(promoted)} promovidos | wiki-ai",
        )
    # F-11(d): o escopo da aprovação em massa é registrado SEMPRE que
    # `--approve-all` é usado — inclusive quando nada casou (contagem 0), para
    # que a tentativa fique auditável no log.md, e não só o resultado.
    if a.approve_all:
        _append_log(
            store_root,
            f"## [{_utc_now()}] promote --approve-all | escopo: topic={approve_topic} "
            f"source_type={a.approve_source_type} | {len(massa_promovidos)} aprovados "
            f"em massa | {approved_by}",
        )
        for rel_massa in massa_promovidos:
            _append_log(store_root, f"- escopo --approve-all: {rel_massa}")
    if approval_lines:
        _append_log(
            store_root,
            f"## [{_utc_now()}] promote --approve | {len(approval_lines)} aprovados | {approved_by}",
        )
        for line in approval_lines:
            _append_log(store_root, line)
    if verify_overridden:
        _append_log(
            store_root,
            f"## [{_utc_now()}] promote --allow-unverified | "
            f"{len(verify_overridden)} promovidos com verify falhado | {log_actor}",
        )
        for item in verify_overridden:
            _append_log(
                store_root,
                f"- override verify: {item.get('id') or '(sem-id)'} | {item['path']} | topic {item['topic']}",
            )
    reindexed = False
    reindex_error = None
    if promoted:
        reindexed, reindex_error = _run_reindex(store_root)

    out = {
        "promovidos": promoted,
        "decisao_humana": human,
        "quarentena": quarantine,
        "bloqueados_verify": verify_blocked,
        "reindexed": reindexed,
    }
    if verify_overridden:
        out["verify_override"] = verify_overridden
    if a.approve_all:
        out["aprovacao_em_massa"] = {
            "topic": approve_topic,
            "source_type": a.approve_source_type,
            "aprovados": len(massa_promovidos),
            "approved_by": approved_by,
        }
    if reindex_error:
        out["reindex_error"] = reindex_error
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if reindex_error else 0


def _slug(s: str) -> str:
    """Normalização crua — PRESERVA `.` e `/` (o `topic` usa `/` como
    separador de sub-tópico). NUNCA use o resultado direto num join de
    caminho: passe por `_slug_component`/`_slug_topic` (F-01)."""
    s = (s or "geral").strip().lower()
    s = re.sub(r"[^a-z0-9._/-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-/")
    return s or "geral"


# ---------- F-01: componentes de caminho vindos do frontmatter ----------

# `id`, `topic` e `source_type` são CONTEÚDO DE ARQUIVO (frontmatter da fonte),
# não argumento confiável, e viram componentes de caminho sob `wiki/` e
# `wiki-docx/` (cmd_compile, cmd_docx) e sob `raw/assets/` (lookup do asset
# HTML). Como `_slug` preserva `.` e `/`, um `id: ../../../evil` sobrevivia à
# normalização e escapava da árvore do store no join. Aqui cada componente
# passa por whitelist [a-z0-9._-], sem sequência `..` e sem iniciar por `.`;
# o que não passa vira erro explícito — nunca escrita fora da árvore.
_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9._-]+$")

_PATH_ACAO = (
    "corrija o frontmatter da fonte: `id` e `source_type` só aceitam "
    "[a-z0-9._-] (um único componente de caminho) e `topic` aceita `/` apenas "
    "como separador de sub-tópico; nenhum componente pode ser `..`, começar "
    "por `.` ou conter separador `\\`. A fonte foi recusada e nada foi gravado "
    "fora da árvore do store"
)


class PathComponentError(ValueError):
    """Componente de caminho recusado pela sanitização (F-01)."""

    def __init__(self, campo: str, valor, motivo: str) -> None:
        self.campo = campo
        self.valor = "" if valor is None else str(valor)
        self.motivo = motivo
        super().__init__(f"{campo} inseguro para caminho ({motivo}): {self.valor!r}")

    def as_dict(self) -> dict:
        return {
            "error": str(self),
            "campo": self.campo,
            "valor": self.valor,
            "motivo": self.motivo,
            "acao": _PATH_ACAO,
        }


def _check_component(value: str, campo: str) -> str:
    """Valida UM componente de caminho já normalizado (minúsculo)."""
    if not value:
        raise PathComponentError(campo, value, "componente vazio")
    if value in (".", "..") or ".." in value:
        raise PathComponentError(campo, value, "contém sequência `..`")
    if value.startswith("."):
        raise PathComponentError(campo, value, "componente iniciado por `.`")
    if not _SAFE_COMPONENT_RE.match(value):
        raise PathComponentError(campo, value, "fora da whitelist [a-z0-9._-]")
    return value


def _slug_component(value, campo: str) -> str:
    """`_slug` + validação: devolve um único componente de caminho seguro.
    Separador (`/` ou `\\`) no valor cru é recusado, não normalizado — em
    `id`/`source_type` ele nunca é legítimo e é exatamente o vetor do ataque."""
    raw = "" if value is None else str(value)
    if "/" in raw or "\\" in raw:
        raise PathComponentError(campo, raw, "contém separador de caminho")
    return _check_component(_slug(raw), campo)


def _slug_topic(value, campo: str = "topic") -> str:
    """`topic` legitimamente tem `/` (sub-tópico: `codebases/demo`): valida
    componente a componente e devolve o slug com `/` preservado."""
    raw = "" if value is None else str(value)
    if "\\" in raw:
        raise PathComponentError(campo, raw, "contém separador `\\`")
    parts = [p for p in _slug(raw).split("/") if p]
    if not parts:
        raise PathComponentError(campo, raw, "vazio depois da normalização")
    return "/".join(_check_component(p, campo) for p in parts)


def _raw_component(value, campo: str) -> str:
    """Valida um valor CRU usado direto num join, sem passar por `_slug`
    (ex.: `raw/assets/<id>.html`, que preserva a caixa do `id`). A whitelist é
    a mesma, aplicada sobre a versão minúscula; o valor volta intacto."""
    raw = "" if value is None else str(value).strip()
    if "/" in raw or "\\" in raw:
        raise PathComponentError(campo, raw, "contém separador de caminho")
    _check_component(raw.lower(), campo)
    return raw


def _confined(root: str, path: str, campo: str, valor) -> str:
    """Última barreira: o destino final tem de ficar dentro de `root` mesmo
    depois de resolver symlink (`realpath`) — cinto e suspensório sobre a
    whitelist de componentes."""
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    try:
        inside = os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:  # unidades diferentes no Windows
        inside = False
    if not inside:
        raise PathComponentError(campo, valor, f"destino resolvido fora de {root_real}")
    return path


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


# ---------- FIX 2: síntese por tópico (overview embutido, não só lista de links) ----------

# `publish` grava `origin: "codescan <repo> — <caminho-relativo-ao-workdir>"`
# (cmd_publish). Usamos esse caminho — nunca o nome do tópico/repo — para
# identificar QUAL artefato SDD uma fonte representa: generaliza para
# qualquer execução do codescan, sem hardcode do tópico de exemplo.
_ORIGIN_CODESCAN_RE = re.compile(r"^codescan .+ — (.+)$")


def _codescan_artifact_rel(origin: str | None) -> str | None:
    if not origin:
        return None
    m = _ORIGIN_CODESCAN_RE.match(origin)
    return m.group(1) if m else None


def _first_heading_title(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line:
            break
    return ""


def _md_headings(body: str, level: int) -> list[str]:
    prefix = "#" * level + " "
    return [ln[len(prefix):].strip() for ln in body.splitlines() if ln.startswith(prefix)]


def _truncate(text: str, n: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _adr_summary(body: str) -> tuple[str, str, str]:
    """(título, status, decisão em 1 linha) extraídos do corpo de um ADR
    (`# ADR-NNN — Título`, opcional `Status: ...`, primeira bullet de `##
    Decisão`). 100% textual, sem heurística de LLM."""
    title = _first_heading_title(body)
    status = "—"
    for line in body.splitlines():
        s = line.strip().lstrip("- ").strip()
        if s.lower().startswith("status:"):
            status = s.split(":", 1)[1].strip()
            break
    decisao = ""
    in_decision = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_decision = re.sub(r"^#+\s*", "", stripped).lower().startswith("decis")
            continue
        if in_decision and stripped.startswith("-"):
            decisao = stripped.lstrip("- ").strip()
            break
    return title, status, _truncate(decisao, 160)


def _build_topic_overview(
    topic: str,
    sources: list[dict],
    base_dir: str,
    page_abspath_by_id: dict[str, str],
    asset_abspath_by_id: dict[str, str],
) -> tuple[str, list[str]]:
    """Página de síntese por tópico: embute conteúdo-chave (arquitetura, C4,
    ADRs, edge cases, diagramas, confiança) em vez de só linkar — FIX 2.
    100% determinístico (texto puro, sem chamada a LLM); tolerante a
    artefatos ausentes (pula a seção, registra em `## Lacunas de síntese`).

    `base_dir` é o diretório onde a página será gravada (usado para calcular
    hrefs relativos corretos a partir de `page_abspath_by_id`/
    `asset_abspath_by_id`, que guardam caminhos absolutos).
    """
    lacunas: list[str] = []
    by_rel: dict[str, dict] = {}
    for s in sources:
        rel = _codescan_artifact_rel(s.get("origin"))
        if rel and rel not in by_rel:  # primeira ocorrência é suficiente
            by_rel[rel] = s

    def href_for(source: dict) -> str:
        p = page_abspath_by_id.get(source.get("id"))
        return os.path.relpath(p, base_dir).replace("\\", "/") if p else "#"

    def rel_sorted(pred) -> list[dict]:
        matches = [s for s in sources if pred(_codescan_artifact_rel(s.get("origin")) or "")]
        return sorted(matches, key=lambda s: _codescan_artifact_rel(s["origin"]) or "")

    lines = [f"# Visão geral — {topic}", ""]
    lines.append(
        "Síntese determinística dos artefatos SDD promovidos para este tópico, "
        "gerada por `wk compile` (sem chamada a LLM)."
    )
    lines.append("")

    # --- Arquitetura ---
    lines.append("## Arquitetura")
    lines.append("")
    arch = by_rel.get("sdd/architecture.md")
    if arch:
        lines.append(arch["body"].strip())
        lines.append("")
    else:
        lacunas.append("Arquitetura: `sdd/architecture.md` ausente entre as fontes promovidas.")
    for label, rel in (
        ("Contexto (C4)", "sdd/c4-context.md"),
        ("Containers (C4)", "sdd/c4-containers.md"),
        ("Componentes (C4)", "sdd/c4-components.md"),
    ):
        c4 = by_rel.get(rel)
        if c4:
            lines.append(f"### {label}")
            lines.append("")
            lines.append(c4["body"].strip())
            lines.append("")
        else:
            lacunas.append(f"Arquitetura: `{rel}` ausente entre as fontes promovidas.")

    # --- Decisões (ADRs) ---
    lines.append("## Decisões")
    lines.append("")
    adrs = rel_sorted(lambda rel: rel.startswith("sdd/adrs/"))
    if adrs:
        lines.append("| ADR | Status | Decisão |")
        lines.append("|---|---|---|")
        for s in adrs:
            titulo, status, decisao = _adr_summary(s["body"])
            lines.append(f"| [{titulo or s['id']}]({href_for(s)}) | {status} | {decisao or '—'} |")
        lines.append("")
    else:
        lacunas.append("Decisões: nenhum ADR (`sdd/adrs/*.md`) entre as fontes promovidas.")

    # --- Análise de código ---
    lines.append("## Análise de código")
    lines.append("")
    ca = by_rel.get("sdd/code-analysis.md")
    if ca:
        headings = _md_headings(ca["body"], level=3)
        modulos = [h[len("Módulo: "):].strip() if h.startswith("Módulo: ") else h for h in headings]
        lines.append(f"{len(modulos)} módulo(s) consolidados (índice; ver artefato completo para o detalhe).")
        lines.append("")
        for m in modulos:
            lines.append(f"- {m}")
        if modulos:
            lines.append("")
        nbytes = len(ca["body"].encode("utf-8"))
        lines.append(f"Artefato completo: [{ca['id']}]({href_for(ca)}) ({nbytes} bytes).")
        lines.append("")
    else:
        lacunas.append("Análise de código: `sdd/code-analysis.md` ausente entre as fontes promovidas.")

    # --- Edge cases ---
    lines.append("## Edge cases")
    lines.append("")
    edge_cases = rel_sorted(lambda rel: rel.startswith("sdd/specs/") and rel.endswith("/edge-cases.md"))
    if edge_cases:
        for s in edge_cases:
            rel = _codescan_artifact_rel(s["origin"]) or ""
            parts = rel.split("/")
            unit = parts[2] if len(parts) > 2 else rel
            lines.append(f"### {unit}")
            lines.append("")
            lines.append(s["body"].strip())
            lines.append("")
    else:
        lacunas.append("Edge cases: nenhum `sdd/specs/*/edge-cases.md` entre as fontes promovidas.")

    # --- Diagramas ---
    lines.append("## Diagramas")
    lines.append("")
    flow_index = by_rel.get("sdd/flowcharts/_index.md")
    if flow_index:
        lines.append("### Fluxo consolidado")
        lines.append("")
        lines.append(flow_index["body"].strip())
        lines.append("")
    flowcharts = rel_sorted(
        lambda rel: rel.startswith("sdd/flowcharts/") and rel != "sdd/flowcharts/_index.md"
    )
    sequences = rel_sorted(lambda rel: rel.startswith("sdd/sequences/"))
    if flowcharts:
        lines.append("### Outros flowcharts")
        lines.append("")
        for s in flowcharts:
            lines.append(f"- [{s['id']}]({href_for(s)})")
        lines.append("")
    if sequences:
        lines.append("### Sequências")
        lines.append("")
        for s in sequences:
            lines.append(f"- [{s['id']}]({href_for(s)})")
        lines.append("")
    if not flow_index and not flowcharts and not sequences:
        lacunas.append("Diagramas: nenhum flowchart/sequence entre as fontes promovidas.")
    html_asset_id = next((s["id"] for s in sources if s.get("id") in asset_abspath_by_id), None)
    if html_asset_id:
        href = os.path.relpath(asset_abspath_by_id[html_asset_id], base_dir).replace("\\", "/")
        lines.append(f"- Visualização interativa de acoplamento: [{os.path.basename(href)}]({href})")
        lines.append("")
    else:
        lacunas.append("Diagramas: nenhum asset HTML de acoplamento (`coupling.html`) publicado para este tópico.")

    # --- Confiança ---
    lines.append("## Confiança")
    lines.append("")
    conf = by_rel.get("sdd/confidence-report.md")
    if conf:
        lines.append(conf["body"].strip())
        lines.append("")
    else:
        lacunas.append("Confiança: `sdd/confidence-report.md` ausente entre as fontes promovidas.")
    gaps_doc = by_rel.get("sdd/gaps.md")
    if gaps_doc:
        lines.append("### Lacunas (`gaps.md`)")
        lines.append("")
        lines.append(gaps_doc["body"].strip())
        lines.append("")
    else:
        lacunas.append("Confiança: `sdd/gaps.md` ausente entre as fontes promovidas.")
    # B2 (validação E2E): confirmed.md/inferred.md são canônicos sob sdd/
    # (codescan/sdd.py:262-263 ArtifactRule + linha 1076-1078 — cópia solta na
    # raiz do workdir é P0 no audit do codescan, nunca a fonte real); a chave
    # tinha que levar o mesmo prefixo "sdd/" que `_codescan_artifact_rel`
    # sempre produz a partir do `origin` gravado por `wk publish`.
    confirmed = by_rel.get("sdd/confirmed.md")
    inferred = by_rel.get("sdd/inferred.md")
    if confirmed or inferred:
        lines.append("### Confirmado vs. inferido")
        lines.append("")
        if confirmed:
            n = len(confirmed["body"].splitlines())
            lines.append(f"- Confirmado: [{confirmed['id']}]({href_for(confirmed)}) ({n} linhas).")
        else:
            lacunas.append("Confiança: `confirmed.md` ausente entre as fontes promovidas.")
        if inferred:
            n = len(inferred["body"].splitlines())
            lines.append(f"- Inferido: [{inferred['id']}]({href_for(inferred)}) ({n} linhas).")
        else:
            lacunas.append("Confiança: `inferred.md` ausente entre as fontes promovidas.")
        lines.append("")
    else:
        lacunas.append("Confiança: nem `confirmed.md` nem `inferred.md` entre as fontes promovidas.")

    if lacunas:
        lines.append("## Lacunas de síntese")
        lines.append("")
        for item in lacunas:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", lacunas


def cmd_compile(a) -> int:
    """Uma página por documento promovido: wiki/<topic>/<source_type>/<id>.md,
    mais uma página de síntese por tópico (`wiki/<topic>/overview.md`, FIX 2)
    que embute o conteúdo-chave (arquitetura, ADRs, edge cases, diagramas,
    confiança) e é linkada em destaque no topo de `wiki/index.md`.

    Granularidade por grupo (topic, source_type) colapsava dezenas de módulos
    e specs numa página só; aqui cada fonte promovida vira sua própria página,
    e `wiki/index.md` agrega todas com link.
    """
    store_root = _store_root(a)

    # FIX 3: bloqueia compile de um topic (ou de todos, sem filtro) cujo
    # workdir de codescan teve `verify` reprovado — salvo --allow-unverified.
    verify_block, verify_overridden = _verify_gate_scope(store_root, a.topic, a.allow_unverified)
    if verify_block:
        print(json.dumps(verify_block, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3

    sources = _promoted_raw_sources(store_root, a.topic)
    wiki_root = os.path.join(store_root, "wiki")
    os.makedirs(wiki_root, exist_ok=True)
    assets_root = os.path.join(store_root, "raw", "assets")

    pages = []
    # F-01: fontes cujo id/topic/source_type não passa na sanitização de
    # componente de caminho. São recusadas UMA A UMA (o resto da wiki continua
    # compilando) e saem no JSON com `acao`; o comando termina != 0 para que
    # nenhuma automação trate a recusa como sucesso silencioso.
    recusados: list[dict] = []
    # (fonte, rel_page, href): rel_page é store-relativo (contrato de saída,
    # em `pages`); href é relativo a `wiki_root` — usado só nos links dentro
    # de `wiki/index.md`, que mora em `wiki/` (BQ1: usar rel_page ali gerava
    # `wiki/wiki/<topic>/...`, um nível a mais, e quebrava todo link).
    page_entries: list[tuple[dict, str, str]] = []
    page_abspath_by_id: dict[str, str] = {}
    asset_abspath_by_id: dict[str, str] = {}

    for s in sources:
        # F-01: TODO componente derivado de frontmatter é sanitizado e o
        # destino final é confinado por realpath/commonpath ANTES de qualquer
        # escrita ou leitura de asset.
        try:
            topic_slug = _slug_topic(s["topic"])
            type_slug = _slug_component(s["source_type"], "source_type")
            id_slug = _slug_component(s["id"] or "sem-id", "id")
            page_dir = _confined(
                wiki_root,
                os.path.join(wiki_root, *topic_slug.split("/"), type_slug),
                "topic",
                s["topic"],
            )
            path = _confined(wiki_root, os.path.join(page_dir, f"{id_slug}.md"), "id", s["id"])
            asset_src = None
            if s.get("id"):
                # O nome do asset preserva a caixa do id (gravado assim pelo
                # publish), então valida-se o valor cru: sem isso um
                # `id: ../../../segredo` fazia o compile LER fora da árvore e
                # copiar o conteúdo para dentro da wiki.
                asset_src = _confined(
                    assets_root,
                    os.path.join(assets_root, f"{_raw_component(s['id'], 'id')}.html"),
                    "id",
                    s["id"],
                )
        except PathComponentError as exc:
            recusados.append({**exc.as_dict(), "fonte": s["rel"]})
            continue
        page_id = f"wiki-{topic_slug}-{type_slug}-{id_slug}"

        # FIX 1: fonte com asset HTML navegável anexado (publish/coupling.html)
        # — copia o asset para dentro de wiki/, ao lado da página, e linka.
        asset_href = None
        if asset_src and os.path.isfile(asset_src):
            os.makedirs(page_dir, exist_ok=True)
            asset_dest = os.path.join(page_dir, f"{id_slug}.html")
            shutil.copyfile(asset_src, asset_dest)
            asset_href = f"{id_slug}.html"
            asset_abspath_by_id[s["id"]] = asset_dest

        body = [
            f"# {s['id']}",
            "",
            f"- topic: `{s['topic']}`",
            f"- source_type: `{s['source_type']}`",
            f"- confidence: `{s['confidence'] or ''}`",
            f"- origem: {s['origin'] or ''}",
            f"- fonte: `{s['rel']}`",
        ]
        if asset_href:
            body += ["", f"- asset navegável: [{asset_href}]({asset_href})"]
        body += ["", "## Conteúdo", "", s["body"], ""]
        text = _frontmatter_for_wiki(page_id, s["topic"], [str(s["id"])] if s.get("id") else [])
        text += "\n" + "\n".join(body).rstrip() + "\n"
        _write_md(path, text)
        rel_page = os.path.relpath(path, store_root).replace("\\", "/")
        href = os.path.relpath(path, wiki_root).replace("\\", "/")
        pages.append(rel_page)
        page_entries.append((s, rel_page, href))
        if s.get("id"):
            page_abspath_by_id[s["id"]] = path

    source_ids = [str(s["id"]) for s in sources if s.get("id")]
    compile_ts = _utc_now()

    by_topic: dict[str, list[tuple[dict, str]]] = {}
    for s, rel_page, href in page_entries:
        by_topic.setdefault(s["topic"], []).append((s, href))

    # FIX 2: página de síntese por tópico — só gerada quando há pelo menos uma
    # fonte com `origin` no formato do `wk publish` (artefato de codescan
    # reconhecível); um tópico só de fontes manuais (ingest de transcrições/
    # docs, sem nenhum artefato SDD) não tem o que sintetizar — nada de página
    # quase vazia só para existir.
    overview_entries: list[tuple[str, str, list[str]]] = []  # (topic, href-a-partir-de-wiki_root, lacunas)
    for topic, entries in sorted(by_topic.items()):
        topic_sources = [s for s, _href in entries]
        if not any(_codescan_artifact_rel(s.get("origin")) for s in topic_sources):
            continue
        # F-01: mesmo tratamento do laço das páginas — o topic aqui já passou
        # pela sanitização (só chega em `by_topic` fonte aceita), mas o join da
        # síntese é revalidado e confinado por conta própria.
        try:
            topic_slug = _slug_topic(topic)
            overview_dir = _confined(
                wiki_root, os.path.join(wiki_root, *topic_slug.split("/")), "topic", topic
            )
        except PathComponentError as exc:
            recusados.append({**exc.as_dict(), "fonte": f"(síntese do topic {topic})"})
            continue
        overview_path = os.path.join(overview_dir, "overview.md")
        overview_body, lacunas = _build_topic_overview(
            topic, topic_sources, overview_dir, page_abspath_by_id, asset_abspath_by_id
        )
        overview_source_ids = [str(s["id"]) for s in topic_sources if s.get("id")]
        overview_page_id = f"wiki-{topic_slug}-overview"
        overview_text = _frontmatter_for_wiki(overview_page_id, topic, overview_source_ids)
        overview_text += "\n\n" + overview_body.rstrip() + "\n"
        _write_md(overview_path, overview_text)
        pages.append(os.path.relpath(overview_path, store_root).replace("\\", "/"))
        overview_href = os.path.relpath(overview_path, wiki_root).replace("\\", "/")
        overview_entries.append((topic, overview_href, lacunas))

    index_body = ["# Wiki Index", ""]
    if a.topic:
        index_body.extend([f"Tópico compilado: {a.topic}", ""])
    if overview_entries:
        index_body.append("## Visão geral por tópico")
        index_body.append("")
        for topic, href, lacunas in overview_entries:
            aviso = f" — {len(lacunas)} lacuna(s) de síntese" if lacunas else ""
            index_body.append(f"- **[{topic}]({href})**{aviso}")
        index_body.append("")
    index_body.append(f"Total: {len(page_entries)} páginas em {len(by_topic)} tópico(s).")
    index_body.append("")
    for topic in sorted(by_topic):
        entries = by_topic[topic]
        index_body.append(f"## {topic} ({len(entries)})")
        index_body.append("")
        for s, href in entries:
            atualizado = s.get("captured_at") or compile_ts
            index_body.append(f"- [{s['id']}]({href}) | `{s['source_type']}` | atualizado {atualizado}")
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
    if verify_overridden:
        _append_log(
            store_root,
            f"## [{_utc_now()}] compile --allow-unverified | "
            f"{len(verify_overridden)} workdir(s) com verify falhado, compilados mesmo assim",
        )
    out = {
        "paginas": pages,
        "fontes": len(sources),
        "reindexed": reindexed,
        "overview": [{"topic": t, "href": h, "lacunas": l} for t, h, l in overview_entries],
    }
    if verify_overridden:
        out["verify_override"] = verify_overridden
    if recusados:
        out["recusados"] = recusados
    if reindex_error:
        out["reindex_error"] = reindex_error
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if (reindex_error or recusados) else 0


def _docx_prune_root(out_root: str, topic: str | None) -> str:
    """Raiz da poda de órfãos: `out_root` inteiro sem filtro de topic; com
    filtro, apenas a subárvore `out_root/<topic>` (topic pode ter `/`, daí
    o split). Escopar assim evita que a poda apague `.docx` de outros
    topics quando a execução só gerou um subconjunto (defeito reportado:
    `wk docx <topic>` removia órfãos de topics não filtrados).

    O slug é obrigatório aqui: `dest_dir` grava em `_slug_topic(s["topic"])`,
    então usar o topic cru erraria a subárvore em qualquer topic que o slug
    altere (maiúscula, espaço, acento). O slug preserva `/`, daí o split.

    F-01: sem sanitização, `wk docx ../..` fazia a poda subir a árvore e
    APAGAR `.docx` fora de `out_root`. `_slug_topic` recusa o componente e
    `_confined` garante, por realpath/commonpath, que a raiz da poda fica
    dentro de `out_root` (levanta `PathComponentError` se não ficar)."""
    if not topic:
        return out_root
    slug = _slug_topic(topic)
    return _confined(
        out_root, os.path.join(out_root, *slug.split("/")), "topic", topic
    )


def cmd_docx(a) -> int:
    """Uma página .docx por documento promovido: wiki-docx/<topic>/<source_type>/<arquivo>,
    mais um `.docx` agregador por tópico (`wiki-docx/<topic>/index.docx`, FIX 6)
    espelhando a página de síntese do FIX 2 (`_build_topic_overview`), passada
    pelo caminho normal de conversão (`docxgen.build_document`) — sem tocar
    `docxgen.py`/`docx_md.py`/`docx_ooxml.py`/`docx_meta.py` (fora do escopo
    deste agente; só a API pública `build_document(source: dict)` é chamada).

    Árvore paralela a wiki/, lida direto de raw/ (não de wiki/compilado) e não
    entra em STORE_TREE (decisão M4, docs/plano-wiki-docx.md) — criada sob
    demanda, mesmo padrão de `wiki_root` em cmd_compile. Escrita sempre
    sobrescreve o path determinístico; nunca `_unique_dest` (essa função
    acumularia `-2.docx`/`-3.docx` a cada execução, ver C1 do plano funcional).
    """
    from wk import docx_meta, docxgen

    store_root = _store_root(a)

    # FIX 3: bloqueia docx de um topic (ou de todos, sem filtro) cujo workdir
    # de codescan teve `verify` reprovado — salvo --allow-unverified.
    verify_block, verify_overridden = _verify_gate_scope(store_root, a.topic, a.allow_unverified)
    if verify_block:
        print(json.dumps(verify_block, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3

    # F-01: o `--topic` da linha de comando vira raiz da poda de órfãos
    # (`_docx_prune_root`); valida antes de qualquer escrita/remoção.
    if a.topic:
        try:
            _slug_topic(a.topic)
        except PathComponentError as exc:
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
            return 2

    sources = _promoted_raw_sources(store_root, a.topic)
    out_root = os.path.join(store_root, a.out_dir)
    os.makedirs(out_root, exist_ok=True)
    assets_root = os.path.join(store_root, "raw", "assets")

    gerados: list[dict] = []
    pulados: list[dict] = []
    # F-01: fontes recusadas pela sanitização de componente de caminho — como
    # em cmd_compile, saem no JSON com `acao` e forçam exit != 0.
    recusados: list[dict] = []
    # FIX 1: skips intencionais (asset HTML sem equivalente .docx) — separado
    # de `pulados` (falhas reais) para não inflar o exit code de um resultado
    # esperado e bem-sucedido.
    ignorados_asset_html: list[dict] = []
    gerado_abs: set[str] = set()
    taken: set[str] = set()
    docx_path_by_id: dict[str, str] = {}

    for s in sorted(sources, key=lambda s: s["id"]):
        # F-01: sanitiza id/topic/source_type antes de qualquer join.
        try:
            topic_slug = _slug_topic(s["topic"])
            type_slug = _slug_component(s["source_type"], "source_type")
            asset_src = (
                _confined(
                    assets_root,
                    os.path.join(assets_root, f"{_raw_component(s['id'], 'id')}.html"),
                    "id",
                    s["id"],
                )
                if s.get("id")
                else None
            )
        except PathComponentError as exc:
            recusados.append({**exc.as_dict(), "fonte": s.get("rel")})
            continue

        # FIX 1: fonte-stub de asset HTML navegável (ex.: coupling.html
        # publicado via `wk publish`) não vira .docx — o conteúdo real não é
        # markdown; `docxgen.build_document` nunca chega a vê-lo.
        if asset_src and os.path.isfile(asset_src):
            asset_rel = os.path.join("raw", "assets", f"{s['id']}.html")
            ignorados_asset_html.append(
                {
                    "id": s["id"],
                    "motivo": f"asset HTML navegável ({asset_rel.replace(os.sep, '/')}) — sem equivalente .docx",
                }
            )
            continue
        try:
            filename = docx_meta.docx_filename_unique(s, taken)
            taken.add(filename[: -len(".docx")])
            dest_dir = os.path.join(out_root, *topic_slug.split("/"), type_slug)
            os.makedirs(dest_dir, exist_ok=True)
            # `docx_meta` já neutraliza `/` e `..` no stem; o confinamento aqui
            # é a barreira final (F-01), válida para qualquer nome futuro.
            path = _confined(out_root, os.path.join(dest_dir, filename), "id", s["id"])
            data, avisos = docxgen.build_document(s)
            with open(path, "wb") as f:
                f.write(data)
        except PathComponentError as exc:
            recusados.append({**exc.as_dict(), "fonte": s.get("rel")})
            continue
        except Exception as exc:
            pulados.append({"id": s["id"], "motivo": str(exc)})
            continue
        gerado_abs.add(os.path.abspath(path))
        if s.get("id"):
            docx_path_by_id[s["id"]] = path
        gerados.append(
            {
                "path": os.path.relpath(path, store_root).replace(os.sep, "/"),
                "id": s["id"],
                "source_type": s["source_type"],
                "avisos": avisos,
            }
        )

    # FIX 6: agregador por tópico — reusa a mesma síntese determinística do
    # FIX 2 (`_build_topic_overview`), embrulhada num `source` sintético e
    # convertida pelo caminho normal (`docxgen.build_document`). Nome fixo
    # `index.docx`: `docx_filename`/`docx_filename_unique` derivam sempre
    # `<topic-slug>-<subject>[-<scope>].docx` a partir de `source["id"]`
    # (docx_meta.py), nunca "index.docx" — sem colisão possível com as fontes
    # reais geradas acima.
    agregados: list[dict] = []
    by_topic_docx: dict[str, list[dict]] = {}
    for s in sources:
        by_topic_docx.setdefault(s["topic"], []).append(s)
    for topic, topic_sources in sorted(by_topic_docx.items()):
        # Mesma guarda do FIX 2 em cmd_compile: sem nenhuma fonte com origin
        # de `wk publish`, não há artefato SDD para sintetizar.
        if not any(_codescan_artifact_rel(s.get("origin")) for s in topic_sources):
            continue
        try:
            agg_topic_slug = _slug_topic(topic)
            dest_dir = _confined(
                out_root, os.path.join(out_root, *agg_topic_slug.split("/")), "topic", topic
            )
        except PathComponentError as exc:
            recusados.append({**exc.as_dict(), "fonte": f"(agregador do topic {topic})"})
            continue
        os.makedirs(dest_dir, exist_ok=True)
        overview_text, lacunas = _build_topic_overview(
            topic, topic_sources, dest_dir, docx_path_by_id, {}
        )
        agg_id = f"wk-docx-overview-{agg_topic_slug.replace('/', '-')}"
        agg_source = {
            "id": agg_id,
            "topic": topic,
            "source_type": "sintese",
            "confidence": None,
            "origin": "wk docx (síntese determinística por tópico, mesma consolidação do wiki compile)",
            "captured_at": _utc_now(),
            "rel": f"wiki/{_slug(topic)}/overview.md (equivalente)",
            "body": overview_text,
        }
        try:
            path = os.path.join(dest_dir, "index.docx")
            data, avisos = docxgen.build_document(agg_source)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as exc:
            pulados.append({"id": agg_id, "motivo": f"agregador: {exc}"})
            continue
        gerado_abs.add(os.path.abspath(path))
        agregados.append(
            {
                "path": os.path.relpath(path, store_root).replace(os.sep, "/"),
                "topic": topic,
                "lacunas": lacunas,
            }
        )

    removidos: list[dict] = []
    if not a.no_prune:
        # Poda escopada ao filtro (ver _docx_prune_root): sem `a.topic` varre
        # `out_root` inteiro como antes; com `a.topic` varre só a subárvore
        # daquele topic, para não apagar `.docx` órfãos de outros topics.
        try:
            prune_root = _docx_prune_root(out_root, a.topic)
        except PathComponentError as exc:  # F-01: nunca podar fora de out_root
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        out_root_abs = os.path.abspath(out_root)
        if os.path.isdir(prune_root):
            existentes = []
            for dirpath, dirnames, filenames in os.walk(prune_root):
                dirnames[:] = sorted(dirnames)
                for fn in sorted(filenames):
                    if fn.endswith(".docx"):
                        existentes.append(os.path.join(dirpath, fn))
            for orfao in sorted(existentes):
                if os.path.abspath(orfao) not in gerado_abs:
                    os.remove(orfao)
                    removidos.append(
                        {"path": os.path.relpath(orfao, store_root).replace(os.sep, "/"), "motivo": "orfao"}
                    )

            for dirpath, _dirnames, _filenames in os.walk(prune_root, topdown=False):
                if os.path.abspath(dirpath) == out_root_abs:
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass

            if a.topic:
                # topic com "/" gera diretórios-pai intermediários (ex.:
                # codebases/exemplo -> pai "codebases"); limpa-os também,
                # parando antes de out_root.
                parent = os.path.dirname(os.path.abspath(prune_root))
                while parent != out_root_abs and parent.startswith(out_root_abs + os.sep):
                    try:
                        if os.listdir(parent):
                            break
                        os.rmdir(parent)
                    except OSError:
                        break
                    parent = os.path.dirname(parent)

    _append_log(
        store_root,
        f"## [{_utc_now()}] docx | {len(gerados)} documentos | {len(agregados)} agregadores | "
        f"{len(sources)} fontes | {len(removidos)} removidos",
    )
    if verify_overridden:
        _append_log(
            store_root,
            f"## [{_utc_now()}] docx --allow-unverified | "
            f"{len(verify_overridden)} workdir(s) com verify falhado, exportados mesmo assim",
        )
    out = {
        "documentos": gerados,
        "agregadores": agregados,
        "fontes": len(sources),
        "removidos": removidos,
        "pulados": pulados,
        "ignorados_asset_html": ignorados_asset_html,
    }
    if verify_overridden:
        out["verify_override"] = verify_overridden
    if recusados:
        out["recusados"] = recusados
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if (pulados or recusados) else 0


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
# .xmi: exportação nativa de ferramentas UML (Enterprise Architect, StarUML,
# MagicDraw). Mesmo conteúdo estrutural de .xml (XMI é um dialeto XML); roteia
# para o mesmo `_convert_xml_architecture`, sem duplicar o conversor.
_INGEST_XMI_EXTS = {".xmi"}
_INGEST_SUPPORTED_EXTS = (
    _INGEST_MD_EXTS | _INGEST_TEXT_EXTS | _INGEST_SUBTITLE_EXTS
    | _INGEST_HTML_EXTS | _INGEST_CODEBLOCK_EXTS | _INGEST_XMI_EXTS
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
    if ext == ".xml" or ext in _INGEST_XMI_EXTS:
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


def _docx_gerado_por_wk(path: str) -> bool:
    """True se `path` for um .docx gerado por `wk docx` (guarda de reingestão, M7).

    `wk docx` marca `docProps/core.xml` com `dc:identifier` ==
    docx_meta.DOCX_GERADO_IDENTIFIER ("wk-docx-gerado"). Falha ao ler o zip
    (arquivo corrompido, sem core.xml, não é zip, etc.) NÃO bloqueia — um
    .docx legítimo qualquer pode ser ilegível por outros motivos; só a
    marcação exata bloqueia.
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            core = zf.read("docProps/core.xml")
        root = ET.fromstring(core)
    except Exception:
        return False
    el = root.find("{http://purl.org/dc/elements/1.1/}identifier")
    return el is not None and (el.text or "").strip() == "wk-docx-gerado"


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
    if ext == ".docx" and _docx_gerado_por_wk(a.file):
        print(
            json.dumps(
                {
                    "error": (
                        f"'{a.file}' é um .docx gerado por `wk docx` — não pode ser "
                        "reingerido como fonte original (proveniência falsa). A "
                        "fonte é o `.md` correspondente em raw/; rode `wk docx` de "
                        "novo se precisar regenerar o .docx."
                    )
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
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


_PUBLISH_CODE_REPO_REL = {
    "sdd/inventory.md", "sdd/dependencies.md", "sdd/coupling.md",
    # FIX 1: coupling.html é o mesmo dado de coupling.md (grafo de acoplamento),
    # só que renderizado interativo pelo motor Java (coupling_java_html.py) —
    # mesma origem determinística, mesma classificação code-repo. NOTA: isto
    # reverte a decisão documentada em references/sdd-contract.md:613-615,635-636
    # e INSTALL.md ("coupling.html ... não é artefato SDD, publish não o leva
    # para inbox/"); a decisão de negócio mudou (evidência real: artefato de
    # 106KB nunca chegava à wiki), mas a doc não foi atualizada aqui — fora do
    # escopo deste agente (arquivos .md são de outro lote). Ver TODOs.
    "sdd/coupling.html",
}
_PUBLISH_EXCLUDED_DIRNAMES = {"agent-packs", "agent-outputs", "agent-runs"}
_PUBLISH_EXCLUDED_FILENAMES = {"state.json", "surface.json"}
# FIX 1: extensões elegíveis em sdd/**. Só .html (não *.htm, não outros
# binários) — é o único formato não-.md que o codescan hoje produz ali
# (coupling.html, motor Java). modules/*.md e confirmed.md/inferred.md na
# raiz continuam só .md (nenhum gerador hoje produz outra coisa ali).
_PUBLISH_SDD_EXTS = (".md", ".html")


def _state_json(workdir: str) -> dict | None:
    """Lê `<workdir>/state.json`. None se ausente/ilegível — chamadores devem
    tratar como "sem informação", nunca como erro fatal (workdirs manuais ou
    de versões antigas do codescan podem não ter o arquivo)."""
    path = os.path.join(workdir, "state.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _expected_module_filenames(workdir: str) -> set[str] | None:
    """FIX 4: nomes de arquivo (`<slug>.md`) autorizados por
    `state.json['stages']['modules']['done']`, usando o mesmo slug de
    `codescan.export._slug` (é o que `codescan/cli.py:_module_artifact` usa
    para gravar `modules/<slug>.md` originalmente).

    None = fallback seguro (publica tudo): state.json ausente, sem stage
    `modules` ou sem lista `done` — nunca bloqueia workdirs de versões antigas
    ou fora do padrão do codescan.
    """
    state = _state_json(workdir)
    if not state:
        return None
    done = ((state.get("stages") or {}).get("modules") or {}).get("done")
    if not isinstance(done, list):
        return None
    from codescan.export import _slug as _codescan_slug

    return {
        f"{_codescan_slug(item.replace(chr(92), '/'))}.md"
        for item in done
        if isinstance(item, str)
    }


def _publish_candidates(workdir: str) -> tuple[list[str], list[str]]:
    """Arquivos elegíveis: `sdd/**/*.{md,html}` e `modules/*.md` (só os
    registrados em `state.json['stages']['modules']['done']` — FIX 4), mais
    confirmed.md/inferred.md na raiz do workdir. Nunca `*.py`, `*.txt`,
    agent-packs/, agent-outputs/, agent-runs/, state.json ou surface.json.

    Devolve `(candidatos, modulos_orfaos_descartados)` — o segundo item é a
    lista de nomes de arquivo em `modules/` que NÃO constam em `done` (workaround
    manual de operador, path bugado etc.); vazio se não houver descarte ou se
    o fallback seguro (sem state.json/sem lista `done`) publicar tudo.
    """
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
                if os.path.splitext(fn)[1].lower() not in _PUBLISH_SDD_EXTS:
                    continue
                candidates.append(os.path.join(dirpath, fn))

    modules_root = os.path.join(workdir, "modules")
    modulos_orfaos: list[str] = []
    if os.path.isdir(modules_root):
        expected = _expected_module_filenames(workdir)
        for fn in sorted(os.listdir(modules_root)):
            full = os.path.join(modules_root, fn)
            if not os.path.isfile(full) or fn in _PUBLISH_EXCLUDED_FILENAMES:
                continue
            if os.path.splitext(fn)[1].lower() != ".md":
                continue
            if expected is not None and fn not in expected:
                modulos_orfaos.append(fn)
                continue
            candidates.append(full)

    for fn in ("confirmed.md", "inferred.md"):
        full = os.path.join(workdir, fn)
        if os.path.isfile(full):
            candidates.append(full)

    return candidates, modulos_orfaos


def _publish_source_type(rel: str) -> str:
    return "code-repo" if rel in _PUBLISH_CODE_REPO_REL else "agent-output"


# ---------- FIX 3: portão de `verify` (promote/compile/docx não promovem/
# compilam/exportam conteúdo de um workdir de codescan cujo verify falhou) ----------


def _codescan_workdirs_for_topic(store_root: str, topic: str | None) -> list[str]:
    """Workdirs em `<store>/.codescan/*` cujo `state.json['topic']` bate com
    `topic` (todos, se `topic` for None/vazio). [] se não houver `.codescan/`
    no store — store só-manual (transcrições/docs) nunca tem workdir, então
    nunca é afetado pelo portão de verify."""
    codescan_root = os.path.join(store_root, ".codescan")
    if not os.path.isdir(codescan_root):
        return []
    out = []
    for name in sorted(os.listdir(codescan_root)):
        wd = os.path.join(codescan_root, name)
        if not os.path.isdir(wd):
            continue
        state = _state_json(wd)
        if not state:
            continue
        if topic and state.get("topic") != topic:
            continue
        out.append(wd)
    return out


def _failed_verify_workdirs(store_root: str, topic: str | None) -> list[dict]:
    """Workdirs (escopados por `topic`, se informado) com
    `stages.verify.status == 'failed'`. Base determinística do bloqueio do
    FIX 3 — nunca considera workdirs sem stage `verify` registrado (versões
    antigas do codescan, ou pipeline ainda não chegou lá) como falhos."""
    failed = []
    for wd in _codescan_workdirs_for_topic(store_root, topic):
        state = _state_json(wd) or {}
        verify = ((state.get("stages") or {}).get("verify")) or {}
        if verify.get("status") == "failed":
            failed.append(
                {
                    "workdir": os.path.relpath(wd, store_root).replace("\\", "/"),
                    "topic": state.get("topic"),
                    "at": verify.get("at"),
                }
            )
    return failed


def _verify_gate_scope(
    store_root: str, topic: str | None, allow_unverified: bool
) -> tuple[dict | None, list[dict]]:
    """Portão de `verify` para comandos escopáveis por `topic` (`compile`,
    `docx`). `topic=None` varre TODOS os workdirs de codescan do store (modo
    "processa tudo"); informado, escopa só ao(s) workdir(s) daquele topic.

    Devolve `(bloqueio, sobrepostos)`:
    - `bloqueio` None = liberado; dict = erro acionável (chamador deve
      imprimir em stderr e abortar com código != 0);
    - `sobrepostos` = workdirs cujo verify falhou mas foram liberados via
      `--allow-unverified` (decisão humana explícita) — chamador deve
      registrar no output e no log.
    """
    failed = _failed_verify_workdirs(store_root, topic)
    if not failed:
        return None, []
    if allow_unverified:
        return None, failed
    return (
        {
            "error": (
                f"`verify` falhou para {len(failed)} workdir(s) de codescan — "
                "comando bloqueado (README/INSTALL: conteúdo não verificado "
                "não deve ser promovido/compilado/exportado)"
            ),
            "workdirs_bloqueados": failed,
            "acao": (
                "rode `wk code --repo <repo> verify --artifact "
                "<workdir>/sdd/confirmed.md` e corrija as citações reprovadas "
                "(ou rebaixe a claim para inferred.md); para prosseguir mesmo "
                "assim (decisão humana explícita, registrada no log/manifesto), "
                "repita o comando com --allow-unverified"
            ),
        },
        [],
    )


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

    # F-18: `--topic` é OBRIGATÓRIO. Sem ele, toda fonte publicada entrava em
    # inbox/ sem `topic`, e o portão de verify do `promote` (que só roda
    # `if item_topic:`) era silenciosamente contornado — artefato de codescan
    # com `verify` reprovado virava canônico sem ninguém decidir nada. A
    # obrigatoriedade fica aqui (e não em `required=True` do argparse) para o
    # erro sair no contrato de saída do comando: JSON com `acao`, exit 2.
    topic = (getattr(a, "topic", None) or "").strip()
    if not topic:
        print(
            json.dumps(
                {
                    "error": "publish exige --topic (não vazio)",
                    "acao": (
                        "repita com `wk publish --workdir <workdir> --topic <slug> "
                        "--store <store>`; o topic é o que liga a fonte ao portão de "
                        "verify do `promote` e ao caminho da página em wiki/"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    # F-01: o topic vai para o frontmatter e depois vira caminho em wiki/ e
    # wiki-docx/; recusa aqui, na entrada, em vez de deixar a fonte envenenada
    # entrar no store.
    try:
        _slug_topic(topic)
    except PathComponentError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2

    workdir = os.path.abspath(a.workdir)
    if not os.path.isdir(workdir):
        print(json.dumps({"error": f"workdir não encontrado: {a.workdir}"}, ensure_ascii=False), file=sys.stderr)
        return 2

    store_root = _store_root(a)
    inbox_root = os.path.abspath(os.path.join(store_root, "inbox"))
    repo_name = _repo_name_from_workdir(workdir)
    candidates, modulos_orfaos = _publish_candidates(workdir)

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

        # F-01: `stem_slug` e `doc_id` viram nome de arquivo em inbox/ e em
        # raw/assets/; ambos passam pela whitelist de componente.
        try:
            stem_slug = _slug_component(
                os.path.splitext(rel)[0].replace("/", "-"), "arquivo do workdir"
            )
            repo_slug = _slug_component(repo_name, "repo")
        except PathComponentError as exc:
            print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
            return 2
        ext = os.path.splitext(path)[1].lower()
        # FIX 1: `sdd/coupling.html` e `sdd/coupling.md` colidiriam no mesmo
        # doc_id (ambos derivam de "sdd-coupling" — o stem ignora extensão);
        # sufixo "-html" desambigua sem tocar no esquema de id dos .md
        # (mantém compatibilidade com ids já emitidos por publishes antigos).
        doc_id = f"sb-publish-{repo_slug}-{stem_slug}"
        if ext == ".html":
            doc_id += "-html"
        asset_rel = None
        if ext == ".html":
            # FIX 1: asset navegável, não markdown. Preserva os bytes originais
            # em raw/assets/ (mesmo padrão de `_ingest_asset` p/ docx/xlsx/csv/
            # pdf) e cria um stub .md — o stub É que passa pelo portão normal
            # de promote/compile; o asset em si nunca é interpretado como
            # markdown em lugar nenhum do pipeline (`cmd_docx` nunca o vê:
            # `docxgen.build_document` só recebe o stub).
            assets_dir = os.path.join(store_root, "raw", "assets")
            os.makedirs(assets_dir, exist_ok=True)
            asset_dest = _confined(
                assets_dir, os.path.join(assets_dir, f"{doc_id}.html"), "id", doc_id
            )
            shutil.copyfile(path, asset_dest)
            asset_rel = os.path.relpath(asset_dest, store_root).replace("\\", "/")
            body = (
                f"# Visualização: {os.path.basename(rel)}\n\n"
                f"Asset navegável: `{asset_rel}`\n\n"
                "Artefato HTML interativo (mapa de acoplamento), gerado "
                "deterministicamente por `scripts/codescan/coupling_java_html.py`. "
                "Não é markdown — abra o arquivo para navegar. `wk compile` copia "
                "este asset para dentro de `wiki/`, ao lado da página desta fonte, "
                "e o linka a partir dela e da visão geral do tópico.\n"
            )
        else:
            text = _read_md(path)
            _, body = split(text)  # descarta frontmatter que o artefato já tivesse

        meta = {
            "id": doc_id,
            "source_type": source_type,
            "origin": f"codescan {repo_name} — {rel}",
            "captured_at": _utc_now(),
            "promoted": False,
            "topic": topic,
        }
        dest = _publish_existing_dest(dest_dir, doc_id)
        refreshed = dest is not None
        if dest is None:
            dest = _unique_dest(os.path.join(dest_dir, f"{stem_slug}.md"))
        try:
            dest = _confined(inbox_root, dest, "arquivo do workdir", stem_slug)
        except PathComponentError as exc:
            print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
            return 2
        _write_md(dest, _render_frontmatter(meta, body))
        entry = {
            "id": doc_id,
            "source_type": source_type,
            "path": os.path.relpath(dest, store_root).replace("\\", "/"),
            "origem": rel,
        }
        if refreshed:
            entry["atualizado"] = True
        if asset_rel:
            entry["asset"] = asset_rel
        published.append(entry)

    _append_log(
        store_root,
        f"## [{_utc_now()}] publish | {len(published)} artefatos | workdir {workdir}",
    )
    out = {"workdir": workdir, "topic": topic, "publicados": published}
    if modulos_orfaos:
        out["modulos_orfaos_descartados"] = {"n": len(modulos_orfaos), "arquivos": modulos_orfaos}
    if not candidates:
        out["aviso"] = "nenhum artefato elegível em sdd/ ou modules/"

    # FIX 3: publish NÃO bloqueia (é só staging em inbox/; nada aqui vira
    # canônico sem `promote`, e agent-output nunca auto-promove) — mas avisa,
    # para o humano decidir com informação em mãos antes de aprovar/promover.
    verify_status = _failed_verify_workdirs(store_root, topic)
    ours = [v for v in verify_status if os.path.abspath(os.path.join(store_root, v["workdir"])) == workdir]
    if ours:
        out["aviso_verify"] = (
            "`verify` falhou para este workdir — os artefatos foram publicados em "
            "inbox/ (staging), mas NÃO devem ser promovidos (`wk promote`) até "
            "corrigir ou usar --allow-unverified conscientemente"
        )
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
    # BQ3: aplica o default de query só depois de `--store`/`--store=V` já
    # extraído de `rest` (podem vir em qualquer posição, ver _split_globals) —
    # senão o scanner de `_default_search_query` confundiria o valor do
    # `--store` com a query posicional.
    if rest and rest[0] == "search":
        rest = ["search"] + _default_search_query(rest[1:])
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
    pr.add_argument("--approve-all", action="store_true",
                    help="aprova em massa os itens de --source-type E --topic (ambos obrigatórios)")
    pr.add_argument("--source-type", dest="approve_source_type",
                    help="tipo exigido por --approve-all (human-transcript|human-doc|web-clip|"
                         "agent-output|code-repo); validado contra o schema")
    pr.add_argument("--topic", dest="approve_topic",
                    help="tópico exigido por --approve-all; filtra pelo `topic` do frontmatter "
                         "(F-11: fecha o escopo da aprovação em massa)")
    pr.add_argument("--approved-by", default=None,
                    help="quem aprovou; vai para promoted_by e para o log.md "
                         "(obrigatório com --approve/--approve-all — sem default)")
    pr.add_argument("--allow-unverified", action="store_true",
                    help="promove mesmo assim fontes de topic cujo `verify` do codescan falhou "
                         "(decisão humana explícita; fica registrada no log)")
    pr.set_defaults(fn=cmd_promote)

    co = sub.add_parser("compile", help="compila wiki/ a partir de raw/ promovido")
    co.add_argument("topic", nargs="?", help="tópico opcional")
    co.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    co.add_argument("--allow-unverified", action="store_true",
                    help="compila mesmo assim topic(s) cujo `verify` do codescan falhou "
                         "(decisão humana explícita; fica registrada no log/manifesto)")
    co.set_defaults(fn=cmd_compile)

    dx = sub.add_parser("docx", help="gera wiki-docx/ (DOCX) a partir de raw/ promovido")
    dx.add_argument("topic", nargs="?", help="filtro opcional por topic")
    dx.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    dx.add_argument("--out-dir", default="wiki-docx", help="pasta de saída dentro do store")
    dx.add_argument("--no-prune", action="store_true", help="não remove .docx órfão")
    dx.add_argument("--allow-unverified", action="store_true",
                    help="exporta mesmo assim topic(s) cujo `verify` do codescan falhou "
                         "(decisão humana explícita; fica registrada no log)")
    dx.set_defaults(fn=cmd_docx)

    li = sub.add_parser("lint", help="audita o índice e escreve wiki/_lint-report.md")
    li.add_argument("path", nargs="?", help="filtro opcional por caminho")
    li.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    li.set_defaults(fn=cmd_lint)

    ig = sub.add_parser("ingest", help="converte arquivo externo em inbox/ com proveniência")
    ig.add_argument("file", help="arquivo a converter (.md/.txt/.vtt/.srt/.html/.xml/.xmi/.json)")
    ig.add_argument("--source-type", dest="source_type", required=True,
                    help="human-transcript | human-doc | code-repo | agent-output | web-clip")
    ig.add_argument("--origin", required=True, help="proveniência concreta (pessoa, agente, link)")
    ig.add_argument("--topic", required=True, help="tópico do wiki-ai")
    ig.add_argument("--captured-at", dest="captured_at", help="ISO 8601 (default: agora, UTC)")
    ig.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    ig.set_defaults(fn=cmd_ingest)

    pu = sub.add_parser("publish", help="leva a árvore SDD de um workdir do codescan para inbox/")
    pu.add_argument("--workdir", required=True, help="workdir do codescan (.codescan/<repo>-<hash>)")
    # F-18: obrigatório de fato, mas checado em `cmd_publish` (não aqui) para
    # que a ausência saia como JSON com `acao`, igual aos demais erros do
    # comando, em vez do usage cru do argparse. Mesmo exit code (2).
    pu.add_argument("--topic", default=None, help="tópico do wiki-ai (OBRIGATÓRIO)")
    pu.add_argument("--store", default=None, help="raiz do store (default: WK_STORE ou ./store)")
    pu.set_defaults(fn=cmd_publish)

    # Grupos repassados às CLIs internas: parsing fica com elas.
    for name, help_ in (
        ("code", "pipeline de repositório (exige --repo)"),
        ("index", "manutenção do índice: reindex, status"),
    ):
        g = sub.add_parser(name, help=help_, add_help=False)
        g.add_argument("args", nargs=argparse.REMAINDER)
    se = sub.add_parser(
        "search",
        help="busca híbrida no store (prefixos lex:/vec:/hyde:; sem prefixo em linha única -> lex:)",
        add_help=False,
    )
    se.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help=(
            "query document: linhas 'lex: termo', 'vec: descrição' e/ou 'hyde: parágrafo' "
            "(prefixos aceitos pelo motor); texto livre de uma linha sem prefixo recebe "
            "'lex:' automaticamente (default; único modo sem embeddings configurados); "
            "omita para ler do stdin"
        ),
    )
    for name, help_ in (
        ("get", "recupera documento ou trecho"),
        ("audit", "regras determinísticas L1/L2/L5"),
    ):
        g = sub.add_parser(name, help=help_, add_help=False)
        g.add_argument("args", nargs=argparse.REMAINDER)
    return p


_PASSTHROUGH_INDEX = ("search", "get", "audit")

# Prefixos de modo aceitos pelo motor (scripts/sbindex/cli.py:parse_query_doc,
# confirmado em código: lex/vec/hyde). Texto livre de uma linha sem nenhum
# deles falha no motor com "linha inválida no query document" — o --help de
# `wk search` não deixa isso óbvio, então aplicamos o default aqui.
_SEARCH_MODE_PREFIXES = ("lex:", "vec:", "hyde:")
# Flags do subcomando `search` do motor (sbindex/cli.py) que consomem o
# próximo token como valor — necessário para achar o positional `query` sem
# reimplementar o parsing do motor.
_SEARCH_FLAGS_WITH_VALUE = ("-c", "--collection", "--filter", "-n", "--format")
_SEARCH_FLAGS_BOOL = ("--full", "-h", "--help")


def _default_search_query(argv_rest: list[str]) -> list[str]:
    """Prefixa `lex:` numa query de texto livre de uma linha, sem prefixo de
    modo conhecido. Query multi-linha (documento de query real, com `\\n`) ou
    já prefixada não é tocada; query omitida (lida do stdin) também não."""
    out = list(argv_rest)
    i = 0
    while i < len(out):
        tok = out[i]
        if tok in _SEARCH_FLAGS_WITH_VALUE:
            i += 2
            continue
        if tok in _SEARCH_FLAGS_BOOL:
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok and "\n" not in tok and not tok.startswith(_SEARCH_MODE_PREFIXES):
            out[i] = f"lex:{tok}"
        break
    return out


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
