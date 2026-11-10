"""token_estimate.py — token 估算器唯一一份（注入預算、萃取 worker 預算、context 用量共用）。

口徑：CJK 約 1.5 tok/字元，其餘約 0.25 tok/字元（皆按字元計）。
CJK 判定涵蓋：漢字（U+4E00-9FFF）、CJK 標點（U+3000-303F）、かな（U+3040-30FF）、
全形/半形形式（U+FF00-FFEF，含全形標點，．，！？（）等）。
"""


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(
        1 for c in text
        if '一' <= c <= '鿿'
        or '　' <= c <= '〿'
        or '぀' <= c <= 'ヿ'
        or '＀' <= c <= '￯'
    )
    ascii_part = len(text) - cjk
    return int(cjk * 1.5 + ascii_part * 0.25)
