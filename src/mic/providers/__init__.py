"""Passive dataset references and lazily initialized source adapters."""

from .bigquery import BigQueryHandle, BigQueryLoader, BigQueryParameter
from .braintrust import BraintrustHandle, BraintrustLoader
from .files import FileHandle, FileLoader
from .memory import MemoryLoader

__all__ = [
    "BigQueryHandle",
    "BigQueryLoader",
    "BigQueryParameter",
    "BraintrustHandle",
    "BraintrustLoader",
    "FileHandle",
    "FileLoader",
    "MemoryLoader",
]
