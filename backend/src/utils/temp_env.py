from __future__ import annotations

from contextlib import contextmanager
import os


@contextmanager
def temp_env(vars: dict[str, str]):
    """在 with 區塊內暫時覆蓋環境變數，用後即還原。"""
    old = {}
    try:
        for k, v in vars.items():
            old[k] = os.environ.get(k)
            if v is None:
                if k in os.environ:
                    del os.environ[k]
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                if k in os.environ:
                    del os.environ[k]
            else:
                os.environ[k] = v
