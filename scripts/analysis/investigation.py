"""Objetivos de investigação, obrigações de leitura e critério de conclusão.

Implementa §6.3 (contrato de investigação de uma capacidade), §6.4
(investigação adaptativa), §6.5 (matriz de falhas e edge cases) e §6.6
(critério de conclusão) em código executável.

O que este módulo garante — cada item tem uma função que o aplica, não uma
frase que o promete:

| Regra do plano | Onde é aplicada |
|---|---|
| Objetivo por capacidade/entrada, nunca por documento | `plan()` itera `CapabilityMap.capabilities` e os órfãos; não existe parâmetro de quantidade nem de tipo de documento |
| Chamada não resolvida vira lacuna rastreável | `_needs_from_gaps()` gera `ReadingNeed(trigger=UNRESOLVED_CALL)` com a `EvidenceRef` do sítio |
| `not_applicable` exige justificativa ligada ao código | `FailureEdgeMatrix.mark()` levanta `MatrixJustificationRequired`; sem marcação a célula permanece `unresolved` por padrão |
| `partial` não vira `complete` por decreto | `InvestigationObjective.set_state()` levanta `ConclusionRejected` enquanto houver obrigação aberta |
| Todo elemento termina explicado/excluído/não resolvido | `ContractField` só aceita `fill`/`exclude`/`unresolve`, cada um com sua exigência; `assert_accounted()` recusa campo `pending` |
| Interromper só por motivo registrado | `close()` exige um `ClosureReason` do vocabulário fechado |

**Nada aqui produz conteúdo de conhecimento.** O módulo produz *obrigações*: o
que precisa ser lido, o que precisa ser respondido e o que impede declarar
conclusão. Quem preenche é a execução (W4), sob as regras que estas estruturas
impõem.

Imports: stdlib + `knowledge` + `analysis`. Nenhuma dependência nativa.
"""

from __future__ import annotations

import builtins as _py_builtins
import datetime as _dt
import enum
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from knowledge.evidence import validate_locator
from knowledge.models import LocatorInvalid, SourceKind

from .capabilities import (
    BoundaryGap,
    CapabilityCandidate,
    CapabilityMap,
    EvidenceRef,
    ExternalDependency,
    OrphanSymbol,
    evidence_ref_for,
    fingerprint,
)
from .extractors.registry import ExtractionResult
from .inventory import FileClass, Inventory
from .snapshot import Snapshot

__all__ = [
    "CONTRACT_FIELDS",
    "CONTRACT_LABELS",
    "FAILURE_FAMILIES",
    "ClosureReason",
    "ClosureRecord",
    "ConclusionRejected",
    "ContractField",
    "ContractFieldStatus",
    "ContractViolation",
    "FailureEdgeMatrix",
    "InvestigationError",
    "InvestigationObjective",
    "MatrixCell",
    "MatrixJustificationRequired",
    "MatrixState",
    "ObjectiveAccountingError",
    "ObjectiveKind",
    "ObjectiveState",
    "PlanAccountingError",
    "ReadingKind",
    "ReadingNeed",
    "ReadingTrigger",
    "assert_plan_accounted",
    "classify_gap",
    "close",
    "objectives_from_dict",
    "objectives_to_dict",
    "plan",
    "plan_accounting",
]


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class InvestigationError(ValueError):
    """Contrato de investigação violado."""


class ContractViolation(InvestigationError):
    """Campo do §6.3 preenchido sem cumprir a exigência do próprio campo."""


class MatrixJustificationRequired(InvestigationError):
    """§6.5 — estado afirmativo da matriz sem justificativa ligada ao código."""


class ConclusionRejected(InvestigationError):
    """§6.6 — tentativa de declarar `complete` com obrigação aberta."""


class ObjectiveAccountingError(InvestigationError):
    """§6.6 — há elemento descoberto que não terminou em nenhum dos três destinos."""


class PlanAccountingError(InvestigationError):
    """§6.6 — capacidade ou órfão sem objetivo, ou objetivo duplicado."""


# --------------------------------------------------------------------------
# Vocabulário
# --------------------------------------------------------------------------

#: §6.3 — as 13 linhas do contrato, na ordem da tabela do plano.
CONTRACT_FIELDS: tuple[str, ...] = (
    "identidade",
    "gatilho",
    "precondicoes",
    "dados",
    "decisoes",
    "persistencia",
    "integracoes",
    "sucesso",
    "falhas",
    "edge_cases",
    "dependencias",
    "verificacao",
    "lacunas",
)

#: Conteúdo obrigatório de cada campo, copiado da tabela do §6.3. Vai junto do
#: objetivo para que o executor receba a exigência, não só o rótulo.
CONTRACT_LABELS: Mapping[str, str] = {
    "identidade": "Nome de negócio, nomes técnicos, entradas e escopo",
    "gatilho": "Requisição, evento, agendamento ou chamada",
    "precondicoes": "Estado, permissões, validações e dados requeridos",
    "dados": "Entradas, normalização, transformações e saídas",
    "decisoes": "Predicados, precedência, short-circuit e configurações que alteram o caminho",
    "persistencia": "Leituras/escritas, transações, commit/rollback e restrições",
    "integracoes": "Operação, contrato, timeout, retry, fallback e idempotência",
    "sucesso": "Resultado, mudança de estado e efeitos emitidos",
    "falhas": "Origem, captura/propagação, resposta e efeitos já executados",
    "edge_cases": "Condições-limite e combinações pertinentes ao comportamento",
    "dependencias": "Componentes, regras, contratos e dados relacionados",
    "verificacao": "Evidências por afirmação; testes existentes e respectivos limites",
    "lacunas": "Pontos não resolvidos e consequências para consumidores",
}

#: §6.5 — famílias e itens da matriz de falhas e edge cases.
FAILURE_FAMILIES: Mapping[str, tuple[str, ...]] = {
    "entrada": (
        "ausente",
        "nula",
        "vazia",
        "malformada",
        "limite_inferior",
        "limite_superior",
        "overflow",
        "tamanho",
    ),
    "negocio": ("regra_conflitante", "ordem_das_regras", "transicao_invalida", "estado_terminal"),
    "persistencia": (
        "registro_inexistente",
        "registro_duplicado",
        "restricao",
        "rollback",
        "concorrencia",
        "atualizacao_perdida",
    ),
    "mensageria": ("duplicidade", "reentrega", "atraso", "ordem", "rejeicao", "dead_letter"),
    "http_rpc": ("timeout", "resposta_invalida", "erro_remoto", "retry", "efeito_duplicado"),
    "tempo": ("expiracao", "fuso", "janela_temporal", "processamento_apos_vencimento"),
    "concorrencia": ("duas_execucoes", "lock", "corrida", "atomicidade_do_efeito"),
    "seguranca_funcional": ("autenticacao", "autorizacao", "comportamento_de_negacao"),
}


class ContractFieldStatus(str, enum.Enum):
    """§6.6 — os três destinos legítimos, mais o estado inicial.

    `PENDING` não é destino: é a ausência de destino, e `assert_accounted()`
    recusa qualquer campo que termine assim.
    """

    PENDING = "pending"
    FILLED = "filled"  # explicado
    EXCLUDED = "excluded"  # excluído com motivo
    UNRESOLVED = "unresolved"  # não resolvido com impacto


class ObjectiveState(str, enum.Enum):
    """§6.6."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class ClosureReason(str, enum.Enum):
    """§6.4 — os únicos motivos de interrupção admitidos."""

    OBJETIVO_ATENDIDO = "objetivo_atendido"
    FRONTEIRA_EXPLICITA = "fronteira_explicita"
    ORCAMENTO = "orcamento"
    BLOQUEIO = "bloqueio"


class ObjectiveKind(str, enum.Enum):
    """Origem do objetivo. Ambos são "por capacidade/entrada"; nenhum é por documento."""

    CAPABILITY = "capability"
    #: §6.1.8 — símbolo público que nenhuma entrada alcança também abre tarefa.
    ORPHAN_GROUP = "orphan_group"


class ReadingKind(str, enum.Enum):
    """§6.4 — "worker pode solicitar leitura adicional por símbolo/faixa/contrato"."""

    SYMBOL = "symbol"
    RANGE = "range"
    CONTRACT = "contract"


class ReadingTrigger(str, enum.Enum):
    """O gatilho do §6.4 que originou a obrigação. Um por marcador do plano."""

    UNRESOLVED_CALL = "unresolved_call"
    PREDICATE = "predicate"
    EFFECT = "effect"
    EXTERNAL_INTEGRATION = "external_integration"
    CONFIGURATION = "configuration"
    LINKED_TEST = "linked_test"


class MatrixState(str, enum.Enum):
    """§6.5 — estados de cobertura."""

    COVERED = "covered"
    NOT_APPLICABLE = "not_applicable"
    UNRESOLVED = "unresolved"


#: Prioridade de despacho (menor = antes). Lacuna rastreável e efeito vêm
#: primeiro porque são o que impede afirmar comportamento; predicado de corpo
#: vem por último porque é o mais volumoso.
_TRIGGER_PRIORITY: Mapping[ReadingTrigger, int] = {
    ReadingTrigger.UNRESOLVED_CALL: 10,
    ReadingTrigger.EFFECT: 15,
    ReadingTrigger.EXTERNAL_INTEGRATION: 20,
    ReadingTrigger.LINKED_TEST: 25,
    ReadingTrigger.CONFIGURATION: 30,
    ReadingTrigger.PREDICATE: 40,
}


# --------------------------------------------------------------------------
# Obrigação de leitura
# --------------------------------------------------------------------------


@dataclass
class ReadingNeed:
    """Uma leitura que precisa acontecer para que uma afirmação seja possível.

    Não é sugestão: enquanto uma necessidade estiver aberta (nem satisfeita nem
    dispensada com motivo), `InvestigationObjective` recusa `complete`.
    """

    need_id: str
    kind: ReadingKind
    target: str
    motivo: str
    trigger: ReadingTrigger
    evidence: EvidenceRef | None = None
    priority: int = 50
    satisfied: bool = False
    satisfied_note: str = ""
    waived_reason: str = ""

    def __post_init__(self) -> None:
        self.kind = ReadingKind(self.kind)
        self.trigger = ReadingTrigger(self.trigger)
        if not self.target:
            raise InvestigationError("ReadingNeed exige target")
        if not self.motivo:
            raise InvestigationError(
                f"ReadingNeed {self.target!r} sem motivo: obrigação sem razão não é rastreável (§6.4)"
            )

    @property
    def open(self) -> bool:
        return not self.satisfied and not self.waived_reason

    def satisfy(self, note: str) -> "ReadingNeed":
        if not note:
            raise InvestigationError(
                f"satisfazer {self.target!r} exige nota dizendo o que foi lido e onde"
            )
        self.satisfied = True
        self.satisfied_note = note
        return self

    def waive(self, reason: str) -> "ReadingNeed":
        """Dispensa explícita. Sem motivo não há dispensa — a obrigação continua aberta."""
        if not reason:
            raise InvestigationError(
                f"dispensar {self.target!r} exige motivo (fronteira explícita, orçamento, "
                "irrelevância comprovada)"
            )
        self.waived_reason = reason
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "need_id": self.need_id,
            "kind": self.kind.value,
            "target": self.target,
            "motivo": self.motivo,
            "trigger": self.trigger.value,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "priority": self.priority,
            "satisfied": self.satisfied,
            "satisfied_note": self.satisfied_note,
            "waived_reason": self.waived_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReadingNeed":
        ev = data.get("evidence")
        return cls(
            need_id=str(data["need_id"]),
            kind=ReadingKind(data["kind"]),
            target=str(data["target"]),
            motivo=str(data["motivo"]),
            trigger=ReadingTrigger(data["trigger"]),
            evidence=EvidenceRef.from_dict(ev) if ev else None,
            priority=int(data.get("priority", 50)),
            satisfied=bool(data.get("satisfied", False)),
            satisfied_note=str(data.get("satisfied_note", "")),
            waived_reason=str(data.get("waived_reason", "")),
        )


# --------------------------------------------------------------------------
# Campo do contrato §6.3
# --------------------------------------------------------------------------


@dataclass
class ContractField:
    """Uma linha da tabela do §6.3, com o destino do §6.6 aplicado no código."""

    name: str
    label: str
    status: ContractFieldStatus = ContractFieldStatus.PENDING
    content: str = ""
    motivo: str = ""
    impacto: str = ""
    evidence_refs: list[EvidenceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = ContractFieldStatus(self.status)
        if self.name not in CONTRACT_FIELDS:
            raise ContractViolation(
                f"campo {self.name!r} fora do contrato §6.3 (aceitos: {', '.join(CONTRACT_FIELDS)})"
            )

    def fill(self, content: str, evidence_refs: Sequence[EvidenceRef]) -> "ContractField":
        """Explicado: exige conteúdo E evidência (§6.3, linha Verificação)."""
        if not content:
            raise ContractViolation(f"campo {self.name!r}: `fill` exige conteúdo")
        if not evidence_refs:
            raise ContractViolation(
                f"campo {self.name!r}: `fill` exige ao menos uma evidência — afirmação sem "
                "evidência por afirmação não é preenchimento (§6.3)"
            )
        self.content = content
        self.evidence_refs = list(evidence_refs)
        self.status = ContractFieldStatus.FILLED
        self.motivo = ""
        self.impacto = ""
        return self

    def exclude(self, motivo: str, evidence_refs: Sequence[EvidenceRef] = ()) -> "ContractField":
        """Excluído com motivo — o único jeito legítimo de um campo não ter conteúdo."""
        if not motivo:
            raise ContractViolation(
                f"campo {self.name!r}: exclusão exige motivo (§6.6, §6.1.3 — nunca silenciosa)"
            )
        self.motivo = motivo
        self.evidence_refs = list(evidence_refs)
        self.status = ContractFieldStatus.EXCLUDED
        self.content = ""
        self.impacto = ""
        return self

    def unresolve(self, impacto: str, evidence_refs: Sequence[EvidenceRef] = ()) -> "ContractField":
        """Não resolvido COM IMPACTO — desconhecido explícito, nunca omissão."""
        if not impacto:
            raise ContractViolation(
                f"campo {self.name!r}: `unresolve` exige impacto para o consumidor (§6.6)"
            )
        self.impacto = impacto
        self.evidence_refs = list(evidence_refs)
        self.status = ContractFieldStatus.UNRESOLVED
        self.content = ""
        self.motivo = ""
        return self

    @property
    def accounted(self) -> bool:
        return self.status is not ContractFieldStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "status": self.status.value,
            "content": self.content,
            "motivo": self.motivo,
            "impacto": self.impacto,
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContractField":
        return cls(
            name=str(data["name"]),
            label=str(data.get("label", CONTRACT_LABELS.get(str(data["name"]), ""))),
            status=ContractFieldStatus(data.get("status", "pending")),
            content=str(data.get("content", "")),
            motivo=str(data.get("motivo", "")),
            impacto=str(data.get("impacto", "")),
            evidence_refs=[EvidenceRef.from_dict(e) for e in data.get("evidence_refs", ())],
        )


# --------------------------------------------------------------------------
# Matriz §6.5
# --------------------------------------------------------------------------


@dataclass
class MatrixCell:
    family: str
    item: str
    state: MatrixState = MatrixState.UNRESOLVED
    justification: EvidenceRef | None = None
    note: str = ""
    marked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "item": self.item,
            "state": self.state.value,
            "justification": self.justification.to_dict() if self.justification else None,
            "note": self.note,
            "marked": self.marked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MatrixCell":
        just = data.get("justification")
        return cls(
            family=str(data["family"]),
            item=str(data["item"]),
            state=MatrixState(data.get("state", "unresolved")),
            justification=EvidenceRef.from_dict(just) if just else None,
            note=str(data.get("note", "")),
            marked=bool(data.get("marked", False)),
        )


class FailureEdgeMatrix:
    """Matriz de falhas e edge cases do §6.5, com a regra de justificativa no código.

    Toda célula nasce `unresolved` e `marked=False`: **ausência de evidência
    produz `unresolved`**, exatamente como o plano exige, sem que ninguém
    precise lembrar de escrever isso. Sair de `unresolved` custa evidência:
    `mark()` recusa `covered` e `not_applicable` sem `justification_evidence`
    ligada a código.
    """

    def __init__(self, families: Mapping[str, Sequence[str]] | None = None) -> None:
        self._families: dict[str, tuple[str, ...]] = {
            fam: tuple(items) for fam, items in (families or FAILURE_FAMILIES).items()
        }
        self.cells: dict[tuple[str, str], MatrixCell] = {
            (fam, item): MatrixCell(family=fam, item=item)
            for fam, items in self._families.items()
            for item in items
        }

    # -- leitura -----------------------------------------------------------
    @property
    def families(self) -> Mapping[str, tuple[str, ...]]:
        return dict(self._families)

    def cell(self, family: str, item: str) -> MatrixCell:
        try:
            return self.cells[(family, item)]
        except KeyError:
            raise InvestigationError(f"célula desconhecida: {family!r}/{item!r}") from None

    def state_of(self, family: str, item: str) -> MatrixState:
        return self.cell(family, item).state

    def unresolved_cells(self) -> list[MatrixCell]:
        return [c for c in self.cells.values() if c.state is MatrixState.UNRESOLVED]

    def implicit_pending(self) -> list[MatrixCell]:
        """Células nunca avaliadas — pendência que ninguém declarou.

        Diferente de `unresolved` marcado com impacto: aquilo é um desconhecido
        explícito, isto é silêncio. `complete` recusa os dois; a distinção
        importa para o relatório de `partial`.
        """
        return [c for c in self.cells.values() if not c.marked]

    def summary(self) -> dict[str, int]:
        out = {s.value: 0 for s in MatrixState}
        for cell in self.cells.values():
            out[cell.state.value] += 1
        out["total"] = len(self.cells)
        out["implicit_pending"] = len(self.implicit_pending())
        return out

    # -- escrita -----------------------------------------------------------
    def mark(
        self,
        family: str,
        item: str,
        state: MatrixState | str,
        justification_evidence: EvidenceRef | None = None,
        note: str = "",
    ) -> MatrixCell:
        """Marca uma célula aplicando a regra do §6.5.

        - `not_applicable` e `covered` EXIGEM `justification_evidence` ligada a
          código (`EvidenceRef` com caminho e faixa; se trouxer `locator`, ele
          é validado como `SourceKind.CODE`). Sem isso: `MatrixJustificationRequired`.
        - `unresolved` exige `note` com o impacto — desconhecido sem consequência
          declarada não é resultado (§6.6).
        """
        cell = self.cell(family, item)
        state = MatrixState(state)
        if state in (MatrixState.NOT_APPLICABLE, MatrixState.COVERED):
            _require_code_evidence(family, item, state, justification_evidence)
        elif state is MatrixState.UNRESOLVED and not note:
            raise InvestigationError(
                f"{family}/{item}: estado 'unresolved' exige note com o impacto do "
                "desconhecido para o consumidor (§6.6)"
            )
        cell.state = state
        cell.justification = justification_evidence
        cell.note = note
        cell.marked = True
        return cell

    # -- serialização ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "families": {fam: list(items) for fam, items in self._families.items()},
            "cells": [c.to_dict() for c in self.cells.values()],
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FailureEdgeMatrix":
        matrix = cls(data.get("families") or FAILURE_FAMILIES)
        for raw in data.get("cells", ()):
            cell = MatrixCell.from_dict(raw)
            matrix.cells[(cell.family, cell.item)] = cell
        return matrix


def _require_code_evidence(
    family: str, item: str, state: MatrixState, evidence: EvidenceRef | None
) -> None:
    """A regra que faz `not_applicable` custar evidência — e não retórica."""
    if evidence is None:
        raise MatrixJustificationRequired(
            f"{family}/{item}: estado {state.value!r} exige justification_evidence ligada ao "
            "código (§6.5). Sem evidência o estado correto é 'unresolved', que é o padrão "
            "da célula — nada precisa ser feito para mantê-lo."
        )
    if not isinstance(evidence, EvidenceRef):
        raise MatrixJustificationRequired(
            f"{family}/{item}: justification_evidence deve ser EvidenceRef, recebido "
            f"{type(evidence).__name__} — texto livre não é justificativa ligada ao código"
        )
    if not evidence.path or evidence.line_start < 1 or evidence.line_end < evidence.line_start:
        raise MatrixJustificationRequired(
            f"{family}/{item}: justification_evidence sem caminho/faixa de código válidos "
            f"({evidence.path!r} {evidence.line_start}..{evidence.line_end})"
        )
    if evidence.locator is not None:
        try:
            validate_locator(SourceKind.CODE, evidence.locator)
        except LocatorInvalid as exc:
            raise MatrixJustificationRequired(
                f"{family}/{item}: localizador da justificativa inválido: {exc}"
            ) from exc


# --------------------------------------------------------------------------
# Conclusão §6.6
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureRecord:
    """Motivo registrado do término (§6.4, último marcador)."""

    reason: ClosureReason
    note: str
    state_at_close: ObjectiveState
    closed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "note": self.note,
            "state_at_close": self.state_at_close.value,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClosureRecord":
        return cls(
            reason=ClosureReason(data["reason"]),
            note=str(data.get("note", "")),
            state_at_close=ObjectiveState(data["state_at_close"]),
            closed_at=str(data.get("closed_at", "")),
        )


@dataclass
class InvestigationObjective:
    """Um objetivo de investigação: uma capacidade/entrada e suas obrigações.

    O objeto é o pacote que o runtime (W4) despacha: `to_dict()` é o payload e
    `from_dict()` o reconstrói sem perda. A regra de conclusão viaja junto com
    o dado — quem receber o pacote não consegue declarar `complete` fora das
    condições do §6.6, porque a validação está no objeto, não no protocolo.
    """

    objective_id: str
    kind: ObjectiveKind
    capability_id: str
    name: str
    entry_keys: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    contract: dict[str, ContractField] = field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    reading_needs: list[ReadingNeed] = field(default_factory=list)
    matrix: FailureEdgeMatrix = field(default_factory=FailureEdgeMatrix)
    state: ObjectiveState = ObjectiveState.PARTIAL
    closure: ClosureRecord | None = None
    accounting: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.kind = ObjectiveKind(self.kind)
        self.state = ObjectiveState(self.state)
        if not self.contract:
            self.contract = {
                name: ContractField(name=name, label=CONTRACT_LABELS[name])
                for name in CONTRACT_FIELDS
            }
        missing = [f for f in CONTRACT_FIELDS if f not in self.contract]
        if missing:
            raise ContractViolation(
                f"objetivo {self.objective_id}: contrato §6.3 incompleto, faltam {missing}"
            )

    # -- leitura -----------------------------------------------------------
    def field(self, name: str) -> ContractField:
        try:
            return self.contract[name]
        except KeyError:
            raise ContractViolation(f"campo {name!r} não existe no contrato §6.3") from None

    def open_needs(self) -> list[ReadingNeed]:
        return [n for n in self.reading_needs if n.open]

    def pending_fields(self) -> list[str]:
        return [n for n in CONTRACT_FIELDS if not self.contract[n].accounted]

    def unresolved_fields(self) -> list[str]:
        return [
            n
            for n in CONTRACT_FIELDS
            if self.contract[n].status is ContractFieldStatus.UNRESOLVED
        ]

    def unmet_obligations(self) -> list[str]:
        """Tudo que impede `complete` — a lista que `set_state` consulta.

        Vazia significa: todo campo do §6.3 explicado ou excluído com motivo,
        nenhuma obrigação de leitura aberta, nenhuma pendência implícita na
        matriz, nenhuma célula em `unresolved` e nenhuma necessidade descartada
        por orçamento.
        """
        unmet: list[str] = []
        pending = self.pending_fields()
        if pending:
            unmet.append(f"campos do §6.3 sem destino declarado: {', '.join(pending)}")
        unresolved = self.unresolved_fields()
        if unresolved:
            unmet.append(
                f"campos não resolvidos (válidos em `partial`, não em `complete`): "
                f"{', '.join(unresolved)}"
            )
        open_needs = self.open_needs()
        if open_needs:
            unmet.append(
                f"{len(open_needs)} obrigação(ões) de leitura aberta(s), ex.: "
                + ", ".join(n.target for n in open_needs[:3])
            )
        implicit = self.matrix.implicit_pending()
        if implicit:
            unmet.append(
                f"{len(implicit)} célula(s) da matriz §6.5 nunca avaliada(s), ex.: "
                + ", ".join(f"{c.family}/{c.item}" for c in implicit[:3])
            )
        unresolved_cells = [c for c in self.matrix.unresolved_cells() if c.marked]
        if unresolved_cells:
            unmet.append(
                f"{len(unresolved_cells)} célula(s) da matriz §6.5 em 'unresolved' "
                "(desconhecido explícito: mantém `partial`)"
            )
        dropped = int(self.accounting.get("reading_needs_dropped", 0))
        if dropped:
            unmet.append(
                f"{dropped} obrigação(ões) de leitura descartada(s) por orçamento no "
                "planejamento: o escopo não foi coberto"
            )
        return unmet

    def evaluate(self) -> ObjectiveState:
        """Estado que a evidência atual sustenta — nunca o estado desejado."""
        if self.state is ObjectiveState.BLOCKED:
            return ObjectiveState.BLOCKED
        if not self.unmet_obligations():
            return ObjectiveState.COMPLETE
        explained = [
            n for n in CONTRACT_FIELDS if self.contract[n].status is ContractFieldStatus.FILLED
        ]
        if not explained:
            return ObjectiveState.BLOCKED
        return ObjectiveState.PARTIAL

    # -- escrita -----------------------------------------------------------
    def set_state(self, state: ObjectiveState | str) -> ObjectiveState:
        """Transição de estado com a regra do §6.6 aplicada.

        `complete` só passa com `unmet_obligations()` vazio. É aqui que
        "não transformar `partial` em `complete` para cumprir quantidade de
        documentos" deixa de ser recomendação e vira `ConclusionRejected`.
        """
        state = ObjectiveState(state)
        if state is ObjectiveState.COMPLETE:
            unmet = self.unmet_obligations()
            if unmet:
                raise ConclusionRejected(
                    f"objetivo {self.objective_id} ({self.name}) não pode ser declarado "
                    f"'complete': {len(unmet)} obrigação(ões) aberta(s) — "
                    + "; ".join(unmet)
                )
        self.state = state
        return self.state

    def assert_accounted(self) -> None:
        """§6.6 — todo elemento descoberto terminou em um dos três destinos."""
        problems: list[str] = []
        pending = self.pending_fields()
        if pending:
            problems.append(
                f"campos do §6.3 em 'pending': {', '.join(pending)} — nem explicados, nem "
                "excluídos com motivo, nem não resolvidos com impacto"
            )
        open_needs = [n.target for n in self.open_needs()]
        if open_needs:
            problems.append(
                f"{len(open_needs)} obrigação(ões) de leitura sem satisfação nem dispensa: "
                + ", ".join(open_needs[:5])
            )
        implicit = self.matrix.implicit_pending()
        if implicit:
            problems.append(
                f"{len(implicit)} célula(s) da matriz §6.5 nunca avaliada(s) "
                "(pendência implícita)"
            )
        if problems:
            raise ObjectiveAccountingError(
                f"objetivo {self.objective_id} ({self.name}): " + "; ".join(problems)
            )

    # -- serialização ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "kind": self.kind.value,
            "capability_id": self.capability_id,
            "name": self.name,
            "entry_keys": list(self.entry_keys),
            "symbols": list(self.symbols),
            "contract": {n: self.contract[n].to_dict() for n in CONTRACT_FIELDS},
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "reading_needs": [n.to_dict() for n in self.reading_needs],
            "matrix": self.matrix.to_dict(),
            "state": self.state.value,
            "closure": self.closure.to_dict() if self.closure else None,
            "accounting": dict(self.accounting),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InvestigationObjective":
        closure = data.get("closure")
        return cls(
            objective_id=str(data["objective_id"]),
            kind=ObjectiveKind(data["kind"]),
            capability_id=str(data.get("capability_id", "")),
            name=str(data.get("name", "")),
            entry_keys=tuple(data.get("entry_keys", ())),
            symbols=tuple(data.get("symbols", ())),
            contract={
                n: ContractField.from_dict(raw)
                for n, raw in (data.get("contract") or {}).items()
            },
            evidence_refs=[EvidenceRef.from_dict(e) for e in data.get("evidence_refs", ())],
            reading_needs=[ReadingNeed.from_dict(n) for n in data.get("reading_needs", ())],
            matrix=FailureEdgeMatrix.from_dict(data.get("matrix") or {}),
            state=ObjectiveState(data.get("state", "partial")),
            closure=ClosureRecord.from_dict(closure) if closure else None,
            accounting=dict(data.get("accounting", {})),
            notes=list(data.get("notes", ())),
        )


def close(
    objective: InvestigationObjective, reason: ClosureReason | str, note: str = ""
) -> ClosureRecord:
    """Encerra um objetivo registrando o motivo (§6.4).

    - `objetivo_atendido` passa por `set_state(COMPLETE)` e portanto herda a
      recusa do §6.6: encerrar dizendo "atendido" com obrigação aberta levanta
      `ConclusionRejected`.
    - `bloqueio` marca `blocked` e exige `note` com o que bloqueou.
    - `fronteira_explicita` e `orcamento` exigem `note` e mantêm `partial`
      (ou `complete`, se por acaso já era elegível).
    """
    reason = ClosureReason(reason)
    if objective.closure is not None:
        raise InvestigationError(
            f"objetivo {objective.objective_id} já encerrado por "
            f"{objective.closure.reason.value!r}: reabrir exige novo objetivo/revisão"
        )
    if reason is ClosureReason.OBJETIVO_ATENDIDO:
        objective.set_state(ObjectiveState.COMPLETE)
    elif reason is ClosureReason.BLOQUEIO:
        if not note:
            raise InvestigationError("encerrar por 'bloqueio' exige note com o bloqueio material")
        objective.state = ObjectiveState.BLOCKED
    else:
        if not note:
            raise InvestigationError(
                f"encerrar por {reason.value!r} exige note dizendo onde a fronteira/o orçamento "
                "parou e o que ficou de fora"
            )
        if objective.state is not ObjectiveState.COMPLETE:
            objective.state = ObjectiveState.PARTIAL
    record = ClosureRecord(
        reason=reason,
        note=note,
        state_at_close=objective.state,
        closed_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    )
    objective.closure = record
    return record


# --------------------------------------------------------------------------
# Classificação de lacuna
# --------------------------------------------------------------------------

_PY_BUILTIN_NAMES: frozenset[str] = frozenset(dir(_py_builtins))


def classify_gap(gap: BoundaryGap) -> tuple[str, str]:
    """`(classe, motivo)` de uma lacuna de fronteira.

    Só existe uma classe de exclusão, e ela é verificável: nome simples que é
    membro de `builtins` num arquivo Python. `len(x)` não é comportamento deste
    repositório e não vira obrigação de leitura — mas a exclusão é *nomeada*,
    contada e reportada, jamais um filtro silencioso (§6.1.3).
    """
    if (
        gap.kind == "call"
        and gap.language == "python"
        and "." not in gap.to_name
        and gap.to_name in _PY_BUILTIN_NAMES
    ):
        return (
            "language_runtime",
            f"{gap.to_name!r} é nome do runtime da linguagem (builtins do Python), "
            "fora do código deste escopo: ler sua implementação não descreve esta capacidade",
        )
    return ("behavior", "")


# --------------------------------------------------------------------------
# Planejamento
# --------------------------------------------------------------------------


def _need(
    prefix: str,
    kind: ReadingKind,
    target: str,
    motivo: str,
    trigger: ReadingTrigger,
    evidence: EvidenceRef | None,
) -> ReadingNeed:
    need_id = "need_" + fingerprint([prefix, kind.value, target, trigger.value])[:24]
    return ReadingNeed(
        need_id=need_id,
        kind=kind,
        target=target,
        motivo=motivo,
        trigger=trigger,
        evidence=evidence,
        priority=_TRIGGER_PRIORITY[trigger],
    )


def _needs_from_gaps(
    prefix: str, gaps: Sequence[BoundaryGap]
) -> tuple[list[ReadingNeed], list[tuple[BoundaryGap, str]]]:
    """§6.4 — "se não resolver, abrir lacuna rastreável"."""
    needs: list[ReadingNeed] = []
    excluded: list[tuple[BoundaryGap, str]] = []
    for gap in gaps:
        klass, motivo_exc = classify_gap(gap)
        if klass != "behavior":
            excluded.append((gap, motivo_exc))
            continue
        needs.append(
            _need(
                prefix,
                ReadingKind.SYMBOL,
                gap.to_name,
                (
                    f"chamada/herança não resolvida em {gap.from_symbol} "
                    f"({gap.path}:{gap.line}, {gap.occurrences}x). Motivo do extrator: "
                    f"{gap.reason or 'não informado'}. Resolver o alvo, ler a implementação e "
                    "registrar o vínculo; não resolvendo, a lacuna permanece com este impacto: "
                    f"{gap.impact}"
                ),
                ReadingTrigger.UNRESOLVED_CALL,
                gap.evidence,
            )
        )
    return needs, excluded


def _needs_from_externals(
    prefix: str, deps: Sequence[ExternalDependency]
) -> list[ReadingNeed]:
    """§6.4 — distinguir contrato consumido de implementação externa indisponível."""
    return [
        _need(
            prefix,
            ReadingKind.CONTRACT,
            dep.module,
            (
                f"integração/dependência externa {dep.module!r} importada por {dep.imported_by} "
                f"({dep.path}:{dep.line}, {dep.occurrences}x). Ler o CONTRATO CONSUMIDO no ponto "
                "de uso (operação, parâmetros, timeout, retry, fallback, idempotência). A "
                "implementação externa está fora do escopo analisado e não deve ser presumida"
            ),
            ReadingTrigger.EXTERNAL_INTEGRATION,
            dep.evidence,
        )
        for dep in deps
    ]


def plan(
    capability_map: CapabilityMap,
    extraction: ExtractionResult,
    *,
    inventory: Inventory | None = None,
    snapshot: Snapshot | None = None,
    max_reading_needs: int | None = None,
) -> list[InvestigationObjective]:
    """Gera um objetivo por capacidade e um por grupo de símbolos órfãos.

    Nunca por documento: não há parâmetro de tipo de documento, de quantidade
    nem de granularidade editorial. O denominador é o `CapabilityMap`, e
    `assert_plan_accounted()` confere que nenhuma capacidade e nenhum órfão
    ficou sem objetivo.

    `max_reading_needs` é orçamento de pacote (§7.3.1) e **não** filtro de
    escopo. Por isso o padrão é `None` (sem corte): o orçamento é decisão do
    runtime que despacha, não do planejamento — planejar já cortando esconderia
    escopo antes de alguém decidir sobre ele. Quando um teto é passado, o que
    ficou de fora é contado em `accounting["reading_needs_dropped"]`, entra em
    `notes` e passa a impedir `complete` (ver `unmet_obligations`).
    """
    objectives: list[InvestigationObjective] = []

    config_by_path: dict[str, list[Any]] = {}
    for item in extraction.configuration:
        config_by_path.setdefault(item.path, []).append(item)
    symbols_by_qual = {s.qualname or s.name: s for s in extraction.symbols}
    test_paths: set[str] = set()
    if inventory is not None:
        test_paths = {f.path for f in inventory.files if f.file_class is FileClass.TEST}
    tests_by_target: dict[str, list[Any]] = {}
    for ref in extraction.references:
        if ref.resolved and ref.target and ref.path in test_paths:
            tests_by_target.setdefault(ref.target, []).append(ref)

    for cap in capability_map.capabilities:
        objectives.append(
            _objective_for_capability(
                cap,
                snapshot=snapshot,
                symbols_by_qual=symbols_by_qual,
                config_by_path=config_by_path,
                tests_by_target=tests_by_target,
                max_reading_needs=max_reading_needs,
            )
        )

    by_module: dict[str, list[OrphanSymbol]] = {}
    for orphan in capability_map.orphans:
        by_module.setdefault(orphan.module or orphan.path, []).append(orphan)
    for module in sorted(by_module):
        objectives.append(
            _objective_for_orphans(
                module, by_module[module], max_reading_needs=max_reading_needs
            )
        )
    return objectives


def _objective_for_capability(
    cap: CapabilityCandidate,
    *,
    snapshot: Snapshot | None,
    symbols_by_qual: Mapping[str, Any],
    config_by_path: Mapping[str, Sequence[Any]],
    tests_by_target: Mapping[str, Sequence[Any]],
    max_reading_needs: int | None,
) -> InvestigationObjective:
    prefix = cap.capability_id
    needs, excluded_gaps = _needs_from_gaps(prefix, cap.gaps)
    needs.extend(_needs_from_externals(prefix, cap.external_dependencies))

    # Configuração (§6.4): default/override/origem — nunca inventar ambiente.
    seen_cfg: set[str] = set()
    for path in cap.paths:
        for item in config_by_path.get(path, ()):
            if item.keypath in seen_cfg:
                continue
            seen_cfg.add(item.keypath)
            needs.append(
                _need(
                    prefix,
                    ReadingKind.RANGE,
                    f"{item.path}:{item.line_start}-{item.line_end}",
                    (
                        f"configuração {item.keypath!r} (kind={item.kind}) tocada por esta "
                        "capacidade: capturar default declarado, pontos de override e origem "
                        "conhecida. Valor de ambiente NÃO pode ser presumido"
                    ),
                    ReadingTrigger.CONFIGURATION,
                    evidence_ref_for(
                        snapshot,
                        item.path,
                        item.line_start,
                        item.line_end,
                        role="configuration",
                        symbol=item.keypath,
                    ),
                )
            )

    # Predicado e efeito (§6.4) por símbolo alcançado.
    sql_by_path: dict[str, list[Any]] = {}
    for path in cap.paths:
        for item in config_by_path.get(path, ()):
            if item.kind == "sql_literal":
                sql_by_path.setdefault(path, []).append(item)
    for qual in cap.reachable_symbols:
        sym = symbols_by_qual.get(qual)
        if sym is None or sym.kind not in ("function", "method"):
            continue
        ev = evidence_ref_for(
            snapshot, sym.path, sym.line_start, sym.line_end, role="symbol_body", symbol=qual
        )
        needs.append(
            _need(
                prefix,
                ReadingKind.RANGE,
                qual,
                (
                    "corpo alcançado pela capacidade: a extração estrutural não expõe ramos. "
                    "Ler para levantar predicados, precedência, short-circuit e ordem de "
                    "avaliação (§6.3 Decisões)"
                ),
                ReadingTrigger.PREDICATE,
                ev,
            )
        )
        hits = [
            item
            for item in sql_by_path.get(sym.path, ())
            if sym.line_start <= item.line_start <= sym.line_end
        ]
        if hits:
            needs.append(
                _need(
                    prefix,
                    ReadingKind.RANGE,
                    f"{qual}#efeito",
                    (
                        f"efeito de persistência dentro de {qual}: {len(hits)} literal(is) SQL "
                        f"no intervalo (ex.: {hits[0].keypath}). Identificar a condição de "
                        "execução do efeito e o que acontece com ele em caso de falha "
                        "subsequente (§6.3 Persistência/Falhas)"
                    ),
                    ReadingTrigger.EFFECT,
                    ev,
                )
            )

    # Teste vinculado (§6.4): assertions <-> comportamento; divergência é achado.
    test_evidence: list[EvidenceRef] = []
    seen_tests: set[str] = set()
    for qual in cap.reachable_symbols:
        for ref in tests_by_target.get(qual, ()):
            key = f"{ref.path}:{ref.from_symbol}->{qual}"
            if key in seen_tests:
                continue
            seen_tests.add(key)
            ev = evidence_ref_for(
                snapshot, ref.path, ref.line, ref.line_end or ref.line,
                role="linked_test", symbol=ref.from_symbol,
            )
            test_evidence.append(ev)
            needs.append(
                _need(
                    prefix,
                    ReadingKind.SYMBOL,
                    ref.from_symbol,
                    (
                        f"teste {ref.from_symbol} exercita {qual} ({ref.path}:{ref.line}). "
                        "Vincular cada assertion ao comportamento afirmado e registrar os "
                        "limites do teste; divergência entre teste e código é achado, não ruído"
                    ),
                    ReadingTrigger.LINKED_TEST,
                    ev,
                )
            )

    # Orçamento de pacote: corte é contado e passa a impedir `complete`.
    needs.sort(key=lambda n: (n.priority, n.target))
    dropped = 0
    if max_reading_needs is not None and len(needs) > max_reading_needs:
        dropped = len(needs) - max_reading_needs
        needs = needs[:max_reading_needs]

    objective_id = "obj_" + fingerprint(["capability", cap.capability_id])[:24]
    obj = InvestigationObjective(
        objective_id=objective_id,
        kind=ObjectiveKind.CAPABILITY,
        capability_id=cap.capability_id,
        name=cap.name,
        entry_keys=cap.entry_keys,
        symbols=cap.reachable_symbols,
        evidence_refs=list(cap.evidence_refs),
        reading_needs=needs,
        accounting={
            "entrypoints": len(cap.entrypoints),
            "reachable_symbols": len(cap.reachable_symbols),
            "gaps_total": len(cap.gaps),
            "gaps_excluded_language_runtime": len(excluded_gaps),
            "external_dependencies": len(cap.external_dependencies),
            "reading_needs": len(needs),
            "reading_needs_dropped": dropped,
        },
        notes=list(cap.notes),
    )
    if dropped:
        obj.notes.append(
            f"{dropped} obrigação(ões) de leitura acima do orçamento de {max_reading_needs} "
            "não entraram no pacote; enquanto isso não for revisto o objetivo não pode ser "
            "declarado complete"
        )
    if excluded_gaps:
        obj.notes.append(
            f"{len(excluded_gaps)} lacuna(s) excluída(s) como runtime da linguagem "
            f"(ex.: {excluded_gaps[0][0].to_name!r}) — exclusão nomeada, não filtro silencioso"
        )
    _prefill_contract(obj, cap, test_evidence)
    return obj


def _prefill_contract(
    obj: InvestigationObjective,
    cap: CapabilityCandidate,
    test_evidence: Sequence[EvidenceRef],
) -> None:
    """Preenche apenas o que a extração estrutural sustenta. O resto fica `pending`.

    Deixar `dados`, `decisoes`, `persistencia`, `integracoes`, `sucesso`,
    `falhas`, `edge_cases` e `precondicoes` em `pending` é deliberado: nenhum
    deles é dedutível de símbolos e arestas, e preenchê-los aqui seria a
    "afirmação sem evidência primária" que o W3 proíbe.
    """
    ident_ev = list(obj.evidence_refs)
    if ident_ev:
        obj.field("identidade").fill(
            (
                f"Nome técnico: {cap.name}. Entradas ({len(cap.entrypoints)}): "
                + ", ".join(f"{e.kind}:{e.name}" for e in cap.entrypoints[:8])
                + (" …" if len(cap.entrypoints) > 8 else "")
                + f". Módulos tocados: {', '.join(cap.modules[:8]) or '(nenhum resolvido)'}"
                + f". Agrupamento: {cap.grouping_basis.value}."
                " Nome de NEGÓCIO não é dedutível de código (lacuna registrada; requer fonte de negócio)."
            ),
            ident_ev,
        )
    else:
        obj.field("identidade").unresolve(
            "nenhum ponto-chave pôde ser citado com localizador: sem evidência primária, a "
            "identidade técnica desta capacidade não é afirmável"
        )

    entry_ev = [e.evidence for e in cap.entrypoints if e.evidence]
    if entry_ev:
        kinds = sorted({e.kind for e in cap.entrypoints})
        frameworks = sorted({e.framework for e in cap.entrypoints if e.framework})
        obj.field("gatilho").fill(
            f"Espécies de entrada: {', '.join(kinds)}."
            + (f" Frameworks reconhecidos: {', '.join(frameworks)}." if frameworks else "")
            + " Condições de disparo (agendamento, filtro de rota, seletor de evento) exigem"
            " leitura do ponto de entrada.",
            entry_ev,
        )
    else:
        obj.field("gatilho").unresolve(
            "entradas descobertas sem evidência citável: o gatilho não pode ser afirmado"
        )

    dep_ev = [d.evidence for d in cap.external_dependencies if d.evidence]
    if cap.external_dependencies and dep_ev:
        obj.field("dependencias").fill(
            f"{len(cap.external_dependencies)} dependência(s) externa(s) ao escopo analisado: "
            + ", ".join(d.module for d in cap.external_dependencies[:10])
            + (" …" if len(cap.external_dependencies) > 10 else "")
            + f". Módulos internos tocados: {len(cap.modules)}.",
            dep_ev,
        )
    elif cap.external_dependencies:
        obj.field("dependencias").unresolve(
            f"{len(cap.external_dependencies)} dependência(s) externa(s) sem evidência citável"
        )
    else:
        obj.field("dependencias").exclude(
            "nenhum import não resolvido nos módulos tocados: no conjunto analisado esta "
            "capacidade não consome código fora do escopo"
        )

    gap_ev = [g.evidence for g in cap.gaps if g.evidence]
    if cap.gaps and gap_ev:
        obj.field("lacunas").fill(
            f"{len(cap.gaps)} referência(s) não resolvida(s) na fronteira do fecho "
            f"({int(obj.accounting.get('gaps_excluded_language_runtime', 0))} excluída(s) como "
            "runtime da linguagem). Enquanto não resolvidas, o comportamento além delas é "
            "desconhecido para os consumidores desta capacidade.",
            gap_ev,
        )
    elif cap.gaps:
        obj.field("lacunas").unresolve(
            f"{len(cap.gaps)} lacuna(s) de fronteira sem evidência citável nesta execução"
        )
    else:
        obj.field("lacunas").exclude(
            "nenhuma referência não resolvida partindo dos símbolos alcançados no conjunto "
            "analisado"
        )

    if test_evidence:
        obj.field("verificacao").fill(
            f"{len(test_evidence)} vínculo(s) teste->símbolo alcançado detectado(s) por "
            "referência resolvida. Os limites de cada teste e a correspondência assertion <-> "
            "comportamento exigem leitura (obrigações trigger=linked_test).",
            list(test_evidence),
        )
    else:
        obj.field("verificacao").unresolve(
            "nenhum teste do escopo referencia os símbolos alcançados por esta capacidade: "
            "não há verificação existente para sustentar afirmações sobre o comportamento"
        )


def _objective_for_orphans(
    module: str, orphans: Sequence[OrphanSymbol], *, max_reading_needs: int | None
) -> InvestigationObjective:
    """§6.1.8 — símbolo público não alcançado também abre tarefa de investigação."""
    prefix = f"orphans:{module}"
    needs = [
        _need(
            prefix,
            ReadingKind.SYMBOL,
            o.qualname,
            (
                f"símbolo público {o.kind} não alcançado por nenhuma entrada "
                f"({o.path}:{o.line_start}). Determinar se há entrada não detectada, se é API "
                "consumida de fora do escopo, ou se é código morto — e registrar qual"
            ),
            ReadingTrigger.UNRESOLVED_CALL,
            o.evidence,
        )
        for o in orphans
    ]
    needs.sort(key=lambda n: (n.priority, n.target))
    dropped = 0
    if max_reading_needs is not None and len(needs) > max_reading_needs:
        dropped = len(needs) - max_reading_needs
        needs = needs[:max_reading_needs]

    obj = InvestigationObjective(
        objective_id="obj_" + fingerprint(["orphans", module])[:24],
        kind=ObjectiveKind.ORPHAN_GROUP,
        capability_id="",
        name=f"orfaos@{module}",
        symbols=tuple(o.qualname for o in orphans),
        evidence_refs=[o.evidence for o in orphans if o.evidence],
        reading_needs=needs,
        accounting={
            "orphan_symbols": len(orphans),
            "reading_needs": len(needs),
            "reading_needs_dropped": dropped,
        },
    )
    ev = obj.evidence_refs
    if ev:
        obj.field("identidade").fill(
            f"{len(orphans)} símbolo(s) público(s) em {module} sem entrada que os alcance: "
            + ", ".join(o.qualname for o in orphans[:8])
            + (" …" if len(orphans) > 8 else ""),
            ev,
        )
    else:
        obj.field("identidade").unresolve(
            f"{len(orphans)} símbolo(s) órfão(s) em {module} sem evidência citável"
        )
    obj.field("gatilho").unresolve(
        "nenhuma entrada do escopo alcança estes símbolos: o gatilho é desconhecido e, "
        "se existir, está fora do conjunto analisado"
    )
    return obj


# --------------------------------------------------------------------------
# Denominadores do plano (§6.6)
# --------------------------------------------------------------------------


def plan_accounting(
    objectives: Sequence[InvestigationObjective], capability_map: CapabilityMap
) -> dict[str, int]:
    return {
        "capabilities": len(capability_map.capabilities),
        "objectives": len(objectives),
        "objectives_capability": sum(
            1 for o in objectives if o.kind is ObjectiveKind.CAPABILITY
        ),
        "objectives_orphan_group": sum(
            1 for o in objectives if o.kind is ObjectiveKind.ORPHAN_GROUP
        ),
        "orphan_symbols": len(capability_map.orphans),
        "reading_needs": sum(len(o.reading_needs) for o in objectives),
        "reading_needs_dropped": sum(
            int(o.accounting.get("reading_needs_dropped", 0)) for o in objectives
        ),
        "entrypoints_total": int(capability_map.totals.get("entrypoints_total", 0)),
        "entry_keys_in_objectives": sum(len(o.entry_keys) for o in objectives),
    }


def assert_plan_accounted(
    objectives: Sequence[InvestigationObjective], capability_map: CapabilityMap
) -> None:
    """Toda capacidade e todo órfão têm exatamente um objetivo (§6.6)."""
    cap_ids = [c.capability_id for c in capability_map.capabilities]
    covered = [o.capability_id for o in objectives if o.kind is ObjectiveKind.CAPABILITY]
    if len(covered) != len(set(covered)):
        raise PlanAccountingError("capacidade com mais de um objetivo: despacho duplicado")
    missing = sorted(set(cap_ids) - set(covered))
    if missing:
        raise PlanAccountingError(
            f"{len(missing)} capacidade(s) sem objetivo: {missing[:3]} (§6.6)"
        )
    extra = sorted(set(covered) - set(cap_ids))
    if extra:
        raise PlanAccountingError(
            f"{len(extra)} objetivo(s) para capacidade inexistente: {extra[:3]}"
        )
    orphan_names = {o.qualname for o in capability_map.orphans}
    planned: list[str] = []
    for obj in objectives:
        if obj.kind is ObjectiveKind.ORPHAN_GROUP:
            planned.extend(obj.symbols)
    if len(planned) != len(set(planned)):
        raise PlanAccountingError("símbolo órfão em mais de um objetivo")
    if set(planned) != orphan_names:
        lost = sorted(orphan_names - set(planned))
        raise PlanAccountingError(
            f"{len(lost)} símbolo(s) órfão(s) sem objetivo: {lost[:3]} — símbolo público "
            "descoberto não pode desaparecer do plano (§6.1.8)"
        )


def objectives_to_dict(objectives: Iterable[InvestigationObjective]) -> list[dict[str, Any]]:
    """Pacote serializável para o runtime (W4)."""
    return [o.to_dict() for o in objectives]


def objectives_from_dict(
    payload: Iterable[Mapping[str, Any]]
) -> list[InvestigationObjective]:
    return [InvestigationObjective.from_dict(item) for item in payload]
