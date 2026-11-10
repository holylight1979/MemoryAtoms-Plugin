"""session_start_persona.py — 插件版的「個人化啟動」：開場送身分、使用者、卡片目錄與記憶規矩。

做什麼（只在插件版跑；安裝版由 CLAUDE.md @import 與 user-init.sh 負責，這裡直接 exit 0、不輸出）：
1. 資料根沒有 IDENTITY.md／USER.md 就照語言從 templates/ 種一份（取代 hooks/user-init.sh 在插件版的工作；選模板規則同它：
   該語言的模板在就用，否則用英文版；{{USERNAME}} 換成作業系統使用者名）。
2. 送 IDENTITY.md、USER.md、<資料根>/memory/MEMORY.md、templates/memory-rules.<語言>.md。
   Claude 設定目錄的 CLAUDE.md 已含 @IDENTITY.md 匯入（遷移過來的人）→ IDENTITY／USER 不重送（CLAUDE.md 已載）。
3. 原生記憶提醒：Claude 設定目錄 settings.json 的 autoMemoryEnabled 不是 false，且 SessionStart 的 source 是 startup／resume → 加一行建議。
4. 總長超過 wg_core.HOOK_TEXT_CAP 就走 wg_core.cap_hook_text（溢出全文留檔、截斷）。
這是 hooks.json 的第二條 SessionStart，自己一份 10,000 字元上限，不與 session_start.py 共用。

MEMORY.md 在第一場可能還沒產生（出廠卡展開與目錄重建在另一條 SessionStart，同事件的 hook 並行）→ 缺檔就略過，下一場才有。
"""
from __future__ import annotations

import getpass
import json
import re
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT / "lib"))
from edition import edition  # noqa: E402
from project_root import claude_config_dir, claude_data_dir  # noqa: E402
import i18n  # noqa: E402  # lib/：給人看的提示走語言表

HINT_SOURCES = ("startup", "resume")
_IMPORT_RE = re.compile(r"(?m)^[^\n`]*@IDENTITY\.md\b")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _template(stem: str, lang: str) -> Path:
    """同 user-init.sh pick_template：非英文且有該語言模板就用，否則英文版。"""
    base = CODE_ROOT / "templates" / f"{stem}.template"
    localized = Path(f"{base}.{lang}.md")
    return localized if lang != "en" and localized.is_file() else Path(f"{base}.md")


def _safe_lang(lang: str) -> str:
    return lang if re.fullmatch(r"[A-Za-z0-9_-]+", lang or "") else "en"


def seed_missing(data_dir: Path, lang: str) -> list[str]:
    """缺的 IDENTITY.md／USER.md 從模板種一份；回種了哪些檔名。模板也缺的丟進 missing 由呼叫端紅字。"""
    seeded: list[str] = []
    try:
        username = getpass.getuser()
    except Exception:
        username = "user"
    for name, stem in (("IDENTITY.md", "IDENTITY"), ("USER.md", "USER")):
        target = data_dir / name
        if target.is_file():
            continue
        tpl = _template(stem, lang)
        try:
            text = tpl.read_text(encoding="utf-8-sig").replace("{{USERNAME}}", username)
            data_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            seeded.append(name)
        except OSError as e:
            print(i18n.t("hook.session_start_persona.seed_failed", name=name, error=e), file=sys.stderr)
    return seeded


def claude_md_imports_identity() -> bool:
    """Claude 設定目錄的 CLAUDE.md 是否含 @IDENTITY.md 匯入（行首、不在程式碼區塊行內）。"""
    return bool(_IMPORT_RE.search(_read(claude_config_dir() / "CLAUDE.md")))


def native_memory_on() -> bool:
    """settings.json 的 autoMemoryEnabled 不是 false 就算開著（Claude Code 預設開）。讀不到設定當開著。"""
    try:
        s = json.loads((claude_config_dir() / "settings.json").read_text(encoding="utf-8-sig"))
        return not (isinstance(s, dict) and s.get("autoMemoryEnabled") is False)
    except (OSError, ValueError):
        return True


def build(source: str) -> str:
    data_dir = claude_data_dir()
    lang = _safe_lang(i18n.current_language())
    seed_missing(data_dir, lang)
    parts: list[str] = []
    if not claude_md_imports_identity():
        for name in ("IDENTITY.md", "USER.md"):
            body = _read(data_dir / name)
            if body:
                parts.append(f"## {name}\n{body}")
    catalog = _read(data_dir / "memory" / "MEMORY.md")
    if catalog:
        parts.append(f"## memory/MEMORY.md\n{catalog}")
    rules = _template_rules(lang)
    if rules:
        parts.append(rules)
    if source in HINT_SOURCES and native_memory_on():
        parts.append(i18n.t("hook.session_start_persona.native_memory_hint"))
    return "\n\n".join(parts)


def _template_rules(lang: str) -> str:
    for cand in (lang, "en"):
        body = _read(CODE_ROOT / "templates" / f"memory-rules.{cand}.md")
        if body:
            return body
    print(i18n.t("hook.session_start_persona.rules_missing"), file=sys.stderr)
    return ""


def main() -> None:
    if edition() != "plugin":
        return
    try:
        data = json.loads(sys.stdin.buffer.read() or b"{}")
    except ValueError:
        data = {}
    source = str(data.get("source") or "") if isinstance(data, dict) else ""
    text = build(source)
    if not text:
        return
    if len(text) > 4000:   # 離上限還遠就不載 wg_core（它載入較重）；cap_hook_text 自己再判上限
        sys.path.insert(0, str(CODE_ROOT / "hooks"))
        import wg_core
        text = wg_core.cap_hook_text(text, "session_start_persona")
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # fail-open：開場注入失敗不擋 session，但要有訊號
        print(i18n.t("hook.session_start_persona.failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
