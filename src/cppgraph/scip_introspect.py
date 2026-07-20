"""Introspect a `.scip` index without building a graph from it.

The `index` wizard shows this to the user before deciding whether to reuse an
existing `.scip` or recompute it (a recompute can take hours). Everything here is
pure — it reads the file and returns plain data, no side effects.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path


def describe_scip(path: str | Path) -> dict:
    """Summarise a `.scip` file for a reuse/recompute decision.

    Returns a dict. When the file is absent: ``{"exists": False, "path": ...}``.
    When present: ``exists``, ``path``, ``size_bytes``, ``mtime`` (epoch float),
    ``mtime_iso``, and — if it parses — ``tool_name``, ``tool_version``,
    ``project_root``, ``document_count``. A parse failure sets ``error`` and
    leaves the SCIP-derived fields absent, so a corrupt `.scip` still yields a
    usable (recompute-leaning) description rather than raising.
    """
    p = Path(path)
    if not p.exists():
        return {"exists": False, "path": str(p)}

    st = p.stat()
    info: dict = {
        "exists": True,
        "path": str(p),
        "size_bytes": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
    }

    # Import lazily: the proto runtime is a heavy, optional-at-rest dependency.
    from cppgraph.proto import scip_pb2

    try:
        index = scip_pb2.Index()
        with open(p, "rb") as f:
            index.ParseFromString(f.read())
    except Exception as exc:  # a truncated/corrupt .scip must not crash the wizard
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    md = index.metadata
    info["tool_name"] = md.tool_info.name or None
    info["tool_version"] = md.tool_info.version or None
    info["project_root"] = md.project_root or None
    info["document_count"] = len(index.documents)
    return info
