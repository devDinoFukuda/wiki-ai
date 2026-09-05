"""Entradas do sistema e agrupamento por comportamento (§6.1 itens 6-8).

Este módulo responde a uma pergunta só: **quais comportamentos existem neste
código e o que cada um alcança?** Ele não lê arquivo, não chama LLM e não
escreve conhecimento; consome o resultado da extração estrutural
(`analysis.extractors`) e o inventário (`analysis.inventory`) e devolve um
`CapabilityMap`.

Quatro regras do plano estão implementadas **aqui, em código executável**:

1. **Agrupamento por comportamento, não por diretório** (§6.1.7). O sinal de
   agrupamento é o fecho transitivo de chamadas resolvidas: duas entradas caem
   na mesma capacidade quando alcançam símbolo em comum. Diretório/módulo é
   usado apenas como *desempate* — para nomear a capacidade e para juntar
   entradas cujo fecho é vazio (`GroupingBasis.MODULE_PREFIX_TIEBREAK`), e o
   critério efetivo fica registrado em `CapabilityCandidate.grouping_basis`.
   Nenhum caminho de código agrupa por diretório quando há fecho disponível.

2. **Heurística não vira aresta.** O grafo só usa `Reference.resolved is True`
   (`_build_graph`). Como `extractors.base.Reference` recusa `resolved=True`
   sob `resolution="heuristic"`, é estruturalmente impossível uma varredura
   léxica inventar alcance. Consequência honesta: linguagem sem parser (JS/TS,
   Java) produz capacidades singleton, e isso aparece na contabilidade em vez
   de virar um agrupamento fictício.

3. **A fronteira é registrada, não apagada** (§6.1.8, §6.4). Toda referência
   NÃO resolvida partindo de um símbolo alcançado vira `BoundaryGap` — uma
   lacuna rastreável com localizador, motivo do extrator e impacto. É a
   matéria-prima das `ReadingNeed` de `analysis.investigation`.

4. **Denominadores fecham** (§6.6). Toda entrada descoberta termina em exatamente
   uma capacidade (inclusive a que não pôde ser ancorada: vira singleton com
   `anchor_basis="unanchored"`), e todo símbolo público termina alcançado ou em
   `orphans`. `CapabilityMap.assert_accounted()` recalcula as duas contas e
   levanta erro se algo sumir.

Sobre "hubs": num repositório real, utilitários compartilhados (um `_digest`,
um `snippet_hash`) são alcançados por quase toda entrada. Se eles contassem
como sinal de agrupamento, *tudo* viraria uma capacidade só — o oposto de
descrever comportamento. `_hub_symbols` marca como compartilhado o símbolo
alcançado por mais que `hub_fraction` das entradas; ele continua em
`reachable_symbols` de cada capacidade (a cadeia é real), mas não une grupos.
O conjunto fica em `CapabilityMap.shared_symbols`, então o critério é auditável
e não silencioso.

Imports: stdlib + `knowledge` + `analysis`. Nenhuma dependência nativa.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from knowledge.identity import entity_id
from knowledge.models import EntityType

from .extractors.base import Entrypoint, Reference, Symbol
from .extractors.registry import ExtractionResult
from .inventory import FileClass, Inventory
from .snapshot import EvidenceRangeInvalid, PathNotInSnapshot, Snapshot, SnapshotStale, resolve_evidence

__all__ = [
    "CONTAINER_SYMBOL_KINDS",
    "EDGE_REFERENCE_KINDS",
    "GAP_REFERENCE_KINDS",
    "ORPHAN_CANDIDATE_KINDS",
    "BoundaryGap",
    "CapabilityAccountingError",
    "CapabilityCandidate",
    "CapabilityMap",
    "EntryRef",
    "EvidenceRef",
    "ExternalDependency",
    "GroupingBasis",
    "OrphanSymbol",
    "discover",
    "evidence_ref_for",
    "fingerprint",
]


# --------------------------------------------------------------------------
# Vocabulário
# --------------------------------------------------------------------------

#: Espécies de referência que constituem **alcance de comportamento**. Import e
#: attribute ficam de fora de propósito: importar um módulo não executa o seu
#: código de negócio, e transformar import em alcance faria toda capacidade
#: alcançar todo o repositório.
EDGE_REFERENCE_KINDS: tuple[str, ...] = ("call", "inherit", "implement")

#: Espécies cuja NÃO resolução, partindo de símbolo alcançado, é lacuna de
#: comportamento (§6.4). Import não resolvido não é lacuna de fluxo: é
#: dependência externa, e sai por `ExternalDependency`.
GAP_REFERENCE_KINDS: tuple[str, ...] = ("call", "inherit", "implement")

#: Símbolos que contêm outros símbolos executáveis. A aresta de contenção
#: existe porque uma função aninhada só é alcançável através da externa, e um
#: método só através da classe. `module` NÃO está aqui: se estivesse, alcançar
#: qualquer símbolo de um arquivo alcançaria o arquivo inteiro, e o
#: agrupamento voltaria a ser por arquivo — exatamente o que o §6.1.7 proíbe.
CONTAINER_SYMBOL_KINDS: tuple[str, ...] = ("class", "function", "method", "interface", "record")

#: Espécies de símbolo que, sendo públicas, precisam terminar alcançadas ou
#: declaradas órfãs. `module`, `variable`, `const`, `field` e `property` não
#: entram: não são comportamento por si.
ORPHAN_CANDIDATE_KINDS: tuple[str, ...] = (
    "function",
    "method",
    "class",
    "interface",
    "record",
    "enum",
)


class GroupingBasis(str, enum.Enum):
    """Como esta capacidade foi formada — o critério fica no dado, não na prosa."""

    #: Entradas unidas porque o fecho transitivo de chamadas resolvidas se
    #: intersecta em símbolo não compartilhado. É o critério do §6.1.7.
    BEHAVIOR_CLOSURE = "behavior_closure"
    #: Entrada isolada: nenhum símbolo em comum com qualquer outra entrada.
    SINGLETON = "singleton"
    #: Desempate: entradas sem fecho útil (ex.: linguagem sem parser) unidas
    #: pelo prefixo de módulo. Diretório como pista, jamais como fronteira
    #: quando havia comportamento a seguir.
    MODULE_PREFIX_TIEBREAK = "module_prefix_tiebreak"


class CapabilityAccountingError(ValueError):
    """Denominador do §6.6 não fecha: entrada ou símbolo sem destino declarado."""


# --------------------------------------------------------------------------
# Evidência
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """Ponteiro para um trecho de código, com localizador quando resolvível.

    `locator` é `None` quando não houve snapshot para citar, ou quando o
    arquivo mudou/saiu do escopo desde a captura. Nesse caso `note` diz o
    motivo — uma citação impossível é declarada, nunca fabricada.
    """

    path: str
    line_start: int
    line_end: int
    role: str = ""
    symbol: str | None = None
    locator: Mapping[str, Any] | None = None
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.locator is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "role": self.role,
            "symbol": self.symbol,
            "locator": dict(self.locator) if self.locator else None,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRef":
        return cls(
            path=str(data["path"]),
            line_start=int(data["line_start"]),
            line_end=int(data["line_end"]),
            role=str(data.get("role", "")),
            symbol=data.get("symbol"),
            locator=dict(data["locator"]) if data.get("locator") else None,
            note=str(data.get("note", "")),
        )


def evidence_ref_for(
    snapshot: Snapshot | None,
    path: str,
    line_start: int,
    line_end: int,
    *,
    role: str = "",
    symbol: str | None = None,
) -> EvidenceRef:
    """`EvidenceRef` com localizador validado quando o snapshot permite citar.

    Delega a `snapshot.resolve_evidence`, que confere o hash do arquivo em
    disco contra o capturado — por isso um arquivo alterado sob a análise
    produz `locator=None` com motivo, e não uma citação errada.
    """
    if snapshot is None:
        return EvidenceRef(
            path=path,
            line_start=line_start,
            line_end=line_end,
            role=role,
            symbol=symbol,
            note="sem snapshot: localizador não pôde ser resolvido nesta execução",
        )
    try:
        resolved = resolve_evidence(snapshot, path, line_start, line_end)
    except (PathNotInSnapshot, SnapshotStale, EvidenceRangeInvalid) as exc:
        return EvidenceRef(
            path=path,
            line_start=line_start,
            line_end=line_end,
            role=role,
            symbol=symbol,
            note=f"{type(exc).__name__}: {exc}",
        )
    return EvidenceRef(
        path=path,
        line_start=line_start,
        line_end=line_end,
        role=role,
        symbol=symbol,
        locator=resolved["locator"],
    )


# --------------------------------------------------------------------------
# Peças da capacidade
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryRef:
    """Entrada descoberta, já ancorada (ou explicitamente não ancorada).

    Forma serializável de `extractors.base.Entrypoint` mais o resultado da
    ancoragem. `anchor` é o `qualname` do símbolo pelo qual o comportamento
    começa; `anchor_basis` diz **como** se chegou nele, para que uma âncora
    fraca (por linha) não se confunda com uma exata.
    """

    kind: str
    name: str
    path: str
    line: int
    line_end: int
    framework: str | None = None
    symbol: str | None = None
    detail: str = ""
    language: str = ""
    anchor: str | None = None
    anchor_basis: str = "unanchored"
    evidence: EvidenceRef | None = None

    @property
    def key(self) -> str:
        """Chave estável da entrada, independente da ordem de descoberta."""
        return f"{self.kind}|{self.path}|{self.line}|{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "line_end": self.line_end,
            "framework": self.framework,
            "symbol": self.symbol,
            "detail": self.detail,
            "language": self.language,
            "anchor": self.anchor,
            "anchor_basis": self.anchor_basis,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EntryRef":
        ev = data.get("evidence")
        return cls(
            kind=str(data["kind"]),
            name=str(data["name"]),
            path=str(data["path"]),
            line=int(data["line"]),
            line_end=int(data["line_end"]),
            framework=data.get("framework"),
            symbol=data.get("symbol"),
            detail=str(data.get("detail", "")),
            language=str(data.get("language", "")),
            anchor=data.get("anchor"),
            anchor_basis=str(data.get("anchor_basis", "unanchored")),
            evidence=EvidenceRef.from_dict(ev) if ev else None,
        )


@dataclass(frozen=True)
class BoundaryGap:
    """Referência não resolvida partindo de símbolo alcançado — lacuna rastreável.

    O `reason` vem do extrator (ex.: "receptor dinâmico: despacho não resolvido
    estaticamente"), não de suposição deste módulo. `impact` é fixo e honesto:
    o que está do outro lado não foi analisado.
    """

    from_symbol: str
    to_name: str
    kind: str
    path: str
    line: int
    line_end: int
    reason: str = ""
    language: str = ""
    evidence: EvidenceRef | None = None
    occurrences: int = 1
    extra_sites: tuple[tuple[str, int], ...] = ()
    impact: str = (
        "o alvo não foi lido: comportamento, efeitos e falhas do outro lado da "
        "chamada permanecem desconhecidos para esta capacidade"
    )

    @property
    def gap_key(self) -> str:
        """Granularidade da lacuna: o VÍNCULO chamador->alvo, não o caractere.

        O mesmo alvo chamado três vezes na mesma função é UMA lacuna com
        `occurrences=3` e os demais sítios em `extra_sites` — não três lacunas.
        Nada é descartado: contagem e posições continuam no dado.
        """
        return f"{self.kind}|{self.from_symbol}|{self.to_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_symbol": self.from_symbol,
            "to_name": self.to_name,
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "line_end": self.line_end,
            "reason": self.reason,
            "language": self.language,
            "impact": self.impact,
            "occurrences": self.occurrences,
            "extra_sites": [list(s) for s in self.extra_sites],
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BoundaryGap":
        ev = data.get("evidence")
        return cls(
            from_symbol=str(data["from_symbol"]),
            to_name=str(data["to_name"]),
            kind=str(data["kind"]),
            path=str(data["path"]),
            line=int(data["line"]),
            line_end=int(data["line_end"]),
            reason=str(data.get("reason", "")),
            language=str(data.get("language", "")),
            evidence=EvidenceRef.from_dict(ev) if ev else None,
            occurrences=int(data.get("occurrences", 1)),
            extra_sites=tuple((str(p), int(n)) for p, n in data.get("extra_sites", ())),
            impact=str(data.get("impact", "")),
        )


@dataclass(frozen=True)
class ExternalDependency:
    """Import não resolvido num módulo tocado: contrato consumido de fora do escopo.

    Separado de `BoundaryGap` porque a obrigação é outra (§6.4): aqui não se
    lê "a implementação", que está fora do escopo por definição — lê-se o
    **contrato consumido** no ponto de uso.
    """

    module: str
    imported_by: str
    path: str
    line: int
    reason: str = ""
    evidence: EvidenceRef | None = None
    occurrences: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "imported_by": self.imported_by,
            "path": self.path,
            "line": self.line,
            "reason": self.reason,
            "occurrences": self.occurrences,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExternalDependency":
        ev = data.get("evidence")
        return cls(
            module=str(data["module"]),
            imported_by=str(data["imported_by"]),
            path=str(data["path"]),
            line=int(data["line"]),
            reason=str(data.get("reason", "")),
            evidence=EvidenceRef.from_dict(ev) if ev else None,
            occurrences=int(data.get("occurrences", 1)),
        )


@dataclass(frozen=True)
class OrphanSymbol:
    """Símbolo público que nenhuma entrada alcança.

    Não é lixo nem ruído: é comportamento exposto cuja porta de entrada não foi
    encontrada. Some da análise apenas se alguém decidir excluí-lo com motivo
    (§6.6), nunca por omissão.
    """

    qualname: str
    kind: str
    path: str
    line_start: int
    line_end: int
    language: str = ""
    module: str = ""
    reason: str = "nenhum entrypoint alcança este símbolo pelo fecho de chamadas resolvidas"
    evidence: EvidenceRef | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualname": self.qualname,
            "kind": self.kind,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "language": self.language,
            "module": self.module,
            "reason": self.reason,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OrphanSymbol":
        ev = data.get("evidence")
        return cls(
            qualname=str(data["qualname"]),
            kind=str(data["kind"]),
            path=str(data["path"]),
            line_start=int(data["line_start"]),
            line_end=int(data["line_end"]),
            language=str(data.get("language", "")),
            module=str(data.get("module", "")),
            reason=str(data.get("reason", "")),
            evidence=EvidenceRef.from_dict(ev) if ev else None,
        )


@dataclass(frozen=True)
class CapabilityCandidate:
    """Uma capacidade candidata: entradas + tudo que elas alcançam.

    "Candidata" porque o nome de negócio não sai de código — sai da
    investigação (§6.3, campo Identidade). O que este módulo entrega é o
    recorte comportamental e sua fronteira.
    """

    capability_id: str
    name: str
    entrypoints: tuple[EntryRef, ...]
    reachable_symbols: tuple[str, ...]
    modules: tuple[str, ...]
    paths: tuple[str, ...]
    gaps: tuple[BoundaryGap, ...]
    external_dependencies: tuple[ExternalDependency, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    grouping_basis: GroupingBasis
    shared_symbols_used: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def entry_keys(self) -> tuple[str, ...]:
        return tuple(e.key for e in self.entrypoints)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "entrypoints": [e.to_dict() for e in self.entrypoints],
            "reachable_symbols": list(self.reachable_symbols),
            "modules": list(self.modules),
            "paths": list(self.paths),
            "gaps": [g.to_dict() for g in self.gaps],
            "external_dependencies": [d.to_dict() for d in self.external_dependencies],
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "grouping_basis": self.grouping_basis.value,
            "shared_symbols_used": list(self.shared_symbols_used),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityCandidate":
        return cls(
            capability_id=str(data["capability_id"]),
            name=str(data["name"]),
            entrypoints=tuple(EntryRef.from_dict(e) for e in data.get("entrypoints", ())),
            reachable_symbols=tuple(data.get("reachable_symbols", ())),
            modules=tuple(data.get("modules", ())),
            paths=tuple(data.get("paths", ())),
            gaps=tuple(BoundaryGap.from_dict(g) for g in data.get("gaps", ())),
            external_dependencies=tuple(
                ExternalDependency.from_dict(d) for d in data.get("external_dependencies", ())
            ),
            evidence_refs=tuple(EvidenceRef.from_dict(e) for e in data.get("evidence_refs", ())),
            grouping_basis=GroupingBasis(data.get("grouping_basis", "singleton")),
            shared_symbols_used=tuple(data.get("shared_symbols_used", ())),
            notes=tuple(data.get("notes", ())),
        )


@dataclass(frozen=True)
class CapabilityMap:
    """Resultado de `discover`: capacidades, órfãos e os denominadores do §6.6."""

    namespace: str
    snapshot_id: str
    capabilities: tuple[CapabilityCandidate, ...]
    orphans: tuple[OrphanSymbol, ...]
    shared_symbols: tuple[str, ...]
    totals: Mapping[str, int]
    notes: tuple[str, ...] = ()

    def by_id(self, capability_id: str) -> CapabilityCandidate | None:
        for cap in self.capabilities:
            if cap.capability_id == capability_id:
                return cap
        return None

    def assert_accounted(self) -> None:
        """§6.6 — recalcula os denominadores e recusa qualquer sumiço silencioso."""
        grouped_keys: list[str] = []
        for cap in self.capabilities:
            grouped_keys.extend(cap.entry_keys)
        if len(grouped_keys) != len(set(grouped_keys)):
            duplicated = sorted({k for k in grouped_keys if grouped_keys.count(k) > 1})
            raise CapabilityAccountingError(
                f"entrada(s) em mais de uma capacidade: {duplicated[:5]} "
                "— uma entrada pertence a exatamente uma capacidade"
            )
        total = int(self.totals.get("entrypoints_total", -1))
        if len(grouped_keys) != total:
            raise CapabilityAccountingError(
                f"entradas agrupadas ({len(grouped_keys)}) != entradas descobertas ({total}): "
                "há entrada sem capacidade declarada (§6.6)"
            )
        pub_total = int(self.totals.get("public_symbols_total", -1))
        reached = int(self.totals.get("public_symbols_reached", -1))
        orphan_total = int(self.totals.get("orphan_symbols", -1))
        if orphan_total != len(self.orphans):
            raise CapabilityAccountingError(
                f"totals.orphan_symbols={orphan_total} != len(orphans)={len(self.orphans)}"
            )
        if reached + orphan_total != pub_total:
            raise CapabilityAccountingError(
                f"símbolos públicos não fecham: alcançados {reached} + órfãos {orphan_total} "
                f"!= descobertos {pub_total} (§6.6)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "snapshot_id": self.snapshot_id,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "orphans": [o.to_dict() for o in self.orphans],
            "shared_symbols": list(self.shared_symbols),
            "totals": dict(self.totals),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityMap":
        return cls(
            namespace=str(data["namespace"]),
            snapshot_id=str(data.get("snapshot_id", "")),
            capabilities=tuple(
                CapabilityCandidate.from_dict(c) for c in data.get("capabilities", ())
            ),
            orphans=tuple(OrphanSymbol.from_dict(o) for o in data.get("orphans", ())),
            shared_symbols=tuple(data.get("shared_symbols", ())),
            totals=dict(data.get("totals", {})),
            notes=tuple(data.get("notes", ())),
        )


# --------------------------------------------------------------------------
# Índices
# --------------------------------------------------------------------------


class _SymbolIndex:
    """Índice de símbolos por `qualname` e por arquivo (para ancoragem por linha)."""

    def __init__(self, symbols: Sequence[Symbol]) -> None:
        self.by_qualname: dict[str, Symbol] = {}
        self.by_path: dict[str, list[Symbol]] = {}
        self.module_of_path: dict[str, str] = {}
        for sym in symbols:
            qual = sym.qualname or sym.name
            # Primeiro vence: colisão de qualname (ex.: dois módulos homônimos)
            # não pode fazer um símbolo apagar o outro do índice.
            self.by_qualname.setdefault(qual, sym)
            self.by_path.setdefault(sym.path, []).append(sym)
            if sym.kind == "module":
                self.module_of_path.setdefault(sym.path, qual)

    def enclosing(self, path: str, line: int) -> Symbol | None:
        """Menor símbolo executável (não-módulo) cujo intervalo cobre a linha."""
        best: Symbol | None = None
        for sym in self.by_path.get(path, ()):
            if sym.kind == "module":
                continue
            if sym.line_start <= line <= sym.line_end:
                if best is None or (sym.line_end - sym.line_start) < (best.line_end - best.line_start):
                    best = sym
        return best

    def module_symbol(self, path: str) -> Symbol | None:
        qual = self.module_of_path.get(path)
        return self.by_qualname.get(qual) if qual else None


def _build_graph(references: Sequence[Reference], index: _SymbolIndex) -> dict[str, set[str]]:
    """Grafo de alcance: só arestas resolvidas + contenção de símbolo aninhado.

    Contenção não inclui `module -> membro`: ver `CONTAINER_SYMBOL_KINDS`.
    """
    graph: dict[str, set[str]] = {}
    for ref in references:
        if not ref.resolved or ref.kind not in EDGE_REFERENCE_KINDS or not ref.target:
            continue
        graph.setdefault(ref.from_symbol, set()).add(ref.target)
    for qual, sym in index.by_qualname.items():
        parent = sym.parent
        if not parent:
            continue
        parent_sym = index.by_qualname.get(parent)
        if parent_sym is None or parent_sym.kind not in CONTAINER_SYMBOL_KINDS:
            continue
        graph.setdefault(parent, set()).add(qual)
    return graph


def _reach(graph: Mapping[str, set[str]], roots: Iterable[str]) -> set[str]:
    """Fecho transitivo com controle de visita — recursão e ciclo terminam (§6.4)."""
    seen: set[str] = set()
    stack = [r for r in roots if r]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return seen


def _anchor_entry(ep: Entrypoint, index: _SymbolIndex) -> tuple[str | None, str]:
    """`(qualname_âncora, base_da_ancoragem)` para uma entrada.

    Ordem: símbolo declarado pelo extrator > handler declarado (argparse) >
    símbolo que envolve a linha > símbolo de módulo do arquivo > não ancorada.
    A base fica registrada porque âncora por linha é mais fraca que por nome, e
    quem consome precisa saber disso.
    """
    if ep.symbol and ep.symbol in index.by_qualname:
        return ep.symbol, "symbol_exact"
    handler = str((ep.extra or {}).get("handler") or "")
    if handler:
        module = index.module_of_path.get(ep.path, "")
        for cand in (f"{module}.{handler}" if module else "", handler):
            if cand and cand in index.by_qualname:
                return cand, "declared_handler"
    enclosing = index.enclosing(ep.path, ep.line)
    if enclosing is not None:
        return enclosing.qualname or enclosing.name, "enclosing_span"
    mod = index.module_symbol(ep.path)
    if mod is not None:
        return mod.qualname or mod.name, "module_of_file"
    return None, "unanchored"


class _Union:
    """Union-find sobre índices de entradas."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _common_dotted_prefix(names: Sequence[str]) -> str:
    """Maior prefixo pontuado comum — usado só para NOMEAR (desempate)."""
    parts = [n.split(".") for n in names if n]
    if not parts:
        return ""
    common: list[str] = []
    for chunk in zip(*parts):
        if len(set(chunk)) != 1:
            break
        common.append(chunk[0])
    return ".".join(common)


def _common_path_prefix(paths: Sequence[str]) -> str:
    """Maior diretório comum — rótulo de último recurso quando não há módulo."""
    parts = [p.split("/")[:-1] for p in paths if p]
    if not parts:
        return ""
    common: list[str] = []
    for chunk in zip(*parts):
        if len(set(chunk)) != 1:
            break
        common.append(chunk[0])
    return "/".join(common)


def _hub_symbols(
    per_entry: Sequence[set[str]], hub_fraction: float, min_entries: int
) -> set[str]:
    """Símbolos alcançados por muitas entradas: utilitário compartilhado, não capacidade."""
    if len(per_entry) < min_entries:
        return set()
    threshold = max(2, int(len(per_entry) * hub_fraction))
    counter: dict[str, int] = {}
    for closure in per_entry:
        for sym in closure:
            counter[sym] = counter.get(sym, 0) + 1
    return {sym for sym, n in counter.items() if n > threshold}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def discover(
    extraction: ExtractionResult,
    inventory: Inventory | None = None,
    *,
    namespace: str = "local/analysis",
    snapshot: Snapshot | None = None,
    hub_fraction: float = 0.25,
    hub_min_entries: int = 4,
    max_evidence_per_capability: int = 24,
    max_sites_per_gap: int = 8,
) -> CapabilityMap:
    """Agrupa as entradas descobertas em capacidades candidatas (§6.1 itens 6-8).

    Parâmetros
    ----------
    extraction: resultado de `extractors.default_registry().extract_all(...)`.
    inventory:  inventário do escopo; usado para restringir o denominador de
                símbolos públicos aos arquivos com `evidence_grade` e para
                excluir arquivos de teste do cálculo de órfãos (um helper de
                teste não é capacidade do produto). `None` desliga o filtro e
                a decisão fica registrada em `CapabilityMap.notes`.
    snapshot:   quando presente, cada ponto-chave sai com localizador validado.
    hub_fraction / hub_min_entries: ver a nota sobre hubs no topo do módulo.

    O retorno já passou por `assert_accounted()`.
    """
    index = _SymbolIndex(extraction.symbols)
    graph = _build_graph(extraction.references, index)
    notes: list[str] = []

    evidence_grade: set[str] | None = None
    test_paths: set[str] = set()
    if inventory is not None:
        evidence_grade = {f.path for f in inventory.files if f.evidence_grade}
        test_paths = {f.path for f in inventory.files if f.file_class is FileClass.TEST}
    else:
        notes.append(
            "sem inventário: denominador de símbolos públicos inclui todo arquivo extraído, "
            "inclusive teste e gerado — o filtro de evidence_grade não foi aplicado"
        )

    # 1) Entradas -> âncoras -> fechos individuais.
    entries: list[EntryRef] = []
    closures: list[set[str]] = []
    for ep in extraction.entrypoints:
        anchor, basis = _anchor_entry(ep, index)
        entries.append(
            EntryRef(
                kind=ep.kind,
                name=ep.name,
                path=ep.path,
                line=ep.line,
                line_end=ep.line_end or ep.line,
                framework=ep.framework,
                symbol=ep.symbol,
                detail=ep.detail,
                language=ep.language,
                anchor=anchor,
                anchor_basis=basis,
                evidence=evidence_ref_for(
                    snapshot,
                    ep.path,
                    ep.line,
                    ep.line_end or ep.line,
                    role="entrypoint",
                    symbol=ep.symbol or ep.name,
                ),
            )
        )
        closures.append(_reach(graph, [anchor]) if anchor else set())

    # 2) Hubs: alcançados por muitas entradas, não servem de sinal de união.
    hubs = _hub_symbols(closures, hub_fraction, hub_min_entries)

    # 3) União por comportamento: símbolo não-hub em comum une as entradas.
    union = _Union(len(entries))
    owners: dict[str, list[int]] = {}
    for i, closure in enumerate(closures):
        for sym in closure:
            if sym in hubs:
                continue
            owners.setdefault(sym, []).append(i)
    behavior_joined: set[int] = set()
    for idxs in owners.values():
        if len(idxs) > 1:
            first = idxs[0]
            for other in idxs[1:]:
                union.union(first, other)
                behavior_joined.add(first)
                behavior_joined.add(other)

    # 4) Desempate por prefixo de módulo — SOMENTE para entradas sem fecho.
    #    Uma entrada com fecho já foi decidida por comportamento; módulo não a
    #    move (§6.1.7: diretório é pista, não fronteira).
    module_of_entry: list[str] = []
    for entry in entries:
        module_of_entry.append(index.module_of_path.get(entry.path, "") or entry.path)
    tiebreak_joined: set[int] = set()
    by_module: dict[str, list[int]] = {}
    for i, entry in enumerate(entries):
        if closures[i]:
            continue
        by_module.setdefault(module_of_entry[i], []).append(i)
    for module, idxs in by_module.items():
        if len(idxs) > 1 and module:
            for other in idxs[1:]:
                union.union(idxs[0], other)
                tiebreak_joined.add(idxs[0])
                tiebreak_joined.add(other)

    # 5) Materialização dos grupos.
    groups: dict[int, list[int]] = {}
    for i in range(len(entries)):
        groups.setdefault(union.find(i), []).append(i)

    refs_by_from: dict[str, list[Reference]] = {}
    for ref in extraction.references:
        refs_by_from.setdefault(ref.from_symbol, []).append(ref)

    candidates: list[CapabilityCandidate] = []
    reached_all: set[str] = set()
    used_names: dict[str, int] = {}
    for _, members in sorted(groups.items(), key=lambda kv: min(kv[1])):
        member_entries = [entries[i] for i in members]
        closure: set[str] = set()
        for i in members:
            closure |= closures[i]
        reached_all |= closure

        paths: set[str] = set()
        modules: set[str] = set()
        for qual in closure:
            sym = index.by_qualname.get(qual)
            if sym is None:
                continue
            paths.add(sym.path)
            mod = index.module_of_path.get(sym.path)
            if mod:
                modules.add(mod)
        for entry in member_entries:
            paths.add(entry.path)
            mod = index.module_of_path.get(entry.path)
            if mod:
                modules.add(mod)

        # Fronteira: referência NÃO resolvida partindo de símbolo alcançado.
        # Agrupada por vínculo (chamador->alvo); repetições viram `occurrences`.
        gap_acc: dict[str, list[Reference]] = {}
        for qual in sorted(closure):
            for ref in refs_by_from.get(qual, ()):
                if ref.resolved or ref.kind not in GAP_REFERENCE_KINDS:
                    continue
                gap_acc.setdefault(f"{ref.kind}|{ref.from_symbol}|{ref.to_name}", []).append(ref)
        gaps: list[BoundaryGap] = []
        for key in sorted(gap_acc):
            group = sorted(gap_acc[key], key=lambda r: (r.path, r.line))
            first = group[0]
            gaps.append(
                BoundaryGap(
                    from_symbol=first.from_symbol,
                    to_name=first.to_name,
                    kind=first.kind,
                    path=first.path,
                    line=first.line,
                    line_end=first.line_end or first.line,
                    reason=first.reason,
                    language=first.language,
                    evidence=evidence_ref_for(
                        snapshot,
                        first.path,
                        first.line,
                        first.line_end or first.line,
                        role="unresolved_reference",
                        symbol=first.from_symbol,
                    ),
                    occurrences=len(group),
                    extra_sites=tuple((r.path, r.line) for r in group[1:max_sites_per_gap]),
                )
            )

        # Imports não resolvidos dos módulos tocados = dependência externa.
        ext_acc: dict[str, list[Reference]] = {}
        for module in sorted(modules):
            for ref in refs_by_from.get(module, ()):
                if ref.resolved or ref.kind != "import":
                    continue
                ext_acc.setdefault(ref.to_name, []).append(ref)
        externals: list[ExternalDependency] = []
        for name in sorted(ext_acc):
            group = sorted(ext_acc[name], key=lambda r: (r.path, r.line))
            first = group[0]
            externals.append(
                ExternalDependency(
                    module=first.to_name,
                    imported_by=first.from_symbol,
                    path=first.path,
                    line=first.line,
                    reason=first.reason,
                    evidence=evidence_ref_for(
                        snapshot,
                        first.path,
                        first.line,
                        first.line_end or first.line,
                        role="external_import",
                        symbol=first.from_symbol,
                    ),
                    occurrences=len(group),
                )
            )

        if any(i in behavior_joined for i in members) and len(members) > 1:
            basis = GroupingBasis.BEHAVIOR_CLOSURE
        elif any(i in tiebreak_joined for i in members) and len(members) > 1:
            basis = GroupingBasis.MODULE_PREFIX_TIEBREAK
        else:
            basis = GroupingBasis.SINGLETON

        name = _capability_name(member_entries, sorted(modules), used_names)
        stable_key = "cap:" + "|".join(sorted(e.key for e in member_entries))
        cap_id = entity_id(namespace, EntityType.CAPABILITY, stable_key)

        evidence_refs: list[EvidenceRef] = [e.evidence for e in member_entries if e.evidence]
        for entry in member_entries:
            if not entry.anchor:
                continue
            sym = index.by_qualname.get(entry.anchor)
            if sym is None:
                continue
            evidence_refs.append(
                evidence_ref_for(
                    snapshot,
                    sym.path,
                    sym.line_start,
                    sym.line_end,
                    role="anchor_symbol",
                    symbol=sym.qualname,
                )
            )
        cap_notes: list[str] = []
        if len(evidence_refs) > max_evidence_per_capability:
            cap_notes.append(
                f"evidence_refs truncado em {max_evidence_per_capability} de "
                f"{len(evidence_refs)} pontos-chave (orçamento de pacote); os demais são "
                "recuperáveis por entrypoints/reachable_symbols, nada foi descartado do modelo"
            )
            evidence_refs = evidence_refs[:max_evidence_per_capability]
        unanchored = [e for e in member_entries if not e.anchor]
        if unanchored:
            cap_notes.append(
                f"{len(unanchored)} entrada(s) sem âncora de símbolo "
                f"({', '.join(sorted(e.key for e in unanchored)[:3])}): fecho de chamadas "
                "indisponível; a capacidade cobre a entrada, não a cadeia"
            )

        candidates.append(
            CapabilityCandidate(
                capability_id=cap_id,
                name=name,
                entrypoints=tuple(sorted(member_entries, key=lambda e: e.key)),
                reachable_symbols=tuple(sorted(closure)),
                modules=tuple(sorted(modules)),
                paths=tuple(sorted(paths)),
                gaps=tuple(gaps),
                external_dependencies=tuple(externals),
                evidence_refs=tuple(evidence_refs),
                grouping_basis=basis,
                shared_symbols_used=tuple(sorted(closure & hubs)),
                notes=tuple(cap_notes),
            )
        )

    # 6) Órfãos: símbolo público de comportamento que nenhuma entrada alcança.
    orphans: list[OrphanSymbol] = []
    public_total = 0
    for qual, sym in sorted(index.by_qualname.items()):
        if sym.kind not in ORPHAN_CANDIDATE_KINDS or sym.visibility != "public":
            continue
        if evidence_grade is not None and sym.path not in evidence_grade:
            continue
        if sym.path in test_paths:
            continue
        public_total += 1
        if qual in reached_all:
            continue
        orphans.append(
            OrphanSymbol(
                qualname=qual,
                kind=sym.kind,
                path=sym.path,
                line_start=sym.line_start,
                line_end=sym.line_end,
                language=sym.language,
                module=index.module_of_path.get(sym.path, ""),
                evidence=evidence_ref_for(
                    snapshot,
                    sym.path,
                    sym.line_start,
                    sym.line_end,
                    role="orphan_symbol",
                    symbol=qual,
                ),
            )
        )

    totals: dict[str, int] = {
        "entrypoints_total": len(entries),
        "entrypoints_grouped": sum(len(c.entrypoints) for c in candidates),
        "entrypoints_unanchored": sum(1 for e in entries if not e.anchor),
        "capabilities": len(candidates),
        "capabilities_by_behavior": sum(
            1 for c in candidates if c.grouping_basis is GroupingBasis.BEHAVIOR_CLOSURE
        ),
        "capabilities_singleton": sum(
            1 for c in candidates if c.grouping_basis is GroupingBasis.SINGLETON
        ),
        "capabilities_module_tiebreak": sum(
            1 for c in candidates if c.grouping_basis is GroupingBasis.MODULE_PREFIX_TIEBREAK
        ),
        "public_symbols_total": public_total,
        "public_symbols_reached": public_total - len(orphans),
        "orphan_symbols": len(orphans),
        "boundary_gaps": sum(len(c.gaps) for c in candidates),
        "external_dependencies": sum(len(c.external_dependencies) for c in candidates),
        "shared_symbols": len(hubs),
        "symbols_total": len(index.by_qualname),
        "references_total": len(extraction.references),
        "references_resolved": len(extraction.resolved_references),
    }
    if totals["references_total"] and not totals["references_resolved"]:
        notes.append(
            "nenhuma referência resolvida na extração: todo agrupamento por comportamento "
            "é impossível nesta execução e as capacidades saem singleton por construção"
        )

    cmap = CapabilityMap(
        namespace=namespace,
        snapshot_id=snapshot.snapshot_id if snapshot else "",
        capabilities=tuple(candidates),
        orphans=tuple(orphans),
        shared_symbols=tuple(sorted(hubs)),
        totals=totals,
        notes=tuple(notes),
    )
    cmap.assert_accounted()
    return cmap


def _capability_name(
    entries: Sequence[EntryRef], modules: Sequence[str], used: dict[str, int]
) -> str:
    """Nome técnico determinístico. Módulo entra só como rótulo, nunca como fronteira."""
    kinds = "+".join(sorted({e.kind for e in entries}))
    scope = _common_dotted_prefix(modules)
    if not scope:
        paths = sorted({e.path for e in entries})
        common = paths[0].rsplit("/", 1)[0] if len(paths) == 1 else _common_path_prefix(paths)
        scope = common or f"multi({len(entries)} entradas)"
    base = f"{kinds}@{scope}"
    if len(entries) == 1:
        base = f"{base}::{entries[0].name}"
    count = used.get(base, 0)
    used[base] = count + 1
    if count:
        return f"{base}#{count + 1}"
    return base


def fingerprint(payload: Any) -> str:
    """Hash estável de estrutura — base dos ids derivados (`investigation`)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
