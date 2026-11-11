"""pack-shipped-cards.py — 發版時把出廠卡打包成 memory/shipped-N.jsonl＋memory/shipped-manifest.json。

做什麼：插件資料夾每檔 < 256 KiB、總數 ≤ 512 檔，四百多張卡不能一張一檔進包，所以打成幾包；
插件版第一場由 lib/shipped_cards.sync_shipped 展開到資料根。挑哪些檔見 lib/shipped_cards.iter_shipped_sources。
- 每行 {"path","sha256","text"}（path 是 memory/… 相對路徑、sha256 是 text 的 UTF-8 sha256），依 path 排序；
  每包 < --max-kib（預設 240 KiB）。
- manifest：version（version.json 的 guardian 版號）、count、packs[{file, count, bytes, sha256}]。
- 可重入：舊的 shipped-*.jsonl 先刪再寫；內容一樣時輸出位元組也一樣（W9 改完卡裡的路徑字串後直接重跑）。
怎麼跑：python tools/pack-shipped-cards.py [--root <程式碼根>] [--out <輸出資料夾，預設 <root>/memory>] [--max-kib 240] [--check] [--bundle <plugin-bundle.json>]
  --bundle：讀該檔的 exclude_cards（卡的相對路徑清單）略過不打包；不給＝全打（原始碼 repo 自己打包時）。
  --check：不寫檔，只比對現有輸出跟重算的一不一樣（不一樣 exit 1）。
尾行：PACK_SHIPPED DONE cards=<n> packs=<n> bytes=<n> max_pack=<n>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from shipped_cards import MANIFEST_NAME, PACK_PATTERN, iter_shipped_sources, sha256_text  # noqa: E402


def build(root: Path, max_bytes: int, exclude: frozenset = frozenset()) -> tuple:
    """回 ([(檔名, bytes)], manifest dict)。"""
    packs: list = []
    current: list = []
    size = 0
    for rel, path in iter_shipped_sources(root):
        if rel in exclude:
            continue
        text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        line = (json.dumps({"path": rel, "sha256": sha256_text(text), "text": text},
                           ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(line) >= max_bytes:
            raise SystemExit(f"card too large for one pack: {rel} ({len(line)} bytes)")
        if current and size + len(line) >= max_bytes:
            packs.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        packs.append(current)
    outputs = []
    meta = []
    for i, lines in enumerate(packs, start=1):
        data = b"".join(lines)
        name = PACK_PATTERN.format(n=i)
        outputs.append((name, data))
        meta.append({"file": name, "count": len(lines), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    try:
        version = json.loads((root / "version.json").read_text(encoding="utf-8")).get("guardian", "")
    except (OSError, ValueError):
        version = ""
    manifest = {"version": version, "count": sum(m["count"] for m in meta), "packs": meta}
    outputs.append((MANIFEST_NAME, (json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8")))
    return outputs, manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-kib", type=int, default=240)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--bundle", type=Path, default=None)
    a = ap.parse_args()
    out = a.out or a.root / "memory"
    exclude = frozenset(k for k in json.loads(a.bundle.read_text(encoding="utf-8")).get("exclude_cards", {}) if not k.startswith("_")) if a.bundle else frozenset()
    outputs, manifest = build(a.root, a.max_kib * 1024, exclude)
    if a.check:
        stale = [n for n, data in outputs if not (out / n).is_file() or (out / n).read_bytes() != data]
        extra = [p.name for p in out.glob("shipped-*.jsonl") if p.name not in {n for n, _ in outputs}]
        for n in stale + extra:
            print(f"stale: {n}", file=sys.stderr)
        if stale or extra:
            return 1
    else:
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("shipped-*.jsonl"):
            old.unlink()
        for name, data in outputs:
            (out / name).write_bytes(data)
    sizes = [m["bytes"] for m in manifest["packs"]]
    print(f"PACK_SHIPPED DONE cards={manifest['count']} packs={len(sizes)} bytes={sum(sizes)} max_pack={max(sizes, default=0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
