# comfyui-explore-gallery

`output` 直下の各フォルダ（explore / selected / Finished / HQ …）と画像をブラウザで一覧・拡大表示し、
選別（selected へ移動）・削除・ダウンロードができる ComfyUI 拡張です。
ComfyUI 本体（8188）のサーバに間借りするので、追加ポート・Jupyter・proxy 設定は不要。

T2I 2分割ワークフロー（`T2I_HQ_A_explore` → `T2I_HQ_B_brushup`）の選別工程用。

## デプロイ

ComfyUI の `custom_nodes/` 配下に **clone するだけ**（フォルダのアップロード不要）:

```bash
cd /workspace/runpod-slim/ComfyUI/custom_nodes
git clone https://github.com/projectgk23/comfyui-explore-gallery.git
```

- **ComfyUI を1回だけ再起動**（拡張ロードのため。以後は常時有効・追加操作不要）。
- ブラウザで開く: `http://<host>:8188/explore_gallery`
  （RunPod なら `https://[podid]-8188.proxy.runpod.net/explore_gallery`）
- 更新は: `git -C comfyui-explore-gallery pull`

> RunPod では、セットアップスクリプト `runpod_setup_t2i_hq_AB.sh`（shared リポジトリ同梱）に
> この clone が組み込まれているので、それを実行すれば自動で配置される。
> Pod は ephemeral なので「Pod 作成 → スクリプト実行（clone 込み）→ ComfyUI 再起動」が基本フロー。

## 使い方

1. 上部タブで見たいフォルダを選ぶ。
2. 良い画像をクリックで選択（✓ が付く）。`全選択 / 全解除` も可。ダブルクリックで拡大確認。
3. 「→ selectedへ移動」で選別、「🗑 ゴミ箱へ」で破棄、「⬇ 選択をDL / フォルダZIP」で取得。
   拡大中なら `↑` 選択 / `↓` ゴミ箱で手早く処理できる。
4. `T2I_HQ_B_brushup` を Queue（incremental_image が1枚ずつ消化）。

## 機能

- **フォルダ切替**: `output` ルート＋全サブフォルダをタブで切替（各タブに画像枚数表示）
- **検索 / 並び替え**: ファイル名で絞り込み、日時 / 名前 / サイズで昇順・降順ソート
- **高速サムネ**: WEBP サムネを `output/.gallery_cache/` に永続キャッシュ（初回のみ生成、以降は即表示）。`output/.gallery_cache` はタブ非表示
- **段階描画**: スクロールに応じて少しずつ描画＆遅延読み込みするので、数百〜千枚規模でも軽い
- **メタ表示**: 各画像にファイル名・解像度・サイズ・作成日時（mtime）を表示
- **プロンプト表示／コピー**: 各カードにポジ（P）・ネガ（N）プロンプトを1行ずつ表示し、`⧉` ボタンでワンクリックコピー。値はカードが画面に入った時点で遅延取得（スクロール分のみ）
- **拡大表示**: サムネをダブルクリック（または左上の 🔍）で全解像度プレビュー。拡大画像をクリックで一覧へ戻る
  - キー操作: `←` / `→` 送り、`↑` 選択トグル、`↓` ゴミ箱（`_trash`）へ、`i` 情報パネル、`Esc` 閉じる（凡例を画面下部に薄く表示）
  - 画像右上にアイコン（情報 `ℹ` / 選択 `✓` / ダウンロード `⬇` / 閉じる `✕`）
  - **情報パネル** `ℹ`: PNG に埋め込まれた生成情報（プロンプト・モデル・Steps/CFG/Sampler/Seed など）を表示。生 JSON（prompt / workflow）も折りたたみで確認可
  - 画像下に**フィルムストリップ**（フォルダ内の前後サムネ・現在は太枠）と「N / 総数」を表示。サムネクリックでジャンプ
- **選別移動**: クリックで選択 → 「→ selectedへ移動」で `output/selected` へ
- **ゴミ箱**: 選択画像を `output/_trash` へ移動（復元可能。`_trash` はタブ非表示）
- **ダウンロード**: 選択1枚はそのまま、複数は ZIP。「フォルダZIP」で表示中フォルダを丸ごと ZIP

## しくみ（エンドポイント）

- `GET  /explore_gallery` … ギャラリー画面（`web/index.html`）
- `GET  /explore_gallery/web/*` … フロント資産（`style.css` / `app.js`）
- `GET  /explore_gallery/dirs` … `output` 直下のフォルダ一覧（枚数付き）
- `GET  /explore_gallery/list?dir=SUB` … 指定フォルダの画像一覧（名前/解像度/サイズ/日時、新しい順）
- `GET  /explore_gallery/thumb?dir=SUB&file=NAME` … WEBP サムネ（永続キャッシュ。未生成なら生成、PIL 不可時は `/view` にフォールバック）
- `GET  /explore_gallery/meta?dir=SUB&file=NAME` … PNG 埋め込みメタ（抽出フィールド＋生 JSON）
- `POST /explore_gallery/move` … `{dir, files}` を `output/selected` へ移動
- `POST /explore_gallery/trash` … `{dir, files}` を `output/_trash` へ移動（復元可能）
- `GET  /explore_gallery/download?dir=SUB&file=NAME` … 単一ファイルDL
- `POST /explore_gallery/zip` … `{dir, files}`（空配列ならフォルダ全体）を ZIP でDL

出力フォルダは `folder_paths.get_output_directory()` で解決するため、ローカル ComfyUI でも
RunPod（`/workspace/runpod-slim/ComfyUI/output`）でも、そのまま動きます。
サムネは Pillow（ComfyUI 同梱）で生成した WEBP を `output/.gallery_cache/` にキャッシュし、
2 回目以降はディスクから即配信。フロントは `web/`（HTML/CSS/JS）に分離。

## 構成

```
comfyui-explore-gallery/
├── __init__.py   # ルート定義・サムネ生成・メタ抽出
└── web/
    ├── index.html
    ├── style.css
    └── app.js
```

