"""
wg_atoms_compat.py — 可選依賴的容錯匯入：atom_index_json／atom_locations 匯入失敗時，名稱退成 None 或預設值，讓上層走降級路徑。

誰叫它：wg_atoms_* 各檔（需要這些名稱的才從這裡取）。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import sys
from pathlib import Path


# prefer _atom_index.json (machine source of truth)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))   # 程式碼用本檔位置，CLAUDE_DIR 只定位資料


try:
    from atom_index_json import (load_atom_index_json, load_personal_rows, to_atom_entries, ATOM_INDEX_JSON,
                                 delete_atom as index_delete_atom)
except ImportError:
    load_atom_index_json = None
    load_personal_rows = None
    to_atom_entries = None
    index_delete_atom = None
    ATOM_INDEX_JSON = "_atom_index.json"


try:
    from atom_locations import iter_atom_files_multi
except ImportError:
    iter_atom_files_multi = None


try:
    from atom_locations import is_in_failures_path
except ImportError:
    is_in_failures_path = None


try:
    from atom_locations import (
        classify_realm, core_protected_source, is_local_realm_path,
        enumerate_local_paths, load_learned_lexicon, append_learned_terms,
        LOCAL_REALM_DEFAULT_DOMAIN, ENTRY_BOUND_STAYS_CORE, entry_bound_ids,
    )
except ImportError:
    classify_realm = None
    core_protected_source = None
    is_local_realm_path = None
    enumerate_local_paths = None
    load_learned_lexicon = None
    append_learned_terms = None
    LOCAL_REALM_DEFAULT_DOMAIN = "Else"
    ENTRY_BOUND_STAYS_CORE = "ENTRY_BOUND_STAYS_CORE"
    entry_bound_ids = None
