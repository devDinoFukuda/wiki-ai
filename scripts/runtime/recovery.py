"""Classes de erro, políticas persistidas e retomada (plano §7.4, onda W4).

O que este módulo é
-------------------
A tabela do §7.4 traduzida em decisão executável. Cada falha vira uma
`ErrorClass`; cada classe tem uma `Policy` persistida em `runtime.db`
(tabela `policies`, criada por `tasks.DDL`); `decide()` devolve a ação.

| Classe                  | Ação padrão            | Tentativas adicionais |
|-------------------------|------------------------|-----------------------|
| `transient`             | `retry`                | 2                     |
| `schema_invalid`        | `resubmit_with_errors` | 1                     |
| `evidence_missing`      | `reopen_claim`         | 1                     |
| `contradictory`         | `focused_reread`       | 1                     |
| `budget`                | `repartition`          | 1                     |
| `write_interrupted`     | `resume_effect`        | 2                     |
| `ambiguous_correlation` | `keep_candidate`       | 0                     |

O que este módulo NÃO é
-----------------------
Não é gate de aprovação. §7.4: "esses limites são mecanismos internos, não
novos gates". `Policy.decision_required` apenas MARCA que uma decisão humana
existe no diagnóstico quando o automático se esgota — não interrompe o fluxo
normal pedindo autorização.

Regra dura contra laço (§7.4)
-----------------------------
`error_hash = sha256(classe + detalhe normalizado)`. O mesmo hash registrado
duas vezes na mesma tarefa ⇒ `blocked`, independentemente do orçamento de
retentativas da classe. Nenhuma classe, nem por configuração, repete
indefinidamente o erro idêntico: `_cap` trunca qualquer valor configurado.

Retomada (`resume`)
-------------------
Reutiliza resultado válido; invalida SOMENTE tarefa cujo `input_versions`
diverge do snapshot atual (comparação por hash, nunca por data); libera lease
expirado. Uma tarefa `done` cujo input continua igual não é reexecutada.

Import de `runtime.context` é TARDIO e DEFENSIVO: `budget` delega a partição a
`BudgetExceeded.suggestion`, e a ausência do módulo vizinho não pode impedir
a decisão nem a retomada.

Só stdlib. Sem import de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import tasks as T

# --------------------------------------------------------------------------
# Classes de erro (§7.4)
# --------------------------------------------------------------------------


class ErrorClass(str, enum.Enum):
    SCHEMA_INVALID = "schema_invalid"
    EVIDENCE_MISSING = "evidence_missing"
    CONTRADICTORY = "contradictory"
    TRANSIENT = "transient"
    BUDGET = "budget"
    WRITE_INTERRUPTED = "write_interrupted"
    AMBIGUOUS_CORRELATION = "ambiguous_correlation"


class Action(str, enum.Enum):
    """Ação automática. Nenhuma delas pede autorização humana para acontecer."""

    RETRY = "retry"
    RESUBMIT_WITH_ERRORS = "resubmit_with_errors"
    REOPEN_CLAIM = "reopen_claim"
    FOCUSED_REREAD = "focused_reread"
    REPARTITION = "repartition"
    RESUME_EFFECT = "resume_effect"
    KEEP_CANDIDATE = "keep_candidate"
    BLOCK = "block"


#: Teto absoluto de repetições do MESMO erro. Não é configurável: é a tradução
#: de "nenhuma repetição ilimitada de erro idêntico" (§7.4).
SAME_ERROR_LIMIT = 2

#: Teto de retentativas adicionais que uma política pode declarar. Protege
#: contra configuração persistida que tentasse burlar o §7.4.
MAX_ADDITIONAL_ATTEMPTS = 3


@dataclass(frozen=True)
class Policy:
    """Política de uma classe de erro. Serializada em `policies.policy_json`."""

    error_class: ErrorClass
    action: Action
    #: Retentativas ALÉM da primeira execução.
    max_additional_attempts: int = 0
    #: A retentativa só é permitida quando a operação é idempotente (§7.4).
    requires_idempotent: bool = False
    #: Há decisão humana possível quando o automático se esgota (coluna
    #: "interação humana" do §7.4). Marca o diagnóstico; não bloqueia o fluxo.
    decision_required_on_exhaustion: bool = True

    def to_json(self) -> str:
        data = asdict(self)
        data["error_class"] = self.error_class.value
        data["action"] = self.action.value
        return json.dumps(data, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Policy":
        data = json.loads(raw)
        return cls(
            error_class=ErrorClass(data["error_class"]),
            action=Action(data["action"]),
            max_additional_attempts=_cap(int(data.get("max_additional_attempts", 0))),
            requires_idempotent=bool(data.get("requires_idempotent", False)),
            decision_required_on_exhaustion=bool(
                data.get("decision_required_on_exhaustion", True)
            ),
        )


def _cap(value: int) -> int:
    return max(0, min(int(value), MAX_ADDITIONAL_ATTEMPTS))


#: Defaults do plano: "até duas retentativas adicionais para falha transitória
#: idempotente; uma correção de schema; nenhuma repetição ilimitada".
DEFAULT_POLICIES: dict[ErrorClass, Policy] = {
    ErrorClass.TRANSIENT: Policy(
        ErrorClass.TRANSIENT, Action.RETRY, 2, requires_idempotent=True
    ),
    ErrorClass.SCHEMA_INVALID: Policy(
        ErrorClass.SCHEMA_INVALID, Action.RESUBMIT_WITH_ERRORS, 1
    ),
    ErrorClass.EVIDENCE_MISSING: Policy(ErrorClass.EVIDENCE_MISSING, Action.REOPEN_CLAIM, 1),
    ErrorClass.CONTRADICTORY: Policy(ErrorClass.CONTRADICTORY, Action.FOCUSED_REREAD, 1),
    ErrorClass.BUDGET: Policy(
        ErrorClass.BUDGET,
        Action.REPARTITION,
        1,
        # Repartir é automático (§7.4): humano só entra para AUMENTAR orçamento
        # total ou reduzir escopo — nunca para autorizar a partição.
        decision_required_on_exhaustion=True,
    ),
    ErrorClass.WRITE_INTERRUPTED: Policy(
        ErrorClass.WRITE_INTERRUPTED,
        Action.RESUME_EFFECT,
        2,
        requires_idempotent=True,
        decision_required_on_exhaustion=False,
    ),
    ErrorClass.AMBIGUOUS_CORRELATION: Policy(
        ErrorClass.AMBIGUOUS_CORRELATION, Action.KEEP_CANDIDATE, 0
    ),
}


# --------------------------------------------------------------------------
# Persistência das políticas
# --------------------------------------------------------------------------


def load_policies(store: T.TaskStore, *, now: str | None = None) -> dict[ErrorClass, Policy]:
    """Lê as políticas de `runtime.db`, semeando os defaults do plano se ausentes.

    Semear na leitura é o que garante que o dict fique VISÍVEL e editável no
    banco em vez de existir só como constante em código.
    """
    moment = now or T.utc_now()
    rows = {
        r["error_class"]: r["policy_json"]
        for r in store.conn.execute("SELECT error_class, policy_json FROM policies")
    }
    out: dict[ErrorClass, Policy] = {}
    missing: list[Policy] = []
    for cls, default in DEFAULT_POLICIES.items():
        raw = rows.get(cls.value)
        if raw is None:
            out[cls] = default
            missing.append(default)
        else:
            out[cls] = Policy.from_json(raw)
    if missing:
        with store.immediate() as conn:
            for policy in missing:
                conn.execute(
                    "INSERT OR IGNORE INTO policies(error_class, policy_json, updated_at) "
                    "VALUES (?,?,?)",
                    (policy.error_class.value, policy.to_json(), moment),
                )
    return out


def save_policy(store: T.TaskStore, policy: Policy, *, now: str | None = None) -> Policy:
    """Persiste política configurada. `max_additional_attempts` é truncado por `_cap`."""
    policy = Policy(
        error_class=policy.error_class,
        action=policy.action,
        max_additional_attempts=_cap(policy.max_additional_attempts),
        requires_idempotent=policy.requires_idempotent,
        decision_required_on_exhaustion=policy.decision_required_on_exhaustion,
    )
    moment = now or T.utc_now()
    with store.immediate() as conn:
        conn.execute(
            "INSERT INTO policies(error_class, policy_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(error_class) DO UPDATE SET policy_json=excluded.policy_json, "
            "updated_at=excluded.updated_at",
            (policy.error_class.value, policy.to_json(), moment),
        )
    return policy


# --------------------------------------------------------------------------
# Hash do erro
# --------------------------------------------------------------------------

#: Ruído que muda a cada tentativa e faria dois erros idênticos parecerem
#: diferentes — o que quebraria a proteção contra laço.
_VOLATILE = (
    re.compile(r"\b[0-9a-f]{8,}\b", re.I),                    # ids/hashes
    re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.+\-]+"),               # timestamps ISO
    re.compile(r"0x[0-9a-f]+", re.I),                          # endereços
    re.compile(r"\bline \d+\b", re.I),                         # linha de traceback
)


def normalize_error_detail(detail: str) -> str:
    """Remove o volátil do detalhe para que "o mesmo erro" seja reconhecível.

    Sem isso, um id de execução novo a cada tentativa geraria hash novo a cada
    tentativa e o limite de repetição nunca dispararia.
    """
    text = " ".join(str(detail).split())
    for pattern in _VOLATILE:
        text = pattern.sub("~", text)
    return text.strip().lower()


def error_hash(error_class: ErrorClass | str, detail: str) -> str:
    """`sha256(classe + detalhe normalizado)`: a identidade de "erro idêntico"."""
    cls = ErrorClass(error_class).value
    return T.sha256_hex(f"{cls}\n{normalize_error_detail(detail)}")


# --------------------------------------------------------------------------
# Decisão
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """O que fazer com a tarefa após um erro. Já traz o diagnóstico curto do §7.2."""

    task_id: str
    error_class: ErrorClass
    action: Action
    retry: bool
    error_hash: str
    same_error_count: int
    attempts_used: int
    max_additional_attempts: int
    cause: str
    impact: str
    attempted_fix: str
    decision_required: bool
    #: Preenchido por `budget`: partição sugerida por `context.BudgetExceeded`.
    suggestion: Any = None
    #: Preenchido por `write_interrupted`: efeitos pendentes a retomar.
    pending_effects: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.action is Action.BLOCK

    def message(self) -> str:
        """Mensagem curta {causa, impacto, correção tentada, decisão necessária?} (§7.2)."""
        parts = [
            f"causa: {self.cause}",
            f"impacto: {self.impact}",
            f"correção tentada: {self.attempted_fix}",
            f"decisão necessária: {'sim' if self.decision_required else 'não'}",
        ]
        return " | ".join(parts)


def _budget_suggestion(exc: BaseException | None) -> Any:
    """Partição sugerida por `context.BudgetExceeded`, se o vizinho existir.

    Import tardio e defensivo: `recovery` precisa decidir mesmo antes de
    `runtime/context.py` existir. Sem o módulo, a ação continua sendo
    `repartition` — apenas sem sugestão pronta.
    """
    if exc is not None and hasattr(exc, "suggestion"):
        return getattr(exc, "suggestion")
    try:  # pragma: no cover - depende do vizinho
        from . import context as _context  # type: ignore
    except Exception:
        return None
    budget_exceeded = getattr(_context, "BudgetExceeded", None)
    if budget_exceeded is not None and isinstance(exc, budget_exceeded):
        return getattr(exc, "suggestion", None)
    return None


def decide(
    store: T.TaskStore,
    task_id: str,
    error_class: ErrorClass | str,
    error_detail: str,
    *,
    policies: Mapping[ErrorClass, Policy] | None = None,
    idempotent: bool = True,
    exception: BaseException | None = None,
    now: str | None = None,
) -> Decision:
    """Decide entre retentar (com qual ação) e bloquear, para UM erro de UMA tarefa.

    Ordem das guardas — a primeira que valer decide:
    1. Mesmo `error_hash` já registrado `SAME_ERROR_LIMIT` vezes ⇒ `BLOCK`.
       Isso vem ANTES do orçamento da classe porque "erro idêntico" é limite
       absoluto do §7.4, não um limite por classe.
    2. Operação não idempotente numa política que exige idempotência ⇒ `BLOCK`
       (§7.4: retentativa limitada "quando a operação for idempotente").
    3. Retentativas da classe esgotadas ⇒ `BLOCK`.
    4. Caso contrário, a ação da política.

    `attempts_used` conta as tentativas JÁ registradas: chamar depois de
    `record_result` é o uso previsto.
    """
    cls = ErrorClass(error_class)
    policies = policies or load_policies(store, now=now)
    policy = policies.get(cls, DEFAULT_POLICIES[cls])
    task = store.get(task_id)
    err_hash = error_hash(cls, error_detail)
    repeated = store.same_error_count(task_id, err_hash)
    attempts_used = task.attempt_count
    allowed = _cap(policy.max_additional_attempts)

    suggestion = _budget_suggestion(exception) if cls is ErrorClass.BUDGET else None
    pending = ()
    if cls is ErrorClass.WRITE_INTERRUPTED:
        pending = tuple(
            e["effect_id"] for e in store.pending_effects() if e["task_id"] == task_id
        )

    def build(action: Action, cause: str, fix: str, decision_required: bool) -> Decision:
        return Decision(
            task_id=task_id,
            error_class=cls,
            action=action,
            retry=action not in (Action.BLOCK, Action.KEEP_CANDIDATE),
            error_hash=err_hash,
            same_error_count=repeated,
            attempts_used=attempts_used,
            max_additional_attempts=allowed,
            cause=cause,
            impact=(
                f"objetivo {task.objective.get('objective_id', task.task_id)} "
                f"({task.kind.value}) sem resultado aceito"
            ),
            attempted_fix=fix,
            decision_required=decision_required,
            suggestion=suggestion,
            pending_effects=pending,
        )

    if repeated >= SAME_ERROR_LIMIT:
        return build(
            Action.BLOCK,
            f"{cls.value} idêntico {repeated}x (hash {err_hash[:12]})",
            f"{policy.action.value} já aplicado; repetição idêntica interrompida",
            policy.decision_required_on_exhaustion,
        )
    if policy.requires_idempotent and not idempotent:
        return build(
            Action.BLOCK,
            f"{cls.value} em operação não idempotente",
            "retentativa não aplicada: repetir poderia duplicar efeito",
            True,
        )
    if attempts_used > allowed:
        return build(
            Action.BLOCK,
            f"{cls.value}: {attempts_used} tentativas, limite {allowed + 1}",
            f"{policy.action.value} esgotado",
            policy.decision_required_on_exhaustion,
        )
    if policy.action is Action.KEEP_CANDIDATE:
        return build(
            Action.KEEP_CANDIDATE,
            f"{cls.value}: identidade não resolvida por evidência",
            "candidato mantido separado; vínculo não confirmado",
            True,
        )
    return build(
        policy.action,
        f"{cls.value}: {normalize_error_detail(error_detail)[:160]}",
        f"{policy.action.value} (tentativa {attempts_used + 1} de {allowed + 1})",
        False,
    )


# --------------------------------------------------------------------------
# Retomada
# --------------------------------------------------------------------------


@dataclass
class ResumePlan:
    """Plano de retomada: o que reaproveitar, o que refazer e o que já foi solto."""

    reusable: list[str] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)
    released_leases: list[str] = field(default_factory=list)
    pending_effects: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "reusable": len(self.reusable),
            "invalidated": len(self.invalidated),
            "released_leases": len(self.released_leases),
            "pending_effects": len(self.pending_effects),
            "ready": len(self.ready),
            "blocked": len(self.blocked),
        }


def resume(
    store: T.TaskStore,
    *,
    current_input_versions: Mapping[str, Any] | None = None,
    scope_task_ids: Iterable[str] | None = None,
    now: str | None = None,
) -> ResumePlan:
    """Retomada após reinício (§7.2/§7.4).

    Três coisas, nesta ordem:

    1. Libera lease EXPIRADO. Lease vigente é preservado — outro processo pode
       estar vivo trabalhando nele, e roubá-lo é justamente o que faria dois
       workers produzirem o mesmo resultado.
    2. Invalida SOMENTE tarefa cujo `input_versions_hash` diverge do snapshot
       atual. Comparação por HASH, não por data: arquivo tocado sem mudança de
       conteúdo não invalida nada, e mudança de conteúdo invalida mesmo que o
       timestamp não mude. Sem `current_input_versions`, nada é invalidado —
       "não sei qual é o snapshot atual" não autoriza descartar resultado.
    3. Lista o que continua reutilizável e o que está pronto.

    Efeitos `pending` são listados, nunca reexecutados aqui: quem os confirma é
    o dono do destino, de forma idempotente (`tasks.confirm_effect`).
    """
    moment = now or T.utc_now()
    plan = ResumePlan()

    plan.released_leases = store.release_expired_leases(moment)

    scope = set(scope_task_ids) if scope_task_ids is not None else None
    expected = (
        T.input_versions_hash(dict(current_input_versions))
        if current_input_versions is not None
        else None
    )
    if expected is None:
        plan.notes.append(
            "snapshot atual não informado: nenhuma tarefa invalidada por divergência de input"
        )

    for task in store.all_tasks():
        if scope is not None and task.task_id not in scope:
            continue
        diverged = expected is not None and task.input_versions_hash != expected
        if diverged:
            if task.state in (T.TaskState.DONE, T.TaskState.FAILED, T.TaskState.BLOCKED) or (
                task.result is not None
            ):
                store.requeue(
                    task.task_id,
                    reason="invalidated:input_divergence",
                    clear_result=True,
                    now=moment,
                )
                plan.invalidated.append(task.task_id)
            continue
        if task.state is T.TaskState.DONE and task.result is not None:
            plan.reusable.append(task.task_id)
        elif task.state is T.TaskState.BLOCKED:
            plan.blocked.append(task.task_id)

    plan.pending_effects = [e["effect_id"] for e in store.pending_effects()]
    plan.ready = [t.task_id for t in store.ready_tasks(now=moment)]
    return plan


__all__ = [
    "Action",
    "DEFAULT_POLICIES",
    "Decision",
    "ErrorClass",
    "MAX_ADDITIONAL_ATTEMPTS",
    "Policy",
    "ResumePlan",
    "SAME_ERROR_LIMIT",
    "decide",
    "error_hash",
    "load_policies",
    "normalize_error_detail",
    "resume",
    "save_policy",
]
