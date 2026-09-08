"""publishing.delivery — pacotes de entrega locais imutáveis (§9.4/§11).

Nunca faz upload, nunca fala com SharePoint/rede: `prepare` copia Markdown e
Word já publicados (`publishing.release`) para um diretório novo e imutável
com `delivery.json`; `confirm` só REGISTRA que um humano disponibilizou e
verificou o conjunto no destino real — declaração do operador, não prova.

Separação de identidade (§9.4): `publication_revision` (gerada localmente por
`publishing.release`) nunca é confundida com `delivery_id` (este módulo).

Seleção (§11 "Seleção de entrega"):

    sem filtros            -> todo o store
    só --repo              -> conhecimento do sistema + fontes DIRETAMENTE
                               relacionadas (§11: a palavra é literal — só o
                               que as próprias unidades citam, não a
                               transitiva completa do grafo)
    só --initiative        -> artefatos da iniciativa + dependências
                               necessárias (mesmo salto direto — o que o
                               leitor encontra citado dentro do conteúdo
                               entregue, que é exatamente o que teria de
                               resolver para entender)
    ambos                  -> a iniciativa no contexto do sistema indicado

`_select` reconstrói `publishing.planner.plan()` sobre a MESMA revisão de
conhecimento do manifesto ativo (determinístico — duas execuções sobre a
mesma revisão produzem o mesmo plano, docstring de `planner.plan`) e filtra
`KnowledgeDocument`s por `namespace` (convenção `code/<repo>`, a MESMA usada
por `wk.cli._repo_key`/`wk analyze`, replicada aqui como uma linha porque
`publishing/` não importa `wk`) e por `SemanticUnit.belonging.initiative_id`.
O fechamento de 1 salto usa `SemanticUnit.relations` (`RelationRef`) — são
exatamente os ponteiros já renderizados no corpo de cada unidade publicada
(§10.3 item 7): entidade referenciada com documento próprio no plano entra na
entrega; sem documento próprio, vira `external_entities` declarada, nunca
incluída em silêncio.

Elegibilidade (§9.4): reaproveita `publishing.validate.validate_sufficiency`
sobre o SUBCONJUNTO selecionado — nenhuma regra de suficiência é duplicada
aqui. Pendência de documento fora da seleção nunca bloqueia (não entra no
subconjunto); pendência de uma dependência necessária (puxada pelo
fechamento) SEMPRE entra no subconjunto, então nunca é omitida pelo filtro.
Documento parcial/estrutural com lacuna DECLARADA não é erro de
`validate_sufficiency` — a entrega segue elegível, só que como conhecimento
`partial`, nunca `complete`; nenhum parâmetro deste módulo aceita "aceite
humano" para tornar `report.errors` elegível (§9.4 último parágrafo).

Manifesto `delivery.json` (§9.4): revisão de publicação, documentos únicos,
hashes, escopos, limitações, `added`/`changed`/`removed` contra a ÚLTIMA
entrega CONFIRMADA para o MESMO destino e MESMA seleção — nunca contra o
disco solto. Documento ainda referenciado pela entrega confirmada de OUTRA
seleção do mesmo destino nunca entra em `removed` (`_still_referenced`).

Store `.delivery/deliveries.json`: `schema_version` explícito; versão
incompatível é recusada ANTES de qualquer leitura/escrita de dado (`_load`).
Falha de preparação (`DeliveryError`, ou qualquer exceção durante a cópia)
nunca escreve em `deliveries.json` nem altera `confirmed` — só depois que
`delivery.json` está gravado em `out_dir` (via staging + `os.replace`,
mesma técnica de `publishing.release`) é que o índice local é atualizado.

Confirmação (§9.4 últimos parágrafos): idempotente para a entrega já
vigente; confirmar uma entrega ANTERIOR (após restauração manual) registra
nova ocorrência em `confirmations[key]` (histórico append-only) e passa a
ser a base da entrega seguinte, sem apagar as ocorrências mais recentes.
`remote_verification` é sempre `"declared_by_operator"` — nunca prova
automática de indexação remota.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from knowledge import identity
from knowledge.models import EntityType, EpistemicStatus, RelationType
from knowledge.repository import Repository

from . import validate as _validate
from .document import PublishingError
from .planner import plan as _plan_revision
from .release import MANIFEST_FILENAME, Manifest

#: Versão do schema de `.delivery/deliveries.json`. Mudança incompatível de
#: formato exige um store novo — `_load` recusa ANTES de tocar no arquivo.
SCHEMA_VERSION = 1

#: `knowledge.db` mora sempre em `<store_root>/knowledge.db` — mesma
#: convenção de `wk.cli._knowledge_db_path`, replicada aqui (uma constante)
#: pela mesma razão de fronteira de `_repo_namespace`.
_KNOWLEDGE_DB_FILENAME = "knowledge.db"

#: Fechamento de dependências: 1 salto a partir dos documentos da seleção
#: (ver docstring do módulo — "diretamente relacionadas" é a própria regra
#: de parada, aplicada de forma uniforme às três formas de seleção).
_CLOSURE_HOPS = 1


class DeliveryError(RuntimeError):
    """Erro do domínio de entrega: seleção, elegibilidade, manifesto ou confirmação."""


# ---------------------------------------------------------------------------
# infraestrutura local (tempo, hash, store `.delivery/`)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def _delivery_store_path(store_root: str) -> str:
    return os.path.join(os.path.abspath(store_root), ".delivery", "deliveries.json")


#: M2: arquivo de lock DEDICADO — nunca `deliveries.json` em si (travar o
#: próprio arquivo de dados encurtaria a janela onde `os.replace` promove o
#: arquivo novo; um arquivo à parte deixa a leitura-modificação-escrita
#: inteira, não só a escrita final, dentro da seção crítica).
_LOCK_FILENAME = "delivery.lock"
_LOCK_POLL_INTERVAL_S = 0.05
_LOCK_DEFAULT_TIMEOUT_S = 15.0

#: Camada extra IN-PROCESSO (`threading.Lock`): o lock de arquivo abaixo
#: (`msvcrt`/`fcntl`) é a garantia ENTRE processos; entre THREADS do mesmo
#: processo Python, alguns backends de lock de arquivo (particularmente
#: `msvcrt.locking` no Windows) não são reentrantes/robustos o bastante para
#: serializar handles distintos do mesmo processo de forma determinística —
#: este `Lock` fecha essa lacuna sem enfraquecer a garantia entre processos
#: (a ordem de aquisição é sempre a mesma: processo primeiro, arquivo depois).
_PROCESS_LOCK = threading.Lock()


def _delivery_lock_path(store_root: str) -> str:
    return os.path.join(os.path.abspath(store_root), ".delivery", _LOCK_FILENAME)


def _lock_file_handle(fh) -> None:
    """Trava `fh` (1 byte, posição 0) de forma exclusiva e NÃO-bloqueante —
    quem chama é responsável por retentar até o timeout. `OSError` (do
    `msvcrt`/`fcntl`) sinaliza "já travado por outro dono", nunca é
    silenciada aqui."""
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file_handle(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class _DeliveryTransaction:
    """M2: lock exclusivo ENTRE PROCESSOS sobre `.delivery/delivery.lock`
    (stdlib apenas — `msvcrt` no Windows, `fcntl` no POSIX), envolvendo a
    leitura-modificação-escrita de `deliveries.json` (`_load` -> mutação do
    chamador -> `_save`) como UMA transação. Sem isto, `prepare`/`confirm`
    concorrentes faziam um `_load` ler o estado, o outro processo terminar
    seu próprio `_load`-mutação-`_save` no meio, e o primeiro `_save`
    sobrescrever esse resultado inteiro — perdendo a entrega/confirmação do
    outro processo (`_save` é `os.replace` de um arquivo INTEIRO, não um
    merge). Esgotado o timeout sem conseguir o lock, levanta `DeliveryError`
    explicável — nunca trava a chamada indefinidamente."""

    def __init__(self, store_root: str, *, timeout: float = _LOCK_DEFAULT_TIMEOUT_S):
        self._path = _delivery_lock_path(store_root)
        self._timeout = timeout
        self._fh = None
        self._process_lock_held = False

    def __enter__(self) -> "_DeliveryTransaction":
        deadline = time.monotonic() + self._timeout
        # 1) camada in-processo — sempre adquirida primeiro, sempre liberada
        #    por último (ordem fixa evita deadlock entre as duas camadas).
        if not _PROCESS_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise DeliveryError(
                f"não foi possível obter o lock de entrega em {self._timeout:.1f}s "
                "(outra chamada deste mesmo processo está preparando/confirmando uma "
                "entrega); tente novamente"
            )
        self._process_lock_held = True

        # 2) camada entre processos — arquivo dedicado, criado sob demanda.
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            fh = open(self._path, "a+b")
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            while True:
                try:
                    _lock_file_handle(fh)
                    self._fh = fh
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        fh.close()
                        raise DeliveryError(
                            f"não foi possível obter o lock de entrega ({self._path}) em "
                            f"{self._timeout:.1f}s; outro processo pode estar preparando/"
                            "confirmando uma entrega — tente novamente"
                        )
                    time.sleep(_LOCK_POLL_INTERVAL_S)
        except BaseException:
            if self._process_lock_held:
                _PROCESS_LOCK.release()
                self._process_lock_held = False
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._fh is not None:
                _unlock_file_handle(self._fh)
                self._fh.close()
                self._fh = None
        finally:
            if self._process_lock_held:
                _PROCESS_LOCK.release()
                self._process_lock_held = False
        return False


def _load(store_root: str) -> dict:
    p = _delivery_store_path(store_root)
    if not os.path.exists(p):
        return {"schema_version": SCHEMA_VERSION, "deliveries": {}, "confirmed": {}, "confirmations": {}}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DeliveryError(
            f"store de entrega incompatível (schema_version={data.get('schema_version')!r}, "
            f"esperado {SCHEMA_VERSION}); use um store novo — nada foi lido nem alterado"
        )
    data.setdefault("deliveries", {})
    data.setdefault("confirmed", {})
    data.setdefault("confirmations", {})
    return data


def _save(store_root: str, data: dict) -> None:
    p = _delivery_store_path(store_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _repo_namespace(repo_abs: str) -> str:
    """`code/<repo>` — mesma regra de `wk.cli._repo_key`/`wk analyze`
    (cli.py:4846 e :6309: ``namespace = f"code/{_repo_key(repo_abs)}"``,
    ``_repo_key`` só troca ``\\`` por ``/``). Replicada aqui porque
    `publishing/` não importa `wk` (fronteira de módulo)."""
    return identity.normalize_namespace("code/" + repo_abs.replace("\\", "/"))


def _selection_key(destination: str, repo: str | None, initiative: str | None) -> str:
    repo_norm = os.path.normcase(os.path.abspath(repo)) if repo else ""
    return "|".join((destination, repo_norm, initiative or ""))


# ---------------------------------------------------------------------------
# manifesto de publicação (fonte: `publishing.release`)
# ---------------------------------------------------------------------------


def _read_manifest(publication_root: str) -> Manifest:
    try:
        with open(os.path.join(publication_root, MANIFEST_FILENAME), encoding="utf-8") as f:
            manifest = Manifest.from_json(json.load(f))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        raise DeliveryError(f"publicação local indisponível: {e}") from e
    if not manifest.documents:
        raise DeliveryError("publicação ativa não contém documentos entregáveis")
    return manifest


# ---------------------------------------------------------------------------
# seleção (§11 "Seleção de entrega")
# ---------------------------------------------------------------------------


def _select(plan_obj: Any, *, repo_namespace: str | None, initiative_id: str | None):
    """Filtra `plan_obj.documents` por (repo, iniciativa) e fecha 1 salto de
    dependência via `SemanticUnit.relations`. Devolve `(documentos, externas)`
    — `externas` é `{entity_id: {"title", "entity_type"}}` para entidades
    referenciadas sem documento próprio no plano (§11: "declara entidades
    externas não incluídas"). Levanta `DeliveryError` em seleção vazia."""
    docs = list(plan_obj.documents)
    if not docs:
        raise DeliveryError("publicação ativa não contém documentos entregáveis")

    if repo_namespace is None and initiative_id is None:
        seeds = docs
    else:
        seeds = []
        for d in docs:
            if repo_namespace is not None and d.namespace != repo_namespace:
                continue
            if initiative_id is not None:
                is_initiative_doc = d.anchor_entity_id == initiative_id
                touches_initiative = any(
                    u.belonging.initiative_id == initiative_id for u in d.units
                )
                if not (is_initiative_doc or touches_initiative):
                    continue
            seeds.append(d)

    if not seeds:
        criteria = []
        if repo_namespace is not None:
            criteria.append(f"--repo (namespace {repo_namespace!r})")
        if initiative_id is not None:
            criteria.append(f"--initiative {initiative_id!r}")
        raise DeliveryError(
            "seleção vazia: nenhum documento publicado corresponde a "
            + (" e ".join(criteria) if criteria else "nenhum filtro informado")
            + " (§11: seleção vazia é erro explicável)"
        )

    # 1º documento por entidade-âncora, na ordem já determinística do plano.
    by_anchor: dict[str, Any] = {}
    for d in docs:
        by_anchor.setdefault(d.anchor_entity_id, d)

    selected: dict[str, Any] = {d.document_id: d for d in seeds}
    external: dict[str, dict[str, str]] = {}
    for _ in range(_CLOSURE_HOPS):
        frontier = list(selected.values())
        for d in frontier:
            for u in d.units:
                for rel in u.relations:
                    oid = rel.other_entity_id
                    dep_doc = by_anchor.get(oid)
                    if dep_doc is not None:
                        selected.setdefault(dep_doc.document_id, dep_doc)
                    else:
                        external.setdefault(
                            oid, {"title": rel.other_title, "entity_type": rel.other_entity_type.value}
                        )

    ordered = sorted(selected.values(), key=lambda d: d.document_id)
    return ordered, external


# ---------------------------------------------------------------------------
# elegibilidade (§9.4) — reaproveita `publishing.validate.validate_sufficiency`
# ---------------------------------------------------------------------------


def _material_contradictions(selected_docs: Sequence[Any]) -> list[str]:
    """S9.4-02 (§9.4: "ausência de contradição material não resolvida"):
    varre `SemanticUnit.relations` do CONJUNTO selecionado por relações
    `RelationType.CONTRADICTS` cuja sustentação ainda não foi resolvida
    (`EpistemicStatus.UNRESOLVED`/`DISPUTED` — os mesmos dois estados que
    `publishing.document.fact_state` já trata como "unidade não resolvida"
    para fatos). Não duplica nenhuma regra de suficiência: usa só o que
    `RelationRef` (já construído por `publishing.document`/`planner` a
    partir de `knowledge.models.Relation`) carrega — nenhuma consulta nova a
    `knowledge.db`. Uma relação `CONTRADICTS` com sustentação
    `SUPPORTED`/`INFERRED` já foi resolvida (a favor de um dos dois lados) e
    não bloqueia."""
    found: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for d in selected_docs:
        for u in d.units:
            for rel in u.relations:
                if rel.relation_type is not RelationType.CONTRADICTS:
                    continue
                if rel.epistemic_status not in (EpistemicStatus.UNRESOLVED, EpistemicStatus.DISPUTED):
                    continue
                key = (u.entity_id, rel.other_entity_id, rel.relation_id)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    f"contradição material não resolvida ({rel.epistemic_status.value}): "
                    f"{u.title!r} x {rel.other_title!r} ({rel.relation_id})"
                )
    return found


def _eligibility(selected_docs: Sequence[Any]) -> dict:
    report = _validate.validate_sufficiency(list(selected_docs))
    completo = report.details.get("documents_completos", [])
    parcial = report.details.get("documents_parciais", [])
    estrutural = report.details.get("documents_estruturais", [])
    contradictions = _material_contradictions(selected_docs)

    # `knowledge_status` global só é rebaixado por documento cujo CONTRATO DE
    # LEITURA é comportamento (`KnowledgeDocument.requires_behavior()` —
    # reaproveitado, não duplicado: mesma regra de `BEHAVIOR_REQUIRED_KINDS`
    # que decide se uma unidade estrutural pode ser publicada). Documento de
    # visão de sistema/iniciativa é estrutural por natureza (identidade e
    # relações) e não deveria, sozinho, rebaixar uma seleção que inclui uma
    # capacidade `completa` para `partial` — só o que PROMETE comportamento
    # (capacidade/contrato/evolução, ou título que promete) entra nesta conta.
    required_ids = {d.document_id for d in selected_docs if d.requires_behavior()}
    if report.errors or contradictions:
        knowledge_status = "blocked"
    elif required_ids & (set(parcial) | set(estrutural)):
        knowledge_status = "partial"
    elif required_ids & set(completo):
        knowledge_status = "complete"
    else:
        knowledge_status = "not_applicable"
    return {
        # S9.4-02: contradição material não resolvida bloqueia elegibilidade
        # (§9.4) mesmo quando `validate_sufficiency` sozinho diria `ok` —
        # cobertura de evidência e ausência de contradição são dois
        # requisitos INDEPENDENTES da mesma frase da spec.
        "ok": report.ok and not contradictions,
        "errors": list(report.errors) + contradictions,
        "warnings": list(report.warnings),
        "knowledge_status": knowledge_status,
        "documents_completos": list(completo),
        "documents_parciais": list(parcial),
        "documents_estruturais": list(estrutural),
    }


def _select_and_assess(manifest: Manifest, store_root: str, repo: str | None, initiative: str | None) -> dict:
    # `repo` vazio (`""`) e `None` são tratados de forma UNIFORME como
    # "sem filtro de repo" — antes disto, `repo=""` era truthy o bastante
    # para `repo is not None` mas falsy o bastante para pular
    # `os.path.abspath`, e `os.path.isdir(None)` levantava `TypeError` em vez
    # de `DeliveryError` (defeito de auditoria).
    repo_abs = os.path.abspath(repo) if repo else None
    if repo_abs is not None and not os.path.isdir(repo_abs):
        raise DeliveryError(f"--repo não existe: {repo_abs}")
    repo_ns = _repo_namespace(repo_abs) if repo_abs else None

    kb_path = os.path.join(os.path.abspath(store_root), _KNOWLEDGE_DB_FILENAME)
    if not os.path.isfile(kb_path):
        raise DeliveryError("knowledge.db ausente no store; nada para selecionar")

    kb = Repository.open(kb_path)
    try:
        if initiative is not None:
            ent = kb.get_entity(initiative, lifecycle=None)
            if ent is None or ent.entity_type is not EntityType.INITIATIVE:
                raise DeliveryError(
                    f"--initiative {initiative!r} não corresponde a uma Initiative conhecida "
                    "em knowledge.db (§11: seleção ambígua é erro explicável)"
                )
        try:
            plan_obj = _plan_revision(kb, manifest.revision_id, namespace=None)
        except PublishingError as exc:
            raise DeliveryError(f"não foi possível reconstruir o plano de publicação: {exc}") from exc
        selected_docs, external = _select(plan_obj, repo_namespace=repo_ns, initiative_id=initiative)
        elig = _eligibility(selected_docs)
    finally:
        kb.close()

    units = sorted({u.unit_id for d in selected_docs for u in d.units})
    return {
        "publication_revision": manifest.revision_id,
        "documents": [d.document_id for d in selected_docs],
        "units": units,
        "external_entities": [
            {"entity_id": eid, **info} for eid, info in sorted(external.items())
        ],
        "knowledge_status": elig["knowledge_status"],
        "delivery_status": "ready" if elig["ok"] else "not_ready",
        "blocked_reasons": elig["errors"],
        "warnings": elig["warnings"],
    }


def eligibility(
    *, store_root: str, publication_root: str, repo: str | None = None, initiative: str | None = None
) -> dict:
    """Leitura pura (não escreve nada): seleção + elegibilidade ATUAIS para
    `(repo, initiative)`. Reaproveitada por `prepare` (abaixo) e pela CLI
    (`wk status --json`, campo `delivery_status`) — UMA função, não duas
    implementações divergentes (requisito da tarefa)."""
    manifest = _read_manifest(publication_root)
    return _select_and_assess(manifest, store_root, repo, initiative)


# ---------------------------------------------------------------------------
# `delivery prepare` / `delivery confirm`
# ---------------------------------------------------------------------------


def _still_referenced(state: dict, destination: str, own_key: str, path_key: tuple) -> bool:
    """`path_key` (md_path, docx_path) ainda está na entrega CONFIRMADA de
    outra seleção do MESMO destino? Se sim, não é `removed` desta entrega
    (§9.4: "documento ainda referenciado por outra seleção do mesmo destino
    não entra na lista de retirada")."""
    for key, rec in state.get("confirmed", {}).items():
        if key == own_key or rec.get("destination") != destination:
            continue
        for doc in rec.get("documents", []):
            if (doc.get("markdown"), doc.get("word")) == path_key:
                return True
    return False


def prepare(
    *,
    store_root: str,
    publication_root: str,
    destination: str,
    out_dir: str,
    repo: str | None = None,
    initiative: str | None = None,
) -> dict:
    """Cria `out_dir` (diretório NOVO, imutável) com `markdown/`, `word/` e
    `delivery.json`. Nada em `out_dir`/`.delivery/deliveries.json` é tocado
    se qualquer etapa falhar (staging + `os.replace`, mesma técnica de
    `publishing.release.publish_revision`)."""
    if not destination:
        raise DeliveryError("destination é obrigatório")
    out_dir = os.path.abspath(out_dir)
    if os.path.exists(out_dir):
        raise DeliveryError("diretório de entrega já existe; escolha um diretório novo")

    manifest = _read_manifest(publication_root)
    assessment = _select_and_assess(manifest, store_root, repo, initiative)
    if assessment["delivery_status"] != "ready":
        raise DeliveryError(
            "seleção não elegível para entrega: "
            + ("; ".join(assessment["blocked_reasons"]) if assessment["blocked_reasons"]
               else "cobertura requerida ausente (§9.4)")
        )
    selected_unit_ids = set(assessment["units"])

    # M2: `_load` (leitura de `state`, inclusive `previous`/`_still_referenced`
    # abaixo) até `_save` roda como UMA transação sob o lock entre processos
    # — sem isto, dois `prepare`/`confirm` concorrentes podiam ler o MESMO
    # `state` antigo, e o `_save` (que substitui `deliveries.json` inteiro)
    # do segundo a terminar apagava silenciosamente o que o primeiro gravou.
    with _DeliveryTransaction(store_root):
        state = _load(store_root)
        key = _selection_key(destination, repo, initiative)
        previous = state["confirmed"].get(key)
        did = "dlv_" + uuid.uuid4().hex
        staging = out_dir + ".tmp-" + uuid.uuid4().hex
        try:
            os.makedirs(os.path.join(staging, "markdown"))
            os.makedirs(os.path.join(staging, "word"))
            seen: dict = {}
            docs: list = []
            for unit_id, entry in manifest.documents.items():
                if unit_id not in selected_unit_ids:
                    continue
                k = (entry.md_path, entry.docx_path)
                if k in seen:
                    seen[k]["unit_ids"].append(unit_id)
                    continue
                record = {
                    "markdown": entry.md_path,
                    "word": entry.docx_path,
                    "unit_ids": [unit_id],
                    "content_hashes": [entry.content_hash],
                }
                for field, folder in (("markdown", "markdown"), ("word", "word")):
                    rel = record[field]
                    src = os.path.abspath(os.path.join(publication_root, rel))
                    if (
                        os.path.commonpath((os.path.abspath(publication_root), src)) != os.path.abspath(publication_root)
                        or not os.path.isfile(src)
                    ):
                        raise DeliveryError(f"artefato publicado ausente: {rel}")
                    name = os.path.basename(rel)
                    dst = os.path.join(staging, folder, name)
                    shutil.copyfile(src, dst)
                    record[field + "_file"] = {"path": name, "sha256": _hash(dst)}
                seen[k] = record
                docs.append(record)

            if not docs:
                raise DeliveryError(
                    "seleção elegível, mas sem artefato publicado correspondente no manifesto ativo "
                    "(conhecimento e publicação divergem); publique novamente antes de preparar a entrega"
                )

            old_paths = {(x["markdown"], x["word"]): x for x in (previous or {}).get("documents", [])}
            now_paths = {(x["markdown"], x["word"]): x for x in docs}
            removed = [
                list(pk) for pk in old_paths
                if pk not in now_paths and not _still_referenced(state, destination, key, pk)
            ]

            payload = {
                "schema_version": SCHEMA_VERSION,
                "delivery_id": did,
                "prepared_at": _now(),
                "publication_revision": manifest.revision_id,
                "destination": destination,
                "scope": {
                    "repo": os.path.abspath(repo) if repo else None,
                    "initiative": initiative,
                    "documents": assessment["documents"],
                    "units": assessment["units"],
                    "external_entities": assessment["external_entities"],
                },
                "knowledge_status": assessment["knowledge_status"],
                "previous_confirmed_delivery_id": (previous or {}).get("delivery_id"),
                "documents": docs,
                "changes": {
                    "added": [list(pk) for pk in now_paths if pk not in old_paths],
                    "changed": [
                        list(pk) for pk in now_paths
                        if pk in old_paths and now_paths[pk]["content_hashes"] != old_paths[pk]["content_hashes"]
                    ],
                    "removed": removed,
                },
                "limitations": [
                    "Entrega local: upload, acesso e recuperação no destino não foram verificados.",
                    *(
                        [
                            f"Conhecimento {assessment['knowledge_status']}: a seleção inclui documento "
                            "parcial/estrutural com lacuna declarada; não é entregue como conhecimento completo."
                        ]
                        if assessment["knowledge_status"] not in ("complete",)
                        else []
                    ),
                ],
            }
            with open(os.path.join(staging, "delivery.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(staging, out_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        state["deliveries"][did] = {**payload, "out_dir": out_dir, "confirmed_at": None, "verified_by": None}
        try:
            _save(store_root, state)
        except OSError as exc:
            # `out_dir` já foi promovido (`os.replace` acima), mas o índice local
            # não pôde ser atualizado: sem reverter, `out_dir` ficaria preso em
            # "diretório já existe" para sempre — repetir `prepare` com o mesmo
            # `--out` falharia na checagem do topo desta função sem chance de
            # recuperação. Reverte `out_dir` (o conteúdo publicado nele nunca foi
            # registrado como uma entrega válida) para que a repetição funcione.
            shutil.rmtree(out_dir, ignore_errors=True)
            raise DeliveryError(
                f"entrega preparada em disco, mas o índice local não pôde ser atualizado ({exc}); "
                f"{out_dir} foi revertido — corrija o acesso ao store e repita `delivery prepare` "
                "com o mesmo --out"
            ) from exc
        return payload


def confirm(*, store_root: str, delivery_id: str, destination: str, verified_by: str) -> dict:
    """Registra que o operador disponibilizou e verificou `delivery_id` no
    destino real. Idempotente para a entrega já vigente; confirmar uma
    entrega anterior (após restauração manual) vira nova ocorrência e passa
    a ser a base atual, sem apagar `confirmations[key]` mais recentes."""
    if not verified_by:
        raise DeliveryError("verified_by é obrigatório")
    # M2: mesma transação de `prepare` — `_load`, a decisão de
    # `already_current` (TOCTOU citado abaixo) e `_save` sob o MESMO lock.
    with _DeliveryTransaction(store_root):
        state = _load(store_root)
        d = state["deliveries"].get(delivery_id)
        if not d:
            raise DeliveryError("delivery_id desconhecido neste store")
        if d["destination"] != destination:
            raise DeliveryError("destination não corresponde à entrega preparada")

        confirmed_at = _now()
        d = dict(d)
        d["confirmed_at"] = confirmed_at
        d["verified_by"] = verified_by
        s = d["scope"]
        key = _selection_key(destination, s.get("repo"), s.get("initiative"))
        # Calculado ANTES de qualquer mutação de `state` — é o próprio `confirm`
        # quem decide "já era a entrega vigente?", nunca o chamador lendo o
        # estado numa chamada separada antes desta (isso era um TOCTOU: entre a
        # leitura do chamador e esta chamada, nada impedia outra confirmação de
        # mudar `state["confirmed"][key]` no meio do caminho — agora fechado
        # pelo lock entre processos, não só pela ordem de leitura/escrita).
        prior = state["confirmed"].get(key)
        already_current = bool(prior and prior.get("delivery_id") == delivery_id)

        record = {
            "delivery_id": delivery_id,
            "destination": destination,
            "confirmed_at": confirmed_at,
            "verified_by": verified_by,
            "publication_revision": d["publication_revision"],
        }
        state["deliveries"][delivery_id] = d
        state["confirmed"][key] = d
        state["confirmations"].setdefault(key, []).append(record)
        _save(store_root, state)

        return {
            "delivery_id": delivery_id,
            "destination": destination,
            "confirmed_at": confirmed_at,
            "verified_by": verified_by,
            "publication_revision": d["publication_revision"],
            "remote_verification": "declared_by_operator",
            "confirmations_for_selection": len(state["confirmations"][key]),
            "already_current": already_current,
        }


__all__ = [
    "DeliveryError",
    "SCHEMA_VERSION",
    "eligibility",
    "prepare",
    "confirm",
]
