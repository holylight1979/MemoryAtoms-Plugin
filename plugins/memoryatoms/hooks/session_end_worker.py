#!/usr/bin/env python3
"""session_end_worker.py — 插件版 SessionEnd 的背景 worker：把 hook 交來的事件跑完整段收尾。

為什麼：插件 hook 的 SessionEnd 預算硬上限 1.5 秒（官方 hooks.md：「Timeouts set on plugin-provided hooks don't raise
the budget」），萃取 worker、衝突偵測、向量重建、自我迭代、同步塞不進去。handlers/session_end.py 在插件版只把事件
寫成 workflow/se-worker/<session>-<pid>.json、脫離起這支，自己立刻返回；這支讀檔（讀完刪）、照 dispatcher 的步驟
載設定、拿 state 鎖，再呼叫同一個 handle_session_end（帶 WG_SESSION_END_WORKER=1，不再派第二層）。
stdout／stderr 由派它的 hook 導到 <資料根>/Logs/session-end-worker.log；收尾一行 `SESSION_END_WORKER DONE|FAIL`。
怎麼跑：python hooks/session_end_worker.py <事件 json 檔>
"""
import json
import os
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
os.environ.setdefault("WG_SESSION_END_WORKER", "1")

STATE_LOCK_WAIT_S = 30   # 派它的 hook 返回後才放 state 鎖；等它放


def main() -> int:
    t0 = time.time()
    job = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    sid = ""
    try:
        input_data = json.loads(job.read_text(encoding="utf-8"))
        sid = str(input_data.get("session_id", ""))
    except Exception as e:
        print(f"SESSION_END_WORKER FAIL session= reason=bad_job:{type(e).__name__}", flush=True)
        return 1
    finally:
        try:
            if job:
                job.unlink()
        except OSError:
            pass
    import wg_core
    rc = 0
    try:
        config = wg_core.load_config()
        wg_core.begin_state_rmw(time.time() + STATE_LOCK_WAIT_S)
        try:
            from handlers.session_end import handle_session_end
            handle_session_end(input_data, config)
        except SystemExit:
            pass
        finally:
            wg_core.end_state_rmw()
    except Exception as e:
        rc = 1
        print(f"[session-end-worker] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        wg_core._atom_debug_error("session_end_worker", e)
    print(f"SESSION_END_WORKER {'DONE' if rc == 0 else 'FAIL'} session={sid} secs={time.time() - t0:.2f}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
