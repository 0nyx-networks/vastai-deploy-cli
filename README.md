# vastai-deploy-cli

Vast.ai の Pod へコンテナを **1コマンドでデプロイ + 後始末** までやる CLI。

- **Tailscale auth-key を都度発行** (ephemeral / 1度きり / 10分有効)
- **同名 Offline デバイスを事前削除** → Tailscale 上の重複名衝突を回避
- **GPU / 国 / VRAM / CUDA / 価格** で自動オファー検索 (国に優先順位あり、JP→US 等)
- **登録待機** (`--wait` / 300秒タイムアウト) → 成功で auth-key を自動 revoke
- **destroy 時に Vast.ai 消滅確認 + Tailscale デバイス削除**
- **ラベル指名** (`ollama` / `comfyui` 等) でも instance id でも操作可
- **denylist** で「もう借りないホスト」を永続除外

## 構成

```
app/
  config.py     - 設定 / プロファイル読み込み (pydantic Settings)
  vast.py       - Vast.ai SDK ラッパー (vastai-sdk)
  tailscale.py  - Tailscale REST クライアント (auth-key / device 操作)
  deploy.py     - 一連の処理オーケストレーション
  denylist.py   - machine_id / host_id 永続除外リスト
  cli.py        - typer ベースの CLI
  api.py        - FastAPI HTTP ラッパー (POST /deploy, GET /instances, GET /healthz)
templates/
  ollama/       - profile.json (+ onstart.sh: ローカル)
  comfyui/      - profile.json (+ onstart.sh: ローカル)
  studio/       - profile.json (+ onstart.sh: ローカル)
denylist.json   - 永続除外マシン (gitignore 対象)
.env            - API キー (gitignore 対象)
```

## セットアップ

```bash
cd /opt/vastai-deploy-cli
pip install -e .

cp .env.example .env
# VAST_API_KEY / TAILSCALE_API_KEY / TAILSCALE_TAILNET を埋める
```

`pip install -e .` で **`vastai-cli`** コマンドが PATH に登録されます。以下のドキュメントは
すべて `vastai-cli` 形式で記述しますが、`python -m app` でも同じです (補完だけ効きません)。

### bash 補完 (任意)

```bash
vastai-cli --install-completion bash
exec bash    # 既存のシェルにも反映
```

これで `vastai-cli <Tab>` でサブコマンド、`vastai-cli deploy --<Tab>` でフラグが補完されます。
zsh / fish / powershell の場合は `--install-completion zsh` 等。

### Tailscale 側の準備

1. <https://login.tailscale.com/admin/settings/keys> で **API access token (PAT)** を発行
2. ACL に使用するタグを `tagOwners` として登録
   ```json
   "tagOwners": { "tag:cloud-gpu-pods": ["autogroup:admin"] }
   ```

### Vast.ai 側の準備

1. <https://cloud.vast.ai/account/> で API key を発行
2. (任意) `SSH_PUBLIC_KEY` を `.env` に入れると Vast.ai インスタンスへ SSH ログイン可
   (create 後に `attach_ssh` で個別付与される)

---

## デプロイ

> **既定は実行** (deploy)。計画のみ確認したいときは `--dry-run` を付ける。
> Tailscale への登録確認は **デフォルト off** (`--no-wait`)。`--wait` を付けると online 化まで最大 300秒ブロック。

```bash
# 本実行 (デフォルト)
vastai-cli deploy ollama
vastai-cli deploy comfyui -v

# 計画のみ表示 (dry-run)
vastai-cli deploy ollama --dry-run

# Tailscale 登録まで待機
vastai-cli deploy ollama --wait

# 名前 (label / Tailscale hostname) を上書き
vastai-cli deploy ollama --name ollama-dev

# 既知の offer_id を指名 (検索スキップ)
vastai-cli deploy ollama --offer-id 12345678

# 待機タイムアウトを延長 (image pull が大きいイメージ向け)
vastai-cli deploy comfyui --wait --wait-timeout 600
```

### 内部フロー

1. `name` を DNS-safe に正規化 (省略時は `profile.env.TAILSCALE_HOSTNAME`、それも無ければ `vast-<target>-<unix>`)
2. **Pre-flight**: Vast.ai 上に **同 label のインスタンスが既にあれば即エラー** (二重課金回避)
3. Tailscale で同名 **Offline** デバイスを削除
4. ephemeral / preauthorized / non-reusable な auth-key を発行 (`expirySeconds=600`)
5. profile の `search.*` で `gpu_name`/`geolocation` 検索 → クライアント側で `cuda_min`/`gpu_ram_gb_min`/`max_dph`/denylist の最終確認 → 国優先度+価格でランク付け
6. 上位10候補に対し `create_instance` を試行 (`no_such_ask` 等は次候補へ自動フォールバック)
7. `attach_ssh` (`SSH_PUBLIC_KEY` セット時のみ)
8. **Tailscale 登録待機** (5秒ポーリング)。成功で auth-key を即 revoke

---

## インスタンス管理

`<target>` には **数字 (instance id)** または **label** を渡せる。
profile.json の `TAILSCALE_HOSTNAME` がそのまま label になっているので、デフォルトでは
`ollama` `comfyui` で操作できる。

```bash
vastai-cli instances                  # 全インスタンス一覧 (id/label/status/gpu/image/dph)
vastai-cli status                     # 全インスタンス詳細一覧 (machine_id/host_id 含む)
vastai-cli status ollama              # 単体詳細
vastai-cli status 36412345            # id でも可

vastai-cli logs ollama                # 直近100行
vastai-cli logs ollama --tail 500     # 行数指定 (--tail / -n)
vastai-cli logs ollama -f             # tail -f 風 (Ctrl-C で停止)
vastai-cli logs ollama -f --interval 5   # ポーリング間隔を変更 (デフォルト3秒)
vastai-cli logs ollama --daemon       # コンテナ daemon 側

vastai-cli stop ollama                # 停止 (storage 課金は継続)
vastai-cli start ollama               # 再開

vastai-cli destroy ollama             # 確認プロンプトあり (default N)
vastai-cli destroy ollama -y          # 確認スキップ
vastai-cli destroy ollama --denylist --note "OOM多発"   # 破棄 + 永続除外
vastai-cli destroy ollama --skip-tailscale   # Tailscale 削除をスキップ
```

`destroy` の処理順:

1. label / machine_id を捕捉
2. Vast.ai destroy
3. **`list_instances` をポーリングして消滅を確認** (デフォルト120秒)
4. 同名 Tailscale デバイスを **online/offline 問わず** 全削除
5. (`--denylist` 時) `denylist.json` に machine_id / host_id を追記

---

## denylist

「もう借りないホスト」を `denylist.json` で永続管理。`filter_offers` の最初に弾く。

```bash
vastai-cli denylist show
vastai-cli denylist add --machine-id 79957 --note "ネット不安定"
vastai-cli denylist add --host-id 12345 --note "別ホスト除外"
vastai-cli denylist remove --machine-id 79957
vastai-cli denylist remove --host-id 12345
```

`destroy --denylist` でも自動追加される。

---

## 検索条件のプレビュー / 診断

```bash
vastai-cli offers ollama --limit 20    # API + クライアント側フィルタ後の候補
vastai-cli ts-find ollama              # Tailscale 上の同名/類似デバイスを精査
vastai-cli ts-purge ollama             # 同名の Offline デバイスを手動掃除
```

dry-run の出力には **上位5候補** (`top_candidates`) が含まれるので、選定が妥当か確認できる。
client-side filter で全部弾かれた場合は `rejects` に **どの条件で何件落としたか** が出る。

---

## HTTP API

```bash
vastai-cli serve --host 127.0.0.1 --port 8000

# デプロイ
curl -X POST localhost:8000/deploy \
  -H 'content-type: application/json' \
  -d '{"target":"ollama","wait_for_tailscale":true,"wait_timeout":300}'

# インスタンス一覧
curl localhost:8000/instances

# ヘルスチェック
curl localhost:8000/healthz
```

---

## プロファイルのカスタマイズ

`templates/<target>/profile.json` の主な項目:

```jsonc
{
  "image": "m10i1986/ollama-running-on-gpupods:latest",  // 好きな Docker image (Tailscale 内包想定)
  "disk_gb": 32,                                          // /workspace 含むディスク
  "onstart_script": "_shared/onstart.sh",                 // ファイルパス (templates 配下)
  "runtype": "ssh",
  "env": {
    "TAILSCALE_HOSTNAME": "ollama",     // ← Tailscale 上の名前 + Vast.ai label
    "TAILSCALE_TAG": "cloud-gpu-pods",  // ← prefix `tag:` は自動付与
    "NUMBER_OF_GPUS": 1                 // ← 数値も可 (str に自動変換される)
    // ここに任意のアプリ env を追加可。
    // TAILSCALE_AUTHKEY は deploy 時に自動注入される。
  },
  "search": {
    "gpu_name": ["RTX 5090"],            // OR 検索 (「RTX 4090」も許可したいなら追記)
    "gpu_ram_gb_min": 32,
    "num_gpus": 1,
    "disk_gb_min": 32,
    "cuda_min": 12.8,                     // ← Vast.ai の cuda_max_good >= cuda_min
    "rentable": true,
    "verified": true,
    "max_dph": 0.6,                       // 上限 USD/hour
    "order": "dph_total",
    "geolocation": ["JP", "US"]           // 優先順位 (JP 在庫優先 → US フォールバック)
  }
}
```

- 追加ターゲットを作る場合は同じ構造の `profile.json` と `onstart.sh` を置けば `vastai-cli deploy <new-name>` で利用可能
- `onstart.sh` は **イメージ内 `entrypoint.sh` を実行する3行スクリプト** で十分 (Tailscale 起動はイメージ側で `TAILSCALE_AUTHKEY` を読む前提)
- `templates/*/onstart.sh` は **gitignore 対象** (環境固有のローカルカスタマイズを許容するため)

---

## 注意

- **ephemeral auth-key**: Vast.ai インスタンス停止 = Tailscale ノード自動削除。`stop` → `start` の再接続では `tailscaled` 側が node-key を保持しているので auth-key 不要 (= 接続維持)
- Vast.ai のコンテナで `/dev/net/tun` が無いケースが多いため、イメージ側は **`tailscaled --tun=userspace-networking`** で起動する想定
- `--wait` 付きで実行時は **Tailscale 登録 online 化を 300秒待つ**。タイムアウトしてもインスタンスは destroy されないので、`destroy` で手動回収すること
- `destroy` 時の確認プロンプトは `-y` で抑止可。CI / スクリプト用途で利用
