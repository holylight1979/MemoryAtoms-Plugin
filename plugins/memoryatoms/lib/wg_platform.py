"""wg_platform.py — 平台差異的唯一落點：Windows 與其他系統不一樣的寫法都收在這支。

做什麼：子行程用的 python（console 版／無視窗 GUI 版）、找 claude 指令、無視窗與脫離旗標、bash 位置、
MSYS 路徑轉換、主控台編碼、路徑大小寫與長路徑前綴、settings 模板的佔位填值。
資料根、家目錄不在這裡——沿用 lib/project_root.py。
怎麼用：hooks 與 lib 內 `from wg_platform import …`（lib 已在 sys.path）；tools 內 `from lib.wg_platform import …`。
本模組只用標準函式庫，import 時不碰檔案系統、不起子行程，hook 每個事件都載也不增加啟動成本。
檔名不叫 platform.py：85 處把 lib 加進 sys.path，會蓋掉標準庫 platform。
"""
from __future__ import annotations

import copy
import functools
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

CREATE_NO_WINDOW = 0x08000000   # Windows 子行程不配主控台視窗（等同 subprocess.CREATE_NO_WINDOW）
DETACHED_PROCESS = 0x00000008   # Windows 子行程脫離父行程主控台（等同 subprocess.DETACHED_PROCESS）

_EXT_UNC_PREFIX = "\\\\?\\UNC\\"
_EXT_PREFIX = "\\\\?\\"


# ─── 無視窗／脫離旗標 ───────────────────────────────────────────────────────

def no_window_flags() -> int:
    """subprocess 的 creationflags：Windows 不彈視窗，其他平台 0。"""
    return CREATE_NO_WINDOW if IS_WINDOWS else 0


def no_window_kwargs() -> Dict[str, Any]:
    """`**kwargs` 版：Windows → {"creationflags": 不彈視窗}，其他平台 {}（creationflags 在 POSIX 不能傳非 0）。"""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


def detached_flags() -> int:
    """subprocess 的 creationflags：Windows 不彈視窗且脫離父行程主控台，其他平台 0。"""
    return (CREATE_NO_WINDOW | DETACHED_PROCESS) if IS_WINDOWS else 0


def background_spawn_kwargs(detached: bool = False) -> Dict[str, Any]:
    """背景起子行程的 Popen 參數：Windows 不彈視窗（detached=True 再加脫離主控台），其他平台 start_new_session。"""
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW | (DETACHED_PROCESS if detached else 0)}
    return {"start_new_session": True}


# ─── 子行程用的 python ──────────────────────────────────────────────────────

def gui_pythonw() -> Optional[str]:
    """無 console 的 GUI pythonw；非 Windows 或都不存在回 None。先找 uv／Python 安裝管理員的全域捷徑
    %LOCALAPPDATA%\\Python\\bin（路徑無版本號，升級不破），再找跑這支程式的直譯器同目錄那支（python.org 安裝器、venv 都這樣放）。"""
    if IS_WINDOWS:
        cand = Path.home() / "AppData" / "Local" / "Python" / "bin" / "pythonw.exe"
        if cand.exists():
            return str(cand)
        sibling = Path(console_python()).with_name("pythonw.exe")
        if sibling.is_file():
            return str(sibling)
    return None


def gui_python() -> str:
    """無視窗 python：GUI pythonw；找不到退回 sys.executable。"""
    return gui_pythonw() or sys.executable


def console_sibling(exe: str) -> str:
    """exe 是 pythonw.exe 且同目錄有 python.exe → 回 python.exe（pythonw 沒有 stdout／stderr，子行程的診斷會消失）；否則原樣。"""
    p = Path(exe)
    if p.name.lower() == "pythonw.exe":
        sib = p.with_name("python.exe")
        if sib.exists():
            return str(sib)
    return str(p)


def console_python() -> str:
    """子行程用的 python（看得到輸出）：本行程若是 pythonw.exe 就換同目錄 python.exe，其他情況就是 sys.executable。"""
    return console_sibling(sys.executable)


def windowless_sibling(path: str) -> str:
    """給 pythonw 用的對應檔（python.exe → pythonw.exe）；已是 w 版或找不到就回原路徑。"""
    p = Path(path)
    if p.stem.endswith("w"):
        return str(p)
    cand = p.with_name(p.stem + "w" + p.suffix)
    return str(cand) if cand.is_file() else str(p)


def claude_cli_name() -> str:
    """Claude Code 原生安裝的可執行檔名（~/.local/bin 底下）。"""
    return "claude.exe" if IS_WINDOWS else "claude"


# ─── 給人貼的指令 ───────────────────────────────────────────────────────────
# 執行當下印給人（或 AI）照貼的指令，一律「<直譯器絕對路徑> "<腳本絕對路徑>" 參數」、一行一個、不用 ~ 與 &&：
# PowerShell 5.1、cmd、Git Bash、bash/zsh 都能照貼。直譯器寫實際那支（macOS／Linux 常只有 python3；Windows 的 python
# 可能沒進 PATH、或是商店的空殼），指令就由裝好套件、hook 在用的同一個 Python 跑。寫進檔案、會被別台機器讀的指令
# （文件、部位表）寫不出別台的絕對路徑，改寫 python "$HOME/.claude/tools/…"（文件另註：找不到 python 改用 python3）。

def _short_path(path: str) -> str:
    """Windows 8.3 短名（C:/Program Files → C:/PROGRA~1）：沒有空白，PowerShell 不必在開頭補 `&`。拿不到（檔不存在、該磁碟關了短名）回原路徑。"""
    import ctypes
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")
    except (AttributeError, OSError, ValueError):   # 不是 Windows（測試把 IS_WINDOWS 換成 True 時）
        return str(path)
    k32.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    k32.GetShortPathNameW.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    size = k32.GetShortPathNameW(str(path), buf, len(buf))
    return buf.value if 0 < size < len(buf) else str(path)


def program_token(path: str) -> str:
    """指令開頭的程式路徑：PowerShell 把開頭的引號字串當運算式而報錯，所以不加引號，並改正斜線（Git Bash 會吃掉沒加引號的
    反斜線；PowerShell、cmd 都認正斜線）。路徑有空白：Windows 先換 8.3 短名；還是有空白（短名關了、或不是 Windows）才加引號，
    那時 PowerShell 要在前面補 `& `。"""
    if IS_WINDOWS and " " in str(path):
        path = _short_path(path)
    posix = Path(path).as_posix()
    return f'"{posix}"' if " " in posix else posix


def paste_command(script: object, *args: str, python: str = "") -> str:
    """給人貼的 `<直譯器> "<腳本絕對路徑>" 參數…`。python 沒給＝本行程的直譯器（pythonw 換同資料夾的 python）；
    參數裡的路徑由呼叫端自己加引號。"""
    return " ".join([program_token(python or console_python()), f'"{script}"', *args])


# ─── 找 claude 指令 ─────────────────────────────────────────────────────────

def _named_claude(path: str) -> bool:
    """檔名是 claude（Windows 是 claude.exe）。npm 裝的 Claude Code 跑在 node 底下，那支 node 不能拿來下 claude 子命令。"""
    return Path(path).name.lower() in ("claude", "claude.exe")


def find_claude() -> Optional[str]:
    """claude 指令的絕對路徑；找不到回 None，下一步由呼叫端給。安裝器、ensure-mcp、health-weekly 共用這一支。依序：
    1. 環境變數 CLAUDE_CODE_EXECPATH：宿主（Claude Desktop、VS Code）起 claude 時給的位置；hook 行程不一定有
    2. PATH 上的 claude
    3. ~/.local/bin/claude(.exe)：原生安裝器的位置（排程器、裝完還沒重開的終端機，PATH 常沒有它）
    4. CLAUDE_PID 反查執行檔：Claude Code 給每個子行程（hook 也有），值就是跑這個行程的那支 claude
    5. 最後退路：祖先行程裡最近一支叫 claude 的（只取最近那支：再往上可能是 Claude Desktop 主程式）
    1、4、5 不是照名字找到的，都要先過 _claude_code_cli 確認才用。"""
    exe = os.environ.get("CLAUDE_CODE_EXECPATH", "").strip()
    if exe and _claude_code_cli(exe):
        return exe
    found = shutil.which("claude")
    if found:
        return found
    native = Path.home() / ".local" / "bin" / claude_cli_name()
    if native.is_file():
        return str(native)
    hint = os.environ.get("CLAUDE_PID", "").strip()
    owner = _process_image(int(hint)) if hint.isdigit() else ""
    if owner and _claude_code_cli(owner):
        return owner
    nearest = _nearest_claude_ancestor()
    return nearest if nearest and _claude_code_cli(nearest) else None


@functools.lru_cache(maxsize=8)
def _claude_code_cli(path: str) -> bool:
    """這支真的是 Claude Code 的命令列程式才用：檔名是 claude、檔案存在；Windows 的 PE 標頭要是主控台程式
    （Claude Desktop 主程式也叫 Claude.exe，但它是 GUI 程式，拿它下指令會開出 Desktop 視窗；看檔頭就排掉，不執行它）；
    macOS 排掉 .app/Contents/MacOS/ 底下的 App 主程式；最後 `--version` 要印出「(Claude Code)」。同一支只確認一次。"""
    if not (_named_claude(path) and os.path.isfile(path)):
        return False
    if IS_WINDOWS and _pe_subsystem(path) != 3:   # 3＝主控台（IMAGE_SUBSYSTEM_WINDOWS_CUI）；2＝GUI
        return False
    if ".app/Contents/MacOS/" in Path(path).as_posix():
        return False
    import subprocess
    try:
        shown = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                               **no_window_kwargs()).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "(Claude Code)" in shown


def _pe_subsystem(path: str) -> int:
    """Windows 執行檔 PE 標頭的 Subsystem 欄（2＝GUI、3＝主控台）；讀不到或不是 PE 回 0。只讀檔頭，不執行。"""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        offset = struct.unpack_from("<I", head, 0x3C)[0]
        if head[:2] != b"MZ" or head[offset:offset + 4] != b"PE\0\0":
            return 0
        return struct.unpack_from("<H", head, offset + 24 + 68)[0]   # PE 簽名 4＋COFF 標頭 20，Subsystem 在選用標頭第 68 位元組
    except (OSError, struct.error):
        return 0


def _nearest_claude_ancestor() -> str:
    """本行程的祖先（最多 32 層，遇到循環就停）裡最近一支執行檔叫 claude 的完整路徑；查不到回空字串。"""
    try:
        table = _process_table()
        pid, seen = os.getpid(), set()
        while pid in table and pid not in seen and len(seen) < 32:
            seen.add(pid)
            pid = table[pid][0]
            if _named_claude(table.get(pid, (0, ""))[1]):
                return _process_image(pid)
    except Exception:  # noqa: BLE001  查不了行程表就當找不到：呼叫端會給下一步，不能讓安裝或開場因此中止
        return ""
    return ""


def _process_table() -> Dict[int, Tuple[int, str]]:
    """pid → (父 pid, 執行檔名或路徑)。Windows 用 ctypes 拍一次 Toolhelp 快照（不起 PowerShell）；其他系統跑一次 ps。"""
    if IS_WINDOWS:
        return _windows_process_table()
    import subprocess
    # no-window-exempt: POSIX 分支（Windows 在上面改走 ctypes 快照），沒有主控台視窗問題
    listed = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=5).stdout
    table = {}
    for line in listed.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return table


def _process_image(pid: int) -> str:
    """pid 的執行檔完整路徑；查不到回空字串。Windows 問 QueryFullProcessImageNameW，Linux 讀 /proc，macOS 問 ps。"""
    try:
        if IS_WINDOWS:
            return _windows_image_path(pid)
        link = f"/proc/{pid}/exe"
        if os.path.exists(link):   # Linux：ps 的 comm 只有前 15 字，路徑看 /proc
            return os.path.realpath(link)
        import subprocess
        # no-window-exempt: POSIX 分支（Windows 在上面改走 QueryFullProcessImageNameW），沒有主控台視窗問題
        shown = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        return shown if os.path.isabs(shown) else ""   # macOS：ps 的 comm 就是完整路徑
    except Exception:  # noqa: BLE001  查不到就當沒有
        return ""


def _kernel32() -> Tuple[Any, Any]:
    """自己的一份 kernel32（不改共用的 ctypes.windll 的函式原型）與 PROCESSENTRY32W 結構。"""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32")
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32, Entry


def _windows_process_table() -> Dict[int, Tuple[int, str]]:
    import ctypes
    k32, Entry = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if snap in (None, ctypes.c_void_p(-1).value):  # INVALID_HANDLE_VALUE
        return {}
    table = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _windows_image_path(pid: int) -> str:
    import ctypes
    from ctypes import wintypes
    k32, _ = _kernel32()
    handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        k32.CloseHandle(handle)


# ─── bash 與 MSYS 路徑 ──────────────────────────────────────────────────────

_GIT_BASH_CANDIDATES = (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe")


def bash_path() -> str:
    """bash 位置：先找 Git for Windows 的真 bash（MSYS 的 bin/ 啟動殼處理不了我們轉接的 pipe），再 PATH；都沒有回空字串。"""
    for p in _GIT_BASH_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("bash") or ""


def bash_argv(script: str, *args: str) -> List[str]:
    """用 bash 跑 script 的 argv；找不到 bash 時用裸 "bash"（交給 PATH，失敗由呼叫端處理）。"""
    return [bash_path() or "bash", script, *args]


_MSYS_DRIVE_RE = re.compile(r"/([A-Za-z])(/.*)?")


def msys_drive_to_windows(token: str) -> str:
    """Git Bash 的磁碟機路徑 `/c/Users/x` → `C:/Users/x`（`/c` → `C:/`）；僅 Windows 轉，其他形式與平台原樣回傳。"""
    m = _MSYS_DRIVE_RE.fullmatch(token or "")
    if not IS_WINDOWS or not m:
        return token
    return f"{m.group(1).upper()}:{m.group(2) or '/'}"


# ─── 編碼 ───────────────────────────────────────────────────────────────────

def ansi_codepage() -> str:
    """Windows ANSI code page（GetACP，svn.exe 收 argv 用；Python 的 UTF-8 mode 會讓 locale 回 utf-8，不能信）；非 Windows 回 utf-8。"""
    if not IS_WINDOWS:
        return "utf-8"
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetACP()}"
    except (AttributeError, OSError):
        import locale
        return locale.getpreferredencoding(False)


def oem_codepage() -> str:
    """主控台 OEM code page（whoami 這類工具的輸出編碼，本機 cp950）；取不到回 mbcs。"""
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:  # noqa: BLE001
        return "mbcs"


def force_utf8_stdio(errors: Optional[str] = None) -> None:
    """Windows：stdout／stderr 改 UTF-8（預設走 cp950 會亂碼）；其他平台不動。"""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            if errors:
                stream.reconfigure(encoding="utf-8", errors=errors)
            else:
                stream.reconfigure(encoding="utf-8")


# ─── 路徑 ───────────────────────────────────────────────────────────────────

def fold_path_case(s: str, casefold: bool = False) -> str:
    """路徑比對鍵的大小寫：Windows 不分大小寫 → 轉小寫（casefold=True 用 casefold）；其他平台原樣。"""
    if not IS_WINDOWS:
        return s
    return s.casefold() if casefold else s.lower()


def strip_extended_prefix(path: str, unc_ignore_case: bool = False) -> str:
    """剝 Windows extended-length 前綴：`\\\\?\\C:\\x` → `C:\\x`、`\\\\?\\UNC\\srv\\share\\x` → `\\\\srv\\share\\x`。
    unc_ignore_case：UNC 前綴不分大小寫比對（磁碟機前綴一律區分）。不看平台，沒有前綴就原樣。"""
    is_unc = path[:8].upper() == _EXT_UNC_PREFIX.upper() if unc_ignore_case else path.startswith(_EXT_UNC_PREFIX)
    if is_unc:
        return "\\\\" + path[8:]
    if path.startswith(_EXT_PREFIX):
        return path[4:]
    return path


def pid_alive(pid: int) -> bool:
    """行程還在不在：Windows 用 OpenProcess（os.kill(pid, 0) 在 Windows 會真的送訊號），其他平台 os.kill(pid, 0)。"""
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def portable_path(path: str) -> str:
    """絕對路徑落在 LOCALAPPDATA／家目錄下 → 寫成 `$LOCALAPPDATA/…`／`$HOME/…`（settings 不帶機器專屬路徑）；否則正斜線原路徑。"""
    p = Path(path).resolve()
    for var, base in (("LOCALAPPDATA", os.environ.get("LOCALAPPDATA")), ("HOME", str(Path.home()))):
        if not base:
            continue
        try:
            rel = p.relative_to(Path(base).resolve())
        except ValueError:
            continue
        return f"${var}/{rel.as_posix()}"
    return p.as_posix()


# ─── Ollama 的位置與啟動（安裝的指令表只在 git 安裝器 tools/install.py，不進插件） ─────

def ollama_path(platform: str = sys.platform) -> str:
    """Ollama 執行檔：先看 PATH，再看官方安裝的固定位置（Windows 用 winget 剛裝好時，這個行程的 PATH 還沒更新）；都沒有回空字串。"""
    found = shutil.which("ollama")
    if found:
        return found
    if platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        candidates = [str(Path(local) / "Programs" / "Ollama" / "ollama.exe")]
    elif platform == "darwin":
        candidates = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]
    else:
        candidates = ["/usr/local/bin/ollama", "/usr/bin/ollama"]
    return next((c for c in candidates if os.path.isfile(c)), "")


def ollama_start_plan(exe: str, platform: str = sys.platform, brew: str = "") -> Dict[str, Any]:
    """daemon 沒回應時怎麼起：macOS 有 brew 就交給 brew services（重開機還在，run＝跑完就回）；
    其他情況背景起 `ollama serve`（detach＝不等它結束）。Windows 與 Linux 的官方安裝本身會掛開機自動啟動。"""
    if platform == "darwin" and brew:
        return {"detach": False, "argv": [brew, "services", "start", "ollama"]}
    return {"detach": True, "argv": [exe, "serve"]}


def start_ollama_daemon(platform: str = sys.platform) -> str:
    """把沒在跑的 Ollama daemon 叫起來（向量服務開機時用）：沿用 ollama_start_plan 的做法，背景脫離、不等結束。
    回 "started"（已發出啟動指令，不保證已就緒）／"missing"（找不到 ollama 執行檔）／"failed"（指令發不出去）。"""
    import subprocess
    exe = ollama_path(platform)
    if not exe:
        return "missing"
    plan = ollama_start_plan(exe, platform, shutil.which("brew") or "")
    try:
        if plan["detach"]:
            subprocess.Popen(plan["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **background_spawn_kwargs(detached=True))
        else:
            subprocess.run(plan["argv"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "started"


# ─── settings 模板 ──────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{(HOOK_PYTHON_PATH|HOOK_PYTHON|PYTHON|CODE_ROOT)\}\}")
_BASH_LAUNCH_RE = re.compile(r'^"\{\{HOOK_PYTHON\}\}" "\{\{CODE_ROOT\}\}/hooks/run-bash-hidden\.py" (\S+)$')


def settings_placeholders(code_root: Path) -> Dict[str, str]:
    """settings 模板佔位的值。HOOK_PYTHON＝hook 用的直譯器（Windows 是無視窗 pythonw，hook 不彈 console）、
    PYTHON＝一般直譯器（看得到輸出）、CODE_ROOT＝程式碼根（正斜線）。這兩支直譯器用 portable_path 寫法，指令字串只用這三個。
    HOOK_PYTHON_PATH＝同一支 hook 直譯器的實際路徑：給 settings.env（插件 hooks.json 靠 "$WG_HOOK_PYTHON" 起），環境變數的值不會再展開一次。"""
    root = code_root.resolve().as_posix()
    if any(c in root for c in "'\"$`"):
        raise ValueError(f"程式碼根含引號或 $ 字元，無法安全寫進 hook 指令：{root}")
    if IS_WINDOWS:
        hook = gui_pythonw() or windowless_sibling(console_python())
    else:
        hook = sys.executable
    return {"HOOK_PYTHON": portable_path(hook), "PYTHON": portable_path(console_python()), "CODE_ROOT": root,
            "HOOK_PYTHON_PATH": Path(hook).as_posix()}


def _fill(obj: Any, values: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], obj)
    if isinstance(obj, list):
        return [_fill(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, values) for k, v in obj.items()}
    return obj


def merge_fragment(base: Dict[str, Any], fragment: Dict[str, Any]) -> None:
    """插件片段併進 settings：hooks 同事件的條目接在後面；permissions 的清單去重接後面；
    statusLine 與其他頂層鍵只在 base 沒有時才補。就地改 base。"""
    for event, groups in (fragment.get("hooks") or {}).items():
        base.setdefault("hooks", {}).setdefault(event, []).extend(copy.deepcopy(groups))
    for key, items in (fragment.get("permissions") or {}).items():
        perms = base.setdefault("permissions", {})
        if isinstance(items, list):
            have = perms.setdefault(key, [])
            have.extend(x for x in items if x not in have)
        else:
            perms.setdefault(key, items)
    for key, value in fragment.items():
        if key not in ("hooks", "permissions"):
            base.setdefault(key, copy.deepcopy(value))


def _non_windows_commands(obj: Any) -> Any:
    """非 Windows：`pythonw run-bash-hidden.py x.sh` 的隱窗外殼不需要 → 直接 `bash <CODE_ROOT>/hooks/x.sh`。"""
    if isinstance(obj, str):
        m = _BASH_LAUNCH_RE.match(obj)
        return f'bash "{{{{CODE_ROOT}}}}/hooks/{m.group(1)}"' if m else obj
    if isinstance(obj, list):
        return [_non_windows_commands(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _non_windows_commands(v) for k, v in obj.items()}
    return obj


def render_settings(template: Dict[str, Any], fragments: Iterable[Dict[str, Any]], code_root: Path,
                    values: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """模板＋插件片段 → 本機的 settings 內容。values 缺省用 settings_placeholders(code_root)。"""
    vals = values or settings_placeholders(code_root)
    out = copy.deepcopy(template)
    for frag in fragments:
        merge_fragment(out, frag)
    if not IS_WINDOWS:
        out = _non_windows_commands(out)
    return _fill(out, vals)


def load_fragments(code_root: Path) -> List[Dict[str, Any]]:
    """<程式碼根>/extensions/*/settings.fragment.json（名稱排序）；壞檔 stderr 一行後略過。"""
    out = []
    for f in sorted((code_root / "extensions").glob("*/settings.fragment.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as e:
            sys.stderr.write(f"[wg_platform] 插件片段讀不了，略過 {f}: {e}\n")
    return out
