"""Laço determinístico de execução automática (plano §7.2, §7.3, §13.1, onda W4).

Ciclo
-----
    plan_from_objectives → ready_tasks → lease → context.build_package →
    executor.submit → executor.status → executor.result → aceitação → gravação

O que este módulo garante
-------------------------
| Regra (plano)                                  | Onde                                   |
|------------------------------------------------|----------------------------------------|
| `execution_id` é do executor, nunca do worker   | `accept_result` (F07)                  |
| Execução desconhecida ⇒ rejeitado com registro  | `accept_result` / `RejectionReason`    |
| Revisão de input divergente ⇒ rejeitado         | `accept_result`                        |
| Schema fechado; campo inesperado que altera semântica/controle ⇒ rejeição (§13.1) | `ResultSchema.validate` |
| Attempt + resultado na MESMA transação          | `tasks.TaskStore.record_result`        |
| Diagnóstico {causa, impacto, correção, decisão} | `Diagnostic.message`                   |

O que este módulo NÃO faz
-------------------------
Não escreve em `knowledge.db`. §7.2: "o grafo de tarefas operacionais é
separado do grafo de conhecimento; uma tarefa concluída não cria
automaticamente um fato confirmado". `run()` deixa o resultado validado em
`tasks.result_json`; integrá-lo à base canônica é passo explícito de quem
consome — inclusive porque só ali existe a revisão e a outbox do §4.2.

Vizinhos (`context.py`, `executors/`) entram por import TARDIO e DEFENSIVO:
sem eles o coordenador continua planejando, agendando e validando — apenas sem
pacote montado (referências vazias) ou sem despacho.

Só stdlib + `analysis`/`knowledge` por contrato. Sem `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import enum
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import agents as A
from . import envelopes as E
from . import recovery as R
from . import state as S
from . import tasks as T

# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class CoordinatorError(Exception):
    """Base dos erros do coordenador."""


class DispatchUnavailable(CoordinatorError):
    """§7.3 — engine sem capacidade real de despacho.

    Informada UMA vez, na configuração do laço. Não existe fallback para
    "copiar prompt": voltar ao handoff manual é exatamente o que W4 remove.
    """


class ResultRejected(CoordinatorError):
    """Resultado recusado por F07/§13.1. Recusa é registrada, nunca silenciosa."""

    def __init__(self, reason: "RejectionReason", detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Aceitação de resultado
# --------------------------------------------------------------------------


class RejectionReason(str, enum.Enum):
    """Motivos de recusa. Cada um vira `termination_reason` no banco."""

    #: `execution_id` que a integração nunca emitiu (F07).
    UNKNOWN_EXECUTION = "rejected:unknown_execution"
    #: Envelope declara execução diferente da que foi submetida.
    EXECUTION_MISMATCH = "rejected:execution_mismatch"
    #: Envelope declara outra tarefa.
    TASK_MISMATCH = "rejected:task_mismatch"
    #: Resultado descreve versão de entrada diferente da tarefa.
    INPUT_DIVERGENCE = "rejected:input_divergence"
    #: Envelope/saída fora do formato de máquina.
    MALFORMED = "rejected:malformed"
    #: Schema fechado violado (campo ausente, tipo errado, campo inesperado).
    SCHEMA_INVALID = "rejected:schema_invalid"
    #: Campo que mudaria orçamento, ferramentas, permissões, destino ou aprovação.
    CONTROL_FIELD = "rejected:control_field"
    #: spec §10.4.4 — envelope de outra versão de protocolo.
    PROTOCOL_DIVERGENCE = "rejected:protocol_divergence"
    #: Envelope declara outra tentativa da mesma tarefa.
    ATTEMPT_MISMATCH = "rejected:attempt_mismatch"
    #: Envelope declara outro objetivo.
    OBJECTIVE_MISMATCH = "rejected:objective_mismatch"
    #: `context_hash` diferente do contexto que foi de fato enviado.
    CONTEXT_INVALID = "rejected:context_invalid"
    #: Resultado tardio de um binding que não é mais o vigente.
    BINDING_DIVERGENCE = "rejected:binding_divergence"
    #: Lease expirado ou invalidado (`agent connect --cancel-active`).
    LEASE_INVALID = "rejected:lease_invalid"
    #: Tentativa cancelada: resultado tardio não altera estado.
    ATTEMPT_CANCELLED = "rejected:attempt_cancelled"
    #: Texto do host não adaptável a resultado válido (nunca `done` vazio).
    UNPARSEABLE = "rejected:unparseable"


#: Códigos de `envelopes.EnvelopeError` → motivo de recusa do coordenador.
#: A tradução existe porque a VALIDAÇÃO de envelope tem uma implementação só
#: (`runtime.envelopes`); aqui só se decide a política de erro do §7.4.
ENVELOPE_REJECTIONS: Mapping[str, RejectionReason] = {
    E.R_PROTOCOL: RejectionReason.PROTOCOL_DIVERGENCE,
    E.R_TASK: RejectionReason.TASK_MISMATCH,
    E.R_EXECUTION: RejectionReason.EXECUTION_MISMATCH,
    E.R_ATTEMPT: RejectionReason.ATTEMPT_MISMATCH,
    E.R_OBJECTIVE: RejectionReason.OBJECTIVE_MISMATCH,
    E.R_INPUT: RejectionReason.INPUT_DIVERGENCE,
    E.R_CONTEXT: RejectionReason.CONTEXT_INVALID,
    E.R_BINDING: RejectionReason.BINDING_DIVERGENCE,
    E.R_LEASE: RejectionReason.LEASE_INVALID,
    E.R_CANCELLED: RejectionReason.ATTEMPT_CANCELLED,
    E.R_MALFORMED: RejectionReason.MALFORMED,
    E.R_SCHEMA: RejectionReason.SCHEMA_INVALID,
    E.R_UNPARSEABLE: RejectionReason.UNPARSEABLE,
}


#: §13.1 — "payload não pode mudar orçamento, ferramentas, permissões ou estado
#: de aprovação" e "nunca aceitar paths arbitrários de worker para escrita".
#: Estes nomes são recusados na saída MESMO que um schema os declare: o schema
#: é de dados, e estes campos são de controle.
CONTROL_FIELDS = frozenset(
    {
        "approval",
        "approved",
        "allowed_paths",
        "budget",
        "budget_json",
        "capabilities",
        "destination",
        "lease_ttl",
        "max_concurrency",
        "output_path",
        "permissions",
        "policy",
        "policies",
        "tools",
        "write_path",
        "write_paths",
    }
)


#: Onda10-C — campos de DADOS (não controle) que descrevem investigação
#: parcial: leituras que o worker precisaria para fechar o objetivo
#: (`reading_needs`) e leituras que ele já conseguiu satisfazer nesta rodada
#: (`reading_satisfied`). Validação aqui é ESTRUTURAL (lista de objetos com
#: chaves string) — o conteúdo de cada item é dado de domínio de
#: `analysis`/`knowledge`, não deste módulo.
READING_LIST_FIELDS = ("reading_needs", "reading_satisfied")


def _invalid_reading_list(name: str, value: Any) -> str | None:
    """`None` quando `value` é uma lista de objetos com chaves string; senão o motivo."""
    if not isinstance(value, list):
        return f"{name} deve ser uma lista; veio {type(value).__name__}"
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            return f"{name}[{index}] deve ser um objeto; veio {type(item).__name__}"
        for key in item:
            if not isinstance(key, str):
                return f"{name}[{index}] tem chave não textual: {key!r}"
    return None


@dataclass(frozen=True)
class ResultSchema:
    """Schema FECHADO de saída (§13.1): o que não está declarado não entra.

    `strict=True` é o default porque "contrato de máquina" com campo livre não
    é contrato: um campo novo que ninguém validou é exatamente por onde entram
    semântica e controle não previstos.
    """

    required: tuple[str, ...] = ("objective_id",)
    optional: tuple[str, ...] = (
        "capability_id",
        "contract",
        "evidence",
        "facts",
        "gaps",
        "matrix",
        "notes",
        # Onda10-C (achado #3, 2ª auditoria externa): DADOS de leitura pendente
        # e leitura satisfeita, não CONTROLE. O worker pode devolvê-los junto
        # do resultado sem que o schema fechado os trate como campo inesperado
        # — quem decide o que fazer com eles é `coordinator.plan_continuations`,
        # nunca o próprio worker (isso continua vedado pelos CONTROL_FIELDS).
        "reading_needs",
        "reading_satisfied",
        "relations",
        "state",
        "unresolved",
    )
    types: Mapping[str, Any] = field(default_factory=dict)
    strict: bool = True
    version: str = "worker_result/1"
    #: Chaves EXTRAS autorizadas pelo OPERADOR (nunca pelo worker) via
    #: `with_extra`. Ficam também em `optional` — este campo existe para que
    #: quem consome o resultado saiba QUAIS chaves são extensão de perfil e
    #: possa preservá-las separadamente (`extra` do outcome integrado), em vez
    #: de tratá-las como vocabulário fixo do runtime.
    extra: tuple[str, ...] = ()

    @property
    def declared(self) -> frozenset[str]:
        return frozenset(self.required) | frozenset(self.optional)

    def with_extra(self, keys: Iterable[str]) -> "ResultSchema":
        """Novo schema com `keys` aceitas como opcionais — `strict` INTACTO.

        Flexibilizar o vocabulário de DADOS é decisão do operador (perfil de
        análise); flexibilizar CONTROLE não é decisão de ninguém. Por isso:

        * chave em `CONTROL_FIELDS` ⇒ `ValueError` (o schema não abre porta que
          `validate` fecharia depois — a recusa é na configuração, não em
          runtime, para que o operador saiba antes de despachar);
        * chave já declarada (`required`/`optional`) ⇒ `ValueError`, porque
          "ampliar" um campo que o runtime já valida esconderia um conflito de
          significado entre perfil e núcleo;
        * `strict` continua o que era: chave NÃO listada segue rejeitada.
        """
        novas: list[str] = []
        for raw in keys or ():
            name = str(raw).strip()
            if not name:
                raise ValueError("chave extra vazia: um campo sem nome não é contrato")
            if name in CONTROL_FIELDS:
                raise ValueError(
                    f"chave extra {name!r} é campo de CONTROLE (§13.1): orçamento, "
                    "ferramentas, permissões e destino nunca vêm do worker"
                )
            if name in self.declared:
                raise ValueError(
                    f"chave extra {name!r} já é campo declarado do schema "
                    f"({self.version}): redeclarar mudaria o significado do núcleo"
                )
            if name in novas:
                continue
            novas.append(name)
        if not novas:
            return self
        return replace(
            self,
            optional=tuple(self.optional) + tuple(novas),
            extra=tuple(self.extra) + tuple(novas),
        )

    def to_dict(self) -> dict[str, Any]:
        """Forma JSON-serializável enviada ao executor.

        O contrato de `AgentExecutor.submit` recebe `Mapping`, e adapters que
        montam prompt serializam o argumento — mandar o dataclass quebraria
        ali. `forbidden` viaja junto para que o worker saiba, antes de
        responder, o que será recusado (§13.1).
        """
        return {
            "version": self.version,
            "required": list(self.required),
            "optional": list(self.optional),
            "strict": self.strict,
            "forbidden": sorted(CONTROL_FIELDS),
            # Só viaja quando existe: um envelope sem extensão de perfil
            # continua byte-a-byte o que era antes desta capacidade.
            **({"extra": list(self.extra)} if self.extra else {}),
        }

    def validate(self, output: Any) -> list[tuple[RejectionReason, str]]:
        """Devolve a lista de violações. Vazia ⇒ saída aceitável.

        Devolver TODAS as violações (e não a primeira) é o que permite o
        `resubmit_with_errors` do §7.4 mandar erros objetivos numa única
        correção, em vez de arrancar um erro por rodada.
        """
        problems: list[tuple[RejectionReason, str]] = []
        if not isinstance(output, Mapping):
            return [(RejectionReason.MALFORMED, f"saída não é objeto: {type(output).__name__}")]
        for name in self.required:
            if name not in output:
                problems.append((RejectionReason.SCHEMA_INVALID, f"campo obrigatório ausente: {name}"))
        for name in sorted(output):
            if name in CONTROL_FIELDS:
                problems.append(
                    (
                        RejectionReason.CONTROL_FIELD,
                        f"campo de controle no payload: {name} (não altera orçamento/"
                        "ferramentas/permissões/destino a partir do worker)",
                    )
                )
            elif name not in self.declared and self.strict:
                problems.append(
                    (RejectionReason.SCHEMA_INVALID, f"campo inesperado em schema fechado: {name}")
                )
        for name, expected in self.types.items():
            if name in output and not isinstance(output[name], expected):
                problems.append(
                    (
                        RejectionReason.SCHEMA_INVALID,
                        f"tipo inválido em {name}: {type(output[name]).__name__}",
                    )
                )
        for name in READING_LIST_FIELDS:
            if name in output:
                detail = _invalid_reading_list(name, output[name])
                if detail is not None:
                    problems.append((RejectionReason.SCHEMA_INVALID, detail))
        return problems


DEFAULT_SCHEMA = ResultSchema()


@dataclass(frozen=True)
class Acceptance:
    """Veredito sobre um envelope de resultado."""

    accepted: bool
    output: dict[str, Any] | None = None
    reason: RejectionReason | None = None
    detail: str = ""
    #: `True` quando o MESMO resultado (mesmo `execution_id` + mesmo hash) já
    #: havia sido aceito: aceitar de novo é no-op, não regravação (§10.4.4).
    duplicate: bool = False
    #: Envelope validado (identidades + proveniência). `None` em recusa.
    result: E.ResultEnvelope | None = None

    @property
    def result_hash(self) -> str:
        return self.result.result_hash if self.result is not None else ""


def envelope_for_task(
    task: T.Task,
    *,
    execution_id: str = "",
    attempt_id: str = "",
    lease_id: str = "",
    context_hash: str = "",
    accumulated_state: Mapping[str, Any] | None = None,
    evidence: Sequence[Any] = (),
    reading_needs: Sequence[Mapping[str, Any]] = (),
    schema: "ResultSchema" = None,  # type: ignore[assignment]
    policy: Mapping[str, Any] | None = None,
    deadline: str | None = None,
) -> E.TaskEnvelope:
    """`Task` + estado acumulado → envelope de tarefa do §10.4.4.

    É o ÚNICO construtor de envelope de tarefa do runtime: o despacho real
    (`run`) e a sonda (`doctor --probe-agent`, via adaptador) usam o mesmo
    formato, então validar um resultado de sonda e validar um resultado de
    produção é literalmente o mesmo código.
    """
    objective = dict(task.objective or {})
    needs = list(reading_needs) or [
        n for n in objective.get("reading_needs", []) if isinstance(n, Mapping)
    ]
    return E.TaskEnvelope(
        task_id=task.task_id,
        objective_id=str(objective.get("objective_id") or objective.get("id") or ""),
        input_revision=task.input_versions_hash,
        context_hash=context_hash,
        objective=objective,
        accumulated_state=dict(accumulated_state or {}),
        reading_needs=tuple(dict(n) for n in needs),
        evidence=tuple(evidence),
        result_schema=(schema or DEFAULT_SCHEMA).to_dict(),
        budget=dict(task.budget or {}),
        deadline=deadline,
        lease_id=lease_id,
        attempt_id=attempt_id,
        execution_id=execution_id,
        vendor={"policy": dict(policy or {})},
    )


def _input_revision_matcher(task: T.Task) -> Callable[[Any], bool]:
    """Compara a revisão declarada com o snapshot da tarefa por HASH ou valor.

    Um adaptador legado devolve `input_versions` (o dicionário); o envelope de
    protocolo devolve `input_revision` (o hash). Os dois casam contra o MESMO
    `input_versions_hash` — sem isso, "revisão errada é rejeitada" dependeria
    do formato do adaptador.
    """

    def matches(declared: Any) -> bool:
        if str(declared) == task.input_versions_hash:
            return True
        return T.input_versions_hash(declared) == task.input_versions_hash

    return matches


def accept_result(
    store: T.TaskStore,
    task: T.Task,
    execution_id: str,
    envelope: Any,
    schema: ResultSchema = DEFAULT_SCHEMA,
    *,
    task_envelope: E.TaskEnvelope | None = None,
    binding_id: str | None = None,
    lease_valid: bool | None = None,
    bindings: Any = None,
) -> Acceptance:
    """ÚNICA porta de aceitação de resultado do runtime (F07 + spec §10.4.4).

    Ordem das checagens — identidade e autorização ANTES de schema, de
    propósito: resultado de execução desconhecida não merece nem ser lido.

    1. `execution_id` precisa existir em `attempts` E pertencer a ESTA tarefa.
       `attempts.execution_id` só é gravado por `start_attempt`, com o valor
       que o adaptador devolveu — um id inventado pelo worker nunca casa.
    2. Tentativa cancelada e lease expirado/invalidado recusam ANTES de ler o
       conteúdo (spec §10.4.4/§10.4.6: invalidação de lease impede resultado
       tardio de alterar o estado, mesmo sem cancelamento físico).
    3. `envelopes.validate_result` checa protocolo, identidades,
       `input_revision`, `context_hash` e `binding_id` — implementação única,
       compartilhada com a sonda do `probe`.
    4. Resultado idêntico já aceito ⇒ no-op idempotente (`duplicate=True`).
    5. A saída (`claims`) passa pelo schema fechado (§13.1).
    """
    attempt = store.attempt_for_execution(execution_id)
    if attempt is None:
        return Acceptance(
            False,
            reason=RejectionReason.UNKNOWN_EXECUTION,
            detail=f"execução {execution_id!r} não foi emitida por esta integração",
        )
    if attempt.task_id != task.task_id:
        return Acceptance(
            False,
            reason=RejectionReason.TASK_MISMATCH,
            detail=f"execução {execution_id!r} pertence à tarefa {attempt.task_id!r}",
        )

    env = task_envelope or envelope_for_task(
        task,
        execution_id=execution_id,
        attempt_id=str(attempt.attempt_no),
        lease_id=attempt.lease_id,
        schema=schema,
    )
    if env.execution_id and env.execution_id != execution_id:
        return Acceptance(
            False,
            reason=RejectionReason.EXECUTION_MISMATCH,
            detail=(
                f"envelope de tarefa aponta execução {env.execution_id!r}; "
                f"resultado chegou por {execution_id!r}"
            ),
        )
    env = env.with_execution_id(execution_id)

    expected_binding = binding_id if binding_id is not None else (attempt.binding_id or None)
    if lease_valid is None:
        lease_valid = _lease_still_valid(store, task, attempt, bindings)

    try:
        validated = E.validate_result(
            envelope,
            env,
            binding_id=expected_binding,
            lease_valid=bool(lease_valid),
            attempt_cancelled=attempt.outcome is T.AttemptOutcome.CANCELLED,
            input_revision_matches=_input_revision_matcher(task),
        )
    except E.EnvelopeError as exc:
        return Acceptance(
            False,
            reason=ENVELOPE_REJECTIONS.get(exc.reason, RejectionReason.MALFORMED),
            detail=exc.detail,
        )

    if attempt.result_hash and attempt.result_hash == validated.result_hash:
        # Mesma execução, mesmo conteúdo: já foi aplicado. No-op.
        return Acceptance(
            True,
            output=dict(task.result or validated.output()),
            duplicate=True,
            result=validated,
            detail="resultado idêntico já aceito para esta execução",
        )

    output = validated.output()
    problems = schema.validate(output)
    if problems:
        reason = next(
            (r for r, _ in problems if r is RejectionReason.CONTROL_FIELD),
            problems[0][0],
        )
        return Acceptance(
            False, reason=reason, detail="; ".join(detail for _, detail in problems)
        )
    return Acceptance(True, output=dict(output), result=validated)


def _lease_still_valid(
    store: T.TaskStore, task: T.Task, attempt: T.Attempt, bindings: Any = None
) -> bool:
    """O lease da tentativa continua vigente?

    Duas fontes, ambas verificáveis: o lease da TAREFA em `runtime.db` (expira
    sozinho quando o worker morre) e, quando existe `BindingStore`, o lease do
    BINDING (invalidado por `agent connect --cancel-active`). Qualquer uma
    negando derruba o resultado tardio.
    """
    if attempt.lease_id:
        current = store.lease_of(task.task_id)
        if current is not None and current.lease_id != attempt.lease_id:
            return False
        if current is not None and current.expired(T.utc_now()):
            return False
        checker = getattr(bindings, "lease_valid", None)
        if callable(checker) and not checker(attempt.lease_id):
            return False
    return True


#: Mapeia recusa → classe de erro do §7.4. Recusa de identidade (execução
#: desconhecida, tarefa/input divergente) NÃO é `schema_invalid`: repetir o
#: envio não corrige divergência de revisão. Vira `contradictory`, cuja
#: política é releitura focal — e cujo orçamento se esgota rápido.
REJECTION_TO_ERROR_CLASS: Mapping[RejectionReason, R.ErrorClass] = {
    RejectionReason.UNKNOWN_EXECUTION: R.ErrorClass.CONTRADICTORY,
    RejectionReason.EXECUTION_MISMATCH: R.ErrorClass.CONTRADICTORY,
    RejectionReason.TASK_MISMATCH: R.ErrorClass.CONTRADICTORY,
    RejectionReason.INPUT_DIVERGENCE: R.ErrorClass.CONTRADICTORY,
    RejectionReason.MALFORMED: R.ErrorClass.SCHEMA_INVALID,
    RejectionReason.SCHEMA_INVALID: R.ErrorClass.SCHEMA_INVALID,
    RejectionReason.CONTROL_FIELD: R.ErrorClass.SCHEMA_INVALID,
    # spec §10.4.4 — recusas de protocolo/autorização. Nenhuma delas se
    # corrige reenviando o MESMO envelope: divergência de protocolo, binding,
    # contexto ou identidade é contradição; lease invalidado e tentativa
    # cancelada também (o dono do trabalho mudou, não o formato da resposta).
    RejectionReason.PROTOCOL_DIVERGENCE: R.ErrorClass.CONTRADICTORY,
    RejectionReason.ATTEMPT_MISMATCH: R.ErrorClass.CONTRADICTORY,
    RejectionReason.OBJECTIVE_MISMATCH: R.ErrorClass.CONTRADICTORY,
    RejectionReason.CONTEXT_INVALID: R.ErrorClass.CONTRADICTORY,
    RejectionReason.BINDING_DIVERGENCE: R.ErrorClass.CONTRADICTORY,
    RejectionReason.LEASE_INVALID: R.ErrorClass.CONTRADICTORY,
    RejectionReason.ATTEMPT_CANCELLED: R.ErrorClass.CONTRADICTORY,
    # Texto não parseável É corrigível com erros objetivos na reenvio.
    RejectionReason.UNPARSEABLE: R.ErrorClass.SCHEMA_INVALID,
}


# --------------------------------------------------------------------------
# Diagnóstico e relatório
# --------------------------------------------------------------------------


@dataclass
class Diagnostic:
    """§7.2 — "mensagem curta com causa, impacto, correção tentada e decisão necessária"."""

    task_id: str
    cause: str
    impact: str
    attempted_fix: str
    decision_required: bool = False
    detail: str = ""

    def message(self) -> str:
        return (
            f"[{self.task_id}] causa: {self.cause} | impacto: {self.impact} | "
            f"correção tentada: {self.attempted_fix} | "
            f"decisão necessária: {'sim' if self.decision_required else 'não'}"
        )

    @classmethod
    def from_decision(cls, decision: R.Decision, detail: str = "") -> "Diagnostic":
        return cls(
            task_id=decision.task_id,
            cause=decision.cause,
            impact=decision.impact,
            attempted_fix=decision.attempted_fix,
            decision_required=decision.decision_required,
            detail=detail,
        )


@dataclass
class RunReport:
    """Resultado do laço. Números por tarefa, não por tentativa interna."""

    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
    retried: int = 0
    blocked: int = 0
    failed: int = 0
    reused: int = 0
    cycles: int = 0
    done_tasks: list[str] = field(default_factory=list)
    blocked_tasks: list[str] = field(default_factory=list)
    rejections: list[tuple[str, str, str]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "submitted": self.submitted,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "retried": self.retried,
            "blocked": self.blocked,
            "failed": self.failed,
            "reused": self.reused,
            "cycles": self.cycles,
        }


# --------------------------------------------------------------------------
# Planejamento
# --------------------------------------------------------------------------


def _as_dict(objective: Any) -> dict[str, Any]:
    """Aceita `InvestigationObjective` (via `to_dict`) ou dict puro.

    Import de `analysis.investigation` é evitado de propósito: o coordenador
    depende do FORMATO do objetivo, não da classe. Assim o mesmo laço serve a
    objetivos de descoberta, correlação e publicação.
    """
    if isinstance(objective, Mapping):
        return dict(objective)
    to_dict = getattr(objective, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise TypeError(f"objetivo não serializável: {type(objective).__name__}")


def plan_from_objectives(
    store: T.TaskStore,
    objectives: Iterable[Any],
    *,
    snapshot_id: str,
    source_version_ids: Sequence[str] = (),
    kind: T.TaskKind | str = T.TaskKind.INVESTIGATION,
    budget: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    depends_on: Mapping[str, Sequence[str]] | None = None,
    now: str | None = None,
) -> list[T.Task]:
    """Persiste objetivos de `analysis.investigation` como tarefas do runtime.

    `input_versions = {snapshot_id, source_version_ids}` — é a versão das
    entradas que o resultado vai descrever, e é o que `recovery.resume` compara
    por hash depois. Sem isso, "reaproveitar resultado" viraria aposta.

    `depends_on` é expresso por `objective_id` (o vocabulário de quem planeja) e
    traduzido aqui para `task_id`. Objetivo já planejado sobre o mesmo snapshot
    devolve a MESMA tarefa (`Task.reused`), sem duplicar trabalho (§7.2).
    """
    moment = now or T.utc_now()
    inputs = {
        "snapshot_id": snapshot_id,
        "source_version_ids": sorted(str(s) for s in source_version_ids),
    }
    pending = [_as_dict(o) for o in objectives]
    by_objective: dict[str, str] = {}
    created: list[T.Task] = []
    deps_map = {k: tuple(v) for k, v in (depends_on or {}).items()}

    # Duas passadas: primeiro tarefas sem dependência pendente, depois as que
    # dependem delas. Sem isso, uma dependência declarada antes de existir
    # levantaria UnknownTask por ordem de entrada, não por erro real.
    remaining = list(pending)
    guard = len(remaining) + 1
    while remaining and guard:
        guard -= 1
        deferred: list[dict[str, Any]] = []
        for objective in remaining:
            oid = str(objective.get("objective_id") or objective.get("id") or "")
            required = deps_map.get(oid, ())
            if any(dep not in by_objective for dep in required):
                deferred.append(objective)
                continue
            task = store.create_task(
                objective.get("task_kind", kind),
                objective,
                inputs,
                depends_on=[by_objective[dep] for dep in required],
                budget=budget,
                config=config,
                now=moment,
            )
            if oid:
                by_objective[oid] = task.task_id
            created.append(task)
        if len(deferred) == len(remaining):
            missing = sorted(
                {
                    dep
                    for o in deferred
                    for dep in deps_map.get(str(o.get("objective_id", "")), ())
                    if dep not in by_objective
                }
            )
            raise CoordinatorError(
                f"dependências não resolvíveis entre os objetivos informados: {missing}"
            )
        remaining = deferred
    store.refresh_states(moment)
    return created


# --------------------------------------------------------------------------
# Continuação de investigação parcial (Onda10-C, achado #3 da 2ª auditoria)
# --------------------------------------------------------------------------


def plan_continuations(
    store: T.TaskStore,
    integration_outcomes: Iterable[Mapping[str, Any]],
    *,
    input_versions: Mapping[str, Any],
    budget: Mapping[str, Any] | None = None,
    max_rounds: int | None = None,
    engine_capabilities: Mapping[str, Any] | None = None,
    progress: Mapping[str, Any] | None = None,
    chain_guard: bool = True,
    now: str | None = None,
) -> dict[str, list[Any]]:
    """Cria tarefas de continuação para objetivos `partial` com pendência concreta.

    `engine_capabilities` (Onda11-T2a, achado BLOQUEANTE #2 da 3ª auditoria,
    parte runtime): o `capabilities()` da engine que EXECUTARIA a
    continuação, quando quem planeja já o tem em mãos (ex.:
    `executor.capabilities()` do laço de `run()`). Quando informado e a
    chave `deepening` é `False` (ou ausente — tratado como sem capacidade),
    NENHUMA tarefa é criada: engine sem capacidade de aprofundamento (ex.:
    `LocalThreadExecutor`, que só roda callables determinísticos já
    registrados — nunca lê código novo) consumiria uma rodada de
    continuação sem poder investigar coisa alguma, e a rodada é justamente o
    recurso escasso que a trava de `round_no > max_rounds` protege. A recusa
    acontece ANTES de qualquer leitura de `round_no`/`task_round` — nenhuma
    tarefa nasce, nenhum contador de rodada avança
    (`T.continuation_rounds` fica inalterado, porque nada foi inserido em
    `tasks`). `engine_capabilities=None` (default) preserva o comportamento
    anterior a esta mudança — quem chama sem informar a capacidade da engine
    continua podendo planejar continuações (compatibilidade com chamadores
    existentes, ex. `wk.cli._plan_continuation_round`).

    `integration_outcomes` é o resumo por objetivo — o formato serializado do
    `IntegrationReport` de `knowledge.integrate`, recebido aqui como `dict`
    (nunca a classe: este módulo não importa `knowledge`). Cada item esperado:

        {"objective_id": ..., "task_id": ..., "state": "partial"|"complete"|"blocked",
         "unmet_needs": [{"kind":..., "target":..., "motivo":...}, ...]}

    Regra de geração (§13.1/§7.2, achado #3):
      * `complete` — objetivo fechado, nada a continuar.
      * `blocked`  — já é estado terminal; continuação não destrava bloqueio.
      * `partial`  — só gera tarefa quando existe ao menos uma `unmet_needs`
        com `target` resolvível (string não vazia). Objetivo `partial` sem
        need concreta é recusado (nada para investigar de novo) em vez de
        criar uma tarefa que repetiria o mesmo impasse.

    `round_no` de cada continuação vem de `T.task_round(tarefa_mãe) + 1`: é
    por isso que chamar `plan_continuations` de novo com o MESMO outcome
    NUNCA duplica — a tarefa-mãe não mudou, então o round pedido é o mesmo, e
    `effect_identity` faz `create_task` devolver a tarefa já existente.

    Devolve `{"criadas": [task_id, ...], "recusadas": [{"objective_id",
    "motivo"}, ...]}` — nunca levanta por objetivo individual malformado ou
    por teto de rodada excedido: essas são recusas registradas, não erros do
    laço (§7.4 "mecanismo interno, nunca laço infinito").
    """
    if engine_capabilities is not None and not dict(engine_capabilities).get("deepening", False):
        return {
            "criadas": [],
            "recusadas": [
                {
                    "motivo": (
                        "engine sem capacidade de aprofundamento (deepening=False); "
                        "continuações exigem agente com capacidade de "
                        "aprofundamento (deepening); conecte um agente com essa "
                        "capacidade (`wk agent list` mostra as capacidades)"
                    )
                }
            ],
        }

    criadas: list[str] = []
    recusadas: list[dict[str, str]] = []
    for raw_outcome in integration_outcomes:
        outcome = dict(raw_outcome) if isinstance(raw_outcome, Mapping) else {}
        objective_id = str(outcome.get("objective_id") or "")
        state = str(outcome.get("state") or "").strip().lower()

        if state != "partial":
            continue  # complete/blocked: nunca geram continuação

        parent_task_id = str(outcome.get("task_id") or "")
        if not objective_id or not parent_task_id:
            recusadas.append(
                {
                    "objective_id": objective_id,
                    "motivo": "outcome sem objective_id/task_id da tarefa que o produziu",
                }
            )
            continue

        raw_needs = outcome.get("unmet_needs")
        if raw_needs is None:
            raw_needs = outcome.get("needs") or []
        needs = [
            dict(n)
            for n in raw_needs
            if isinstance(n, Mapping) and str(n.get("target") or "").strip()
        ]
        if not needs:
            recusadas.append(
                {
                    "objective_id": objective_id,
                    "motivo": "partial sem nenhuma unmet_needs com target resolvível",
                }
            )
            continue

        try:
            parent = store.get(parent_task_id)
        except T.UnknownTask:
            recusadas.append(
                {
                    "objective_id": objective_id,
                    "motivo": f"tarefa {parent_task_id!r} não existe em runtime.db",
                }
            )
            continue

        round_no = T.task_round(parent) + 1
        limit = (
            T.get_max_continuation_rounds(store) if max_rounds is None else max(0, int(max_rounds))
        )
        if round_no > limit:
            recusadas.append(
                {
                    "objective_id": objective_id,
                    "motivo": f"round {round_no} excede o máximo de {limit} rodadas de continuação",
                    "stop_reason": S.STOP_BUDGET_EXHAUSTED,
                }
            )
            continue

        # -- teto TOTAL da cadeia, orçamento e progresso (§7.3) -------------
        # Estas guardas são o que separa "mais uma rodada" de "mais uma
        # invocação com crédito novo". `chain_guard=False` existe só para quem
        # planeja fora de uma cadeia (inspeção/teste de unidade da criação).
        if chain_guard:
            recusa = _chain_refusal(
                store,
                objective_id,
                needs,
                declared_max_rounds=max_rounds,
                progress=progress,
                now=now,
            )
            if recusa is not None:
                recusadas.append(recusa)
                continue

        # Onda11-T2a: `parent_result` vem do resultado ACEITO da tarefa-mãe
        # (já carregado acima), a menos que o outcome traga um resumo
        # próprio — `contract_state`/`capability_context` só existem quando
        # quem monta o outcome (ex.: `knowledge.integrate`) os inclui;
        # ausência é `None`, e `create_continuation_tasks` trata `None` como
        # "nada a acrescentar" (nunca falha por ausência).
        task_ids = T.create_continuation_tasks(
            store,
            parent_task_id=parent_task_id,
            objective_id=objective_id,
            needs=needs,
            input_versions=input_versions,
            budget=budget,
            round_no=round_no,
            max_rounds=max_rounds,
            contract_state=outcome.get("contract_state"),
            parent_result=outcome.get("parent_result", parent.result),
            capability_context=outcome.get("capability_context"),
            now=now,
        )
        if task_ids:
            criadas.extend(task_ids)
            if chain_guard:
                # A rodada só CONTA aqui: tarefa realmente criada. Recusa de
                # executor e tarefa-base não passam por este ponto (§7.3).
                store.record_chain_round(
                    objective_id,
                    package_hash=S.needs_package_hash(needs),
                    progress=_progress_dict(progress, objective_id),
                    counts_round=True,
                    now=now,
                )
                store.set_chain_stop(objective_id, "", now=now)
        else:  # defensivo: teto/needs já checados acima, mas nunca confiar 2x
            recusadas.append(
                {"objective_id": objective_id, "motivo": "create_continuation_tasks recusou"}
            )
    return {"criadas": criadas, "recusadas": recusadas}


def _progress_dict(progress: Any, objective_id: str) -> dict[str, Any]:
    """Progresso da rodada anterior DESTE objetivo, em forma de dicionário.

    Aceita `S.Progress`, o dicionário dele, um mapa `objective_id -> progresso`
    ou `None` (nada informado). Um `bool` também é aceito porque quem só sabe
    "houve/não houve progresso" (um chamador antigo) ainda precisa conseguir
    dizê-lo sem montar a estrutura inteira.
    """
    if progress is None:
        return {}
    if isinstance(progress, S.Progress):
        return progress.to_dict()
    if isinstance(progress, bool):
        return {"has_progress": progress}
    if isinstance(progress, Mapping):
        if objective_id and objective_id in progress:
            return _progress_dict(progress[objective_id], "")
        if "has_progress" in progress or "obligations_closed" in progress:
            return dict(progress)
    return {}


def _obligation_diagnostic(needs: Sequence[Mapping[str, Any]]) -> str:
    """Obrigação e alvo concretos da rodada que não avançou (§7.3).

    "Nenhum progresso" sem dizer QUAL obrigação e QUAL alvo é o diagnóstico
    inútil que o §7.3 proíbe: o operador precisa saber onde a cadeia parou
    para decidir (fornecer informação, escolher identidade, ampliar orçamento).
    """
    for need in needs:
        if not isinstance(need, Mapping):
            continue
        alvo = str(need.get("target") or need.get("alvo") or "").strip()
        if not alvo:
            continue
        tipo = str(need.get("kind") or need.get("tipo") or "leitura").strip()
        motivo = str(need.get("motivo") or need.get("reason") or "").strip()
        detalhe = f"obrigação {tipo!r} sobre {alvo!r}"
        return f"{detalhe} ({motivo})" if motivo else detalhe
    return "nenhuma obrigação com alvo concreto"


def _chain_refusal(
    store: T.TaskStore,
    objective_id: str,
    needs: Sequence[Mapping[str, Any]],
    *,
    declared_max_rounds: int | None,
    progress: Any,
    now: str | None,
) -> dict[str, Any] | None:
    """Recusa de cadeia (ou `None` para "pode continuar").

    Três condições materiais do §7.3, nesta ordem:

    1. **teto total da cadeia** — `rounds_used` é persistido e não zera por
       nova invocação; `--max-rounds N` é o teto DAQUELA cadeia. Motivo
       canônico `budget_exhausted`.
    2. **orçamento consumido** — teto declarado em `set_chain_limits` já
       atingido. Mesmo motivo canônico, detalhe diferente (consumo x teto).
    3. **ausência de progresso** — a rodada anterior não encerrou obrigação,
       não aceitou evidência nova, não resolveu conflito e não PREENCHEU campo
       do contrato (`Progress.has_progress`), OU o pacote de necessidades seria
       byte-a-byte o mesmo já despachado. Motivo canônico `no_progress`,
       sempre com a obrigação e o alvo no diagnóstico. A segunda condição (o
       pacote repetido) é independente da primeira: uma rodada COM progresso
       que devolve o mesmo pacote continua parando aqui.

    Toda recusa PERSISTE o motivo (`set_chain_stop`): a próxima invocação —
    inclusive depois de reiniciar o processo — lê o mesmo diagnóstico.
    """
    status = store.chain_status(objective_id)
    limit = status["max_rounds"] if declared_max_rounds is None else max(0, int(declared_max_rounds))
    if status["rounds_used"] >= limit:
        store.set_chain_stop(objective_id, S.STOP_BUDGET_EXHAUSTED, now=now)
        return {
            "objective_id": objective_id,
            "motivo": (
                f"cadeia excede o teto total: {status['rounds_used']} de {limit} rodadas já "
                "consumidas nesta cadeia (o teto é da CADEIA, não da invocação); "
                f"amplie com set_chain_limits(objective_id={objective_id!r}, max_rounds=N)"
            ),
            "stop_reason": S.STOP_BUDGET_EXHAUSTED,
            "chain": status,
        }

    esgotado, detalhe = S.budget_exhausted(status["budget"], status["consumed"])
    if esgotado:
        store.set_chain_stop(objective_id, S.STOP_BUDGET_EXHAUSTED, now=now)
        return {
            "objective_id": objective_id,
            "motivo": f"orçamento da cadeia esgotado ({detalhe}); estado preservado",
            "stop_reason": S.STOP_BUDGET_EXHAUSTED,
            "chain": status,
        }

    anterior = _progress_dict(progress, objective_id) or status["last_progress"]
    ja_rodou = bool(status["rounds_used"]) or bool(status["last_package_hash"])
    if ja_rodou and anterior and not anterior.get("has_progress", False):
        store.set_chain_stop(objective_id, S.STOP_NO_PROGRESS, now=now)
        return {
            "objective_id": objective_id,
            "motivo": (
                "rodada anterior sem progresso semântico (nenhuma obrigação encerrada, "
                "evidência nova aceita, conflito resolvido ou campo do contrato "
                "preenchido): "
                + _obligation_diagnostic(needs)
            ),
            "stop_reason": S.STOP_NO_PROGRESS,
            "chain": status,
        }

    package_hash = S.needs_package_hash(needs)
    if package_hash and package_hash == status["last_package_hash"]:
        store.set_chain_stop(objective_id, S.STOP_NO_PROGRESS, now=now)
        return {
            "objective_id": objective_id,
            "motivo": (
                "pacote de necessidades idêntico ao já despachado "
                f"({package_hash[:12]}): repetir o mesmo pacote é proibido — "
                + _obligation_diagnostic(needs)
            ),
            "stop_reason": S.STOP_NO_PROGRESS,
            "chain": status,
        }
    return None


# --------------------------------------------------------------------------
# Laço de execução
# --------------------------------------------------------------------------

#: Vocabulário de `executor.status()["state"]`. Estado fora destes conjuntos é
#: tratado como "ainda executando" — nunca como sucesso.
RUNNING_STATES = frozenset({"queued", "pending", "running", "in_progress", "started"})
SUCCESS_STATES = frozenset({"succeeded", "success", "completed", "complete", "done"})
FAILURE_STATES = frozenset({"failed", "error", "timeout", "cancelled", "canceled", "aborted"})


def _build_package(
    context_builder: Callable[..., Any] | None,
    objective: Mapping[str, Any],
    budget: Mapping[str, Any],
    resolver: Any,
) -> tuple[Any, BaseException | None]:
    """Monta o pacote pelo vizinho `context.build_package`, se existir.

    Import tardio: sem `runtime/context.py` o laço segue com referências vazias
    (e o diagnóstico registra), em vez de falhar no import.
    """
    builder = context_builder
    if builder is None:
        try:  # pragma: no cover - depende do vizinho
            from .context import build_package as builder  # type: ignore
        except Exception:
            return None, None
    try:
        return builder(dict(objective), budget, resolver), None
    except Exception as exc:  # BudgetExceeded ou falha de montagem
        return None, exc


def _references(package: Any) -> Any:
    """Referências enviadas ao executor: o PACOTE COMPLETO, quando montado.

    Achado ALTO nº4 (auditoria externa): mandar só `package.refs` deixa
    `ref_id`/`part_id` órfãos no worker — os trechos (`package.parts`) que o
    context builder montou nunca chegavam a `executor.submit`. `references`
    é opaco ao protocolo `AgentExecutor` (só o adapter concreto interpreta o
    formato — ver `executors/base.AgentExecutor.submit`), então o pacote
    inteiro (`objective_id` + `refs` + `parts` + limites, via
    `Package.to_json()`) viaja como o ÚNICO item de uma sequência — mantém a
    assinatura `Sequence[Any]` do protocolo sem reinterpretar o parâmetro.

    O teto de orçamento (F05) já foi aplicado pelo context builder
    (`context.build_package`/`_fit_to_budget`); esta função NÃO trunca nada
    de novo — só encaminha o que já veio pronto.

    Compatibilidade: sem pacote montado (tarefa sem contexto — vizinho
    ausente ou objetivo sem evidence_refs), devolve `()`, como antes.
    """
    if package is None:
        return ()
    to_json = getattr(package, "to_json", None)
    if callable(to_json):
        return (to_json(),)
    # Defensivo: objeto sem `to_json` (ex.: stub de teste) cai para o
    # comportamento anterior em vez de quebrar o despacho.
    return getattr(package, "refs", ())


def _classify_exception(exc: BaseException) -> R.ErrorClass:
    """Classe de erro de uma exceção da integração.

    `BudgetExceeded` é reconhecido pelo NOME da classe, não por `isinstance`:
    o vizinho pode não estar importável, e classificar errado transformaria
    "repartir objetivo" em "retentar o mesmo pacote grande demais".
    """
    declared = getattr(exc, "error_class", None)
    if declared is not None:
        try:
            return R.ErrorClass(declared)
        except ValueError:
            pass
    name = type(exc).__name__
    if name == "BudgetExceeded" or "Budget" in name:
        return R.ErrorClass.BUDGET
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return R.ErrorClass.TRANSIENT
    return R.ErrorClass.TRANSIENT


def _check_dispatch(executor: Any) -> dict[str, Any]:
    """§7.3 — informa o bloqueio UMA vez quando não há despacho real."""
    for name in ("submit", "status", "result"):
        if not callable(getattr(executor, name, None)):
            raise DispatchUnavailable(
                f"executor {type(executor).__name__} não implementa {name}(): "
                "sem despacho real, o fluxo não volta a copiar prompts"
            )
    caps_fn = getattr(executor, "capabilities", None)
    caps = dict(caps_fn()) if callable(caps_fn) else {}
    if caps.get("dispatch") is False:
        # §7.3, "executor indisponível": preservar progresso E indicar correção
        # CONCRETA. A engine já sabe por que não despacha (binário fora do
        # PATH, host não autenticado); descartar esse `reason` obrigava o
        # operador a adivinhar qual das causas era a dele.
        motivo = str(caps.get("reason") or "").strip()
        raise DispatchUnavailable(
            "engine declarou ausência de despacho em capabilities()"
            + (f": {motivo}" if motivo else "")
            + "; configure a integração antes de executar análise"
        )
    return caps


def _open_binding_lease(
    bindings: Any, lease: T.Lease, binding: A.AgentBinding, task: T.Task
) -> None:
    """Registra o lease da tarefa no `BindingStore`, quando existe um.

    Sem `BindingStore` (chamada legada da CLI) não há o que registrar: a
    validade do lease continua sendo decidida pelo `runtime.db`.
    """
    opener = getattr(bindings, "open_lease", None)
    if not callable(opener):
        return
    repo = str((task.input_versions or {}).get("repo_id") or "") or None
    opener(
        lease.lease_id,
        binding_id=binding.binding_id,
        repo=repo,
        task_id=task.task_id,
    )


def _close_binding_lease(bindings: Any, lease_id: str, reason: str) -> None:
    closer = getattr(bindings, "close_lease", None)
    if callable(closer):
        closer(lease_id, reason=reason)


def bind_executor(executor: Any) -> tuple[Any, A.AgentBinding, dict[str, Any]]:
    """Executor já construído → (adaptador, binding, capacidades).

    Ponte de compatibilidade do §10.4: `run(store, executor=...)` continua
    existindo, mas o laço NÃO fala mais com executores — só com adaptadores.
    Assim `wk.cli._dispatch_objectives` e os testes atuais seguem funcionando
    sobre a MESMA rota do despacho por binding.
    """
    caps = _check_dispatch(executor)
    from .executors.agent_adapters import LegacyExecutorAdapter

    adapter = LegacyExecutorAdapter(executor)
    try:
        binding = adapter.bind()
    except A.HandshakeFailed as exc:
        raise DispatchUnavailable(str(exc)) from exc
    return adapter, binding, caps


def run(
    store: T.TaskStore,
    executor: Any = None,
    context_builder: Callable[..., Any] | None = None,
    *,
    adapter: Any = None,
    binding: A.AgentBinding | None = None,
    bindings: Any = None,
    max_concurrency: int = 2,
    policy: Mapping[str, Any] | None = None,
    schema: ResultSchema | Mapping[Any, ResultSchema] = DEFAULT_SCHEMA,
    result_extra_keys: Iterable[str] = (),
    owner: str = "coordinator",
    lease_ttl_seconds: float = 300.0,
    resolver: Any = None,
    max_cycles: int = 10_000,
    unknown_state_limit: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.0,
    now_fn: Callable[[], str] = T.utc_now,
) -> RunReport:
    """Executa o grafo de tarefas até esgotar o que é executável.

    Determinismo: a cada ciclo, `ready_tasks()` já vem ordenado por
    `created_at, task_id`, e o despacho respeita essa ordem até o limite de
    concorrência. Duas execuções sobre o mesmo banco despacham igual.

    Isolamento de falha: cada tarefa tem seu lease e sua tentativa. Um worker
    que morre solta apenas a SUA tarefa (lease expirado) — nenhum ponto deste
    laço reinicia o grafo inteiro por causa de uma falha isolada.

    Nada aqui grava fato: o resultado aceito fica em `tasks.result_json`
    (§7.2, grafos separados).
    """
    if executor is not None:
        adapter, binding, caps = bind_executor(executor)
    elif adapter is None or binding is None:
        raise DispatchUnavailable(
            "run() exige um executor OU (adapter, binding) conectados; "
            "sem binding não há despacho — e o núcleo não escolhe um agente sozinho"
        )
    else:
        if not binding.connected:
            raise DispatchUnavailable(
                f"binding {binding.binding_id!r} de {binding.agent_id!r} não está conectado; "
                "execute agent connect antes de despachar"
            )
        caps = binding.capabilities.to_executor_capabilities()
    provenance = E.Provenance.from_binding(binding).to_dict()
    # `capabilities()` declara concorrência real (§7.3). Dois nomes são aceitos
    # porque o vocabulário do adapter é dele, não deste laço; o coordenador
    # nunca despacha ACIMA do que a engine declarou suportar.
    cap_conc = caps.get("max_concurrency", caps.get("concurrency"))
    if isinstance(cap_conc, int) and not isinstance(cap_conc, bool) and cap_conc > 0:
        max_concurrency = min(max_concurrency, cap_conc)
    max_concurrency = max(1, int(max_concurrency))

    policies = R.load_policies(store)
    report = RunReport()
    inflight: dict[str, tuple[T.Lease, str]] = {}
    #: envelope de tarefa efetivamente submetido, por tarefa — é ele (e não o
    #: payload do worker) que decide identidade, revisão e contexto no aceite.
    submitted_envelopes: dict[str, E.TaskEnvelope] = {}
    #: quantas vezes seguidas cada execução respondeu estado não observável
    unobservable: dict[str, int] = {}

    # As chaves extras são do OPERADOR (perfil de análise) e valem para TODO
    # schema desta invocação — inclusive os schemas por `TaskKind`. Derivar
    # aqui, uma vez, garante que despacho e aceite usem o MESMO vocabulário:
    # anunciar um schema ao worker e validar por outro é justamente como a
    # chave nova sumiria sem diagnóstico.
    #
    # A derivação é EAGER de propósito: chave extra inválida (campo de
    # controle, ou nome que o núcleo já declara) estoura `ValueError` ANTES de
    # qualquer despacho. Descobrir a configuração errada tarefa a tarefa, no
    # meio do laço, viraria falha de tarefa — e o operador leria "montagem de
    # envelope falhou" no lugar de "essa chave é de controle".
    #
    # Compatibilidade: `with_extra(())` devolve `self`, então uma chamada sem
    # `result_extra_keys` segue usando exatamente os objetos recebidos.
    extra_keys = tuple(str(k) for k in (result_extra_keys or ()))
    if isinstance(schema, ResultSchema):
        fallback_schema = schema.with_extra(extra_keys)
        schemas_por_kind: dict[Any, ResultSchema] | None = None
    else:
        fallback_schema = DEFAULT_SCHEMA.with_extra(extra_keys)
        schemas_por_kind = {k: v.with_extra(extra_keys) for k, v in dict(schema).items()}

    def schema_for(task: T.Task) -> ResultSchema:
        if schemas_por_kind is None:
            return fallback_schema
        return schemas_por_kind.get(
            task.kind, schemas_por_kind.get(task.kind.value, fallback_schema)
        )

    def close_with_failure(
        task: T.Task,
        lease: T.Lease,
        execution_id: str | None,
        error_class: R.ErrorClass,
        detail: str,
        *,
        outcome: T.AttemptOutcome,
        termination_prefix: str,
        idempotent: bool = True,
    ) -> None:
        """Fecha a tentativa e aplica a política do §7.4 (retentar ou bloquear).

        Quando a falha aconteceu ANTES de existir execução (contexto/submit),
        abre-se uma tentativa sem `execution_id` para que a falha conte: sem
        ela, `attempt_count` não subiria e o erro repetido nunca esgotaria a
        política — o laço ilimitado que o §7.4 proíbe.
        """
        err_hash = R.error_hash(error_class, detail)
        moment = now_fn()
        _close_binding_lease(bindings, lease.lease_id, f"{termination_prefix}:{error_class.value}")
        if execution_id is None:
            store.start_attempt(task.task_id, lease.lease_id, None, now=moment)
        store.record_result(
            task.task_id,
            lease.lease_id,
            execution_id,
            state=T.TaskState.PENDING,
            outcome=outcome,
            result=None,
            termination_reason=f"{termination_prefix}:{error_class.value}",
            error_class=error_class.value,
            error_detail=detail,
            error_hash=err_hash,
            now=moment,
        )
        decision = R.decide(
            store,
            task.task_id,
            error_class,
            detail,
            policies=policies,
            idempotent=idempotent,
            now=moment,
        )
        report.diagnostics.append(Diagnostic.from_decision(decision, detail))
        if decision.blocked or decision.action is R.Action.KEEP_CANDIDATE:
            # `record_result` já fechou a tentativa e soltou o lease; aqui só
            # falta o estado terminal, sem apagar o histórico de tentativas.
            with store.immediate() as conn:
                conn.execute(
                    "UPDATE tasks SET state=?, termination_reason=?, updated_at=? WHERE task_id=?",
                    (
                        T.TaskState.BLOCKED.value,
                        f"blocked:{error_class.value}",
                        moment,
                        task.task_id,
                    ),
                )
            report.blocked += 1
            report.blocked_tasks.append(task.task_id)
        else:
            report.retried += 1

    while report.cycles < max_cycles:
        report.cycles += 1
        moment = now_fn()
        progressed = False

        # 1. leases expirados voltam ao pool (worker morto) -----------------
        for released in store.release_expired_leases(moment):
            if released in inflight:
                inflight.pop(released, None)
                submitted_envelopes.pop(released, None)
                progressed = True

        # 2. despacho de tarefas prontas -----------------------------------
        if len(inflight) < max_concurrency:
            for task in store.ready_tasks(now=moment):
                if len(inflight) >= max_concurrency:
                    break
                if task.task_id in inflight:
                    continue
                cached = store.reuse_result(task.effect_identity)
                if cached is not None and task.state is not T.TaskState.DONE:
                    # Defensivo: `effect_identity` é UNIQUE, então este caminho
                    # só existe para o caso de a mesma tarefa já ter resultado.
                    report.reused += 1
                    continue
                try:
                    lease = store.acquire_lease(
                        task.task_id, owner, ttl_seconds=lease_ttl_seconds, now=moment
                    )
                except T.LeaseHeld:
                    continue  # outro coordenador pegou; não é erro
                # §10.4.6: o lease da tarefa é TAMBÉM o lease do binding. Só
                # assim `agent connect --cancel-active` invalida trabalho em
                # voo — e `accept_result` reconhece o resultado tardio.
                _open_binding_lease(bindings, lease, binding, task)
                package, exc = _build_package(
                    context_builder, task.objective, task.budget, resolver
                )
                if exc is not None:
                    close_with_failure(
                        task,
                        lease,
                        None,
                        _classify_exception(exc),
                        f"montagem de contexto falhou: {exc}",
                        outcome=T.AttemptOutcome.FAILED,
                        termination_prefix="context",
                    )
                    progressed = True
                    continue
                references = _references(package)
                # §10.4.4 + §7.1: o envelope carrega objetivo, ESTADO
                # ACUMULADO, obrigações de leitura, evidência montada,
                # orçamento, lease e o hash do contexto efetivamente enviado.
                # `context_hash` é calculado sobre o que VAI no envelope, não
                # sobre uma promessa — é ele que o resultado precisa refletir.
                # B1: montar o envelope também pode falhar (estado acumulado
                # não serializável em `context_hash`, schema inválido). Sem a
                # blindagem, a exceção escapava de `run()` com o lease ABERTO:
                # a tarefa ficava presa até o TTL e o binding seguia ocupado.
                try:
                    envelope = envelope_for_task(
                        task,
                        lease_id=lease.lease_id,
                        attempt_id=str(task.attempt_count + 1),
                        context_hash=E.context_hash(
                            {"evidence": list(references), "state": task.result or {}}
                        ),
                        accumulated_state=dict(task.result or {}),
                        evidence=references,
                        schema=schema_for(task),
                        policy=dict(policy or {}),
                    )
                except Exception as exc:
                    close_with_failure(
                        task,
                        lease,
                        None,
                        _classify_exception(exc),
                        f"montagem do envelope falhou: {exc}",
                        outcome=T.AttemptOutcome.FAILED,
                        termination_prefix="envelope",
                    )
                    progressed = True
                    continue
                try:
                    execution_id = adapter.submit(binding, envelope)
                except Exception as exc:  # falha de despacho
                    close_with_failure(
                        task,
                        lease,
                        None,
                        _classify_exception(exc),
                        f"submit falhou: {exc}",
                        outcome=T.AttemptOutcome.FAILED,
                        termination_prefix="submit",
                    )
                    progressed = True
                    continue
                if not isinstance(execution_id, str) or not execution_id:
                    close_with_failure(
                        task,
                        lease,
                        None,
                        R.ErrorClass.CONTRADICTORY,
                        f"executor devolveu execution_id inválido: {execution_id!r}",
                        outcome=T.AttemptOutcome.REJECTED,
                        termination_prefix="submit",
                    )
                    progressed = True
                    continue
                try:
                    store.start_attempt(
                        task.task_id,
                        lease.lease_id,
                        execution_id,
                        binding_id=binding.binding_id,
                        provenance=provenance,
                        now=moment,
                    )
                except sqlite3.IntegrityError:
                    # `ux_attempts_exec` é UNIQUE: um executor que reemite um
                    # execution_id já usado não pode "herdar" a execução de
                    # outra tarefa. Rejeita esta submissão, não o grafo.
                    close_with_failure(
                        task,
                        lease,
                        None,
                        R.ErrorClass.CONTRADICTORY,
                        f"executor reemitiu execution_id já registrado: {execution_id!r}",
                        outcome=T.AttemptOutcome.REJECTED,
                        termination_prefix="submit",
                    )
                    progressed = True
                    continue
                inflight[task.task_id] = (lease, execution_id)
                submitted_envelopes[task.task_id] = envelope.with_execution_id(execution_id)
                report.submitted += 1
                progressed = True

        if not inflight:
            if not progressed:
                break
            continue

        # 3. coleta ---------------------------------------------------------
        for task_id in sorted(inflight):
            lease, execution_id = inflight[task_id]
            task = store.get(task_id)
            moment = now_fn()
            try:
                observed = adapter.poll(binding, execution_id)
            except Exception as exc:
                inflight.pop(task_id, None)
                submitted_envelopes.pop(task_id, None)
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    _classify_exception(exc),
                    f"poll falhou: {exc}",
                    outcome=T.AttemptOutcome.FAILED,
                    termination_prefix="status",
                )
                progressed = True
                continue
            state = str((observed or {}).get("state", "running")).lower()
            if state in RUNNING_STATES:
                unobservable.pop(execution_id, None)
                # Batimento observado renova o lease: worker vivo não é despejado.
                # B1: se o store falhar no batimento, a exceção escapava de
                # `run()` com o lease ABERTO — fecha-se a tentativa como falha
                # transitória em vez de deixar tarefa e binding presos.
                try:
                    store.heartbeat(lease.lease_id, ttl_seconds=lease_ttl_seconds, now=moment)
                except Exception as exc:
                    inflight.pop(task_id, None)
                    submitted_envelopes.pop(task_id, None)
                    unobservable.pop(execution_id, None)
                    close_with_failure(
                        task,
                        lease,
                        execution_id,
                        _classify_exception(exc),
                        f"renovação do lease falhou: {exc}",
                        outcome=T.AttemptOutcome.FAILED,
                        termination_prefix="heartbeat",
                    )
                    progressed = True
                continue
            if state not in SUCCESS_STATES and state not in FAILURE_STATES:
                # `unknown` (ou vocabulário não reconhecido) NUNCA vira sucesso.
                # Também não pode esperar para sempre: a engine que perdeu a
                # execução manteria a tarefa presa com heartbeat eterno.
                seen = unobservable.get(execution_id, 0) + 1
                unobservable[execution_id] = seen
                if seen <= unknown_state_limit:
                    try:
                        store.heartbeat(
                            lease.lease_id, ttl_seconds=lease_ttl_seconds, now=moment
                        )
                    except Exception as exc:  # B1: idem — lease nunca fica aberto
                        inflight.pop(task_id, None)
                        submitted_envelopes.pop(task_id, None)
                        unobservable.pop(execution_id, None)
                        close_with_failure(
                            task,
                            lease,
                            execution_id,
                            _classify_exception(exc),
                            f"renovação do lease falhou: {exc}",
                            outcome=T.AttemptOutcome.FAILED,
                            termination_prefix="heartbeat",
                        )
                        progressed = True
                    continue
                inflight.pop(task_id, None)
                unobservable.pop(execution_id, None)
                progressed = True
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    R.ErrorClass.TRANSIENT,
                    f"estado não observável ({state}) após {seen} consultas",
                    outcome=T.AttemptOutcome.FAILED,
                    termination_prefix="status",
                )
                continue
            inflight.pop(task_id, None)
            sent = submitted_envelopes.pop(task_id, None)
            progressed = True
            if state in FAILURE_STATES:
                detail = str(
                    (observed or {}).get("detail")
                    or (observed or {}).get("error")
                    or state
                )
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    R.ErrorClass.TRANSIENT if state == "timeout" else _class_from_state(observed),
                    f"execução terminou em {state}: {detail}",
                    outcome=T.AttemptOutcome.FAILED,
                    termination_prefix="execution",
                )
                continue
            envelope = (observed or {}).get("result")
            if envelope is None:
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    R.ErrorClass.TRANSIENT,
                    f"adaptador terminou sem resultado: {(observed or {}).get('error') or state}",
                    outcome=T.AttemptOutcome.FAILED,
                    termination_prefix="result",
                )
                continue

            verdict = accept_result(
                store,
                task,
                execution_id,
                envelope,
                schema_for(task),
                task_envelope=sent,
                binding_id=binding.binding_id,
                bindings=bindings,
            )
            if verdict.duplicate:
                # Mesma execução, mesmo conteúdo: nada é regravado (§10.4.4).
                report.accepted += 1
                report.done_tasks.append(task.task_id)
                continue
            if not verdict.accepted:
                assert verdict.reason is not None
                report.rejected += 1
                report.rejections.append(
                    (task.task_id, verdict.reason.value, verdict.detail)
                )
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    REJECTION_TO_ERROR_CLASS[verdict.reason],
                    f"{verdict.reason.value}: {verdict.detail}",
                    outcome=T.AttemptOutcome.REJECTED,
                    termination_prefix=verdict.reason.value.split(":")[0],
                )
                continue

            store.record_result(
                task.task_id,
                lease.lease_id,
                execution_id,
                state=T.TaskState.DONE,
                outcome=T.AttemptOutcome.SUCCEEDED,
                result=verdict.output,
                termination_reason="completed",
                result_hash=verdict.result_hash,
                now=now_fn(),
            )
            _close_binding_lease(bindings, lease.lease_id, "resultado aceito")
            report.accepted += 1
            report.done_tasks.append(task.task_id)

        if inflight and not progressed and poll_interval:
            sleep(poll_interval)

    report.failed = len(store.tasks_in_state(T.TaskState.FAILED))
    return report


# --------------------------------------------------------------------------
# Laço automático da cadeia (§7.3)
# --------------------------------------------------------------------------


@dataclass
class RoundReport:
    """Uma rodada do laço automático: o que rodou, o que moveu, o que parou."""

    round_no: int
    #: `RunReport` do despacho desta rodada (`None` quando nem despachou).
    run: RunReport | None = None
    #: `objective_id` -> `Progress.to_dict()`
    progress: dict[str, Any] = field(default_factory=dict)
    outcomes: tuple[Mapping[str, Any], ...] = ()
    planned: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    detail: str = ""

    @property
    def has_progress(self) -> bool:
        return any(bool(p.get("has_progress")) for p in self.progress.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "run": self.run.summary() if self.run is not None else None,
            "progress": {k: dict(v) for k, v in self.progress.items()},
            "has_progress": self.has_progress,
            "planned": dict(self.planned),
            "stop_reason": self.stop_reason,
            "detail": self.detail,
        }


@dataclass
class ChainReport:
    """Resultado do laço inteiro — o que `analyze`/`update`/`resume` publicam."""

    objective_ids: tuple[str, ...] = ()
    rounds: int = 0
    stop_reason: str = ""
    detail: str = ""
    progress_by_round: list[dict[str, Any]] = field(default_factory=list)
    round_reports: list[RoundReport] = field(default_factory=list)
    #: `objective_id` -> `ConsolidatedState.to_dict()` ao fim do laço.
    state: dict[str, Any] = field(default_factory=dict)
    #: `objective_id` -> `TaskStore.chain_status(objective_id)`
    chain: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_ids": list(self.objective_ids),
            "rounds": self.rounds,
            "stop_reason": self.stop_reason,
            "detail": self.detail,
            "progress_by_round": [dict(p) for p in self.progress_by_round],
            "rounds_detail": [r.to_dict() for r in self.round_reports],
            "state": {k: dict(v) for k, v in self.state.items()},
            "chain": {k: dict(v) for k, v in self.chain.items()},
            "diagnostics": list(self.diagnostics),
        }


def _validate_extra_keys(
    schema: ResultSchema | Mapping[Any, ResultSchema], keys: Sequence[str]
) -> None:
    """Aplica `with_extra` sem guardar o resultado — só para levantar cedo.

    Existe porque `run_chain` grava `set_chain_limits` ANTES da primeira
    rodada: descobrir a configuração inválida dentro de `run()` deixaria a
    cadeia com teto e orçamento persistidos para trabalho que não existe.
    """
    if isinstance(schema, ResultSchema):
        schema.with_extra(keys)
        return
    DEFAULT_SCHEMA.with_extra(keys)
    for candidato in dict(schema).values():
        candidato.with_extra(keys)


def _outcome_state(outcome: Mapping[str, Any]) -> str:
    return str(outcome.get("state") or "").strip().lower()


def run_chain(
    store: T.TaskStore,
    *,
    objective_ids: Sequence[str] | str,
    input_versions: Mapping[str, Any],
    outcomes_fn: Callable[[int], Sequence[Mapping[str, Any]]],
    repo_id: str = "",
    input_revision: str = "",
    executor: Any = None,
    adapter: Any = None,
    binding: A.AgentBinding | None = None,
    bindings: Any = None,
    budget: Mapping[str, Any] | None = None,
    max_rounds: int | None = None,
    policy: Mapping[str, Any] | None = None,
    result_extra_keys: Iterable[str] = (),
    engine_capabilities: Mapping[str, Any] | None = None,
    state_store: Any = None,
    on_round: Callable[[RoundReport], None] | None = None,
    max_loops: int = 32,
    now_fn: Callable[[], str] = T.utc_now,
    run_kwargs: Mapping[str, Any] | None = None,
) -> ChainReport:
    """Executa a cadeia INTEIRA numa invocação: planejar → despachar → aceitar → aplicar.

    É o §7.3 em código: `analyze`, `update` e `resume` chamam esta função UMA
    vez e ela repete as rodadas disponíveis até conclusão ou motivo material de
    parada. O operador não roda `resume` por rodada.

    Contrato dos parâmetros que a CLI precisa fornecer:

    * `outcomes_fn(round_no)` — a integração da rodada. Devolve a lista de
      outcomes por objetivo (o `IntegrationReport.to_dict()["objetivos"]` do
      `knowledge.integrate`). É um *callback* porque este módulo não importa
      `knowledge`: a fronteira runtime/knowledge continua valendo.
    * `input_versions` — snapshot das entradas; `input_revision` cai para
      `T.input_versions_hash(input_versions)` quando não informado, e é a
      terceira parte da chave do estado consolidado (§7.1).
    * `executor` OU `(adapter, binding)` — mesma regra de `run()`.
    * `policy` — limites de execução PEDIDOS pelo operador (perfil). Viaja em
      `TaskEnvelope.vendor["policy"]` até
      `AgentCapabilities.negotiate`, que devolve o mínimo entre pedido e
      declarado. Sem ele, `negotiate` recebia `{}` e o operador não tinha como
      pedir mais contexto/tempo a um agente que suporta mais.
    * `budget` — teto de CONSUMO da cadeia; gravado em `set_chain_limits` e
      lido de volta por `chain_status`, que é a fonte de
      `state.budget_exhausted`.
    * `result_extra_keys` — chaves de DADOS que o schema fechado passa a
      aceitar nesta cadeia (`ResultSchema.with_extra`). Campo de controle
      continua recusado.

    Parada, sempre com motivo canônico de `runtime.state`:

    | Motivo                 | Quando                                             |
    |------------------------|----------------------------------------------------|
    | `completed`            | nenhum objetivo do escopo continua `partial`       |
    | `executor_unavailable` | `run()` recusou o despacho — rodada NÃO é contada  |
    | `budget_exhausted`     | teto da cadeia ou orçamento consumido              |
    | `no_progress`          | rodada sem progresso, ou pacote idêntico ao anterior |
    | `evidence_changed`     | alguma leitura satisfeita foi invalidada na rodada |
    | `ambiguity`            | outcome declarou ambiguidade de identidade         |
    | `interrupted`          | `KeyboardInterrupt` OU erro interno na rodada (outcome fora de contrato, falha do store) — estado da rodada já persistido |

    Retomada idempotente: o estado é gravado A CADA rodada e cada delta é
    identificado por `result_hash`. Reexecutar `run_chain` depois de uma
    interrupção reaplica nada — `apply_result` devolve `duplicate=True` — e a
    cadeia continua de onde parou, sem reiniciar conclusões válidas.
    """
    ids = (objective_ids,) if isinstance(objective_ids, str) else tuple(str(o) for o in objective_ids)
    revision = str(input_revision or T.input_versions_hash(input_versions))
    states = state_store if state_store is not None else S.StateStore.of(store)
    report = ChainReport(objective_ids=ids)
    extra = dict(run_kwargs or {})
    # Parâmetros nomeados do operador vencem `run_kwargs` (que é a via genérica
    # e antiga): declarar `policy=` e ver o valor ignorado por causa de um
    # `run_kwargs` esquecido seria perda silenciosa de configuração.
    if policy is not None:
        extra["policy"] = dict(policy)
    if result_extra_keys:
        extra["result_extra_keys"] = tuple(str(k) for k in result_extra_keys)
        # Validação ANTES de `set_chain_limits`: uma chave extra inválida
        # (campo de controle, ou nome que o núcleo já declara — `matrix`,
        # `contract`, `objective_id`…) não pode deixar teto e orçamento
        # gravados para uma cadeia que nunca vai despachar. `run()` repete a
        # mesma derivação; aqui ela é feita só para estourar cedo, com o mesmo
        # `ValueError` e a mesma regra — sem persistir nada.
        _validate_extra_keys(extra.get("schema", DEFAULT_SCHEMA), extra["result_extra_keys"])

    # Teto e orçamento são da CADEIA: gravados uma vez, não a cada rodada.
    for oid in ids:
        if max_rounds is not None or budget is not None:
            store.set_chain_limits(oid, max_rounds=max_rounds, budget=budget, now=now_fn())

    current: dict[str, S.ConsolidatedState] = {
        oid: states.load_or_new(repo_id, oid, revision, input_versions=input_versions)
        for oid in ids
    }

    def _finish(reason: str, detail: str = "") -> ChainReport:
        report.stop_reason = reason
        report.detail = detail
        for oid in ids:
            # A1: a persistência da parada é BEST-EFFORT por objetivo. Se o
            # store falhar aqui, o ChainReport ainda sai com `stop_reason` e
            # com as rodadas já aplicadas — perder o relatório inteiro por
            # causa de uma gravação era o pior dos dois resultados, porque o
            # operador ficava sem saber sequer que rodadas rodaram.
            try:
                parado = S.with_stop_reason(current[oid], reason, now=now_fn())
                current[oid] = states.save(parado, now=now_fn())
                report.state[oid] = current[oid].to_dict()
                store.set_chain_stop(oid, reason, now=now_fn())
                report.chain[oid] = store.chain_status(oid)
            except Exception as exc:  # noqa: BLE001 — diagnóstico, não silêncio
                report.diagnostics.append(
                    f"parada {reason!r} não persistida para {oid!r}: "
                    f"{exc.__class__.__name__}: {exc}"
                )
        return report

    def _register_round(round_report: RoundReport) -> None:
        """Publica a rodada no relatório uma única vez (idempotente)."""
        snapshot = round_report.to_dict()
        if report.round_reports and report.round_reports[-1] is round_report:
            report.progress_by_round[-1] = snapshot
            return
        report.round_reports.append(round_report)
        report.progress_by_round.append(snapshot)
        if on_round is not None:
            on_round(round_report)

    def _integrate_round(round_report: RoundReport) -> tuple[str, str] | None:
        """Etapas 2–5 de uma rodada. `None` = a cadeia continua.

        Isolada em função para que `run_chain` possa blindar o corpo inteiro
        da rodada (A1) sem perder a legibilidade das etapas.
        """
        round_no_local = round_report.round_no
        # -- 2. integração da rodada ---------------------------------------
        outcomes = tuple(outcomes_fn(round_no_local) or ())
        round_report.outcomes = outcomes

        # -- 3. delta sobre o estado consolidado ---------------------------
        reabertas: list[str] = []
        ambiguidade = ""
        for outcome in outcomes:
            if not isinstance(outcome, Mapping):
                raise CoordinatorError(
                    f"outcomes_fn devolveu item fora de contrato na rodada "
                    f"{round_no_local}: esperado Mapping, veio {type(outcome).__name__}"
                )
            oid = str(outcome.get("objective_id") or "")
            if oid not in current:
                continue
            novo, progresso = states.apply(
                current[oid], outcome, round_no=round_no_local, now=now_fn()
            )
            current[oid] = novo
            round_report.progress[oid] = progresso.to_dict()
            reabertas.extend(progresso.readings_reopened)
            if str(outcome.get("ambiguidade") or outcome.get("ambiguity") or "").strip():
                ambiguidade = str(outcome.get("ambiguidade") or outcome.get("ambiguity"))
        for oid in ids:
            report.state[oid] = current[oid].to_dict()
        _register_round(round_report)

        # -- 4. motivos materiais de parada --------------------------------
        pendentes = [o for o in outcomes if _outcome_state(o) == "partial"]
        if outcomes and not pendentes:
            return (S.STOP_COMPLETED, "nenhum objetivo do escopo continua parcial")
        if ambiguidade:
            return (S.STOP_AMBIGUITY, ambiguidade)
        if reabertas:
            # "Evidência alterada: invalidar somente dependentes e replanejar"
            # — a invalidação já aconteceu no delta; a decisão de replanejar é
            # do chamador, com o estado que ele acaba de receber.
            return (
                S.STOP_EVIDENCE_CHANGED,
                f"leituras reabertas por invalidação: {sorted(set(reabertas))}",
            )

        # -- 5. planejar a rodada seguinte ---------------------------------
        planejado = plan_continuations(
            store,
            outcomes,
            input_versions=input_versions,
            budget=budget,
            max_rounds=max_rounds,
            engine_capabilities=engine_capabilities,
            progress=round_report.progress,
            now=now_fn(),
        )
        round_report.planned = planejado
        report.progress_by_round[-1] = round_report.to_dict()
        if not planejado.get("criadas"):
            motivos = [dict(r) for r in planejado.get("recusadas") or ()]
            reason = next(
                (str(r["stop_reason"]) for r in motivos if r.get("stop_reason")),
                S.STOP_NO_PROGRESS,
            )
            detalhe = "; ".join(str(r.get("motivo") or "") for r in motivos) or (
                "nenhuma continuação disponível"
            )
            report.diagnostics.extend(str(r.get("motivo") or "") for r in motivos)
            return (reason, detalhe)
        return None

    round_no = 0
    while round_no < max_loops:
        round_report = RoundReport(round_no=round_no)
        # -- 1. despacho ---------------------------------------------------
        try:
            round_report.run = run(
                store,
                executor=executor,
                adapter=adapter,
                binding=binding,
                bindings=bindings,
                now_fn=now_fn,
                **extra,
            )
        except DispatchUnavailable as exc:
            # §7.3: "preservar progresso, indicar correção concreta e
            # retomada". A rodada NÃO conta — o operador não perde crédito por
            # uma engine ausente.
            round_report.stop_reason = S.STOP_EXECUTOR_UNAVAILABLE
            round_report.detail = str(exc)
            report.round_reports.append(round_report)
            report.progress_by_round.append(round_report.to_dict())
            if on_round is not None:
                on_round(round_report)
            report.diagnostics.append(f"executor indisponível: {exc}")
            return _finish(S.STOP_EXECUTOR_UNAVAILABLE, str(exc))
        except KeyboardInterrupt:
            return _finish(S.STOP_INTERRUPTED, "interrompido durante o despacho")

        # -- 2 a 5: integração, delta, motivos e planejamento ---------------
        # A1: o corpo da rodada é BLINDADO. Antes, um `outcome` fora de
        # contrato (item não-Mapping vindo de `outcomes_fn`) ou uma falha do
        # store dentro de `states.apply`/`plan_continuations` propagava a
        # exceção para fora de `run_chain`: o ChainReport com as rodadas JÁ
        # APLICADAS era descartado, nenhum `stop_reason` chegava ao disco e
        # `_finish()` nunca rodava — a cadeia ficava sem parada registrada e o
        # operador sem retomada. Agora qualquer erro vira parada canônica
        # `interrupted` (mesmo vocabulário fechado do §7.3, já traduzido pela
        # CLI), com `detail` e `diagnostics` nomeando a causa.
        try:
            veredito = _integrate_round(round_report)
        except KeyboardInterrupt:
            detalhe = "interrompido durante a integração"
            round_report.stop_reason = S.STOP_INTERRUPTED
            round_report.detail = detalhe
            _register_round(round_report)
            return _finish(S.STOP_INTERRUPTED, detalhe)
        except Exception as exc:  # noqa: BLE001 — vira parada, não crash
            detalhe = (
                f"rodada {round_no} abortada por erro interno: "
                f"{exc.__class__.__name__}: {exc}"
            )
            round_report.stop_reason = S.STOP_INTERRUPTED
            round_report.detail = detalhe
            _register_round(round_report)
            report.diagnostics.append(detalhe)
            return _finish(S.STOP_INTERRUPTED, detalhe)
        if veredito is not None:
            return _finish(*veredito)

        report.rounds += 1
        round_no += 1

    return _finish(S.STOP_BUDGET_EXHAUSTED, f"laço atingiu max_loops={max_loops}")


def _class_from_state(observed: Mapping[str, Any] | None) -> R.ErrorClass:
    """Classe declarada pela engine, se houver; senão `transient`."""
    declared = (observed or {}).get("error_class")
    if declared:
        try:
            return R.ErrorClass(declared)
        except ValueError:
            pass
    return R.ErrorClass.TRANSIENT


__all__ = [
    "Acceptance",
    "ChainReport",
    "ENVELOPE_REJECTIONS",
    "RoundReport",
    "bind_executor",
    "run_chain",
    "envelope_for_task",
    "CONTROL_FIELDS",
    "CoordinatorError",
    "DEFAULT_SCHEMA",
    "Diagnostic",
    "DispatchUnavailable",
    "FAILURE_STATES",
    "REJECTION_TO_ERROR_CLASS",
    "RUNNING_STATES",
    "RejectionReason",
    "ResultRejected",
    "ResultSchema",
    "RunReport",
    "SUCCESS_STATES",
    "READING_LIST_FIELDS",
    "accept_result",
    "plan_continuations",
    "plan_from_objectives",
    "run",
]
