"""
wg_session_checks.py — session 收尾與改檔時的小工具：找逐字稿、衝突偵測、跨 session 改檔回饋。

一場 session 的知識走 Stop 收割寫成卡（wg_harvest.py），這裡不產任何卡。
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from wg_core import (
    WORKFLOW_DIR, get_transcript_path, vector_service_port,
    _now_iso, _atom_debug_error,
)

_CODE_ROOT = Path(__file__).resolve().parents[1]   # 本檔所在的樹根（~/.claude 或它的 worktree）；不用 Path.home()
_TOOLS_DIR = str(_CODE_ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
_LIB_PARENT = str(_CODE_ROOT)
if _LIB_PARENT not in sys.path:
    sys.path.insert(0, _LIB_PARENT)


def _find_session_transcript(session_id: str, cwd: str) -> Optional[Path]:
    """Locate the JSONL transcript for this session.

    Path format: ~/.claude/projects/{slug}/{session_id}.jsonl
    """
    return get_transcript_path(session_id, cwd)


# ─── Conflict Detection ───────────────────────────────────────────────


def _detect_atom_conflicts(
    state: Dict[str, Any], config: Dict[str, Any]
) -> List[dict]:
    """Detect potential conflicts between session-modified atoms and existing atoms.

    For each modified atom file, query vector search (score 0.60-0.95).
    Results in that range suggest semantically similar but different content — potential conflicts.
    """
    vs_config = config.get("vector_search", {})
    if not vs_config.get("enabled", True):
        return []

    port = vector_service_port(config)
    timeout_s = 5
    conflicts: List[dict] = []

    modified = state.get("modified_files", [])
    atom_files = [
        m for m in modified
        if "/memory/" in m.get("path", "").replace("\\", "/")
        and m.get("path", "").endswith(".md")
    ]

    for m in atom_files:
        fpath = m["path"]
        try:
            content = Path(fpath).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue

        # Use first 200 chars as query
        query = content[:200].strip()
        if len(query) < 30:
            continue

        try:
            import urllib.error
            import urllib.parse
            import urllib.request
            params = urllib.parse.urlencode({
                "q": query, "top_k": 5, "min_score": 0.60,
            })
            url = f"http://127.0.0.1:{port}/search/ranked?{params}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    results = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    params = urllib.parse.urlencode({
                        "q": query, "top_k": 5, "min_score": 0.60,
                    })
                    url = f"http://127.0.0.1:{port}/search?{params}"
                    req = urllib.request.Request(url, headers={"Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        results = json.loads(resp.read())
                else:
                    continue

            fname = Path(fpath).stem
            for r in results:
                score = r.get("score", 0)
                other_name = r.get("atom_name", "")
                other_path = r.get("file_path", "")
                # Skip self-match (same file or score > 0.95)
                if score > 0.95 or other_name == fname:
                    continue
                if other_path and os.path.normpath(other_path) == os.path.normpath(fpath):
                    continue
                conflicts.append({
                    "source": fname,
                    "target": other_name,
                    "score": round(score, 3),
                    "snippet": r.get("text", r.get("content", ""))[:80],
                })

        except Exception as e:
            print(f"Conflict detection error: {e}", file=sys.stderr)
            continue

    return conflicts


# ─── Output Quality Feedback ──────────────────────────────────────────


def _check_output_quality(
    file_path: str, session_id: str, config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Check if file_path was modified in a recent (different) session.

    Scans recent state files for ended sessions. If the same file was
    modified in a previous session, returns a quality feedback record.
    Returns None if no match or if the file is a memory/plan file.
    """
    normalized = file_path.replace("\\", "/").lower()

    # Skip memory atoms and plan files — they're expected to be updated often
    if "/memory/" in normalized or "/plans/" in normalized:
        return None

    scan_count = config.get("quality_feedback", {}).get("scan_recent_sessions", 5)

    try:
        state_files = sorted(
            WORKFLOW_DIR.glob("state-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception as e:
        _atom_debug_error("萃取:_check_output_quality:glob", e)
        return None

    for sf in state_files[:scan_count + 5]:  # scan extra to skip active sessions
        sid = sf.stem.replace("state-", "")
        if sid == session_id:
            continue

        try:
            with sf.open(encoding="utf-8") as f:
                prev = json.load(f)
        except Exception as e:
            _atom_debug_error(f"萃取:_check_output_quality:parse:{sf.name}", e)
            continue

        # Only check ended sessions
        if not prev.get("ended_at"):
            continue

        prev_files = {
            m.get("path", "").replace("\\", "/").lower()
            for m in prev.get("modified_files", [])
        }

        if normalized in prev_files:
            return {
                "path": file_path,
                "original_session": sid[:8],
                "original_ended": prev.get("ended_at", ""),
                "at": _now_iso(),
            }

    return None
