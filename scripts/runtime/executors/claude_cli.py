"""`ClaudeCliExecutor`: adapter da engine LLM via CLI oficial headless (plano §7.3).

Integra por CLI oficialmente disponibilizada — nunca por automação de
interface interativa. `detect()` sonda o binário de verdade (`shutil.which`
+ `<binário> --version` com timeout curto); sem sondagem OK, `capabilities()`
reporta `dispatch: False` com o motivo, e o bloqueio é logado UMA vez (não a
cada chamada) para não virar ciclo de "copiar prompt manualmente" como
operação normal (§7.3).

`binary_path` é injetável de propósito: a demonstração ad-hoc usa um script
Python fake no lugar do binário `claude` real (ver bloco `__main__`), porque
a validação aqui NUNCA invoca a engine de verdade.

`submit()`:
- monta o prompt do pacote: um JSON (task_id+objective+references+schema)
  seguido, quando o pacote enviado em `references` traz `parts` (trechos de
  código montados pelo context builder), de uma seção "EVIDÊNCIAS" com cada
  parte — localizador `path:linhas` + conteúdo — na ordem do pacote (achado
  ALTO nº4 da auditoria: sem isso o worker via `ref_id`/`part_id` sem o
  trecho correspondente);
- gera `execution_id="claude-cli:<uuid4>"` ANTES do fork e registra (F07);
- inicia `subprocess.Popen([...,"-p", prompt, "--output-format", "json"])`
  de forma assíncrona — sem `shell=True`, cwd isolado em `tempfile.mkdtemp`,
  env mínimo (sem herdar todo o ambiente do processo pai);
- uma thread interna espera o processo e faz o parse do stdout; `status()`
  e `result()` só leem o registro, nunca bloqueiam esperando a engine.

Somente stdlib. Nenhum import de `wk`/`codescan`/`sbindex`. Nenhuma chamada
de rede/AWS; nenhuma invocação real de LLM na validação deste módulo.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, List, Mapping, Optional, Sequence, Union

from .base import BaseExecutor, ExecutionRecord, ExecutionState, ExecutorUnavailableError

logger = logging.getLogger(__name__)

# Variáveis de ambiente preservadas no subprocesso (env mínimo, não o
# ambiente inteiro do pai). Cobre o necessário para localizar o executável e
# escrever em diretório temporário, em POSIX e Windows.
_ENV_SAFELIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "COMSPEC",
    }
)

_DEFAULT_TIMEOUT_S = 60.0
_DETECT_TIMEOUT_S = 5.0
_STDERR_SUMMARY_LIMIT = 2000


def _minimal_env() -> dict:
    return {k: v for k, v in os.environ.items() if k.upper() in _ENV_SAFELIST}


BinaryPath = Union[str, Sequence[str]]


def _as_command(binary_path: BinaryPath) -> List[str]:
    if isinstance(binary_path, str):
        return [binary_path]
    return list(binary_path)


# --------------------------------------------------------------------------
# Achado ALTO nº4 (auditoria externa) — as `parts` do pacote (trechos de
# código montados pelo context builder) precisam chegar ao prompt do
# worker, não só as `refs`. `runtime.coordinator._references` manda o
# Package inteiro (`to_json()`: objective_id+refs+parts+limites) como o
# único item de `references`; aqui só se LÊ `parts` de dentro dele — nada é
# re-truncado (o teto F05 já foi aplicado pelo context builder).
# --------------------------------------------------------------------------


def _extract_parts(references: Optional[Sequence[Any]]) -> List[Mapping[str, Any]]:
    """Extrai `parts` do pacote completo enviado em `references`, se houver.

    Compatibilidade: `references` sem pacote montado (lista de refs "crua",
    vazia, ou `None`) devolve lista vazia — igual ao comportamento anterior
    a este pacote passar a viajar inteiro.
    """
    if not references:
        return []
    parts: List[Mapping[str, Any]] = []
    for item in references:
        if isinstance(item, Mapping) and isinstance(item.get("parts"), (list, tuple)):
            parts.extend(p for p in item["parts"] if isinstance(p, Mapping))
    return parts


def _format_locator(part: Mapping[str, Any]) -> str:
    """`path:linha_inicial-linha_final`, com o `locator` do resolvedor anexado
    quando presente (commit/outro identificador — formato do vizinho)."""
    path = part.get("path", "")
    start = part.get("line_start", "")
    end = part.get("line_end", "")
    locator_str = f"{path}:{start}-{end}"
    extra = part.get("locator")
    if extra:
        locator_str += " " + json.dumps(extra, ensure_ascii=False, sort_keys=True)
    return locator_str


def _format_evidence_section(parts: Sequence[Mapping[str, Any]]) -> str:
    """Seção "EVIDÊNCIAS" do prompt: cada `part`, na ordem do pacote, com seu
    localizador (path:linhas) seguido do conteúdo (`snippet`).

    Sem esta seção o worker recebia (via `refs`) `ref_id`/`part_id` sem o
    trecho correspondente — exatamente o achado ALTO nº4 da auditoria.
    """
    if not parts:
        return ""
    blocks = ["EVIDÊNCIAS:"]
    for part in parts:
        blocks.append(f"--- {_format_locator(part)} ---")
        blocks.append(str(part.get("snippet", "")))
    return "\n".join(blocks)


class ClaudeCliExecutor(BaseExecutor):
    """Adapter headless: `claude -p <prompt> --output-format json`.

    `binary_path` aceita uma string (nome/caminho de um único executável,
    ex.: `"claude"`) ou uma sequência (comando com prefixo, ex.:
    `[sys.executable, "fake_claude.py"]`) — a demonstração ad-hoc usa a
    segunda forma para injetar um script fake sem depender do binário real.
    """

    def __init__(
        self,
        binary_path: BinaryPath = "claude",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        workdir_root: Optional[str] = None,
    ):
        super().__init__(prefix="claude-cli")
        self._binary_path = binary_path
        self._default_timeout_s = timeout_s
        self._workdir_root = workdir_root
        self._threads: dict[str, threading.Thread] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._workdirs: dict[str, str] = {}
        self._blocked_logged = False
        self._detected, self._detect_reason = self._detect()

    # -- detecção real (nunca "compatível por suposição") ---------------------

    def _detect(self) -> tuple[bool, Optional[str]]:
        command = _as_command(self._binary_path)
        if not command:
            return False, "binary_path vazio"

        resolved = shutil.which(command[0])
        if resolved is None:
            return False, f"binário não encontrado no PATH: {command[0]!r}"

        try:
            proc = subprocess.run(
                [*command, "--version"],
                cwd=tempfile.gettempdir(),
                env=_minimal_env(),
                capture_output=True,
                text=True,
                timeout=_DETECT_TIMEOUT_S,
                shell=False,
            )
        except FileNotFoundError:
            return False, f"binário não executável: {command[0]!r}"
        except subprocess.TimeoutExpired:
            return False, f"'{command[0]} --version' excedeu {_DETECT_TIMEOUT_S}s"
        except OSError as exc:
            return False, f"falha ao sondar binário: {type(exc).__name__}: {exc}"

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:_STDERR_SUMMARY_LIMIT]
            return False, f"'{command[0]} --version' saiu com código {proc.returncode}: {stderr}"

        return True, None

    # -- capabilities: só o que foi sondado -----------------------------------

    def capabilities(self) -> dict:
        if not self._detected:
            if not self._blocked_logged:
                logger.warning(
                    "ClaudeCliExecutor sem despacho disponível: %s. "
                    "Configure o binário e reinicie — não voltar ao ciclo de "
                    "copiar prompt manualmente como operação normal (plano §7.3).",
                    self._detect_reason,
                )
                self._blocked_logged = True
            return {
                "dispatch": False,
                "concurrency": 0,
                "cancellation": "process-kill",
                "tools": [],
                "structured_output": False,
                "telemetry": False,
                "reason": self._detect_reason,
                # Onda11-T2a: sem despacho não há aprofundamento possível —
                # `plan_continuations` já recusaria por `dispatch: False`
                # antes de chegar a olhar `deepening`, mas a chave fica
                # explícita (nunca ausente) para quem só olha capabilities().
                "deepening": False,
            }
        return {
            "dispatch": True,
            "concurrency": None,  # engine externa: sem limite conhecido imposto por este adapter
            "cancellation": "process-kill",
            "tools": [],
            "structured_output": True,
            "telemetry": False,
            # Onda11-T2a (achado BLOQUEANTE #2, 3ª auditoria, parte runtime):
            # binário detectado e funcional (F07 `_detect`) — a engine LÊ o
            # prompt+evidências enviados e pode aprofundar investigação numa
            # continuação, ao contrário de `LocalThreadExecutor`.
            "deepening": True,
        }

    # -- submit ------------------------------------------------------------------

    def submit(
        self,
        task_id: str,
        objective: Any,
        references: Optional[Sequence[Any]] = None,
        schema: Optional[Mapping[str, Any]] = None,
        policy: Optional[Mapping[str, Any]] = None,
    ) -> str:
        if not self._detected:
            raise ExecutorUnavailableError(
                f"ClaudeCliExecutor sem despacho disponível: {self._detect_reason}"
            )

        validated_policy = self._validate_policy(policy)
        timeout_s = _coerce_timeout(validated_policy.get("timeout_s"), self._default_timeout_s)

        refs_list = list(references) if references is not None else []
        payload = {
            "task_id": task_id,
            "objective": objective,
            "references": refs_list,
            "schema": schema,
        }
        prompt = json.dumps(payload, ensure_ascii=False)
        # Achado ALTO nº4: as `parts` (trechos de código) do pacote precisam
        # chegar ao prompt de verdade, não só sobreviver dentro do JSON de
        # `references` — por isso ganham seção própria, explícita, anexada
        # ao prompt (path:linhas + conteúdo, na ordem do pacote).
        evidence_section = _format_evidence_section(_extract_parts(refs_list))
        if evidence_section:
            prompt = f"{prompt}\n\n{evidence_section}"

        execution_id = self._new_execution_id()  # emitido ANTES do fork (F07)
        record = ExecutionRecord(
            execution_id=execution_id,
            task_id=task_id,
            objective=objective,
            references=references,
            schema=schema,
            policy=validated_policy,
        )
        self._register(record)

        workdir = tempfile.mkdtemp(prefix="claude-cli-", dir=self._workdir_root)
        self._workdirs[execution_id] = workdir

        command = [*_as_command(self._binary_path), "-p", prompt, "--output-format", "json"]
        try:
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                env=_minimal_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
        except OSError as exc:
            with self._lock:
                record.state = ExecutionState.FAILED
                record.error = f"falha ao iniciar subprocesso: {type(exc).__name__}: {exc}"
                record.heartbeat_at = time.time()
            return execution_id

        with self._lock:
            record.state = ExecutionState.RUNNING
            record.heartbeat_at = time.time()
        self._procs[execution_id] = proc

        thread = threading.Thread(
            target=self._wait_and_parse,
            args=(execution_id, proc, timeout_s),
            name=f"claude-cli-wait-{execution_id}",
            daemon=True,
        )
        self._threads[execution_id] = thread
        thread.start()
        return execution_id

    def _wait_and_parse(self, execution_id: str, proc: subprocess.Popen, timeout_s: float) -> None:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self._finish(
                execution_id,
                state=ExecutionState.FAILED,
                error=f"timeout após {timeout_s}s — processo encerrado",
            )
            self._cleanup_workdir(execution_id)
            return

        if proc.returncode != 0:
            self._finish(
                execution_id,
                state=ExecutionState.FAILED,
                error=f"código de saída {proc.returncode}: {(stderr or '').strip()[:_STDERR_SUMMARY_LIMIT]}",
            )
            self._cleanup_workdir(execution_id)
            return

        try:
            output = json.loads(stdout)
        except json.JSONDecodeError as exc:
            self._finish(
                execution_id,
                state=ExecutionState.FAILED,
                error=(
                    f"stdout não é JSON válido ({exc}); stderr: "
                    f"{(stderr or '').strip()[:_STDERR_SUMMARY_LIMIT]}"
                ),
            )
            self._cleanup_workdir(execution_id)
            return

        self._finish(execution_id, state=ExecutionState.DONE, output=output)
        self._cleanup_workdir(execution_id)

    def _finish(
        self,
        execution_id: str,
        state: str,
        output: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            record = self._records.get(execution_id)
            if record is None:
                return
            # Cancelamento pode ter chegado primeiro; não sobrescrever.
            if record.state == ExecutionState.CANCELLED:
                return
            record.state = state
            record.output = output
            record.error = error
            record.heartbeat_at = time.time()

    def _cleanup_workdir(self, execution_id: str) -> None:
        workdir = self._workdirs.pop(execution_id, None)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- status/result: herdados de BaseExecutor (F07 via _get_owned) -----------

    # -- cancel --------------------------------------------------------------------

    def cancel(self, execution_id: str) -> bool:
        record = self._get_owned(execution_id)  # levanta UnknownExecutionError se alheio
        with self._lock:
            if record.state in ExecutionState.TERMINAL:
                return False
            record.state = ExecutionState.CANCELLED
            record.heartbeat_at = time.time()
        proc = self._procs.get(execution_id)
        if proc is not None and proc.poll() is None:
            proc.kill()
        return True


def _coerce_timeout(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
