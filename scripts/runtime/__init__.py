"""Runtime operacional do wiki-ai: tarefas, coordenação e retomada (plano §4.2, §7.2, §7.4, onda W4).

Este pacote é o *grafo de tarefas operacionais*. Ele é SEPARADO do grafo de
conhecimento (§7.2): uma tarefa `done` não cria fato nem revisão. Integrar um
resultado ao `knowledge.db` é passo explícito de quem consome `result_json` —
por isso nenhum módulo daqui importa `knowledge.repository` para escrever.

Divisão do pacote
-----------------
| Módulo          | Responsabilidade                                              |
|-----------------|---------------------------------------------------------------|
| `tasks.py`      | `runtime.db`: tarefas, dependências, leases, tentativas, efeitos, idempotência |
| `coordinator.py`| Laço determinístico: planejar → agendar → submeter → validar → gravar |
| `recovery.py`   | Classes de erro, políticas persistidas, retomada (`resume`)    |
| `context.py`    | (vizinho, NÃO deste dono) montagem e orçamento de pacotes      |
| `executors/`    | (vizinho, NÃO deste dono) adapters reais de engine             |

Contratos dos vizinhos (assumidos; import é TARDIO e DEFENSIVO para que este
pacote seja validável antes de `context.py`/`executors/` existirem)
-------------------------------------------------------------------
`runtime.context`:

    build_package(objective_dict, budget, resolver) -> Package
        Package.payload_bytes : bytes  — payload efetivo medido em bytes
        Package.token_estimate: int
        Package.exact_tokens  : bool   — False ⇒ estimativa com margem (§7.3.1)
        Package.parts         : Sequence — partição quando o objetivo foi dividido
        Package.refs          : Sequence — referências resolvíveis enviadas
    Levanta `BudgetExceeded` com `.suggestion` = partição sugerida (§7.4/orçamento).

`runtime.executors`:

    class AgentExecutor(Protocol):
        capabilities() -> dict
        submit(task_id, objective, references, schema, policy) -> str  # execution_id EMITIDO PELO EXECUTOR
        status(execution_id) -> {"state": str, "heartbeat": str|None}
        result(execution_id) -> {"execution_id": str, "output": dict}
        cancel(execution_id) -> bool

F07 (§7.3/§13.1): o `execution_id` é sempre o devolvido por `submit`. Resultado
cuja execução é desconhecida, ou cuja revisão de input diverge do snapshot da
tarefa, é REJEITADO e registrado — nunca aceito por vir "de dentro" do payload.

Só stdlib; pode ler `knowledge`/`analysis` por contrato. Sem `wk`/`codescan`/`sbindex`.
"""

from __future__ import annotations

__all__ = ["tasks", "coordinator", "recovery"]
