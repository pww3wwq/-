#!/usr/bin/env python3
"""GitHub リポジトリを経由して ComfyUI を監視・操作するローカル常駐エージェント。

  commands/*.json  … 受信箱。リモート（Claude や自分）が push した命令。
  status/*.json    … 送信箱。エージェントが書いて push する状態と結果。

PC を外部に公開せず、git の pull/push だけで双方向のやり取りを成立させる。
Windows / macOS / Linux で動く（python3 と git があればよい）。

    python3 agent.py --config config.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy_client import ComfyClient, ComfyError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "comfy_url": "http://127.0.0.1:8188",
    "remote": "origin",
    "branch": "claude/test-functionality-2fkj05",
    "push": True,
    "poll_interval": 20,
    "stall_seconds": 300,
    "history_items": 5,
    "max_alerts": 50,
    "enable_websocket": True,
    "allowed_actions": [
        "status",
        "queue",
        "interrupt",
        "clear_queue",
        "delete",
        "free",
        "history",
        "object_info",
    ],
    "git_user_name": "comfy-agent",
    "git_user_email": "comfy-agent@localhost",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{utcnow()}] {msg}", flush=True)


# ----------------------------------------------------------------------
# git
# ----------------------------------------------------------------------
def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {(proc.stderr or proc.stdout).strip()}")
    return proc


def git_sync(repo: Path, cfg: dict) -> None:
    """リモートの最新（＝新しい命令）を取り込む。"""
    remote, branch = cfg["remote"], cfg["branch"]
    run_git(repo, "fetch", remote, branch)
    run_git(repo, "pull", "--rebase", "--autostash", remote, branch)


def git_publish(repo: Path, cfg: dict, message: str) -> bool:
    """status/ の変更をコミットして push する。変更がなければ False。"""
    run_git(repo, "add", "status")
    if not run_git(repo, "status", "--porcelain", "status").stdout.strip():
        return False

    run_git(repo, "-c", f"user.name={cfg['git_user_name']}",
            "-c", f"user.email={cfg['git_user_email']}",
            "commit", "-m", message)

    if not cfg.get("push", True):
        return True

    remote, branch = cfg["remote"], cfg["branch"]
    delay = 2
    for attempt in range(4):
        if run_git(repo, "push", "-u", remote, f"HEAD:{branch}", check=False).returncode == 0:
            return True
        log(f"push 失敗（{attempt + 1}/4）。{delay}s 後に rebase して再試行")
        time.sleep(delay)
        delay *= 2
        run_git(repo, "pull", "--rebase", "--autostash", remote, branch, check=False)
    log("push を 4 回試して失敗。次のループで再試行する")
    return True


# ----------------------------------------------------------------------
# WebSocket から拾うライブ進捗
# ----------------------------------------------------------------------
class LiveState:
    """ComfyUI の /ws から流れてくる進捗を保持する（スレッド間共有）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.client_id = str(uuid.uuid4())
        self.connected = False
        self.prompt_id: str | None = None
        self.node: str | None = None
        self.progress: dict | None = None
        self.last_event: str | None = None
        self.last_event_at: str | None = None
        self.last_progress_key: tuple | None = None
        self.last_progress_at: float = time.time()
        self.pending_alerts: list[dict] = []

    def set_connected(self, value: bool) -> None:
        with self.lock:
            self.connected = value

    def add_alert(self, kind: str, detail: dict) -> None:
        with self.lock:
            self.pending_alerts.append({"at": utcnow(), "kind": kind, "detail": detail})

    def drain_alerts(self) -> list[dict]:
        with self.lock:
            out, self.pending_alerts = self.pending_alerts, []
            return out

    def on_event(self, msg: dict) -> None:
        mtype = msg.get("type")
        data = msg.get("data") or {}
        now = time.time()
        with self.lock:
            self.last_event = mtype
            self.last_event_at = utcnow()

            if mtype == "execution_start":
                self.prompt_id = data.get("prompt_id")
                self.node = None
                self.progress = None
                self.last_progress_at = now
            elif mtype == "executing":
                self.node = data.get("node")
                self.prompt_id = data.get("prompt_id") or self.prompt_id
                self.last_progress_at = now
                if data.get("node") is None:  # そのプロンプトの完了合図
                    self.progress = None
            elif mtype == "progress":
                self.progress = {"value": data.get("value"), "max": data.get("max")}
                self.node = data.get("node") or self.node
                key = (data.get("prompt_id"), data.get("node"), data.get("value"))
                if key != self.last_progress_key:
                    self.last_progress_key = key
                    self.last_progress_at = now
            elif mtype in ("executed", "execution_cached", "execution_interrupted"):
                self.last_progress_at = now
            elif mtype == "execution_error":
                self.last_progress_at = now
                self.pending_alerts.append(
                    {
                        "at": utcnow(),
                        "kind": "execution_error",
                        "detail": {
                            "prompt_id": data.get("prompt_id"),
                            "node_id": data.get("node_id"),
                            "node_type": data.get("node_type"),
                            "exception_type": data.get("exception_type"),
                            "exception_message": data.get("exception_message"),
                            "traceback": (data.get("traceback") or [])[-8:],
                        },
                    }
                )

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "websocket_connected": self.connected,
                "prompt_id": self.prompt_id,
                "node": self.node,
                "progress": self.progress,
                "last_event": self.last_event,
                "last_event_at": self.last_event_at,
                "seconds_since_progress": round(time.time() - self.last_progress_at, 1),
            }


def websocket_loop(cfg: dict, state: LiveState, stop: threading.Event) -> None:
    try:
        import websocket  # websocket-client
    except ImportError:
        log("websocket-client が無いのでライブ進捗は無効（HTTP ポーリングのみで動作）")
        return

    base = cfg["comfy_url"].rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_url}/ws?clientId={state.client_id}"

    while not stop.is_set():
        sock = None
        try:
            sock = websocket.WebSocket()
            sock.connect(url, timeout=10)
            sock.settimeout(5)
            state.set_connected(True)
            log(f"WebSocket 接続: {url}")
            while not stop.is_set():
                try:
                    raw = sock.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw or isinstance(raw, (bytes, bytearray)):
                    continue  # バイナリはプレビュー画像なので捨てる
                try:
                    state.on_event(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as exc:  # 接続断は想定内。黙って張り直す
            state.set_connected(False)
            log(f"WebSocket 切断: {exc}")
            stop.wait(5)
        finally:
            state.set_connected(False)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


# ----------------------------------------------------------------------
# 命令の実行
# ----------------------------------------------------------------------
def apply_overrides(workflow: dict, overrides: dict) -> dict:
    """"6.inputs.text" のようなドット区切りパスでノード入力を差し替える。"""
    applied = {}
    for path, value in overrides.items():
        parts = str(path).split(".")
        cursor = workflow
        for part in parts[:-1]:
            cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
        leaf = parts[-1]
        if isinstance(cursor, list):
            cursor[int(leaf)] = value
        else:
            cursor[leaf] = value
        applied[path] = value
    return applied


def load_workflow(repo: Path, params: dict) -> dict:
    if isinstance(params.get("workflow"), dict):
        return json.loads(json.dumps(params["workflow"]))  # 呼び出し側を壊さないため複製

    rel = params.get("workflow_file")
    if not rel:
        raise ValueError("queue には workflow か workflow_file が必要")

    target = (repo / rel).resolve()
    if not str(target).startswith(str(repo.resolve())):
        raise ValueError(f"リポジトリ外は読めない: {rel}")
    if not target.is_file():
        raise ValueError(f"ワークフローが見つからない: {rel}")
    return json.loads(target.read_text(encoding="utf-8"))


def execute(client: ComfyClient, state: LiveState, repo: Path, cfg: dict,
            action: str, params: dict) -> dict:
    if action not in cfg["allowed_actions"]:
        raise ValueError(f"許可されていない action: {action}（config の allowed_actions を確認）")

    if action == "status":
        return build_snapshot(client, state, cfg)

    if action == "queue":
        workflow = load_workflow(repo, params)
        applied = apply_overrides(workflow, params.get("overrides") or {})
        result = client.prompt(workflow, client_id=state.client_id, extra_data=params.get("extra_data"))
        node_errors = (result or {}).get("node_errors") or {}
        if node_errors:
            raise ComfyError(f"ComfyUI がワークフローを拒否: {json.dumps(node_errors, ensure_ascii=False)[:1500]}")
        return {"prompt_id": (result or {}).get("prompt_id"),
                "number": (result or {}).get("number"),
                "overrides_applied": applied}

    if action == "interrupt":
        return client.interrupt()

    if action == "clear_queue":
        return client.clear_queue()

    if action == "delete":
        ids = params.get("prompt_ids") or ([params["prompt_id"]] if params.get("prompt_id") else [])
        if not ids:
            raise ValueError("delete には prompt_id か prompt_ids が必要")
        return client.delete_queued(ids)

    if action == "free":
        return client.free(bool(params.get("unload_models", True)), bool(params.get("free_memory", True)))

    if action == "history":
        if params.get("prompt_id"):
            return client.history(prompt_id=params["prompt_id"])
        return client.history(max_items=int(params.get("max_items", cfg["history_items"])))

    if action == "object_info":
        node_class = params.get("node_class")
        if node_class:
            return client.object_info(node_class)
        # 全ノードは数 MB になるので名前だけ返す
        return {"node_classes": sorted((client.object_info() or {}).keys())}

    raise ValueError(f"未知の action: {action}")


def process_commands(client: ComfyClient, state: LiveState, repo: Path, cfg: dict) -> list[str]:
    commands_dir = repo / "commands"
    results_dir = repo / "status" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    handled: list[str] = []
    for path in sorted(commands_dir.glob("*.json")):
        command_id = path.stem
        result_path = results_dir / f"{command_id}.json"
        if result_path.exists():
            continue  # 処理済み

        started = utcnow()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            action = payload["action"]
            params = payload.get("params") or {}
        except Exception as exc:
            record = {"id": command_id, "ok": False, "received_at": started,
                      "finished_at": utcnow(), "error": f"命令の読み込みに失敗: {exc}"}
            result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            handled.append(command_id)
            continue

        log(f"実行: {command_id} action={action}")
        record = {"id": command_id, "action": action, "params": params, "received_at": started}
        try:
            record["result"] = execute(client, state, repo, cfg, action, params)
            record["ok"] = True
        except Exception as exc:
            record["ok"] = False
            record["error"] = str(exc)
            record["error_type"] = type(exc).__name__
            state.add_alert("command_failed", {"id": command_id, "action": action, "error": str(exc)})
            log(f"  -> 失敗: {exc}")
        record["finished_at"] = utcnow()

        result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        handled.append(command_id)

    return handled


# ----------------------------------------------------------------------
# 監視スナップショット
# ----------------------------------------------------------------------
def build_snapshot(client: ComfyClient, state: LiveState, cfg: dict) -> dict:
    snapshot: dict = {"checked_at": utcnow(), "comfy_url": cfg["comfy_url"], "live": state.snapshot()}

    reachable, error = client.ping()
    snapshot["comfy_reachable"] = reachable
    if not reachable:
        snapshot["error"] = error
        return snapshot

    try:
        stats = client.system_stats() or {}
        devices = []
        for dev in stats.get("devices") or []:
            total, free = dev.get("vram_total"), dev.get("vram_free")
            devices.append({
                "name": dev.get("name"),
                "type": dev.get("type"),
                "vram_total_mb": round(total / 1048576) if isinstance(total, (int, float)) else None,
                "vram_free_mb": round(free / 1048576) if isinstance(free, (int, float)) else None,
                "vram_used_pct": round((1 - free / total) * 100, 1)
                if isinstance(total, (int, float)) and isinstance(free, (int, float)) and total else None,
            })
        snapshot["devices"] = devices
        snapshot["comfyui_version"] = (stats.get("system") or {}).get("comfyui_version")
    except ComfyError as exc:
        snapshot["stats_error"] = str(exc)

    try:
        q = client.queue() or {}
        running = q.get("queue_running") or []
        pending = q.get("queue_pending") or []
        snapshot["queue"] = {
            "running": len(running),
            "pending": len(pending),
            # キュー要素は [number, prompt_id, prompt, extra_data, outputs] の配列
            "running_prompt_ids": [item[1] for item in running if isinstance(item, list) and len(item) > 1],
            "pending_prompt_ids": [item[1] for item in pending if isinstance(item, list) and len(item) > 1],
        }
    except ComfyError as exc:
        snapshot["queue_error"] = str(exc)

    try:
        history = client.history(max_items=int(cfg["history_items"])) or {}
        recent = []
        for prompt_id, entry in list(history.items())[-int(cfg["history_items"]):]:
            status = (entry or {}).get("status") or {}
            outputs = []
            for node_output in ((entry or {}).get("outputs") or {}).values():
                for image in (node_output or {}).get("images") or []:
                    outputs.append({"filename": image.get("filename"),
                                    "subfolder": image.get("subfolder"),
                                    "type": image.get("type")})
            recent.append({
                "prompt_id": prompt_id,
                "status": status.get("status_str"),
                "completed": status.get("completed"),
                "outputs": outputs[:20],
            })
        snapshot["recent"] = recent
    except ComfyError as exc:
        snapshot["history_error"] = str(exc)

    return snapshot


def detect_stall(snapshot: dict, cfg: dict) -> dict | None:
    """実行中なのに一定時間まったく進んでいない状態を検出する。"""
    queue = snapshot.get("queue") or {}
    if not queue.get("running"):
        return None
    live = snapshot.get("live") or {}
    if not live.get("websocket_connected"):
        return None  # WS が無いと進捗の判定材料が無いので誤検知させない
    idle = live.get("seconds_since_progress") or 0
    if idle < float(cfg["stall_seconds"]):
        return None
    return {
        "prompt_id": live.get("prompt_id"),
        "node": live.get("node"),
        "seconds_since_progress": idle,
        "threshold": cfg["stall_seconds"],
    }


def write_status(repo: Path, snapshot: dict, state: LiveState, cfg: dict) -> None:
    status_dir = repo / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    new_alerts = state.drain_alerts()
    stall = detect_stall(snapshot, cfg)
    if stall:
        new_alerts.append({"at": utcnow(), "kind": "stalled", "detail": stall})
    if not snapshot.get("comfy_reachable"):
        new_alerts.append({"at": utcnow(), "kind": "unreachable",
                           "detail": {"error": snapshot.get("error")}})
    if not new_alerts:
        return

    alerts_path = status_dir / "alerts.json"
    existing = []
    if alerts_path.exists():
        try:
            existing = json.loads(alerts_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    combined = (existing + new_alerts)[-int(cfg["max_alerts"]):]
    alerts_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for alert in new_alerts:
        log(f"ALERT {alert['kind']}: {json.dumps(alert['detail'], ensure_ascii=False)[:300]}")


# ----------------------------------------------------------------------
# エントリポイント
# ----------------------------------------------------------------------
def load_config(path: Path | None) -> dict:
    cfg = dict(DEFAULTS)
    if path:
        if not path.is_file():
            raise SystemExit(f"設定ファイルが無い: {path}")
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub 経由の ComfyUI 監視・操作エージェント")
    parser.add_argument("--config", type=Path, default=None, help="設定 JSON（省略時は既定値）")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="バスとして使うリポジトリのパス")
    parser.add_argument("--once", action="store_true", help="1 周だけ回して終了する")
    parser.add_argument("--no-git", action="store_true", help="git の同期をせずローカルだけで動かす")
    args = parser.parse_args()

    cfg = load_config(args.config)
    repo = args.repo.resolve()

    if shutil.which("git") is None and not args.no_git:
        raise SystemExit("git が PATH に無い")
    if not (repo / ".git").exists() and not args.no_git:
        raise SystemExit(f"git リポジトリではない: {repo}")

    (repo / "commands").mkdir(parents=True, exist_ok=True)
    (repo / "status" / "results").mkdir(parents=True, exist_ok=True)

    client = ComfyClient(cfg["comfy_url"])
    state = LiveState()
    stop = threading.Event()

    if cfg.get("enable_websocket", True):
        threading.Thread(target=websocket_loop, args=(cfg, state, stop), daemon=True).start()

    log(f"起動: repo={repo} branch={cfg['branch']} comfy={cfg['comfy_url']} 間隔={cfg['poll_interval']}s")
    reachable, error = client.ping()
    log(f"ComfyUI 到達性: {'OK' if reachable else 'NG -> ' + str(error)}")

    try:
        while True:
            cycle_started = time.time()
            try:
                if not args.no_git:
                    git_sync(repo, cfg)

                handled = process_commands(client, state, repo, cfg)
                snapshot = build_snapshot(client, state, cfg)
                write_status(repo, snapshot, state, cfg)

                if not args.no_git:
                    summary = f"comfy-agent: {len(handled)} 件処理" if handled else "comfy-agent: 状態更新"
                    git_publish(repo, cfg, f"{summary} ({utcnow()})")
            except Exception:
                log("ループ中に例外:")
                traceback.print_exc()

            if args.once:
                return 0

            elapsed = time.time() - cycle_started
            stop.wait(max(1.0, float(cfg["poll_interval"]) - elapsed))
    except KeyboardInterrupt:
        log("停止")
        return 0
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
