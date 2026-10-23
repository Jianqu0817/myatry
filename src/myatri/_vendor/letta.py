# Copyright 2025, Letta authors
# SPDX-License-Identifier: Apache-2.0
"""Small production-used code adaptations from Letta Code.

``atomic_write`` adapts the complete-snapshot write kernel of ``writeAuthFile``:
https://github.com/letta-ai/letta-code/blob/e8073d4e475ab7f56e76f9aaeea81a7618a92575/src/backend/local/local-provider-auth-store.ts#L145-L167

Changes: TypeScript to Python; accept already-serialized bytes and an explicit
destination instead of authentication data; retain MYATRI's hidden temporary
filename and POSIX directory fsync. No credentials or auth-store files are read.
The exclusive temporary file uses upstream's 0600 creation permissions. Cleanup
errors cannot replace the original write/fsync/rename error, as in upstream.
Descriptor-close failures also preserve any earlier IO failure; a standalone
close failure is still reported. On Windows, 0600 is not a POSIX ACL guarantee.
See LICENSES/upstream/Letta-Code-LICENSE.txt for the complete upstream license.
"""
from __future__ import annotations

import os
from pathlib import Path
import uuid


def atomic_write(path: Path, data: bytes) -> None:
    """Publish complete binary snapshots through a same-directory rename.

    Before the rename, a failure leaves an existing destination unchanged.
    A POSIX directory-fsync failure is reported after publication; it cannot
    imply rollback of a rename that has already completed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # Create before entering cleanup: an O_EXCL collision belongs to somebody
    # else, so we must neither overwrite it nor remove it on failure.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary_path, flags, 0o600)
    try:
        primary_error = None
        stream = None
        try:
            stream = os.fdopen(fd, "wb", closefd=False)
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as error:
                        if primary_error is None:
                            primary_error = error
                            raise
            finally:
                try:
                    os.close(fd)
                except OSError:
                    if primary_error is None:
                        raise
        os.replace(temporary_path, path)
        # MYATRI extension: persist rename metadata where supported.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            primary_error = None
            try:
                os.fsync(directory_fd)
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    os.close(directory_fd)
                except OSError:
                    if primary_error is None:
                        raise
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            # Usually already renamed. Cleanup must not mask a failed write.
            pass
