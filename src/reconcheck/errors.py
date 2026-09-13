"""Exceptions raised by the engine."""


class ReconCheckError(Exception):
    """Base class for all reconcheck errors."""


class UnsupportedFormatError(ReconCheckError):
    """The input file type is not supported yet."""
