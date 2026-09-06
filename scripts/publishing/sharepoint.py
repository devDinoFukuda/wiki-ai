"""publishing.sharepoint — perfil, estados e catálogo de agentes (§11.1/§11.2/§11.3, W7-T7.2).

Este módulo NUNCA fala com SharePoint, Copilot Studio ou qualquer rede: toda
função aqui é local — JSON em disco (perfil, rastreamento de publicação) e
cópia de arquivo local (`upload_package`, sempre para um diretório do próprio
sistema de arquivos, nunca para uma URL). Onde o plano pede "identificar
integração" ou "verificar disponibilidade no destino", este módulo modela o
DADO (perfil, evidência de verificação) e a REGRA (quem pode virar
`retrievable` e quando); a chamada de rede real, se algum dia existir, é
responsabilidade de outra camada que injeta a evidência aqui — nunca o
inverso.

Três entregas, na ordem do enunciado da tarefa:

1. **`PublicationProfile`** (§11.2) — o modo de integração (`url_integration`
   ou `file_sync`) é identificado UMA VEZ e persistido; `identify_profile`
   chamado de novo devolve o que já foi persistido, em vez de deixar duas
   partes do sistema divergirem sobre o modo em uso. `limits_json` nasce
   VAZIO — nenhuma quota de plataforma é um número fixo no motor (§11.2);
   `set_limit` é o único jeito de preencher, a partir da documentação
   oficial vigente, e fica registrado no próprio perfil persistido.

2. **Estados por documento** `{generated, validated, published, retrievable}`
   (§11.2). `generated`/`validated`/`published` já existem como contrato de
   dado em `publishing.release.DocumentState`; este módulo ACRESCENTA
   `retrievable` sem reabrir aquele módulo (é responsabilidade de outro
   agente) — `mark_published` promove um manifesto de release inteiro a
   `published` NA VISÃO DO SHAREPOINT (evento distinto do `published` local
   de `release.py`: aqui significa "enviado ao destino", lá significa
   "promovido a revisão ativa do disco"); `confirm_retrievable` é o ÚNICO
   caminho para `retrievable`, e exige evidência de verificação no destino.
   Sem integração, nada neste módulo promove nada sozinho: o estado fica
   honestamente em `published` e `report()` diz isso, nunca inventa.

3. **`agent_catalog`** (§11.1/§11.3) — uma descrição por agente (`ask`,
   `inception`, `historias`, `refinamento`), não por arquivo: configuração
   única reaproveitada pelos quatro. **`Handoff`** (§11.3) — a estrutura de
   transporte entre consumidores: sistema/iniciativa, pergunta, referências e
   estado da proposta, para não depender só de resumo narrativo do agente
   anterior.

Mais **`upload_package`** (§11.1): descreve — e, se pedido, MONTA — uma pasta
Word pronta para upload manual, organizada por grupo (sistema/iniciativa),
com lista de arquivos, hash e nota de substituição. Nunca faz upload real.

Fronteira de importação (ordem da tarefa): stdlib + `publishing.release`
(manifesto — único vizinho de `publishing` permitido). Nenhum import de
`publishing.planner`/`document`/`markdown`/`word`, nenhum import de `wk`,
`codescan` ou `sbindex`. Objetos desses módulos são aceitos por DUCK TYPING
(`_pick`, mesma convenção de `publishing/release.py`), nunca por tipo
importado.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .release import DocumentState, Manifest

PROFILE_FILENAME = "sharepoint_profile.json"
TRACKING_FILENAME = "sharepoint_publication.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------


class SharePointError(RuntimeError):
    """Base do domínio deste módulo (§11)."""


class UnknownMode(SharePointError):
    """`mode` fora de `{"url_integration", "file_sync"}` (§11.2)."""


class UnknownDocument(SharePointError):
    """`doc_id` não rastreado por nenhum `mark_published` anterior."""


class InvalidStateTransition(SharePointError):
    """Tentativa de pular etapa (ex.: `retrievable` sem `published`), ou
    `confirm_retrievable` sem evidência de verificação (§11.2)."""


# ---------------------------------------------------------------------------
# Utilidades pequenas (mesmo padrão de `publishing/release.py`, reimplementado
# aqui para não depender de nomes privados de outro módulo)
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: Any) -> str:
    folded = _WS_RE.sub(" ", str(text or "").strip()).lower()
    slug = _SLUG_RE.sub("-", folded).strip("-")
    return slug or "geral"


def _pick(obj: Any, *names: str, default: Any = None) -> Any:
    """Lê o primeiro atributo/chave não vazio dentre `names` — aceita dict,
    dataclass ou objeto arbitrário (mesma convenção de `release._pick`).
    Existe aqui como cópia deliberada: este módulo não importa símbolos
    privados (`_pick` de `release.py` não é parte do contrato público)."""
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


def _write_json_atomic(path: str, obj: Mapping[str, Any]) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sharepoint-", suffix=".tmp")
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


def _file_sha256(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. Perfil de publicação (§11.2)
# ---------------------------------------------------------------------------

#: Os dois modos de integração previstos — "não aplicar limites, sincronização
#: ou opções de um modo ao outro" (§11.2) começa por fechar o conjunto aqui.
MODE_URL_INTEGRATION = "url_integration"
MODE_FILE_SYNC = "file_sync"
ALLOWED_MODES: frozenset[str] = frozenset({MODE_URL_INTEGRATION, MODE_FILE_SYNC})

#: Chaves de quota RECONHECIDAS pela documentação da plataforma — SEM valor
#: aqui. Preencher é ato explícito (`set_limit`), nunca efeito colateral de
#: importar este módulo (§11.2: "não fixar números de quotas... no motor").
KNOWN_LIMIT_KEYS: tuple[str, ...] = (
    "max_file_size_bytes",
    "max_files_per_library",
    "max_sync_items",
    "url_integration_max_sources",
)


@dataclass(frozen=True)
class PublicationProfile:
    """Perfil de publicação SharePoint/Copilot Studio, identificado uma vez
    e persistido (§11.2). `limits_json` é o único lugar onde uma quota da
    plataforma pode aparecer — e só depois de alguém chamar `set_limit` com
    o valor tirado da documentação oficial vigente."""

    profile_id: str
    mode: str
    target_hint: str
    limits_json: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "mode": self.mode,
            "target_hint": self.target_hint,
            "limits_json": dict(self.limits_json),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> "PublicationProfile":
        mode = str(d["mode"])
        if mode not in ALLOWED_MODES:
            raise UnknownMode(f"perfil persistido com mode={mode!r} fora de {sorted(ALLOWED_MODES)}")
        return PublicationProfile(
            profile_id=str(d["profile_id"]),
            mode=mode,
            target_hint=str(d.get("target_hint", "")),
            limits_json=dict(d.get("limits_json", {})),
            created_at=str(d.get("created_at", "")),
        )


def _profile_path(store_dir: str) -> str:
    return os.path.join(store_dir, PROFILE_FILENAME)


def load_profile(store_dir: str) -> Optional[PublicationProfile]:
    """Perfil persistido em `store_dir`, ou `None` se ainda não identificado."""
    path = _profile_path(store_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return PublicationProfile.from_json(raw)


def save_profile(store_dir: str, profile: PublicationProfile) -> PublicationProfile:
    """Grava (ou regrava) o perfil explicitamente — usado por `identify_profile`
    na primeira identificação e por `set_limit` para persistir uma quota
    preenchida a partir da documentação oficial."""
    _write_json_atomic(_profile_path(store_dir), profile.to_json())
    return profile


def identify_profile(
    store_dir: str,
    mode: str,
    target_hint: str,
    *,
    limits: Optional[Mapping[str, Any]] = None,
    now: Optional[str] = None,
) -> PublicationProfile:
    """Identifica o modo de integração (§11.2) e persiste — UMA VEZ.

    Chamada seguinte com `store_dir` já identificado devolve o perfil
    PERSISTIDO, ignorando os argumentos novos: é isso que impede duas partes
    do sistema decidirem, em momentos diferentes, dois modos diferentes para
    o mesmo destino. Para mudar de modo deliberadamente, chame `save_profile`
    com um `PublicationProfile` novo (ato explícito, não efeito colateral).
    """
    if mode not in ALLOWED_MODES:
        raise UnknownMode(
            f"mode={mode!r} não é um dos modos previstos por §11.2: {sorted(ALLOWED_MODES)}"
        )
    existing = load_profile(store_dir)
    if existing is not None:
        return existing
    profile = PublicationProfile(
        profile_id="spp_" + uuid.uuid4().hex,
        mode=mode,
        target_hint=target_hint,
        limits_json=dict(limits or {}),
        created_at=now or _utc_now(),
    )
    return save_profile(store_dir, profile)


def set_limit(store_dir: str, profile: PublicationProfile, key: str, value: Any) -> PublicationProfile:
    """Preenche UMA quota a partir da documentação oficial e persiste (§11.2).

    Não valida `key` contra `KNOWN_LIMIT_KEYS` de forma bloqueante — a lista é
    um guia, não um teto: a plataforma pode documentar uma quota nova antes
    deste módulo ser atualizado, e isso não pode impedir o registro.
    """
    updated = replace(profile, limits_json={**dict(profile.limits_json), key: value})
    return save_profile(store_dir, updated)


# ---------------------------------------------------------------------------
# 2. Estados por documento — generated/validated/published/retrievable (§11.2)
# ---------------------------------------------------------------------------

#: `retrievable` é o quarto estado (§11.2), deliberadamente FORA de
#: `release.DocumentState` (módulo de outro agente): nasce aqui porque só
#: aqui existe a noção de "verificado no destino".
STATE_RETRIEVABLE = "retrievable"
ALL_STATES: frozenset[str] = frozenset(DocumentState.ALL) | {STATE_RETRIEVABLE}


@dataclass(frozen=True)
class DocumentPublicationState:
    """Estado de UM documento/unidade na publicação SharePoint — distinto do
    `state` de `release.ManifestEntry` (aquele é sobre o disco local; este é
    sobre o destino externo)."""

    doc_id: str
    state: str
    profile_id: Optional[str] = None
    published_at: Optional[str] = None
    verification: Optional[Mapping[str, Any]] = None

    def to_json(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "state": self.state,
            "profile_id": self.profile_id,
            "published_at": self.published_at,
            "verification": dict(self.verification) if self.verification else None,
        }

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> "DocumentPublicationState":
        state = str(d["state"])
        if state not in ALL_STATES:
            raise InvalidStateTransition(f"estado desconhecido persistido: {state!r}")
        verification = d.get("verification")
        return DocumentPublicationState(
            doc_id=str(d["doc_id"]),
            state=state,
            profile_id=d.get("profile_id"),
            published_at=d.get("published_at"),
            verification=dict(verification) if verification else None,
        )


@dataclass(frozen=True)
class PublicationTracking:
    """Rastreamento de publicação SharePoint por `doc_id` (§11.2), imutável —
    toda transição devolve uma instância NOVA (`mark_published`,
    `confirm_retrievable`), nunca muta a anterior em memória."""

    documents: Mapping[str, DocumentPublicationState] = field(default_factory=dict)

    def get(self, doc_id: str) -> Optional[DocumentPublicationState]:
        return self.documents.get(doc_id)

    def to_json(self) -> dict[str, Any]:
        return {"documents": {k: v.to_json() for k, v in self.documents.items()}}

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> "PublicationTracking":
        docs = {
            k: DocumentPublicationState.from_json(v)
            for k, v in dict(d.get("documents", {})).items()
        }
        return PublicationTracking(documents=docs)


def _tracking_path(store_dir: str) -> str:
    return os.path.join(store_dir, TRACKING_FILENAME)


def load_tracking(store_dir: str) -> PublicationTracking:
    """Rastreamento persistido, ou vazio se nada foi publicado ainda."""
    path = _tracking_path(store_dir)
    if not os.path.isfile(path):
        return PublicationTracking()
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return PublicationTracking.from_json(raw)


def save_tracking(store_dir: str, tracking: PublicationTracking) -> PublicationTracking:
    _write_json_atomic(_tracking_path(store_dir), tracking.to_json())
    return tracking


def mark_published(
    manifest: Manifest,
    profile: PublicationProfile,
    *,
    tracking: Optional[PublicationTracking] = None,
    now: Optional[str] = None,
) -> PublicationTracking:
    """A partir de um `release.Manifest` (ativo) e do perfil identificado,
    marca cada unidade do manifesto como `published` (§11.2) — e SÓ isso.

    Nunca atribui `retrievable`: uma republicação sempre reabre a exigência
    de verificação (qualquer confirmação anterior é descartada aqui —
    conteúdo pode ter mudado sob o mesmo `doc_id`), e `confirm_retrievable`
    é o único caminho para reconquistá-la, com evidência nova.
    """
    now = now or _utc_now()
    docs = dict(tracking.documents) if tracking is not None else {}
    for doc_id in sorted(manifest.documents.keys()):
        docs[doc_id] = DocumentPublicationState(
            doc_id=doc_id,
            state=DocumentState.PUBLISHED,
            profile_id=profile.profile_id,
            published_at=now,
            verification=None,
        )
    return PublicationTracking(documents=docs)


def confirm_retrievable(
    tracking: PublicationTracking,
    doc_id: str,
    verification_evidence: Mapping[str, Any],
    *,
    now: Optional[str] = None,
) -> PublicationTracking:
    """ÚNICO caminho para `retrievable` (§11.2): exige `doc_id` já
    `published` e evidência de verificação NO DESTINO. Sem integração, esta
    função simplesmente não é chamada — o estado permanece `published` e
    `report()` diz isso explicitamente; nada aqui promove por omissão.
    """
    prev = tracking.get(doc_id)
    if prev is None:
        raise UnknownDocument(
            f"doc_id {doc_id!r} não foi marcado published por nenhuma publicação rastreada"
        )
    if prev.state != DocumentState.PUBLISHED:
        raise InvalidStateTransition(
            f"doc_id {doc_id!r} está em estado {prev.state!r}; só published pode virar "
            "retrievable (§11.2)"
        )
    if not verification_evidence:
        raise InvalidStateTransition(
            f"confirm_retrievable({doc_id!r}) sem verification_evidence: retrievable sem "
            "evidência de verificação no destino é exatamente o que §11.2 proíbe"
        )
    docs = dict(tracking.documents)
    docs[doc_id] = replace(
        prev,
        state=STATE_RETRIEVABLE,
        verification=dict(verification_evidence),
        published_at=now or prev.published_at,
    )
    return PublicationTracking(documents=docs)


def report(tracking: PublicationTracking) -> dict[str, Any]:
    """Relatório objetivo do que é EFETIVAMENTE verificável (§11.2 aceite
    W7: "upload ou geração não é reportado como recuperação confirmada sem
    evidência dessa etapa"). `published_not_verified` nunca é confundido com
    `retrievable_verified` na mesma lista.
    """
    published_not_verified: list[dict[str, Any]] = []
    retrievable_verified: list[dict[str, Any]] = []
    for doc_id in sorted(tracking.documents):
        st = tracking.documents[doc_id]
        if st.state == STATE_RETRIEVABLE:
            retrievable_verified.append({"doc_id": doc_id, "verification": dict(st.verification or {})})
        else:
            published_not_verified.append(
                {
                    "doc_id": doc_id,
                    "state": st.state,
                    "note": "enviado/gerado, mas recuperação no destino NÃO verificada",
                }
            )
    return {
        "published_not_verified": published_not_verified,
        "retrievable_verified": retrievable_verified,
        "total_tracked": len(tracking.documents),
    }


# ---------------------------------------------------------------------------
# 3. Catálogo de fontes por agente e handoff (§11.1/§11.3)
# ---------------------------------------------------------------------------

AGENT_ASK = "ask"
AGENT_INCEPTION = "inception"
AGENT_STORIES = "historias"
AGENT_REFINEMENT = "refinamento"

#: Finalidade de cada agente (§11.3) — texto fixo, reaproveitado
#: identicamente por todo `agent_catalog`: é a "configuração única, não
#: repetida por arquivo" que o §11.1 exige.
AGENT_PURPOSE: dict[str, str] = {
    AGENT_ASK: (
        "Responde perguntas objetivas com escopo e evidências; nunca transforma proposta "
        "em comportamento atual (§11.3)."
    ),
    AGENT_INCEPTION: (
        "Recebe capacidades atuais, restrições, integrações e decisões pertinentes para "
        "apoiar inception de nova iniciativa (§11.3)."
    ),
    AGENT_STORIES: (
        "Usa fatos atuais e decisões da iniciativa para propor mudanças e critérios de "
        "aceite de histórias (§11.3)."
    ),
    AGENT_REFINEMENT: (
        "Recebe regras, arquitetura, contratos, falhas e relações de origem para "
        "detalhamento técnico de refinamento (§11.3)."
    ),
}

AGENTS: tuple[str, ...] = (AGENT_ASK, AGENT_INCEPTION, AGENT_STORIES, AGENT_REFINEMENT)


@dataclass(frozen=True)
class AgentSource:
    """Uma entrada do catálogo (§11.1): descrição de sistema/domínio +
    finalidade do agente, apontando para a MESMA revisão publicada — nunca
    uma fonte por arquivo."""

    agent: str
    purpose: str
    domain_description: str
    revision_id: str
    document_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "purpose": self.purpose,
            "domain_description": self.domain_description,
            "revision_id": self.revision_id,
            "document_count": self.document_count,
        }


def agent_catalog(plan_or_manifest: Any) -> dict[str, AgentSource]:
    """Catálogo de fontes por agente (§11.1/§11.3).

    Aceita `publishing.planner.PublicationPlan` (`.namespace`, `.documents`,
    `.revision_id`), `publishing.release.Manifest` (`.revision_id`,
    `.documents` por `unit_id`), ou qualquer dict/objeto equivalente — leitura
    por nome de campo (`_pick`), NENHUM tipo de `publishing.planner`/
    `document` é importado por este módulo (fronteira da tarefa).

    Uma `AgentSource` por agente, com a MESMA descrição de domínio/revisão:
    é a "configuração única, não repetida por arquivo" exigida pelo §11.1 —
    nenhum campo aqui varia por documento/arquivo individual.
    """
    namespace = str(_pick(plan_or_manifest, "namespace", default="") or "")
    revision_id = str(_pick(plan_or_manifest, "revision_id", default="") or "")
    documents = _pick(plan_or_manifest, "documents", default=None)
    if isinstance(documents, Mapping):
        document_count = len(documents)
    elif documents is not None:
        document_count = len(list(documents))
    else:
        document_count = 0

    domain_description = (
        f"Conhecimento de {namespace or '(namespace não informado)'}, revisão "
        f"{revision_id or '(revisão não informada)'}: {document_count} documento(s) "
        "publicado(s) nesta fonte."
    )

    return {
        agent: AgentSource(
            agent=agent,
            purpose=AGENT_PURPOSE[agent],
            domain_description=domain_description,
            revision_id=revision_id,
            document_count=document_count,
        )
        for agent in AGENTS
    }


@dataclass(frozen=True)
class Handoff:
    """Estrutura de transporte entre consumidores (§11.3): sistema/iniciativa,
    pergunta, referências e estado da proposta — não depende só de resumo
    narrativo do agente anterior."""

    system_or_initiative: str
    question: str
    references: tuple[str, ...]
    proposal_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_or_initiative": self.system_or_initiative,
            "question": self.question,
            "references": list(self.references),
            "proposal_state": self.proposal_state,
        }


def make_handoff(
    system_or_initiative: str,
    question: str,
    references: Sequence[str],
    proposal_state: str,
) -> Handoff:
    """Monta um `Handoff` válido; rejeita transporte sem sistema/iniciativa
    ou sem pergunta — os dois campos que, ausentes, forçariam o próximo
    agente a reconstruir contexto por resumo narrativo (§11.3)."""
    if not (system_or_initiative or "").strip():
        raise SharePointError("handoff exige sistema/iniciativa identificado (§11.3)")
    if not (question or "").strip():
        raise SharePointError("handoff exige a pergunta transportada (§11.3)")
    return Handoff(
        system_or_initiative=system_or_initiative,
        question=question,
        references=tuple(references),
        proposal_state=proposal_state,
    )


# ---------------------------------------------------------------------------
# 4. Pasta pronta para upload manual (§11.1) — NUNCA upload real
# ---------------------------------------------------------------------------


def upload_package(
    out_root: str,
    manifest: Manifest,
    *,
    grouping: Optional[Mapping[str, str]] = None,
    previous_manifest: Optional[Manifest] = None,
    target_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Descreve — e, se `target_dir` for informado, MONTA — uma pasta Word
    pronta para upload manual (§11.1). NUNCA faz upload real: sem
    `target_dir` é só o relatório (dry-run); com `target_dir`, copia
    arquivos locais de `out_root` para pastas locais, nada mais.

    `grouping`: `unit_id -> rótulo de sistema/iniciativa`. Sem mapeamento,
    tudo cai no grupo `"_geral"` — este módulo não inventa a associação
    sistema/iniciativa de uma unidade que não a informou.

    `previous_manifest`: quando informado, cada arquivo é anotado como
    "substitui versão anterior" (mesmo caminho, hash de conteúdo diferente),
    "sem mudança de conteúdo" (mesmo caminho e hash) ou "novo arquivo"
    (caminho não existia antes) — nunca um chute sobre o destino real, só a
    comparação entre as duas revisões locais.
    """
    grouping = dict(grouping or {})
    files_by_path: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[str]] = {}

    for unit_id in sorted(manifest.documents):
        entry = manifest.documents[unit_id]
        group_label = grouping.get(unit_id, "_geral")
        if entry.docx_path not in files_by_path:
            abs_path = os.path.join(out_root, entry.docx_path)
            files_by_path[entry.docx_path] = {
                "docx_path": entry.docx_path,
                "content_hash": entry.content_hash,
                "sha256_on_disk": _file_sha256(abs_path),
                "exists": os.path.isfile(abs_path),
                "unit_ids": [],
                "substitution": _substitution_note(entry, previous_manifest),
            }
        files_by_path[entry.docx_path]["unit_ids"].append(unit_id)
        groups.setdefault(group_label, [])
        if entry.docx_path not in groups[group_label]:
            groups[group_label].append(entry.docx_path)

    files = sorted(files_by_path.values(), key=lambda f: f["docx_path"])
    missing = [f["docx_path"] for f in files if not f["exists"]]

    result: dict[str, Any] = {
        "out_root": os.path.abspath(out_root),
        "revision_id": manifest.revision_id,
        "groups": {label: sorted(paths) for label, paths in sorted(groups.items())},
        "files": files,
        "missing_files": missing,
        "target_dir": None,
        "copied": [],
    }

    if target_dir:
        target_dir_abs = os.path.abspath(target_dir)
        copied: list[str] = []
        for group_label, paths in groups.items():
            group_dir = os.path.join(target_dir_abs, _slug(group_label))
            os.makedirs(group_dir, exist_ok=True)
            for docx_path in paths:
                info = files_by_path[docx_path]
                if not info["exists"]:
                    continue
                src = os.path.join(out_root, docx_path)
                dest = os.path.join(group_dir, os.path.basename(docx_path))
                shutil.copyfile(src, dest)
                copied.append(dest)
        result["target_dir"] = target_dir_abs
        result["copied"] = copied

    return result


def _substitution_note(entry: Any, previous_manifest: Optional[Manifest]) -> str:
    if previous_manifest is None:
        return "não avaliado (sem manifesto anterior informado)"
    for prev_entry in previous_manifest.documents.values():
        if prev_entry.docx_path == entry.docx_path:
            if prev_entry.content_hash == entry.content_hash:
                return "sem mudança de conteúdo"
            return "substitui versão anterior (mesmo caminho, conteúdo diferente)"
    return "novo arquivo"


__all__ = [
    "AGENT_ASK",
    "AGENT_INCEPTION",
    "AGENT_PURPOSE",
    "AGENT_REFINEMENT",
    "AGENT_STORIES",
    "AGENTS",
    "ALLOWED_MODES",
    "ALL_STATES",
    "AgentSource",
    "DocumentPublicationState",
    "Handoff",
    "InvalidStateTransition",
    "KNOWN_LIMIT_KEYS",
    "MODE_FILE_SYNC",
    "MODE_URL_INTEGRATION",
    "PublicationProfile",
    "PublicationTracking",
    "STATE_RETRIEVABLE",
    "SharePointError",
    "UnknownDocument",
    "UnknownMode",
    "agent_catalog",
    "confirm_retrievable",
    "identify_profile",
    "load_profile",
    "load_tracking",
    "make_handoff",
    "mark_published",
    "report",
    "save_profile",
    "save_tracking",
    "set_limit",
    "upload_package",
]
