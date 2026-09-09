"""Estado consolidado por objetivo e aplicação de resultados como DELTA (§7.1).

O que este módulo resolve
-------------------------
Antes dele o "progresso" de um objetivo era reconstruído a partir da ÚLTIMA
tarefa concluída: a rodada n+1 nascia do resultado da rodada n, e tudo o que a
rodada n-1 tinha fechado sumia se o worker da rodada n não repetisse a mesma
frase. O §7.1 exige o contrário: um estado AUTORITATIVO por
`(repo_id, objective_id, input_revision)`, sobre o qual cada resultado aceito é
aplicado como delta validado.

Invariante central (§7.1), em código e não em prosa::

    leituras_satisfeitas(n) ⊆ leituras_satisfeitas(n+1)

`apply_result` NUNCA remove uma leitura satisfeita. A única forma de reabrir é
`invalidate(state, reading_id, cause)` — explícita, com causa persistida — ou a
mudança da própria obrigação (o `obligation_hash` do need muda), que também
grava a causa (`obligation_changed`). Uma rodada n+1 que simplesmente não
menciona a leitura A não reabre A.

Pureza
------
`apply_result`, `invalidate` e `merge_needs` são FUNÇÕES PURAS: recebem estado,
devolvem estado novo. Persistência é responsabilidade de `StateStore`, que grava
uma linha por chave em `objective_state` (tabela de `runtime.db`). Isso é o que
permite testar a álgebra do estado sem banco e reexecutar a aplicação sem
duplicar efeito: `result_hash` já aplicado é no-op (`Progress.duplicate`).

O que conta como PROGRESSO (§7.3)
---------------------------------
Obrigação encerrada, evidência relevante NOVA aceita ou conflito resolvido.
Texto diferente, novo `task_id` ou arquivo novo não contam: um campo do
contrato que muda de redação sem trazer citação nova não produz progresso, e é
por isso que `Progress.has_progress` olha para as três listas nomeadas pelo §7.3
e não para a igualdade dos dicionários.

Só stdlib. Nenhum import de `wk`/`knowledge`/`analysis`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# Motivos de parada canônicos (§7.3, tabela de motivos)
# --------------------------------------------------------------------------

STOP_COMPLETED = "completed"
STOP_EXECUTOR_UNAVAILABLE = "executor_unavailable"
STOP_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_NO_PROGRESS = "no_progress"
STOP_AMBIGUITY = "ambiguity"
STOP_EVIDENCE_CHANGED = "evidence_changed"
STOP_INTERRUPTED = "interrupted"

#: Vocabulário FECHADO. Quem grava um motivo fora daqui recebe `ValueError`:
#: "parou por algum motivo" não é diagnóstico, e o §7.3 associa uma conduta
#: distinta a cada um destes sete.
STOP_REASONS = (
    STOP_COMPLETED,
    STOP_EXECUTOR_UNAVAILABLE,
    STOP_BUDGET_EXHAUSTED,
    STOP_NO_PROGRESS,
    STOP_AMBIGUITY,
    STOP_EVIDENCE_CHANGED,
    STOP_INTERRUPTED,
)

#: Causas de reabertura de leitura satisfeita. Reabrir sem causa é proibido.
CAUSE_EVIDENCE_INVALID = "evidence_invalid"
CAUSE_EVIDENCE_CHANGED = "evidence_changed"
CAUSE_OBLIGATION_CHANGED = "obligation_changed"
CAUSE_OPERATOR = "operator_decision"


class StateError(ValueError):
    """Delta recusado por violar o contrato do estado consolidado."""


# --------------------------------------------------------------------------
# Serialização determinística
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def evidence_key(ref: Any) -> str:
    """Identidade de uma citação: `caminho:linha_inicial-linha_final`.

    É por esta chave que "evidência NOVA" é decidida. Repetir a mesma citação
    numa rodada seguinte não é progresso — o §7.3 é explícito: repetir o mesmo
    pacote não avança nada.
    """
    if isinstance(ref, Mapping):
        path = _text(ref.get("path") or ref.get("caminho") or ref.get("file"))
        start = ref.get("line_start", ref.get("start_line", ref.get("start")))
        end = ref.get("line_end", ref.get("end_line", ref.get("end")))
        if path:
            return f"{path.replace(chr(92), '/').lower()}:{start}-{end}"
        return content_hash(dict(ref))
    text = _text(ref)
    return text.lower() if text else content_hash(ref)


def obligation_hash(need: Mapping[str, Any]) -> str:
    """Identidade SEMÂNTICA de uma obrigação: tipo + alvo + motivo.

    Não inclui `need_id`, nem o texto livre que o worker anexa: a obrigação
    "ler `foo.py:10-40` para decidir X" continua sendo a mesma obrigação quando
    o relatório a reescreve com outras palavras. É a mudança DESTE hash que
    autoriza reabrir uma leitura já satisfeita (causa `obligation_changed`).
    """
    return content_hash(
        {
            "kind": _text(need.get("kind") or need.get("tipo")),
            "target": _text(need.get("target") or need.get("alvo")),
            "motivo": _text(need.get("motivo") or need.get("reason")),
        }
    )


def need_id_of(need: Mapping[str, Any]) -> str:
    """Id estável de uma obrigação. Sem `need_id` declarado, usa alvo/hash."""
    declared = _text(need.get("need_id") or need.get("reading_id") or need.get("id"))
    if declared:
        return declared
    target = _text(need.get("target") or need.get("alvo"))
    if target:
        return f"need:{target}"
    return f"need:{obligation_hash(need)[:16]}"


def needs_package_hash(needs: Sequence[Mapping[str, Any]]) -> str:
    """Hash do PACOTE de necessidades a despachar.

    Dois pacotes com as mesmas obrigações (em qualquer ordem) têm o mesmo hash:
    é assim que `coordinator.plan_continuations` reconhece "este pacote já foi
    despachado" e recusa repetir, como o §7.3 exige.
    """
    return content_hash(sorted(obligation_hash(n) for n in needs if isinstance(n, Mapping)))


# --------------------------------------------------------------------------
# Progresso de uma rodada
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    """O que a rodada MOVEU — nunca o que ela escreveu (§7.3)."""

    obligations_closed: tuple[str, ...] = ()
    evidence_accepted: tuple[str, ...] = ()
    conflicts_resolved: tuple[str, ...] = ()
    #: Campos do contrato que ganharam conteúdo NESTA rodada — campo antes
    #: vazio que passou a ter conteúdo. É PROGRESSO (ver `has_progress`):
    #: reescrever um campo já preenchido continua não sendo, porque
    #: "mudança textual não representa progresso" — o que muda aqui é que
    #: PREENCHER um campo que estava vazio move o contrato do §6.3 e por isso
    #: não pode ser contado como rodada estéril.
    contract_fields_filled: tuple[str, ...] = ()
    readings_reopened: tuple[str, ...] = ()
    rejected: tuple[Mapping[str, Any], ...] = ()
    #: `True` quando o MESMO `result_hash` já havia sido aplicado: reexecutar
    #: a rodada depois de uma interrupção não duplica aceitação (§7.3).
    duplicate: bool = False
    detail: str = ""

    @property
    def has_progress(self) -> bool:
        """A rodada MOVEU alguma coisa?

        Quatro movimentos contam: obrigação encerrada, evidência nova aceita,
        conflito resolvido e campo do contrato que saiu de vazio para
        preenchido. O quarto entrou porque uma rodada que apurou `regra` pela
        primeira vez — sem fechar leitura e sem citação inédita — era
        classificada como `no_progress` e a cadeia parava em cima de trabalho
        real. Reescrita de campo JÁ preenchido continua fora (`apply_result`
        só registra o campo quando `depois and not antes`), então texto novo
        sobre conteúdo velho segue sem valer progresso.
        """
        return bool(
            self.obligations_closed
            or self.evidence_accepted
            or self.conflicts_resolved
            or self.contract_fields_filled
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligations_closed": list(self.obligations_closed),
            "evidence_accepted": list(self.evidence_accepted),
            "conflicts_resolved": list(self.conflicts_resolved),
            "contract_fields_filled": list(self.contract_fields_filled),
            "readings_reopened": list(self.readings_reopened),
            "rejected": [dict(r) for r in self.rejected],
            "duplicate": self.duplicate,
            "has_progress": self.has_progress,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Progress":
        raw = dict(data or {})
        return cls(
            obligations_closed=tuple(str(x) for x in raw.get("obligations_closed") or ()),
            evidence_accepted=tuple(str(x) for x in raw.get("evidence_accepted") or ()),
            conflicts_resolved=tuple(str(x) for x in raw.get("conflicts_resolved") or ()),
            contract_fields_filled=tuple(str(x) for x in raw.get("contract_fields_filled") or ()),
            readings_reopened=tuple(str(x) for x in raw.get("readings_reopened") or ()),
            rejected=tuple(dict(r) for r in raw.get("rejected") or () if isinstance(r, Mapping)),
            duplicate=bool(raw.get("duplicate", False)),
            detail=str(raw.get("detail") or ""),
        )


def merge_progress(items: Iterable[Progress]) -> Progress:
    """Progresso agregado de uma rodada com vários objetivos."""
    closed: list[str] = []
    evidence: list[str] = []
    conflicts: list[str] = []
    fields: list[str] = []
    reopened: list[str] = []
    rejected: list[Mapping[str, Any]] = []
    duplicate = True
    seen_any = False
    for item in items:
        seen_any = True
        closed.extend(item.obligations_closed)
        evidence.extend(item.evidence_accepted)
        conflicts.extend(item.conflicts_resolved)
        fields.extend(item.contract_fields_filled)
        reopened.extend(item.readings_reopened)
        rejected.extend(item.rejected)
        duplicate = duplicate and item.duplicate
    return Progress(
        obligations_closed=tuple(closed),
        evidence_accepted=tuple(evidence),
        conflicts_resolved=tuple(conflicts),
        contract_fields_filled=tuple(fields),
        readings_reopened=tuple(reopened),
        rejected=tuple(rejected),
        duplicate=bool(seen_any and duplicate),
    )


# --------------------------------------------------------------------------
# Estado consolidado
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsolidatedState:
    """Estado autoritativo de UM objetivo sobre UMA revisão de entrada (§7.1).

    Campos mínimos do §7.1, todos presentes e todos persistidos: contrato
    consolidado (campos COM conteúdo), obrigações abertas/concluídas, leituras
    satisfeitas (com evidência e rodada), evidências aceitas, resultados
    rejeitados (com motivo), conflitos, tentativas, orçamento (teto e
    consumo), motivo de parada e hash da entrada.
    """

    repo_id: str
    objective_id: str
    input_revision: str
    #: nome do campo -> {"content", "evidence_refs", "status", "round"}
    contract: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: need_id -> obrigação aberta (com `obligation_hash`)
    open_obligations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: need_id -> obrigação encerrada (com rodada e evidência que a fechou)
    closed_obligations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: reading_id -> {"evidence": [...], "round": n, "obligation_hash": ...}
    satisfied_readings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: chave de evidência -> citação aceita (com a rodada em que entrou)
    accepted_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    rejected_results: tuple[Mapping[str, Any], ...] = ()
    conflicts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: causa de cada reabertura, na ordem em que aconteceram (§7.1)
    invalidations: tuple[Mapping[str, Any], ...] = ()
    attempts: int = 0
    rounds_used: int = 0
    max_rounds: int = 0
    budget: Mapping[str, Any] = field(default_factory=dict)
    consumed: Mapping[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    input_hash: str = ""
    #: `result_hash` já aplicados — a retomada idempotente do §7.3.
    applied_results: tuple[str, ...] = ()
    #: hash de cada pacote de necessidades já despachado (§7.3: não repetir).
    dispatched_packages: tuple[str, ...] = ()
    last_progress: Mapping[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    # -- leitura -----------------------------------------------------------

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.repo_id, self.objective_id, self.input_revision)

    def satisfied_ids(self) -> frozenset[str]:
        return frozenset(self.satisfied_readings)

    def open_ids(self) -> frozenset[str]:
        return frozenset(self.open_obligations)

    def contract_content(self) -> dict[str, str]:
        """Contrato ACUMULADO em forma plana — é o que vai no pacote do worker.

        O §7.2 proíbe mandar "uma lista de IDs sem o conteúdo correspondente":
        este dicionário é campo -> texto já apurado, para que a rodada n+1
        receba o que a rodada n descobriu, e não só a notícia de que descobriu.
        """
        return {
            name: str((data or {}).get("content") or "")
            for name, data in self.contract.items()
            if str((data or {}).get("content") or "").strip()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "objective_id": self.objective_id,
            "input_revision": self.input_revision,
            "contract": {k: dict(v) for k, v in self.contract.items()},
            "open_obligations": {k: dict(v) for k, v in self.open_obligations.items()},
            "closed_obligations": {k: dict(v) for k, v in self.closed_obligations.items()},
            "satisfied_readings": {k: dict(v) for k, v in self.satisfied_readings.items()},
            "accepted_evidence": {k: dict(v) for k, v in self.accepted_evidence.items()},
            "rejected_results": [dict(r) for r in self.rejected_results],
            "conflicts": {k: dict(v) for k, v in self.conflicts.items()},
            "invalidations": [dict(i) for i in self.invalidations],
            "attempts": self.attempts,
            "rounds_used": self.rounds_used,
            "max_rounds": self.max_rounds,
            "budget": dict(self.budget),
            "consumed": dict(self.consumed),
            "stop_reason": self.stop_reason,
            "input_hash": self.input_hash,
            "applied_results": list(self.applied_results),
            "dispatched_packages": list(self.dispatched_packages),
            "last_progress": dict(self.last_progress),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConsolidatedState":
        raw = dict(data or {})

        def _mapmap(name: str) -> dict[str, dict[str, Any]]:
            value = raw.get(name) or {}
            if not isinstance(value, Mapping):
                return {}
            return {str(k): dict(v) for k, v in value.items() if isinstance(v, Mapping)}

        def _listmap(name: str) -> tuple[dict[str, Any], ...]:
            value = raw.get(name) or ()
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(dict(v) for v in value if isinstance(v, Mapping))

        return cls(
            repo_id=str(raw.get("repo_id") or ""),
            objective_id=str(raw.get("objective_id") or ""),
            input_revision=str(raw.get("input_revision") or ""),
            contract=_mapmap("contract"),
            open_obligations=_mapmap("open_obligations"),
            closed_obligations=_mapmap("closed_obligations"),
            satisfied_readings=_mapmap("satisfied_readings"),
            accepted_evidence=_mapmap("accepted_evidence"),
            rejected_results=_listmap("rejected_results"),
            conflicts=_mapmap("conflicts"),
            invalidations=_listmap("invalidations"),
            attempts=int(raw.get("attempts") or 0),
            rounds_used=int(raw.get("rounds_used") or 0),
            max_rounds=int(raw.get("max_rounds") or 0),
            budget=dict(raw.get("budget") or {}),
            consumed=dict(raw.get("consumed") or {}),
            stop_reason=str(raw.get("stop_reason") or ""),
            input_hash=str(raw.get("input_hash") or ""),
            applied_results=tuple(str(x) for x in raw.get("applied_results") or ()),
            dispatched_packages=tuple(str(x) for x in raw.get("dispatched_packages") or ()),
            last_progress=dict(raw.get("last_progress") or {}),
            updated_at=str(raw.get("updated_at") or ""),
        )


def new_state(
    repo_id: str,
    objective_id: str,
    input_revision: str,
    *,
    input_versions: Any = None,
    budget: Mapping[str, Any] | None = None,
    max_rounds: int = 0,
) -> ConsolidatedState:
    """Estado inicial VAZIO da chave `(repo_id, objective_id, input_revision)`."""
    return ConsolidatedState(
        repo_id=str(repo_id or ""),
        objective_id=str(objective_id or ""),
        input_revision=str(input_revision or ""),
        budget=dict(budget or {}),
        max_rounds=int(max_rounds or 0),
        input_hash=content_hash(input_versions) if input_versions is not None else str(input_revision or ""),
    )


# --------------------------------------------------------------------------
# Normalização do delta recebido
# --------------------------------------------------------------------------


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _result_payload(result: Any) -> dict[str, Any]:
    """Aceita `ResultEnvelope`, `IntegrationReport`-outcome ou dicionário cru.

    O estado consolidado não pode depender do FORMATO de quem produziu o
    delta: o coordenador entrega `envelopes.ResultEnvelope`, a integração
    entrega o outcome por objetivo do `knowledge.integrate` (com `leituras`,
    `contract_state` e `unmet_needs`). Os dois descrevem a mesma coisa.
    """
    if isinstance(result, Mapping):
        data = dict(result)
    else:
        data = {}
        for name in (
            "result_hash", "claims", "evidence_refs", "reading_satisfied",
            "remaining_needs", "diagnostics", "usage", "execution_status",
            "objective_id", "input_revision",
        ):
            if hasattr(result, name):
                data[name] = getattr(result, name)
        output = getattr(result, "output", None)
        if callable(output):
            try:
                data.setdefault("claims", output())
            except Exception:  # pragma: no cover - defensivo
                pass
    claims = data.get("claims")
    if isinstance(claims, Mapping):
        for key, value in claims.items():
            data.setdefault(key, value)
    return data


def _contract_of(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Campos do contrato do delta, em `{nome: {content, evidence_refs, status}}`."""
    raw = payload.get("contract") or payload.get("contract_state") or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return out
    for name, value in raw.items():
        if isinstance(value, Mapping):
            out[str(name)] = {
                "content": str(value.get("content") or value.get("conteudo") or ""),
                "evidence_refs": [
                    dict(e) for e in _as_sequence(value.get("evidence_refs")) if isinstance(e, Mapping)
                ],
                "status": str(value.get("status") or ""),
                "impacto": str(value.get("impacto") or ""),
            }
        elif isinstance(value, str):
            out[str(name)] = {"content": value, "evidence_refs": [], "status": "", "impacto": ""}
    return out


def _satisfied_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Leituras que o delta AFIRMA ter fechado, já com o veredito de quem apurou.

    `satisfeita` (vindo de `knowledge.integrate._satisfy_readings`) é o veredito
    depois de resolver a evidência no snapshot. Quando ele não vem — delta cru
    do worker — a leitura só fecha se houver alguma evidência citada: declarar
    "li" sem citação nunca fecha obrigação.
    """
    entries = _as_sequence(payload.get("leituras")) or _as_sequence(payload.get("reading_satisfied"))
    out: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            out.append({"need_id": entry.strip(), "satisfied": False, "evidence": [], "motivo": ""})
            continue
        if not isinstance(entry, Mapping):
            continue
        evidence = _as_sequence(entry.get("evidencia")) or _as_sequence(entry.get("evidence")) \
            or _as_sequence(entry.get("evidence_refs"))
        if "satisfeita" in entry or "satisfied" in entry:
            ok = bool(entry.get("satisfeita", entry.get("satisfied")))
        else:
            ok = bool(evidence)
        out.append(
            {
                "need_id": need_id_of(entry),
                "target": _text(entry.get("target") or entry.get("alvo")),
                "satisfied": ok,
                "evidence": evidence,
                "motivo": _text(entry.get("motivo") or entry.get("reason")),
            }
        )
    return out


def _needs_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    for name in ("unmet_needs", "remaining_needs", "reading_needs", "needs"):
        items = [n for n in _as_sequence(payload.get(name)) if isinstance(n, Mapping)]
        if items:
            return [dict(n) for n in items]
    return []


def _conflicts_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = _as_sequence(payload.get("conflicts")) or _as_sequence(payload.get("inconsistencias"))
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        data = dict(item)
        data["conflict_id"] = _text(
            data.get("conflict_id") or data.get("id") or content_hash(data)[:16]
        )
        data["resolved"] = bool(data.get("resolved", data.get("resolvido", False)))
        out.append(data)
    return out


def _evidence_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = [e for e in _as_sequence(payload.get("evidence_refs")) if isinstance(e, Mapping)]
    refs.extend(e for e in _as_sequence(payload.get("evidence")) if isinstance(e, Mapping))
    for value in _contract_of(payload).values():
        refs.extend(value.get("evidence_refs") or ())
    return refs


def _add_usage(consumed: Mapping[str, Any], usage: Any) -> dict[str, Any]:
    out = dict(consumed)
    out["calls"] = int(out.get("calls") or 0) + 1
    if isinstance(usage, Mapping):
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            out[str(key)] = (out.get(str(key)) or 0) + value
    return out


# --------------------------------------------------------------------------
# Delta: apply_result / invalidate
# --------------------------------------------------------------------------


def apply_result(
    state: ConsolidatedState,
    result: Any,
    *,
    round_no: int | None = None,
    now: str = "",
) -> tuple[ConsolidatedState, Progress]:
    """Aplica UM resultado aceito como delta. FUNÇÃO PURA (§7.1).

    Nunca reconstrói o estado a partir do resultado: o que chega é somado ao
    que já havia. Consequência direta e testável (T06): rodada A fecha o campo
    `A`, rodada B fecha o campo `B`, e o estado — logo o pacote da rodada
    seguinte — tem A **e** B.

    Recusa e rejeição também são delta: um resultado marcado `rejected` entra
    em `rejected_results` COM motivo, conta tentativa e não produz progresso.

    Reexecução idempotente: `result_hash` já presente em `applied_results`
    devolve o MESMO estado e `Progress(duplicate=True)`. É o que permite
    retomar uma cadeia interrompida sem aceitar o mesmo resultado duas vezes.
    """
    payload = _result_payload(result)
    rhash = _text(payload.get("result_hash")) or content_hash(
        {k: v for k, v in payload.items() if k not in {"result_hash", "usage"}}
    )
    if rhash in state.applied_results:
        return state, Progress(duplicate=True, detail=f"result_hash {rhash[:12]} já aplicado")

    round_no = int(round_no if round_no is not None else state.rounds_used)
    moment = now or state.updated_at

    contract = {k: dict(v) for k, v in state.contract.items()}
    open_obligations = {k: dict(v) for k, v in state.open_obligations.items()}
    closed = {k: dict(v) for k, v in state.closed_obligations.items()}
    satisfied = {k: dict(v) for k, v in state.satisfied_readings.items()}
    evidence_pool = {k: dict(v) for k, v in state.accepted_evidence.items()}
    conflicts = {k: dict(v) for k, v in state.conflicts.items()}
    rejected_results = list(state.rejected_results)
    invalidations = list(state.invalidations)

    closed_now: list[str] = []
    evidence_now: list[str] = []
    conflicts_now: list[str] = []
    fields_now: list[str] = []
    reopened_now: list[str] = []
    rejected_now: list[dict[str, Any]] = []

    # -- resultado recusado ------------------------------------------------
    rejection = payload.get("rejection") or payload.get("rejected")
    status = _text(payload.get("execution_status") or payload.get("state")).lower()
    if rejection or status in {"failed", "cancelled", "rejected"}:
        motivo = _text(
            payload.get("rejection_reason")
            or payload.get("motivo")
            or (rejection if isinstance(rejection, str) else "")
            or status
            or "recusado"
        )
        entry = {"result_hash": rhash, "motivo": motivo, "round": round_no, "at": moment}
        rejected_results.append(entry)
        rejected_now.append(entry)
        novo = replace(
            state,
            rejected_results=tuple(rejected_results),
            attempts=state.attempts + 1,
            consumed=_add_usage(state.consumed, payload.get("usage")),
            applied_results=tuple([*state.applied_results, rhash]),
            updated_at=moment or state.updated_at,
        )
        progresso = Progress(rejected=tuple(rejected_now), detail=f"resultado recusado: {motivo}")
        return replace(novo, last_progress=progresso.to_dict()), progresso

    # -- contrato: acumula, nunca substitui o que já tinha conteúdo --------
    for name, incoming in _contract_of(payload).items():
        atual = contract.get(name) or {}
        antes = str(atual.get("content") or "").strip()
        depois = str(incoming.get("content") or "").strip()
        refs = list(atual.get("evidence_refs") or ())
        conhecido = {evidence_key(r) for r in refs}
        for ref in incoming.get("evidence_refs") or ():
            if evidence_key(ref) not in conhecido:
                refs.append(dict(ref))
                conhecido.add(evidence_key(ref))
        # Conteúdo vazio NÃO apaga conteúdo apurado: uma rodada que não fala do
        # campo A não desfaz o que a rodada anterior apurou sobre A.
        conteudo = depois or antes
        contract[name] = {
            "content": conteudo,
            "evidence_refs": refs,
            "status": str(incoming.get("status") or atual.get("status") or ""),
            "impacto": str(incoming.get("impacto") or atual.get("impacto") or ""),
            "round": round_no if depois and depois != antes else atual.get("round", round_no),
        }
        if depois and not antes:
            fields_now.append(name)

    # -- evidência nova ----------------------------------------------------
    for ref in _evidence_of(payload):
        key = evidence_key(ref)
        if key in evidence_pool:
            continue
        evidence_pool[key] = {"ref": dict(ref), "round": round_no, "at": moment}
        evidence_now.append(key)

    # -- obrigações que o delta FECHOU ------------------------------------
    for entry in _satisfied_of(payload):
        need_id = entry["need_id"]
        if not entry["satisfied"]:
            continue
        if need_id in satisfied:
            continue  # já satisfeita: reafirmar não é progresso
        aberta = open_obligations.pop(need_id, None) or {}
        registro = {
            "need_id": need_id,
            "target": entry.get("target") or _text(aberta.get("target")),
            "evidence": [e if isinstance(e, str) else dict(e) for e in entry.get("evidence") or ()],
            "round": round_no,
            "at": moment,
            # SÓ o hash vindo da obrigação ABERTA correspondente. Derivá-lo da
            # entrada de `reading_satisfied` (que traz `need_id` + evidência,
            # sem `kind`/`motivo`) produzia um hash incomparável com o da
            # obrigação real: na rodada seguinte ele "mudava" e reabria uma
            # leitura que ninguém invalidou — violação direta da invariante do
            # §7.1. Vazio significa "forma da obrigação ainda desconhecida", e
            # o laço de necessidades abaixo a adota na primeira vez que a vê.
            "obligation_hash": str(aberta.get("obligation_hash") or ""),
        }
        satisfied[need_id] = registro
        closed[need_id] = {**aberta, **registro, "satisfied": True}
        closed_now.append(need_id)

    # -- obrigações que continuam (ou nascem) abertas ----------------------
    for need in _needs_of(payload):
        need_id = need_id_of(need)
        ohash = obligation_hash(need)
        registro = {**dict(need), "need_id": need_id, "obligation_hash": ohash, "round": round_no}
        if need_id in satisfied:
            # INVARIANTE §7.1: leitura satisfeita NÃO reabre por reaparecer na
            # lista de pendências da rodada seguinte. Só a mudança da própria
            # obrigação reabre — e com causa persistida.
            anterior = str(satisfied[need_id].get("obligation_hash") or "")
            if not anterior:
                # Primeira vez que a obrigação correspondente é vista com
                # forma completa (a leitura foi fechada por um resultado que
                # só trazia `need_id` + evidência). ADOTAR o hash não reabre
                # nada: sem referência anterior não existe "mudou", e tratar
                # ausência como mudança reabriria toda leitura fechada por um
                # worker que não repete a descrição da obrigação — exatamente
                # o que a invariante proíbe.
                satisfied[need_id] = {**satisfied[need_id], "obligation_hash": ohash}
                if need_id in closed:
                    closed[need_id] = {**closed[need_id], "obligation_hash": ohash}
                continue
            if anterior != ohash:
                satisfied.pop(need_id, None)
                closed.pop(need_id, None)
                open_obligations[need_id] = registro
                invalidations.append(
                    {
                        "reading_id": need_id,
                        "cause": CAUSE_OBLIGATION_CHANGED,
                        "detail": f"obrigação mudou ({anterior[:12]} -> {ohash[:12]})",
                        "round": round_no,
                        "at": moment,
                    }
                )
                reopened_now.append(need_id)
            continue
        open_obligations[need_id] = registro

    # -- conflitos ---------------------------------------------------------
    for item in _conflicts_of(payload):
        cid = item["conflict_id"]
        anterior = conflicts.get(cid)
        if item["resolved"]:
            if anterior is None or not anterior.get("resolved"):
                conflicts_now.append(cid)
            conflicts[cid] = {**(anterior or {}), **item, "resolved": True, "round": round_no}
        elif anterior is None:
            conflicts[cid] = {**item, "round": round_no}

    progresso = Progress(
        obligations_closed=tuple(closed_now),
        evidence_accepted=tuple(evidence_now),
        conflicts_resolved=tuple(conflicts_now),
        contract_fields_filled=tuple(fields_now),
        readings_reopened=tuple(reopened_now),
        rejected=tuple(rejected_now),
    )
    novo = replace(
        state,
        contract=contract,
        open_obligations=open_obligations,
        closed_obligations=closed,
        satisfied_readings=satisfied,
        accepted_evidence=evidence_pool,
        conflicts=conflicts,
        rejected_results=tuple(rejected_results),
        invalidations=tuple(invalidations),
        attempts=state.attempts + 1,
        consumed=_add_usage(state.consumed, payload.get("usage")),
        applied_results=tuple([*state.applied_results, rhash]),
        last_progress=progresso.to_dict(),
        updated_at=moment or state.updated_at,
    )
    return novo, progresso


def invalidate(
    state: ConsolidatedState,
    reading_id: str,
    cause: str,
    *,
    detail: str = "",
    round_no: int | None = None,
    now: str = "",
) -> ConsolidatedState:
    """Reabre UMA leitura satisfeita. É a ÚNICA porta de reabertura explícita.

    A causa é obrigatória e fica persistida em `invalidations`: o §7.1 exige
    saber POR QUE uma leitura voltou a estar aberta — sem isso, "reabriu" é
    indistinguível de "o worker esqueceu de citar de novo", que é justamente o
    que a invariante proíbe.

    Reabrir o que nunca esteve satisfeito é no-op (o estado volta igual): a
    obrigação já está aberta, e inventar uma invalidação sem leitura fecharia
    a porta para o diagnóstico correto.
    """
    if not _text(cause):
        raise StateError("invalidate exige causa: reabrir leitura sem causa é proibido (§7.1)")
    reading_id = _text(reading_id)
    if reading_id not in state.satisfied_readings:
        return state
    round_no = int(round_no if round_no is not None else state.rounds_used)
    registro = dict(state.satisfied_readings[reading_id])
    satisfied = {k: dict(v) for k, v in state.satisfied_readings.items() if k != reading_id}
    closed = {k: dict(v) for k, v in state.closed_obligations.items() if k != reading_id}
    open_obligations = {k: dict(v) for k, v in state.open_obligations.items()}
    open_obligations[reading_id] = {
        "need_id": reading_id,
        "target": registro.get("target", ""),
        "obligation_hash": registro.get("obligation_hash", ""),
        "motivo": detail or f"leitura invalidada ({cause})",
        "reopened_round": round_no,
    }
    invalidations = [
        *state.invalidations,
        {
            "reading_id": reading_id,
            "cause": _text(cause),
            "detail": _text(detail),
            "round": round_no,
            "at": now or state.updated_at,
        },
    ]
    return replace(
        state,
        satisfied_readings=satisfied,
        closed_obligations=closed,
        open_obligations=open_obligations,
        invalidations=tuple(invalidations),
        updated_at=now or state.updated_at,
    )


def record_dispatch(
    state: ConsolidatedState, needs: Sequence[Mapping[str, Any]], *, round_no: int | None = None
) -> tuple[ConsolidatedState, str]:
    """Registra o pacote despachado e devolve `(estado, package_hash)`.

    O hash fica persistido para que a rodada seguinte possa recusar despachar
    EXATAMENTE o mesmo pacote (§7.3: "repetir o mesmo pacote sem mudança é
    proibido").
    """
    phash = needs_package_hash(needs)
    if phash in state.dispatched_packages:
        return state, phash
    return (
        replace(
            state,
            dispatched_packages=tuple([*state.dispatched_packages, phash]),
            rounds_used=int(round_no) if round_no is not None else state.rounds_used,
        ),
        phash,
    )


def already_dispatched(state: ConsolidatedState, needs: Sequence[Mapping[str, Any]]) -> bool:
    return needs_package_hash(needs) in state.dispatched_packages


def with_stop_reason(state: ConsolidatedState, reason: str, *, now: str = "") -> ConsolidatedState:
    if reason and reason not in STOP_REASONS:
        raise StateError(
            f"motivo de parada {reason!r} fora do vocabulário do §7.3: {list(STOP_REASONS)}"
        )
    return replace(state, stop_reason=reason, updated_at=now or state.updated_at)


def budget_exhausted(budget: Mapping[str, Any], consumed: Mapping[str, Any]) -> tuple[bool, str]:
    """`(esgotado, detalhe)` comparando teto e consumo por chave.

    Chave sem teto declarado não esgota nada: um orçamento que não foi definido
    não pode ser "estourado" — o §7.3 manda informar consumo e o comando para
    ampliar, não inventar um limite.
    """
    for name, limit in dict(budget or {}).items():
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            continue
        key = str(name)
        used = consumed.get(key)
        if used is None and key.startswith("max_"):
            used = consumed.get(key[4:])
        if used is None:
            continue
        if used >= limit:
            return True, f"{key}: {used} de {limit} consumido"
    return False, ""


# --------------------------------------------------------------------------
# Persistência
# --------------------------------------------------------------------------

STATE_TABLE = "objective_state"


class StateStore:
    """Persistência do estado consolidado em `runtime.db` (tabela nova).

    Uma linha por `(repo_id, objective_id, input_revision)` — a chave do §7.1.
    O estado inteiro vai em JSON canônico: o consumidor deste dado é o próprio
    laço de continuação, que sempre o lê inteiro, e um esquema colunar aqui
    obrigaria migração a cada campo novo do contrato consolidado.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def of(cls, store: Any) -> "StateStore":
        """Aceita um `TaskStore` (usa a MESMA conexão) ou uma conexão."""
        conn = getattr(store, "conn", store)
        return cls(conn)

    def load(
        self, repo_id: str, objective_id: str, input_revision: str
    ) -> ConsolidatedState | None:
        row = self.conn.execute(
            f"SELECT state_json FROM {STATE_TABLE} "
            "WHERE repo_id=? AND objective_id=? AND input_revision=?",
            (str(repo_id or ""), str(objective_id or ""), str(input_revision or "")),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["state_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, Mapping):
            return None
        return ConsolidatedState.from_dict(data)

    def load_or_new(
        self,
        repo_id: str,
        objective_id: str,
        input_revision: str,
        **kwargs: Any,
    ) -> ConsolidatedState:
        found = self.load(repo_id, objective_id, input_revision)
        if found is not None:
            return found
        return new_state(repo_id, objective_id, input_revision, **kwargs)

    def save(self, state: ConsolidatedState, *, now: str = "") -> ConsolidatedState:
        moment = now or state.updated_at
        stored = replace(state, updated_at=moment)
        self.conn.execute(
            f"INSERT INTO {STATE_TABLE}"
            "(repo_id, objective_id, input_revision, state_json, stop_reason, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(repo_id, objective_id, input_revision) DO UPDATE SET "
            "state_json=excluded.state_json, stop_reason=excluded.stop_reason, "
            "updated_at=excluded.updated_at",
            (
                stored.repo_id,
                stored.objective_id,
                stored.input_revision,
                canonical_json(stored.to_dict()),
                stored.stop_reason,
                moment,
            ),
        )
        return stored

    def states_for(self, objective_id: str) -> list[ConsolidatedState]:
        rows = self.conn.execute(
            f"SELECT state_json FROM {STATE_TABLE} WHERE objective_id=? ORDER BY updated_at",
            (str(objective_id or ""),),
        ).fetchall()
        out: list[ConsolidatedState] = []
        for row in rows:
            try:
                data = json.loads(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(data, Mapping):
                out.append(ConsolidatedState.from_dict(data))
        return out

    def apply(
        self,
        state: ConsolidatedState,
        result: Any,
        *,
        round_no: int | None = None,
        now: str = "",
    ) -> tuple[ConsolidatedState, Progress]:
        """`apply_result` + persistência, numa chamada. O delta é gravado sempre.

        Gravar mesmo quando `Progress.has_progress` é `False` é deliberado: o
        §7.3 exige diagnóstico de uma rodada sem progresso, e o diagnóstico
        depende de saber que ela ACONTECEU (tentativa contada, resultado já
        aplicado). É também o que impede a retomada de reaplicar o resultado.
        """
        novo, progresso = apply_result(state, result, round_no=round_no, now=now)
        if progresso.duplicate:
            return novo, progresso
        return self.save(novo, now=now), progresso


__all__ = [
    "CAUSE_EVIDENCE_CHANGED",
    "CAUSE_EVIDENCE_INVALID",
    "CAUSE_OBLIGATION_CHANGED",
    "CAUSE_OPERATOR",
    "STATE_TABLE",
    "STOP_AMBIGUITY",
    "STOP_BUDGET_EXHAUSTED",
    "STOP_COMPLETED",
    "STOP_EVIDENCE_CHANGED",
    "STOP_EXECUTOR_UNAVAILABLE",
    "STOP_INTERRUPTED",
    "STOP_NO_PROGRESS",
    "STOP_REASONS",
    "ConsolidatedState",
    "Progress",
    "StateError",
    "StateStore",
    "already_dispatched",
    "apply_result",
    "budget_exhausted",
    "canonical_json",
    "content_hash",
    "evidence_key",
    "invalidate",
    "merge_progress",
    "need_id_of",
    "needs_package_hash",
    "new_state",
    "obligation_hash",
    "record_dispatch",
    "with_stop_reason",
]
