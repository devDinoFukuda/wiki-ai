from __future__ import annotations


class DocxError(Exception):
    pass


class BlockValidationError(DocxError):
    pass


class PackageValidationError(DocxError):
    pass
