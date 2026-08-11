# comfy-bus — GitHub 経由で ComfyUI を監視・操作する

PC で動いている ComfyUI を、**PC を外部に一切公開せずに**、離れた場所から監視して指示を出すための仕組み。
GitHub リポジトリをメッセージバスとして使う。

```
指示する側  ──push──▶  commands/*.json  ──┐
                                          │  GitHub
状態を見る  ◀──pull──  status/*.json   ◀──┘
                                          ▲
                            PC 常駐エージェントが
                            pull → ComfyUI API 実行 → push
```

ComfyUI 側に着信ポートは開かない。エージェントが外向きに git を叩くだけなので、
トンネルもポート開放も不要で、ルータ設定にも触らない。

## なぜこの形か

Claude Code on the web はクラウドのコンテナで動くので、あなたの PC の `127.0.0.1:8188` には届かない。
[Remote Control](https://code.claude.com/docs/en/remote-control) を使えば PC 上の Claude Code を直接動かせるが、
[セッション間メッセージ](https://code.claude.com/docs/en/cross-session-messaging)は macOS / Linux 限定で、
ネイティブ Windows では使えない。この方式は git だけが前提なので **OS を問わない**。

## セットアップ

### 1. PC にこのリポジトリを clone

```bash
git clone https://github.com/pww3wwq/-.git comfy-bus
cd comfy-bus
git checkout claude/test-functionality-2fkj05
```

push できる認証（`gh auth login` か credential helper）が要る。

### 2. 設定

```bash
cp comfy-agent/config.example.json comfy-agent/config.json
```

`comfy_url` が実際の ComfyUI と一致しているか確認する。既定は `http://127.0.0.1:8188`。
`--listen` 付きで別ポートで動かしているなら、そこに合わせる。

### 3. ライブ進捗（任意だが推奨）

```bash
pip install websocket-client
```

入れると WebSocket で進捗（何ノード目・何 %）を拾い、**ハング検知**が有効になる。
無くても動くが、その場合は進捗が取れないのでハング検知は無効化される（誤検知させないため）。

### 4. 起動

```bash
python3 comfy-agent/agent.py --config comfy-agent/config.json
```

落ちたら終わりなので、長時間回すなら `tmux` / `screen`、Windows ならタスクスケジューラや
別ウィンドウで常駐させる。

動作確認だけなら:

```bash
python3 comfy-agent/agent.py --once --no-git    # git に触らず 1 周だけ
```

## 使い方

`commands/` に JSON を置いて push すると、次のポーリング（既定 20 秒）で実行され、
結果が `status/results/` に、状態が `status/snapshot.json` に push で返ってくる。

形式と action の一覧は [`commands/README.md`](commands/README.md)、
返ってくる内容は [`status/README.md`](status/README.md) を参照。

```json
{
  "action": "queue",
  "params": {
    "workflow_file": "workflows/example-txt2img.json",
    "overrides": { "6.inputs.text": "a red sports car at night", "3.inputs.seed": 987654 }
  }
}
```

## 監視でわかること

- ComfyUI が生きているか、落ちたか
- 実行中／待機中のキュー本数
- VRAM の空きと使用率
- 実行中のノードと進捗（WebSocket 有効時）
- **ハング検知** — 実行中なのに `stall_seconds`（既定 300 秒）進捗が無い
- ノードが投げた例外（`exception_message` と traceback）
- 完了ジョブと出力ファイル名

## 設定項目

| キー | 既定 | 意味 |
|---|---|---|
| `comfy_url` | `http://127.0.0.1:8188` | ComfyUI のアドレス |
| `branch` | `claude/test-functionality-2fkj05` | バスに使うブランチ |
| `poll_interval` | `20` | ポーリング間隔（秒）。ここが指示の遅延になる |
| `stall_seconds` | `300` | この秒数進捗が無ければハングとみなす |
| `history_items` | `5` | スナップショットに含める直近履歴の件数 |
| `max_alerts` | `50` | `alerts.json` に残す件数 |
| `enable_websocket` | `true` | ライブ進捗を使うか |
| `push` | `true` | `false` にすると commit だけして push しない |
| `allowed_actions` | 8 種 | 実行を許す action。ここから外せばその操作は拒否される |

## 安全側の作り

- **ComfyUI の API しか叩かない。** 任意コマンド実行の口は無い。リポジトリに書き込める者が
  PC 上で好きなコードを走らせられる、という状態を作らないため。
- **`allowed_actions` で操作を絞れる。** 監視だけにしたいなら `["status", "history"]` にする。
- **`workflow_file` はリポジトリ内に限定。** `../` で外へ抜けようとすると拒否される。
- **`push: false`** で、外に何も出さず手元で挙動だけ確認できる。

なお、このリポジトリが public だとワークフローの内容やファイル名が公開される。
private を推奨。

## 制約

- 指示の反映は最短でも `poll_interval` 分だけ遅れる。リアルタイム制御用ではない。
- 生成画像そのものは push しない。ファイル名だけ返るので、実体は PC 側で `/view` から取る。
- エージェントのプロセスが落ちれば当然止まる。`snapshot.json` の `checked_at` が
  更新されていなければ、エージェント自体が死んでいる。
