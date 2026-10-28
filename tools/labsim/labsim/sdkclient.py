"""What the SDK shims share: list a kind of device on the bench, call one."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .link import LabsimError, shared


class SdkCallError(Exception):
    def __init__(self, msg: str, kind: Optional[str] = None, code: Any = None):
        super().__init__(msg)
        self.kind, self.code = kind, code


def devices(kind: str) -> List[Dict[str, Any]]:
    """The bench's devices of this kind that are connected and on ([] without a bench)."""
    try:
        return shared().must("sdk_list", kind=kind)["devices"]
    except LabsimError:
        return []


def call(key: str, method: str, *args: Any, **kwargs: Any) -> Any:
    try:
        r = shared().call("sdk", key=key, method=method, args=list(args), kwargs=kwargs)
    except LabsimError as e:
        raise SdkCallError(str(e), "IOError")
    if "err" in r:
        raise SdkCallError(r.get("msg") or r["err"], r.get("type"), r.get("code"))
    return r["result"]
