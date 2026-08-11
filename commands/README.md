# commands/ — 受信箱

ここに置かれた `*.json` を、PC 上の `comfy-agent` が拾って ComfyUI に対して実行する。
ファイル名（拡張子を除いた部分）が命令 ID になり、結果は `status/results/<ID>.json` に出る。
すでに結果ファイルがある命令は二度実行されない。

ファイル名は `YYYYMMDD-HHMMSS-<なにをするか>.json` のように、時刻順に並ぶ形を推奨。

## 形式

```json
{
  "action": "queue",
  "params": { }
}
```

## action 一覧

| action | params | 効果 |
|---|---|---|
| `status` | なし | VRAM・キュー・直近履歴のスナップショットを取る |
| `queue` | `workflow_file` または `workflow`、任意で `overrides` / `extra_data` | ワークフローをキューに投入 |
| `interrupt` | なし | 実行中のジョブを中断（待機中は残る） |
| `clear_queue` | なし | 待機中のキューを全消し（実行中は止まらない） |
| `delete` | `prompt_id` または `prompt_ids` | 指定ジョブをキューから削除 |
| `free` | `unload_models` / `free_memory`（既定 true） | モデルをアンロードして VRAM を解放 |
| `history` | `prompt_id` または `max_items` | 実行履歴と出力ファイル名を取得 |
| `object_info` | `node_class`（省略時はクラス名一覧のみ） | ノード定義を取得 |

`allowed_actions` に無い action は実行されず、拒否として結果に記録される。

## 例

ワークフローを投入し、プロンプトとシードだけ差し替える:

```json
{
  "action": "queue",
  "params": {
    "workflow_file": "workflows/example-txt2img.json",
    "overrides": {
      "6.inputs.text": "a red sports car at night, cinematic",
      "3.inputs.seed": 987654,
      "3.inputs.steps": 30
    }
  }
}
```

`overrides` のキーは API 形式ワークフローに対するドット区切りパス（`<ノードID>.inputs.<入力名>`）。
存在しないパスを指定した場合はエラーとして結果に残り、ワークフローは投入されない。

止まっているジョブを蹴る:

```json
{ "action": "interrupt", "params": {} }
```

VRAM を空ける:

```json
{ "action": "free", "params": { "unload_models": true, "free_memory": true } }
```

## ワークフローの用意

ComfyUI の Web UI で **Settings → Enable Dev mode options** を有効にすると
**Save (API Format)** が出る。これで保存した JSON が `/prompt` に投げられる形式で、
`workflows/` に置いて `workflow_file` から参照する。

通常保存した JSON（UI 形式）はノードの座標などを含む別物で、そのままでは投入できない。
