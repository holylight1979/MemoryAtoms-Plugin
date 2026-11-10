"""
wg_atoms.py — atom 注入管線的對外入口（門面）：本檔只 re-export，實作拆在 wg_atoms_<職責>.py。

  compat   可選依賴的容錯匯入（atom_index_json／atom_locations）
  index    索引解析、atom 讀取、scope 可見性
  pool     關聯圖、Supersedes 快取、角色卡、候選池
  match    活化度、觸發詞、BM25、RRF
  render   atom 節錄渲染
  budget   熱冷分類、token 預算內載入、組注入文字
  use      用卡偵測與歸因
  truncate 依活化度裁切上下文、章節節錄
  intent   意圖分類、主題追蹤、_AIDocs 索引
  vector   向量服務、語意搜尋、增量建索引
  forget   選擇性遺忘
  realm    Realm 自動歸類 sweep
  iterate  自我迭代（晉升／封存）

既有 `from wg_atoms import …` / `wg_atoms.X` 不必改。模組層可變狀態各留在自己的檔
（_LINK_GRAPH_CACHE 在 pool、_vector_obs_logger 在 vector），測試要 patch 請指向定義該函式的檔。
"""

import json
import math
import os
import re
import sys
import time
from collections import Counter
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wg_core import (
    CLAUDE_DIR, CODE_DIR, MEMORY_DIR, WORKFLOW_DIR,
    MEMORY_INDEX, ATOM_INDEX, REALM_AUTOMOVE_MARKER,
    CONTEXT_BUDGET_DEFAULT, TURN_BUDGET_LIMIT,
    compute_token_budget,  # re-export：budget 單一來源在 wg_core，舊 caller 仍從本模組 import
    _estimate_tokens,  # CJK-aware 估算器（單一口徑，中文 ~1.5 tok/字）
    discover_all_project_memory_dirs, resolve_access_json,
    get_project_memory_dir, find_project_root, cwd_to_project_slug,
    _is_under_claude_dir, is_cross_project_local,
    log_promotion_audit, log_promotion_heartbeat,
    _atom_debug_log, _atom_debug_error,
    sanitize_harness_noise, vector_service_port,
)
from wg_platform import no_window_flags  # noqa: E402
from atom_spec import SCOPE_DIR_NAMES  # lib/（wg_core 已放進 sys.path，要排在它後面）

# ─── re-export（名稱的實作在各 wg_atoms_<職責>.py）──────────────────────────
from wg_atoms_compat import (  # noqa: E402,F401
    ATOM_INDEX_JSON,
    ENTRY_BOUND_STAYS_CORE,
    LOCAL_REALM_DEFAULT_DOMAIN,
    append_learned_terms,
    classify_realm,
    core_protected_source,
    entry_bound_ids,
    enumerate_local_paths,
    index_delete_atom,
    is_in_failures_path,
    is_local_realm_path,
    iter_atom_files_multi,
    load_atom_index_json,
    load_learned_lexicon,
    load_personal_rows,
    to_atom_entries,
)
from wg_atoms_index import (  # noqa: E402,F401
    ALIAS_RE,
    AtomEntry,
    TABLE_ROW_RE,
    _find_atom_path,
    _parse_trigger_table,
    entry_visible,
    filter_visible,
    load_access_cached,
    parse_memory_index,
    parse_project_aliases,
    read_atom_text,
    scope_from_rel_path,
    visible_vector_layers,
)
from wg_atoms_pool import (  # noqa: E402,F401
    SUPERSEDES_MEMO,
    _LINK_GRAPH_CACHE,
    _RELATED_RE,
    _SUPERSEDED_CACHE,
    _SUPERSEDES_LINE_RE,
    _SUPERSEDES_MEMO_CAP,
    _collect_v4_role_atoms,
    _load_supersedes_memo,
    _org_memory_dir,
    _related_from_file,
    build_candidate_pool,
    collect_superseded_names,
    load_link_graph,
    spread_related,
    superseded_names_cached,
)
from wg_atoms_match import (  # noqa: E402,F401
    BM25_MIN_SCORE_DEFAULT,
    RRF_ACTIVATION_GAIN,
    RRF_K_DEFAULT,
    _BM25_B,
    _BM25_CJK_STOP,
    _BM25_K1,
    _DECAY_D_MAX,
    _DECAY_D_MIN,
    _STABILITY_GAMMA_DEFAULT,
    _bm25_score,
    _bm25_tokenize,
    _decay_exponent,
    _kw_match,
    _kw_pattern,
    any_trigger_hit,
    bm25_match,
    compute_activation,
    compute_injection_rank,
    count_trigger_hits,
    match_triggers,
    rrf_fuse,
)
from wg_atoms_render import (  # noqa: E402,F401
    _FALLBACK_KNOWLEDGE_LINES,
    _FALLBACK_LINE_CHARS,
    _FRONTMATTER_KEEP_RE,
    _HTML_COMMENT_RE,
    _KNOWLEDGE_CAP_TOKENS_DEFAULT,
    _STRIP_META_RE,
    _STRIP_SECTION_RE,
    _detect_atom_type,
    _extract_named_section,
    _extract_title_and_frontmatter,
    _legacy_strip_atom_for_injection,
    _strip_atom_for_injection,
    _strip_atom_for_injection_impression_only,
)
from wg_atoms_budget import (  # noqa: E402,F401
    SUBAGENT_INJECT_MARKER,
    _COLD_LINE_CAP,
    _HOT_RECENT_DAYS,
    _HOT_RECENT_WINDOW_SEC,
    _NAME_ECHO_STRIP_RE,
    _STATUS_CAP,
    _STATUS_LINE_RE,
    _SUBAGENT_TOP_K,
    _TURN_BUDGET_LIMIT,
    _recent_count,
    _recent_reads_7d,
    atom_status_suffix,
    build_injection_blob,
    classify_hot_cold,
    decide_atom_injection,
    format_cold_inject_line,
    load_atoms_within_budget,
    pointer_path,
    title_echoes_name,
)
from wg_atoms_use import (  # noqa: E402,F401
    _ATTR_CITE_CUE,
    _ATTR_DEMONSTRATIVE,
    _ATTR_NAME_PIECE_STOP,
    _ATTR_NEG_PRED,
    _ATTR_NEG_PREFIX,
    _ATTR_PATH_NOISE,
    _ATTR_SENT_SPLIT_RE,
    _READ_EVIDENCE_TMPL,
    _RESCUE_GENERIC,
    _USE_CJK_RE,
    _USE_CODE_RE,
    _USE_EXT_RE,
    _USE_STOPWORDS,
    _attr_anchor_res,
    _attr_cited_sentences,
    _attr_clean_tokens,
    _attr_name_pieces,
    _attr_path_noise,
    _attr_read_evidence,
    _attr_rejected,
    _rescue_specific,
    _use_pieces,
    build_atom_df,
    detect_atom_use,
    detect_atom_use_v2,
    extract_distinctive_tokens,
    make_embed_tiebreak_fn,
    resolve_atom_path,
)
from wg_atoms_truncate import (  # noqa: E402,F401
    SECTION_INJECT_THRESHOLD,
    TRUNCATED_POINTER_MAX_DEFAULT,
    _RELATED_LINE_RE,
    _SECTION_HEADER_RE,
    _extract_sections,
    _resolve_block_activation,
    _truncate_context_by_activation,
)
from wg_atoms_intent import (  # noqa: E402,F401
    AiDocsEntry,
    INTENT_PATTERNS,
    _TOPIC_STOP_WORDS,
    _update_topic_tracker,
    classify_intent,
    extract_aidocs_keywords,
    parse_aidocs_index,
)
from wg_atoms_vector import (  # noqa: E402,F401
    _REKICK_COOLDOWN_S,
    _REKICK_MARKER,
    _VECTOR_OBS_LOG,
    _ensure_vector_ready,
    _get_vector_obs_logger,
    _log_vector_obs,
    _semantic_search,
    _trigger_incremental_index,
    _vector_obs_logger,
    _vector_obs_logger_failed,
)
from wg_atoms_forget import (  # noqa: E402,F401
    _forget_drop_index_entries,
    _forget_load_index_strict,
    _same_file,
    _trigger_sync_memory_index,
    apply_selective_forget,
    select_forget_candidates,
)
from wg_atoms_realm import (  # noqa: E402,F401
    _autocapture_unconfirmed_from_text,
    _is_atom_physical_rel,
    _is_unconfirmed_autocapture,
    _load_tool_module,
    _read_atom_excerpt,
    _scan_doc_refs,
    _sweep_realm_auto_migrate,
)
from wg_atoms_iterate import (  # noqa: E402,F401
    _self_iterate_atoms,
    _staging_dir_for_atom,
    archive_score,
)
