"""Secure persistence primitives for risk state and execution audit records."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1
STATE_FILE_MODE = 0o600
_ZERO_HASH = "0" * 64
_FaultInjector = Callable[[str], None]


class PersistenceSafetyError(RuntimeError):
    """Base error for persistence conditions that require trading to stop."""


class UnsafePersistencePathError(PersistenceSafetyError):
    """Raised when a state, journal, or lock path is unsafe."""


class CorruptStateError(PersistenceSafetyError):
    """Raised when state content is corrupt, incompatible, or fails integrity checks."""


class ConcurrentStateWriterError(PersistenceSafetyError):
    """Raised when another process or broker instance owns the state file."""


class AuditJournalError(PersistenceSafetyError):
    """Raised when the required audit journal is unavailable or invalid."""


class AcceptedOrderPersistenceError(PersistenceSafetyError):
    """Raised when an accepted order cannot be fully recorded durably."""


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Validated state payload and persistence metadata."""

    payload: dict[str, Any]
    generation: int
    legacy: bool = False


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CorruptStateError("persistence content is not canonical JSON") from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorruptStateError("persistence content contains a duplicate JSON key")
        result[key] = value
    return result


def _decode_json(content: bytes, *, kind: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (CorruptStateError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, CorruptStateError):
            raise
        raise CorruptStateError(f"{kind} is not valid UTF-8 JSON") from error


def _path_flags(base: int) -> int:
    return base | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _validate_stat(path: Path, metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafePersistencePathError(f"persistence path is not a regular file: {path}")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise UnsafePersistencePathError(f"persistence path is not owned by this user: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != STATE_FILE_MODE:
            raise UnsafePersistencePathError(
                f"persistence path must have mode 0600, found {mode:04o}: {path}"
            )


def _validate_fd(path: Path, descriptor: int) -> None:
    _validate_stat(path, os.fstat(descriptor))


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and path.is_junction())


def _is_trusted_posix_root_alias(path: Path) -> bool:
    if os.name != "posix" or path.parent != Path("/") or not path.is_symlink():
        return False
    metadata = path.lstat()
    root_metadata = path.parent.stat()
    root_mode = stat.S_IMODE(root_metadata.st_mode)
    return metadata.st_uid == 0 and root_metadata.st_uid == 0 and not root_mode & 0o022


def _restrict_file_mode(descriptor: int) -> None:
    if os.name == "posix":
        os.fchmod(descriptor, STATE_FILE_MODE)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    absolute_parent = path.absolute().parent
    parent_chain = (absolute_parent, *absolute_parent.parents)
    has_untrusted_link = any(
        _is_link(parent) and not _is_trusted_posix_root_alias(parent) for parent in parent_chain
    )
    if not absolute_parent.is_dir() or has_untrusted_link:
        raise UnsafePersistencePathError(
            f"persistence parent is not a real directory: {path.parent}"
        )


def _open_existing(path: Path, flags: int = os.O_RDONLY) -> int:
    if _is_link(path):
        raise UnsafePersistencePathError(f"persistence path is a link: {path}")
    try:
        descriptor = os.open(path, _path_flags(flags))
    except OSError as error:
        raise UnsafePersistencePathError(f"cannot safely open persistence path: {path}") from error
    try:
        _validate_fd(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_existing(path: Path) -> bytes:
    descriptor = _open_existing(path)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _create_or_open_lock(path: Path) -> int:
    _ensure_parent(path)
    if _is_link(path):
        raise UnsafePersistencePathError(f"persistence lock is a link: {path}")
    existed = path.exists()
    try:
        descriptor = os.open(path, _path_flags(os.O_RDWR | os.O_CREAT), STATE_FILE_MODE)
    except OSError as error:
        raise UnsafePersistencePathError(f"cannot safely open persistence lock: {path}") from error
    try:
        if not existed:
            _restrict_file_mode(descriptor)
        _validate_fd(path, descriptor)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise UnsafePersistencePathError(f"persistence lock is unsafe: {path}") from error
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _lock_descriptor(descriptor: int, *, nonblocking: bool) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
            msvcrt.locking(descriptor, mode, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(descriptor, flags)
    except (BlockingIOError, OSError) as error:
        raise ConcurrentStateWriterError("another writer owns the persistence lock") from error


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    descriptor = _create_or_open_lock(path)
    try:
        _lock_descriptor(descriptor, nonblocking=False)
        yield
    finally:
        _unlock_descriptor(descriptor)
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(
    path: Path,
    content: bytes,
    *,
    fault_injector: _FaultInjector | None = None,
) -> None:
    _ensure_parent(path)
    if path.exists() or path.is_symlink():
        descriptor = _open_existing(path)
        os.close(descriptor)

    try:
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as error:
        raise PersistenceSafetyError("cannot create an atomic persistence file") from error
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(temporary_descriptor, "wb") as handle:
            _restrict_file_mode(handle.fileno())
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if fault_injector is not None:
            fault_injector("after_temp_fsync")
        os.replace(temporary_path, path)
        if fault_injector is not None:
            fault_injector("after_replace")
        _sync_directory(path.parent)
        if fault_injector is not None:
            fault_injector("after_directory_fsync")
    except OSError as error:
        raise PersistenceSafetyError("atomic persistence replacement failed") from error
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _state_checksum(schema_version: int, generation: int, payload: dict[str, Any]) -> str:
    protected = {
        "schema_version": schema_version,
        "generation": generation,
        "payload": payload,
    }
    return hashlib.sha256(_canonical_json(protected)).hexdigest()


class SecureStateStore:
    """Versioned, integrity-checked state with one exclusive writer."""

    def __init__(self, path: str | Path, *, fault_injector: _FaultInjector | None = None):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._fault_injector = fault_injector
        self._lease_descriptor: int | None = None

    def acquire_writer(self) -> None:
        if self._lease_descriptor is not None:
            return
        descriptor = _create_or_open_lock(self.lock_path)
        try:
            _lock_descriptor(descriptor, nonblocking=True)
        except BaseException:
            os.close(descriptor)
            raise
        self._lease_descriptor = descriptor

    @property
    def has_writer(self) -> bool:
        """Return whether this store currently owns its writer lease."""
        return self._lease_descriptor is not None

    def release_writer(self) -> None:
        descriptor = self._lease_descriptor
        if descriptor is None:
            return
        self._lease_descriptor = None
        _unlock_descriptor(descriptor)
        os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._lease_descriptor is not None:
            yield
            return
        with _exclusive_lock(self.lock_path):
            yield

    def load(self) -> StateSnapshot | None:
        if self._lease_descriptor is None and not self.path.exists() and not self.path.is_symlink():
            return None
        with self._locked():
            return self._load_unlocked()

    def _load_unlocked(self) -> StateSnapshot | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        content = _read_existing(self.path)
        data = _decode_json(content, kind="risk state")
        if not isinstance(data, dict):
            raise CorruptStateError("risk state root must be an object")
        if "schema_version" not in data:
            return StateSnapshot(payload=data, generation=0, legacy=True)
        if set(data) != {"schema_version", "generation", "payload", "checksum"}:
            raise CorruptStateError("risk state envelope contains unsupported fields")
        version = data["schema_version"]
        generation = data["generation"]
        payload = data["payload"]
        checksum = data["checksum"]
        if version != STATE_SCHEMA_VERSION:
            raise CorruptStateError(f"unsupported risk state schema version: {version!r}")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise CorruptStateError("risk state generation must be a positive integer")
        if not isinstance(payload, dict) or not isinstance(checksum, str):
            raise CorruptStateError("risk state envelope has invalid field types")
        expected = _state_checksum(version, generation, payload)
        if not hmac.compare_digest(checksum, expected):
            raise CorruptStateError("risk state integrity check failed")
        return StateSnapshot(payload=payload, generation=generation)

    def save(self, payload: dict[str, Any], *, expected_generation: int | None) -> int:
        with self._locked():
            current = self._load_unlocked()
            current_generation = current.generation if current is not None else 0
            if expected_generation is not None and current_generation != expected_generation:
                raise ConcurrentStateWriterError("risk state changed after this broker loaded it")
            generation = current_generation + 1
            envelope = {
                "schema_version": STATE_SCHEMA_VERSION,
                "generation": generation,
                "payload": payload,
                "checksum": _state_checksum(STATE_SCHEMA_VERSION, generation, payload),
            }
            _atomic_write(
                self.path,
                _canonical_json(envelope) + b"\n",
                fault_injector=self._fault_injector,
            )
            return generation


def _journal_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(entry)).hexdigest()


def _journal_head_checksum(sequence: int, head_hash: str) -> str:
    protected = {"schema_version": 1, "sequence": sequence, "head_hash": head_hash}
    return hashlib.sha256(_canonical_json(protected)).hexdigest()


class SecureAuditJournal:
    """Owner-only append journal with a validated hash chain."""

    def __init__(self, path: str | Path, *, fault_injector: _FaultInjector | None = None):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.head_path = self.path.with_name(f"{self.path.name}.head")
        self._fault_injector = fault_injector

    def _load_head_unlocked(self) -> tuple[int, str]:
        if not self.head_path.exists() and not self.head_path.is_symlink():
            if (self.path.exists() or self.path.is_symlink()) and _read_existing(self.path):
                raise AuditJournalError("audit journal head is missing")
            return 0, ""
        try:
            data = _decode_json(_read_existing(self.head_path), kind="audit journal head")
        except (CorruptStateError, PersistenceSafetyError) as error:
            raise AuditJournalError("audit journal head is invalid") from error
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "sequence",
            "head_hash",
            "checksum",
        }:
            raise AuditJournalError("audit journal head fields are invalid")
        sequence = data["sequence"]
        head_hash = data["head_hash"]
        checksum = data["checksum"]
        if (
            data["schema_version"] != 1
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or not isinstance(head_hash, str)
            or len(head_hash) != 64
            or not isinstance(checksum, str)
            or not hmac.compare_digest(checksum, _journal_head_checksum(sequence, head_hash))
        ):
            raise AuditJournalError("audit journal head integrity check failed")
        return sequence, head_hash

    def _write_head_unlocked(self, sequence: int, head_hash: str) -> None:
        head = {
            "schema_version": 1,
            "sequence": sequence,
            "head_hash": head_hash,
            "checksum": _journal_head_checksum(sequence, head_hash),
        }
        _atomic_write(self.head_path, _canonical_json(head) + b"\n")

    def _scan_unlocked(self, *, anchor_sequence: int = 0, anchor_hash: str = "") -> tuple[int, str]:
        if not self.path.exists() and not self.path.is_symlink():
            if anchor_sequence:
                raise AuditJournalError("audit journal was truncated below the persisted head")
            return 0, _ZERO_HASH
        content = _read_existing(self.path)
        if not content:
            if anchor_sequence:
                raise AuditJournalError("audit journal was truncated below the persisted head")
            return 0, _ZERO_HASH
        if not content.endswith(b"\n"):
            raise AuditJournalError("audit journal ends with a truncated record")
        sequence = 0
        previous_hash = _ZERO_HASH
        for raw_line in content.splitlines():
            try:
                entry = _decode_json(raw_line, kind="audit journal record")
            except CorruptStateError as error:
                raise AuditJournalError(str(error)) from error
            if not isinstance(entry, dict):
                raise AuditJournalError("audit journal record must be an object")
            recorded_hash = entry.get("entry_hash")
            unsigned = {key: value for key, value in entry.items() if key != "entry_hash"}
            if (
                entry.get("sequence") != sequence + 1
                or entry.get("previous_hash") != previous_hash
                or not isinstance(recorded_hash, str)
                or not hmac.compare_digest(recorded_hash, _journal_hash(unsigned))
            ):
                raise AuditJournalError("audit journal hash chain is invalid")
            sequence += 1
            previous_hash = recorded_hash
            if sequence == anchor_sequence and not hmac.compare_digest(previous_hash, anchor_hash):
                raise AuditJournalError("audit journal does not match the persisted head")
        if sequence < anchor_sequence:
            raise AuditJournalError("audit journal was truncated below the persisted head")
        return sequence, previous_hash

    def validate(self) -> tuple[int, str]:
        try:
            with _exclusive_lock(self.lock_path):
                persisted_sequence, persisted_hash = self._load_head_unlocked()
                sequence, head_hash = self._scan_unlocked(
                    anchor_sequence=persisted_sequence,
                    anchor_hash=persisted_hash,
                )
                if sequence > persisted_sequence:
                    self._write_head_unlocked(sequence, head_hash)
                return sequence, head_hash
        except AuditJournalError:
            raise
        except (OSError, PersistenceSafetyError) as error:
            raise AuditJournalError(str(error)) from error

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        try:
            with _exclusive_lock(self.lock_path):
                persisted_sequence, persisted_hash = self._load_head_unlocked()
                sequence, previous_hash = self._scan_unlocked(
                    anchor_sequence=persisted_sequence,
                    anchor_hash=persisted_hash,
                )
                unsigned = {
                    **event,
                    "sequence": sequence + 1,
                    "previous_hash": previous_hash,
                }
                entry = {**unsigned, "entry_hash": _journal_hash(unsigned)}
                encoded = _canonical_json(entry) + b"\n"
                _ensure_parent(self.path)
                if _is_link(self.path):
                    raise UnsafePersistencePathError(f"persistence path is a link: {self.path}")
                existed = self.path.exists()
                descriptor = os.open(
                    self.path,
                    _path_flags(os.O_WRONLY | os.O_APPEND | os.O_CREAT),
                    STATE_FILE_MODE,
                )
                try:
                    if not existed:
                        _restrict_file_mode(descriptor)
                    _validate_fd(self.path, descriptor)
                    if self._fault_injector is not None:
                        self._fault_injector("before_append")
                    written = 0
                    while written < len(encoded):
                        written += os.write(descriptor, encoded[written:])
                    if self._fault_injector is not None:
                        self._fault_injector("after_append")
                    os.fsync(descriptor)
                    if self._fault_injector is not None:
                        self._fault_injector("after_journal_fsync")
                finally:
                    os.close(descriptor)
                if not existed:
                    _sync_directory(self.path.parent)
                self._write_head_unlocked(sequence + 1, entry["entry_hash"])
                return entry
        except AuditJournalError:
            raise
        except (OSError, PersistenceSafetyError) as error:
            raise AuditJournalError("audit journal append failed") from error

    def tail(self, limit: int = 3) -> list[dict[str, Any]]:
        """Return validated trailing records."""
        if limit <= 0 or (not self.path.exists() and not self.path.is_symlink()):
            return []
        try:
            with _exclusive_lock(self.lock_path):
                persisted_sequence, persisted_hash = self._load_head_unlocked()
                sequence, head_hash = self._scan_unlocked(
                    anchor_sequence=persisted_sequence,
                    anchor_hash=persisted_hash,
                )
                if sequence > persisted_sequence:
                    self._write_head_unlocked(sequence, head_hash)
                if not self.path.exists() and not self.path.is_symlink():
                    return []
                content = _read_existing(self.path)
                return [
                    entry
                    for entry in (
                        _decode_json(line, kind="audit journal record")
                        for line in content.splitlines()[-limit:]
                    )
                    if isinstance(entry, dict)
                ]
        except AuditJournalError:
            raise
        except (CorruptStateError, OSError, PersistenceSafetyError) as error:
            raise AuditJournalError("audit journal tail is unavailable") from error


_SENSITIVE_KEYS = {
    "account",
    "account_id",
    "account_number",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "secret_key",
    "token",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|secret(?:[_ -]?key)?|password|token|authorization)\s*[:=]\s*\S+"
)
_ACCOUNT_IDENTIFIER = re.compile(r"\b(?:DU\d{4,}|PA[A-Z0-9-]{4,}|PK[A-Z0-9]{8,})\b")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """Redact credential and account identifiers from retained diagnostics."""
    normalized_key = key.lower().replace("-", "_") if key is not None else None
    if normalized_key in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_sensitive(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, BaseException):
        value = str(value)
    if isinstance(value, str):
        value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
        value = _BEARER.sub("Bearer [REDACTED]", value)
        return _ACCOUNT_IDENTIFIER.sub("[REDACTED]", value)
    return value


__all__ = [
    "AcceptedOrderPersistenceError",
    "AuditJournalError",
    "ConcurrentStateWriterError",
    "CorruptStateError",
    "PersistenceSafetyError",
    "SecureAuditJournal",
    "SecureStateStore",
    "StateSnapshot",
    "UnsafePersistencePathError",
    "redact_sensitive",
]
