"""Errors callers can distinguish without inspecting message text."""


class MicError(Exception):
    """Base error for mic configuration and dataset setup."""


class ConfigurationError(MicError, ValueError):
    """An evaluation or invocation cannot be executed as configured."""


class DatasetError(MicError, ValueError):
    """A source cannot produce a valid, bounded evaluation dataset."""


class MissingExpectedError(MicError, ValueError):
    """A callback requested a label from an explicitly unlabeled case."""
