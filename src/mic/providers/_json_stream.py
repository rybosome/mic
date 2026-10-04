"""Frame selected JSON records and validate/discard unrelated wrapper values."""

from collections.abc import Iterator
from typing import Protocol

from mic.errors import DatasetError
from mic.models import JsonValue

from ._io import parse_json


class ByteReader(Protocol):
    def read(self, size: int) -> bytes: ...


class _Cursor:
    def __init__(self, stream: ByteReader) -> None:
        self.stream: ByteReader = stream
        self.chunk: bytes = b""
        self.offset = 0
        self.eof = False

    def peek(self) -> int | None:
        if self.offset == len(self.chunk) and not self.eof:
            self.chunk = self.stream.read(8192)
            self.offset = 0
            self.eof = not self.chunk
        return None if self.eof else self.chunk[self.offset]

    def take(self) -> int:
        byte = self.peek()
        if byte is None:
            raise DatasetError("Incomplete JSON document")
        self.offset += 1
        return byte

    def whitespace(self) -> None:
        while self.peek() in (32, 9, 13, 10):
            self.take()

    def expect(self, byte: int) -> None:
        self.whitespace()
        if self.take() != byte:
            raise DatasetError("Invalid JSON document structure")

    def frame(self) -> bytes:
        # Only lexical boundaries are checked here. Decoding an individual record
        # happens later, so a reliably framed bad record can still be skipped.
        self.whitespace()
        value = bytearray()
        stack: list[int] = []
        quoted = escaped = False
        while (byte := self.peek()) is not None:
            if not quoted and not stack and byte in b",]} \t\r\n":
                break
            value.append(self.take())
            if quoted:
                if escaped:
                    escaped = False
                elif byte == ord("\\"):
                    escaped = True
                elif byte == ord('"'):
                    quoted = False
            elif byte == ord('"'):
                quoted = True
            elif byte in b"[{":
                stack.append(ord("]") if byte == ord("[") else ord("}"))
            elif byte in b"]}":
                if not stack or stack.pop() != byte:
                    raise DatasetError("Unbalanced JSON record")
        if quoted or stack or not value:
            raise DatasetError("Incomplete or empty JSON value")
        return bytes(value)

    def scalar(self, *, key: bool = False) -> JsonValue:
        self.whitespace()
        if key:
            # Object keys end at their closing quote, not at the following colon.
            self.expect(ord('"'))
            value = bytearray(b'"')
            escaped = False
            while True:
                byte = self.take()
                value.append(byte)
                if byte == ord('"') and not escaped:
                    break
                escaped = byte == ord("\\") and not escaped
            data = bytes(value)
        else:
            data = self.frame()
        try:
            return parse_json(data, "JSON wrapper")
        except DatasetError:
            # Decoder exceptions can include source tokens or invalid bytes.
            raise DatasetError("Invalid JSON wrapper value") from None

    def members(self) -> Iterator[str]:
        self.expect(ord("{"))
        self.whitespace()
        if self.peek() == ord("}"):
            self.take()
            return
        keys: set[str] = set()
        while True:
            key = self.scalar(key=True)
            assert isinstance(key, str)
            if key in keys:
                raise DatasetError("Duplicate JSON wrapper key")
            keys.add(key)
            self.expect(ord(":"))
            yield key
            self.whitespace()
            if self.peek() == ord("}"):
                self.take()
                return
            self.expect(ord(","))

    def elements(self) -> Iterator[None]:
        self.expect(ord("["))
        self.whitespace()
        if self.peek() == ord("]"):
            self.take()
            return
        while True:
            yield None
            self.whitespace()
            if self.peek() == ord("]"):
                self.take()
                return
            self.expect(ord(","))

    def discard(self, depth: int = 0) -> None:
        # Bound recursion in surrounding metadata independently of record parsing.
        self.whitespace()
        if depth >= 128 and self.peek() in (ord("{"), ord("[")):
            raise DatasetError("JSON wrapper nesting exceeds 128 levels")
        if self.peek() == ord("{"):
            for _ in self.members():
                self.discard(depth + 1)
        elif self.peek() == ord("["):
            for _ in self.elements():
                self.discard(depth + 1)
        else:
            self.scalar()


def array_records(stream: ByteReader, records_key: str | None) -> Iterator[bytes]:
    cursor = _Cursor(stream)
    if records_key is None:
        for _ in cursor.elements():
            yield cursor.frame()
    else:
        found = False
        for key in cursor.members():
            if key == records_key:
                found = True
                for _ in cursor.elements():
                    yield cursor.frame()
            else:
                cursor.discard()
        if not found:
            raise DatasetError("JSON document is missing the selected records key")
    cursor.whitespace()
    if cursor.peek() is not None:
        raise DatasetError("Unexpected content after JSON document")
