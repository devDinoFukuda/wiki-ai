"""Resultado aceito de tarefa -> fato verificado em `knowledge.db` (achado nº1).

O buraco que este módulo fecha
------------------------------
`runtime.coordinator.accept_result` valida o envelope e deixa a saída do worker
em `tasks.result_json`. E parava ali: nada lia aquele JSON, nada o confrontava
com o código, nada o gravava. Uma investigação podia descobrir "total > 1000
devolve 409", a tarefa terminava `done` — e a regra não existia em
`knowledge.db`, logo não existia na consulta nem na publicação.

O caminho agora é executável e tem três portas, nesta ordem:

| Passo | Função | O que garante |
|---|---|---|
| ler | `collect_results` | só tarefa `done` de investigação/verificação, com resultado; recusada nunca entra |
| confrontar | `results_to_claims` + `analysis.verification.verify_claim` | a afirmação é confrontada com o TRECHO real do snapshot |
| gravar | `integrate` | uma revisão atômica, com o `epistemic` do VEREDITO — nunca o que o worker disse de si |

Invariantes em código, não em prosa
-----------------------------------
1. **O worker nunca se aprova.** `asserted_by` é o worker (`llm:...`);
   `support_recorded_by` só é preenchido a partir de `Verdict.support_recorded_by`
   (`pipeline:verification`). `knowledge.repository._check_support` rejeitaria os
   dois iguais, e `verify_claim` já rebaixa antes disso.
2. **`implemented` exige veredito `supported` sobre evidência executável.**
   `_nature_for` só devolve `FactNature.IMPLEMENTED` quando o veredito é
   `SUPPORTED` e a natureza do próprio veredito é `IMPLEMENTED` — que
   `verify_claim` só emite com `LocationResult.executable`. Afirmação não
   sustentada vira `declared_requirement`/`test_expectation`, que
   `knowledge.query._bucket_of` mantém FORA de `implemented_current`.
3. **Citação que contradiz vira `disputed`, não silêncio.** Vem inteiro de
   `check_support`/`check_consistency`; aqui só se preserva o veredito.
4. **Afirmação sem evidência nunca é `supported`.** Sem `evidence_refs` o
   veredito é `unresolved`, o fato é gravado assim mesmo e a lacuna entra no
   relatório: desconhecido explícito, não omissão (§6.6).
5. **Reintegrar não duplica.** `claim_id` é conteúdo (`objetivo:campo:hash do
   enunciado`), `fact_id` deriva de `(namespace, subject, predicate, scope)` e
   `evidence_id` do localizador. Mesmo resultado ⇒ mesmo `content_hash` ⇒
   `WriteResult.changed=False`.
6. **`partial` nunca vira `complete`.** O estado é RECALCULADO por
   `InvestigationObjective.evaluate()` a partir do contrato preenchido pelo
   resultado; o `state` que o worker declarou só é honrado quando ele PIORA o
   estado (`blocked`).

O que este módulo NÃO faz
-------------------------
Não invoca LLM, não escreve em `runtime.db` (só lê), não publica e não fia
nenhum comando: `integrate` devolve `IntegrationReport` serializável e quem
chama (o CLI) decide o que fazer com ele. Também não inventa entidade de
capacidade quando ela já existe — só cria a que falta, para que reintegração
sobre base já povoada por `wk analyze` não gere revisão de título.

Imports: stdlib + `knowledge` + `analysis` (verification/snapshot/investigation)
+ `runtime.tasks` (leitura). Sem `wk`, `codescan`, `publishing` ou `sbindex`.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from analysis.investigation import (
    CONTRACT_FIELDS,
    CONTRACT_LABELS,
    ContractField,
    ContractFieldStatus,
    InvestigationObjective,
    ObjectiveState,
)
from analysis.snapshot import Snapshot
from analysis.verification import (
    SUPPORT_RECORDER,
    Claim,
    ClaimEvidence,
    Inconsistency,
    LocationResult,
    Verdict,
    VerificationError,
    apply_inconsistencies,
    check_consistency,
    reread_obligations,
    verification_summary,
    verify_claim,
)
from runtime import tasks as rt_tasks

from . import evidence as ev_mod
from .models import (
    ApprovalState,
    ContentKind,
    EntityDraft,
    EntityType,
    EpistemicStatus,
    Evidence,
    FactDraft,
    FactNature,
    KnowledgeError,
    LifecycleStatus,
    RelationDraft,
    RelationType,
    SourceKind,
)

__all__ = [
    "FIELD_PREDICATE_KIND",
    "INTEGRATOR",
    "RULE_ENTITY_TYPE",
    "AcceptedResult",
    "FactWrite",
    "IntegrationReport",
    "ObjectiveOutcome",
    "collect_results",
    "integrate",
    "results_to_claims",
]


#: Autoria da REVISÃO e de qualquer mudança de natureza. Origem `pipeline`
#: (exigida por `repository._check_nature_transition`) e distinta de
#: `SUPPORT_RECORDER`: quem integra não é quem registra sustentação.
INTEGRATOR = "pipeline:knowledge-integrate"

#: `asserted_by` de fallback quando nem a tarefa nem o chamador dizem qual
#: worker afirmou. Prefixo `llm:` deliberado: na dúvida, a afirmação é tratada
#: como saída de modelo — o caminho mais restritivo (§5.3).
DEFAULT_ASSERTED_BY = "llm:worker"

#: §6.3 -> `analysis.verification.PREDICATE_KINDS`. `lacunas` não aparece: um
#: ponto não resolvido é lacuna declarada, nunca afirmação a verificar.
FIELD_PREDICATE_KIND: Mapping[str, str] = {
    "identidade": "contract",
    "gatilho": "contract",
    "precondicoes": "behavior",
    "dados": "data",
    "decisoes": "behavior",
    "persistencia": "behavior",
    "integracoes": "contract",
    "sucesso": "behavior",
    "falhas": "behavior",
    "edge_cases": "behavior",
    "dependencias": "contract",
    "verificacao": "test",
}

#: Campo do contrato -> classe de entidade quando a afirmação traz assunto
#: NOMEADO. Regra de negócio e fluxo são as duas classes do §5.1 que uma
#: investigação de comportamento pode legitimamente criar; o resto continua
#: preso à Capability.
RULE_ENTITY_TYPE: Mapping[str, EntityType] = {
    "decisoes": EntityType.BUSINESS_RULE,
    "falhas": EntityType.BUSINESS_RULE,
    "precondicoes": EntityType.BUSINESS_RULE,
    "edge_cases": EntityType.BUSINESS_RULE,
    "gatilho": EntityType.FLOW,
    "sucesso": EntityType.FLOW,
}

#: Prefixo do `predicate` gravado. Fixo para que a consulta padrão possa
#: separar o que veio de investigação do que veio da extração estrutural.
PREDICATE_PREFIX = "investigacao"

_TASK_KINDS = (rt_tasks.TaskKind.INVESTIGATION, rt_tasks.TaskKind.VERIFICATION)

#: Chaves aceitas como lista de afirmações dentro de um campo do contrato.
_ASSERTION_KEYS = ("afirmacoes", "afirmações", "assertions", "statements", "itens", "items")

#: Chaves aceitas como nome do assunto/regra dentro de uma afirmação.
_NAME_KEYS = ("rule", "regra", "nome", "name", "assunto", "subject", "titulo", "título")

#: Chaves aceitas como enunciado dentro de uma afirmação.
_STATEMENT_KEYS = ("statement", "afirmacao", "afirmação", "texto", "text", "content", "descricao", "descrição")

#: Chaves aceitas como lista de evidências dentro de uma afirmação/campo.
_EVIDENCE_KEYS = ("evidence_refs", "evidencias", "evidências", "evidence", "refs")

_STATEMENT_FIELD_ALIASES: Mapping[str, str] = {
    "condition": "condition",
    "condicao": "condition",
    "condição": "condition",
    "comparator": "comparator",
    "comparador": "comparator",
    "operator": "comparator",
    "operador": "comparator",
    "value": "value",
    "valor": "value",
    "effect": "effect",
    "efeito": "effect",
    "exception": "exception",
    "excecao": "exception",
    "exceção": "exception",
    "erro": "exception",
    "negated": "negated",
    "negado": "negated",
    "literals": "literals",
    "literais": "literals",
    "after": "after",
    "apos": "after",
    "após": "after",
    "exception_to": "exception_to",
    "excecao_de": "exception_to",
}

_CMP_SYMBOL = r"(>=|<=|===|!==|==|!=|<>|=|>|<)"
_TEXT_CMP_SYMBOLIC = re.compile(
    r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)\s*" + _CMP_SYMBOL + r"\s*"
    r"([-+]?(?:\d[\d_]*(?:\.\d+)?|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z_0-9]*"
    r"(?:\.[A-Za-z_][A-Za-z_0-9]*)*))"
)
_TEXT_CMP_WORDS = re.compile(
    r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)\s+"
    r"(maior ou igual a|menor ou igual a|maior que|menor que|igual a|diferente de|acima de|abaixo de)\s+"
    r"([-+]?(?:\d[\d_]*(?:\.\d+)?|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z_0-9]*))",
    re.IGNORECASE,
)
_WORD_COMPARATOR: Mapping[str, str] = {
    "maior ou igual a": ">=",
    "menor ou igual a": "<=",
    "maior que": ">",
    "acima de": ">",
    "menor que": "<",
    "abaixo de": "<",
    "igual a": "==",
    "diferente de": "!=",
}
_TEXT_RAISE = re.compile(
    r"(?:raise[sd]?|throw[sn]?|levanta|lan[cç]a)\s+([^\W\d]\w*(?:\.[^\W\d]\w*)*)",
    re.IGNORECASE,
)
_EXC_NAME = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Fault))\b")
_NAMED_PREFIX = re.compile(r"^\s*([^:\n]{3,80}?)\s*:\s*(\S.*)$", re.DOTALL)
_BULLET = re.compile(r"^\s*(?:[-*•—]|\d+[.)])\s*")
_ASSERT_LINE = re.compile(r"\b(assert|expect|should|it\(|test)", re.IGNORECASE)


# --------------------------------------------------------------------------
# Leitura de `runtime.db`
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptedResult:
    """Um resultado que o coordenador ACEITOU, pronto para virar claims.

    `input_versions_hash` viaja junto porque é a identidade da entrada que o
    resultado descreve: reintegrar o MESMO resultado sobre o MESMO snapshot é
    a operação que precisa ser idempotente, e é este par que a define.
    """

    task_id: str
    kind: str
    objective_id: str
    capability_id: str
    execution_id: str | None
    input_versions: Mapping[str, Any]
    input_versions_hash: str
    output: Mapping[str, Any]
    objective_payload: Mapping[str, Any]
    asserted_by: str
    created_at: str = ""
    updated_at: str = ""

    @property
    def integration_key(self) -> str:
        """Identidade do PAR (resultado, entradas). Estável entre execuções."""
        return _hash(
            rt_tasks.canonical_json(
                {
                    "objective_id": self.objective_id,
                    "input_versions_hash": self.input_versions_hash,
                    "output": dict(self.output),
                }
            )
        )


def _worker_of(task: rt_tasks.Task) -> str:
    """Quem afirmou, a partir da configuração da tarefa.

    Nunca vem do `output`: `coordinator.ResultSchema` é fechado e `asserted_by`
    não está declarado — um worker não escolhe em nome de quem fala.
    """
    config = task.config or {}
    for key in ("asserted_by", "worker", "agent", "agent_slot", "engine"):
        raw = str(config.get(key) or "").strip()
        if not raw:
            continue
        return raw if ":" in raw else f"llm:{raw}"
    return DEFAULT_ASSERTED_BY


def collect_results(
    task_store: rt_tasks.TaskStore,
    objective_ids: Sequence[str] | None = None,
    *,
    latest_only: bool = True,
) -> list[AcceptedResult]:
    """Resultados aceitos em `runtime.db`, prontos para integração.

    Filtra por construção o que NÃO pode virar conhecimento:

    - estado diferente de `done` (recusa e falha nunca chegam aqui: o
      coordenador as move para `failed`/`blocked`);
    - `termination_reason` de recusa (`rejected:*`, `submit:*`), mesmo que a
      linha tenha resultado antigo de outra tentativa;
    - tarefa sem `result_json` (A01 já impede `done` sem resultado);
    - tarefa cuja última tentativa terminou `rejected`.

    `latest_only` mantém apenas a tarefa mais recente por `objective_id`:
    `create_task` nunca ATUALIZA linha existente — código mudado gera tarefa
    NOVA — e integrar as duas gravaria a versão velha por cima da nova.
    """
    wanted = {str(o) for o in objective_ids} if objective_ids is not None else None
    accepted: list[AcceptedResult] = []

    for task in task_store.tasks_in_state(rt_tasks.TaskState.DONE):
        if task.kind not in _TASK_KINDS:
            continue
        if not isinstance(task.result, Mapping) or not task.result:
            continue
        reason = (task.termination_reason or "").strip().lower()
        if reason and reason != "completed":
            continue
        attempts = task_store.attempts(task.task_id)
        if attempts and attempts[-1].outcome is rt_tasks.AttemptOutcome.REJECTED:
            continue
        execution_id = next(
            (
                a.execution_id
                for a in reversed(attempts)
                if a.outcome is rt_tasks.AttemptOutcome.SUCCEEDED
            ),
            None,
        )
        objective_payload = task.objective or {}
        objective_id = str(
            task.result.get("objective_id") or objective_payload.get("objective_id") or ""
        )
        if not objective_id:
            continue
        if wanted is not None and objective_id not in wanted:
            continue
        accepted.append(
            AcceptedResult(
                task_id=task.task_id,
                kind=task.kind.value,
                objective_id=objective_id,
                capability_id=str(
                    task.result.get("capability_id")
                    or objective_payload.get("capability_id")
                    or ""
                ),
                execution_id=execution_id,
                input_versions=dict(task.input_versions or {}),
                input_versions_hash=task.input_versions_hash,
                output=dict(task.result),
                objective_payload=dict(objective_payload),
                asserted_by=_worker_of(task),
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )

    if not latest_only:
        return accepted

    latest: dict[str, AcceptedResult] = {}
    for item in accepted:
        current = latest.get(item.objective_id)
        if current is None or (item.created_at, item.task_id) > (
            current.created_at,
            current.task_id,
        ):
            latest[item.objective_id] = item
    return [latest[k] for k in sorted(latest)]


# --------------------------------------------------------------------------
# Resultado -> claims
# --------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text if text is not None else "")).strip()


def _slug(text: str, limit: int = 60) -> str:
    """Chave estável e legível para `stable_key` de entidade."""
    folded = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "sem-assunto"


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_dicts(container: Any) -> list[dict[str, Any]]:
    if not isinstance(container, Mapping):
        return []
    for key in _EVIDENCE_KEYS:
        raw = container.get(key)
        if isinstance(raw, Mapping):
            return [dict(raw)]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return [dict(e) for e in raw if isinstance(e, Mapping)]
    return []


def _as_evidence_ref_dict(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normaliza para o formato de `analysis.capabilities.EvidenceRef.to_dict`.

    `ContractField.from_dict` reconstrói `EvidenceRef`, que exige
    `path`/`line_start`/`line_end`: um worker que mandar `start_line` (o nome
    do LOCALIZADOR, não o do ref) não pode perder a citação por causa disso.
    """
    citation = _claim_evidence(raw)
    if citation is None:
        return None
    locator = _as_mapping(raw.get("locator"))
    return {
        "path": citation.path,
        "line_start": citation.start_line,
        "line_end": citation.end_line,
        "role": str(raw.get("role") or ""),
        "symbol": raw.get("symbol") or locator.get("symbol"),
        "locator": locator or None,
        "note": str(raw.get("note") or ""),
    }


def _claim_evidence(raw: Mapping[str, Any]) -> ClaimEvidence | None:
    """`EvidenceRef`/localizador do worker -> `ClaimEvidence` verificável.

    Aceita as três formas que aparecem no caminho real: o `to_dict()` de
    `analysis.capabilities.EvidenceRef`, o localizador de código de
    `knowledge.evidence` e o par `path` + `start_line`/`end_line`. Localizador
    sem faixa não vira citação — devolve `None` em vez de faixa inventada.
    """
    locator = _as_mapping(raw.get("locator"))
    source = locator or raw
    path = str(source.get("path") or raw.get("path") or "").strip()
    start = source.get("start_line", source.get("line_start", raw.get("line_start")))
    end = source.get("end_line", source.get("line_end", raw.get("line_end")))
    if not path:
        return None
    try:
        start_line = int(start)
        end_line = int(end if end is not None else start)
    except (TypeError, ValueError):
        return None
    if start_line < 1 or end_line < start_line:
        return None
    declared = raw.get("content_kind") or locator.get("content_kind")
    try:
        content_kind = ContentKind(declared) if declared else ContentKind.EXECUTABLE
    except ValueError:
        content_kind = ContentKind.EXECUTABLE
    snippet_hash = locator.get("snippet_hash") or raw.get("snippet_hash")
    try:
        return ClaimEvidence(
            path=path,
            start_line=start_line,
            end_line=end_line,
            content_kind=content_kind,
            source_kind=SourceKind.CODE,
            snippet_hash=str(snippet_hash) if snippet_hash else None,
        )
    except VerificationError:
        return None


def _split_statements(text: str) -> list[str]:
    """Conteúdo livre de um campo -> uma afirmação por linha/bullet.

    Uma linha é a menor unidade que um worker escreve como afirmação isolada.
    Fundir o campo inteiro num claim só produziria um enunciado que nenhuma
    checagem mecânica alcança.
    """
    out: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = _BULLET.sub("", raw_line).strip()
        if len(line) >= 3:
            out.append(line)
    if not out:
        collapsed = _norm_ws(text)
        if len(collapsed) >= 3:
            out.append(collapsed)
    return out


def _derive_statement_fields(text: str, campo: str) -> dict[str, Any]:
    """Campos mecânicos EXTRAÍDOS do enunciado, quando ele já é estruturado.

    Deliberadamente conservador. Só duas formas são extraídas: a comparação
    (`total > 1000`) e a exceção nomeada (`raise ConflictError`). Derivar
    também o consequente ("→ 409") como literal soaria mais completo e seria
    pior: `check_support` exige que TODA checagem aplicável passe, então um
    literal adivinhado que caia fora da faixa citada rebaixaria um predicado
    genuinamente sustentado. Consequente não extraído continua registrado no
    `value` do fato e visível na consulta.
    """
    statement = _norm_ws(text)
    if not statement:
        return {}

    match = _TEXT_CMP_SYMBOLIC.search(statement)
    if match:
        comparator = match.group(2)
        return {
            "condition": match.group(1),
            "comparator": "==" if comparator == "=" else comparator,
            "value": match.group(3),
        }

    worded = _TEXT_CMP_WORDS.search(statement)
    if worded:
        return {
            "condition": worded.group(1),
            "comparator": _WORD_COMPARATOR[worded.group(2).lower()],
            "value": worded.group(3),
        }

    raised = _TEXT_RAISE.search(statement)
    if raised:
        return {"exception": raised.group(1)}
    if campo == "falhas":
        named = _EXC_NAME.search(statement)
        if named:
            return {"exception": named.group(1)}
    return {}


def _structured_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Campos mecânicos DECLARADOS pelo worker, normalizados para o schema
    fechado de `Claim.statement_fields` (campo desconhecido é ignorado aqui e
    seria rejeitado por `Claim.__post_init__`)."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        target = _STATEMENT_FIELD_ALIASES.get(str(key).strip().lower())
        if target is None or value in (None, "", [], {}):
            continue
        out[target] = value
    return out


def _named_subject(raw: Mapping[str, Any], statement: str) -> str:
    """Assunto nomeado da afirmação — o que vira `BusinessRule`/`Flow`.

    Prefere o campo explícito; só então o prefixo `Nome: enunciado`, e apenas
    quando o prefixo é curto o bastante para ser um nome e não uma frase.
    """
    for key in _NAME_KEYS:
        value = _norm_ws(raw.get(key))
        if value:
            return value
    match = _NAMED_PREFIX.match(statement or "")
    if match:
        candidate = _norm_ws(match.group(1))
        if candidate and len(candidate.split()) <= 8 and not _TEXT_CMP_SYMBOLIC.search(candidate):
            return candidate
    return ""


@dataclass(frozen=True)
class _Assertion:
    """Uma afirmação normalizada, antes de virar `Claim`."""

    campo: str
    statement: str
    subject_name: str
    statement_fields: Mapping[str, Any]
    evidence: tuple[ClaimEvidence, ...]
    symbol: str | None


def _field_payload(output: Mapping[str, Any], name: str) -> Any:
    contract = output.get("contract")
    if isinstance(contract, Mapping):
        return contract.get(name)
    return None


def _merged_field(objective: InvestigationObjective, output: Mapping[str, Any], name: str) -> ContractField:
    """Campo do contrato depois do resultado, como `ContractField` válido."""
    base = objective.contract.get(name)
    data = base.to_dict() if base is not None else {
        "name": name,
        "label": CONTRACT_LABELS.get(name, ""),
        "status": ContractFieldStatus.PENDING.value,
    }
    raw = _field_payload(output, name)
    if isinstance(raw, str):
        raw = {"status": ContractFieldStatus.FILLED.value, "content": raw}
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        raw = {"status": ContractFieldStatus.FILLED.value, "afirmacoes": list(raw)}
    if isinstance(raw, Mapping):
        for key in ("status", "content", "motivo", "impacto", "label"):
            if key in raw and raw[key] not in (None, ""):
                data[key] = raw[key]
        refs = [d for d in (_as_evidence_ref_dict(r) for r in _evidence_dicts(raw)) if d]
        if refs:
            data["evidence_refs"] = refs
    data["name"] = name
    data.setdefault("label", CONTRACT_LABELS.get(name, ""))
    try:
        return ContractField.from_dict(data)
    except Exception:
        return ContractField(name=name, label=CONTRACT_LABELS.get(name, ""))


def _citations(raws: Sequence[Mapping[str, Any]]) -> tuple[tuple[ClaimEvidence, ...], str | None]:
    """Citações verificáveis + o símbolo declarado na primeira delas."""
    out: list[ClaimEvidence] = []
    symbol: str | None = None
    for raw in raws:
        citation = _claim_evidence(raw)
        if citation is None:
            continue
        out.append(citation)
        if symbol is None:
            candidate = raw.get("symbol") or _as_mapping(raw.get("locator")).get("symbol")
            symbol = _norm_ws(candidate) or None
    return tuple(out), symbol


def _assertions_of(
    campo: str, raw: Any, merged: ContractField
) -> list[_Assertion]:
    """Afirmações de UM campo do contrato, venham como lista ou como texto."""
    field_raw_refs = _evidence_dicts(raw if isinstance(raw, Mapping) else {}) or [
        e.to_dict() for e in merged.evidence_refs
    ]
    field_evidence, field_symbol = _citations(field_raw_refs)

    items: list[Any] = []
    if isinstance(raw, Mapping):
        for key in _ASSERTION_KEYS:
            value = raw.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                items = list(value)
                break
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = list(raw)

    if not items:
        content = ""
        if isinstance(raw, Mapping):
            content = str(raw.get("content") or "")
        elif isinstance(raw, str):
            content = raw
        content = content or merged.content
        items = _split_statements(content)

    out: list[_Assertion] = []
    for item in items:
        if isinstance(item, Mapping):
            statement = ""
            for key in _STATEMENT_KEYS:
                statement = _norm_ws(item.get(key))
                if statement:
                    break
            structured = _structured_fields(item)
            if not statement:
                # `value` só serve de enunciado quando não é o operando de uma
                # comparação declarada — senão o mesmo campo seria as duas coisas.
                statement = "" if "comparator" in structured else _norm_ws(item.get("value"))
                if statement:
                    structured.pop("value", None)
            if not statement:
                continue
            own_evidence, own_symbol = _citations(_evidence_dicts(item))
            evidence = own_evidence or field_evidence
            symbol = _norm_ws(item.get("symbol")) or own_symbol or (
                field_symbol if evidence is field_evidence else None
            )
            out.append(
                _Assertion(
                    campo=campo,
                    statement=statement,
                    subject_name=_named_subject(item, statement),
                    statement_fields=structured or _derive_statement_fields(statement, campo),
                    evidence=evidence,
                    symbol=symbol or None,
                )
            )
            continue

        statement = _norm_ws(item)
        if len(statement) < 3:
            continue
        out.append(
            _Assertion(
                campo=campo,
                statement=statement,
                subject_name=_named_subject({}, statement),
                statement_fields=_derive_statement_fields(statement, campo),
                evidence=field_evidence,
                symbol=field_symbol,
            )
        )
    return out


def results_to_claims(
    result: AcceptedResult | Mapping[str, Any],
    objective: InvestigationObjective | Mapping[str, Any] | None = None,
) -> list[Claim]:
    """Contrato §6.3 do resultado -> `Claim`s de `analysis.verification`.

    `predicate_kind` sai de `FIELD_PREDICATE_KIND` (o campo diz que TIPO de
    afirmação é aquilo), `asserted_by` é o worker e `claim_id` é conteúdo:
    `objetivo:campo:hash do enunciado`. Isso é o que faz reintegrar o mesmo
    resultado cair no mesmo `fact_id` em vez de gerar um segundo fato.

    Afirmação sem `evidence_refs` VIRA claim mesmo assim — sem checagem
    aplicável, portanto — para que `verify_claim` a marque `unresolved` e a
    lacuna fique registrada. Descartá-la aqui apagaria a pergunta em aberto.
    """
    output, asserted_by, objective_id = _result_parts(result)
    obj = _objective_of(objective, output, objective_id)
    objective_id = objective_id or obj.objective_id

    claims: list[Claim] = []
    seen: set[str] = set()
    for campo in CONTRACT_FIELDS:
        predicate_kind = FIELD_PREDICATE_KIND.get(campo)
        if predicate_kind is None:  # `lacunas` — não é afirmação a verificar
            continue
        merged = _merged_field(obj, output, campo)
        if merged.status in (ContractFieldStatus.EXCLUDED, ContractFieldStatus.UNRESOLVED):
            continue
        raw = _field_payload(output, campo)
        for assertion in _assertions_of(campo, raw, merged):
            claim_id = f"{objective_id}:{campo}:{_hash(assertion.statement)[:12]}"
            if claim_id in seen:
                continue
            seen.add(claim_id)
            try:
                claims.append(
                    Claim(
                        claim_id=claim_id,
                        subject=assertion.symbol or "",
                        predicate_kind=predicate_kind,
                        statement_fields=dict(assertion.statement_fields),
                        evidence_refs=assertion.evidence,
                        asserted_by=asserted_by,
                        statement=assertion.statement,
                        symbol=assertion.symbol,
                        scope=f"{objective_id}:{campo}",
                    )
                )
            except VerificationError:
                # Campo fora do schema fechado do claim: a afirmação vira
                # claim SEM estrutura mecânica em vez de sumir.
                claims.append(
                    Claim(
                        claim_id=claim_id,
                        subject=assertion.symbol or "",
                        predicate_kind=predicate_kind,
                        statement_fields={},
                        evidence_refs=assertion.evidence,
                        asserted_by=asserted_by,
                        statement=assertion.statement,
                        symbol=assertion.symbol,
                        scope=f"{objective_id}:{campo}",
                    )
                )
    return claims


def _result_parts(
    result: AcceptedResult | Mapping[str, Any]
) -> tuple[dict[str, Any], str, str]:
    if isinstance(result, AcceptedResult):
        return dict(result.output), result.asserted_by, result.objective_id
    data = _as_mapping(result)
    output = _as_mapping(data.get("output")) or data
    asserted_by = str(data.get("asserted_by") or DEFAULT_ASSERTED_BY)
    return output, asserted_by, str(output.get("objective_id") or "")


def _objective_of(
    objective: InvestigationObjective | Mapping[str, Any] | None,
    output: Mapping[str, Any],
    objective_id: str,
) -> InvestigationObjective:
    if isinstance(objective, InvestigationObjective):
        return objective
    if isinstance(objective, Mapping) and objective.get("objective_id"):
        return InvestigationObjective.from_dict(objective)
    return InvestigationObjective(
        objective_id=objective_id or str(output.get("objective_id") or "objetivo"),
        kind="capability",
        capability_id=str(output.get("capability_id") or ""),
        name=str(output.get("capability_id") or objective_id or ""),
    )


# --------------------------------------------------------------------------
# Estado do objetivo (achado nº6)
# --------------------------------------------------------------------------


def _objective_after_result(
    objective: InvestigationObjective, output: Mapping[str, Any]
) -> InvestigationObjective:
    """Objetivo do PLANO + o que o resultado preencheu, sem promoção indevida.

    O `state` declarado pelo worker só entra quando PIORA o estado (`blocked`):
    o estado final vem sempre de `evaluate()`, que exige `unmet_obligations()`
    vazio para `complete`. É por isso que um worker não consegue fechar um
    objetivo declarando-o fechado.
    """
    data = objective.to_dict()
    contract = {name: _merged_field(objective, output, name).to_dict() for name in CONTRACT_FIELDS}
    data["contract"] = contract
    matrix = output.get("matrix")
    if isinstance(matrix, Mapping) and matrix:
        data["matrix"] = dict(matrix)
    declared = str(output.get("state") or "").strip().lower()
    if declared == ObjectiveState.BLOCKED.value:
        data["state"] = ObjectiveState.BLOCKED.value
    elif data.get("state") == ObjectiveState.COMPLETE.value:
        # Nunca reafirmar `complete` vindo do payload: recalcula do zero.
        data["state"] = ObjectiveState.PARTIAL.value
    try:
        return InvestigationObjective.from_dict(data)
    except Exception:
        return objective


def _lacunas_of(
    objective: InvestigationObjective,
    output: Mapping[str, Any],
    verdicts: Sequence[Verdict],
    claims_by_id: Mapping[str, Claim],
) -> list[dict[str, Any]]:
    """Tudo que ficou em aberto, com a origem de cada pendência."""
    out: list[dict[str, Any]] = []
    lacunas_field = objective.contract.get("lacunas")
    if lacunas_field is not None:
        for statement in _split_statements(lacunas_field.content):
            out.append({"origem": "contrato:lacunas", "detalhe": statement})
        if lacunas_field.status is ContractFieldStatus.UNRESOLVED and lacunas_field.impacto:
            out.append({"origem": "contrato:lacunas", "detalhe": lacunas_field.impacto})

    for name in CONTRACT_FIELDS:
        campo = objective.contract[name]
        if campo.status is ContractFieldStatus.UNRESOLVED:
            out.append(
                {
                    "origem": f"campo_nao_resolvido:{name}",
                    "detalhe": campo.impacto or "campo não resolvido sem impacto declarado",
                }
            )
    # Campo `pending` não entra aqui: já é a primeira linha de
    # `unmet_obligations()`, e repetir a mesma pendência em dois lugares faz o
    # relatório parecer maior do que a dívida real.

    for raw in (output.get("gaps"), output.get("unresolved")):
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                detalhe = _norm_ws(item if not isinstance(item, Mapping) else item.get("detail") or item)
                if detalhe:
                    out.append({"origem": "worker", "detalhe": detalhe})
        elif isinstance(raw, str) and raw.strip():
            out.append({"origem": "worker", "detalhe": _norm_ws(raw)})

    for verdict in verdicts:
        if verdict.epistemic is not EpistemicStatus.UNRESOLVED:
            continue
        claim = claims_by_id.get(verdict.claim_id)
        out.append(
            {
                "origem": "afirmacao_sem_sustentacao",
                "claim_id": verdict.claim_id,
                "detalhe": (
                    (claim.statement if claim else verdict.claim_id)
                    + " — "
                    + "; ".join(verdict.reasons[:2])
                ),
            }
        )
    return out


# --------------------------------------------------------------------------
# Escrita em `knowledge.db`
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FactWrite:
    """Um fato gravado (ou reconhecido como idêntico) por esta integração."""

    claim_id: str
    fact_id: str
    subject_id: str
    campo: str
    predicate: str
    epistemic: str
    nature: str
    changed: bool
    evidence_refs: tuple[str, ...] = ()
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "fact_id": self.fact_id,
            "subject_id": self.subject_id,
            "campo": self.campo,
            "predicate": self.predicate,
            "epistemic": self.epistemic,
            "nature": self.nature,
            "changed": self.changed,
            "evidence_refs": list(self.evidence_refs),
            "entity_id": self.entity_id,
        }


@dataclass
class ObjectiveOutcome:
    """Como UM objetivo terminou nesta integração."""

    objective_id: str
    capability_id: str
    subject_id: str | None
    state: str
    unmet: list[str] = field(default_factory=list)
    lacunas: list[dict[str, Any]] = field(default_factory=list)
    fatos: list[FactWrite] = field(default_factory=list)
    inconsistencias: list[dict[str, Any]] = field(default_factory=list)
    entidades: list[dict[str, Any]] = field(default_factory=list)
    relacoes: list[dict[str, Any]] = field(default_factory=list)
    bloqueios: list[str] = field(default_factory=list)
    task_id: str = ""
    execution_id: str | None = None
    integration_key: str = ""

    def _count(self, status: EpistemicStatus) -> int:
        return sum(1 for f in self.fatos if f.epistemic == status.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "capability_id": self.capability_id,
            "subject_id": self.subject_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "integration_key": self.integration_key,
            "state": self.state,
            "fatos_gravados": len(self.fatos),
            "fatos_novos": sum(1 for f in self.fatos if f.changed),
            "supported": self._count(EpistemicStatus.SUPPORTED),
            "inferred": self._count(EpistemicStatus.INFERRED),
            "disputed": self._count(EpistemicStatus.DISPUTED),
            "unresolved": self._count(EpistemicStatus.UNRESOLVED),
            "unmet": list(self.unmet),
            "lacunas": list(self.lacunas),
            "inconsistencias": list(self.inconsistencias),
            "entidades": list(self.entidades),
            "relacoes": list(self.relacoes),
            "bloqueios": list(self.bloqueios),
            "fatos": [f.to_dict() for f in self.fatos],
        }


@dataclass
class IntegrationReport:
    """O que a integração fez — serializável, para o CLI consumir e imprimir."""

    objetivos: list[ObjectiveOutcome] = field(default_factory=list)
    revisao: str | None = None
    mudancas: int = 0
    reread_obligations: list[dict[str, Any]] = field(default_factory=list)
    verificacao: dict[str, Any] = field(default_factory=dict)
    bloqueios: list[str] = field(default_factory=list)

    @property
    def fatos_gravados(self) -> int:
        return sum(len(o.fatos) for o in self.objetivos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revisao": self.revisao,
            "mudancas": self.mudancas,
            "fatos_gravados": self.fatos_gravados,
            "objetivos": [o.to_dict() for o in self.objetivos],
            "reread_obligations": list(self.reread_obligations),
            "verificacao": dict(self.verificacao),
            "bloqueios": list(self.bloqueios),
        }


def _nature_for(claim: Claim, verdict: Verdict, supported: bool) -> FactNature:
    """Natureza do fato. `implemented` SÓ com veredito `supported`.

    `verify_claim` só emite `SUPPORTED` com `nature=IMPLEMENTED` quando a
    localização é executável (§5.4); reproduzir a decisão aqui seria duplicar
    a regra — este ponto apenas se recusa a inventar natureza melhor do que o
    veredito. Sem sustentação, a afirmação continua sendo o que é: algo
    DECLARADO por quem investigou, que `knowledge.query._bucket_of` mantém
    fora de `implemented_current`.
    """
    if supported and verdict.nature is not None:
        return verdict.nature
    if claim.predicate_kind == "test":
        return FactNature.TEST_EXPECTATION
    return FactNature.DECLARED_REQUIREMENT


def _test_assertions(snippet: str) -> list[str]:
    lines = [l.strip() for l in (snippet or "").splitlines() if l.strip()]
    marked = [l for l in lines if _ASSERT_LINE.search(l)]
    return (marked or lines)[:20]


def _evidence_for_location(
    namespace: str, source_version_id: str | None, loc: LocationResult, symbol: str | None
) -> Evidence | None:
    """`LocationResult` verificado -> `knowledge.models.Evidence`.

    O `content_kind` usado é o DETECTADO por `check_location`, nunca o
    declarado: é o que impede um comentário citado de entrar no banco como
    evidência executável. Trecho de teste vira fonte `test` (cujo
    `supports_implemented` é `False` por construção), configuração vira fonte
    `config`.
    """
    if not loc.ok or not loc.locator or not source_version_id:
        return None
    kind = loc.content_kind
    try:
        if kind is ContentKind.TEST_ASSERTION:
            locator: dict[str, Any] = {
                "case": f"{loc.path}:{loc.start_line}-{loc.end_line}",
                "assertions": _test_assertions(loc.snippet),
                "version": source_version_id,
            }
            return ev_mod.make_evidence(
                namespace, SourceKind.TEST, kind, source_version_id, locator
            )
        if kind is ContentKind.CONFIG_VALUE:
            locator = {
                "file": loc.path,
                "key": symbol or f"lines:{loc.start_line}-{loc.end_line}",
                "version": source_version_id,
            }
            return ev_mod.make_evidence(
                namespace, SourceKind.CONFIG, kind, source_version_id, locator
            )
        locator = dict(loc.locator)
        if symbol and "symbol" not in locator:
            locator["symbol"] = symbol
        return ev_mod.make_evidence(
            namespace, SourceKind.CODE, kind, source_version_id, locator
        )
    except KnowledgeError:
        # `LocatorInvalid` (localizador fora do schema §5.4) e qualquer outro
        # invariante: citação que não vira evidência válida não vira evidência
        # aproximada — o claim segue sem sustentação.
        return None


class _SourceVersions:
    """Cache de `source_version_id` por arquivo, com a MESMA fórmula de
    `wk analyze` (`register_source(path)` + versão rotulada `sha256`).

    Sem a mesma fórmula, o fato de investigação citaria uma versão de fonte
    diferente da que a análise estrutural citou para o mesmo arquivo, e a
    invalidação por versão (`knowledge.invalidate`) pegaria só metade.
    """

    def __init__(self, repo: Any, namespace: str, snapshot: Snapshot) -> None:
        self.repo = repo
        self.namespace = namespace
        self.files = snapshot.file_map()
        self.cache: dict[str, str | None] = {}

    def get(self, path: str) -> str | None:
        norm = (path or "").replace("\\", "/").strip("/")
        if norm in self.cache:
            return self.cache[norm]
        entry = self.files.get(norm)
        sha = getattr(entry, "sha256", None) if entry is not None else None
        if not sha:
            self.cache[norm] = None
            return None
        source = self.repo.register_source(self.namespace, SourceKind.CODE, uri=norm)
        version = self.repo.register_source_version(
            source, version_label="sha256", content_hash=sha
        )
        self.cache[norm] = version.source_version_id
        return version.source_version_id


def integrate(
    repo: Any,
    task_store: rt_tasks.TaskStore,
    snapshot: Snapshot,
    extraction: Any,
    namespace: str,
    *,
    objectives: Iterable[Any] | None = None,
    capability_entity_map: Mapping[str, str] | None = None,
    results: Sequence[AcceptedResult] | None = None,
    objective_ids: Sequence[str] | None = None,
    reason: str = "integração de resultados de investigação",
    create_missing_capability: bool = True,
) -> IntegrationReport:
    """Resultados aceitos -> fatos verificados, numa ÚNICA revisão atômica.

    `capability_entity_map` (capability_id -> entity_id) é parâmetro, e não
    consulta ao `wk`: quem chama já resolveu a identidade da capacidade e este
    módulo não deve reimplementar aquela decisão. Sem entrada no mapa, tenta-se
    o próprio `capability_id` como `entity_id` (é a convenção que
    `wk._write_structural_knowledge` usa ao gravar `EntityDraft(entity_id=
    cap.capability_id)`), e só então cria-se a Capability que falta.

    A revisão é uma só de propósito: ou todos os fatos desta integração entram
    com a mesma proveniência, ou nenhum entra (§4.2).
    """
    accepted = list(results) if results is not None else collect_results(
        task_store, objective_ids
    )
    index = _objectives_index(objectives)
    report = IntegrationReport()
    if not accepted:
        report.bloqueios.append(
            "nenhum resultado aceito em runtime.db (tarefas done de investigação/verificação "
            "com resultado): nada a integrar"
        )
        return report

    prepared: list[dict[str, Any]] = []
    all_verdicts: list[Verdict] = []
    for result in accepted:
        base = index.get(result.objective_id)
        if base is None:
            base = _objective_of(result.objective_payload or None, result.output, result.objective_id)
        objective = _objective_after_result(base, result.output)
        claims = results_to_claims(result, objective)
        verdicts = [verify_claim(c, snapshot, extraction) for c in claims]
        inconsistencies: list[Inconsistency] = check_consistency(claims, verdicts)
        verdicts = apply_inconsistencies(verdicts, inconsistencies)
        all_verdicts.extend(verdicts)
        prepared.append(
            {
                "result": result,
                "objective": objective,
                "claims": {c.claim_id: c for c in claims},
                "ordered_claims": claims,
                "verdicts": verdicts,
                "inconsistencies": inconsistencies,
            }
        )

    report.verificacao = verification_summary(all_verdicts)
    report.reread_obligations = _reread_with_objective(prepared)

    versions = _SourceVersions(repo, namespace, snapshot)
    with repo.revision(author=INTEGRATOR, reason=reason) as rev:
        for item in prepared:
            report.objetivos.append(
                _write_objective(
                    rev=rev,
                    repo=repo,
                    namespace=namespace,
                    versions=versions,
                    result=item["result"],
                    objective=item["objective"],
                    claims=item["claims"],
                    verdicts=item["verdicts"],
                    inconsistencies=item["inconsistencies"],
                    capability_entity_map=capability_entity_map or {},
                    create_missing_capability=create_missing_capability,
                )
            )
        report.revisao = rev.revision_id
        report.mudancas = rev.change_count
    return report


def _objectives_index(objectives: Iterable[Any] | None) -> dict[str, InvestigationObjective]:
    out: dict[str, InvestigationObjective] = {}
    for raw in objectives or ():
        try:
            obj = raw if isinstance(raw, InvestigationObjective) else InvestigationObjective.from_dict(raw)
        except Exception:
            continue
        out[obj.objective_id] = obj
    return out


def _reread_with_objective(prepared: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Obrigações de releitura, cada uma sabendo de qual objetivo veio."""
    out: list[dict[str, Any]] = []
    for item in prepared:
        objective: InvestigationObjective = item["objective"]
        claims: Mapping[str, Claim] = item["claims"]
        for obligation in reread_obligations(item["verdicts"]):
            claim = claims.get(str(obligation.get("claim_id")))
            obligation["objective_id"] = objective.objective_id
            obligation["capability_id"] = objective.capability_id
            obligation["statement"] = claim.statement if claim else ""
            out.append(obligation)
    return out


def _capability_subject(
    rev: Any,
    repo: Any,
    namespace: str,
    objective: InvestigationObjective,
    capability_entity_map: Mapping[str, str],
    create_missing: bool,
    outcome: ObjectiveOutcome,
) -> str | None:
    """Entidade à qual os fatos deste objetivo se prendem.

    Só cria a Capability quando ela NÃO existe: recriar com título próprio
    geraria uma revisão de entidade a cada integração sobre uma base que
    `wk analyze` já povoou — idempotência aparente é pior que nenhuma.
    """
    mapped = str(capability_entity_map.get(objective.capability_id) or "").strip()
    if mapped:
        if repo.entity_exists(mapped):
            return mapped
        outcome.bloqueios.append(
            f"capability_entity_map aponta para entidade inexistente: {mapped!r}"
        )
    if objective.capability_id and repo.entity_exists(objective.capability_id):
        return objective.capability_id
    if not create_missing:
        outcome.bloqueios.append(
            f"capacidade {objective.capability_id!r} sem entidade em knowledge.db: "
            "fatos deste objetivo não foram gravados"
        )
        return None

    stable_key = f"cap:{objective.capability_id or objective.objective_id}"
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=EntityType.CAPABILITY,
            stable_key=stable_key,
            title=objective.name or objective.capability_id or objective.objective_id,
            entity_id=objective.capability_id or None,
            attributes={"objective_id": objective.objective_id, "origem": "integracao"},
        )
    )
    outcome.entidades.append(
        {"entity_id": write.target_id, "tipo": EntityType.CAPABILITY.value, "novo": write.changed}
    )
    return write.target_id


def _rule_entity(
    rev: Any,
    namespace: str,
    objective: InvestigationObjective,
    campo: str,
    subject_name: str,
    capability_subject: str,
    evidence_ids: Sequence[str],
    verdict: Verdict,
    outcome: ObjectiveOutcome,
) -> str:
    """`BusinessRule`/`Flow` nomeada, com `implements` para a capacidade.

    Identidade determinística por `capability + assunto` (`stable_key`), então
    a mesma regra reafirmada em outra execução revisita a MESMA entidade em vez
    de criar uma homônima.
    """
    entity_type = RULE_ENTITY_TYPE.get(campo, EntityType.BUSINESS_RULE)
    stable_key = f"{entity_type.value.lower()}:{objective.capability_id or objective.objective_id}:{_slug(subject_name)}"
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=entity_type,
            stable_key=stable_key,
            title=subject_name,
            attributes={"campo": campo, "objective_id": objective.objective_id},
            evidence_refs=tuple(evidence_ids),
        )
    )
    outcome.entidades.append(
        {"entity_id": write.target_id, "tipo": entity_type.value, "novo": write.changed, "titulo": subject_name}
    )

    supported = verdict.epistemic is EpistemicStatus.SUPPORTED and bool(evidence_ids)
    relation = rev.put_relation(
        RelationDraft(
            namespace=namespace,
            source_entity_id=write.target_id,
            relation_type=RelationType.IMPLEMENTS,
            target_entity_id=capability_subject,
            scope=objective.objective_id,
            epistemic_status=EpistemicStatus.SUPPORTED if supported else EpistemicStatus.INFERRED,
            lifecycle_status=LifecycleStatus.CURRENT,
            asserted_by=verdict.asserted_by or DEFAULT_ASSERTED_BY,
            support_recorded_by=SUPPORT_RECORDER if supported else None,
            evidence_refs=tuple(evidence_ids) if supported else (),
        )
    )
    outcome.relacoes.append(
        {
            "relation_id": relation.target_id,
            "tipo": RelationType.IMPLEMENTS.value,
            "de": write.target_id,
            "para": capability_subject,
            "novo": relation.changed,
        }
    )
    return write.target_id


def _verifies_relation(
    rev: Any,
    namespace: str,
    objective: InvestigationObjective,
    subject_id: str,
    verdict: Verdict,
    versions: _SourceVersions,
    evidence_ids: Sequence[str],
    outcome: ObjectiveOutcome,
) -> None:
    """Arquivo de teste citado -> `Source` -> `verifies` o sujeito do claim.

    Só nasce de citação que RESOLVEU e cujo conteúdo foi classificado como
    assertion de teste: sem isso, "existe teste" viraria afirmação sem fonte.
    """
    test_locations = [
        loc for loc in verdict.locations if loc.ok and loc.content_kind is ContentKind.TEST_ASSERTION
    ]
    if not test_locations:
        return
    loc = test_locations[0]
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=EntityType.SOURCE,
            stable_key=f"source:{loc.path}",
            title=loc.path.rsplit("/", 1)[-1],
            source_version_id=versions.get(loc.path),
            attributes={"path": loc.path, "papel": "teste"},
            evidence_refs=tuple(evidence_ids),
        )
    )
    supported = verdict.epistemic is EpistemicStatus.SUPPORTED and bool(evidence_ids)
    relation = rev.put_relation(
        RelationDraft(
            namespace=namespace,
            source_entity_id=write.target_id,
            relation_type=RelationType.VERIFIES,
            target_entity_id=subject_id,
            scope=objective.objective_id,
            epistemic_status=EpistemicStatus.SUPPORTED if supported else EpistemicStatus.INFERRED,
            lifecycle_status=LifecycleStatus.CURRENT,
            asserted_by=verdict.asserted_by or DEFAULT_ASSERTED_BY,
            support_recorded_by=SUPPORT_RECORDER if supported else None,
            evidence_refs=tuple(evidence_ids) if supported else (),
        )
    )
    outcome.entidades.append(
        {"entity_id": write.target_id, "tipo": EntityType.SOURCE.value, "novo": write.changed}
    )
    outcome.relacoes.append(
        {
            "relation_id": relation.target_id,
            "tipo": RelationType.VERIFIES.value,
            "de": write.target_id,
            "para": subject_id,
            "novo": relation.changed,
        }
    )


def _write_objective(
    *,
    rev: Any,
    repo: Any,
    namespace: str,
    versions: _SourceVersions,
    result: AcceptedResult,
    objective: InvestigationObjective,
    claims: Mapping[str, Claim],
    verdicts: Sequence[Verdict],
    inconsistencies: Sequence[Inconsistency],
    capability_entity_map: Mapping[str, str],
    create_missing_capability: bool,
) -> ObjectiveOutcome:
    """Grava os fatos de UM objetivo e recalcula seu estado (§6.6)."""
    state = objective.evaluate()
    outcome = ObjectiveOutcome(
        objective_id=objective.objective_id,
        capability_id=objective.capability_id,
        subject_id=None,
        state=state.value,
        unmet=objective.unmet_obligations(),
        lacunas=_lacunas_of(objective, result.output, verdicts, claims),
        inconsistencias=[i.as_dict() for i in inconsistencies],
        task_id=result.task_id,
        execution_id=result.execution_id,
        integration_key=result.integration_key,
    )

    capability_subject = _capability_subject(
        rev, repo, namespace, objective, capability_entity_map, create_missing_capability, outcome
    )
    outcome.subject_id = capability_subject
    if capability_subject is None:
        return outcome

    for verdict in verdicts:
        claim = claims.get(verdict.claim_id)
        if claim is None:
            continue
        _write_claim(
            rev=rev,
            namespace=namespace,
            versions=versions,
            result=result,
            objective=objective,
            claim=claim,
            verdict=verdict,
            capability_subject=capability_subject,
            outcome=outcome,
        )
    return outcome


def _record_evidence(
    rev: Any, namespace: str, versions: _SourceVersions, verdict: Verdict, symbol: str | None
) -> tuple[list[str], str | None]:
    """Localizações do veredito -> linhas de `evidence`, com a versão da fonte.

    Só localização RESOLVIDA vira evidência; citação que não resolve some daqui
    e reaparece como lacuna no relatório — o oposto de gravar uma referência
    que não volta a ser verificável.
    """
    evidence_ids: list[str] = []
    source_version_id: str | None = None
    for loc in verdict.locations:
        svid = versions.get(loc.path)
        evidence = _evidence_for_location(namespace, svid, loc, symbol)
        if evidence is None:
            continue
        evidence_ids.append(rev.add_evidence(evidence))
        source_version_id = source_version_id or svid
    return evidence_ids, source_version_id


def _write_claim(
    *,
    rev: Any,
    namespace: str,
    versions: _SourceVersions,
    result: AcceptedResult,
    objective: InvestigationObjective,
    claim: Claim,
    verdict: Verdict,
    capability_subject: str,
    outcome: ObjectiveOutcome,
) -> None:
    """Um claim verificado -> um fato, com o `epistemic` do VEREDITO."""
    campo = claim.scope.rsplit(":", 1)[-1] if ":" in claim.scope else ""
    evidence_ids, source_version_id = _record_evidence(
        rev, namespace, versions, verdict, claim.symbol
    )

    subject_id = capability_subject
    subject_name = _subject_name_of(claim, result.output, campo)
    entity_id: str | None = None
    if subject_name and campo in RULE_ENTITY_TYPE:
        entity_id = _rule_entity(
            rev, namespace, objective, campo, subject_name, capability_subject,
            evidence_ids, verdict, outcome,
        )
        subject_id = entity_id

    # Veredito `supported` cuja citação não virou linha de `evidence` (arquivo
    # fora do snapshot, localizador irrecuperável) é rebaixado AQUI:
    # `repository._check_support` recusaria depois, e recusar tarde derrubaria
    # a revisão inteira em vez de registrar o que se sabe.
    supported = verdict.epistemic is EpistemicStatus.SUPPORTED and bool(evidence_ids)
    epistemic = (
        verdict.epistemic
        if supported or verdict.epistemic is not EpistemicStatus.SUPPORTED
        else EpistemicStatus.INFERRED
    )
    nature = _nature_for(claim, verdict, supported)
    predicate = f"{PREDICATE_PREFIX}.{campo}" if campo else PREDICATE_PREFIX

    try:
        write = rev.put_fact(
            FactDraft(
                namespace=namespace,
                subject_id=subject_id,
                predicate=predicate,
                value=claim.statement or claim.claim_id,
                scope=claim.claim_id,
                nature=nature,
                epistemic_status=epistemic,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by=claim.asserted_by or result.asserted_by,
                evidence_refs=tuple(evidence_ids),
                source_version_id=source_version_id,
                support_recorded_by=(
                    (verdict.support_recorded_by or SUPPORT_RECORDER) if supported else None
                ),
                approval_state=ApprovalState.NONE,
                nature_change_recorded_by=INTEGRATOR,
            )
        )
    except KnowledgeError as exc:
        # Invariante do repositório recusou ESTE fato: registra a recusa e
        # segue. Deixar a exceção subir abortaria a revisão inteira e perderia
        # os fatos legítimos do mesmo objetivo.
        outcome.bloqueios.append(f"{claim.claim_id}: {type(exc).__name__}: {exc}")
        return

    outcome.fatos.append(
        FactWrite(
            claim_id=claim.claim_id,
            fact_id=write.target_id,
            subject_id=subject_id,
            campo=campo,
            predicate=predicate,
            epistemic=epistemic.value,
            nature=nature.value,
            changed=write.changed,
            evidence_refs=tuple(evidence_ids),
            entity_id=entity_id,
        )
    )

    if claim.predicate_kind == "test":
        _verifies_relation(
            rev, namespace, objective, subject_id, verdict, versions, evidence_ids, outcome
        )


def _subject_name_of(claim: Claim, output: Mapping[str, Any], campo: str) -> str:
    """Assunto nomeado da afirmação, recuperado do enunciado.

    Reconstruído (e não guardado do parsing) porque `Claim` é o contrato de
    `analysis.verification` e não carrega vocabulário de entidade — colocar o
    nome lá acoplaria os dois módulos por um campo que a verificação ignora.
    """
    raw = _field_payload(output, campo)
    if isinstance(raw, Mapping):
        for key in _ASSERTION_KEYS:
            items = raw.get(key)
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    for skey in _STATEMENT_KEYS:
                        if _norm_ws(item.get(skey)) == claim.statement:
                            return _named_subject(item, claim.statement)
    return _named_subject({}, claim.statement)
