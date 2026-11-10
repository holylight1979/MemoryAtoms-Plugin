"""
wg_atoms_truncate.py — 上下文依活化度裁切與段落節錄：超過預算時保留高活化區塊、其餘改成指標；長 atom 按章節抽取。

誰叫它：注入管線。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import re
from typing import Any, Dict, List, Optional, Tuple
from wg_core import (
    CONTEXT_BUDGET_DEFAULT,
    MEMORY_DIR,
    _atom_debug_error,
    _atom_debug_log,
    _estimate_tokens,
    discover_all_project_memory_dirs,
)
from pathlib import Path
from wg_atoms_budget import pointer_path
from wg_atoms_match import compute_activation


# 截斷指標行數量上限（config injection.truncated_pointer_max 覆寫）：超支犧牲的
# atom 中只有 activation 最高的前 N 顆留一行指標，其餘整塊不注入——寧缺勿截，
# 截到只剩路標的條目幾乎零效用卻照樣耗 budget，數量必須有頂。
TRUNCATED_POINTER_MAX_DEFAULT = 3


def _resolve_block_activation(
    atom_name: str, src_dir: Optional[Path], fallback_roots: List[Path],
) -> Tuple[float, Optional[Path]]:
    """回 (activation, src_dir)。src_dir 給定直接算；否則只採「access sidecar 實際
    存在」的 root 取最高分（compute_activation 對缺檔回中性 0.0，不過濾會讓缺檔
    root 的 0.0 蓋掉真實負值 activation）。"""
    if src_dir:
        return compute_activation(atom_name, src_dir), src_dir
    best: Optional[float] = None
    best_dir: Optional[Path] = None
    for cand in fallback_roots:
        if not (cand / f"{atom_name}.access.json").exists():
            continue
        score = compute_activation(atom_name, cand)
        if best is None or score > best:
            best = score
            best_dir = cand
    return (0.0 if best is None else best), best_dir


def _truncate_context_by_activation(
    lines: List[str], limit: int = CONTEXT_BUDGET_DEFAULT,
    source_dirs: Optional[Dict[str, Path]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Truncate additionalContext lines to fit within token budget.

    超支時按 ACT-R activation 由低到高犧牲 atom 區塊。activation 是近期存取強度
    （log 尺度天然跨零），負值≠不相關——相關性已由 trigger/BM25/vector 入場閘
    把關，故不做 activation<=0 過濾（會誤殺低近期性但高相關的策展 atom）。
    寧缺勿截：被犧牲者中 activation 較高的前 N 顆（injection.truncated_pointer_max）
    降級成一行指標，其餘整塊移除；兩者皆落 atom-debug log，且尾行 budget 標記
    附裁切統計（可觀測性鐵律：降級必浮出訊號）。"""
    full_text = "\n".join(lines)
    used = _estimate_tokens(full_text)
    if used <= limit:
        lines.append(f"[Context budget: {used}/{limit} tokens]")
        return lines

    # 每個 atom 區塊就是 lines 裡的一個元素（assemble_injection 以一整段字串 append）；
    # 以前把「下一個 [Atom: 標頭之前的所有元素」都算進同一區塊，尾端的 Guardian 訊息
    # 會被當成最後一顆 atom 的一部分一起裁掉。
    ATOM_LINE_RE = re.compile(r"^\[Atom:(\S+)\]")
    atom_blocks: List[dict] = []
    for i, entry in enumerate(lines):
        m = ATOM_LINE_RE.match(entry)
        if not m:
            continue
        atom_blocks.append({
            "name": m.group(1),
            "start": i,
            "end": i + 1,
            "tokens": _estimate_tokens(entry),
            "first_line": entry.split("\n", 1)[0],
        })

    if not atom_blocks:
        lines.append(f"[Context budget: {used}/{limit} tokens (over)]")
        return lines

    fallback_roots: List[Path] = [MEMORY_DIR]
    try:
        for _slug, mem_dir in discover_all_project_memory_dirs():
            fallback_roots.append(mem_dir)
    except Exception as e:
        _atom_debug_error("usefulness:project_roots_discover", e)

    for ab in atom_blocks:
        ab["activation"], ab["src_dir"] = _resolve_block_activation(
            ab["name"],
            source_dirs.get(ab["name"]) if source_dirs else None,
            fallback_roots,
        )

    atom_blocks.sort(key=lambda x: x["activation"])

    def _display_path(ab: dict) -> str:
        """截斷提示的真實路徑：src_dir 優先，否則掃 roots 找實檔（絕對路徑，跨 realm 可解析）。"""
        name = ab["name"]
        src = ab.get("src_dir")
        if src is None:
            for cand in fallback_roots:
                if (cand / f"{name}.md").exists():
                    src = cand
                    break
        if src is None:
            return (MEMORY_DIR / f"{name}.md").as_posix()  # 找不到實檔的最後退路
        return pointer_path(Path(src) / f"{name}.md")

    pointer_max = int(
        ((config or {}).get("injection") or {})
        .get("truncated_pointer_max", TRUNCATED_POINTER_MAX_DEFAULT)
    )

    # Phase A：由低 activation 到高標記需犧牲的區塊（以指標行節省量估算，直到夠用）
    reduce_list: List[dict] = []
    projected = used
    for ab in atom_blocks:
        if projected <= limit:
            break
        summary = f"[Atom:{ab['name']}] (truncated) Read {_display_path(ab)}"
        saved = ab["tokens"] - _estimate_tokens(summary)
        if saved <= 0:
            continue
        ab["summary"] = summary
        ab["pointer_saved"] = saved
        reduce_list.append(ab)
        projected -= saved

    # Phase B：從 activation 高到低回填——塞得下全文就恢復全文；塞不下且指標行
    # 未達上限就留一行指標；再不行整塊移除。Phase A 以「指標行節省量」估算犧牲
    # 名單，若直接把名單外的全丟，整塊移除省下的遠多於估算，預算會被砍到遠低於
    # 上限（實測 359/1000 卻丟 5 顆）；回填讓預算用滿、犧牲最少。
    truncated_indices: set = set()
    dropped_indices: set = set()
    used_now = used - sum(ab["tokens"] for ab in reduce_list)
    pointers = 0
    for ab in reversed(reduce_list):
        ptr_tokens = ab["tokens"] - ab["pointer_saved"]
        if used_now + ab["tokens"] <= limit:
            used_now += ab["tokens"]
            _atom_debug_log(
                "BUDGET",
                f"final-trim atom={ab['name']} activation={ab['activation']:.2f} form=restored-full",
                config,
            )
        elif pointers < pointer_max and used_now + ptr_tokens <= limit:
            truncated_indices.add(ab["start"])
            used_now += ptr_tokens
            pointers += 1
            _atom_debug_log(
                "BUDGET",
                f"final-trim atom={ab['name']} activation={ab['activation']:.2f} form=pointer",
                config,
            )
        else:
            dropped_indices.add(ab["start"])
            _atom_debug_log(
                "BUDGET",
                f"final-trim atom={ab['name']} activation={ab['activation']:.2f} "
                "form=dropped（塞不下全文也塞不下指標，或指標行已達上限）",
                config,
            )
    used = used_now

    new_lines: List[str] = []
    skip_until = -1
    for idx, line in enumerate(lines):
        if idx < skip_until:
            continue
        found = False
        for ab in atom_blocks:
            if ab["start"] != idx:
                continue
            if idx in truncated_indices:
                new_lines.append(ab["summary"])
                skip_until = ab["end"]
                found = True
            elif idx in dropped_indices:
                skip_until = ab["end"]
                found = True
            break
        if not found and idx >= skip_until:
            new_lines.append(line)

    # 尾行附裁切統計：降級不得無聲（可觀測性鐵律），明細在 atom-debug log
    trim_note = ""
    if reduce_list:
        n_ptr = len(truncated_indices)
        n_drop = len(dropped_indices)
        trim_note = f" | trim: {n_ptr} pointer, {n_drop} dropped"
    new_lines.append(f"[Context budget: {used}/{limit} tokens{trim_note}]")
    return new_lines


# ─── Section-Level Extraction ───────────────────────────────────────────────

SECTION_INJECT_THRESHOLD = 200


_SECTION_HEADER_RE = re.compile(r"^(#{2,3})\s+(.+)", re.MULTILINE)


_RELATED_LINE_RE = re.compile(r"^- Related:[ \t]*.+", re.MULTILINE)


def _extract_sections(
    content: str,
    section_hints: List[Dict[str, Any]],
) -> Optional[str]:
    """Extract matching sections from atom content based on vector search hints."""
    if not section_hints:
        return None

    lines = content.split("\n")
    total_lines = len(lines)

    section_map: List[Dict[str, Any]] = []
    for m in _SECTION_HEADER_RE.finditer(content):
        level = len(m.group(1))
        header_text = m.group(2).strip()
        line_no = content[:m.start()].count("\n")
        section_map.append({
            "header": header_text,
            "level": level,
            "start": line_no,
            "end": total_lines,
        })

    for i in range(len(section_map) - 1):
        section_map[i]["end"] = section_map[i + 1]["start"]

    hint_names = set()
    for h in section_hints:
        s = h.get("section", "").strip()
        if s:
            hint_names.add(s.lower())

    matched_sections: List[Dict[str, Any]] = []
    matched_indices: set = set()

    for idx, sec in enumerate(section_map):
        header_lower = sec["header"].lower()
        if header_lower in hint_names:
            matched_sections.append(sec)
            matched_indices.add(idx)

    unmatched_hints = hint_names - {sec["header"].lower() for sec in matched_sections}
    if unmatched_hints:
        for idx, sec in enumerate(section_map):
            if idx in matched_indices:
                continue
            header_lower = sec["header"].lower()
            for hint in unmatched_hints:
                if hint in header_lower or header_lower in hint:
                    matched_sections.append(sec)
                    matched_indices.add(idx)
                    break

    if not matched_sections:
        return None

    parent_indices: set = set()
    for sec in matched_sections:
        if sec["level"] == 3:
            sec_start = sec["start"]
            candidate_idx = None
            for idx, s in enumerate(section_map):
                if s["level"] == 2 and s["start"] < sec_start:
                    candidate_idx = idx
            if candidate_idx is not None and candidate_idx not in matched_indices:
                parent_indices.add(candidate_idx)

    include_lines: set = set()

    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            include_lines.add(i)
            break
    rm = _RELATED_LINE_RE.search(content)
    if rm:
        rel_line_no = content[:rm.start()].count("\n")
        include_lines.add(rel_line_no)

    for sec in matched_sections:
        for i in range(sec["start"], sec["end"]):
            include_lines.add(i)

    for pidx in parent_indices:
        include_lines.add(section_map[pidx]["start"])

    if len(include_lines) >= total_lines * 0.70:
        return None

    omitted = len(section_map) - len(matched_sections) - len(parent_indices)
    output_lines: List[str] = []
    sorted_lines = sorted(include_lines)

    prev = -1
    for i in sorted_lines:
        if prev >= 0 and i > prev + 1:
            pass
        output_lines.append(lines[i])
        prev = i

    if omitted > 0:
        output_lines.append(f"\n[+{omitted} sections omitted]")

    return "\n".join(output_lines)
