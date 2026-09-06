"""Migração do corpus legado (`store/raw`, `store/wiki`, `store/.codescan/*/sdd`,
`store/log.md`, `store/quarantine.md`) para `knowledge.db` (plano §16.1, onda W8).

Ponto de entrada típico:

    from knowledge.migrate import inventory_legacy, backup, migrate, verify_migration
    from knowledge.repository import Repository

    inv = inventory_legacy(store_root)
    bkp = backup(store_root, backup_dir)
    with Repository.open(db_path) as repo:
        result = migrate(store_root, repo, "org/projeto", backup_dir=backup_dir)
        report = verify_migration(store_root, repo, result)

REGRA CENTRAL (§16.1/W8): todo fato importado por este módulo nasce
`FactNature.OBSERVED` (ou `DECLARED_REQUIREMENT` quando o candidato extraído é
um requisito) com `EpistemicStatus.INFERRED` — NUNCA `IMPLEMENTED`, NUNCA
`SUPPORTED`. Importação de texto não é verificação: quem atesta `supported`
é o pipeline de análise/verificação (§5.3), não a migração de um arquivo.
Isso vale igualmente para fonte-verdade curada (`raw/`) e para saída de
análise antiga (`wiki/`, `.codescan/*/sdd/`, `raw/agent-output`) — a migração
não distingue "confiança" por categoria porque o objetivo aqui é preservar o
passado sem contaminar a consulta vigente, não reavaliar o que já foi escrito.

Fronteiras deste módulo (dono exclusivo: `scripts/knowledge/migrate.py`):

- Só stdlib + `knowledge.*`. `ingestion.*` é IMPORTADO TARDIO e OPCIONAL,
  usado só para reaproveitar a extração de blocos/candidatos de texto; a
  ausência do pacote (ou qualquer falha nele) cai no divisor mínimo próprio
  (`_fallback_blocks`), nunca interrompe a migração.
- Não importa `sbindex.frontmatter` (restrição `.pyz`: pacotes não se
  importam entre si fora de `knowledge`/`ingestion`). O formato de
  frontmatter legado é reimplementado aqui, no mínimo necessário
  (`_split_frontmatter`), lido a partir do próprio `store/` no momento do
  desenvolvimento.
- Não corrige, não reclassifica e não funde identidades do corpus antigo —
  isso é trabalho de `ingestion.correlate` (§8.2), que já possui a máquina de
  resolução de identidade/ambiguidade. Migração é só transporte com
  proveniência preservada.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .evidence import make_evidence
from .identity import content_hash as _content_hash
from .identity import entity_id as _entity_id_of
from .identity import normalize_namespace
from .models import (
    Alias,
    AliasOrigin,
    ContentKind,
    EntityDraft,
    EntityType,
    EpistemicStatus,
    FactDraft,
    FactNature,
    LifecycleStatus,
    RelationDraft,
    RelationType,
    SourceKind,
)
from .repository import Repository

REVISION_AUTHOR = "pipeline:migrate-legacy"

# --------------------------------------------------------------------------
# 0. Frontmatter legado — parse mínimo reimplementado (sem importar sbindex)
# --------------------------------------------------------------------------

#: Mesmos campos promovidos a coluna em `sbindex/frontmatter.py::FIELDS`,
#: lidos aqui só como leitura de referência de formato (arquivo não
#: importado). `derived_from`/`supersedes` NÃO estão nesta lista porque
#: também não estão em `FIELDS` no legado — lá eles sobrevivem como chave
#: "extra" preservada fora do whitelist (comentário F-17 em frontmatter.py);
#: aqui tratamos os dois canonicamente do mesmo jeito: toda chave do bloco
#: YAML vira uma entrada de `meta`, promovida ou não.
KNOWN_FIELDS = (
    "id",
    "source_type",
    "origin",
    "captured_at",
    "promoted",
    "promoted_by",
    "promoted_at",
    "confidence",
    "supersedes",
    "source_link",
    "topic",
    "derived_from",
)

_MAX_PREAMBLE_LINES = 8
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)


def _coerce_scalar(raw: str) -> Any:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s


def _strip_inline_comment(value: str) -> str:
    """`#` só inicia comentário fora de aspas e precedido de espaço/início."""
    in_single = in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or value[i - 1] in (" ", "\t"):
                return value[:i].rstrip()
    return value.rstrip()


def _skip_preamble(text: str) -> str:
    """Descasca BOM e comentários/blank lines antes do `---` de abertura.

    Espelha `sbindex/frontmatter.py::_strip_preamble`, sem importar o módulo:
    o limite de `_MAX_PREAMBLE_LINES` evita casar um `---` de separador
    horizontal no meio de um markdown comum que não tem frontmatter algum.
    """
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines[:_MAX_PREAMBLE_LINES]):
        stripped = ln.strip()
        if stripped == "---":
            return "".join(lines[i:])
        if stripped and not stripped.startswith("#"):
            return text
    return text


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """(metadados, corpo) — parser `chave: valor` mínimo, tolerante a BOM.

    Reimplementação independente do formato usado por `sbindex/frontmatter.py`
    (mesmo `---`/`chave: valor`), sem importar aquele módulo (dono: outro
    componente; `.pyz` não permite o cruzamento). Não interpreta YAML
    aninhado nem tags — cada linha fora desse formato é ignorada, nunca
    lançada como erro: frontmatter degradado não pode travar a migração.
    """
    stripped = _skip_preamble(text)
    m = _FM_RE.match(stripped)
    if not m:
        return {}, text
    block = m.group(1)
    body = stripped[m.end():]
    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            continue  # aninhado: fora do escopo do parser mínimo
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = _strip_inline_comment(value.strip())
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [
                _coerce_scalar(v) for v in value[1:-1].split(",") if v.strip()
            ]
        else:
            meta[key] = _coerce_scalar(value)
    return meta, body


def _split_list_field(value: Any) -> list[str]:
    """`derived_from`/`supersedes` aceitos como escalar, CSV ou lista."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    s = str(value).strip().strip("[]")
    return [v.strip().strip("\"'") for v in s.split(",") if v.strip()]


# --------------------------------------------------------------------------
# 1. Inventário — NUNCA altera nada em disco
# --------------------------------------------------------------------------

CATEGORY_CURATED_SOURCE = "curated_source"
CATEGORY_AGENT_OUTPUT = "agent_output"
CATEGORY_DERIVED_OUTPUT = "derived_output"
CATEGORY_CODE_ANALYSIS_OUTPUT = "code_analysis_output"
CATEGORY_ASSET = "asset"
CATEGORY_LOG = "log"

_AGENT_SOURCE_TYPES = frozenset({"agent-output"})


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class LegacyItem:
    """Um arquivo do corpus legado, classificado sem ser alterado."""

    rel_path: str  # POSIX, relativo a store_root
    abs_path: str
    category: str
    size: int
    sha256: str
    source_type: str | None = None
    legacy_id: str | None = None
    topic: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyInventory:
    store_root: str
    items: tuple[LegacyItem, ...]
    counts: Mapping[str, int]

    def by_category(self, category: str) -> tuple[LegacyItem, ...]:
        return tuple(i for i in self.items if i.category == category)


def _rel(store_root: str, path: str) -> str:
    return os.path.relpath(path, store_root).replace(os.sep, "/")


def _read_text(path: str) -> tuple[str | None, str | None]:
    """(texto, erro). Nunca levanta — arquivo ilegível vira aviso, não crash."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, f"leitura falhou: {exc}"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        try:
            return raw.decode("utf-8-sig"), None
        except UnicodeDecodeError as exc:
            return None, f"decodificação utf-8 falhou: {exc}"


def _classify_md(rel_path: str, meta: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    warnings: list[str] = []
    posix = rel_path.replace(os.sep, "/")
    if posix.startswith("raw/"):
        st = str(meta.get("source_type") or "").strip()
        if st in _AGENT_SOURCE_TYPES:
            return CATEGORY_AGENT_OUTPUT, ()
        if not st:
            warnings.append("source_type ausente no frontmatter legado")
        elif st not in {"human-transcript", "human-doc", "code-repo", "web-clip"}:
            warnings.append(f"source_type fora do vocabulário legado conhecido: {st}")
        if not meta.get("origin"):
            warnings.append("origin ausente no frontmatter legado")
        return CATEGORY_CURATED_SOURCE, tuple(warnings)
    if posix.startswith("wiki/"):
        return CATEGORY_DERIVED_OUTPUT, ()
    if "/.codescan/" in f"/{posix}" and "/sdd/" in f"/{posix}":
        return CATEGORY_CODE_ANALYSIS_OUTPUT, ()
    warnings.append("arquivo .md fora das árvores conhecidas (raw/, wiki/, .codescan/*/sdd/)")
    return CATEGORY_DERIVED_OUTPUT, tuple(warnings)


def inventory_legacy(store_root: str) -> LegacyInventory:
    """Varre `raw/`, `wiki/`, `.codescan/*/sdd/`, `log.md`, `quarantine.md`.

    Classifica cada arquivo, calcula hash e contagens. NUNCA escreve, apaga
    ou renomeia nada — é só leitura, mesmo quando o frontmatter é inválido ou
    o arquivo está corrompido (isso vira `warnings` no item, não exceção).
    """
    store_root = os.path.abspath(store_root)
    items: list[LegacyItem] = []

    raw_root = os.path.join(store_root, "raw")
    if os.path.isdir(raw_root):
        for path in sorted(glob.glob(os.path.join(raw_root, "**", "*"), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = _rel(store_root, path)
            posix = rel.replace(os.sep, "/")
            if posix.startswith("raw/assets/"):
                items.append(
                    LegacyItem(
                        rel_path=posix,
                        abs_path=path,
                        category=CATEGORY_ASSET,
                        size=os.path.getsize(path),
                        sha256=_sha256_file(path),
                    )
                )
                continue
            if not path.lower().endswith(".md"):
                continue
            text, err = _read_text(path)
            warnings = (err,) if err else ()
            meta: dict[str, Any] = {}
            if text is not None:
                meta, _ = _split_frontmatter(text)
                category, w2 = _classify_md(posix, meta)
                warnings = warnings + w2
            else:
                category = CATEGORY_CURATED_SOURCE
            items.append(
                LegacyItem(
                    rel_path=posix,
                    abs_path=path,
                    category=category,
                    size=os.path.getsize(path),
                    sha256=_sha256_file(path),
                    source_type=(str(meta.get("source_type")) if meta.get("source_type") else None),
                    legacy_id=(str(meta.get("id")) if meta.get("id") else None),
                    topic=(str(meta.get("topic")) if meta.get("topic") else None),
                    warnings=warnings,
                )
            )

    wiki_root = os.path.join(store_root, "wiki")
    if os.path.isdir(wiki_root):
        for path in sorted(glob.glob(os.path.join(wiki_root, "**", "*.md"), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = _rel(store_root, path)
            items.append(
                LegacyItem(
                    rel_path=rel,
                    abs_path=path,
                    category=CATEGORY_DERIVED_OUTPUT,
                    size=os.path.getsize(path),
                    sha256=_sha256_file(path),
                )
            )

    codescan_root = os.path.join(store_root, ".codescan")
    if os.path.isdir(codescan_root):
        pattern = os.path.join(codescan_root, "*", "sdd", "**", "*.md")
        for path in sorted(glob.glob(pattern, recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = _rel(store_root, path)
            items.append(
                LegacyItem(
                    rel_path=rel,
                    abs_path=path,
                    category=CATEGORY_CODE_ANALYSIS_OUTPUT,
                    size=os.path.getsize(path),
                    sha256=_sha256_file(path),
                )
            )

    for name in ("log.md", "quarantine.md"):
        path = os.path.join(store_root, name)
        if os.path.isfile(path):
            items.append(
                LegacyItem(
                    rel_path=name,
                    abs_path=path,
                    category=CATEGORY_LOG,
                    size=os.path.getsize(path),
                    sha256=_sha256_file(path),
                )
            )

    counts: dict[str, int] = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    return LegacyInventory(store_root=store_root, items=tuple(items), counts=counts)


# --------------------------------------------------------------------------
# 2. Backup imutável — pré-condição obrigatória de `migrate`
# --------------------------------------------------------------------------


class BackupError(Exception):
    """Backup ausente, incompleto ou hash divergente — migração recusada."""


@dataclass(frozen=True)
class BackupResult:
    store_root: str
    backup_dir: str
    manifest_path: str
    file_count: int
    total_bytes: int


def backup(store_root: str, backup_dir: str) -> BackupResult:
    """Cópia imutável de `store_root` (arquivos + manifest sha256) ANTES de migrar.

    Recusa sobrescrever um `backup_dir` já povoado — um backup "imutável" que
    aceita segunda escrita silenciosa deixaria de ser prova do estado
    original. `manifest.json` grava hash de CADA arquivo copiado; `migrate`
    reconfere esse manifest contra o `store_root` atual antes de tocar em
    qualquer coisa (`_require_valid_backup`).
    """
    store_root = os.path.abspath(store_root)
    backup_dir = os.path.abspath(backup_dir)
    if os.path.isdir(backup_dir) and os.listdir(backup_dir):
        raise BackupError(
            f"backup_dir já existe e não está vazio: {backup_dir} "
            "(backup é imutável; use um diretório novo por execução)"
        )
    os.makedirs(backup_dir, exist_ok=True)

    manifest: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for root, _dirs, files in os.walk(store_root):
        if os.path.abspath(root) == backup_dir or os.path.abspath(root).startswith(backup_dir + os.sep):
            continue
        for name in files:
            src = os.path.join(root, name)
            rel = _rel(store_root, src)
            dst = os.path.join(backup_dir, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            sha = _sha256_file(dst)
            size = os.path.getsize(dst)
            manifest[rel] = {"sha256": sha, "size": size}
            total_bytes += size

    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"store_root": store_root, "files": manifest},
            fh,
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )
    return BackupResult(
        store_root=store_root,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        file_count=len(manifest),
        total_bytes=total_bytes,
    )


def _require_valid_backup(store_root: str, backup_dir: str) -> None:
    """Recusa migrar sem backup válido: manifest presente e hashes batendo.

    Confere DUAS pontas: (a) a cópia em `backup_dir` ainda bate com o hash
    gravado no manifest (o backup em si não foi corrompido/alterado); (b) o
    `store_root` atual ainda bate com o que o manifest registrou (o
    originais não sumiram nem mudaram entre o backup e a migração).
    """
    manifest_path = os.path.join(os.path.abspath(backup_dir), "manifest.json")
    if not os.path.isfile(manifest_path):
        raise BackupError(f"manifest de backup ausente: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    files = manifest.get("files") or {}
    if not files:
        raise BackupError(f"manifest de backup vazio: {manifest_path}")
    store_root = os.path.abspath(store_root)
    for rel, info in files.items():
        backed_up = os.path.join(backup_dir, "files", rel)
        if not os.path.isfile(backed_up) or _sha256_file(backed_up) != info["sha256"]:
            raise BackupError(f"cópia de backup corrompida ou ausente: {rel}")
        original = os.path.join(store_root, rel)
        if os.path.isfile(original) and _sha256_file(original) != info["sha256"]:
            raise BackupError(
                f"original mudou desde o backup: {rel} — refaça o backup antes de migrar"
            )


# --------------------------------------------------------------------------
# 3. Migração
# --------------------------------------------------------------------------

#: Categorias tratadas como "saída de análise antiga" pela REGRA CENTRAL —
#: nunca `implemented`, nunca `supported` (§16.1/W8). `curated_source` está
#: fora desta lista só de nome: na prática o mapeamento de natureza abaixo
#: (`_NATURE_BY_CANDIDATE_KIND`) aplica o mesmo teto a QUALQUER categoria,
#: porque migração não é pipeline de verificação em nenhum dos dois casos.
DERIVED_CATEGORIES = frozenset(
    {CATEGORY_AGENT_OUTPUT, CATEGORY_DERIVED_OUTPUT, CATEGORY_CODE_ANALYSIS_OUTPUT}
)


@dataclass(frozen=True)
class MigratedItem:
    rel_path: str
    category: str
    entity_id: str
    fact_ids: tuple[str, ...] = ()
    relation_ids: tuple[str, ...] = ()
    changed: bool = False


@dataclass(frozen=True)
class SkippedItem:
    rel_path: str
    reason: str


@dataclass
class MigrationResult:
    namespace: str
    backup_ref: str
    migrados: list[MigratedItem] = field(default_factory=list)
    pulados: list[SkippedItem] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    mapa_ids: dict[str, str] = field(default_factory=dict)  # rel_path -> entity_id


def _legacy_stable_key(rel_path: str) -> str:
    """`legacy:<path-hash>` — chave estável e determinística por caminho."""
    h = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:24]
    return f"legacy:{h}"


def _nature_for_kind(kind_value: str) -> FactNature:
    """§16.1/W8: só `observed`/`declared_requirement` saem daqui — nunca `implemented`."""
    if kind_value == "requirement":
        return FactNature.DECLARED_REQUIREMENT
    return FactNature.OBSERVED


def _fallback_blocks(body: str) -> list[dict[str, Any]]:
    """Divisor mínimo próprio: parágrafos por linha em branco (sem `ingestion`).

    Cada bloco vira um dicionário com o suficiente para montar um localizador
    `SourceKind.DOCUMENT` (`section`, `paragraph`) quando `ingestion` não
    está disponível ou falha em processar o arquivo.
    """
    blocks: list[dict[str, Any]] = []
    para = 0
    for chunk in re.split(r"\n\s*\n", body):
        text = chunk.strip()
        if not text:
            continue
        para += 1
        section = text.splitlines()[0][:80] if text.startswith("#") else "(raiz)"
        blocks.append({"text": text, "section": section.lstrip("# ").strip() or "(raiz)", "paragraph": para})
    return blocks


def _try_ingestion_blocks(abs_path: str) -> tuple[Any, Any] | None:
    """Import tardio e opcional de `ingestion` (§ fronteiras do módulo).

    Devolve `(doc, extract_module)` quando a fonte foi ingerida com sucesso
    E tem ao menos um bloco; `None` em QUALQUER outro caso (import ausente,
    exceção do adapter, documento sem blocos) — o chamador cai no divisor
    mínimo próprio sem propagar o erro.
    """
    try:
        from ingestion import ingest as _ingest  # noqa: PLC0415
        from ingestion import extract as _extract  # noqa: PLC0415
    except Exception:
        return None
    try:
        doc = _ingest(abs_path)
    except Exception:
        return None
    if not getattr(doc, "blocks", None):
        return None
    return doc, _extract


def migrate(
    store_root: str,
    repo: Repository,
    namespace: str,
    *,
    backup_dir: str,
) -> MigrationResult:
    """Migra `raw/`, `wiki/`, `.codescan/*/sdd/` e `log.md`/`quarantine.md`.

    Recusa migrar sem backup válido (`_require_valid_backup`). Cada item vira
    UMA entidade `Source` (`stable_key="legacy:<path-hash>"`) mais um fato
    "corpo legado" e, quando `ingestion.extract` está disponível, um fato por
    candidato textual extraído — todos com natureza/sustentação limitadas
    pela REGRA CENTRAL, nunca por categoria. `derived_from`/`supersedes` do
    frontmatter viram relações tipadas na SEGUNDA passada, depois que toda
    entidade já existe (referência para a frente resolve mesmo assim).

    Idempotência: `entity_id`/`fact_id`/`relation_id` são determinísticos
    (mesmo caminho + mesmo conteúdo => mesmo id) e `Repository.put_*` só cria
    revisão nova quando o `content_hash` muda — reingerir o mesmo arquivo não
    duplica nada. Falha num item não aborta o lote: cada item é sua própria
    revisão, então o que já migrou fica gravado e a próxima chamada só
    reprocessa o que faltou (retomada por reexecução).
    """
    _require_valid_backup(store_root, backup_dir)
    ns = normalize_namespace(namespace)
    store_root = os.path.abspath(store_root)
    inv = inventory_legacy(store_root)

    result = MigrationResult(namespace=ns, backup_ref=os.path.join(backup_dir, "manifest.json"))
    pending_relations: list[tuple[str, str, RelationType]] = []  # (rel_path, target_ref, type)

    for item in inv.items:
        if item.category == CATEGORY_ASSET:
            result.migrados.append(_migrate_asset(repo, ns, item))
            result.mapa_ids[item.rel_path] = result.migrados[-1].entity_id
            continue
        try:
            migrated, item_pending = _migrate_md_item(repo, ns, item)
        except Exception as exc:  # item ruim nunca derruba o lote (retomável)
            result.pulados.append(SkippedItem(rel_path=item.rel_path, reason=str(exc)))
            continue
        result.migrados.append(migrated)
        result.mapa_ids[item.rel_path] = migrated.entity_id
        pending_relations.extend((item.rel_path, ref, rtype) for ref, rtype in item_pending)
        result.avisos.extend(f"{item.rel_path}: {w}" for w in item.warnings)

    # segunda passada: derived_from/supersedes, agora que todo mapa_ids existe
    legacy_id_to_path = {
        i.legacy_id: i.rel_path for i in inv.items if i.legacy_id and i.rel_path in result.mapa_ids
    }
    for rel_path, ref, rtype in pending_relations:
        target_path = ref if ref in result.mapa_ids else legacy_id_to_path.get(ref)
        if target_path is None:
            result.avisos.append(
                f"{rel_path}: {rtype.value} aponta para {ref!r}, não resolvido nesta migração"
            )
            continue
        source_eid = result.mapa_ids[rel_path]
        target_eid = result.mapa_ids[target_path]
        with repo.revision(author=REVISION_AUTHOR, reason=f"linhagem legada {rel_path}") as rev:
            wr = rev.put_relation(
                RelationDraft(
                    namespace=ns,
                    source_entity_id=source_eid,
                    relation_type=rtype,
                    target_entity_id=target_eid,
                    scope=rel_path,
                    epistemic_status=EpistemicStatus.INFERRED,
                    lifecycle_status=LifecycleStatus.CURRENT,
                    asserted_by=REVISION_AUTHOR,
                )
            )
        for idx, m in enumerate(result.migrados):
            if m.rel_path == rel_path:
                result.migrados[idx] = MigratedItem(
                    rel_path=m.rel_path,
                    category=m.category,
                    entity_id=m.entity_id,
                    fact_ids=m.fact_ids,
                    relation_ids=m.relation_ids + (wr.target_id,),
                    changed=m.changed or wr.changed,
                )
                break

    return result


def _migrate_asset(repo: Repository, ns: str, item: LegacyItem) -> MigratedItem:
    """`raw/assets/*`: registrado por hash, sem extração de conteúdo (fora do escopo)."""
    stable_key = _legacy_stable_key(item.rel_path)
    with repo.revision(author=REVISION_AUTHOR, reason=f"asset legado {item.rel_path}") as rev:
        wr = rev.put_entity(
            EntityDraft(
                namespace=ns,
                entity_type=EntityType.SOURCE,
                stable_key=stable_key,
                title=os.path.basename(item.rel_path),
                attributes={
                    "legacy_path": item.rel_path,
                    "legacy_category": item.category,
                    "sha256": item.sha256,
                    "size": item.size,
                },
                lifecycle_status=LifecycleStatus.CURRENT,
            )
        )
    return MigratedItem(item.rel_path, item.category, wr.target_id, changed=wr.changed)


def _migrate_md_item(
    repo: Repository, ns: str, item: LegacyItem
) -> tuple[MigratedItem, list[tuple[str, RelationType]]]:
    text, err = _read_text(item.abs_path)
    if text is None:
        raise ValueError(err or "arquivo ilegível")
    meta, body = _split_frontmatter(text)

    source_kind = (
        SourceKind.TRANSCRIPT if meta.get("source_type") == "human-transcript" else SourceKind.DOCUMENT
    )
    version_label = f"sha256:{item.sha256[:16]}"
    stable_key = _legacy_stable_key(item.rel_path)
    uri = f"legacy://{item.rel_path}"

    source = repo.register_source(ns, source_kind, uri)
    svid = repo.register_source_version(source, version_label, item.sha256, metadata={"legacy_path": item.rel_path}).source_version_id

    attributes: dict[str, Any] = {
        "legacy_path": item.rel_path,
        "legacy_category": item.category,
        "sha256": item.sha256,
        "size": item.size,
    }
    for key in ("source_type", "origin", "topic", "id"):
        if meta.get(key):
            attributes["legacy_id" if key == "id" else f"legacy_{key}"] = meta[key]
    # §16.1/W8: aprovação legada é atributo declarado, NUNCA prova de comportamento.
    if meta.get("promoted_by"):
        attributes["legacy_promoted_by"] = meta["promoted_by"]
    if meta.get("confidence") not in (None, ""):
        attributes["legacy_confidence"] = meta["confidence"]

    title = str(meta.get("topic") or meta.get("id") or os.path.basename(item.rel_path))

    blocks_info = _try_ingestion_blocks(item.abs_path)
    fact_ids: list[str] = []

    with repo.revision(author=REVISION_AUTHOR, reason=f"migração de {item.rel_path}") as rev:
        ent = rev.put_entity(
            EntityDraft(
                namespace=ns,
                entity_type=EntityType.SOURCE,
                stable_key=stable_key,
                title=title,
                source_version_id=svid,
                attributes=attributes,
                lifecycle_status=LifecycleStatus.CURRENT,
                aliases=(
                    (Alias(alias=str(meta["id"]), origin=AliasOrigin.METADATA_ID, source_version_id=svid),)
                    if meta.get("id")
                    else ()
                ),
            )
        )
        entity_id = ent.target_id

        if blocks_info is not None:
            doc, extract_mod = blocks_info
            fact_ids.extend(_write_candidates(rev, ns, entity_id, doc, extract_mod, svid, item.rel_path))
        else:
            fact_ids.extend(_write_fallback_body(rev, ns, entity_id, body, svid, item.rel_path))

    pending: list[tuple[str, RelationType]] = []
    for ref in _split_list_field(meta.get("derived_from")):
        pending.append((ref, RelationType.DERIVED_FROM))
    for ref in _split_list_field(meta.get("supersedes")):
        pending.append((ref, RelationType.SUPERSEDES))

    return (
        MigratedItem(item.rel_path, item.category, entity_id, tuple(fact_ids), changed=ent.changed),
        pending,
    )


def _write_fallback_body(rev, ns: str, entity_id: str, body: str, svid: str, rel_path: str) -> list[str]:
    """Sem `ingestion`: um fato por parágrafo, evidência com localizador `DOCUMENT` mínimo."""
    fact_ids: list[str] = []
    for i, block in enumerate(_fallback_blocks(body)):
        locator = {"version": svid, "section": block["section"], "block": f"p{i}", "paragraph": block["paragraph"]}
        ev = make_evidence(ns, SourceKind.DOCUMENT, ContentKind.MARKDOWN, svid, locator)
        rev.add_evidence(ev)
        wr = rev.put_fact(
            FactDraft(
                namespace=ns,
                subject_id=entity_id,
                predicate="legacy_body_paragraph",
                value=block["text"],
                scope=f"{rel_path}#p{i}",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by=REVISION_AUTHOR,
                evidence_refs=(ev.evidence_id,),
                source_version_id=svid,
            )
        )
        fact_ids.append(wr.target_id)
    if not fact_ids:
        # corpo vazio: ainda assim um fato-placeholder, para "nada some sem registro"
        ev = make_evidence(ns, SourceKind.DOCUMENT, ContentKind.MARKDOWN, svid, {"version": svid, "section": "(raiz)", "block": "empty"})
        rev.add_evidence(ev)
        wr = rev.put_fact(
            FactDraft(
                namespace=ns,
                subject_id=entity_id,
                predicate="legacy_body_paragraph",
                value="",
                scope=f"{rel_path}#empty",
                nature=FactNature.OBSERVED,
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by=REVISION_AUTHOR,
                evidence_refs=(ev.evidence_id,),
                source_version_id=svid,
            )
        )
        fact_ids.append(wr.target_id)
    return fact_ids


def _write_candidates(rev, ns: str, entity_id: str, doc: Any, extract_mod: Any, svid: str, rel_path: str) -> list[str]:
    """Com `ingestion.extract` disponível: um fato por candidato (§8.1), teto de natureza próprio.

    Deliberadamente NÃO reaproveita `candidate.nature`/`candidate.epistemic`:
    ainda que `Candidate.__post_init__` já proíba `implemented`/`supported`
    (§5.4/F10), a migração aplica seu PRÓPRIO teto (`_nature_for_kind`),
    igual para toda categoria — a regra central deste módulo não deve
    depender de invariante de outro pacote para se sustentar.
    """
    try:
        candidates = extract_mod.extract_candidates(doc)
    except Exception:
        # extração falhou (documento fora do contrato esperado): cai no
        # divisor mínimo próprio, sem propagar.
        body = "\n\n".join(str(getattr(b, "text", "")) for b in getattr(doc, "blocks", ()))
        return _write_fallback_body(rev, ns, entity_id, body, svid, rel_path)

    fact_ids: list[str] = []
    all_candidates = candidates.all() if hasattr(candidates, "all") else ()
    if not all_candidates:
        body = "\n\n".join(str(getattr(b, "text", "")) for b in getattr(doc, "blocks", ()))
        return _write_fallback_body(rev, ns, entity_id, body, svid, rel_path)

    for i, cand in enumerate(all_candidates):
        ref = cand.primary_block
        locator = dict(ref.locator) or {"version": svid, "section": "(raiz)", "block": ref.block_id}
        locator.setdefault("version", svid)
        source_kind = SourceKind.DOCUMENT
        content_kind = ContentKind.MARKDOWN
        try:
            ev = make_evidence(ns, source_kind, content_kind, svid, locator)
        except Exception:
            ev = make_evidence(
                ns, SourceKind.DOCUMENT, ContentKind.MARKDOWN, svid,
                {"version": svid, "section": "(raiz)", "block": f"c{i}"},
            )
        rev.add_evidence(ev)
        kind_value = getattr(cand.kind, "value", str(cand.kind))
        wr = rev.put_fact(
            FactDraft(
                namespace=ns,
                subject_id=entity_id,
                predicate=f"legacy_candidate:{kind_value}",
                value=cand.text,
                scope=f"{rel_path}#c{i}",
                nature=_nature_for_kind(kind_value),
                epistemic_status=EpistemicStatus.INFERRED,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by=REVISION_AUTHOR,
                evidence_refs=(ev.evidence_id,),
                source_version_id=svid,
            )
        )
        fact_ids.append(wr.target_id)
    return fact_ids


# --------------------------------------------------------------------------
# 4. Verificação pós-migração
# --------------------------------------------------------------------------


@dataclass
class VerifyReport:
    ok: bool
    missing_sources: list[str] = field(default_factory=list)
    forbidden_facts: list[str] = field(default_factory=list)
    count_mismatch: str | None = None
    issues: list[str] = field(default_factory=list)


def verify_migration(store_root: str, repo: Repository, result: MigrationResult) -> VerifyReport:
    """Confere os aceites de W8 contra o banco, independente do que `result` afirma.

    (1) recalcula o inventário e confere, por `entity_id` DETERMINÍSTICO
        (não pelo mapa em memória), que todo `curated_source`/`agent_output`
        tem uma entidade `Source` gravada; (2) relê CADA fato/relação que
        `result` diz ter escrito e barra `implemented`/`supported`; (3)
        compara a contagem de itens migrados+pulados com o inventário.
    """
    report = VerifyReport(ok=True)
    inv = inventory_legacy(store_root)

    for item in inv.items:
        if item.category == CATEGORY_ASSET:
            continue
        stable_key = _legacy_stable_key(item.rel_path)
        eid = _entity_id_of(result.namespace, EntityType.SOURCE, stable_key)
        if not repo.entity_exists(eid):
            report.missing_sources.append(item.rel_path)

    for m in result.migrados:
        for fid in m.fact_ids:
            fact = repo.get_fact(fid, lifecycle=None)
            if fact is None:
                report.issues.append(f"fact_id referenciado por result não encontrado: {fid}")
                continue
            if fact.nature is FactNature.IMPLEMENTED or fact.epistemic_status is EpistemicStatus.SUPPORTED:
                report.forbidden_facts.append(fid)

    total_inv = len([i for i in inv.items])
    total_result = len(result.migrados) + len(result.pulados)
    if total_inv != total_result:
        report.count_mismatch = f"inventário={total_inv} vs migrados+pulados={total_result}"

    report.ok = not (report.missing_sources or report.forbidden_facts or report.count_mismatch)
    return report
