"""Objetivos de investigação, obrigações de leitura e critério de conclusão.

Implementa §6.3 (contrato de investigação de uma capacidade), §6.4
(investigação adaptativa), §6.5 (matriz de falhas e edge cases) e §6.6
(critério de conclusão) em código executável.

O que este módulo garante — cada item tem uma função que o aplica, não uma
frase que o promete:

| Regra do plano | Onde é aplicada |
|---|---|
| Objetivo por capacidade/entrada, nunca por documento | `plan()` itera `CapabilityMap.capabilities` e os órfãos; não existe parâmetro de quantidade nem de tipo de documento |
| Análise não depende de haver extrator para a linguagem | `plan()` abre `ObjectiveKind.DISCOVERY` com `ReadingTrigger.SOURCE_FILE` para todo arquivo de código do inventário que nenhuma obrigação alcançou; `plan_coverage()` mede e `assert_plan_accounted(inventory=...)` recusa arquivo sem objetivo |
| Teto de pacote particiona, não descarta | `_apply_budget()` com `defer_sink` manda o excedente para objetivo de descoberta; `reading_needs_dropped` só sobra para obrigação sem arquivo citável |
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
import hashlib
import os
from dataclasses import dataclass, field, replace as _dc_replace
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
from .profile import (
    DEFAULT_PROFILE,
    DISCOVERY_MAX_FILES_PER_OBJECTIVE,
    AnalysisProfile,
    ProfileError,
)
from .snapshot import Snapshot

__all__ = [
    "ANALYSIS_DIRECTIVES",
    "CONTRACT_FIELDS",
    "CONTRACT_LABELS",
    "DISCOVERY_DIRECTIVES",
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
    "MatrixIntegrityError",
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
    "plan_coverage",
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


class MatrixIntegrityError(InvestigationError):
    """§6.5 — matriz reconstruída de payload com família/célula fora do conjunto
    permitido, ou com estado afirmativo sem justificativa.

    Fecha a brecha entre `mark()` (que sempre exigiu justificativa) e
    `from_dict()` (que aceitava o payload como verdade): reconstruir não pode
    ser um caminho mais barato do que marcar.
    """


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

#: Instruções que viajam DENTRO do objetivo serializado
#: (`InvestigationObjective.analysis_directives` -> `to_dict()`), para os TRÊS
#: tipos de objetivo. Não é documentação: é o campo que o runtime coloca no
#: envelope do pacote, e é sobre ele que o teste de contrato afirma. O ponto
#: das três primeiras linhas é fechar a porta que faz uma análise parecer
#: completa sem ser: descrever só o que o código declara, e aceitar prosa
#: (README, comentário, docstring) como se fosse comportamento.
ANALYSIS_DIRECTIVES: tuple[str, ...] = (
    "Capture TODAS as regras de negócio, invariantes, pré-condições e pós-condições, "
    "edge cases e comportamentos implícitos observáveis no código — inclusive os que "
    "não estão declarados em lugar nenhum e só existem no fluxo de execução.",
    "Cada afirmação exige evidência de CÓDIGO EXECUTÁVEL citada por path + faixa de "
    "linhas. Afirmação sem evidência é lacuna registrada, nunca conteúdo.",
    "README, comentários, docstrings e qualquer texto em prosa NÃO são evidência e não "
    "podem ser usados como fonte: descrevem intenção declarada, não o comportamento "
    "implementado — quando divergirem do código, o código é a verdade e a divergência "
    "é um achado.",
    "Os 13 campos do contrato §6.3 terminam explicados com evidência, excluídos com "
    "motivo ou não resolvidos com impacto. Nenhum campo fica em branco.",
    "A matriz de falhas e edge cases §6.5 é preenchida célula a célula: 'covered' e "
    "'not_applicable' exigem justificativa ligada ao código; sem evidência a célula "
    "permanece 'unresolved' — ausência de evidência nunca vira cobertura.",
)

#: Acrescentadas SÓ ao objetivo de descoberta: não houve extração estrutural
#: para estes arquivos, então o que substitui símbolos e arestas é a leitura
#: integral do fonte pelo próprio modelo.
DISCOVERY_DIRECTIVES: tuple[str, ...] = (
    "Objetivo de DESCOBERTA: não existe extração estrutural para estes arquivos. Leia "
    "o fonte INTEIRO de cada arquivo listado nas obrigações de leitura antes de "
    "afirmar qualquer coisa sobre o módulo.",
    "Mapeie para este módulo, com path + linhas em cada item: capacidades e o que cada "
    "uma faz; pontos de entrada (rota, job, fila, CLI, batch, transação); regras de "
    "negócio e a ordem em que se aplicam; integrações externas e o contrato consumido; "
    "persistência (o que é lido/escrito, quando e sob qual transação); e o que acontece "
    "em cada falha, inclusive com efeito já executado.",
    "A ausência de extrator para a linguagem é limitação da ferramenta, não do escopo: "
    "o módulo é descrito lendo o código, no mesmo contrato de 13 campos e na mesma "
    "matriz §6.5 dos demais objetivos.",
)

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
    """Origem do objetivo. Nenhum é por documento."""

    CAPABILITY = "capability"
    #: §6.1.8 — símbolo público que nenhuma entrada alcança também abre tarefa.
    ORPHAN_GROUP = "orphan_group"
    #: Arquivo de código do inventário que a extração estrutural NÃO cobriu:
    #: linguagem sem adaptador, extração vazia, ou arquivo que nenhuma
    #: capacidade/órfão alcançou. O objetivo existe para que a análise não
    #: dependa de haver extrator — o modelo lê o fonte direto.
    DISCOVERY = "discovery"


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
    #: Arquivo de código inteiro, quando não há extração que o descreva.
    SOURCE_FILE = "source_file"


class MatrixState(str, enum.Enum):
    """§6.5 — estados de cobertura."""

    COVERED = "covered"
    NOT_APPLICABLE = "not_applicable"
    UNRESOLVED = "unresolved"


#: Prioridade de despacho (menor = antes). Lacuna rastreável e efeito vêm
#: primeiro porque são o que impede afirmar comportamento; predicado de corpo
#: vem por último porque é o mais volumoso.
_TRIGGER_PRIORITY: Mapping[ReadingTrigger, int] = {
    #: Prioridade MÁXIMA: enquanto o fonte não foi lido não existe nem
    #: símbolo nem aresta sobre a qual priorizar qualquer outra coisa.
    ReadingTrigger.SOURCE_FILE: 5,
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
    #: Proveniência NÃO-código de uma justificativa: hoje só o perfil de análise
    #: do sistema (`"profile:<camada>"`). Existe para que uma exclusão declarada
    #: pelo perfil seja distinguível de uma exclusão sustentada por evidência de
    #: código — e para que nem uma nem outra possa ser vazia.
    justification_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "item": self.item,
            "state": self.state.value,
            "justification": self.justification.to_dict() if self.justification else None,
            "note": self.note,
            "marked": self.marked,
            "justification_source": self.justification_source,
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
            justification_source=str(data.get("justification_source", "")),
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
        cell.justification_source = ""
        return cell

    # -- escrita vinda do perfil de análise --------------------------------
    def exclude_family(self, family: str, motivo: str, *, source: str) -> list[MatrixCell]:
        """Marca TODA célula de `family` como `not_applicable` com motivo do perfil.

        É o único caminho que produz `not_applicable` sem `EvidenceRef`, e ele
        custa exatamente o mesmo que `mark()`: motivo não vazio (senão
        `MatrixJustificationRequired`) e proveniência registrada em
        `justification_source`, que sai em `to_dict()`. Um consumidor consegue
        separar "não se aplica, disse o código" de "não se aplica, disse o
        perfil deste sistema".
        """
        if not (motivo or "").strip():
            raise MatrixJustificationRequired(
                f"família {family!r}: exclusão pelo perfil exige motivo não vazio — "
                "exclusão silenciosa é proibida (§6.1.3/§6.5)"
            )
        if family not in self._families:
            raise MatrixIntegrityError(
                f"família desconhecida na matriz: {family!r} "
                f"(presentes: {', '.join(sorted(self._families))})"
            )
        touched: list[MatrixCell] = []
        for item in self._families[family]:
            cell = self.cells[(family, item)]
            cell.state = MatrixState.NOT_APPLICABLE
            cell.justification = None
            cell.justification_source = source
            cell.note = motivo.strip()
            cell.marked = True
            touched.append(cell)
        return touched

    def add_family(
        self, family: str, items: Sequence[str], *, source: str
    ) -> list[MatrixCell]:
        """Acrescenta uma família nova com células `unresolved` (o padrão do §6.5).

        Célula nova nasce por obrigação, não por cobertura: `unresolved` e
        `marked=False` mantêm o objetivo em `partial` até alguém marcar com
        evidência. `justification_source` guarda de onde veio a família para que
        `from_dict()` a reconheça como declarada, e não como injetada.
        """
        if family in self._families:
            raise MatrixIntegrityError(
                f"família {family!r} já existe na matriz: redefinir apagaria as células atuais"
            )
        parsed = tuple(str(i).strip() for i in items if str(i).strip())
        if not parsed:
            raise MatrixIntegrityError(
                f"família {family!r} sem itens: família vazia não acrescenta obrigação nenhuma"
            )
        self._families[family] = parsed
        created = [
            MatrixCell(family=family, item=item, justification_source=source)
            for item in parsed
        ]
        for cell in created:
            self.cells[(family, cell.item)] = cell
        return created

    # -- proveniência de perfil -------------------------------------------
    def profile_cells(self) -> frozenset[tuple[str, str]]:
        """Células cuja justificativa vem do PERFIL, não de código.

        É o conjunto que um resultado de agente não pode nem criar nem remover:
        quem decidiu que uma família não se aplica a este sistema foi o perfil,
        e o worker não tem autoridade para revogar nem para inventar essa
        decisão. `reapply_profile_cells()` é o que impõe isso.
        """
        return frozenset(
            (c.family, c.item)
            for c in self.cells.values()
            if (c.justification_source or "").strip()
        )

    def reapply_profile_cells(self, prior: "FailureEdgeMatrix") -> dict[str, list[str]]:
        """Reimpõe sobre ESTA matriz exatamente as células de perfil de `prior`.

        Uso: `prior` é a matriz do objetivo planejado (escrita pelo pipeline,
        confiável); `self` é a matriz que voltou do agente (não confiável). O
        agente pode ter apagado uma exclusão do perfil para transformar
        obrigação em cobertura, ou forjado uma nova para se dispensar de
        trabalho. Depois desta chamada:

        - toda célula de perfil de `prior` volta idêntica (estado, note e
          `justification_source`), com a família recriada se o agente a removeu;
        - qualquer célula com `justification_source` que NÃO seja de `prior` é
          despida da proveniência forjada e volta a `unresolved`/`marked=False`;
        - as demais células ficam como o agente as deixou.

        Devolve o registro do que foi mexido — reimposição contada, não muda.
        """
        restored: list[str] = []
        revoked: list[str] = []
        legit = prior.profile_cells()
        for family, item in sorted(legit):
            source_cell = prior.cells[(family, item)]
            current = self.cells.get((family, item))
            if current is None or current.to_dict() != source_cell.to_dict():
                self.cells[(family, item)] = _dc_replace(source_cell)
                items = self._families.get(family, ())
                if item not in items:
                    self._families[family] = items + (item,)
                restored.append(f"{family}/{item}")
        for key, cell in sorted(self.cells.items()):
            if key in legit or not (cell.justification_source or "").strip():
                continue
            cell.justification_source = ""
            cell.justification = None
            cell.state = MatrixState.UNRESOLVED
            cell.marked = False
            cell.note = ""
            revoked.append(f"{key[0]}/{key[1]}")
        return {"restored": restored, "revoked_forged_provenance": revoked}

    # -- serialização ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "families": {fam: list(items) for fam, items in self._families.items()},
            "cells": [c.to_dict() for c in self.cells.values()],
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        allowed_families: Mapping[str, Sequence[str]] | None = None,
        trust_profile_cells: bool = False,
    ) -> "FailureEdgeMatrix":
        """Reconstrói a matriz APLICANDO a mesma regra de `mark()` (§6.5).

        Antes desta versão, `from_dict` aceitava do payload qualquer família,
        qualquer célula e qualquer estado: bastava um resultado de agente
        declarar `state: "not_applicable"` para a obrigação sumir sem
        justificativa nenhuma — enquanto `mark()` exigia `EvidenceRef` ligada
        ao código. Reconstruir era o caminho barato. Agora:

        - célula em `covered`/`not_applicable` exige `justification` válida
          (`_require_code_evidence`);
        - célula MARCADA em `unresolved` exige `note` com o impacto;
        - família/célula fora de `allowed_families` (default: `FAILURE_FAMILIES`)
          exige justificativa não vazia — família inventada sem proveniência é
          `MatrixIntegrityError`, nunca uma linha a mais na matriz.

        `trust_profile_cells` decide se `justification_source` (a proveniência
        "isto veio do perfil") vale como justificativa. **O default é `False`, e
        isso é o ponto**: `justification_source` é só uma string, e o payload de
        um agente chega aqui pelo mesmo caminho que o payload do pipeline. Sem
        essa separação, um worker escreveria `"profile:cli"` numa célula e se
        dispensaria da obrigação sem evidência nenhuma — exatamente a brecha que
        `mark()` sempre recusou.

        Quem chama decide pela ORIGEM do payload, nunca pelo conteúdo dele:

        | Origem | `trust_profile_cells` |
        |---|---|
        | matriz escrita por `plan()`/store/estado acumulado | `True` |
        | `output["matrix"]` de um agente | `False` (e depois `reapply_profile_cells`) |
        """
        allowed = allowed_families if allowed_families is not None else FAILURE_FAMILIES
        allowed_pairs = {
            (fam, item) for fam, items in allowed.items() for item in items
        }
        matrix = cls(data.get("families") or allowed)
        for raw in data.get("cells", ()):
            cell = MatrixCell.from_dict(raw)
            matrix.cells[(cell.family, cell.item)] = cell
            matrix._families.setdefault(cell.family, ())
            if cell.item not in matrix._families[cell.family]:
                matrix._families[cell.family] = matrix._families[cell.family] + (cell.item,)
        for cell in matrix.cells.values():
            _validate_reconstructed_cell(cell, allowed_pairs, trust_profile_cells)
        return matrix


def _validate_reconstructed_cell(
    cell: MatrixCell, allowed_pairs: frozenset | set, trust_profile_cells: bool = False
) -> None:
    """Regra única de aceitação de uma célula vinda de payload (ver `from_dict`)."""
    source = (cell.justification_source or "").strip()
    if source and not trust_profile_cells:
        raise MatrixIntegrityError(
            f"{cell.family}/{cell.item}: proveniência de perfil não aceita desta origem "
            f"(justification_source={source!r}). `justification_source` é texto: só o próprio "
            "pipeline pode escrevê-lo, e este payload não veio dele. Se a célula se sustenta, "
            "traga `justification` ligada ao código; se ela é do perfil, quem reconstrói deve "
            "reimpô-la com `reapply_profile_cells(prior)` sobre a matriz anterior validada"
        )
    known = (cell.family, cell.item) in allowed_pairs
    if not known and not source and cell.justification is None:
        raise MatrixIntegrityError(
            f"{cell.family}/{cell.item}: célula fora do conjunto permitido do §6.5 e sem "
            "justificativa — uma família/célula nova precisa declarar de onde veio "
            "(justification_source do perfil) ou trazer evidência de código; aceitar em "
            "silêncio deixaria qualquer payload redesenhar a matriz"
        )
    if cell.state in (MatrixState.NOT_APPLICABLE, MatrixState.COVERED):
        if not source:
            _require_code_evidence(cell.family, cell.item, cell.state, cell.justification)
        elif not (cell.note or "").strip():
            raise MatrixJustificationRequired(
                f"{cell.family}/{cell.item}: estado {cell.state.value!r} declarado por "
                f"{source!r} sem motivo em `note` — exclusão sem motivo não é exclusão (§6.5)"
            )
    elif cell.state is MatrixState.UNRESOLVED and cell.marked and not (cell.note or "").strip():
        raise MatrixIntegrityError(
            f"{cell.family}/{cell.item}: 'unresolved' MARCADO exige note com o impacto do "
            "desconhecido para o consumidor (§6.6) — a mesma exigência de `mark()`"
        )


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
    #: Instruções de análise que o runtime entrega ao modelo junto do objetivo.
    #: Default = `ANALYSIS_DIRECTIVES` para os TRÊS tipos: um objetivo sem
    #: diretiva nenhuma seria um objetivo que aceita prosa como evidência.
    analysis_directives: list[str] = field(
        default_factory=lambda: list(ANALYSIS_DIRECTIVES)
    )

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
            "analysis_directives": list(self.analysis_directives),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        allowed_families: Mapping[str, Sequence[str]] | None = None,
        trust_profile_cells: bool = False,
    ) -> "InvestigationObjective":
        """`trust_profile_cells` só quando o payload foi escrito pelo próprio
        pipeline (ver `FailureEdgeMatrix.from_dict`). Resultado de agente: `False`."""
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
            matrix=FailureEdgeMatrix.from_dict(
                data.get("matrix") or {},
                allowed_families=allowed_families,
                trust_profile_cells=trust_profile_cells,
            ),
            state=ObjectiveState(data.get("state", "partial")),
            closure=ClosureRecord.from_dict(closure) if closure else None,
            accounting=dict(data.get("accounting", {})),
            notes=list(data.get("notes", ())),
            analysis_directives=[
                str(d) for d in data.get("analysis_directives", ANALYSIS_DIRECTIVES)
            ],
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


def _trigger_priority(profile: AnalysisProfile | None) -> Mapping[ReadingTrigger, int]:
    """Prioridade EFETIVA desta chamada. `_TRIGGER_PRIORITY` nunca é mutado.

    O perfil sobrescreve só os gatilhos que nomear; o resto mantém o valor
    histórico, então um perfil que ajusta um gatilho não reordena os outros
    por acidente.
    """
    if profile is None or not profile.trigger_priority:
        return _TRIGGER_PRIORITY
    merged = dict(_TRIGGER_PRIORITY)
    for raw, value in profile.trigger_priority.items():
        merged[ReadingTrigger(raw)] = int(value)
    return merged


def _need(
    prefix: str,
    kind: ReadingKind,
    target: str,
    motivo: str,
    trigger: ReadingTrigger,
    evidence: EvidenceRef | None,
    priorities: Mapping[ReadingTrigger, int] = _TRIGGER_PRIORITY,
) -> ReadingNeed:
    need_id = "need_" + fingerprint([prefix, kind.value, target, trigger.value])[:24]
    return ReadingNeed(
        need_id=need_id,
        kind=kind,
        target=target,
        motivo=motivo,
        trigger=trigger,
        evidence=evidence,
        priority=priorities[trigger],
    )


def _needs_from_gaps(
    prefix: str,
    gaps: Sequence[BoundaryGap],
    priorities: Mapping[ReadingTrigger, int] = _TRIGGER_PRIORITY,
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
                priorities,
            )
        )
    return needs, excluded


def _needs_from_externals(
    prefix: str,
    deps: Sequence[ExternalDependency],
    priorities: Mapping[ReadingTrigger, int] = _TRIGGER_PRIORITY,
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
            priorities,
        )
        for dep in deps
    ]


def _objective_id_for_capability(capability_id: str) -> str:
    return "obj_" + fingerprint(["capability", capability_id])[:24]


def _objective_id_for_orphans(module: str) -> str:
    return "obj_" + fingerprint(["orphans", module])[:24]


def _objective_id_for_discovery(module: str) -> str:
    return "obj_" + fingerprint(["discovery", module])[:24]


def _need_path(need: ReadingNeed) -> str:
    """Arquivo que esta obrigação faz alguém ler, ou `""` quando não há um.

    É o que liga obrigação a ARQUIVO — a unidade em que `plan_coverage()`
    responde "sobrou algum arquivo de código que ninguém vai ler?".
    """
    ev = need.evidence
    if ev is not None and getattr(ev, "path", ""):
        return str(ev.path)
    return ""


def _apply_budget(
    needs: list[ReadingNeed],
    max_reading_needs: int | None,
    defer_sink: list[ReadingNeed] | None,
) -> tuple[list[ReadingNeed], int, int]:
    """Ordena e aplica o orçamento. Devolve `(mantidas, descartadas, diferidas)`.

    Com `defer_sink` (isto é: há inventário, logo há para onde diferir), o que
    não coube NÃO é descartado — a obrigação sai deste pacote e o arquivo dela
    volta obrigatoriamente por um objetivo de descoberta, porque
    `plan()` calcula a cobertura a partir das obrigações que SOBRARAM. Só
    obrigação sem arquivo citável (contrato de dependência externa, por
    exemplo) continua sendo descarte, e descarte continua impedindo `complete`.
    """
    needs.sort(key=lambda n: (n.priority, n.target))
    if max_reading_needs is None or len(needs) <= max_reading_needs:
        return needs, 0, 0
    kept = needs[:max_reading_needs]
    cut = needs[max_reading_needs:]
    if defer_sink is None:
        return kept, len(cut), 0
    deferred = [n for n in cut if _need_path(n)]
    dropped = [n for n in cut if not _need_path(n)]
    defer_sink.extend(deferred)
    return kept, len(dropped), len(deferred)


def _discovery_module(path: str) -> str:
    """Módulo de agrupamento de um arquivo sem extração.

    `Inventory` não tem noção de módulo (só `path`), e a noção de
    `capabilities` depende de um `Symbol` de kind `module` — que por definição
    não existe aqui. Sobra o diretório de 1º/2º nível, que é determinístico e
    não depende de nenhum extrator.
    """
    dirs = path.split("/")[:-1]
    if not dirs:
        return "(raiz)"
    return "/".join(dirs[:2])


def _discovery_groups(
    paths: Sequence[str], limit: int
) -> list[tuple[str, tuple[str, ...]]]:
    """Agrupa por módulo e PARTICIONA pelo teto — nunca corta fora.

    O rótulo do primeiro pedaço é o módulo puro, então um módulo que cabe
    inteiro tem exatamente o id histórico `fingerprint(["discovery", modulo])`;
    pedaços seguintes ganham sufixo `#2`, `#3`… e portanto ids próprios e
    estáveis.
    """
    limit = max(1, int(limit))
    by_module: dict[str, list[str]] = {}
    for path in sorted(set(paths)):
        by_module.setdefault(_discovery_module(path), []).append(path)
    groups: list[tuple[str, tuple[str, ...]]] = []
    for module in sorted(by_module):
        files = by_module[module]
        for start in range(0, len(files), limit):
            chunk = tuple(files[start:start + limit])
            index = start // limit
            groups.append((module if index == 0 else f"{module}#{index + 1}", chunk))
    return groups


def _line_counts(snapshot: Snapshot | None, paths: Sequence[str]) -> dict[str, int]:
    """`{path: total_de_linhas}` para os arquivos que o snapshot ainda sustenta.

    Confere o hash contra o capturado pelo mesmo motivo de
    `snapshot.resolve_evidence`: citar "arquivo inteiro, linhas 1..N" de um
    arquivo que mudou sob a análise seria evidência errada. Path ausente do
    resultado significa faixa não resolvida — a obrigação continua existindo,
    sem faixa e sem evidência, e diz isso no motivo.
    """
    out: dict[str, int] = {}
    if snapshot is None:
        return out
    file_map = snapshot.file_map()
    for path in paths:
        entry = file_map.get(path)
        if entry is None or entry.sha256 is None:
            continue
        full = os.path.join(snapshot.repo, *path.split("/"))
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if hashlib.sha256(raw).hexdigest() != entry.sha256:
            continue
        total = len(raw.decode("utf-8", errors="replace").splitlines())
        if total >= 1:
            out[path] = total
    return out


def _inventory_code_files(inventory: Inventory) -> tuple[str, ...]:
    """Arquivos de CÓDIGO do inventário — o denominador de `plan_coverage()`.

    `FileClass.CODE` cobre toda linguagem, com adaptador ou sem
    (`inventory._LANGUAGE_BY_EXT` mapeia COBOL, JCL, PL/I, Assembler, dialetos
    Sybase, Delphi, ABAP… e o piso `Language.UNKNOWN` para texto não mapeado).
    """
    return tuple(
        sorted(f.path for f in inventory.files if f.file_class is FileClass.CODE)
    )


def _profile_source(profile: AnalysisProfile | None) -> str:
    """Rótulo de proveniência que vai para dentro do payload (motivo/justificativa)."""
    if profile is None or not profile.source:
        return "profile"
    return "profile:" + "<".join(profile.source)


def _matrix_for(profile: AnalysisProfile | None) -> FailureEdgeMatrix:
    """Matriz §6.5 desta chamada: famílias extras acrescentadas, famílias
    excluídas marcadas `not_applicable` COM o motivo do perfil.

    Sempre uma instância nova: `FAILURE_FAMILIES` não é tocado, e dois
    objetivos nunca compartilham células.
    """
    matrix = FailureEdgeMatrix()
    if profile is None:
        return matrix
    source = _profile_source(profile)
    for family, items in sorted(profile.failure_families_extra.items()):
        matrix.add_family(family, items, source=source)
    for family, motivo in sorted(profile.failure_families_excluded.items()):
        matrix.exclude_family(family, motivo, source=source)
    return matrix


def _apply_contract_exclusions(
    obj: InvestigationObjective, profile: AnalysisProfile | None
) -> None:
    """Aplica `contract_exclusions` DEPOIS do pré-preenchimento.

    Ordem importa: `_prefill_contract` escreve `identidade`, `gatilho`,
    `dependencias`, `lacunas` e `verificacao`; excluir antes seria sobrescrito.
    O motivo entra em `contract[campo]["motivo"]` com a proveniência colada —
    é o que torna a exclusão auditável no payload e o que faz
    `unmet_obligations()` parar de cobrar o campo (status vira `excluded`,
    logo sai de `pending_fields()` e não é `unresolved`).
    """
    if profile is None or not profile.contract_exclusions:
        return
    source = _profile_source(profile)
    for name, motivo in sorted(profile.contract_exclusions.items()):
        obj.field(name).exclude(f"{motivo} [{source}]")
    obj.notes.append(
        f"{len(profile.contract_exclusions)} campo(s) do §6.3 excluído(s) pelo perfil de "
        f"análise ({source}): {', '.join(sorted(profile.contract_exclusions))} — exclusão "
        "nomeada com motivo no próprio contrato, não filtro silencioso"
    )


def plan(
    capability_map: CapabilityMap,
    extraction: ExtractionResult,
    *,
    inventory: Inventory | None = None,
    snapshot: Snapshot | None = None,
    max_reading_needs: int | None = None,
    profile: AnalysisProfile | None = None,
) -> list[InvestigationObjective]:
    """Gera um objetivo por capacidade, um por grupo de órfãos e um por módulo
    de código que a extração não cobriu.

    Nunca por documento: não há parâmetro de tipo de documento, de quantidade
    nem de granularidade editorial. O denominador é duplo e ambos são
    conferidos por `assert_plan_accounted()`: o `CapabilityMap` (nenhuma
    capacidade e nenhum órfão sem objetivo) e, quando `inventory` é passado, o
    conjunto de arquivos `FileClass.CODE` (nenhum arquivo de código sem
    obrigação de leitura em objetivo nenhum).

    O segundo denominador é o que faz a análise valer para QUALQUER linguagem.
    Capacidade e órfão nascem de extratores; um repositório Go, COBOL, Delphi
    ou Sybase não tem extrator e antes produzia plano VAZIO — `succeeded` com
    zero objetivos e zero pendências, porque o modelo nunca era consultado.
    Agora o que sobra vira `ObjectiveKind.DISCOVERY`: cada arquivo entra como
    obrigação `ReadingTrigger.SOURCE_FILE` sobre a faixa 1..N inteira, e o
    contrato de 13 campos e a matriz §6.5 são exatamente os mesmos.

    `inventory=None` desliga a descoberta e restaura o comportamento
    histórico byte a byte — é o que os testes de compatibilidade fixam.

    `profile` liga as alavancas por sistema, todas com o comportamento
    histórico preservado quando ele é `None` ou `DEFAULT_PROFILE`:

    | Alavanca | Efeito verificável |
    |---|---|
    | `objectives` | filtra por `objective_id` exato ou substring de `capability_id`/`name`; o corte é do chamador e aparece em `plan_accounting(..., profile=)` |
    | `contract_exclusions` | campo do §6.3 vira `excluded` com o motivo do perfil no payload; sai de `unmet_obligations()` |
    | `failure_families_excluded` | células da família viram `not_applicable` com `justification_source` do perfil |
    | `failure_families_extra` | famílias novas entram `unresolved` (obrigação nova, não cobertura) |
    | `trigger_priority` | reordena o despacho POR CHAMADA; `_TRIGGER_PRIORITY` não é mutado |
    | `max_reading_needs` | usado quando o parâmetro homônimo é `None` |
    | `discovery_max_files_per_objective` | teto de arquivos por objetivo de descoberta; PARTICIONA em `modulo`, `modulo#2`, … (default `DISCOVERY_MAX_FILES_PER_OBJECTIVE`) |

    `max_reading_needs` é orçamento de pacote (§7.3.1) e **não** filtro de
    escopo. Por isso o padrão é `None` (sem corte): o orçamento é decisão do
    runtime que despacha, não do planejamento — planejar já cortando esconderia
    escopo antes de alguém decidir sobre ele. Com `inventory`, o que não cabe é
    DIFERIDO (`accounting["reading_needs_deferred"]`) e volta pelo objetivo de
    descoberta do módulo, então nenhum arquivo sai do plano por orçamento. Sem
    `inventory` não há para onde diferir: o excedente continua sendo
    `accounting["reading_needs_dropped"]`, entra em `notes` e impede `complete`
    (ver `unmet_obligations`) — igual a antes.
    """
    if profile is not None and not isinstance(profile, AnalysisProfile):
        raise ProfileError(
            f"plan(profile=...) exige AnalysisProfile, recebido {type(profile).__name__}"
        )
    if max_reading_needs is None and profile is not None:
        max_reading_needs = profile.max_reading_needs
    priorities = _trigger_priority(profile)
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

    # Descoberta só é possível com inventário (é dele que sai o denominador
    # de arquivos). Sem inventário, `defer_sink=None` e o orçamento volta a
    # DESCARTAR como sempre descartou — o caminho histórico, byte a byte.
    discovery_enabled = inventory is not None
    deferred_needs: list[ReadingNeed] | None = [] if discovery_enabled else None

    # Os objetivos de capacidade/órfão são construídos SEM o filtro e só
    # depois filtrados. É o que impede o filtro `objectives` de virar fábrica
    # de descoberta: um arquivo já descrito por uma capacidade suprimida
    # continua "coberto por capacidade", não vira objetivo novo.
    capability_objectives = [
        _objective_for_capability(
            cap,
            snapshot=snapshot,
            symbols_by_qual=symbols_by_qual,
            config_by_path=config_by_path,
            tests_by_target=tests_by_target,
            max_reading_needs=max_reading_needs,
            profile=profile,
            priorities=priorities,
            defer_sink=deferred_needs,
        )
        for cap in capability_map.capabilities
    ]

    by_module: dict[str, list[OrphanSymbol]] = {}
    for orphan in capability_map.orphans:
        by_module.setdefault(orphan.module or orphan.path, []).append(orphan)
    orphan_objectives = [
        _objective_for_orphans(
            module,
            by_module[module],
            max_reading_needs=max_reading_needs,
            profile=profile,
            priorities=priorities,
            defer_sink=deferred_needs,
        )
        for module in sorted(by_module)
    ]

    for obj, cap in zip(capability_objectives, capability_map.capabilities):
        if profile is not None and not profile.selects_objective(
            objective_id=obj.objective_id,
            capability_id=cap.capability_id,
            name=cap.name,
        ):
            continue
        objectives.append(obj)
    for obj in orphan_objectives:
        if profile is not None and not profile.selects_objective(
            objective_id=obj.objective_id, capability_id="", name=obj.name
        ):
            continue
        objectives.append(obj)

    if not discovery_enabled:
        return objectives

    objectives.extend(
        _discovery_objectives(
            capability_objectives + orphan_objectives,
            deferred_needs or [],
            extraction,
            inventory,
            snapshot=snapshot,
            max_reading_needs=max_reading_needs,
            profile=profile,
            priorities=priorities,
        )
    )
    return objectives


def _discovery_objectives(
    prior_objectives: Sequence[InvestigationObjective],
    deferred_needs: Sequence[ReadingNeed],
    extraction: ExtractionResult,
    inventory: Inventory,
    *,
    snapshot: Snapshot | None,
    max_reading_needs: int | None,
    profile: AnalysisProfile | None,
    priorities: Mapping[ReadingTrigger, int],
) -> list[InvestigationObjective]:
    """Um objetivo por módulo para TODO arquivo de código que sobrou.

    "Sobrou" tem definição única e verificável: arquivo `FileClass.CODE` do
    inventário que não aparece em nenhuma obrigação de leitura dos objetivos de
    capacidade/órfão. Isso engloba, sem precisar de três caminhos diferentes:
    linguagem sem adaptador, extração vazia, arquivo extraído que nenhuma
    entrada alcança, e obrigação diferida por orçamento. O motivo de cada
    arquivo é registrado individualmente na obrigação de leitura.
    """
    covered: set[str] = set()
    for obj in prior_objectives:
        for need in obj.reading_needs:
            path = _need_path(need)
            if path:
                covered.add(path)
    forced = {p for p in (_need_path(n) for n in deferred_needs) if p}
    code_files = _inventory_code_files(inventory)
    uncovered = [p for p in code_files if p not in covered or p in forced]
    if not uncovered:
        return []

    without_adapter: dict[str, str] = {}
    for lang, paths in extraction.files_without_adapter.items():
        for path in paths:
            without_adapter[path] = lang
    without_symbols = set(extraction.files_without_symbols)
    language_of = {
        f.path: (f.language.value if f.language is not None else "")
        for f in inventory.files
    }

    imports_by_path: dict[str, list[Any]] = {}
    for ref in extraction.references:
        if ref.kind == "import":
            imports_by_path.setdefault(ref.path, []).append(ref)

    limit = DISCOVERY_MAX_FILES_PER_OBJECTIVE
    if profile is not None and profile.discovery_max_files_per_objective is not None:
        limit = profile.discovery_max_files_per_objective
    if max_reading_needs is not None:
        # Teto de pacote também PARTICIONA aqui: um objetivo de descoberta
        # nunca sai com obrigação a menos do que os arquivos que declara.
        limit = max(1, min(limit, max_reading_needs))

    line_counts = _line_counts(snapshot, uncovered)
    out: list[InvestigationObjective] = []
    for module, files in _discovery_groups(uncovered, limit):
        objective_id = _objective_id_for_discovery(module)
        name = f"discovery@{module}"
        if profile is not None and not profile.selects_objective(
            objective_id=objective_id, capability_id="", name=name
        ):
            continue
        out.append(
            _objective_for_discovery(
                module,
                files,
                snapshot=snapshot,
                line_counts=line_counts,
                without_adapter=without_adapter,
                without_symbols=without_symbols,
                language_of=language_of,
                imports_by_path=imports_by_path,
                profile=profile,
                priorities=priorities,
            )
        )
    return out


def _discovery_reason(
    path: str,
    without_adapter: Mapping[str, str],
    without_symbols: set[str],
    language_of: Mapping[str, str],
) -> tuple[str, str]:
    """`(chave_de_contagem, razão)` — a razão executável que vai na obrigação."""
    lang = without_adapter.get(path) or language_of.get(path) or "desconhecida"
    if path in without_adapter:
        return (
            "files_without_extractor",
            f"linguagem {lang} sem extrator — leitura direta",
        )
    if path in without_symbols:
        return ("files_without_symbols", "arquivo sem símbolos extraídos")
    return (
        "files_not_reached",
        f"arquivo de linguagem {lang} extraído, porém não alcançado por nenhuma "
        "capacidade nem grupo de órfãos — leitura direta",
    )


def _objective_for_discovery(
    module: str,
    files: Sequence[str],
    *,
    snapshot: Snapshot | None,
    line_counts: Mapping[str, int],
    without_adapter: Mapping[str, str],
    without_symbols: set[str],
    language_of: Mapping[str, str],
    imports_by_path: Mapping[str, Sequence[Any]],
    profile: AnalysisProfile | None = None,
    priorities: Mapping[ReadingTrigger, int] = _TRIGGER_PRIORITY,
) -> InvestigationObjective:
    """Objetivo de descoberta: o arquivo INTEIRO vira obrigação de leitura."""
    prefix = f"discovery:{module}"
    needs: list[ReadingNeed] = []
    evidence_refs: list[EvidenceRef] = []
    counts = {
        "files_without_extractor": 0,
        "files_without_symbols": 0,
        "files_not_reached": 0,
    }
    for path in files:
        key, razao = _discovery_reason(path, without_adapter, without_symbols, language_of)
        counts[key] += 1
        total = int(line_counts.get(path, 0))
        if total >= 1:
            target = f"{path}:1-{total}"
            faixa = f"linhas 1..{total}"
            evidence = evidence_ref_for(
                snapshot, path, 1, total, role="source_file", symbol=path
            )
            evidence_refs.append(evidence)
        else:
            target = path
            faixa = (
                "faixa integral NÃO resolvida nesta execução (arquivo ausente do "
                "snapshot, alterado sob a análise ou vazio): leia o arquivo inteiro"
            )
            evidence = None
        needs.append(
            _need(
                prefix,
                ReadingKind.RANGE,
                target,
                (
                    f"{razao}: ler {path} por inteiro ({faixa}). Extrair deste fonte as "
                    "capacidades, os pontos de entrada, as regras de negócio e a ordem em "
                    "que se aplicam, as integrações e seus contratos, a persistência e o "
                    "comportamento em falha — cada item citando path + linhas do próprio "
                    "código. Comentário e docstring do arquivo não sustentam nenhuma "
                    "dessas afirmações"
                ),
                ReadingTrigger.SOURCE_FILE,
                evidence,
                priorities,
            )
        )

    needs, dropped, deferred = _apply_budget(needs, None, None)

    obj = InvestigationObjective(
        objective_id=_objective_id_for_discovery(module),
        matrix=_matrix_for(profile),
        kind=ObjectiveKind.DISCOVERY,
        capability_id="",
        name=f"discovery@{module}",
        symbols=(),
        evidence_refs=evidence_refs,
        reading_needs=needs,
        accounting={
            "discovery_files": len(files),
            "reading_needs": len(needs),
            "reading_needs_dropped": dropped,
            "reading_needs_deferred": deferred,
            **counts,
        },
        analysis_directives=list(ANALYSIS_DIRECTIVES) + list(DISCOVERY_DIRECTIVES),
    )
    obj.notes.append(
        f"{len(files)} arquivo(s) de código do módulo {module} sem cobertura estrutural: "
        f"{counts['files_without_extractor']} sem extrator para a linguagem, "
        f"{counts['files_without_symbols']} extraído(s) sem nenhum símbolo, "
        f"{counts['files_not_reached']} extraído(s) e não alcançado(s) por capacidade/órfão. "
        "A leitura do fonte pelo próprio modelo SUBSTITUI a extração aqui: não há símbolo, "
        "aresta nem entrada pré-computados para este módulo"
    )
    if evidence_refs:
        obj.field("identidade").fill(
            (
                f"Módulo (diretório) {module}, {len(files)} arquivo(s): "
                + ", ".join(files[:8])
                + (" …" if len(files) > 8 else "")
                + ". Agrupamento: diretório do inventário (não há símbolo de módulo "
                "extraído para estes arquivos). Nome de NEGÓCIO e nome técnico do "
                "componente exigem a leitura do fonte."
            ),
            evidence_refs,
        )
    else:
        obj.field("identidade").unresolve(
            f"nenhum dos {len(files)} arquivo(s) de {module} pôde ser citado com "
            "localizador nesta execução: sem citação, nem a identidade técnica do módulo "
            "é afirmável"
        )

    import_refs = [r for path in files for r in imports_by_path.get(path, ())]
    import_evidence = [
        evidence_ref_for(
            snapshot, r.path, r.line, r.line_end or r.line, role="import", symbol=r.to_name
        )
        for r in import_refs[:20]
    ]
    if import_refs and import_evidence:
        modules = sorted({r.to_name for r in import_refs})
        obj.field("dependencias").fill(
            f"{len(import_refs)} import(s) extraído(s) nestes arquivos: "
            + ", ".join(modules[:10])
            + (" …" if len(modules) > 10 else "")
            + ". O que cada dependência entrega (operação, contrato, timeout, retry, "
            "fallback, idempotência) exige leitura do ponto de uso.",
            import_evidence,
        )
    else:
        langs = sorted({without_adapter.get(f) or language_of.get(f, "") for f in files} - {""})
        obj.field("dependencias").unresolve(
            "nenhum import pôde ser extraído destes arquivos "
            f"(linguagem(ns): {', '.join(langs) or 'desconhecida'}): as dependências deste "
            "módulo são desconhecidas até que o fonte seja lido, e qualquer afirmação sobre "
            "o que ele consome carece de evidência"
        )
    _apply_contract_exclusions(obj, profile)
    return obj


def _objective_for_capability(
    cap: CapabilityCandidate,
    *,
    snapshot: Snapshot | None,
    symbols_by_qual: Mapping[str, Any],
    config_by_path: Mapping[str, Sequence[Any]],
    tests_by_target: Mapping[str, Sequence[Any]],
    max_reading_needs: int | None,
    profile: AnalysisProfile | None = None,
    priorities: Mapping[ReadingTrigger, int] = _TRIGGER_PRIORITY,
    defer_sink: list[ReadingNeed] | None = None,
) -> InvestigationObjective:
    prefix = cap.capability_id
    needs, excluded_gaps = _needs_from_gaps(prefix, cap.gaps, priorities)
    needs.extend(_needs_from_externals(prefix, cap.external_dependencies, priorities))

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
                    priorities,
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
                priorities,
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
                    priorities,
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
                    priorities,
                )
            )

    # Orçamento de pacote. Com `defer_sink` (há inventário), o que não coube é
    # DIFERIDO para objetivo de descoberta e não impede `complete`; sem ele, o
    # corte continua sendo descarte contado e bloqueante.
    needs, dropped, deferred = _apply_budget(needs, max_reading_needs, defer_sink)

    objective_id = _objective_id_for_capability(cap.capability_id)
    obj = InvestigationObjective(
        objective_id=objective_id,
        matrix=_matrix_for(profile),
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
            "reading_needs_deferred": deferred,
        },
        notes=list(cap.notes),
    )
    if dropped:
        obj.notes.append(
            f"{dropped} obrigação(ões) de leitura acima do orçamento de {max_reading_needs} "
            "não entraram no pacote; enquanto isso não for revisto o objetivo não pode ser "
            "declarado complete"
        )
    if deferred:
        obj.notes.append(
            f"{deferred} obrigação(ões) de leitura acima do orçamento de "
            f"{max_reading_needs} foram DIFERIDAS: os arquivos delas voltam ao plano em "
            "objetivo(s) de descoberta, então nenhum arquivo saiu do escopo — teto de "
            "pacote particiona, não descarta"
        )
    if excluded_gaps:
        obj.notes.append(
            f"{len(excluded_gaps)} lacuna(s) excluída(s) como runtime da linguagem "
            f"(ex.: {excluded_gaps[0][0].to_name!r}) — exclusão nomeada, não filtro silencioso"
        )
    _prefill_contract(obj, cap, test_evidence)
    _apply_contract_exclusions(obj, profile)
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
    module: str,
    orphans: Sequence[OrphanSymbol],
    *,
    max_reading_needs: int | None,
    profile: AnalysisProfile | None = None,
    priorities: Mapping[ReadingTrigger, int] = _TRIGGER_PRIORITY,
    defer_sink: list[ReadingNeed] | None = None,
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
            priorities,
        )
        for o in orphans
    ]
    needs, dropped, deferred = _apply_budget(needs, max_reading_needs, defer_sink)

    obj = InvestigationObjective(
        objective_id=_objective_id_for_orphans(module),
        matrix=_matrix_for(profile),
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
            "reading_needs_deferred": deferred,
        },
    )
    if deferred:
        obj.notes.append(
            f"{deferred} obrigação(ões) de leitura acima do orçamento de "
            f"{max_reading_needs} foram DIFERIDAS para objetivo(s) de descoberta: teto de "
            "pacote particiona, não descarta"
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
    _apply_contract_exclusions(obj, profile)
    return obj


# --------------------------------------------------------------------------
# Denominadores do plano (§6.6)
# --------------------------------------------------------------------------


def _selected_capabilities(
    capability_map: CapabilityMap, profile: AnalysisProfile | None
) -> list[Any]:
    if profile is None or not profile.objectives:
        return list(capability_map.capabilities)
    return [
        c
        for c in capability_map.capabilities
        if profile.selects_objective(
            objective_id=_objective_id_for_capability(c.capability_id),
            capability_id=c.capability_id,
            name=c.name,
        )
    ]


def _selected_orphans(
    capability_map: CapabilityMap, profile: AnalysisProfile | None
) -> list[Any]:
    if profile is None or not profile.objectives:
        return list(capability_map.orphans)
    by_module: dict[str, list[OrphanSymbol]] = {}
    for orphan in capability_map.orphans:
        by_module.setdefault(orphan.module or orphan.path, []).append(orphan)
    out: list[Any] = []
    for module, group in sorted(by_module.items()):
        if profile.selects_objective(
            objective_id=_objective_id_for_orphans(module),
            capability_id="",
            name=f"orfaos@{module}",
        ):
            out.extend(group)
    return out


def plan_coverage(
    objectives: Sequence[InvestigationObjective],
    inventory: Inventory,
) -> dict[str, Any]:
    """Invariante de cobertura POR ARQUIVO: `{files_total, files_covered,
    files_uncovered}`.

    Um arquivo está coberto quando alguma obrigação de leitura de algum
    objetivo aponta para ele. É a única definição usada em todo o módulo, e é
    ela que `plan()` fecha por construção: o que sobra depois dos objetivos de
    capacidade/órfão vira objetivo de DESCOBERTA. Objetivo de capacidade
    acelera e permite verificação mecânica, mas nunca limita o que é lido —
    quando ele não alcança um arquivo, o arquivo não sai do plano, muda de
    objetivo.
    """
    code_files = _inventory_code_files(inventory)
    covered: set[str] = set()
    for obj in objectives:
        for need in obj.reading_needs:
            path = _need_path(need)
            if path:
                covered.add(path)
    uncovered = [p for p in code_files if p not in covered]
    return {
        "files_total": len(code_files),
        "files_covered": len(code_files) - len(uncovered),
        "files_uncovered": uncovered,
    }


def plan_accounting(
    objectives: Sequence[InvestigationObjective],
    capability_map: CapabilityMap,
    *,
    profile: AnalysisProfile | None = None,
    extraction: ExtractionResult | None = None,
    inventory: Inventory | None = None,
) -> dict[str, Any]:
    """Denominadores do plano. Com `profile`, o que o filtro `objectives`
    suprimiu é CONTADO (`capabilities_suppressed_by_profile`,
    `orphan_symbols_suppressed_by_profile`) — filtrar escopo nunca pode
    parecer cobertura."""
    selected_caps = _selected_capabilities(capability_map, profile)
    selected_orphans = _selected_orphans(capability_map, profile)
    out: dict[str, Any] = {
        "capabilities": len(capability_map.capabilities),
        "capabilities_selected": len(selected_caps),
        "capabilities_suppressed_by_profile": (
            len(capability_map.capabilities) - len(selected_caps)
        ),
        "orphan_symbols_selected": len(selected_orphans),
        "orphan_symbols_suppressed_by_profile": (
            len(capability_map.orphans) - len(selected_orphans)
        ),
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
        "discovery_objectives": sum(
            1 for o in objectives if o.kind is ObjectiveKind.DISCOVERY
        ),
        "discovery_files": sum(
            int(o.accounting.get("discovery_files", 0))
            for o in objectives
            if o.kind is ObjectiveKind.DISCOVERY
        ),
        "reading_needs_deferred": sum(
            int(o.accounting.get("reading_needs_deferred", 0)) for o in objectives
        ),
        # Sem `extraction` não há como afirmar POR LINGUAGEM quantos arquivos
        # ficaram sem adaptador; devolver 0/{} seria dizer "nenhum". Devolver
        # o mapa vazio com a contagem ausente é o comportamento honesto: quem
        # quer o número passa `extraction=`.
        "files_without_extractor": (
            {
                lang: len(paths)
                for lang, paths in sorted(extraction.files_without_adapter.items())
            }
            if extraction is not None
            else {}
        ),
        "files_without_symbols": (
            len(extraction.files_without_symbols) if extraction is not None else 0
        ),
    }
    if inventory is not None:
        coverage = plan_coverage(objectives, inventory)
        out["files_total"] = coverage["files_total"]
        out["files_covered"] = coverage["files_covered"]
        out["files_uncovered"] = len(coverage["files_uncovered"])
        out["files_uncovered_suppressed_by_profile"] = (
            len(coverage["files_uncovered"])
            if (profile is not None and profile.objectives)
            else 0
        )
    return out


def assert_plan_accounted(
    objectives: Sequence[InvestigationObjective],
    capability_map: CapabilityMap,
    *,
    profile: AnalysisProfile | None = None,
    inventory: Inventory | None = None,
) -> None:
    """Toda capacidade e todo órfão SELECIONADOS têm exatamente um objetivo (§6.6).

    Sem `profile` (ou com perfil sem filtro `objectives`), o denominador é o
    `CapabilityMap` inteiro — comportamento histórico. Com filtro, o
    denominador passa a ser o subconjunto que o próprio filtro escolheu: o que
    ficou de fora não vira "coberto", vira contagem em `plan_accounting()`.
    """
    cap_ids = [c.capability_id for c in _selected_capabilities(capability_map, profile)]
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
    orphan_names = {o.qualname for o in _selected_orphans(capability_map, profile)}
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
    discovery_ids = [
        o.objective_id for o in objectives if o.kind is ObjectiveKind.DISCOVERY
    ]
    if len(discovery_ids) != len(set(discovery_ids)):
        raise PlanAccountingError(
            "objetivo de descoberta duplicado: o mesmo módulo despachado duas vezes"
        )
    if inventory is None:
        return
    uncovered = plan_coverage(objectives, inventory)["files_uncovered"]
    if not uncovered:
        return
    if profile is not None and profile.objectives:
        # Com filtro por objetivo, o denominador é o subconjunto escolhido pelo
        # chamador; o que ficou fora é CONTADO em `plan_accounting`
        # (`files_uncovered_suppressed_by_profile`), nunca apresentado como
        # coberto — mesma regra já aplicada a capacidades e órfãos.
        return
    raise PlanAccountingError(
        f"{len(uncovered)} arquivo(s) de código sem NENHUMA obrigação de leitura: "
        f"{uncovered[:3]} — todo arquivo de código do inventário termina em um objetivo "
        "(capacidade, órfão ou descoberta); arquivo que ninguém vai ler é escopo perdido "
        "em silêncio (§6.1.3/§6.6)"
    )


def objectives_to_dict(objectives: Iterable[InvestigationObjective]) -> list[dict[str, Any]]:
    """Pacote serializável para o runtime (W4)."""
    return [o.to_dict() for o in objectives]


def objectives_from_dict(
    payload: Iterable[Mapping[str, Any]],
    *,
    allowed_families: Mapping[str, Sequence[str]] | None = None,
    trust_profile_cells: bool = False,
) -> list[InvestigationObjective]:
    """Reconstrói objetivos. `trust_profile_cells=True` APENAS para payload
    escrito pelo pipeline (escopo, objective_payload, estado acumulado)."""
    return [
        InvestigationObjective.from_dict(
            item,
            allowed_families=allowed_families,
            trust_profile_cells=trust_profile_cells,
        )
        for item in payload
    ]
