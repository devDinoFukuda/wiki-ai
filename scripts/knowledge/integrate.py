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
7. **A frase inteira vale pelo componente MAIS FRACO.** Uma frase condicional
   tem condição *e* consequência; aprovar a condição e gravar a frase toda como
   sustentada é o achado nº1 da 2ª auditoria ("total > 1000 é aprovado com HTTP
   200" virava `supported` porque `total > 1000` existe no código que devolve
   409). `_consequence_of` extrai a consequência, `_consequence_guard` exige que
   ela seja confirmada MECANICAMENTE no trecho, e rebaixa (`inferred`) ou
   contradiz (`disputed`) a frase inteira quando não é.
7b. **Condição e efeito precisam estar no MESMO RAMO.** Confirmar a consequência
   no trecho não bastava: em `if total > 1000: return 409` seguido de
   `return 200`, "quando total > 1000 retorna 200" citando a função inteira
   achava o `200` — no ramo errado — e saía `supported` (achado nº1 da 3ª
   auditoria). `_branch_association` localiza no `ast` o `if` cujo teste é a
   condição afirmada (inversão de operandos conta) e só confirma o efeito dentro
   daquele ramo; efeito no caminho oposto não confirma, ramo com efeito
   incompatível vira `disputed`, e condição não localizável ou trecho não-Python
   têm teto `inferred`.
8. **Integração é ESCOPADA.** `integrate` exige `objectives`/`objective_ids` e
   descarta (em `IntegrationReport.descartados`) resultado de objetivo fora do
   escopo ou cujas entradas não pertencem ao snapshot corrente. Dois repositórios
   no mesmo store deixaram de se contaminar: sem escopo, `collect_results`
   devolvia toda tarefa `done` do runtime.db, inclusive as do outro repositório.
9. **Fato nunca aponta entidade de outro namespace.** `_capability_subject` e
   `_write_claim` conferem o namespace da entidade ANTES de gravar; citação cujo
   caminho não existe no snapshot corrente rejeita o claim com motivo.

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

import ast
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace
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
    check_location,
    extract_effects,
    reread_obligations,
    verification_summary,
    verify_claim,
)
# Reuso deliberado das PRIMITIVAS de `verification` (nunca reimplementadas aqui):
# `_parse_python` é o mesmo parser defensivo (dedent + wrap em função/classe) que
# `extract_comparisons`/`extract_effects` usam, e `_operand_equal` é a MESMA
# igualdade de operandos que `check_support` aplica (`self.x` ≡ `x`, `0` ≡ `0.0`).
# Duplicar qualquer uma faria a associação condição→ramo divergir da checagem que
# aprovou a condição — que é justamente o buraco do achado nº1.
from analysis.verification import _operand_equal as _operand_equal
from analysis.verification import _parse_python as _parse_python_fragment
from runtime import tasks as rt_tasks

from . import evidence as ev_mod
from . import identity as id_mod
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

#: Verbos que abrem a CONSEQUÊNCIA de uma frase (o "então" do "se ... então").
#: Chaves em minúsculas SEM acento (o texto é dobrado por `_accent_fold` antes
#: da busca, porque o worker escreve tanto "é aprovado" quanto "e aprovado").
#: Valor: `(efeito observável que confirmaria a consequência, polaridade)`.
#: A polaridade só serve para DESCREVER divergência: consequência positiva
#: ("aprovado") contra trecho que levanta exceção nomeada é contradição.
_CONSEQUENCE_VERBS: Mapping[str, tuple[str, str]] = {
    "devolve": ("return", "neutral"), "devolvem": ("return", "neutral"),
    "devolver": ("return", "neutral"), "devolvido": ("return", "neutral"),
    "retorna": ("return", "neutral"), "retornam": ("return", "neutral"),
    "retornar": ("return", "neutral"), "retornado": ("return", "neutral"),
    "responde": ("return", "neutral"), "responde com": ("return", "neutral"),
    "return": ("return", "neutral"), "returns": ("return", "neutral"),
    "aprova": ("return", "positive"), "aprovado": ("return", "positive"),
    "aprovada": ("return", "positive"), "aceito": ("return", "positive"),
    "aceita": ("return", "positive"), "autoriza": ("return", "positive"),
    "autorizado": ("return", "positive"), "permitido": ("return", "positive"),
    "rejeita": ("raise", "negative"), "rejeitado": ("raise", "negative"),
    "rejeitada": ("raise", "negative"), "recusa": ("raise", "negative"),
    "recusado": ("raise", "negative"), "nega": ("raise", "negative"),
    "negado": ("raise", "negative"), "bloqueia": ("raise", "negative"),
    "bloqueado": ("raise", "negative"), "impede": ("raise", "negative"),
    "falha": ("raise", "negative"), "cancelado": ("state", "negative"),
    "lanca": ("raise", "negative"), "lança": ("raise", "negative"),
    "levanta": ("raise", "negative"), "dispara": ("raise", "negative"),
    "raise": ("raise", "negative"), "raises": ("raise", "negative"),
    "throw": ("raise", "negative"), "throws": ("raise", "negative"),
    "notifica": ("publish", "neutral"), "notificar": ("publish", "neutral"),
    "avisa": ("publish", "neutral"), "envia": ("publish", "neutral"),
    "publica": ("publish", "neutral"), "emite": ("publish", "neutral"),
    "grava": ("write", "neutral"), "salva": ("write", "neutral"),
    "persiste": ("write", "neutral"), "registra": ("write", "neutral"),
    "atualiza": ("write", "neutral"),
    "vira": ("state", "neutral"), "muda para": ("state", "neutral"),
    "passa a": ("state", "neutral"), "torna-se": ("state", "neutral"),
    "fica": ("state", "neutral"), "marcado como": ("state", "neutral"),
}
_CONSEQUENCE_VERB_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(re.escape(v) for v in sorted(_CONSEQUENCE_VERBS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
#: Código de status (HTTP e afins) citado na consequência. Literal mecânico por
#: excelência: ou o número está no trecho, ou não está.
_STATUS_CODE = re.compile(r"(?<![\w.])([1-5]\d{2})(?![\w.])")
_STATUS_WORD = re.compile(r"\b(?:HTTP|status|c[oó]digo|code)\b\s*[:=]?\s*(\d{3})\b", re.IGNORECASE)
#: Nome de estado citado (`APROVADO`, `PENDING_REVIEW`) ou literal entre aspas.
_STATE_TOKEN = re.compile(r"(?<![\w])([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)*)(?![\w])")
_QUOTED = re.compile(r"'([^']{1,60})'|\"([^\"]{1,60})\"")
#: Fim da cláusula de consequência: outra oração começa.
_CLAUSE_END = re.compile(r"\s+(?:exceto|salvo|a menos que|caso contrario|porem|mas)\b|[;.]")
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


def _expected_hash_for(
    expected: str | Mapping[str, str] | None, objective_id: str
) -> str:
    """Hash de entradas esperado para ESTE objetivo (str única ou mapa)."""
    if expected is None:
        return ""
    if isinstance(expected, Mapping):
        return str(expected.get(objective_id) or "")
    return str(expected)


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
    expected_input_versions_hash: str | Mapping[str, str] | None = None,
) -> list[AcceptedResult]:
    """Resultados aceitos em `runtime.db`, prontos para integração.

    Filtra por construção o que NÃO pode virar conhecimento:

    - estado diferente de `done` (recusa e falha nunca chegam aqui: o
      coordenador as move para `failed`/`blocked`);
    - `termination_reason` de recusa (`rejected:*`, `submit:*`), mesmo que a
      linha tenha resultado antigo de outra tentativa;
    - tarefa sem `result_json` (A01 já impede `done` sem resultado);
    - tarefa cuja última tentativa terminou `rejected`;
    - `objective_id` fora de `objective_ids`, quando dado;
    - `input_versions_hash` diferente do esperado, quando dado.

    `objective_ids` é opcional AQUI (a função também serve para inspeção), mas
    `integrate` o exige: um `runtime.db` guarda as tarefas de TODOS os
    repositórios já analisados no mesmo store, e integrar sem escopo foi o que
    fez a integração do repositório B processar o objetivo do repositório A
    (achado nº5).

    `expected_input_versions_hash` aceita um hash único ou um mapa
    `objective_id -> hash`: resultado que descreve OUTRA versão das entradas
    descreve outro código, e reintegrá-lo grava conhecimento vencido.

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
        expected = _expected_hash_for(expected_input_versions_hash, objective_id)
        if expected and expected != task.input_versions_hash:
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


def _accent_fold(text: str) -> str:
    """Remove diacríticos PRESERVANDO o comprimento em caracteres.

    NFKD decompõe `é` em `e` + combining; descartar só os combining devolve uma
    string com o mesmo número de caracteres para o português corrente, então os
    offsets do casamento no texto dobrado indexam o texto ORIGINAL. É o que
    permite detectar o verbo em "é aprovado" e recortar a cláusula no original.
    """
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    )
    return folded if len(folded) == len(text or "") else (text or "")


@dataclass(frozen=True)
class _Consequence:
    """A parte da frase que diz O QUE ACONTECE — o "então" do "se ... então".

    `literals` são os valores mecanicamente confrontáveis com o trecho (código
    de status, nome de estado, literal citado). `effect_kind` é o efeito que
    confirmaria a consequência quando ela não traz literal nenhum. `verifiable`
    é falso quando a consequência existe mas não produziu NADA mecânico — o
    caso em que o claim inteiro não pode sair `supported`.
    """

    text: str
    verb: str
    effect_kind: str
    polarity: str
    literals: tuple[str, ...] = ()
    exception: str | None = None

    @property
    def verifiable(self) -> bool:
        return bool(self.literals or self.exception or self.effect_kind in _MECHANICAL_EFFECTS)


#: Efeitos que `analysis.verification.extract_effects` sabe observar no trecho.
#: `state` fica de fora: "vira aprovado" sem literal não tem forma mecânica.
_MECHANICAL_EFFECTS: frozenset[str] = frozenset({"return", "raise", "publish", "write"})


def _consequence_of(statement: str, start: int = 0) -> _Consequence | None:
    """Consequência declarada no enunciado, a partir de `start` (§achado nº1).

    `start` é o fim da CONDIÇÃO já extraída: procurar o verbo depois dela evita
    confundir o operando da comparação com o valor da consequência.
    """
    text = statement or ""
    folded = _accent_fold(text)
    match = _CONSEQUENCE_VERB_RE.search(folded, start)
    if match is None and start <= 0:
        # Sem verbo de consequência E sem condição antes: a frase inteira é uma
        # afirmação só. Colher "literais" dela seria inventar consequência onde
        # o enunciado não declara nenhuma — e inventar checagem que PASSA é pior
        # que não checar (promoveria claim hoje `unresolved` a `supported`).
        return None
    clause_start = match.start() if match else start
    end_match = _CLAUSE_END.search(folded, clause_start + (len(match.group(1)) if match else 0))
    clause = text[clause_start : end_match.start() if end_match else len(text)]
    if not clause.strip():
        return None

    literals: list[str] = []
    for m in _STATUS_WORD.finditer(clause):
        literals.append(m.group(1))
    for m in _STATUS_CODE.finditer(clause):
        if m.group(1) not in literals:
            literals.append(m.group(1))
    for m in _QUOTED.finditer(clause):
        value = m.group(1) or m.group(2) or ""
        if value and value not in literals:
            literals.append(value)

    exception = None
    raised = _TEXT_RAISE.search(clause)
    if raised:
        exception = raised.group(1)
    else:
        named = _EXC_NAME.search(clause)
        if named:
            exception = named.group(1)

    if match is None:
        # Sem verbo: só é consequência se houver valor mecânico solto depois da
        # condição ("Se total > 1000, HTTP 409").
        if not literals and exception is None:
            return None
        return _Consequence(
            text=_norm_ws(clause), verb="", effect_kind="", polarity="neutral",
            literals=tuple(literals), exception=exception,
        )

    verb = _accent_fold(match.group(1)).lower()
    effect_kind, polarity = _CONSEQUENCE_VERBS.get(verb, ("", "neutral"))
    if effect_kind == "state" and not literals:
        for m in _STATE_TOKEN.finditer(clause):
            if m.group(1) not in literals:
                literals.append(m.group(1))
    return _Consequence(
        text=_norm_ws(clause),
        verb=verb,
        effect_kind=effect_kind,
        polarity=polarity,
        literals=tuple(literals),
        exception=exception,
    )


def _statement_parse(text: str, campo: str = "") -> tuple[dict[str, Any], _Consequence | None]:
    """Enunciado -> (campos mecânicos, consequência). Nunca para na 1ª extração.

    A versão anterior RETORNAVA na comparação e a consequência ficava fora do
    claim: `check_support` aprovava `total > 1000`, e `_write_claim` gravava a
    FRASE INTEIRA ("... é aprovado com HTTP 200") como sustentada, contra um
    trecho que devolve 409. Aqui condição, consequência e exceção são derivadas
    juntas — e a consequência volta ao chamador para que a regra de sustentação
    (`_consequence_guard`) possa julgar a frase pelo componente mais fraco.
    """
    statement = _norm_ws(text)
    if not statement:
        return {}, None

    fields: dict[str, Any] = {}
    end = 0
    match = _TEXT_CMP_SYMBOLIC.search(statement)
    if match:
        comparator = match.group(2)
        fields["condition"] = match.group(1)
        fields["comparator"] = "==" if comparator == "=" else comparator
        fields["value"] = match.group(3)
        end = match.end()
    else:
        worded = _TEXT_CMP_WORDS.search(statement)
        if worded:
            fields["condition"] = worded.group(1)
            fields["comparator"] = _WORD_COMPARATOR[worded.group(2).lower()]
            fields["value"] = worded.group(3)
            end = worded.end()

    consequence = _consequence_of(statement, end)
    if consequence is not None:
        # `literals` é a forma mecânica da consequência que NÃO presume a
        # gramática do efeito: "devolve 409" contra `raise ConflictError(409)`
        # continua sustentado (o valor está lá), enquanto "aprovado com HTTP
        # 200" contra o mesmo trecho falha — que é exatamente o achado nº1.
        # `effect` NÃO é derivado por isto: `_check_effect` exigiria um `return`
        # no trecho e rebaixaria o primeiro caso, verdadeiro. O efeito é
        # confrontado em `_consequence_confirmed`, com `extract_effects`.
        if consequence.literals:
            fields["literals"] = list(consequence.literals)
        if consequence.exception:
            fields["exception"] = consequence.exception

    if "exception" not in fields:
        raised = _TEXT_RAISE.search(statement)
        if raised:
            fields["exception"] = raised.group(1)
        elif campo == "falhas":
            named = _EXC_NAME.search(statement)
            if named:
                fields["exception"] = named.group(1)
    return fields, consequence


def _derive_statement_fields(text: str, campo: str) -> dict[str, Any]:
    """Campos mecânicos EXTRAÍDOS do enunciado (condição + consequência)."""
    return _statement_parse(text, campo)[0]


# --------------------------------------------------------------------------
# Regra de sustentação da FRASE INTEIRA (achado nº1 da 2ª auditoria)
# --------------------------------------------------------------------------

def _literal_in(text: str, snippet: str) -> bool:
    """Mesma semântica de `verification._check_literals`: substring normalizada."""
    if not text:
        return False
    if text in (snippet or ""):
        return True
    if _norm_ws(text) and _norm_ws(text) in _norm_ws(snippet):
        return True
    collapsed = re.sub(r"\s+", "", snippet or "")
    compact = re.sub(r"\s+", "", text)
    return bool(compact) and compact in collapsed


def _consequence_in(cons: _Consequence, code: str, path: str) -> bool:
    """A consequência aparece MECANICAMENTE neste PEDAÇO de código?

    `code` é um trecho citado inteiro OU o corpo de um ramo (a associação
    condição→ramo usa a MESMA regra de confirmação, para que "está no trecho" e
    "está no ramo" não divirjam). Literal presente basta e é deliberado:
    "devolve 409" contra `raise ConflictError(409)` continua sustentado — o valor
    afirmado ESTÁ ali, e exigir a forma `return` reprovaria uma afirmação
    verdadeira. O que não passa é o valor AUSENTE ("HTTP 200" contra o mesmo
    trecho).
    """
    if not cons.verifiable:
        return False
    if cons.literals and not all(_literal_in(lit, code) for lit in cons.literals):
        return False
    if cons.exception:
        effects = extract_effects(code, path)
        leaf = cons.exception.rsplit(".", 1)[-1]
        if not any(r.rsplit(".", 1)[-1] == leaf for r in effects.raises if r):
            return False
    if not cons.literals and not cons.exception:
        effects = extract_effects(code, path)
        observed = {
            "return": effects.returns,
            "raise": effects.raises,
            "publish": tuple(effects.publishes),
            "write": tuple(effects.writes),
        }.get(cons.effect_kind, ())
        if not observed:
            return False
    return True


def _consequence_confirmed(cons: _Consequence, verdict: Verdict) -> bool:
    """A consequência aparece em alguma citação que resolveu?

    Regra de PRESENÇA, sem vínculo de controle: só é usada quando o enunciado
    NÃO traz condição mecânica (frase não condicional). Frase condicional passa
    por `_branch_association` — presença no trecho não prova que o efeito
    pertence ao ramo da condição (achado nº1 da 3ª auditoria).
    """
    if not cons.verifiable:
        return False
    return any(
        loc.ok and _consequence_in(cons, loc.snippet, loc.path) for loc in verdict.locations
    )


# --------------------------------------------------------------------------
# Associação condição -> ramo -> efeito (achado nº1 da 3ª auditoria)
# --------------------------------------------------------------------------
#
# O buraco: `_consequence_confirmed` respondia "o literal afirmado está no
# trecho?". Para
#
#     def approve(total):
#         if total > 1000:
#             return {"status": 409}
#         return {"status": 200}
#
# e o claim "quando total > 1000, retorna HTTP 200" citando a função INTEIRA, a
# resposta era SIM — o `200` está no trecho, no ramo ERRADO. Condição e
# consequência em ramos diferentes viravam `supported`. Aqui a pergunta passa a
# ser "o efeito está DENTRO do ramo governado pela condição?", respondida pela
# gramática (`ast`), nunca por proximidade textual.

#: Comparador espelhado quando os operandos trocam de lado (`a > b` ≡ `b < a`).
_MIRROR_CMP: Mapping[str, str] = {
    ">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "!=": "!=",
}
#: Comparador negado — o que vale no caminho COMPLEMENTAR do mesmo `if`.
_NEGATE_CMP: Mapping[str, str] = {
    ">": "<=", "<": ">=", ">=": "<", "<=": ">", "==": "!=", "!=": "==",
}
#: Comparadores do enunciado que não vêm normalizados de `_statement_parse`.
_CMP_CANON: Mapping[str, str] = {"===": "==", "!==": "!=", "<>": "!=", "=": "=="}
_AST_CMP_SYM: Mapping[type, str] = {
    ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
    ast.Eq: "==", ast.NotEq: "!=", ast.Is: "==", ast.IsNot: "!=",
}

#: Vereditos da associação. Só os dois primeiros deixam a frase `supported`.
_ASSOC_INSIDE = "inside"            # efeito DENTRO do ramo da condição
_ASSOC_COMPLEMENT = "complement"    # condição complementar + efeito no else/fallthrough do MESMO if
_ASSOC_CONTRARY = "contrary"        # o ramo faz outra coisa, incompatível
_ASSOC_OUTSIDE = "outside"          # efeito existe, mas fora do ramo
_ASSOC_UNDETERMINED = "undetermined"  # condição não localizada como ramo no trecho
_ASSOC_HEURISTIC = "heuristic"      # trecho não parseável como Python
_ASSOC_NONE = ""                    # enunciado sem condição mecânica: regra antiga


@dataclass(frozen=True)
class _Condition:
    """A condição do enunciado em forma comparável com um `if` do código."""

    left: str
    op: str
    right: str

    def render(self) -> str:
        return f"{self.left} {self.op} {self.right}"


def _condition_of(fields: Mapping[str, Any]) -> _Condition | None:
    """Condição mecânica do enunciado, ou `None` quando a frase não traz uma."""
    left = _norm_ws(fields.get("condition"))
    right = _norm_ws(fields.get("value"))
    op = _norm_ws(fields.get("comparator"))
    op = _CMP_CANON.get(op, op)
    if not left or not right or op not in _NEGATE_CMP:
        return None
    return _Condition(left, op, right)


@dataclass(frozen=True)
class _Branch:
    """Um ramo do código: o que roda QUANDO a condição do claim vale.

    `taken` é o caminho governado pela condição afirmada; `other` é o caminho
    oposto — usado só para DIZER que a consequência apareceu do lado errado.
    """

    taken: tuple[Any, ...]
    other: tuple[Any, ...]
    test_src: str
    via: str
    complementary: bool = False


def _src(node: Any) -> str:
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError, TypeError, RecursionError):
        return ""


def _block_src(stmts: Sequence[Any]) -> str:
    return "\n".join(s for s in (_src(node) for node in stmts) if s)


def _stmt_lists(tree: ast.AST) -> Iterable[list[Any]]:
    """Todo bloco de statements do trecho (corpo, else, finally) — inclusive
    aninhados, porque o efeito pode estar num `if` interno do mesmo ramo."""
    for node in ast.walk(tree):
        for name in ("body", "orelse", "finalbody"):
            seq = getattr(node, name, None)
            if isinstance(seq, list) and all(isinstance(s, ast.stmt) for s in seq):
                yield seq


def _terminates(stmts: Sequence[Any]) -> bool:
    """O bloco SEMPRE sai (return/raise)?

    É a condição mecânica para o `fallthrough` — o código DEPOIS do `if`, sem
    `else` — ser o caminho complementar: se o corpo sempre retorna/levanta, o que
    vem depois só executa quando a condição é FALSA. Sem isso, "depois do if" não
    prova nada e a associação fica indeterminada.
    """
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If):
        return _terminates(last.body) and _terminates(last.orelse)
    return False


def _and_parts(node: Any, negated: bool) -> Iterable[tuple[Any, bool]]:
    """Condições que precisam TODAS valer para entrar no corpo.

    `and` distribui (entrar no corpo implica cada conjunto verdadeiro); `or` NÃO
    (entrar não implica o ramo específico), então só é aberto sob negação, onde
    `not (a or b)` ≡ `not a and not b`.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from _and_parts(node.operand, not negated)
        return
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And) and not negated:
            for value in node.values:
                yield from _and_parts(value, False)
            return
        if isinstance(node.op, ast.Or) and negated:
            for value in node.values:
                yield from _and_parts(value, True)
            return
    yield node, negated


def _cmp_same(sym: str, left: str, right: str, cond: _Condition) -> bool:
    """`left sym right` é a MESMA comparação que a condição do enunciado?

    Inversão de operandos conta: `total > 1000` ≡ `1000 < total`.
    """
    if sym == cond.op and _operand_equal(left, cond.left) and _operand_equal(right, cond.right):
        return True
    return (
        sym == _MIRROR_CMP[cond.op]
        and _operand_equal(left, cond.right)
        and _operand_equal(right, cond.left)
    )


def _test_relation(test: Any, cond: _Condition) -> str | None:
    """`direct` (o teste é a condição), `complement` (é a negação dela) ou None."""
    for part, negated in _and_parts(test, False):
        if not isinstance(part, ast.Compare) or len(part.ops) != 1:
            continue
        sym = _AST_CMP_SYM.get(type(part.ops[0]))
        if sym is None:
            continue
        if negated:
            sym = _NEGATE_CMP[sym]
        left, right = _src(part.left), _src(part.comparators[0])
        if _cmp_same(sym, left, right, cond):
            return "direct"
        if _cmp_same(_NEGATE_CMP[sym], left, right, cond):
            return "complement"
    return None


def _branches_for(tree: ast.AST, cond: _Condition) -> list[_Branch]:
    """Ramos do trecho governados pela condição do enunciado."""
    out: list[_Branch] = []
    for body in _stmt_lists(tree):
        for idx, node in enumerate(body):
            if not isinstance(node, ast.If):
                continue
            relation = _test_relation(node.test, cond)
            if relation is None:
                continue
            fall = tuple(body[idx + 1:]) if (not node.orelse and _terminates(node.body)) else ()
            test_src = _src(node.test)
            if relation == "direct":
                out.append(
                    _Branch(tuple(node.body), tuple(node.orelse) + fall, test_src, "corpo do if")
                )
                continue
            complement = tuple(node.orelse) or fall
            if complement:
                out.append(
                    _Branch(
                        complement,
                        tuple(node.body),
                        test_src,
                        "else/fallthrough imediato do mesmo if",
                        complementary=True,
                    )
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp):
            continue
        relation = _test_relation(node.test, cond)
        if relation is None:
            continue
        yes, no = (ast.Expr(node.body),), (ast.Expr(node.orelse),)
        direct = relation == "direct"
        out.append(
            _Branch(
                yes if direct else no,
                no if direct else yes,
                _src(node.test),
                "ramo do ternário",
                complementary=not direct,
            )
        )
    return out


def _branch_contradiction(cons: _Consequence, code: str, path: str) -> str:
    """O ramo faz algo INCOMPATÍVEL com a consequência afirmada? O quê."""
    codes = {lit for lit in cons.literals if _STATUS_CODE.fullmatch(lit or "")}
    found = {m.group(1) for m in _STATUS_CODE.finditer(code or "")}
    if codes and found and not (codes & found):
        return f"usa código {', '.join(sorted(found))}, não {', '.join(sorted(codes))}"
    effects = extract_effects(code, path)
    named = sorted({r for r in effects.raises if r})
    if cons.exception:
        # Guard-rail simétrico: exceção nomeada segue a MESMA regra de ramo.
        leaf = cons.exception.rsplit(".", 1)[-1]
        if named and effects.raises_all_named and not any(
            r.rsplit(".", 1)[-1] == leaf for r in named
        ):
            return f"levanta {', '.join(named)}, não {cons.exception}"
    elif cons.polarity == "positive" and named and effects.raises_all_named:
        return f"levanta {', '.join(named)} (consequência afirmada é de aprovação/retorno)"
    return ""


def _branch_association(cons: _Consequence, cond: _Condition, verdict: Verdict) -> tuple[str, str]:
    """(veredito, motivo) do vínculo condição→ramo→efeito nas citações que resolveram.

    Precedência: uma citação que CONFIRMA vence; senão contradição de ramo vence
    "só existe fora do ramo", que vence "condição não localizada". Nenhum
    resultado que não seja `inside`/`complement` pode deixar a frase `supported`.
    """
    best: tuple[str, str] | None = None
    order = {
        _ASSOC_CONTRARY: 0,
        _ASSOC_OUTSIDE: 1,
        _ASSOC_UNDETERMINED: 2,
        _ASSOC_HEURISTIC: 3,
    }
    for loc in verdict.locations:
        if not loc.ok:
            continue
        assoc, why = _branch_association_at(cons, cond, loc.snippet, loc.path, loc.language)
        why = f"{why} (trecho em {loc.span})"
        if assoc in (_ASSOC_INSIDE, _ASSOC_COMPLEMENT):
            return assoc, why
        if best is None or order[assoc] < order[best[0]]:
            best = (assoc, why)
    if best is None:
        return _ASSOC_UNDETERMINED, "nenhuma citação resolvida para associar condição e efeito"
    return best


def _branch_association_at(
    cons: _Consequence, cond: _Condition, snippet: str, path: str, language: str = ""
) -> tuple[str, str]:
    """Associação condição→ramo→efeito DENTRO de um trecho citado."""
    if language and language != "python":
        return (
            _ASSOC_HEURISTIC,
            f"trecho em {language}: associação condição→ramo só é resolvida por gramática Python",
        )
    tree = _parse_python_fragment(snippet or "")
    if tree is None:
        return (
            _ASSOC_HEURISTIC,
            "trecho não parseável como Python: associação condição→ramo seria heurística",
        )
    branches = _branches_for(tree, cond)
    if not branches:
        return (
            _ASSOC_UNDETERMINED,
            f"condição `{cond.render()}` não localizada como ramo (if/ternário) no trecho",
        )
    contrary = ""
    outside = ""
    for branch in branches:
        taken = _block_src(branch.taken)
        if _consequence_in(cons, taken, path):
            assoc = _ASSOC_COMPLEMENT if branch.complementary else _ASSOC_INSIDE
            return assoc, f"efeito afirmado está no {branch.via} de `{branch.test_src}`"
        why = _branch_contradiction(cons, taken, path)
        if why and not contrary:
            contrary = f"o {branch.via} de `{branch.test_src}` {why}"
        if not outside and _consequence_in(cons, _block_src(branch.other), path):
            outside = (
                f"a consequência aparece APENAS FORA do {branch.via} de `{branch.test_src}` "
                "(outro caminho de execução)"
            )
    if contrary:
        return _ASSOC_CONTRARY, contrary
    if outside:
        return _ASSOC_OUTSIDE, outside
    return (
        _ASSOC_OUTSIDE,
        f"efeito afirmado não observado no ramo de `{cond.render()}`",
    )


def _contrary_consequence(cons: _Consequence, verdict: Verdict) -> str:
    """O trecho mostra consequência CONTRÁRIA à afirmada? Devolve a divergência.

    Duas formas mecânicas, ambas do achado nº1: código de status afirmado que
    não é nenhum dos códigos do trecho, e consequência de aprovação/retorno
    contra trecho que só levanta exceção nomeada.
    """
    codes = {lit for lit in cons.literals if _STATUS_CODE.fullmatch(lit or "")}
    for loc in verdict.locations:
        if not loc.ok:
            continue
        if codes:
            found = {m.group(1) for m in _STATUS_CODE.finditer(loc.snippet or "")}
            if found and not (codes & found):
                return (
                    f"afirmado código {', '.join(sorted(codes))}; o trecho em {loc.span} usa "
                    f"{', '.join(sorted(found))}"
                )
        if cons.polarity == "positive":
            effects = extract_effects(loc.snippet, loc.path)
            named = sorted({r for r in effects.raises if r})
            if named and effects.raises_all_named:
                return (
                    f"consequência afirmada é de aprovação/retorno; o trecho em {loc.span} "
                    f"levanta {', '.join(named)}"
                )
    return ""


def _consequence_guard(claim: Claim, verdict: Verdict) -> tuple[Verdict, str]:
    """Veredito da frase INTEIRA + o escopo efetivamente sustentado (achado nº1).

    Reprodução da auditoria: código `if total > 1000: raise Conflict(409)`,
    worker devolve "Quando total > 1000, o pedido é aprovado com HTTP 200".
    `check_support` aprovava a comparação — que existe mesmo — e a frase inteira
    era gravada `supported/implemented`. Aqui a frase só continua sustentada se
    a CONSEQUÊNCIA também for confirmada no trecho; se o trecho mostra a
    consequência contrária, o claim vira `disputed`; se a consequência não é
    mecanicamente verificável, cai para `inferred`. Nunca promove nada.

    3ª auditoria: confirmar a consequência no TRECHO não bastava — em
    `if total > 1000: return 409` / `return 200`, o claim "quando total > 1000
    retorna 200" citando a função inteira achava o `200` no ramo ERRADO e saía
    `supported`. Frase com condição mecânica passa agora por
    `_branch_association`: o efeito tem de estar DENTRO do ramo governado pela
    condição (ou no caminho complementar, quando a condição afirmada é a
    complementar). Fora do ramo, sem ramo localizável, ou trecho não parseável
    como Python: nunca `supported`.
    """
    fields, consequence = _statement_parse(claim.statement)
    if consequence is None:
        return verdict, ""
    # Frase CONDICIONAL passa pela associação condição→ramo→efeito (3ª auditoria):
    # presença do literal no trecho não prova que o efeito pertence ao ramo da
    # condição. Frase sem condição mecânica (ou consequência sem forma mecânica)
    # segue pela regra de presença, que é tudo que há para checar.
    condition = _condition_of(fields) if consequence.verifiable else None
    assoc, assoc_why = (
        _branch_association(consequence, condition, verdict)
        if condition is not None
        else (_ASSOC_NONE, "")
    )
    if assoc in (_ASSOC_INSIDE, _ASSOC_COMPLEMENT):
        return verdict, f"frase completa: condição e consequência no mesmo ramo — {assoc_why}"
    if assoc == _ASSOC_NONE and _consequence_confirmed(consequence, verdict):
        return verdict, "frase completa: condição e consequência confirmadas no trecho"

    contrary = assoc_why if assoc == _ASSOC_CONTRARY else _contrary_consequence(consequence, verdict)
    if contrary:
        reasons = verdict.reasons + (
            f"consequência afirmada ({consequence.text!r}) CONTRADITA pelo trecho: {contrary}",
            "a frase vale pelo componente mais fraco: condição sustentada não sustenta "
            "consequência divergente (achado nº1)",
        )
        return (
            replace(
                verdict,
                epistemic=EpistemicStatus.DISPUTED,
                reasons=reasons,
                support_recorded_by=None,
                external_behavior_supported=False,
                insufficient=False,
            ),
            f"nenhum: o trecho mostra consequência contrária ({contrary})",
        )

    motivo = (
        f"consequência afirmada ({consequence.text!r}) não confirmada mecanicamente no trecho"
        if consequence.verifiable
        else (
            f"consequência afirmada ({consequence.text!r}) não é derivável em campo mecânico "
            "(sem literal, efeito ou exceção a confrontar)"
        )
    )
    if assoc == _ASSOC_OUTSIDE:
        motivo = (
            f"consequência afirmada ({consequence.text!r}) não confirmada NO RAMO da condição "
            f"`{condition.render()}`: {assoc_why}"
        )
    elif assoc == _ASSOC_UNDETERMINED:
        motivo = (
            f"vínculo condição→efeito não estabelecido para ({consequence.text!r}): {assoc_why}"
        )
    elif assoc == _ASSOC_HEURISTIC:
        motivo = (
            f"vínculo condição→efeito não estabelecido para ({consequence.text!r}): {assoc_why} "
            "— associação heurística nunca sustenta a frase inteira"
        )
    escopo = f"apenas a condição — {motivo}"
    if verdict.epistemic is not EpistemicStatus.SUPPORTED:
        return verdict, escopo
    reasons = verdict.reasons + (
        motivo,
        "claim rebaixado a inferred: a condição sozinha não sustenta a frase inteira "
        "(achado nº1)",
    )
    return (
        replace(
            verdict,
            epistemic=EpistemicStatus.INFERRED,
            reasons=reasons,
            nature=None,
            support_recorded_by=None,
            external_behavior_supported=False,
            insufficient=not consequence.verifiable,
        ),
        escopo,
    )


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
        try:
            return InvestigationObjective.from_dict(objective)
        except Exception:
            # Payload parcial (o `objective_json` da tarefa nem sempre traz o
            # pacote inteiro): cai para o objetivo mínimo em vez de derrubar a
            # integração dos demais objetivos.
            pass
    return InvestigationObjective(
        objective_id=objective_id or str(output.get("objective_id") or "objetivo"),
        kind="capability",
        capability_id=str(output.get("capability_id") or ""),
        name=str(output.get("capability_id") or objective_id or ""),
    )


# --------------------------------------------------------------------------
# Estado do objetivo (achado nº6)
# --------------------------------------------------------------------------


def _reading_satisfied_entries(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Campo opcional `reading_satisfied` do resultado, normalizado.

    É DADO, não comando: cada item diz qual obrigação o worker afirma ter
    cumprido e com que evidência. Nada aqui fecha obrigação por si — quem fecha
    é `_satisfy_readings`, e só depois de resolver a evidência no snapshot.
    """
    raw = output.get("reading_satisfied")
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(dict(item))
        elif isinstance(item, str) and item.strip():
            out.append({"need_id": item.strip()})
    return out


def _satisfy_readings(
    data: dict[str, Any], output: Mapping[str, Any], snapshot: Snapshot | None
) -> list[dict[str, Any]]:
    """Fecha as obrigações de leitura que o resultado EVIDENCIOU (achado nº3).

    Declaração não fecha nada: cada `evidence_refs` vira `ClaimEvidence` e passa
    por `check_location` contra o snapshot — localizador que não resolve (ou
    cujo `snippet_hash` não bate) deixa a obrigação ABERTA, e ela continua em
    `unmet_obligations()`. É a mesma porta que o resto do módulo usa para
    evidência; um worker não fecha obrigação escrevendo que leu.
    """
    entries = _reading_satisfied_entries(output)
    needs = data.get("reading_needs")
    if not entries or not isinstance(needs, list):
        return []

    fechadas: list[dict[str, Any]] = []
    for entry in entries:
        need_id = _norm_ws(entry.get("need_id"))
        target = _norm_ws(entry.get("target"))
        alvo = [
            n
            for n in needs
            if isinstance(n, Mapping)
            and not n.get("satisfied")
            and not (n.get("waived_reason") or "")
            and (
                (need_id and str(n.get("need_id") or "") == need_id)
                or (not need_id and target and _norm_ws(n.get("target")) == target)
            )
        ]
        if not alvo:
            continue
        spans: list[str] = []
        motivos: list[str] = []
        for raw in _evidence_dicts(entry) or ([entry] if entry.get("path") else []):
            citation = _claim_evidence(raw)
            if citation is None:
                motivos.append("evidência sem localizador utilizável (path + faixa de linhas)")
                continue
            if snapshot is None:
                motivos.append("sem snapshot para resolver a evidência")
                continue
            loc = check_location(citation, snapshot)
            if loc.ok:
                spans.append(loc.span)
            else:
                motivos.append(f"{citation.span}: {loc.reason}")
        registro = {
            "need_id": need_id or str(alvo[0].get("need_id") or ""),
            "target": target or _norm_ws(alvo[0].get("target")),
            "satisfeita": bool(spans),
            "evidencia": spans,
            "motivo": "; ".join(motivos[:3]),
        }
        if spans:
            nota = "leitura satisfeita com evidência resolvida no snapshot: " + ", ".join(spans)
            for need in alvo:
                need["satisfied"] = True
                need["satisfied_note"] = nota
        else:
            registro["motivo"] = (
                registro["motivo"]
                or "reading_satisfied sem evidence_refs: declaração não fecha obrigação"
            )
        fechadas.append(registro)
    return fechadas


def _objective_after_result(
    objective: InvestigationObjective,
    output: Mapping[str, Any],
    snapshot: Snapshot | None = None,
) -> tuple[InvestigationObjective, list[dict[str, Any]]]:
    """Objetivo do PLANO + o que o resultado preencheu, sem promoção indevida.

    O `state` declarado pelo worker só entra quando PIORA o estado (`blocked`):
    o estado final vem sempre de `evaluate()`, que exige `unmet_obligations()`
    vazio para `complete`. É por isso que um worker não consegue fechar um
    objetivo declarando-o fechado.

    `reading_satisfied` (achado nº3) é o único caminho pelo qual o resultado
    fecha obrigação de leitura — e só quando a evidência resolve no snapshot.
    Devolve também o registro do que foi (e do que não foi) fechado.
    """
    data = objective.to_dict()
    contract = {name: _merged_field(objective, output, name).to_dict() for name in CONTRACT_FIELDS}
    data["contract"] = contract
    leituras = _satisfy_readings(data, output, snapshot)
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
        return InvestigationObjective.from_dict(data), leituras
    except Exception:
        return objective, leituras


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
    #: Que PARTE da frase o veredito sustenta. Vazio quando a frase não tem
    #: consequência separável; preenchido quando só a condição ficou de pé
    #: (achado nº1) — o `value` gravado continua sendo a frase inteira, e é este
    #: campo que diz por que ela não é `supported`.
    escopo_sustentado: str = ""

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
            "escopo_sustentado": self.escopo_sustentado,
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
    #: Claims que NÃO viraram fato por incoerência de escopo (achado nº5):
    #: citação para caminho fora do snapshot corrente, sujeito de outro
    #: namespace. Cada item traz `claim_id` e `motivo`.
    rejeitados: list[dict[str, Any]] = field(default_factory=list)
    #: Obrigações de leitura fechadas por `reading_satisfied` COM evidência
    #: resolvida (achado nº3).
    leituras_satisfeitas: list[dict[str, Any]] = field(default_factory=list)
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
            "rejeitados": list(self.rejeitados),
            "leituras_satisfeitas": list(self.leituras_satisfeitas),
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
    #: Resultados aceitos pelo coordenador que esta integração NÃO processou,
    #: com o motivo (achado nº5): objetivo fora do escopo pedido, ou entradas
    #: que não são as do snapshot corrente. Descarte silencioso seria o mesmo
    #: erro com outra aparência.
    descartados: list[dict[str, Any]] = field(default_factory=list)

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
            "descartados": list(self.descartados),
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

    @staticmethod
    def normalize(path: str) -> str:
        return (path or "").replace("\\", "/").strip("/")

    def in_snapshot(self, path: str) -> bool:
        """O caminho pertence ao snapshot corrente? (achado nº5)

        Citação para arquivo que não está aqui é citação de OUTRO escopo — o
        outro repositório do mesmo store, ou uma árvore anterior.
        """
        return self.normalize(path) in self.files

    def sha_of(self, path: str) -> str:
        entry = self.files.get(self.normalize(path))
        return str(getattr(entry, "sha256", "") or "") if entry is not None else ""

    def get(self, path: str) -> str | None:
        norm = self.normalize(path)
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
    expected_input_versions_hash: str | Mapping[str, str] | None = None,
    reason: str = "integração de resultados de investigação",
    create_missing_capability: bool = True,
) -> IntegrationReport:
    """Resultados aceitos -> fatos verificados, numa ÚNICA revisão atômica.

    ESCOPO É OBRIGATÓRIO (achado nº5). `objectives` (ou `objective_ids`) define
    o conjunto que esta integração pode gravar; resultado de objetivo fora dele
    vai para `IntegrationReport.descartados` com motivo, e nada é escrito por
    ele. Sem escopo declarado, a integração inteira é bloqueada: um `runtime.db`
    compartilhado guarda tarefas de todos os repositórios já analisados, e
    `collect_results` sem filtro devolvia as do repositório vizinho — foi assim
    que a integração do repositório B recriou a regra do A no namespace do B.

    Também é descartado o resultado cujas ENTRADAS não são as do snapshot
    corrente: `expected_input_versions_hash` quando o chamador o conhece, e, em
    todo caso, os `source_version_ids` (`caminho@sha256`) declarados na tarefa,
    conferidos arquivo a arquivo contra `snapshot.file_map()`.

    `capability_entity_map` (capability_id -> entity_id) é parâmetro, e não
    consulta ao `wk`: quem chama já resolveu a identidade da capacidade e este
    módulo não deve reimplementar aquela decisão. Sem entrada no mapa, tenta-se
    o próprio `capability_id` como `entity_id` (é a convenção que
    `wk._write_structural_knowledge` usa ao gravar `EntityDraft(entity_id=
    cap.capability_id)`), e só então cria-se a Capability que falta — sempre
    conferindo que a entidade reutilizada é DESTE namespace.

    A revisão é uma só de propósito: ou todos os fatos desta integração entram
    com a mesma proveniência, ou nenhum entra (§4.2).
    """
    report = IntegrationReport()
    index = _objectives_index(objectives)
    scope = _scope_of(objectives, objective_ids)
    if not scope:
        report.bloqueios.append(
            "integração sem escopo: `objectives` (ou `objective_ids`) é obrigatório — "
            "sem ele, resultados de OUTRO repositório no mesmo runtime.db seriam gravados "
            "neste namespace (achado nº5). Nada foi integrado."
        )
        return report

    versions = _SourceVersions(repo, namespace, snapshot)
    # Coleta SEM filtro e descarta aqui: o filtro dentro de `collect_results`
    # deixaria o descarte invisível, e "não integrei o resultado do outro
    # repositório" é informação, não silêncio (achado nº5).
    collected = list(results) if results is not None else collect_results(task_store)
    accepted: list[AcceptedResult] = []
    for result in collected:
        motivo = _out_of_scope_reason(result, scope, versions, expected_input_versions_hash)
        if motivo:
            report.descartados.append(
                {
                    "objective_id": result.objective_id,
                    "task_id": result.task_id,
                    "capability_id": result.capability_id,
                    "input_versions_hash": result.input_versions_hash,
                    "motivo": motivo,
                }
            )
            continue
        accepted.append(result)

    if not accepted:
        report.bloqueios.append(
            "nenhum resultado aceito em runtime.db (tarefas done de investigação/verificação "
            "com resultado) dentro do escopo pedido: nada a integrar"
            + (f"; {len(report.descartados)} resultado(s) descartado(s)" if report.descartados else "")
        )
        return report

    prepared: list[dict[str, Any]] = []
    all_verdicts: list[Verdict] = []
    for result in accepted:
        base = index.get(result.objective_id)
        if base is None:
            base = _objective_of(result.objective_payload or None, result.output, result.objective_id)
        objective, leituras = _objective_after_result(base, result.output, snapshot)
        claims = results_to_claims(result, objective)
        verdicts = [verify_claim(c, snapshot, extraction) for c in claims]
        inconsistencies: list[Inconsistency] = check_consistency(claims, verdicts)
        verdicts = apply_inconsistencies(verdicts, inconsistencies)
        by_id = {c.claim_id: c for c in claims}
        # Achado nº1: a frase inteira é reavaliada pelo componente mais fraco
        # ANTES do resumo e da gravação, para que relatório e banco contem a
        # mesma história.
        guarded: list[Verdict] = []
        escopos: dict[str, str] = {}
        for verdict in verdicts:
            claim = by_id.get(verdict.claim_id)
            if claim is None:
                guarded.append(verdict)
                continue
            adjusted, escopo = _consequence_guard(claim, verdict)
            guarded.append(adjusted)
            if escopo:
                escopos[verdict.claim_id] = escopo
        verdicts = guarded
        all_verdicts.extend(verdicts)
        prepared.append(
            {
                "result": result,
                "objective": objective,
                "claims": by_id,
                "ordered_claims": claims,
                "verdicts": verdicts,
                "inconsistencies": inconsistencies,
                "escopos": escopos,
                "leituras": leituras,
            }
        )

    report.verificacao = verification_summary(all_verdicts)
    report.reread_obligations = _reread_with_objective(prepared)

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
                    escopos=item["escopos"],
                    leituras=item["leituras"],
                )
            )
        report.revisao = rev.revision_id
        report.mudancas = rev.change_count
    return report


def _scope_of(
    objectives: Iterable[Any] | None, objective_ids: Sequence[str] | None
) -> set[str]:
    """Ids que esta integração pode gravar.

    Lê o `objective_id` do objeto BRUTO (e não só do índice reconstruído) de
    propósito: um objetivo que `InvestigationObjective.from_dict` recuse ainda
    delimita escopo — perder o id ali alargaria silenciosamente o filtro.
    """
    ids: set[str] = set()
    for raw in objectives or ():
        oid = getattr(raw, "objective_id", None)
        if oid is None and isinstance(raw, Mapping):
            oid = raw.get("objective_id")
        if oid:
            ids.add(str(oid))
    ids.update(str(o) for o in (objective_ids or ()) if str(o))
    return ids


def _out_of_scope_reason(
    result: AcceptedResult,
    scope: set[str],
    versions: "_SourceVersions",
    expected: str | Mapping[str, str] | None,
) -> str:
    """Por que este resultado NÃO pertence a esta integração (ou ``""``)."""
    if result.objective_id not in scope:
        return (
            f"objective_id {result.objective_id!r} fora do escopo desta integração "
            f"({len(scope)} objetivo(s)): resultado de outro conjunto/repositório"
        )
    want = _expected_hash_for(expected, result.objective_id)
    if want and want != result.input_versions_hash:
        return (
            f"input_versions_hash {result.input_versions_hash[:12]!r} difere do esperado "
            f"{want[:12]!r}: o resultado descreve outra versão das entradas"
        )
    declared = result.input_versions.get("source_version_ids")
    entries = (
        [str(s) for s in declared]
        if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes))
        else []
    )
    foreign: list[str] = []
    for entry in entries:
        path, sep, sha = entry.rpartition("@")
        if not sep:
            path, sha = entry, ""
        if not versions.in_snapshot(path):
            foreign.append(f"{path} (fora do snapshot atual)")
        elif sha and versions.sha_of(path) != sha:
            foreign.append(f"{path} (sha divergente do snapshot atual)")
    if foreign:
        return (
            "entradas declaradas pela tarefa não pertencem ao snapshot corrente: "
            + "; ".join(foreign[:3])
            + (f" (+{len(foreign) - 3})" if len(foreign) > 3 else "")
        )
    return ""


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


def _entity_namespace(repo: Any, entity_id: str) -> str | None:
    """Namespace da entidade, ou `None` quando ela não existe/não é legível.

    `repository.entity_exists` responde só "existe" — e existir em OUTRO
    namespace foi o que permitiu, com dois repositórios no mesmo store, um fato
    do namespace B apontar a entidade do A (achado nº5).
    """
    getter = getattr(repo, "get_entity", None)
    if getter is None:
        return None
    try:
        entity = getter(entity_id, lifecycle=None)
    except TypeError:
        try:
            entity = getter(entity_id)
        except Exception:
            return None
    except Exception:
        return None
    return getattr(entity, "namespace", None)


def _same_namespace(repo: Any, entity_id: str, namespace: str) -> bool:
    """A entidade EXISTENTE pertence a este namespace?"""
    found = _entity_namespace(repo, entity_id)
    if found is None:
        return False
    try:
        return id_mod.normalize_namespace(found) == id_mod.normalize_namespace(namespace)
    except KnowledgeError:
        return False


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

    Toda entidade REUTILIZADA passa por `_same_namespace`: prender os fatos
    desta integração a uma entidade de outro namespace é exatamente o cruzamento
    que o achado nº5 descreve, e existir não é pertencer.
    """
    mapped = str(capability_entity_map.get(objective.capability_id) or "").strip()
    if mapped:
        if repo.entity_exists(mapped):
            if _same_namespace(repo, mapped, namespace):
                return mapped
            outcome.bloqueios.append(
                f"capability_entity_map aponta para entidade {mapped!r} do namespace "
                f"{_entity_namespace(repo, mapped)!r}, não de {namespace!r}: recusada "
                "(fato deste namespace não aponta entidade de outro)"
            )
        else:
            outcome.bloqueios.append(
                f"capability_entity_map aponta para entidade inexistente: {mapped!r}"
            )
    existing = bool(objective.capability_id) and repo.entity_exists(objective.capability_id)
    if existing:
        if _same_namespace(repo, objective.capability_id, namespace):
            return objective.capability_id
        outcome.bloqueios.append(
            f"capacidade {objective.capability_id!r} já existe no namespace "
            f"{_entity_namespace(repo, objective.capability_id)!r}: este objetivo não é "
            f"de {namespace!r} ou o id colide — nova identidade será derivada do namespace"
        )
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
            # `entity_id` explícito SÓ quando ninguém mais o reivindicou: reusar
            # o id de uma entidade de outro namespace seria `IdentityConflict`
            # (e, sem a checagem, seria contaminação silenciosa).
            entity_id=(objective.capability_id or None) if not existing else None,
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
    statement: str = "",
) -> str:
    """`BusinessRule`/`Flow` nomeada, com `implements` para a capacidade.

    Identidade determinística por `capability + assunto` (`stable_key`), então
    a mesma regra reafirmada em outra execução revisita a MESMA entidade em vez
    de criar uma homônima.

    Quando o nome (ou o enunciado) traz um id explícito do corpus — `RN-023`,
    `CAP-007` —, esse id vira ALIAS canônico (achado nº4). A `stable_key` é
    derivada (`businessrule:<capability>:rn-023`) e a ingestão resolve pela
    chave LITERAL "RN-023": sem o alias, os dois lados nunca se encontram e a
    mesma regra existe duas vezes. Com ele, `repository.put_entity` chama
    `identity.resolve_identity`, que casa pelo alias e REUSA a entidade.
    """
    entity_type = RULE_ENTITY_TYPE.get(campo, EntityType.BUSINESS_RULE)
    stable_key = f"{entity_type.value.lower()}:{objective.capability_id or objective.objective_id}:{_slug(subject_name)}"
    aliases = id_mod.explicit_id_aliases(subject_name, statement)
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=entity_type,
            stable_key=stable_key,
            title=subject_name,
            aliases=aliases,
            attributes={"campo": campo, "objective_id": objective.objective_id},
            evidence_refs=tuple(evidence_ids),
        )
    )
    outcome.entidades.append(
        {
            "entity_id": write.target_id,
            "tipo": entity_type.value,
            "novo": write.changed,
            "titulo": subject_name,
            "aliases": [a.alias for a in aliases],
        }
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
    escopos: Mapping[str, str] | None = None,
    leituras: Sequence[Mapping[str, Any]] = (),
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
        leituras_satisfeitas=[dict(l) for l in leituras],
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
            escopo_sustentado=(escopos or {}).get(claim.claim_id, ""),
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
    escopo_sustentado: str = "",
) -> None:
    """Um claim verificado -> um fato, com o `epistemic` do VEREDITO."""
    campo = claim.scope.rsplit(":", 1)[-1] if ":" in claim.scope else ""

    # Achado nº5: citação para arquivo que não está no snapshot corrente é prova
    # de OUTRO escopo (o repositório vizinho no mesmo store, ou uma árvore
    # anterior). Gravar o fato aqui apontaria conhecimento deste namespace para
    # evidência que ele não pode reabrir. Claim SEM citação continua virando
    # fato `unresolved` — ausência de prova é lacuna declarada (invariante 4);
    # prova de fora do escopo é incoerência, e incoerência é recusada.
    if claim.evidence_refs and not any(
        versions.in_snapshot(ref.path) for ref in claim.evidence_refs
    ):
        motivo = (
            "todas as citações apontam para fora do snapshot desta integração: "
            + ", ".join(sorted({ref.path for ref in claim.evidence_refs}))[:200]
        )
        outcome.rejeitados.append({"claim_id": claim.claim_id, "campo": campo, "motivo": motivo})
        outcome.bloqueios.append(f"{claim.claim_id}: {motivo}")
        return

    evidence_ids, source_version_id = _record_evidence(
        rev, namespace, versions, verdict, claim.symbol
    )

    subject_id = capability_subject
    subject_name = _subject_name_of(claim, result.output, campo)
    entity_id: str | None = None
    if subject_name and campo in RULE_ENTITY_TYPE:
        entity_id = _rule_entity(
            rev, namespace, objective, campo, subject_name, capability_subject,
            evidence_ids, verdict, outcome, claim.statement,
        )
        subject_id = entity_id

    # Veredito `supported` cuja citação não virou linha de `evidence` (arquivo
    # fora do snapshot, localizador irrecuperável) é rebaixado AQUI:
    # `repository._check_support` recusaria depois, e recusar tarde derrubaria
    # a revisão inteira em vez de registrar o que se sabe.
    #
    # O `value` gravado é a frase INTEIRA (é ela que a consulta e a publicação
    # mostram), e por isso o `epistemic` já chega aqui julgado pelo componente
    # mais fraco: `_consequence_guard` rebaixou/contradisse antes. Gravar a
    # frase toda com o `epistemic` da condição isolada é o achado nº1.
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
            escopo_sustentado=escopo_sustentado,
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
