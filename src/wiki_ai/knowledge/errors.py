from __future__ import annotations


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
