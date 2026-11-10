"""transcript.py — Claude Code transcript（jsonl）的單一讀法。

規則（所有 hook／worker 共用）：一行一筆 JSON；壞行（半行、非 JSON、不是物件）整行跳過，不拋例外；
assistant 文字＝type=="assistant" 的紀錄裡 message.content 清單中 type=="text" 的區塊。
各處要「取多少」（最後一則／本回合／從 offset 起／尾段）不同，那是呼叫端的事；這裡只管「怎麼讀一行、怎麼取文字」。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def parse_record(raw: Union[str, bytes]) -> Optional[Dict[str, Any]]:
    """一行 jsonl → dict；壞行或不是物件回 None。"""
    try:
        obj = json.loads(raw)
    except ValueError:    # JSONDecodeError 與 UnicodeDecodeError 都是 ValueError 子類
        return None
    return obj if isinstance(obj, dict) else None


def parse_records(text: str) -> List[Dict[str, Any]]:
    """整份 transcript 文字 → 依序的紀錄 list（壞行跳過）。"""
    out: List[Dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        rec = parse_record(raw)
        if rec is not None:
            out.append(rec)
    return out


def assistant_text_blocks(rec: Dict[str, Any], min_chars: int = 1) -> List[str]:
    """一筆紀錄是 assistant 時，回它 content 裡長度 >= min_chars 的 text 區塊文字（依序）；不是 assistant 回 []。"""
    if rec.get("type") != "assistant":
        return []
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    out: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text", "")
            if isinstance(t, str) and t and len(t) >= min_chars:
                out.append(t)
    return out


def last_assistant_text_in(text: str, min_chars: int = 1) -> str:
    """整份 transcript 文字裡最後一個 assistant text 區塊（長度 >= min_chars）；沒有回 ""。"""
    last = ""
    for rec in parse_records(text):
        blocks = assistant_text_blocks(rec, min_chars)
        if blocks:
            last = blocks[-1]
    return last


def last_assistant_text(path: Union[str, Path], min_chars: int = 1) -> str:
    """讀檔版：讀不到（OSError／非 UTF-8）回 ""。逐行讀，不整檔進記憶體。"""
    last = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                rec = parse_record(raw)
                if rec is None:
                    continue
                blocks = assistant_text_blocks(rec, min_chars)
                if blocks:
                    last = blocks[-1]
    except (OSError, UnicodeDecodeError):
        return ""
    return last
