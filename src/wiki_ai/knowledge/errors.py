from __future__ import annotations

from dataclasses import dataclass


class KnowledgeError(Exception):
    pass


class InvalidKind(KnowledgeError):
    pass


class InvalidIdentity(KnowledgeError):
    pass


class LocatorInvalid(KnowledgeError):
    pass


class PayloadInvalid(KnowledgeError):
    pass


class UnknownReference(KnowledgeError):
    pass


class RevisionClosed(KnowledgeError):
    pass


class FormatVersionMismatch(KnowledgeError):
    pass


class UnsupportedEvidence(KnowledgeError):
    pass


class RelationCycle(KnowledgeError):
    pass


class UnknownKind(KnowledgeError):
    pass


class InvalidRelationPair(KnowledgeError):
    pass


class MissingRequiredAttribute(KnowledgeError):
    pass


class GapNotFound(KnowledgeError):
    pass


@dataclass(frozen=True)
class IdentityFacts:
    entity_id: str
    kind: str
    owner_id: str | None
    canonical_name: str


class IdentityCollision(KnowledgeError):
    def __init__(self, existing: IdentityFacts, incoming: IdentityFacts) -> None:
        super().__init__(
            f"id {incoming.entity_id} já pertence a "
            f"kind={existing.kind} owner={existing.owner_id or '-'} "
            f"nome={existing.canonical_name}; recebido "
            f"kind={incoming.kind} owner={incoming.owner_id or '-'} "
            f"nome={incoming.canonical_name}"
        )
        self.existing = existing
        self.incoming = incoming

