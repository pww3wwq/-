# status/ — 送信箱

`comfy-agent` が書いて push する。人が編集する場所ではない。

| ファイル | 中身 |
|---|---|
| `snapshot.json` | 毎周期の状態。到達可否・VRAM・キュー・ライブ進捗・直近の出力 |
| `alerts.json` | 異常の履歴（実行エラー / ハング検知 / 到達不可 / 命令失敗）。最新 `max_alerts` 件 |
| `results/<ID>.json` | `commands/<ID>.json` の実行結果 |

## snapshot.json の読み方

```jsonc
{
  "checked_at": "…",
  "comfy_reachable": true,          // false なら ComfyUI が落ちているか URL 違い
  "live": {
    "websocket_connected": true,    // false だと進捗とハング検知が効かない
    "prompt_id": "…",
    "node": "13",                   // 実行中のノード ID
    "progress": { "value": 7, "max": 30 },
    "seconds_since_progress": 4.2   // これが伸び続けている＝止まっている
  },
  "devices": [{ "vram_free_mb": 7629, "vram_used_pct": 68.9 }],
  "queue": { "running": 1, "pending": 3 },
  "recent": [{ "prompt_id": "…", "status": "success", "outputs": [{ "filename": "…png" }] }]
}
```

## alerts.json の kind

| kind | 意味 |
|---|---|
| `execution_error` | ノードが例外を投げた。`exception_message` と traceback 末尾が入る |
| `stalled` | 実行中なのに `stall_seconds` の間まったく進捗が無い |
| `unreachable` | ComfyUI に繋がらない |
| `command_failed` | `commands/` の命令が失敗した |

生成された画像そのものはここには入らない（リポジトリを太らせるため）。
`recent[].outputs[].filename` を見て、必要なら PC 側で
`http://127.0.0.1:8188/view?filename=…&subfolder=…&type=output` から取る。
