"""Exceptions raised by the engine."""


class ReconCheckError(Exception):
    """Base class for all reconcheck errors."""


class UnsupportedFormatError(ReconCheckError):
    """The input file type is not supported yet."""


class TextDecodeError(ReconCheckError):
    """File bytes did not decode into plausible text (binary/junk input)."""


class EmptyDocumentError(ReconCheckError):
    """A document contains no parseable table."""


class PdfOcrRequiredError(ReconCheckError):
    """The PDF has no extractable text layer; OCR is required but not wired."""
