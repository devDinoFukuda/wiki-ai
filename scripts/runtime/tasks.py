"""`runtime.db` e o modelo de tarefas do coordenador (plano §4.2, §7.2, onda W4).

Forma geral
-----------
Uma TAREFA é a unidade persistida de trabalho: objetivo, entradas versionadas,
dependências, orçamento, resultado e motivo de término (§7.2). Ao redor dela:

| Tabela            | Papel                                                          |
|-------------------|----------------------------------------------------------------|
| `tasks`           | objetivo, `input_versions`, `depends_on`, orçamento, estado, resultado |
| `task_deps`       | forma normalizada de `depends_on` (com FK) usada pela consulta de prontidão |
| `leases`          | posse temporária e expirável de uma tarefa por um worker        |
| `attempts`        | uma linha por execução tentada, com `execution_id` real e classe do erro |
| `effects_runtime` | espelho local do `effect_id` da OUTBOX de `knowledge.db`        |
| `policies`        | políticas de erro configuráveis e persistidas (§7.4)            |

Fronteiras que este módulo NÃO cruza
------------------------------------
- `effects_runtime.effect_id` referencia a outbox de `knowledge.db` e **não tem
  FK**: são dois arquivos SQLite distintos. Não existe transação distribuída
  (§4.2). O efeito só vira `confirmed` depois de confirmação idempotente do
  destino; enquanto não vier, sobrevive a reinício como `pending`.
- Tarefa concluída NÃO cria fato. Este módulo não escreve em `knowledge.db`.

Idempotência (§7.2)
-------------------
`effect_identity = sha256(objetivo + versão das entradas + configuração
relevante)` e é UNIQUE em `tasks`. Criar a mesma tarefa duas vezes devolve a
existente; se ela já está `done`, o resultado é reutilizado em vez de
reexecutado (`reuse_result`).

Relógio: `utc_now()` usa microssegundos (o de `knowledge.repository` usa
segundos) porque expiração de lease precisa de resolução menor que 1s. O
formato tem largura fixa, então comparação lexicográfica de ISO-8601 UTC é
comparação cronológica — é assim que a expiração é testada em SQL.

Só stdlib. Sem import de `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class RuntimeStoreError(Exception):
    """Base de todo erro deste módulo."""


class SchemaVersionMismatch(RuntimeStoreError):
    """`runtime.db` está numa versão que este código não suporta."""


class UnknownTask(RuntimeStoreError):
    """`task_id` não existe em `runtime.db`."""


class LeaseHeld(RuntimeStoreError):
    """A tarefa já tem lease vigente de outro dono. Duas aquisições concorrentes: uma falha."""


class LeaseLost(RuntimeStoreError):
    """O `lease_id` apresentado não é o lease vigente da tarefa (expirou ou foi retomado)."""


class InvalidTransition(RuntimeStoreError):
    """Transição de estado não prevista pelo modelo."""


# --------------------------------------------------------------------------
# Relógio, JSON canônico e identidade
# --------------------------------------------------------------------------


def utc_now() -> str:
    """Instante ISO-8601 UTC com microssegundos. Único relógio do módulo."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def shift(moment: str, seconds: float) -> str:
    """`moment` deslocado em `seconds`. Usado para calcular expiração de lease."""
    return (datetime.fromisoformat(moment) + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )


def canonical_json(value: Any) -> str:
    """Serialização determinística: mesma entrada semântica ⇒ mesmos bytes.

    `sort_keys` é o que torna `effect_identity` estável entre execuções; sem
    ele, a ordem de um dict mudaria o hash e a idempotência do §7.2 seria
    apenas aparente.
    """
    return json.dumps(
        value if value is not None else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def effect_identity(
    objective: Any, input_versions: Any, config: Any = None
) -> str:
    """§7.2 — identidade do efeito: objetivo + versão das entradas + configuração relevante.

    Só entra aqui o que muda o RESULTADO. Nada de timestamp, host, owner ou
    tentativa: se isso entrasse, cada reinício produziria identidade nova e a
    retomada duplicaria trabalho em vez de reutilizá-lo.
    """
    return sha256_hex(
        canonical_json(
            {
                "objective": objective,
                "input_versions": input_versions,
                "config": config,
            }
        )
    )


def input_versions_hash(input_versions: Any) -> str:
    """Hash das entradas versionadas. É a comparação usada por `recovery.resume`."""
    return sha256_hex(canonical_json(input_versions))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# Enums do modelo
# --------------------------------------------------------------------------


class TaskKind(str, enum.Enum):
    """§7.2 — "gerar tarefas de descoberta, investigação, verificação, correlação e publicação"."""

    DISCOVERY = "discovery"
    INVESTIGATION = "investigation"
    VERIFICATION = "verification"
    CORRELATION = "correlation"
    PUBLICATION = "publication"


class TaskState(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


#: Estados que encerram a tarefa. `done` é o único que autoriza um dependente.
TERMINAL_STATES = (TaskState.DONE, TaskState.FAILED, TaskState.BLOCKED)


class AttemptOutcome(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Resultado recusado por F07/§13.1 (execução desconhecida, input divergente,
    #: schema inválido). Não é "falha do worker": é resultado NÃO aceito.
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EffectState(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT ''
);

-- ----------------------------------------------------------------- tarefas
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    objective_json      TEXT NOT NULL,
    -- snapshot_id + source_version_ids: a versão das entradas que este
    -- resultado descreve. Divergiu do snapshot atual => resultado inválido.
    input_versions_json TEXT NOT NULL DEFAULT '{}',
    input_versions_hash TEXT NOT NULL,
    -- forma declarada de depends_on; task_deps é a forma consultável (FK)
    depends_on_json     TEXT NOT NULL DEFAULT '[]',
    budget_json         TEXT NOT NULL DEFAULT '{}',
    config_json         TEXT NOT NULL DEFAULT '{}',
    -- §7.2: UNIQUE é o que impede duplicar trabalho já feito
    effect_identity     TEXT NOT NULL UNIQUE,
    state               TEXT NOT NULL DEFAULT 'pending',
    result_json         TEXT,
    termination_reason  TEXT NOT NULL DEFAULT '',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (kind IN ('discovery','investigation','verification','correlation','publication')),
    CHECK (state IN ('pending','ready','leased','done','failed','blocked'))
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id            TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id)
);

-- ------------------------------------------------------------------ leases
-- PK em task_id: existe no máximo UM lease por tarefa. Reaquisição só
-- acontece por substituição de um lease EXPIRADO (ver acquire_lease).
CREATE TABLE IF NOT EXISTS leases (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    lease_id     TEXT NOT NULL UNIQUE,
    owner        TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

-- --------------------------------------------------------------- tentativas
CREATE TABLE IF NOT EXISTS attempts (
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    attempt_no   INTEGER NOT NULL,
    -- emitido pelo executor (F07); NUNCA pelo worker dentro do payload
    execution_id TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    outcome      TEXT NOT NULL DEFAULT 'running',
    error_class  TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    -- hash do erro: mesmo hash duas vezes => blocked (§7.4, sem laço infinito)
    error_hash   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, attempt_no),
    CHECK (outcome IN ('running','succeeded','failed','rejected','cancelled'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_attempts_exec
    ON attempts(execution_id) WHERE execution_id IS NOT NULL;

-- ------------------------------------------------------------------ efeitos
-- effect_id vem da OUTBOX de knowledge.db. Sem FK de propósito: bancos
-- distintos, sem transação distribuída (§4.2).
CREATE TABLE IF NOT EXISTS effects_runtime (
    effect_id           TEXT PRIMARY KEY,
    task_id             TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    state               TEXT NOT NULL DEFAULT 'pending',
    registered_at       TEXT NOT NULL,
    confirmed_at        TEXT,
    confirmation_detail TEXT NOT NULL DEFAULT '',
    CHECK (state IN ('pending','confirmed'))
);

-- ---------------------------------------------------------------- políticas
-- §7.4: "políticas configuráveis e persistidas". Mecanismo interno; não é
-- gate de aprovação humana.
CREATE TABLE IF NOT EXISTS policies (
    error_class TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tasks_state    ON tasks(state, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_identity ON tasks(effect_identity);
CREATE INDEX IF NOT EXISTS ix_tasks_inputs   ON tasks(input_versions_hash);
CREATE INDEX IF NOT EXISTS ix_deps_dep       ON task_deps(depends_on_task_id);
CREATE INDEX IF NOT EXISTS ix_leases_exp     ON leases(expires_at);
CREATE INDEX IF NOT EXISTS ix_attempts_task  ON attempts(task_id, attempt_no);
CREATE INDEX IF NOT EXISTS ix_attempts_hash  ON attempts(task_id, error_hash);
CREATE INDEX IF NOT EXISTS ix_effects_state  ON effects_runtime(state, registered_at);
"""

TABLES = (
    "schema_version",
    "tasks",
    "task_deps",
    "leases",
    "attempts",
    "effects_runtime",
    "policies",
)


def apply_schema(conn: sqlite3.Connection, now: str) -> int:
    """Cria o schema se ausente e valida a versão instalada.

    Migração é ato explícito (W8), nunca efeito colateral de abrir conexão —
    mesma regra de `knowledge.schema.apply_schema`.
    """
    conn.executescript(DDL)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    installed = row[0] if row else None
    if installed is None:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at, note) VALUES (?,?,?)",
            (SCHEMA_VERSION, now, "initial"),
        )
        return SCHEMA_VERSION
    if int(installed) != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"runtime.db está na versão {installed}; este código suporta "
            f"{SCHEMA_VERSION}. Migração é explícita — não há upgrade automático."
        )
    return int(installed)


def connect(path: str, now: str | None = None, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Abre `runtime.db` com WAL, `foreign_keys` ON e schema validado.

    `isolation_level=None` desliga o gerenciamento implícito de transação do
    driver: quem decide os limites é `immediate()`, com `BEGIN IMMEDIATE`. É o
    que faz duas aquisições concorrentes de lease serem serializadas em vez de
    ambas "vencerem" e escreverem por cima.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    apply_schema(conn, now or utc_now())
    return conn


# --------------------------------------------------------------------------
# Tipos de leitura
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    task_id: str
    lease_id: str
    owner: str
    acquired_at: str
    expires_at: str
    heartbeat_at: str

    def expired(self, now: str) -> bool:
        return self.expires_at <= now


@dataclass(frozen=True)
class Attempt:
    task_id: str
    attempt_no: int
    execution_id: str | None
    started_at: str
    ended_at: str | None
    outcome: AttemptOutcome
    error_class: str
    error_detail: str
    error_hash: str


@dataclass
class Task:
    task_id: str
    kind: TaskKind
    objective: dict[str, Any]
    input_versions: dict[str, Any]
    input_versions_hash: str
    depends_on: tuple[str, ...]
    budget: dict[str, Any]
    config: dict[str, Any]
    effect_identity: str
    state: TaskState
    result: dict[str, Any] | None
    termination_reason: str
    attempt_count: int
    created_at: str
    updated_at: str
    #: Preenchido só quando a criação encontrou identidade já existente (§7.2).
    reused: bool = field(default=False, compare=False)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            task_id=row["task_id"],
            kind=TaskKind(row["kind"]),
            objective=json.loads(row["objective_json"]),
            input_versions=json.loads(row["input_versions_json"]),
            input_versions_hash=row["input_versions_hash"],
            depends_on=tuple(json.loads(row["depends_on_json"])),
            budget=json.loads(row["budget_json"]),
            config=json.loads(row["config_json"]),
            effect_identity=row["effect_identity"],
            state=TaskState(row["state"]),
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
            termination_reason=row["termination_reason"],
            attempt_count=int(row["attempt_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class TaskStore:
    """Acesso transacional a `runtime.db`.

    Toda escrita que precisa ser atômica passa por `immediate()`. Não existe
    "escrita solta": gravar resultado, fechar tentativa, atualizar contador e
    liberar lease acontecem na MESMA transação (aceite "tarefas concluídas em
    paralelo não perdem registros").
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: str, now: str | None = None, busy_timeout_ms: int = 5000) -> "TaskStore":
        return cls(connect(path, now=now, busy_timeout_ms=busy_timeout_ms))

    def close(self) -> None:
        self.conn.close()

    # -- transação ---------------------------------------------------------
    @contextlib.contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK`.

        `IMMEDIATE` (e não `DEFERRED`) porque a decisão de lease precisa do
        lock de escrita ANTES de ler: com `DEFERRED`, dois donos leriam
        "sem lease" e ambos tentariam gravar.
        """
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- criação -----------------------------------------------------------
    def create_task(
        self,
        kind: TaskKind | str,
        objective: Mapping[str, Any],
        input_versions: Mapping[str, Any],
        *,
        depends_on: Sequence[str] = (),
        budget: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> Task:
        """Persiste uma tarefa; se a `effect_identity` já existe, devolve a existente.

        §7.2 "evitar duplicidade de trabalho": o caminho de deduplicação é a
        própria identidade do efeito, não o `task_id`. Replanejar o mesmo
        objetivo sobre o mesmo snapshot NÃO cria segunda tarefa — e se a
        primeira já está `done`, o resultado dela está aqui para ser
        reutilizado (`Task.reused is True`).
        """
        kind = TaskKind(kind)
        moment = now or utc_now()
        identity = effect_identity(objective, input_versions, config)
        with self.immediate() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE effect_identity=?", (identity,)
            ).fetchone()
            if row is not None:
                existing = Task.from_row(row)
                existing.reused = True
                return existing
            tid = task_id or new_id("task")
            deps = tuple(dict.fromkeys(depends_on))
            conn.execute(
                "INSERT INTO tasks(task_id, kind, objective_json, input_versions_json, "
                "input_versions_hash, depends_on_json, budget_json, config_json, "
                "effect_identity, state, result_json, termination_reason, attempt_count, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'',0,?,?)",
                (
                    tid,
                    kind.value,
                    canonical_json(dict(objective)),
                    canonical_json(dict(input_versions)),
                    input_versions_hash(dict(input_versions)),
                    canonical_json(list(deps)),
                    canonical_json(dict(budget or {})),
                    canonical_json(dict(config or {})),
                    identity,
                    TaskState.PENDING.value,
                    moment,
                    moment,
                ),
            )
            for dep in deps:
                if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (dep,)).fetchone() is None:
                    raise UnknownTask(f"dependência {dep!r} de {tid!r} não existe em runtime.db")
                conn.execute(
                    "INSERT OR IGNORE INTO task_deps(task_id, depends_on_task_id) VALUES (?,?)",
                    (tid, dep),
                )
            return Task.from_row(
                conn.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            )

    # -- leitura -----------------------------------------------------------
    def get(self, task_id: str) -> Task:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise UnknownTask(f"tarefa {task_id!r} não existe em runtime.db")
        return Task.from_row(row)

    def all_tasks(self) -> list[Task]:
        return [
            Task.from_row(r)
            for r in self.conn.execute("SELECT * FROM tasks ORDER BY created_at, task_id")
        ]

    def tasks_in_state(self, state: TaskState | str) -> list[Task]:
        state = TaskState(state)
        return [
            Task.from_row(r)
            for r in self.conn.execute(
                "SELECT * FROM tasks WHERE state=? ORDER BY created_at, task_id", (state.value,)
            )
        ]

    def reuse_result(self, identity: str) -> dict[str, Any] | None:
        """Resultado já produzido para a mesma `effect_identity`, se houver.

        É o "nunca duplica trabalho" do §7.2 no ponto de decisão: antes de
        submeter, o coordenador pergunta aqui.
        """
        row = self.conn.execute(
            "SELECT result_json FROM tasks WHERE effect_identity=? AND state=?",
            (identity, TaskState.DONE.value),
        ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    # -- prontidão ---------------------------------------------------------
    def refresh_states(self, now: str | None = None) -> dict[str, int]:
        """Promove `pending`→`ready` e `pending`→`blocked` conforme as dependências.

        Dependência `failed`/`blocked` bloqueia o dependente: sem isso, uma
        tarefa esperaria para sempre por um pai que nunca vai concluir. Um pai
        que volte a `pending` (invalidado por `recovery.resume`) devolve o
        filho de `ready` para `pending`.
        """
        moment = now or utc_now()
        promoted = blocked = demoted = 0
        with self.immediate() as conn:
            rows = conn.execute(
                "SELECT task_id, state FROM tasks WHERE state IN ('pending','ready')"
            ).fetchall()
            for row in rows:
                tid = row["task_id"]
                dep_states = [
                    r["state"]
                    for r in conn.execute(
                        "SELECT p.state AS state FROM task_deps d JOIN tasks p "
                        "ON p.task_id=d.depends_on_task_id WHERE d.task_id=?",
                        (tid,),
                    )
                ]
                if any(s in (TaskState.FAILED.value, TaskState.BLOCKED.value) for s in dep_states):
                    conn.execute(
                        "UPDATE tasks SET state=?, termination_reason=?, updated_at=? "
                        "WHERE task_id=?",
                        (
                            TaskState.BLOCKED.value,
                            "blocked:dependency_terminal",
                            moment,
                            tid,
                        ),
                    )
                    blocked += 1
                    continue
                satisfied = all(s == TaskState.DONE.value for s in dep_states)
                if satisfied and row["state"] == TaskState.PENDING.value:
                    conn.execute(
                        "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                        (TaskState.READY.value, moment, tid),
                    )
                    promoted += 1
                elif not satisfied and row["state"] == TaskState.READY.value:
                    conn.execute(
                        "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                        (TaskState.PENDING.value, moment, tid),
                    )
                    demoted += 1
        return {"ready": promoted, "blocked": blocked, "pending": demoted}

    def ready_tasks(self, *, limit: int | None = None, now: str | None = None) -> list[Task]:
        """Tarefas executáveis agora: dependências `done` e sem lease vigente.

        A ordenação por `created_at, task_id` é o que torna o laço do
        coordenador determinístico — duas execuções sobre o mesmo banco
        despacham na mesma ordem.
        """
        moment = now or utc_now()
        self.refresh_states(moment)
        sql = (
            "SELECT t.* FROM tasks t "
            "WHERE t.state=? "
            "AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id=t.task_id AND l.expires_at>?) "
            "ORDER BY t.created_at, t.task_id"
        )
        params: list[Any] = [TaskState.READY.value, moment]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [Task.from_row(r) for r in self.conn.execute(sql, params)]

    # -- leases ------------------------------------------------------------
    def lease_of(self, task_id: str) -> Lease | None:
        row = self.conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
        return None if row is None else Lease(**dict(row))

    def acquire_lease(
        self, task_id: str, owner: str, *, ttl_seconds: float = 300.0, now: str | None = None
    ) -> Lease:
        """Toma posse da tarefa por `ttl_seconds`. Lease vigente de outro dono ⇒ `LeaseHeld`.

        Reaquisição só existe para lease EXPIRADO: o worker que morreu segurando
        a tarefa não a prende para sempre, e o worker vivo não é despejado no
        meio da execução. Como tudo acontece dentro de `BEGIN IMMEDIATE`, duas
        aquisições concorrentes se serializam e a segunda vê o lease da
        primeira — uma falha, sempre.
        """
        moment = now or utc_now()
        with self.immediate() as conn:
            trow = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if trow is None:
                raise UnknownTask(f"tarefa {task_id!r} não existe em runtime.db")
            current = conn.execute(
                "SELECT * FROM leases WHERE task_id=?", (task_id,)
            ).fetchone()
            if current is not None and current["expires_at"] > moment:
                raise LeaseHeld(
                    f"tarefa {task_id!r} já está com lease {current['lease_id']!r} de "
                    f"{current['owner']!r} até {current['expires_at']}"
                )
            lease_id = new_id("lease")
            expires = shift(moment, ttl_seconds)
            if current is None:
                conn.execute(
                    "INSERT INTO leases(task_id, lease_id, owner, acquired_at, expires_at, "
                    "heartbeat_at) VALUES (?,?,?,?,?,?)",
                    (task_id, lease_id, owner, moment, expires, moment),
                )
            else:
                conn.execute(
                    "UPDATE leases SET lease_id=?, owner=?, acquired_at=?, expires_at=?, "
                    "heartbeat_at=? WHERE task_id=?",
                    (lease_id, owner, moment, expires, moment, task_id),
                )
            conn.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                (TaskState.LEASED.value, moment, task_id),
            )
            return Lease(task_id, lease_id, owner, moment, expires, moment)

    def heartbeat(
        self, lease_id: str, *, ttl_seconds: float = 300.0, now: str | None = None
    ) -> Lease:
        """Renova a expiração do lease. Lease já substituído ⇒ `LeaseLost`."""
        moment = now or utc_now()
        with self.immediate() as conn:
            row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise LeaseLost(f"lease {lease_id!r} não é mais o lease vigente de nenhuma tarefa")
            expires = shift(moment, ttl_seconds)
            conn.execute(
                "UPDATE leases SET expires_at=?, heartbeat_at=? WHERE lease_id=?",
                (expires, moment, lease_id),
            )
            return Lease(
                row["task_id"], lease_id, row["owner"], row["acquired_at"], expires, moment
            )

    def release_lease(self, lease_id: str, *, now: str | None = None) -> bool:
        """Devolve a tarefa ao pool. Idempotente: liberar duas vezes não é erro."""
        moment = now or utc_now()
        with self.immediate() as conn:
            row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM leases WHERE lease_id=?", (lease_id,))
            conn.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE task_id=? AND state=?",
                (TaskState.READY.value, moment, row["task_id"], TaskState.LEASED.value),
            )
            return True

    def expired_leases(self, now: str | None = None) -> list[Lease]:
        moment = now or utc_now()
        return [
            Lease(**dict(r))
            for r in self.conn.execute(
                "SELECT * FROM leases WHERE expires_at<=? ORDER BY task_id", (moment,)
            )
        ]

    def release_expired_leases(self, now: str | None = None) -> list[str]:
        """Libera leases expirados. Só a tarefa do worker morto volta ao pool.

        Aceite W4 "worker falha ⇒ somente sua tarefa/objetivo é retomado": o
        critério é o lease, que é por tarefa; nada aqui olha para o worker.
        """
        moment = now or utc_now()
        released: list[str] = []
        with self.immediate() as conn:
            rows = conn.execute(
                "SELECT * FROM leases WHERE expires_at<=? ORDER BY task_id", (moment,)
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM leases WHERE lease_id=?", (row["lease_id"],))
                conn.execute(
                    "UPDATE tasks SET state=?, updated_at=? WHERE task_id=? AND state=?",
                    (TaskState.READY.value, moment, row["task_id"], TaskState.LEASED.value),
                )
                released.append(row["task_id"])
        return released

    def _assert_lease(self, conn: sqlite3.Connection, task_id: str, lease_id: str) -> None:
        row = conn.execute(
            "SELECT lease_id FROM leases WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None or row["lease_id"] != lease_id:
            raise LeaseLost(
                f"lease {lease_id!r} não é o lease vigente de {task_id!r}: "
                "resultado de dono antigo não pode sobrescrever o atual"
            )

    # -- tentativas --------------------------------------------------------
    def start_attempt(
        self,
        task_id: str,
        lease_id: str,
        execution_id: str | None = None,
        *,
        now: str | None = None,
    ) -> Attempt:
        """Registra a tentativa com o `execution_id` DEVOLVIDO PELO EXECUTOR (F07).

        É esta linha que transforma "execução conhecida" em fato verificável:
        `attempt_for_execution` só encontra o que passou por aqui, então um
        resultado com `execution_id` inventado pelo worker não tem como casar.

        `execution_id=None` é a tentativa que morreu ANTES de existir execução
        (montagem de contexto ou `submit` que falhou). Ela precisa existir
        assim mesmo: é ela que incrementa `attempt_count` e carrega o
        `error_hash` — sem esse registro, uma falha de despacho recorrente
        nunca esgotaria política nem limite de erro idêntico (§7.4).
        """
        moment = now or utc_now()
        with self.immediate() as conn:
            self._assert_lease(conn, task_id, lease_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM attempts WHERE task_id=?",
                (task_id,),
            ).fetchone()
            attempt_no = int(row["n"])
            conn.execute(
                "INSERT INTO attempts(task_id, attempt_no, execution_id, started_at, outcome) "
                "VALUES (?,?,?,?,?)",
                (task_id, attempt_no, execution_id, moment, AttemptOutcome.RUNNING.value),
            )
            conn.execute(
                "UPDATE tasks SET attempt_count=attempt_count+1, updated_at=? WHERE task_id=?",
                (moment, task_id),
            )
            return Attempt(
                task_id, attempt_no, execution_id, moment, None, AttemptOutcome.RUNNING, "", "", ""
            )

    def attempt_for_execution(self, execution_id: str) -> Attempt | None:
        """Tentativa que originou `execution_id`, ou `None` se a execução é desconhecida."""
        row = self.conn.execute(
            "SELECT * FROM attempts WHERE execution_id=?", (execution_id,)
        ).fetchone()
        return None if row is None else _attempt(row)

    def attempts(self, task_id: str) -> list[Attempt]:
        return [
            _attempt(r)
            for r in self.conn.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no", (task_id,)
            )
        ]

    def same_error_count(self, task_id: str, err_hash: str) -> int:
        """Quantas vezes ESTE erro exato já foi registrado para a tarefa (§7.4)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE task_id=? AND error_hash=? AND error_hash<>''",
            (task_id, err_hash),
        ).fetchone()
        return int(row["n"])

    # -- resultado ---------------------------------------------------------
    def record_result(
        self,
        task_id: str,
        lease_id: str,
        execution_id: str | None,
        *,
        state: TaskState | str,
        outcome: AttemptOutcome | str,
        result: Mapping[str, Any] | None = None,
        termination_reason: str = "",
        error_class: str = "",
        error_detail: str = "",
        error_hash: str = "",
        release: bool = True,
        now: str | None = None,
    ) -> Task:
        """Fecha tentativa + grava resultado + move estado + libera lease NUMA transação.

        Aceite W4 "tarefas concluídas em paralelo não perdem registros": não há
        janela entre "resultado gravado" e "tentativa fechada". Ou os quatro
        efeitos existem, ou nenhum existe.

        Só o dono do lease vigente grava (`_assert_lease`) — resultado de um
        worker cujo lease já expirou e foi retomado por outro é recusado com
        `LeaseLost` em vez de sobrescrever o trabalho de quem está vivo.
        """
        moment = now or utc_now()
        state = TaskState(state)
        outcome = AttemptOutcome(outcome)
        with self.immediate() as conn:
            self._assert_lease(conn, task_id, lease_id)
            if execution_id is not None:
                arow = conn.execute(
                    "SELECT attempt_no FROM attempts WHERE task_id=? AND execution_id=?",
                    (task_id, execution_id),
                ).fetchone()
            else:
                arow = conn.execute(
                    "SELECT attempt_no FROM attempts WHERE task_id=? AND outcome=? "
                    "ORDER BY attempt_no DESC LIMIT 1",
                    (task_id, AttemptOutcome.RUNNING.value),
                ).fetchone()
            if arow is not None:
                conn.execute(
                    "UPDATE attempts SET ended_at=?, outcome=?, error_class=?, error_detail=?, "
                    "error_hash=? WHERE task_id=? AND attempt_no=?",
                    (
                        moment,
                        outcome.value,
                        error_class,
                        error_detail,
                        error_hash,
                        task_id,
                        int(arow["attempt_no"]),
                    ),
                )
            payload = None if result is None else canonical_json(dict(result))
            if state is TaskState.DONE and payload is None:
                raise InvalidTransition(
                    f"tarefa {task_id!r} não pode ir para 'done' sem resultado "
                    "(A01: nenhum estado concluído sem resultado válido)"
                )
            conn.execute(
                "UPDATE tasks SET state=?, result_json=COALESCE(?, result_json), "
                "termination_reason=?, updated_at=? WHERE task_id=?",
                (state.value, payload, termination_reason, moment, task_id),
            )
            if release:
                conn.execute("DELETE FROM leases WHERE lease_id=?", (lease_id,))
            return Task.from_row(
                conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def requeue(
        self, task_id: str, *, reason: str, clear_result: bool = True, now: str | None = None
    ) -> Task:
        """Devolve a tarefa a `pending` (retentativa/invalidação), soltando o lease."""
        moment = now or utc_now()
        with self.immediate() as conn:
            if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise UnknownTask(f"tarefa {task_id!r} não existe em runtime.db")
            conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
            if clear_result:
                conn.execute(
                    "UPDATE tasks SET state=?, result_json=NULL, termination_reason=?, "
                    "updated_at=? WHERE task_id=?",
                    (TaskState.PENDING.value, reason, moment, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET state=?, termination_reason=?, updated_at=? WHERE task_id=?",
                    (TaskState.PENDING.value, reason, moment, task_id),
                )
            return Task.from_row(
                conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    # -- efeitos -----------------------------------------------------------
    def register_effect(
        self, effect_id: str, *, task_id: str | None = None, now: str | None = None
    ) -> str:
        """Espelha um `effect_id` da outbox de `knowledge.db` como `pending`.

        Não há FK nem transação distribuída (§4.2): a revisão e sua outbox
        nascem juntas em `knowledge.db`; aqui só existe o acompanhamento da
        confirmação do destino.
        """
        moment = now or utc_now()
        with self.immediate() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO effects_runtime(effect_id, task_id, state, registered_at) "
                "VALUES (?,?,?,?)",
                (effect_id, task_id, EffectState.PENDING.value, moment),
            )
        return effect_id

    def confirm_effect(
        self, effect_id: str, *, detail: str = "", now: str | None = None
    ) -> bool:
        """Marca o efeito como concluído. Devolve `False` se já estava confirmado.

        Idempotente por construção: a segunda confirmação do mesmo destino não
        move `confirmed_at` nem "reconta" o efeito. É o que permite retomar uma
        escrita interrompida sem duplicar (§4.2, §7.4/escrita interrompida).
        """
        moment = now or utc_now()
        with self.immediate() as conn:
            row = conn.execute(
                "SELECT state FROM effects_runtime WHERE effect_id=?", (effect_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO effects_runtime(effect_id, task_id, state, registered_at, "
                    "confirmed_at, confirmation_detail) VALUES (?,NULL,?,?,?,?)",
                    (effect_id, EffectState.CONFIRMED.value, moment, moment, detail),
                )
                return True
            if row["state"] == EffectState.CONFIRMED.value:
                return False
            conn.execute(
                "UPDATE effects_runtime SET state=?, confirmed_at=?, confirmation_detail=? "
                "WHERE effect_id=?",
                (EffectState.CONFIRMED.value, moment, detail, effect_id),
            )
            return True

    def pending_effects(self) -> list[dict[str, Any]]:
        """Efeitos ainda não confirmados. Sobrevivem a reinício por serem persistidos."""
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM effects_runtime WHERE state=? ORDER BY registered_at, effect_id",
                (EffectState.PENDING.value,),
            )
        ]


# --------------------------------------------------------------------------
# Continuação de investigação parcial (Onda10-C, achado #3 da 2ª auditoria)
# --------------------------------------------------------------------------
#
# Um resultado `partial` (worker devolveu `reading_needs`/`state=partial` em
# vez de fechar o objetivo) não pode gerar retentativa da MESMA tarefa: a
# tarefa já terminou com um resultado aceito (`done`), e §7.2 proíbe reabrir
# tarefa concluída. O que existe em vez disso é uma NOVA tarefa de
# investigação — a "continuação" — que herda o objetivo original acrescido
# das leituras pendentes e de um número de rodada.
#
# A chave contra laço infinito é dupla: (1) `round_no` cresce a partir da
# própria tarefa-mãe (`task_round(parent) + 1`), então repetir a mesma
# chamada para a mesma tarefa-mãe sempre pede a MESMA rodada — e
# `effect_identity` (que inclui objective_id+round+hash(needs)+input_versions)
# faz `create_task` devolver a tarefa já existente em vez de duplicar
# trabalho; (2) `round_no > max_rounds` é recusa peremptória, sem exceção.

#: Chave usada na tabela `policies` (PK livre, sem CHECK) para persistir o
#: teto de rodadas de continuação. Não colide com nenhum `ErrorClass` de
#: `recovery.py` (todos em minúsculas com nomes de classe de erro).
CONTINUATION_ROUNDS_POLICY_KEY = "runtime:continuation_max_rounds"

#: Default do plano: no máximo 3 rodadas de continuação por objetivo antes de
#: exigir decisão humana (o objetivo fica `partial` sem nova tarefa).
DEFAULT_MAX_CONTINUATION_ROUNDS = 3


def get_max_continuation_rounds(store: "TaskStore", *, default: int = DEFAULT_MAX_CONTINUATION_ROUNDS) -> int:
    """Teto de rodadas de continuação persistido em `policies`, ou `default`.

    Leitura pura (não semeia a tabela): ausência de linha é "usar o default",
    igual a como `load_policies` trata classes de erro ausentes antes de
    escrever — aqui não escrevemos até alguém chamar `set_max_continuation_rounds`.
    """
    row = store.conn.execute(
        "SELECT policy_json FROM policies WHERE error_class=?",
        (CONTINUATION_ROUNDS_POLICY_KEY,),
    ).fetchone()
    if row is None:
        return default
    try:
        data = json.loads(row["policy_json"])
        return max(0, int(data.get("max_rounds", default)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def set_max_continuation_rounds(
    store: "TaskStore", max_rounds: int, *, now: str | None = None
) -> int:
    """Persiste o teto de rodadas de continuação. Devolve o valor efetivamente gravado."""
    value = max(0, int(max_rounds))
    moment = now or utc_now()
    with store.immediate() as conn:
        conn.execute(
            "INSERT INTO policies(error_class, policy_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(error_class) DO UPDATE SET policy_json=excluded.policy_json, "
            "updated_at=excluded.updated_at",
            (
                CONTINUATION_ROUNDS_POLICY_KEY,
                canonical_json({"max_rounds": value}),
                moment,
            ),
        )
    return value


def task_round(task: "Task") -> int:
    """Rodada de continuação da PRÓPRIA tarefa: 0 se não é uma continuação.

    Lida com o objetivo já carregado em `Task.objective` — nunca reabre o
    banco. Uma tarefa original (planejada por `plan_from_objectives`) não tem
    `continuation`/`round` no objetivo, então vale 0; a próxima continuação
    dela é `task_round(parent) + 1 == 1`.
    """
    objective = task.objective if isinstance(task.objective, Mapping) else {}
    if not objective.get("continuation"):
        return 0
    try:
        return max(0, int(objective.get("round", 0)))
    except (TypeError, ValueError):
        return 0


def continuation_rounds(store: "TaskStore", objective_id: str) -> int:
    """Maior rodada de continuação já criada para `objective_id` (0 se nenhuma).

    Uso: quem planeja quer saber "em que rodada este objetivo está" sem
    precisar rastrear task_ids manualmente. Varredura em Python (não JSON1):
    `runtime.db` é o grafo operacional de uma auditoria, não uma tabela de
    fatos de grande volume — o custo de desserializar `objective_json` aqui é
    aceitável e evita depender de extensão SQLite opcional.
    """
    best = 0
    rows = store.conn.execute(
        "SELECT objective_json FROM tasks WHERE kind=?",
        (TaskKind.INVESTIGATION.value,),
    )
    for row in rows:
        try:
            objective = json.loads(row["objective_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(objective, dict):
            continue
        if not objective.get("continuation"):
            continue
        if str(objective.get("objective_id") or "") != objective_id:
            continue
        try:
            round_no = int(objective.get("round", 0))
        except (TypeError, ValueError):
            continue
        if round_no > best:
            best = round_no
    return best


def _reading_needs_hash(needs: Sequence[Mapping[str, Any]]) -> str:
    return sha256_hex(canonical_json([dict(n) for n in needs]))


def create_continuation_tasks(
    store: "TaskStore",
    *,
    parent_task_id: str,
    objective_id: str,
    needs: Sequence[Mapping[str, Any]],
    input_versions: Mapping[str, Any],
    budget: Mapping[str, Any] | None = None,
    round_no: int,
    max_rounds: int | None = None,
    config: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> list[str]:
    """Cria a tarefa de continuação (kind=investigation) para leituras pendentes.

    Recusa — devolve `[]`, NUNCA levanta e NUNCA cria tarefa — quando:
      * `needs` está vazio (nada concreto para investigar de novo);
      * `round_no` excede o teto (`max_rounds`, ou o persistido em `policies`
        via `get_max_continuation_rounds` quando `max_rounds` é `None`). Esta
        é a trava contra laço infinito: nenhuma quantidade de chamadas passa
        do teto, porque quem decide `round_no` é `task_round(parent) + 1`
        (monótono na cadeia de tarefas-mãe), não um contador externo.

    `effect_identity` (via `create_task`) inclui objective_id, `round`,
    `hash(needs)` e `input_versions` — chamar de novo com os MESMOS argumentos
    devolve a MESMA tarefa (`Task.reused`) em vez de duplicar (§7.2).

    `depends_on=[parent_task_id]`: a tarefa-mãe já terminou (é dela que veio
    o resultado `partial`), então a continuação nasce imediatamente elegível
    — `refresh_states` a promove a `ready` nesta mesma chamada.
    """
    needs = [dict(n) for n in needs if isinstance(n, Mapping)]
    if not needs:
        return []
    round_no = int(round_no)
    limit = get_max_continuation_rounds(store) if max_rounds is None else max(0, int(max_rounds))
    if round_no > limit:
        return []
    moment = now or utc_now()
    objective = {
        "objective_id": objective_id,
        "continuation": True,
        "round": round_no,
        "needs": needs,
        "parent_task_id": parent_task_id,
    }
    cfg = dict(config or {})
    cfg.setdefault("continuation_needs_hash", _reading_needs_hash(needs))
    depends = (parent_task_id,) if parent_task_id else ()
    task = store.create_task(
        TaskKind.INVESTIGATION,
        objective,
        input_versions,
        depends_on=depends,
        budget=budget,
        config=cfg,
        now=moment,
    )
    store.refresh_states(moment)
    return [task.task_id]


def _attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        task_id=row["task_id"],
        attempt_no=int(row["attempt_no"]),
        execution_id=row["execution_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        outcome=AttemptOutcome(row["outcome"]),
        error_class=row["error_class"],
        error_detail=row["error_detail"],
        error_hash=row["error_hash"],
    )


__all__ = [
    "AttemptOutcome",
    "Attempt",
    "CONTINUATION_ROUNDS_POLICY_KEY",
    "DDL",
    "DEFAULT_MAX_CONTINUATION_ROUNDS",
    "EffectState",
    "InvalidTransition",
    "Lease",
    "LeaseHeld",
    "LeaseLost",
    "RuntimeStoreError",
    "SCHEMA_VERSION",
    "SchemaVersionMismatch",
    "TABLES",
    "TERMINAL_STATES",
    "Task",
    "TaskKind",
    "TaskState",
    "TaskStore",
    "UnknownTask",
    "apply_schema",
    "canonical_json",
    "connect",
    "continuation_rounds",
    "create_continuation_tasks",
    "effect_identity",
    "get_max_continuation_rounds",
    "input_versions_hash",
    "new_id",
    "set_max_continuation_rounds",
    "sha256_hex",
    "shift",
    "task_round",
    "utc_now",
]
