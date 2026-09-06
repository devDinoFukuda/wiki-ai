"""publishing.release — ativação de revisão, manifesto ativo e poda segura (plano §10.6, W6/T6.3).

Responsabilidade única deste módulo: dado um `PublicationPlan` já MONTADO por
`publishing.planner` e um par de projeções (`markdown`/`word`) já RENDERIZÁVEIS,
decidir se a revisão nova pode virar a revisão ATIVA de `out_root` — e, se não
puder, garantir que a revisão anterior continue 100% utilizável (F09/§10.6).

Fluxo de `publish_revision` (§10.6):

    1. staging = diretório IRMÃO de `out_root` (mesmo volume — promoção usa
       `os.replace`, que não atravessa volume).
    2. Renderiza TODAS as saídas (markdown/ e word/) dentro do staging.
    3. Roda os validadores recebidos por injeção sobre o staging.
    4. Só se TODOS os validadores (e a própria renderização) disserem `ok`:
       promove staging → out_root arquivo a arquivo (`os.replace`), escreve
       `manifest.json` novo por escrita atômica (tmp + `os.replace`).
    5. Qualquer falha em 2/3: staging é removido, NADA em `out_root` é tocado
       — nem o `manifest.json` antigo, nem os arquivos antigos — e o retorno
       carrega os bloqueios detalhados (nunca uma exceção solta).

Consumidores nunca veem um `manifest.json` apontando para metade de um
conjunto: a troca do arquivo é a ÚLTIMA operação de uma publicação bem
sucedida, depois que todo o resto já está gravado em disco.

Contrato dos vizinhos (injetados, NUNCA importados por este módulo — os
outros arquivos de `publishing/` são responsabilidade de outro agente,
concorrentemente em construção):

    plan.documents                          -> Sequence[KnowledgeDocument]
    document.units / .unidades / .semantic_units -> Sequence[SemanticUnit]
    unit.unit_id                            -> str

    renderers.markdown.render(document)         -> str  (texto Markdown)
    renderers.markdown.render_manifest(plan)    -> {unit_id: {..., "state",
                                                     "fact_ids", "blocked",
                                                     "blocked_reasons", ...}}
    renderers.word.render(document)             -> bytes (.docx)
    renderers.word.render_manifest_word(plan)   -> mesmo formato acima
                                                    (confirmado em
                                                    `publishing/word.py`,
                                                    chave "docx_filename")

    validators.validate_equivalence(plan, staging_root)     -> Report
    validators.validate_semantics(plan, staging_root)       -> Report
    validators.validate_docx_structure(plan, staging_root)  -> Report
    Report ~ objeto/dict com `ok: bool` e `bloqueios`/`errors: list[str]`
    (`_coerce_report` aceita qualquer um dos dois nomes de campo — o
    `Report` real de `publishing.validate` usa `errors`; expõe `bloqueios`
    como alias por compatibilidade, mas este módulo não depende disso).

    `renderers`/`validators` podem ser passados como objeto com esses
    atributos OU como `dict` (normalizado internamente); a leitura desses
    objetos é só por nome de campo (duck typing), como já é convenção em
    `publishing/word.py` (`_get`). Os nomes de chave de `render_manifest`
    (markdown) são um MELHOR PALPITE hoje (módulo ainda não existe):
    `_first_present` tenta várias variantes e a leitura nunca quebra o
    processo — na pior hipótese vira bloqueio explícito, nunca publicação
    incompleta silenciosa. Ver "Limitações" no relatório de entrega.

    Único vizinho importado por este módulo: `publishing.validate` — e só
    via IMPORT PREGUIÇOSO (dentro de `_default_validators`), só quando
    `publish_revision` é chamado SEM `validators` explícito. Quem injeta
    seus próprios `validators` (produção real ou teste) nunca paga esse
    import. `publishing.document/planner/markdown/word` continuam nunca
    importados aqui.

Cada `document` vira exatamente UM arquivo Markdown e UM arquivo .docx
(confirmado por `publishing/word.py: render`/`render_manifest_word`, que
produz um único .docx por documento com várias unidades dentro). O manifesto
de release, no entanto, é indexado por `unit_id` (contrato pedido pela
tarefa): várias unidades do mesmo documento apontam para o MESMO par
`md_path`/`docx_path`.

Nomes de arquivo são ESTÁVEIS por `unit_id` (plano §10.6 item 5): quando uma
unidade já existia no manifesto ativo anterior, o caminho gravado ali é
reaproveitado ao pé da letra, mesmo que o renderer sugira um nome diferente
nesta rodada — isso é o que impede duas revisões de competerem por nomes
diferentes para a mesma unidade.

Poda (`prune`) só reconhece como substituído/removível um arquivo que:
  (a) tinha aquele `unit_id` no manifesto anterior E o `unit_id` sumiu do
      manifesto ativo, com motivo registrado em `removal_reasons`; ou
  (b) tinha aquele caminho no manifesto anterior e o caminho ativo da MESMA
      unidade mudou (caso raro, dado (5) acima).
Em ambos os casos o arquivo é MOVIDO (nunca apagado) para
`out_root/.history/<revision_id_anterior>/<caminho>`.

Conteúdo sobrescrito NO MESMO caminho (unidade que mudou de conteúdo mas
manteve o nome — o caso comum) é copiado para `.history/<revisão_anterior>/`
durante a PRÓPRIA promoção, antes do `os.replace` — é o único instante em que
o byte antigo ainda existe; sem isso `rollback` não teria como restaurá-lo.

Estados (§11.2): só `generated` → `validated` → `published` são atribuídos
aqui. `retrievable` NUNCA aparece neste módulo — ele exige verificação no
destino (SharePoint/Copilot Studio), que é responsabilidade de W7.

Somente stdlib e `knowledge` (reuso de `knowledge.identity.content_hash` para
o hash canônico), mais o import preguiçoso de `publishing.validate` descrito
acima (só dentro de `_default_validators`, só quando `publish_revision` é
chamado sem `validators` explícito). Nenhum import de `wk`/`codescan`/
`sbindex`, nenhum import de `publishing.document`/`planner`/`markdown`/`word`
em nenhuma circunstância.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Any,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

try:  # pragma: no cover - caminho normal quando scripts/ já está no sys.path
    from knowledge.identity import content_hash as _canonical_hash
except ImportError:  # pragma: no cover
    _SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from knowledge.identity import content_hash as _canonical_hash


MANIFEST_FILENAME = "manifest.json"
HISTORY_DIRNAME = ".history"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Estados (§11.2)
# ---------------------------------------------------------------------------


class DocumentState:
    """Estados de publicação por documento/unidade (§11.2).

    `retrievable` é um quarto estado do plano, mas DELIBERADAMENTE não
    existe aqui: exige verificar a fonte de destino (upload concluído não
    significa conteúdo já recuperável), o que é escopo de W7. Nenhuma
    função deste módulo atribui `retrievable` a nada.
    """

    GENERATED = "generated"
    VALIDATED = "validated"
    PUBLISHED = "published"

    ALL = frozenset({GENERATED, VALIDATED, PUBLISHED})


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------


class ReleaseError(RuntimeError):
    """Erro do domínio de publicação (ativação, manifesto, poda ou rollback)."""


class ManifestCorruptError(ReleaseError):
    """`manifest.json` (ativo ou em `.history`) existe mas não pôde ser lido."""


class PruneError(ReleaseError):
    """`prune` recusou operar por não conseguir identificar substitutos com segurança."""


# ---------------------------------------------------------------------------
# Contratos estruturais dos vizinhos (Protocol — NUNCA importa os módulos)
# ---------------------------------------------------------------------------


@runtime_checkable
class UnitLike(Protocol):
    unit_id: str


@runtime_checkable
class DocumentLike(Protocol):
    units: Sequence[UnitLike]


@runtime_checkable
class PlanLike(Protocol):
    documents: Sequence[DocumentLike]


@runtime_checkable
class MarkdownRenderer(Protocol):
    def render(self, document: DocumentLike) -> str: ...
    def render_manifest(self, plan: PlanLike) -> Mapping[str, Mapping[str, Any]]: ...


@runtime_checkable
class WordRenderer(Protocol):
    def render(self, document: DocumentLike) -> bytes: ...
    def render_manifest_word(self, plan: PlanLike) -> Mapping[str, Mapping[str, Any]]: ...


@runtime_checkable
class Renderers(Protocol):
    markdown: MarkdownRenderer
    word: WordRenderer


@dataclass(frozen=True)
class Report:
    """Formato mínimo aceito de volta dos validadores (`ok`/`bloqueios`).

    `_coerce_report` também aceita `dict` (`{"ok": ..., "bloqueios": [...]}`)
    ou `bool` puro — os validadores reais (`publishing.validate`) ainda estão
    em construção; esta tolerância evita acoplar `release.py` ao tipo exato
    que eles vierem a devolver.
    """

    ok: bool
    bloqueios: Sequence[str] = ()


@runtime_checkable
class Validators(Protocol):
    def validate_equivalence(self, plan: PlanLike, staging_root: str) -> Any: ...
    def validate_semantics(self, plan: PlanLike, staging_root: str) -> Any: ...
    def validate_docx_structure(self, plan: PlanLike, staging_root: str) -> Any: ...


# ---------------------------------------------------------------------------
# Manifesto (contrato de dados — dono deste módulo)
# ---------------------------------------------------------------------------


@dataclass
class ManifestEntry:
    unit_id: str
    md_path: str
    docx_path: str
    state: str
    fact_ids: list
    content_hash: str

    def to_json(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "md_path": self.md_path,
            "docx_path": self.docx_path,
            "state": self.state,
            "fact_ids": list(self.fact_ids),
            "content_hash": self.content_hash,
        }

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> "ManifestEntry":
        state = str(d["state"])
        if state not in DocumentState.ALL:
            raise ValueError(f"state desconhecido em manifest entry: {state!r}")
        return ManifestEntry(
            unit_id=str(d["unit_id"]),
            md_path=str(d["md_path"]),
            docx_path=str(d["docx_path"]),
            state=state,
            fact_ids=[str(x) for x in d.get("fact_ids", [])],
            content_hash=str(d["content_hash"]),
        )


@dataclass
class Manifest:
    """Manifesto ATIVO de uma revisão: `documents` é indexado por `unit_id`
    (contrato pedido pela tarefa), mesmo que várias unidades do mesmo
    `KnowledgeDocument` compartilhem o mesmo `md_path`/`docx_path`."""

    revision_id: str
    generated_at: str
    documents: dict
    previous_revision: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "generated_at": self.generated_at,
            "documents": {uid: e.to_json() for uid, e in self.documents.items()},
            "previous_revision": self.previous_revision,
        }

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> "Manifest":
        return Manifest(
            revision_id=str(d["revision_id"]),
            generated_at=str(d["generated_at"]),
            documents={
                uid: ManifestEntry.from_json(v)
                for uid, v in dict(d.get("documents", {})).items()
            },
            previous_revision=d.get("previous_revision"),
        )


# `PublicationResult` é o nome usado na tabela de contratos do plano (§13.1);
# `ReleaseResult` é o nome pedido explicitamente pela tarefa W6-T6.3. Mesmo
# papel — um só tipo, para não haver dois formatos de retorno concorrendo.
@dataclass
class ReleaseResult:
    ok: bool
    revision_id: str
    out_root: str
    manifest: Optional[Manifest]
    blocked_by: list = field(default_factory=list)


PublicationResult = ReleaseResult


@dataclass
class PruneResult:
    ok: bool
    moved: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


@dataclass
class RollbackResult:
    ok: bool
    revision_id: str
    blocked_by: list = field(default_factory=list)
    manifest: Optional[Manifest] = None


# ---------------------------------------------------------------------------
# Utilidades pequenas (tempo, ids, slugs, acesso tolerante)
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_revision_id() -> str:
    return "pub_" + uuid.uuid4().hex


def _slug(text: Any) -> str:
    folded = _WS_RE.sub(" ", str(text or "").strip()).lower()
    slug = _SLUG_RE.sub("-", folded).strip("-")
    return slug or "documento"


def _pick(obj: Any, *names: str, default: Any = None) -> Any:
    """Lê o primeiro atributo/chave não vazio dentre `names` — aceita dict,
    dataclass ou objeto arbitrário. Mesma convenção usada em
    `publishing/word.py:_get` (o contrato do vizinho é honrado por nome de
    campo, nunca por tipo importado)."""
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, Mapping):
            val = obj.get(name)
        else:
            val = getattr(obj, name, None)
        if val not in (None, "", [], {}, ()):
            return val
    return default


def _unit_ids_of(document: Any) -> list:
    units = _pick(document, "units", "unidades", "semantic_units", default=None) or []
    ids = []
    for unit in units:
        uid = _pick(unit, "unit_id", "id", default=None)
        if uid:
            ids.append(str(uid))
    return ids


def _first_present(manifest_dict: Mapping[str, Any], unit_ids: Sequence[str], *keys: str) -> Optional[str]:
    for uid in unit_ids:
        info = manifest_dict.get(uid)
        if not info:
            continue
        val = _pick(info, *keys, default=None)
        if val:
            return str(val)
    return None


def _namespaced(obj: Any) -> Any:
    """Aceita `dict` OU objeto/módulo com os atributos esperados — para não
    forçar o chamador a instanciar um tipo específico de `Renderers`/
    `Validators`."""
    if isinstance(obj, Mapping):
        return SimpleNamespace(**obj)
    return obj


# ---------------------------------------------------------------------------
# I/O atômico (mesmo padrão de `wk.cli._write_atomic`/`_promote_staged_tree`,
# reimplementado aqui — sem importar `wk`)
# ---------------------------------------------------------------------------


def _write_json_atomic(path: str, obj: Mapping[str, Any]) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".release-manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _write_manifest_atomic(path: str, manifest: Manifest) -> None:
    _write_json_atomic(path, manifest.to_json())


def _history_dir(out_root: str, revision_id: str) -> str:
    return os.path.join(out_root, HISTORY_DIRNAME, revision_id)


def _read_manifest_file(path: str) -> Optional[Manifest]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestCorruptError(f"manifesto ilegível em {path}: {exc}") from exc
    try:
        return Manifest.from_json(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestCorruptError(f"manifesto malformado em {path}: {exc}") from exc


def _load_manifest(out_root: str) -> Optional[Manifest]:
    return _read_manifest_file(os.path.join(out_root, MANIFEST_FILENAME))


def _load_history_manifest(out_root: str, revision_id: str) -> Optional[Manifest]:
    return _read_manifest_file(os.path.join(_history_dir(out_root, revision_id), MANIFEST_FILENAME))


def _staging_dir_for(out_root: str) -> str:
    """Diretório de staging IRMÃO de `out_root` (mesmo pai => mesmo volume,
    exigido por `os.replace`). Nome único via `tempfile.mkdtemp`, prefixado
    com `.` — nunca colide com um caminho publicável real nem é varrido por
    `prune` (que só enxerga dentro de `out_root`, nunca do pai)."""
    parent = os.path.dirname(os.path.abspath(out_root)) or "."
    os.makedirs(parent, exist_ok=True)
    base = os.path.basename(os.path.abspath(out_root)) or "publishing"
    return tempfile.mkdtemp(dir=parent, prefix=f".{base}.staging-")


def _write_staged(staging_root: str, relpath: str, data: bytes) -> None:
    full = os.path.join(staging_root, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)


def _same_bytes(path_a: str, path_b: str) -> bool:
    if os.path.getsize(path_a) != os.path.getsize(path_b):
        return False
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        return fa.read() == fb.read()


def _archive_before_overwrite(out_root: str, revision_id: str, dest_path: str) -> None:
    """Copia (nunca move) o conteúdo prestes a ser sobrescrito para
    `.history/<revision_id>/<relpath>`. É chamado ANTES do `os.replace` que
    vai destruir esse byte — depois disso não existe mais nenhum lugar de
    onde recuperá-lo (nome de arquivo estável => próxima revisão reusa o
    mesmo caminho)."""
    rel = os.path.relpath(dest_path, out_root)
    hist_path = os.path.join(_history_dir(out_root, revision_id), rel)
    os.makedirs(os.path.dirname(hist_path), exist_ok=True)
    shutil.copyfile(dest_path, hist_path)


def _compute_doc_hash(md_bytes: bytes, docx_bytes: bytes) -> str:
    """Hash canônico de um par (md, docx) — o mesmo valor gravado em
    `ManifestEntry.content_hash` (`_render_all`, abaixo) e o mesmo usado por
    `rollback` para verificar se um arquivo ATIVO sem cópia em `.history/`
    ainda é seguro de manter (`keep`) em vez de restaurar."""
    return _canonical_hash(
        {
            "md_sha256": hashlib.sha256(md_bytes).hexdigest(),
            "docx_sha256": hashlib.sha256(docx_bytes).hexdigest(),
        }
    )


def _promote(staging_root: str, out_root: str, previous_manifest: Optional[Manifest]) -> None:
    """Promove `staging_root` inteiro para dentro de `out_root`, arquivo a
    arquivo, via `os.replace` — cada arquivo aparece completo no destino ou
    não aparece, nunca truncado a meio caminho. Chamado só depois que TODA a
    renderização e TODA a validação já disseram `ok` (nunca antes)."""
    previous_revision_id = previous_manifest.revision_id if previous_manifest else None
    for dirpath, _dirnames, filenames in os.walk(staging_root):
        if not filenames:
            continue
        rel_dir = os.path.relpath(dirpath, staging_root)
        dest_dir = out_root if rel_dir == "." else os.path.join(out_root, rel_dir)
        os.makedirs(dest_dir, exist_ok=True)
        for fn in filenames:
            staged_path = os.path.join(dirpath, fn)
            dest_path = os.path.join(dest_dir, fn)
            if previous_revision_id and os.path.isfile(dest_path):
                if not _same_bytes(staged_path, dest_path):
                    _archive_before_overwrite(out_root, previous_revision_id, dest_path)
            os.replace(staged_path, dest_path)


# ---------------------------------------------------------------------------
# Renderização + coleta do manifesto novo
# ---------------------------------------------------------------------------


def _previous_document_paths(previous_manifest: Optional[Manifest], unit_ids: Sequence[str]):
    """Nome estável (§10.6 item 5): se QUALQUER unidade do documento já
    existia no manifesto anterior, TODO o documento (todas as suas unidades,
    inclusive as novas) herda aquele mesmo par de caminhos."""
    if previous_manifest is None:
        return None, None
    for uid in unit_ids:
        entry = previous_manifest.documents.get(uid)
        if entry is not None:
            return entry.md_path, entry.docx_path
    return None, None


def _render_all(
    plan: PlanLike,
    staging_root: str,
    renderers: Any,
    previous_manifest: Optional[Manifest],
):
    """Renderiza markdown/ e word/ de TODAS as unidades publicáveis do plano
    dentro do staging. Retorna (entries, blockers) — `entries` só é
    confiável quando `blockers` está vazio (senão pode estar parcial: o
    chamador NUNCA promove um staging parcial, então isso é inofensivo)."""
    blockers: list = []

    try:
        md_manifest = dict(renderers.markdown.render_manifest(plan) or {})
    except Exception as exc:  # noqa: BLE001 - falha de um vizinho vira bloqueio, não crash
        return {}, [f"markdown.render_manifest falhou: {exc!r}"]
    try:
        word_manifest = dict(renderers.word.render_manifest_word(plan) or {})
    except Exception as exc:  # noqa: BLE001
        return {}, [f"word.render_manifest_word falhou: {exc!r}"]

    documents = list(_pick(plan, "documents", default=None) or [])
    entries: dict = {}

    for document in documents:
        doc_id = str(_pick(document, "document_id", "id", default="") or "")
        unit_ids = _unit_ids_of(document)
        if not unit_ids:
            continue

        # §10.5 — equivalência markdown/word e bloqueio (F08/D14) checados
        # ANTES de gastar renderização: uma unidade sem representação textual
        # equivalente bloqueia a REVISÃO INTEIRA (nunca publicação parcial).
        doc_blockers = []
        for uid in unit_ids:
            md_info = md_manifest.get(uid)
            word_info = word_manifest.get(uid)
            if md_info is None:
                doc_blockers.append(f"{uid}: ausente do manifesto markdown (equivalência §10.5)")
            if word_info is None:
                doc_blockers.append(f"{uid}: ausente do manifesto word (equivalência §10.5)")
            reasons = list(_pick(md_info, "blocked_reasons", default=[]) or []) + list(
                _pick(word_info, "blocked_reasons", default=[]) or []
            )
            if _pick(md_info, "blocked", default=False) or _pick(word_info, "blocked", default=False):
                doc_blockers.append(f"{uid}: unidade bloqueada ({'; '.join(reasons) or 'motivo não informado'})")
        if doc_blockers:
            blockers.extend(doc_blockers)
            continue

        try:
            md_content = renderers.markdown.render(document)
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"documento {doc_id}: markdown.render falhou: {exc!r}")
            continue
        try:
            docx_content = renderers.word.render(document)
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"documento {doc_id}: word.render falhou: {exc!r}")
            continue

        md_bytes = md_content.encode("utf-8") if isinstance(md_content, str) else bytes(md_content)
        docx_bytes = bytes(docx_content)

        computed_md_name = _first_present(
            md_manifest, unit_ids, "md_filename", "markdown_filename", "filename", "path", "md_path"
        ) or f"{_slug(doc_id)}.md"
        computed_docx_name = _first_present(
            word_manifest, unit_ids, "docx_filename", "filename", "path", "docx_path"
        ) or f"{_slug(doc_id)}.docx"

        prev_md_rel, prev_docx_rel = _previous_document_paths(previous_manifest, unit_ids)
        md_relpath = prev_md_rel or os.path.join("markdown", computed_md_name)
        docx_relpath = prev_docx_rel or os.path.join("word", computed_docx_name)

        _write_staged(staging_root, md_relpath, md_bytes)
        _write_staged(staging_root, docx_relpath, docx_bytes)

        doc_hash = _compute_doc_hash(md_bytes, docx_bytes)

        for uid in unit_ids:
            fact_ids = list(
                _pick(word_manifest.get(uid), "fact_ids", default=None)
                or _pick(md_manifest.get(uid), "fact_ids", default=None)
                or []
            )
            entries[uid] = ManifestEntry(
                unit_id=uid,
                md_path=md_relpath,
                docx_path=docx_relpath,
                state=DocumentState.GENERATED,
                fact_ids=[str(f) for f in fact_ids],
                content_hash=doc_hash,
            )

    return entries, blockers


def _coerce_report(result: Any, name: str) -> Report:
    """Aceita `Report` deste módulo, `bool` puro, ou qualquer objeto/dict com
    `.ok` e uma lista de bloqueios sob QUALQUER um destes nomes: `bloqueios`
    (nome histórico deste módulo), `blockers`/`blocked_by` (sinônimos
    tolerados), ou `errors`/`warnings` — o formato REAL de
    `publishing.validate.Report` (`.ok`/`.errors`/`.warnings`; `.bloqueios`
    também existe lá como alias de `.errors`, mas não é exigido: `_pick`
    already acha `.errors` sozinho)."""
    if isinstance(result, Report):
        return result
    if isinstance(result, bool):
        return Report(ok=result, bloqueios=[] if result else [f"{name}: falhou (retorno bool)"])
    ok = bool(_pick(result, "ok", default=False))
    bloqueios = list(
        _pick(result, "bloqueios", "blockers", "blocked_by", "errors", default=[]) or []
    )
    if not ok and not bloqueios:
        bloqueios = [f"{name}: falhou sem detalhe de bloqueio"]
    return Report(ok=ok, bloqueios=bloqueios)


_DEFAULT_VALIDATORS: Any = None


def _default_validators() -> Any:
    """`validators` de fato usado por `publish_revision` quando o chamador
    não injeta nenhum: os validadores REAIS de `publishing.validate`, via
    `validate.StagedValidators` — mesmos nomes de método do Protocol
    `Validators` (`validate_equivalence`/`validate_semantics`/
    `validate_docx_structure`, `(plan, staging_root) -> Report`).

    Import PREGUIÇOSO (só quando esta função roda, nunca no import de
    `release`) e cacheado num global: quem sempre injeta seu próprio
    `validators` (como os testes de `test_release.py`) nunca paga esse
    import nem depende de `publishing.validate` existir."""
    global _DEFAULT_VALIDATORS
    if _DEFAULT_VALIDATORS is None:
        try:
            from publishing import validate as _validate
        except ImportError:  # pragma: no cover
            scripts_dir = str(Path(__file__).resolve().parent.parent)
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from publishing import validate as _validate
        _DEFAULT_VALIDATORS = _validate.StagedValidators
    return _DEFAULT_VALIDATORS


def _run_validators(validators: Any, plan: PlanLike, staging_root: str):
    reports = []
    for name in ("validate_equivalence", "validate_semantics", "validate_docx_structure"):
        fn = getattr(validators, name, None)
        if fn is None:
            reports.append(Report(ok=False, bloqueios=[f"validators.{name} ausente"]))
            continue
        try:
            result = fn(plan, staging_root)
        except Exception as exc:  # noqa: BLE001 - exceção do validador vira bloqueio
            reports.append(Report(ok=False, bloqueios=[f"{name} levantou exceção: {exc!r}"]))
            continue
        reports.append(_coerce_report(result, name))
    return reports


# ---------------------------------------------------------------------------
# API pública 1: publish_revision
# ---------------------------------------------------------------------------


def publish_revision(
    plan: PlanLike,
    out_root: str,
    renderers: Any,
    validators: Any = None,
    *,
    revision_id: Optional[str] = None,
    now: Optional[str] = None,
) -> ReleaseResult:
    """Prepara em staging, valida e só então promove — ou preserva a revisão
    anterior intacta e devolve os bloqueios (§10.6, F09).

    `renderers`/`validators`: objeto com os atributos do contrato do módulo,
    OU `dict` equivalente (normalizado via `_namespaced`). `validators`
    omitido (ou `None`) usa os validadores REAIS de `publishing.validate`
    (`_default_validators`) — nenhum chamador precisa mais escrever um
    adaptador de assinatura para publicar de verdade.
    """
    renderers = _namespaced(renderers)
    validators = _namespaced(validators) if validators is not None else _default_validators()

    out_root = os.path.abspath(out_root)
    os.makedirs(out_root, exist_ok=True)
    previous_manifest = _load_manifest(out_root)
    revision_id = revision_id or _new_revision_id()
    generated_at = now or _utc_now()

    staging_root = _staging_dir_for(out_root)
    try:
        entries, blockers = _render_all(plan, staging_root, renderers, previous_manifest)
        if blockers:
            return ReleaseResult(
                ok=False,
                revision_id=revision_id,
                out_root=out_root,
                manifest=previous_manifest,
                blocked_by=blockers,
            )
        if not entries:
            return ReleaseResult(
                ok=False,
                revision_id=revision_id,
                out_root=out_root,
                manifest=previous_manifest,
                blocked_by=["plano não produziu nenhum documento publicável"],
            )

        reports = _run_validators(validators, plan, staging_root)
        failing_blockers = [
            b for r in reports if not r.ok for b in (list(r.bloqueios) or ["validação falhou sem detalhe"])
        ]
        if failing_blockers:
            return ReleaseResult(
                ok=False,
                revision_id=revision_id,
                out_root=out_root,
                manifest=previous_manifest,
                blocked_by=failing_blockers,
            )

        # Tudo ok => promoção atômica (§10.6: só agora, nunca antes).
        for entry in entries.values():
            entry.state = DocumentState.VALIDATED

        _promote(staging_root, out_root, previous_manifest)

        for entry in entries.values():
            entry.state = DocumentState.PUBLISHED

        if previous_manifest is not None:
            # Preserva o manifesto que está prestes a deixar de ser o ativo —
            # é a única fonte que `prune`/`rollback` terão para reconstruir o
            # que essa revisão continha, já que `manifest.json` vai virar o
            # novo abaixo.
            _write_manifest_atomic(
                os.path.join(_history_dir(out_root, previous_manifest.revision_id), MANIFEST_FILENAME),
                previous_manifest,
            )

        manifest = Manifest(
            revision_id=revision_id,
            generated_at=generated_at,
            documents=entries,
            previous_revision=previous_manifest.revision_id if previous_manifest else None,
        )
        _write_manifest_atomic(os.path.join(out_root, MANIFEST_FILENAME), manifest)

        return ReleaseResult(ok=True, revision_id=revision_id, out_root=out_root, manifest=manifest)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# API pública 2: document_states
# ---------------------------------------------------------------------------


def document_states(release_result: ReleaseResult) -> dict:
    """{unit_id: state} do manifesto carregado por `release_result` (§11.2).

    Nunca inclui `retrievable` — esse estado não é decidido por este módulo.
    """
    if release_result.manifest is None:
        return {}
    return {uid: entry.state for uid, entry in release_result.manifest.documents.items()}


# ---------------------------------------------------------------------------
# API pública 3: prune
# ---------------------------------------------------------------------------


def prune(out_root: str, active_manifest: Manifest, *, removal_reasons: Optional[Mapping[str, str]] = None) -> PruneResult:
    """Remove SOMENTE arquivos órfãos: presentes no manifesto anterior e
    (a) cujo `unit_id` foi removido do plano ATIVO, com motivo em
    `removal_reasons[unit_id]`; ou (b) cujo `unit_id` continua ativo mas com
    caminho diferente do gravado antes (raro, dado o nome estável). Nunca
    apaga — move para `out_root/.history/<revisão_anterior>/<caminho>`.

    Recusa operar (`PruneError`) se `active_manifest` não bater com o
    `manifest.json` REALMENTE em disco — poda só roda depois de uma
    promoção validada, nunca sobre um manifesto hipotético/desatualizado.
    """
    out_root = os.path.abspath(out_root)
    on_disk = _load_manifest(out_root)
    if on_disk is None or on_disk.revision_id != active_manifest.revision_id:
        raise PruneError(
            "poda só pode rodar sobre o manifesto REALMENTE ativo em disco "
            f"(disco={getattr(on_disk, 'revision_id', None)!r}, recebido={active_manifest.revision_id!r})"
        )

    moved: list = []
    skipped: list = []
    previous_revision_id = active_manifest.previous_revision
    if not previous_revision_id:
        return PruneResult(ok=True, moved=moved, skipped=skipped)

    previous_manifest = _load_history_manifest(out_root, previous_revision_id)
    if previous_manifest is None:
        raise PruneError(
            f"manifesto histórico da revisão anterior ({previous_revision_id}) não encontrado em "
            f"{HISTORY_DIRNAME}/ — poda abortada para não decidir substituição às cegas"
        )

    active_paths = set()
    for e in active_manifest.documents.values():
        active_paths.add(e.md_path)
        active_paths.add(e.docx_path)

    removal_reasons = dict(removal_reasons or {})
    seen_paths = set()
    for uid, old_entry in previous_manifest.documents.items():
        for old_path in (old_entry.md_path, old_entry.docx_path):
            if old_path in seen_paths or old_path in active_paths:
                continue
            seen_paths.add(old_path)

            if uid in active_manifest.documents:
                # unidade continua no plano; caminho antigo ficou órfão só
                # porque o nome mudou (substituto inequívoco pela própria
                # identidade da unidade) — não exige motivo.
                pass
            else:
                reason = removal_reasons.get(uid)
                if reason is None:
                    skipped.append(
                        {"unit_id": uid, "path": old_path, "reason": "unidade removida sem motivo registrado"}
                    )
                    continue

            full_old = os.path.join(out_root, old_path)
            if not os.path.isfile(full_old):
                continue  # já não existe: poda idempotente
            hist_path = os.path.join(_history_dir(out_root, previous_revision_id), old_path)
            os.makedirs(os.path.dirname(hist_path), exist_ok=True)
            os.replace(full_old, hist_path)
            moved.append(old_path)

    return PruneResult(ok=True, moved=moved, skipped=skipped)


# ---------------------------------------------------------------------------
# API pública 4: rollback
# ---------------------------------------------------------------------------


def _rollback_archive_dir(out_root: str, from_label: str) -> str:
    """Bucket SEMPRE novo (uuid) fora do namespace usado por `_promote`/
    `prune` — evita colidir com o significado de `.history/<revision_id>/`
    (conteúdo substituído numa promoção normal) ao arquivar o que um
    `rollback` desloca."""
    base = os.path.join(out_root, HISTORY_DIRNAME, f"_rollback-from-{_slug(from_label)}-{uuid.uuid4().hex[:8]}")
    os.makedirs(base, exist_ok=True)
    return base


def rollback(out_root: str, to_revision: str) -> RollbackResult:
    """Restaura manifesto + arquivos da revisão `to_revision` a partir de
    `.history/<to_revision>/`. Falha (sem tocar em nada) se o histórico
    estiver incompleto — nunca aplica uma restauração parcial em silêncio.

    BUG REAL corrigido aqui: `_promote` só arquiva um arquivo em
    `.history/<revisão>/` quando o byte MUDA (`_same_bytes`, ao lado de
    `_promote`) — uma unidade que atravessa uma republicação sem mudar de
    conteúdo nunca aparece em `.history/<revisão-em-que-não-mudou>/`. Isso
    por si só é correto (não há para onde arquivar um byte que nunca foi
    sobrescrito); o bug era o que `rollback` CONCLUÍA daí: ausência em
    `.history/<to_revision>/` virava "o arquivo ATIVO já é o de
    `to_revision`" sem checagem nenhuma — mas o ativo pode já ser de uma
    revisão POSTERIOR (arquivada sob `.history/` de OUTRA revisão, a que
    finalmente mudou aquele arquivo). Corrigido: antes de aceitar "keep" de
    um par (md, docx) ausente de `.history/<to_revision>/`, comparamos o
    hash do par ATIVO — pela MESMA função (`_compute_doc_hash`) que gravou
    `ManifestEntry.content_hash` — contra o hash gravado no manifesto ALVO.
    Só bate => `keep` seguro. Diverge, ou o par está incompleto em
    `.history/` (só um dos dois arquivos presente) => histórico
    insuficiente para restaurar com integridade: vira bloqueio, `rollback`
    falha sem tocar em nada (menor correção correta: restaurar o byte EXATO
    da revisão alvo nesse caso exigiria reconstruir a cadeia completa de
    `.history/` entre as revisões, fora do escopo deste fix — ver relatório
    de entrega).
    """
    out_root = os.path.abspath(out_root)
    current = _load_manifest(out_root)

    if current is not None and current.revision_id == to_revision:
        return RollbackResult(ok=True, revision_id=to_revision, blocked_by=[], manifest=current)

    target = _load_history_manifest(out_root, to_revision)
    if target is None:
        return RollbackResult(
            ok=False,
            revision_id=to_revision,
            blocked_by=[f"revisão {to_revision} não encontrada em {HISTORY_DIRNAME}/"],
            manifest=current,
        )

    hist_dir = _history_dir(out_root, to_revision)
    blockers: list = []
    plan_ops = []  # ("restore"|"keep", relpath, source_abs)
    seen_pairs = set()
    for entry in target.documents.values():
        pair = (entry.md_path, entry.docx_path)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        hist_md = os.path.join(hist_dir, entry.md_path)
        hist_docx = os.path.join(hist_dir, entry.docx_path)
        live_md = os.path.join(out_root, entry.md_path)
        live_docx = os.path.join(out_root, entry.docx_path)
        md_in_hist = os.path.isfile(hist_md)
        docx_in_hist = os.path.isfile(hist_docx)

        if md_in_hist and docx_in_hist:
            plan_ops.append(("restore", entry.md_path, hist_md))
            plan_ops.append(("restore", entry.docx_path, hist_docx))
            continue

        if md_in_hist or docx_in_hist:
            # Par incompleto em .history/: restaurar só metade misturaria
            # bytes de duas revisões diferentes no mesmo documento.
            blockers.append(
                f"{entry.md_path} / {entry.docx_path}: histórico parcial em "
                f"{HISTORY_DIRNAME}/{to_revision} (só um dos dois arquivos do par está presente)"
            )
            continue

        if os.path.isfile(live_md) and os.path.isfile(live_docx):
            with open(live_md, "rb") as fh:
                live_md_bytes = fh.read()
            with open(live_docx, "rb") as fh:
                live_docx_bytes = fh.read()
            if _compute_doc_hash(live_md_bytes, live_docx_bytes) == entry.content_hash:
                plan_ops.append(("keep", entry.md_path, live_md))
                plan_ops.append(("keep", entry.docx_path, live_docx))
                continue
            blockers.append(
                f"{entry.md_path} / {entry.docx_path}: ausentes em {HISTORY_DIRNAME}/{to_revision} "
                f"e o conteúdo ATIVO diverge do hash gravado para {to_revision} — histórico "
                "incompleto, rollback abortado para não restaurar conteúdo errado"
            )
            continue

        blockers.append(
            f"{entry.md_path} / {entry.docx_path}: ausente(s) tanto em "
            f"{HISTORY_DIRNAME}/{to_revision} quanto no diretório ativo"
        )

    if blockers:
        return RollbackResult(ok=False, revision_id=to_revision, blocked_by=blockers, manifest=current)

    target_paths = {relpath for _kind, relpath, _src in plan_ops}
    current_paths = set()
    if current is not None:
        for e in current.documents.values():
            current_paths.add(e.md_path)
            current_paths.add(e.docx_path)
    displaced = current_paths - target_paths

    try:
        if displaced:
            bucket = _rollback_archive_dir(out_root, current.revision_id if current else "sem-revisao")
            for relpath in displaced:
                live_file = os.path.join(out_root, relpath)
                if os.path.isfile(live_file):
                    dest = os.path.join(bucket, relpath)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.replace(live_file, dest)

        for kind, relpath, source_abs in plan_ops:
            if kind == "keep":
                continue
            dest = os.path.join(out_root, relpath)
            dest_dir = os.path.dirname(dest)
            os.makedirs(dest_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".rollback-", suffix=".tmp")
            os.close(fd)
            try:
                shutil.copyfile(source_abs, tmp_path)
                os.replace(tmp_path, dest)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
                raise

        restored_manifest = dataclasses.replace(target, generated_at=_utc_now())
        _write_manifest_atomic(os.path.join(out_root, MANIFEST_FILENAME), restored_manifest)
    except OSError as exc:
        return RollbackResult(
            ok=False,
            revision_id=to_revision,
            blocked_by=[f"falha ao aplicar rollback: {exc!r}"],
            manifest=current,
        )

    return RollbackResult(ok=True, revision_id=to_revision, blocked_by=[], manifest=restored_manifest)
