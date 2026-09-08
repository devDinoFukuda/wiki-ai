"""Envelopes versionados de tarefa e resultado (spec §10.4.4, linhas 426-430).

Contrato ÚNICO entre controlador e adaptador. O adaptador pode transformar o
envelope; não pode mudar o significado da afirmação — por isso a validação
mora aqui, e não em cada adaptador.

Invariantes em código:

* Schemas FECHADOS: `TASK_FIELDS`/`RESULT_FIELDS` são a superfície inteira.
  Campo desconhecido em envelope de protocolo é recusado; campo extensível de
  fornecedor só existe dentro da área declarada `vendor`.
* `validate_result` recusa, com motivo distinto: tentativa cancelada, lease
  expirado/invalidado, `protocol_version`/`input_revision`/`binding_id`
  divergentes, `context_hash` inválido e identidade de execução/tentativa
  divergente.
* Receber o mesmo resultado duas vezes é idempotente: `ResultEnvelope.result_hash`
  é estável por `execution_id` + conteúdo, e quem grava compara o hash antes
  de aplicar (ver `coordinator.accept_result`).
* Texto não parseável vira `execution_status="diagnostic"` com diagnóstico
  (`adapt_agent_text`), NUNCA `done` com `claims` vazio — e `validate_result`
  recusa `done` sem nenhuma afirmação/evidência.
* `agent_provenance.model = None` é registrado como INDISPONÍVEL
  (`model_available: False`); nunca inferido do nome do host (§10.4.2 item 7).

Somente stdlib.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

#: Versão do contrato de envelope. Divergência é recusa, não conversão.
PROTOCOL_VERSION = "wiki-ai/agent-protocol/1"

#: §10.4.4 — envelope de tarefa versionado.
TASK_FIELDS = (
    "protocol_version",
    "task_id",
    "execution_id",
    "attempt_id",
    "objective_id",
    "input_revision",
    "context_hash",
    "objective",
    "accumulated_state",
    "reading_needs",
    "evidence",
    "result_schema",
    "budget",
    "deadline",
    "lease_id",
    "vendor",
)

#: §10.4.4 — envelope de resultado com identidades correspondentes.
RESULT_FIELDS = (
    "protocol_version",
    "task_id",
    "execution_id",
    "attempt_id",
    "objective_id",
    "input_revision",
    "context_hash",
    "binding_id",
    "execution_status",
    "claims",
    "evidence_refs",
    "reading_satisfied",
    "remaining_needs",
    "diagnostics",
    "usage",
    "agent_provenance",
    "vendor",
)

#: Chaves aceitas de um envelope LEGADO (`{execution_id, output}` do protocolo
#: `AgentExecutor`), traduzidas para o envelope fechado por `coerce_result`.
LEGACY_RESULT_KEYS = frozenset(
    {"execution_id", "task_id", "objective_id", "output", "error", "input_versions",
     "input_revision", "usage", "context_hash", "attempt_id", "binding_id"}
)

DONE = "done"
PARTIAL = "partial"
FAILED = "failed"
CANCELLED = "cancelled"
DIAGNOSTIC = "diagnostic"

EXECUTION_STATUSES = frozenset({DONE, PARTIAL, FAILED, CANCELLED, DIAGNOSTIC})

#: Status que exigem conteúdo: `done`/`partial` sem nenhuma afirmação,
#: evidência ou leitura satisfeita são recusados (nunca "done vazio").
CONTENTFUL_STATUSES = frozenset({DONE, PARTIAL})


class EnvelopeError(ValueError):
    """Envelope recusado. `reason` é o código estável usado pelo coordenador."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# Códigos de recusa (estáveis; mapeados em `coordinator.REJECTION_TO_ERROR_CLASS`).
R_PROTOCOL = "protocol_divergence"
R_TASK = "task_mismatch"
R_EXECUTION = "execution_mismatch"
R_ATTEMPT = "attempt_mismatch"
R_OBJECTIVE = "objective_mismatch"
R_INPUT = "input_divergence"
R_CONTEXT = "context_invalid"
R_BINDING = "binding_divergence"
R_LEASE = "lease_invalid"
R_CANCELLED = "attempt_cancelled"
R_MALFORMED = "malformed"
R_SCHEMA = "schema_invalid"
R_UNPARSEABLE = "unparseable"


def _as_items(candidate: Mapping[str, Any], name: str) -> tuple[Any, ...]:
    """Lista de itens de um campo de resultado, ou `EnvelopeError` tipado.

    `tuple(x or ())` levantava `TypeError` para qualquer valor não iterável
    (`{"evidence_refs": 3}`) e a exceção NÃO era `EnvelopeError`: ela subia por
    `validate_result` -> `coordinator.accept_result` -> `coordinator.run` e
    derrubava o laço inteiro por causa de UM worker malformado. Pior, `str` é
    iterável: `"a,b"` virava `("a", ",", "b")` — três "referências de
    evidência" inventadas a partir de uma frase.

    Aqui só lista/tupla passam; qualquer outra coisa é recusa do resultado
    (motivo `malformed`), que o coordenador já sabe classificar e registrar
    como tentativa rejeitada daquela tarefa.
    """
    raw = candidate.get(name)
    if raw is None or raw == "":
        return ()
    if isinstance(raw, Mapping):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    raise EnvelopeError(
        R_MALFORMED,
        f"{name} não é lista de objetos: {type(raw).__name__}",
    )


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_hash(payload: Any) -> str:
    """Hash do contexto efetivamente enviado (pacote montado + estado acumulado)."""
    return canonical_hash(payload)


# --------------------------------------------------------------------------
# Proveniência (§10.4.2 item 7)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Agente, versão do adaptador, transporte, binding, host e modelo."""

    agent_id: str
    adapter_version: str
    transport: str
    binding_id: str
    host_version: str | None = None
    #: `None` ⇒ o host não expôs o modelo. Registrar indisponibilidade é o
    #: contrato; inferir do nome do host é proibido.
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "adapter_version": self.adapter_version,
            "transport": self.transport,
            "binding_id": self.binding_id,
            "host_version": self.host_version,
            "model": self.model,
            "model_available": self.model is not None,
        }

    @classmethod
    def from_binding(cls, binding: Any) -> "Provenance":
        return cls(
            agent_id=getattr(binding, "agent_id", ""),
            adapter_version=getattr(binding, "adapter_version", ""),
            transport=getattr(binding, "transport", ""),
            binding_id=getattr(binding, "binding_id", ""),
            host_version=getattr(binding, "host_version", None),
            model=getattr(binding, "model", None),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Provenance":
        if not isinstance(data, Mapping):
            raise EnvelopeError(R_MALFORMED, "agent_provenance não é objeto")
        unknown = set(data) - set(cls.__dataclass_fields__) - {"model_available"}
        if unknown:
            raise EnvelopeError(
                R_SCHEMA, f"agent_provenance com campo não declarado: {sorted(unknown)}"
            )
        return cls(
            agent_id=str(data.get("agent_id") or ""),
            adapter_version=str(data.get("adapter_version") or ""),
            transport=str(data.get("transport") or ""),
            binding_id=str(data.get("binding_id") or ""),
            host_version=data.get("host_version"),
            # Ausente e `None` são a MESMA coisa: indisponível.
            model=data.get("model"),
        )


# --------------------------------------------------------------------------
# Envelope de tarefa
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    objective_id: str
    input_revision: str
    context_hash: str
    objective: Mapping[str, Any]
    accumulated_state: Mapping[str, Any] = field(default_factory=dict)
    reading_needs: tuple[Mapping[str, Any], ...] = ()
    evidence: tuple[Any, ...] = ()
    result_schema: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)
    deadline: str | None = None
    lease_id: str = ""
    attempt_id: str = ""
    #: Emitido pelo adaptador em `submit`; vazio até então (F07).
    execution_id: str = ""
    protocol_version: str = PROTOCOL_VERSION
    vendor: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.task_id).strip():
            raise EnvelopeError(R_MALFORMED, "envelope de tarefa sem task_id")
        if self.protocol_version != PROTOCOL_VERSION:
            raise EnvelopeError(
                R_PROTOCOL, f"protocol_version {self.protocol_version!r} não suportada"
            )

    def with_execution_id(self, execution_id: str) -> "TaskEnvelope":
        return replace(self, execution_id=str(execution_id))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "attempt_id": self.attempt_id,
            "objective_id": self.objective_id,
            "input_revision": self.input_revision,
            "context_hash": self.context_hash,
            "objective": dict(self.objective),
            "accumulated_state": dict(self.accumulated_state),
            "reading_needs": [dict(n) for n in self.reading_needs],
            "evidence": list(self.evidence),
            "result_schema": dict(self.result_schema),
            "budget": dict(self.budget),
            "deadline": self.deadline,
            "lease_id": self.lease_id,
            "vendor": dict(self.vendor),
        }
        assert set(data) == set(TASK_FIELDS)  # schema fechado, verificado em execução
        return data

    @property
    def envelope_hash(self) -> str:
        return canonical_hash(self.to_dict())


def validate_task(raw: Mapping[str, Any]) -> TaskEnvelope:
    """Schema fechado do envelope de tarefa (usado por adaptadores de teste/ponte)."""
    if not isinstance(raw, Mapping):
        raise EnvelopeError(R_MALFORMED, f"envelope de tarefa não é objeto: {type(raw).__name__}")
    unknown = set(raw) - set(TASK_FIELDS)
    if unknown:
        raise EnvelopeError(R_SCHEMA, f"campo não declarado no envelope de tarefa: {sorted(unknown)}")
    return TaskEnvelope(
        task_id=str(raw.get("task_id") or ""),
        objective_id=str(raw.get("objective_id") or ""),
        input_revision=str(raw.get("input_revision") or ""),
        context_hash=str(raw.get("context_hash") or ""),
        objective=dict(raw.get("objective") or {}),
        accumulated_state=dict(raw.get("accumulated_state") or {}),
        reading_needs=tuple(dict(n) for n in (raw.get("reading_needs") or ())),
        evidence=tuple(raw.get("evidence") or ()),
        result_schema=dict(raw.get("result_schema") or {}),
        budget=dict(raw.get("budget") or {}),
        deadline=raw.get("deadline"),
        lease_id=str(raw.get("lease_id") or ""),
        attempt_id=str(raw.get("attempt_id") or ""),
        execution_id=str(raw.get("execution_id") or ""),
        protocol_version=str(raw.get("protocol_version") or PROTOCOL_VERSION),
        vendor=dict(raw.get("vendor") or {}),
    )


# --------------------------------------------------------------------------
# Envelope de resultado
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultEnvelope:
    task_id: str
    execution_id: str
    objective_id: str
    input_revision: str
    context_hash: str
    binding_id: str
    execution_status: str
    agent_provenance: Provenance
    attempt_id: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[Any, ...] = ()
    reading_satisfied: tuple[Mapping[str, Any], ...] = ()
    remaining_needs: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, Any] | None = None
    protocol_version: str = PROTOCOL_VERSION
    vendor: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "attempt_id": self.attempt_id,
            "objective_id": self.objective_id,
            "input_revision": self.input_revision,
            "context_hash": self.context_hash,
            "binding_id": self.binding_id,
            "execution_status": self.execution_status,
            "claims": dict(self.claims),
            "evidence_refs": list(self.evidence_refs),
            "reading_satisfied": [dict(r) for r in self.reading_satisfied],
            "remaining_needs": [dict(r) for r in self.remaining_needs],
            "diagnostics": [dict(d) for d in self.diagnostics],
            "usage": dict(self.usage) if self.usage is not None else None,
            "agent_provenance": self.agent_provenance.to_dict(),
            "vendor": dict(self.vendor),
        }
        assert set(data) == set(RESULT_FIELDS)  # schema fechado, verificado em execução
        return data

    @property
    def result_hash(self) -> str:
        """Identidade do resultado: mesmo `execution_id` + mesmo conteúdo ⇒ mesmo hash.

        É o que torna receber o mesmo resultado duas vezes um no-op.
        """
        return canonical_hash(self.to_dict())

    def output(self) -> dict[str, Any]:
        """Payload de negócio (o que o `ResultSchema` fechado do coordenador valida)."""
        return dict(self.claims)


def build_result(
    envelope: TaskEnvelope,
    *,
    execution_id: str,
    provenance: Provenance,
    execution_status: str = DONE,
    claims: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[Any] = (),
    reading_satisfied: Sequence[Mapping[str, Any]] = (),
    remaining_needs: Sequence[Mapping[str, Any]] = (),
    diagnostics: Sequence[Mapping[str, Any]] = (),
    usage: Mapping[str, Any] | None = None,
    vendor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Candidato a resultado, com as identidades COPIADAS do envelope de tarefa.

    É o ponto onde o adaptador normaliza a saída do host: as identidades não
    vêm do worker (F07), vêm do envelope que o controlador emitiu.
    """
    return ResultEnvelope(
        task_id=envelope.task_id,
        execution_id=str(execution_id),
        objective_id=envelope.objective_id,
        input_revision=envelope.input_revision,
        context_hash=envelope.context_hash,
        binding_id=provenance.binding_id,
        execution_status=execution_status,
        agent_provenance=provenance,
        attempt_id=envelope.attempt_id,
        claims=dict(claims or {}),
        evidence_refs=tuple(evidence_refs),
        reading_satisfied=tuple(dict(r) for r in reading_satisfied),
        remaining_needs=tuple(dict(r) for r in remaining_needs),
        diagnostics=tuple(dict(d) for d in diagnostics),
        usage=dict(usage) if usage is not None else None,
        vendor=dict(vendor or {}),
    ).to_dict()


def adapt_agent_text(
    text: Any,
    envelope: TaskEnvelope,
    provenance: Provenance,
    *,
    execution_id: str,
) -> dict[str, Any]:
    """Adapta uma resposta TEXTUAL do host para envelope válido (§10.4.4).

    JSON parseável vira `claims`. Texto não parseável vira
    `execution_status="diagnostic"` com o motivo e um recorte do texto —
    nunca `done` com conteúdo vazio.
    """
    raw = text
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, Mapping):
        return build_result(
            envelope, execution_id=execution_id, provenance=provenance,
            claims=dict(raw),
        )
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return build_result(
                envelope,
                execution_id=execution_id,
                provenance=provenance,
                execution_status=DIAGNOSTIC,
                diagnostics=[
                    {
                        "code": R_UNPARSEABLE,
                        "message": f"resposta textual não parseável como JSON: {exc}",
                        "excerpt": raw[:2000],
                    }
                ],
            )
        if isinstance(parsed, Mapping):
            return build_result(
                envelope, execution_id=execution_id, provenance=provenance,
                claims=dict(parsed),
            )
        return build_result(
            envelope,
            execution_id=execution_id,
            provenance=provenance,
            execution_status=DIAGNOSTIC,
            diagnostics=[
                {
                    "code": R_UNPARSEABLE,
                    "message": f"JSON de topo não é objeto: {type(parsed).__name__}",
                    "excerpt": raw[:2000],
                }
            ],
        )
    return build_result(
        envelope,
        execution_id=execution_id,
        provenance=provenance,
        execution_status=DIAGNOSTIC,
        diagnostics=[
            {
                "code": R_UNPARSEABLE,
                "message": f"resposta de tipo não adaptável: {type(raw).__name__}",
            }
        ],
    )


def coerce_result(raw: Any) -> dict[str, Any]:
    """Normaliza qualquer envelope recebido para a FORMA FECHADA do §10.4.4.

    Duas entradas são aceitas e ambas terminam no MESMO formato:

    * envelope de protocolo (tem `protocol_version`) — schema fechado, campo
      não declarado é recusado;
    * envelope legado do protocolo `AgentExecutor` (`{execution_id, output,
      error}`) — `output` vira `claims`, `error` vira `diagnostics`, e
      qualquer chave fora de `LEGACY_RESULT_KEYS` vai para `vendor` (área
      declarada), sem alterar o contrato de negócio.
    """
    if not isinstance(raw, Mapping):
        raise EnvelopeError(R_MALFORMED, f"envelope não é objeto: {type(raw).__name__}")
    if "protocol_version" in raw:
        unknown = set(raw) - set(RESULT_FIELDS)
        if unknown:
            raise EnvelopeError(
                R_SCHEMA, f"campo não declarado em schema fechado: {sorted(unknown)}"
            )
        return dict(raw)

    output = raw.get("output", None)
    error = raw.get("error")
    diagnostics: list[dict[str, Any]] = []
    if error is not None:
        diagnostics.append(
            {"code": "executor_error", "message": error if isinstance(error, str) else str(error)}
        )
    vendor = {k: v for k, v in raw.items() if k not in LEGACY_RESULT_KEYS}
    status = DONE if isinstance(output, Mapping) else (FAILED if error is not None else DIAGNOSTIC)
    if output is not None and not isinstance(output, Mapping):
        raise EnvelopeError(R_MALFORMED, f"output legado não é objeto: {type(output).__name__}")
    if output is None and error is None:
        raise EnvelopeError(R_MALFORMED, "envelope sem campo 'output'")
    return {
        "task_id": raw.get("task_id"),
        "execution_id": raw.get("execution_id"),
        "attempt_id": raw.get("attempt_id"),
        "objective_id": raw.get("objective_id"),
        "input_revision": raw.get("input_revision", raw.get("input_versions")),
        "context_hash": raw.get("context_hash"),
        "binding_id": raw.get("binding_id"),
        "execution_status": status,
        "claims": dict(output or {}),
        "diagnostics": diagnostics,
        "usage": raw.get("usage"),
        "vendor": vendor,
    }


def _mismatch(name: str, declared: Any, expected: Any) -> bool:
    """`True` quando o valor DECLARADO existe e diverge do esperado.

    Ausência não é divergência: um adaptador legado não preenche todas as
    identidades, e o envelope de tarefa é a autoridade sobre elas.
    """
    if declared in (None, ""):
        return False
    return str(declared) != str(expected)


def validate_result(
    raw: Any,
    envelope: TaskEnvelope,
    *,
    binding_id: str | None = None,
    lease_valid: bool = True,
    attempt_cancelled: bool = False,
    input_revision_matches: Any = None,
) -> ResultEnvelope:
    """Única porta de aceitação de resultado do runtime (§10.4.4).

    Ordem das recusas — identidade e autorização ANTES de conteúdo:

    1. tentativa cancelada (`attempt_cancelled`);
    2. lease expirado/invalidado (`lease_valid=False`);
    3. `protocol_version` divergente;
    4. identidades (`task_id`, `execution_id`, `attempt_id`, `objective_id`);
    5. `input_revision` e `context_hash` divergentes;
    6. `binding_id` divergente (resultado tardio do binding anterior);
    7. `execution_status` fora do vocabulário fechado;
    8. `done`/`partial` sem afirmação/evidência/leitura ⇒ `unparseable`.

    `input_revision_matches` permite que o chamador compare por HASH em vez de
    igualdade textual (o coordenador compara `input_versions_hash`): quando
    informado, é um callable `(declarado) -> bool`.
    """
    if attempt_cancelled:
        raise EnvelopeError(
            R_CANCELLED,
            f"tentativa da execução {envelope.execution_id or '?'} foi cancelada; "
            "resultado tardio não altera estado",
        )
    if not lease_valid:
        raise EnvelopeError(
            R_LEASE,
            f"lease {envelope.lease_id!r} expirado ou invalidado; resultado recusado",
        )

    candidate = coerce_result(raw)

    declared_protocol = candidate.get("protocol_version")
    if declared_protocol not in (None, "", PROTOCOL_VERSION):
        raise EnvelopeError(
            R_PROTOCOL,
            f"resultado declara protocolo {declared_protocol!r}; suportado {PROTOCOL_VERSION!r}",
        )

    for key, expected, reason in (
        ("task_id", envelope.task_id, R_TASK),
        ("execution_id", envelope.execution_id, R_EXECUTION),
        ("attempt_id", envelope.attempt_id, R_ATTEMPT),
        ("objective_id", envelope.objective_id, R_OBJECTIVE),
    ):
        if expected and _mismatch(key, candidate.get(key), expected):
            raise EnvelopeError(
                reason, f"resultado declara {key}={candidate.get(key)!r}; esperado {expected!r}"
            )

    declared_rev = candidate.get("input_revision")
    if declared_rev not in (None, ""):
        if callable(input_revision_matches):
            if not input_revision_matches(declared_rev):
                raise EnvelopeError(
                    R_INPUT,
                    f"resultado descreve input_revision fora do snapshot da tarefa: {declared_rev!r}",
                )
        elif _mismatch("input_revision", declared_rev, envelope.input_revision):
            raise EnvelopeError(
                R_INPUT,
                f"resultado descreve input_revision {declared_rev!r}; "
                f"esperado {envelope.input_revision!r}",
            )

    if envelope.context_hash and _mismatch(
        "context_hash", candidate.get("context_hash"), envelope.context_hash
    ):
        raise EnvelopeError(
            R_CONTEXT,
            f"resultado descreve contexto {str(candidate.get('context_hash'))[:12]}; "
            f"enviado {envelope.context_hash[:12]}",
        )

    provenance_raw = candidate.get("agent_provenance")
    provenance = (
        Provenance.from_dict(provenance_raw)
        if isinstance(provenance_raw, Mapping)
        else Provenance("", "", "", str(candidate.get("binding_id") or binding_id or ""))
    )
    expected_binding = binding_id if binding_id is not None else provenance.binding_id
    if expected_binding:
        if _mismatch("binding_id", candidate.get("binding_id"), expected_binding):
            raise EnvelopeError(
                R_BINDING,
                f"resultado do binding {candidate.get('binding_id')!r}; vigente {expected_binding!r}",
            )
        if provenance.binding_id and _mismatch(
            "binding_id", provenance.binding_id, expected_binding
        ):
            raise EnvelopeError(
                R_BINDING,
                f"proveniência do binding {provenance.binding_id!r}; vigente {expected_binding!r}",
            )

    status = str(candidate.get("execution_status") or DONE)
    if status not in EXECUTION_STATUSES:
        raise EnvelopeError(R_SCHEMA, f"execution_status fora do vocabulário: {status!r}")

    claims = candidate.get("claims") or {}
    if not isinstance(claims, Mapping):
        raise EnvelopeError(R_MALFORMED, f"claims não é objeto: {type(claims).__name__}")
    evidence_refs = _as_items(candidate, "evidence_refs")
    reading_satisfied = _as_items(candidate, "reading_satisfied")
    diagnostics = _as_items(candidate, "diagnostics")
    remaining_needs = _as_items(candidate, "remaining_needs")
    if status in CONTENTFUL_STATUSES and not (claims or evidence_refs or reading_satisfied):
        raise EnvelopeError(
            R_UNPARSEABLE,
            f"execution_status={status!r} sem claims/evidence_refs/reading_satisfied; "
            "resposta vazia não é conclusão",
        )

    return ResultEnvelope(
        task_id=envelope.task_id,
        execution_id=envelope.execution_id or str(candidate.get("execution_id") or ""),
        objective_id=envelope.objective_id,
        input_revision=envelope.input_revision,
        context_hash=envelope.context_hash,
        binding_id=str(expected_binding or ""),
        execution_status=status,
        agent_provenance=provenance,
        attempt_id=envelope.attempt_id,
        claims=dict(claims),
        evidence_refs=evidence_refs,
        reading_satisfied=tuple(dict(r) for r in reading_satisfied if isinstance(r, Mapping)),
        remaining_needs=tuple(dict(r) for r in remaining_needs if isinstance(r, Mapping)),
        diagnostics=tuple(dict(d) for d in diagnostics if isinstance(d, Mapping)),
        usage=dict(candidate["usage"]) if isinstance(candidate.get("usage"), Mapping) else None,
        vendor=dict(candidate.get("vendor") or {}),
    )


__all__ = [
    "CANCELLED",
    "CONTENTFUL_STATUSES",
    "DIAGNOSTIC",
    "DONE",
    "EXECUTION_STATUSES",
    "FAILED",
    "LEGACY_RESULT_KEYS",
    "PARTIAL",
    "PROTOCOL_VERSION",
    "RESULT_FIELDS",
    "TASK_FIELDS",
    "EnvelopeError",
    "Provenance",
    "ResultEnvelope",
    "TaskEnvelope",
    "adapt_agent_text",
    "build_result",
    "canonical_hash",
    "coerce_result",
    "context_hash",
    "validate_result",
    "validate_task",
    "R_ATTEMPT",
    "R_BINDING",
    "R_CANCELLED",
    "R_CONTEXT",
    "R_EXECUTION",
    "R_INPUT",
    "R_LEASE",
    "R_MALFORMED",
    "R_OBJECTIVE",
    "R_PROTOCOL",
    "R_SCHEMA",
    "R_TASK",
    "R_UNPARSEABLE",
]
