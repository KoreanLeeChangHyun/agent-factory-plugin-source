"""Codex adapter errors shared by capability probing and transport."""
import json


class NativeError(Exception):
    pass


class RpcError(NativeError):
    def __init__(self, method, error):
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__(f"{method}: {json.dumps(error)[:2000]}")
