"""ComfyUI の HTTP API を叩く最小クライアント（標準ライブラリのみ）。

依存を足さないのが方針。ComfyUI に同梱の Python でそのまま動く。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ComfyError(RuntimeError):
    """ComfyUI への到達失敗、または API がエラーを返した。"""


class ComfyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # ローカル宛てなので、環境変数のプロキシ設定は明示的に無視する。
        # HTTP_PROXY が刺さっている PC で 127.0.0.1 が proxy に流れる事故を防ぐ。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------
    # 低レベル
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, body=None, raw: bool = False):
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise ComfyError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"{method} {path} -> 到達不可: {exc.reason}") from exc
        except OSError as exc:  # タイムアウトなど
            raise ComfyError(f"{method} {path} -> {exc}") from exc

        if raw:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": payload.decode("utf-8", "replace")[:2000]}

    # ------------------------------------------------------------------
    # 監視系
    # ------------------------------------------------------------------
    def system_stats(self):
        """VRAM / RAM / デバイス情報。ComfyUI の生存確認も兼ねる。"""
        return self._request("GET", "/system_stats")

    def queue(self):
        """実行中 (queue_running) と待機中 (queue_pending)。"""
        return self._request("GET", "/queue")

    def history(self, prompt_id: str | None = None, max_items: int | None = None):
        if prompt_id:
            return self._request("GET", f"/history/{urllib.parse.quote(prompt_id)}")
        path = "/history"
        if max_items:
            path += f"?max_items={int(max_items)}"
        return self._request("GET", path)

    def object_info(self, node_class: str | None = None):
        """ノード定義。node_class を省くと全ノード（巨大）。"""
        if node_class:
            return self._request("GET", f"/object_info/{urllib.parse.quote(node_class)}")
        return self._request("GET", "/object_info")

    # ------------------------------------------------------------------
    # 操作系
    # ------------------------------------------------------------------
    def prompt(self, workflow: dict, client_id: str | None = None, extra_data: dict | None = None):
        """API 形式のワークフローをキューに投入する。"""
        body: dict = {"prompt": workflow}
        if client_id:
            body["client_id"] = client_id
        if extra_data:
            body["extra_data"] = extra_data
        return self._request("POST", "/prompt", body)

    def interrupt(self):
        """実行中のジョブを中断する（キューの待機分は残る）。"""
        self._request("POST", "/interrupt", {})
        return {"interrupted": True}

    def clear_queue(self):
        """待機中のキューを全消しする（実行中は止まらない）。"""
        self._request("POST", "/queue", {"clear": True})
        return {"cleared": True}

    def delete_queued(self, prompt_ids: list[str]):
        self._request("POST", "/queue", {"delete": list(prompt_ids)})
        return {"deleted": list(prompt_ids)}

    def free(self, unload_models: bool = True, free_memory: bool = True):
        """モデルをアンロードして VRAM を解放する。"""
        self._request("POST", "/free", {"unload_models": unload_models, "free_memory": free_memory})
        return {"unload_models": unload_models, "free_memory": free_memory}

    # ------------------------------------------------------------------
    # 生成物
    # ------------------------------------------------------------------
    def view_url(self, filename: str, subfolder: str = "", folder_type: str = "output") -> str:
        params = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": folder_type}
        )
        return f"{self.base_url}/view?{params}"

    def ping(self) -> tuple[bool, str | None]:
        """(到達可否, エラー文字列)。"""
        try:
            self.system_stats()
            return True, None
        except ComfyError as exc:
            return False, str(exc)
