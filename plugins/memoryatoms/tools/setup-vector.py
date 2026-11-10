"""setup-vector.py — 向量搜尋選裝（/memoryatoms:setup-vector 背後的程式）：四步，每步先講下載什麼、多大、裝到哪，點頭才做。

做什麼：
- 不帶 --apply＝只列計畫（dry-run）：四步各自已完成／待做、要下載什麼、多大、裝到哪。只看檔案，不連網、不起子行程。
- --step venv --apply：在向量 venv（插件版 ${CLAUDE_PLUGIN_DATA}/venv；沒有這個變數落到資料根 workflow/vector-venv）
  建獨立 Python 夾，照 tools/memory-vector-service/requirements.lock 的釘死版本裝套件；不動系統 Python。
- --step ollama --apply：有 Ollama 就跳過；沒有就從 Ollama 官方 GitHub 最新版下載安裝檔到檔案，比對該版公布的 sha256
  （Windows 另驗 Authenticode 簽章）才執行：Windows 官方安裝檔靜默安裝、macOS／Linux 官方 install.sh。不把下載內容直接接給 shell 執行。
- --step model --apply：ollama pull <設定的嵌入模型>（出廠 qwen3-embedding，約 4.7 GB）。
- --step extract-model --apply：ollama pull <設定的萃取模型>（vector_search.ollama_backends.local.llm_model，
  沒有看 ollama_llm_model；出廠 qwen3:1.7b，約 1.4 GB）。SessionEnd／失敗萃取用它；沒有它萃取每次失敗，
  畫面上什麼都不說，只在資料根 Logs/atom-debug.log 記一行 ERROR（lib/ollama_extract_core._call_ollama）。
- 環境變數 WG_FAKE_EXEC=1：所有會連網、下載、執行的動作只記到 WG_FAKE_EXEC_LOG（沒給就資料根 Logs/setup-vector-fake.jsonl），不真的做；測試一律用它。
怎麼跑：python tools/setup-vector.py [--step venv|ollama|model|extract-model] [--apply] [--plugin-data DIR]
  Bash 工具拿不到 CLAUDE_PLUGIN_DATA，SKILL.md 用 --plugin-data "${CLAUDE_PLUGIN_DATA}" 明給。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))
from lib.i18n import t  # noqa: E402
from lib.project_root import claude_data_dir  # noqa: E402
from lib.vector_venv import venv_dir, venv_has_lancedb, venv_python  # noqa: E402

LOCK_FILE = CODE_ROOT / "tools" / "memory-vector-service" / "requirements.lock"
RELEASE_API = "https://api.github.com/repos/ollama/ollama/releases/latest"
DOWNLOAD_PAGE = "https://ollama.com/download"
DEFAULT_MODEL = "qwen3-embedding"
DEFAULT_EXTRACT_MODEL = "qwen3:1.7b"   # 出廠 workflow/config.json 的 vector_search.ollama_backends.local.llm_model
STEPS = ("venv", "ollama", "model", "extract-model")
PLATFORM = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "linux")
INSTALLER_ASSET = {"windows": "OllamaSetup.exe", "macos": "install.sh", "linux": "install.sh"}
WINDOWS_SILENT_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]   # 官方安裝檔是 Inno Setup


# ─── 執行器：真的做／只記錄（WG_FAKE_EXEC=1） ────────────────────────────────

class RealExec:
    fake = False

    def run(self, argv: List[str], timeout: Optional[float] = None) -> Tuple[int, str]:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def fetch_json(self, url: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "memoryatoms-setup-vector"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "memoryatoms-setup-vector"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out, 1024 * 1024)


class FakeExec:
    """只記錄不執行：run 一律回成功（輸出帶 Valid 與 Ollama，讓簽章檢查走得完）；下載寫空檔；
    最新版資訊回一份假的、sha256 是空檔的值，所以比對與執行的程式路徑照樣跑過一遍。"""
    fake = True
    EMPTY_SHA = hashlib.sha256(b"").hexdigest()

    def __init__(self, log: Path) -> None:
        self.log = log

    def _record(self, kind: str, **values: Any) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"kind": kind, **values}, ensure_ascii=False, default=str) + "\n")

    def run(self, argv: List[str], timeout: Optional[float] = None) -> Tuple[int, str]:
        self._record("run", argv=[str(a) for a in argv])
        return 0, "Valid|CN=Ollama Inc."

    def fetch_json(self, url: str) -> Dict[str, Any]:
        self._record("fetch_json", url=url)
        names = sorted(set(INSTALLER_ASSET.values()))
        return {"tag_name": "fake", "assets": [
            {"name": n, "browser_download_url": f"https://example.invalid/{n}", "digest": f"sha256:{self.EMPTY_SHA}"} for n in names]}

    def download(self, url: str, dest: Path) -> None:
        self._record("download", url=url, dest=str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"")


def executor() -> Any:
    if os.environ.get("WG_FAKE_EXEC", "").strip() == "1":
        log = os.environ.get("WG_FAKE_EXEC_LOG", "").strip()
        return FakeExec(Path(log) if log else claude_data_dir() / "Logs" / "setup-vector-fake.jsonl")
    return RealExec()


# ─── 現況（只看檔案） ─────────────────────────────────────────────────────

def configured_model() -> str:
    """資料根設定 vector_search.embedding_model；沒有用出廠值。"""
    for name in ("config.local.json", "config.json"):
        try:
            cfg = json.loads((claude_data_dir() / "workflow" / name).read_text(encoding="utf-8"))
            model = (cfg.get("vector_search") or {}).get("embedding_model")
            if model:
                return str(model)
        except (OSError, ValueError, AttributeError):
            continue
    return DEFAULT_MODEL


def configured_extract_model() -> str:
    """資料根設定的本機萃取模型：vector_search.ollama_backends.local.llm_model，再看 ollama_llm_model；沒有用出廠值。"""
    for name in ("config.local.json", "config.json"):
        try:
            vs = json.loads((claude_data_dir() / "workflow" / name).read_text(encoding="utf-8")).get("vector_search") or {}
            model = ((vs.get("ollama_backends") or {}).get("local") or {}).get("llm_model") or vs.get("ollama_llm_model")
            if model:
                return str(model)
        except (OSError, ValueError, AttributeError):
            continue
    return DEFAULT_EXTRACT_MODEL


def find_ollama() -> Optional[str]:
    """PATH 上的 ollama，或官方安裝檔的預設位置。"""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = []
    if PLATFORM == "windows" and os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe")
    elif PLATFORM == "macos":
        candidates.append(Path("/Applications/Ollama.app/Contents/Resources/ollama"))
    else:
        candidates.append(Path("/usr/local/bin/ollama"))
    return next((str(c) for c in candidates if c.is_file()), None)


def models_dir() -> Path:
    override = os.environ.get("OLLAMA_MODELS", "").strip()
    return Path(override) if override else Path.home() / ".ollama" / "models"


def model_present(model: str) -> bool:
    name, _, tag = model.partition(":")
    base = models_dir() / "manifests" / "registry.ollama.ai" / "library" / name
    return (base / tag).is_file() if tag else base.is_dir() and any(base.iterdir())


def downloads_dir(plugin_data: Optional[str]) -> Path:
    base = (plugin_data or os.environ.get("CLAUDE_PLUGIN_DATA", "")).strip()
    return Path(base) / "downloads" if base else claude_data_dir() / "workflow" / "downloads"


def status_of(step: str, plugin_data: Optional[str]) -> bool:
    if step == "venv":
        return venv_python(plugin_data).is_file() and venv_has_lancedb(plugin_data)
    if step == "ollama":
        return find_ollama() is not None
    if step == "extract-model":
        return model_present(configured_extract_model())
    return model_present(configured_model())


def _status_word(done: bool) -> str:
    return t("cli.setup_vector.status_done") if done else t("cli.setup_vector.status_todo")


def _platform_texts() -> Tuple[str, str, str]:
    """計畫那行的平台字（安裝檔多大、多驗什麼、裝到哪）；鍵寫字面字串，語言表工具才掃得到。"""
    return {
        "windows": (t("cli.setup_vector.size_windows"), t("cli.setup_vector.check_windows"), t("cli.setup_vector.where_windows")),
        "macos": (t("cli.setup_vector.size_macos"), t("cli.setup_vector.check_macos"), t("cli.setup_vector.where_macos")),
        "linux": (t("cli.setup_vector.size_linux"), t("cli.setup_vector.check_linux"), t("cli.setup_vector.where_linux")),
    }[PLATFORM]


def plan_lines(steps: List[str], plugin_data: Optional[str]) -> List[str]:
    lines = []
    for step in steps:
        done = status_of(step, plugin_data)
        status = _status_word(done)
        if step == "venv":
            lines.append(t("cli.setup_vector.plan_venv", status=status, lock=LOCK_FILE, venv=venv_dir(plugin_data)))
        elif step == "ollama":
            if done:
                lines.append(t("cli.setup_vector.plan_ollama_found", status=status, path=find_ollama()))
            else:
                size, check, where = _platform_texts()
                lines.append(t("cli.setup_vector.plan_ollama", status=status, asset=INSTALLER_ASSET[PLATFORM],
                               size=size, downloads=downloads_dir(plugin_data),
                               check=check, where=where,
                               page=DOWNLOAD_PAGE))
        elif step == "extract-model":
            lines.append(t("cli.setup_vector.plan_extract_model", status=status, model=configured_extract_model(),
                           where=models_dir()))
        else:
            lines.append(t("cli.setup_vector.plan_model", status=status, model=configured_model(), where=models_dir()))
    return lines


# ─── 四步 ─────────────────────────────────────────────────────────────────

def apply_venv(ex: Any, plugin_data: Optional[str]) -> bool:
    if sys.version_info < (3, 10):
        print(t("cli.setup_vector.python_too_old", python=sys.executable, version=sys.version.split()[0]))
        return False
    target, py = venv_dir(plugin_data), venv_python(plugin_data)
    if not py.is_file():
        rc, out = ex.run([sys.executable, "-m", "venv", str(target)], timeout=600)
        if rc != 0:
            print(t("cli.setup_vector.venv_failed", venv=target, why=out.strip()[-400:]))
            return False
    rc, out = ex.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                      "--only-binary=:all:", "-r", str(LOCK_FILE)], timeout=1800)
    if rc != 0:
        print(t("cli.setup_vector.pip_failed", lock=LOCK_FILE, why=out.strip()[-400:]))
        return False
    print(t("cli.setup_vector.venv_ready", venv=target))
    return True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_ollama(ex: Any, plugin_data: Optional[str]) -> bool:
    found = find_ollama()
    if found:
        print(t("cli.setup_vector.ollama_skip", path=found))
        return True
    name = INSTALLER_ASSET[PLATFORM]
    try:
        release = ex.fetch_json(RELEASE_API)
    except Exception as e:  # 連不上 GitHub：給官網頁，讓使用者自己裝
        print(t("cli.setup_vector.release_failed", why=e, page=DOWNLOAD_PAGE))
        return False
    asset = next((a for a in release.get("assets") or [] if a.get("name") == name), None)
    digest = str((asset or {}).get("digest") or "")
    if not asset or not digest.startswith("sha256:"):
        print(t("cli.setup_vector.no_digest", asset=name, tag=release.get("tag_name"), page=DOWNLOAD_PAGE))
        return False
    dest = downloads_dir(plugin_data) / f"ollama-{release.get('tag_name')}" / name
    try:
        ex.download(asset["browser_download_url"], dest)
    except Exception as e:
        print(t("cli.setup_vector.download_failed", url=asset["browser_download_url"], why=e, page=DOWNLOAD_PAGE))
        return False
    actual, expected = _sha256(dest), digest.split(":", 1)[1].lower()
    if actual != expected:
        print(t("cli.setup_vector.sha_mismatch", file=dest, expected=expected, actual=actual))
        dest.unlink(missing_ok=True)
        return False
    if PLATFORM == "windows":
        literal = str(dest).replace("'", "''")
        rc, out = ex.run(["powershell", "-NoProfile", "-Command",
                          f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; \"$($s.Status)|$($s.SignerCertificate.Subject)\""],
                         timeout=60)
        if rc != 0 or not out.strip().startswith("Valid") or "Ollama" not in out:
            print(t("cli.setup_vector.signature_bad", file=dest, result=out.strip()[:200]))
            return False
        argv = [str(dest), *WINDOWS_SILENT_ARGS]
    else:
        argv = ["sh", str(dest)]
    print(t("cli.setup_vector.ollama_running", command=" ".join(argv)))
    rc, out = ex.run(argv, timeout=3600)
    if rc != 0:
        print(t("cli.setup_vector.ollama_failed", why=out.strip()[-400:], page=DOWNLOAD_PAGE))
        return False
    print(t("cli.setup_vector.ollama_ready", tag=release.get("tag_name")))
    return True


def _pull(ex: Any, model: str) -> bool:
    ollama = find_ollama()
    if not ollama:
        print(t("cli.setup_vector.need_ollama"))
        return False
    rc, out = ex.run([ollama, "pull", model], timeout=7200)
    if rc != 0:
        print(t("cli.setup_vector.model_failed", model=model, why=out.strip()[-400:]))
        return False
    return True


def apply_model(ex: Any, plugin_data: Optional[str]) -> bool:
    model = configured_model()
    if not _pull(ex, model):
        return False
    print(t("cli.setup_vector.model_ready", model=model))
    return True


def apply_extract_model(ex: Any, plugin_data: Optional[str]) -> bool:
    model = configured_extract_model()
    if not _pull(ex, model):
        return False
    print(t("cli.setup_vector.extract_model_ready", model=model))
    return True


APPLY = {"venv": apply_venv, "ollama": apply_ollama, "model": apply_model, "extract-model": apply_extract_model}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=t("cli.setup_vector.description"))
    ap.add_argument("--step", choices=STEPS, help=t("cli.setup_vector.arg_step"))
    ap.add_argument("--apply", action="store_true", help=t("cli.setup_vector.arg_apply"))
    ap.add_argument("--plugin-data", default=None, help=t("cli.setup_vector.arg_plugin_data"))
    args = ap.parse_args(argv)
    plugin_data = args.plugin_data or None
    steps = [args.step] if args.step else list(STEPS)
    me = f"python {Path(__file__).resolve().as_posix()}"
    data_arg = f' --plugin-data "{plugin_data}"' if plugin_data else ""

    if not args.apply:
        print(t("cli.setup_vector.dry_run_header"))
        for line in plan_lines(steps, plugin_data):
            print(line)
        todo = [s for s in steps if not status_of(s, plugin_data)]
        if todo:
            print(t("cli.setup_vector.next_step", command=f"{me} --step {todo[0]} --apply{data_arg}"))
        else:
            print(t("cli.setup_vector.all_done"))
        print("SETUP_VECTOR PLAN " + " ".join(f"{s}={'done' if s not in todo else 'todo'}" for s in steps))
        return 0
    if not args.step:
        print(t("cli.setup_vector.apply_needs_step"))
        return 2

    ex = executor()
    if ex.fake:
        print(t("cli.setup_vector.fake_exec", log=ex.log))
    ok = APPLY[args.step](ex, plugin_data)
    rest = [s for s in STEPS if not status_of(s, plugin_data)]
    if ok and rest and rest[0] != args.step:
        print(t("cli.setup_vector.next_step", command=f"{me} --step {rest[0]}{data_arg}"))
    elif ok and not rest:
        print(t("cli.setup_vector.all_done"))
    print(f"SETUP_VECTOR {'DONE' if ok else 'FAIL'} step={args.step} fake={int(ex.fake)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
