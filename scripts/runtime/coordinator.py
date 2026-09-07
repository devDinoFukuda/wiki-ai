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
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import recovery as R
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

    @property
    def declared(self) -> frozenset[str]:
        return frozenset(self.required) | frozenset(self.optional)

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


def accept_result(
    store: T.TaskStore,
    task: T.Task,
    execution_id: str,
    envelope: Any,
    schema: ResultSchema = DEFAULT_SCHEMA,
) -> Acceptance:
    """Valida o envelope `{execution_id, output}` antes de qualquer gravação.

    Ordem das checagens — identidade da execução ANTES de schema, de propósito:
    resultado de execução desconhecida não merece nem ser lido como dado.

    1. `execution_id` precisa existir em `attempts` E pertencer a ESTA tarefa.
       `attempts.execution_id` só é gravado por `start_attempt`, com o valor que
       `executor.submit` devolveu — logo, um id inventado pelo worker nunca casa.
    2. O envelope não pode declarar outra execução nem outra tarefa.
    3. A revisão de input declarada precisa bater com `input_versions_hash` da
       tarefa (aceite "revisão errada é rejeitada").
    4. A saída passa pelo schema fechado (§13.1).
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
    if not isinstance(envelope, Mapping):
        return Acceptance(
            False,
            reason=RejectionReason.MALFORMED,
            detail=f"envelope não é objeto: {type(envelope).__name__}",
        )

    declared_exec = envelope.get("execution_id")
    if declared_exec is not None and declared_exec != execution_id:
        return Acceptance(
            False,
            reason=RejectionReason.EXECUTION_MISMATCH,
            detail=(
                f"envelope declara execução {declared_exec!r}; a integração emitiu "
                f"{execution_id!r}"
            ),
        )
    declared_task = envelope.get("task_id")
    if declared_task is not None and declared_task != task.task_id:
        return Acceptance(
            False,
            reason=RejectionReason.TASK_MISMATCH,
            detail=f"envelope declara tarefa {declared_task!r}; esperada {task.task_id!r}",
        )

    for key in ("input_versions", "input_revision"):
        declared_inputs = envelope.get(key)
        if declared_inputs is None:
            continue
        if T.input_versions_hash(declared_inputs) != task.input_versions_hash:
            return Acceptance(
                False,
                reason=RejectionReason.INPUT_DIVERGENCE,
                detail=(
                    f"resultado descreve {key} fora do snapshot da tarefa "
                    f"({task.input_versions_hash[:12]})"
                ),
            )

    output = envelope.get("output", None)
    if output is None:
        return Acceptance(
            False, reason=RejectionReason.MALFORMED, detail="envelope sem campo 'output'"
        )
    problems = schema.validate(output)
    if problems:
        reason = next(
            (r for r, _ in problems if r is RejectionReason.CONTROL_FIELD),
            problems[0][0],
        )
        return Acceptance(
            False, reason=reason, detail="; ".join(detail for _, detail in problems)
        )
    return Acceptance(True, output=dict(output))


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
    now: str | None = None,
) -> dict[str, list[Any]]:
    """Cria tarefas de continuação para objetivos `partial` com pendência concreta.

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
                }
            )
            continue

        task_ids = T.create_continuation_tasks(
            store,
            parent_task_id=parent_task_id,
            objective_id=objective_id,
            needs=needs,
            input_versions=input_versions,
            budget=budget,
            round_no=round_no,
            max_rounds=max_rounds,
            now=now,
        )
        if task_ids:
            criadas.extend(task_ids)
        else:  # defensivo: teto/needs já checados acima, mas nunca confiar 2x
            recusadas.append(
                {"objective_id": objective_id, "motivo": "create_continuation_tasks recusou"}
            )
    return {"criadas": criadas, "recusadas": recusadas}


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
        raise DispatchUnavailable(
            "engine declarou ausência de despacho em capabilities(); "
            "configure a integração antes de executar análise"
        )
    return caps


def run(
    store: T.TaskStore,
    executor: Any,
    context_builder: Callable[..., Any] | None = None,
    *,
    max_concurrency: int = 2,
    policy: Mapping[str, Any] | None = None,
    schema: ResultSchema | Mapping[Any, ResultSchema] = DEFAULT_SCHEMA,
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
    caps = _check_dispatch(executor)
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
    #: quantas vezes seguidas cada execução respondeu estado não observável
    unobservable: dict[str, int] = {}

    def schema_for(task: T.Task) -> ResultSchema:
        if isinstance(schema, ResultSchema):
            return schema
        return schema.get(task.kind, schema.get(task.kind.value, DEFAULT_SCHEMA))

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
                try:
                    execution_id = executor.submit(
                        task.task_id,
                        task.objective,
                        _references(package),
                        schema_for(task).to_dict(),
                        dict(policy or {}),
                    )
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
                    store.start_attempt(task.task_id, lease.lease_id, execution_id, now=moment)
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
                observed = executor.status(execution_id)
            except Exception as exc:
                inflight.pop(task_id, None)
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    _classify_exception(exc),
                    f"status falhou: {exc}",
                    outcome=T.AttemptOutcome.FAILED,
                    termination_prefix="status",
                )
                progressed = True
                continue
            state = str((observed or {}).get("state", "running")).lower()
            if state in RUNNING_STATES:
                unobservable.pop(execution_id, None)
                # Batimento observado renova o lease: worker vivo não é despejado.
                store.heartbeat(lease.lease_id, ttl_seconds=lease_ttl_seconds, now=moment)
                continue
            if state not in SUCCESS_STATES and state not in FAILURE_STATES:
                # `unknown` (ou vocabulário não reconhecido) NUNCA vira sucesso.
                # Também não pode esperar para sempre: a engine que perdeu a
                # execução manteria a tarefa presa com heartbeat eterno.
                seen = unobservable.get(execution_id, 0) + 1
                unobservable[execution_id] = seen
                if seen <= unknown_state_limit:
                    store.heartbeat(lease.lease_id, ttl_seconds=lease_ttl_seconds, now=moment)
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
            progressed = True
            if state in FAILURE_STATES:
                detail = str((observed or {}).get("detail") or _failure_detail(executor, execution_id) or state)
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
            try:
                envelope = executor.result(execution_id)
            except Exception as exc:
                close_with_failure(
                    task,
                    lease,
                    execution_id,
                    _classify_exception(exc),
                    f"result falhou: {exc}",
                    outcome=T.AttemptOutcome.FAILED,
                    termination_prefix="result",
                )
                continue

            verdict = accept_result(store, task, execution_id, envelope, schema_for(task))
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
                now=now_fn(),
            )
            report.accepted += 1
            report.done_tasks.append(task.task_id)

        if inflight and not progressed and poll_interval:
            sleep(poll_interval)

    report.failed = len(store.tasks_in_state(T.TaskState.FAILED))
    return report


def _failure_detail(executor: Any, execution_id: str) -> str:
    """Causa da falha declarada pela engine, quando `result()` a expõe.

    Execução terminada em falha costuma trazer o erro em `result()["error"]`
    (é o contrato de `executors.base._result_dict`). Sem isso, o diagnóstico
    diria apenas "failed" — e, pior, dois erros DIFERENTES teriam o mesmo
    `error_hash`, bloqueando a tarefa por "erro idêntico" que não é idêntico.
    """
    try:
        envelope = executor.result(execution_id)
    except Exception:
        return ""
    if not isinstance(envelope, Mapping):
        return ""
    error = envelope.get("error")
    if error is None:
        return ""
    if isinstance(error, Mapping):
        return "; ".join(f"{k}={error[k]}" for k in sorted(error))
    return str(error)


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
