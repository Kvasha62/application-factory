"""Serialization of mutations to one deployment operation across processes.

Deployment state is written atomically (``os.replace``), and atomicity is not
mutual exclusion: two independent processes that read a record, act on the
Running Platform and write the record back can still lose one another's
history — a restart record, a reconciliation record, a whole attempt. Nothing
in this repository locked a deployment operation before this module, so the
production entry point serializes its mutations here:

* the unit of serialization is one deployment operation — the identity
  ``derive_deployment_id`` derives from the environment, the exact instance and
  the attempt number (ADR-0016 §8, §9) — so conflicting mutations of the *same*
  operation are ordered while different operations stay independent;
* the lock is a host-local, advisory ``flock`` on a file beside the state it
  protects (``<operations_dir>/locks/<sha256(deployment_id)>.lock``). The file
  carries no data and is never read: it is not a persistence-model change and
  not a second source of truth, and the kernel releases it when the holding
  process terminates — a crashed process leaves no lock behind to be cleaned up
  by hand;
* there is no distributed locking and no lock service. A deployment operation's
  state is local to the host that holds it, and a mutation that cannot be
  serialized on this host is refused rather than attempted unserialized.

The entry point holds the lock across the whole unit it mutates: for
``deploy`` that is the duplicate-attempt pre-flight and the deployment; for
``restart`` and ``reconcile`` it is the re-binding (``attach``) *and* the
operation, so a second process can never interleave between them.
"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from deployment_operations.environment import DeploymentEnvironment
from deployment_operations.production.errors import ProductionOperationRefused

try:  # POSIX. This is the supported production boundary, not a preference.
    import fcntl
except ImportError:  # pragma: no cover - no POSIX lock on this platform
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "LOCK_DIRECTORY_NAME",
    "deployment_mutation",
    "lock_path",
]

#: How long a mutation waits for another process before it is refused. It is a
#: bound, not a retry: the caller is told which operation was busy and that
#: nothing was changed, and re-running later is an explicit operator decision.
DEFAULT_LOCK_TIMEOUT_SECONDS = 300.0

#: The subdirectory of ``operations_dir`` that holds mutation locks. It is not
#: deployment state: the files are empty, never read, and named after the
#: operation they serialize.
LOCK_DIRECTORY_NAME = "locks"

_POLL_SECONDS = 0.05


def lock_path(environment: DeploymentEnvironment, deployment_id: str) -> Path:
    """The lock file that serializes mutations of one deployment operation."""
    digest = hashlib.sha256(deployment_id.encode("utf-8")).hexdigest()
    return environment.operations_dir / LOCK_DIRECTORY_NAME / f"{digest}.lock"


def _open_lock_file(path: Path) -> int:
    """Open the lock file, refusing rather than proceeding unlocked."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise ProductionOperationRefused(
            [
                (
                    f"the deployment mutation lock {path} could not be opened "
                    f"({error.__class__.__name__}: {error}); no operation was "
                    "started, and no mutation is attempted without its lock"
                )
            ]
        ) from error


def _acquire(descriptor: int, path: Path, deployment_id: str, timeout: float) -> None:
    """Take the exclusive lock within the deadline, or refuse."""
    if fcntl is None:  # pragma: no cover - no POSIX lock on this platform
        raise ProductionOperationRefused(
            [
                (
                    "this host provides no file locking (POSIX fcntl), so "
                    "mutations of one deployment operation cannot be serialized "
                    "across processes; no operation was started"
                )
            ]
        )
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise ProductionOperationRefused(
                    [
                        (
                            f"the deployment mutation lock {path} could not be "
                            f"taken ({error.__class__.__name__}: {error})"
                        )
                    ]
                ) from error
            if time.monotonic() >= deadline:
                raise ProductionOperationRefused(
                    [
                        (
                            f"{deployment_id}: another process is mutating this "
                            f"deployment operation; this invocation waited "
                            f"{timeout:g}s, changed nothing and started no "
                            "operation. Re-run it once the other process has "
                            "finished, or read deployment state to see what it "
                            "recorded."
                        )
                    ]
                ) from error
            time.sleep(_POLL_SECONDS)


@contextmanager
def deployment_mutation(
    environment: DeploymentEnvironment,
    deployment_id: str,
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Serialize mutations of one deployment operation on this host.

    Raises :class:`~deployment_operations.production.errors.ProductionOperationRefused`
    when the lock cannot be taken within ``timeout`` (default
    :data:`DEFAULT_LOCK_TIMEOUT_SECONDS`): a mutation that cannot be serialized
    is refused, never attempted unserialized.
    """
    path = lock_path(environment, deployment_id)
    descriptor = _open_lock_file(path)
    try:
        _acquire(
            descriptor,
            path,
            deployment_id,
            DEFAULT_LOCK_TIMEOUT_SECONDS if timeout is None else timeout,
        )
        try:
            yield
        finally:
            if fcntl is not None:  # pragma: no branch - _acquire guarantees it
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
