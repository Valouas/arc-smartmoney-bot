"""Publish complete JSON snapshots with same-directory atomic replacement."""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def atomic_text(path, *, encoding='utf-8', newline=None):
    """Never truncate the previous export; tolerate brief Windows reader locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding=encoding, newline=newline, dir=path.parent,
                                         prefix=path.name+'.', suffix='.tmp', delete=False) as f:
            name = f.name
            yield f
            f.flush()
            os.fsync(f.fileno())
        from data_preservation import before_replace
        before_replace(path,name)
        for attempt in range(3):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(.05 * 2**attempt)
    finally:
        if name and os.path.exists(name): os.unlink(name)


def write_json(path, payload):
    with atomic_text(path) as f:
        json.dump(payload, f, indent=2)
