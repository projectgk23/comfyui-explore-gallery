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

1. 上部タブで見たいフォルダを選ぶ（中身のあるフォルダのみ表示。空フォルダや `_trash` は「非表示フォルダ ▾」から開ける）。
2. 画像を**クリックで選択**（緑枠が付く）。**ドラッグで矩形範囲の一括選択**も可（Shift / Ctrl を押しながらで追加選択）。`全選択 / 全解除` も可。サムネ左上に解像度・アスペクト比、左下に **📋（WFコピー）**、右下に**拡大アイコン**。
3. 選択中だけ出る**アクションバー**で、`移動先` を選んで「→ 移動」（任意フォルダ／新規フォルダ可）、「⬇ DL」、「🗑 ゴミ箱へ」。
   `_trash` を開いているときはアクションバーが「↩ 復元（移動先を選んで戻す）」「❌ 完全削除」に変わる。
4. ファイル名検索は**全フォルダ横断**（`_trash`・非表示フォルダは対象外）。「**メタも検索**」を ON にすると、プロンプト・モデル名・シードなど埋め込み情報も対象になる（やや時間がかかる）。`_trash` 内を検索したいときは、非表示フォルダから `_trash` を開いてから検索する（開いているフォルダ内検索になる）。
5. 「**自動更新**」を ON にすると、新着画像を検知して自動で取り込む（一覧の先頭付近にいるとき。拡大中やスクロール中は「新着があります」と通知）。
6. **表示**（小/中/大）でサムネサイズを切り替え（設定はブラウザに記憶）。
7. **ワークフローをコピー → ComfyUI 画面で Ctrl+V（貼り付け）** すると、その画像を生成したワークフローが読み込まれる。
8. **OS から画像をドラッグ＆ドロップ**すると、開いているフォルダにアップロードできる（RunPod 運用向け）。
9. `T2I_HQ_B_brushup` を Queue（incremental_image が1枚ずつ消化）。

## 機能

- **フォルダ切替**: 中身のある（1枚以上）フォルダだけをタブ表示（各タブに枚数）。空フォルダと `_trash` は「**非表示フォルダ ▾**」ドロップダウンから開ける。`output/.gallery_cache` はどちらにも出さない
- **検索（全フォルダ横断）**: ファイル名で全フォルダを横断検索（`_trash`・非表示フォルダは除外）。非表示フォルダ（`_trash` など）を開いている間は、そのフォルダ内のみの検索になる
- **並び替え**: 日時 / 名前 / サイズで昇順・降順ソート
- **高速サムネ**: WEBP サムネを `output/.gallery_cache/` に永続キャッシュ（初回のみ生成、以降は即表示）
- **段階描画**: スクロールに応じて少しずつ描画＆遅延読み込みするので、数百〜千枚規模でも軽い
- **メタ表示**: 各画像にファイル名・解像度・**アスペクト比**・作成日時（mtime）を表示
- **プロンプト表示／コピー**: 各カードにポジ（P）・ネガ（N）プロンプトを1行ずつ表示し、`⧉` ボタンでワンクリックコピー。値はカードが画面に入った時点で遅延取得（スクロール分のみ）
- **クリック / ドラッグで選択**: 画像クリックで選択（緑枠）。**ドラッグで矩形範囲を一括選択**（Shift/Ctrl で追加）。拡大はサムネ右下の**拡大アイコン**から
- **ドラッグ＆ドロップ アップロード**: OS から画像をページにドロップすると、開いているフォルダに保存（`/upload`）。RunPod などデスクトップのファイラが無い環境向け
- **サムネサイズ切替**: 小 / 中 / 大（ブラウザに記憶）
- **トースト通知**: 操作結果は右下にトーストで一時表示
- **任意フォルダへ移動**: アクションバーの `移動先` で既存フォルダや**新規フォルダ**を指定して移動（横断検索の結果はフォルダ別にまとめて移動）
- **ゴミ箱と復元/完全削除**: 通常は `output/_trash` へ移動（復元可能）。`_trash` を開くとアクションバーが「↩ 復元（任意フォルダへ戻す）」「❌ 完全削除（不可逆）」に切り替わる
- **メタ検索**: 「メタも検索」ON で、ファイル名に加えプロンプト・モデル名・シードなど PNG 埋め込み情報も横断検索
- **新着自動更新**: 「自動更新」ON で `/stat` を軽くポーリングし、新着を検知したら自動取り込み（先頭付近にいるとき）
- **ワークフローのコピー＆ペースト**: サムネ／拡大ビューの **📋** で、その画像のワークフローを共有 localStorage 経由でコピー。ComfyUI 画面で **Ctrl+V** すると `app.loadGraphData` で読み込まれる（`js/explore_gallery.js` を ComfyUI が読み込む）
- **選別クォータ（目安枚数）**: ツールバーの「目安」に枚数を入れると、ステータスが `選択 3 / 全 12 ・ 目安 5` のように出る。目安に達すると緑、超えると警告色（選択バーの枚数表示も連動）。**ブロックはしない**自制用の補助線。設定はブラウザに記憶
- **未判定のみ（フォーカスモード）**: 「未判定のみ」ON で採用（選択）済みをグリッドから隠し、まだ判定していない画像だけを表示。`残り N ・ 採用 M` 表示でゴールが見える。キープを選ぶたびに消えて「残り」が減る。OFF で選択済みも戻る
- **選別チェック観点パネル**: 「✅ チェック観点」（拡大表示中は `g` キー）で、指の破綻・四肢のねじれ・目の左右差など**アニメ選別の着眼点**をフロート表示。隅に出したまま選別できる
- **拡大表示**: 全解像度プレビュー。**ホイールでズーム / ドラッグでパン**（クリックでも2倍ズーム・トグル）。背景 / `✕` / `Esc` で閉じる
  - キー操作: `←` / `→` 送り、`↑` 選択トグル、`↓` ゴミ箱（`_trash` では完全削除）、`i` 情報パネル、`c` ワークフローコピー、`g` チェック観点、`Esc` 閉じる
  - 画像右上にアイコン（情報 `ℹ` / WFコピー `📋` / 選択 `✓` / ダウンロード `⬇` / 閉じる `✕`）
  - **情報パネル** `ℹ`: PNG に埋め込まれた生成情報（プロンプト・モデル・Steps/CFG/Sampler/Seed など）を表示。生 JSON（prompt / workflow）も折りたたみで確認可
  - 画像下に**フィルムストリップ**（フォルダ内の前後サムネ・現在は太枠）と「N / 総数」を表示。サムネクリックでジャンプ
- **ダウンロード**: 選択1枚はそのまま、複数は ZIP。「フォルダZIP」で表示中フォルダを丸ごと ZIP

## しくみ（エンドポイント）

- `GET  /explore_gallery` … ギャラリー画面（`web/index.html`）
- `GET  /explore_gallery/web/*` … フロント資産（`style.css` / `app.js`）
- `GET  /explore_gallery/dirs` … フォルダ一覧（`dirs`=表示、`hidden`=空フォルダ/_trash、各枚数付き）
- `GET  /explore_gallery/list?dir=SUB` … 指定フォルダの画像一覧（名前/解像度/サイズ/日時、新しい順）
- `GET  /explore_gallery/search?q=WORD[&meta=1]` … 表示フォルダ横断検索（`_trash`・非表示は除外。各 hit に `dir` 付き）。`meta=1` でファイル名に加え PNG 埋め込み情報も対象
- `GET  /explore_gallery/stat?dir=SUB` … フォルダの軽量フィンガープリント（画像枚数＋最新 mtime）。新着ポーリング用
- `GET  /explore_gallery/thumb?dir=SUB&file=NAME` … WEBP サムネ（永続キャッシュ。未生成なら生成、PIL 不可時は `/view` にフォールバック）
- `GET  /explore_gallery/meta?dir=SUB&file=NAME` … PNG 埋め込みメタ（抽出フィールド＋生 JSON）
- `GET  /explore_gallery/workflow?dir=SUB&file=NAME` … PNG 埋め込みの `workflow`（UI形式）/ `prompt`（API形式）を返す（コピー用）
- `POST /explore_gallery/move` … `{dir, files, dst}` を `output/<dst>` へ移動（`dst` 省略時は `selected`。無ければ作成）
- `POST /explore_gallery/trash` … `{dir, files}` を `output/_trash` へ移動（復元可能）
- `POST /explore_gallery/delete` … `{dir, files}` を完全削除（不可逆。主に `_trash` を空にする用）
- `POST /explore_gallery/upload?dir=SUB` … multipart の画像ファイル群を `output/<dir>` に保存（同名は自動リネーム）
- `GET  /explore_gallery/download?dir=SUB&file=NAME` … 単一ファイルDL
- `POST /explore_gallery/zip` … `{dir, files}`（空配列ならフォルダ全体）を ZIP でDL

出力フォルダは `folder_paths.get_output_directory()` で解決するため、ローカル ComfyUI でも
RunPod（`/workspace/runpod-slim/ComfyUI/output`）でも、そのまま動きます。
サムネは Pillow（ComfyUI 同梱）で生成した WEBP を `output/.gallery_cache/` にキャッシュし、
2 回目以降はディスクから即配信。フロントは `web/`（HTML/CSS/JS）に分離。

## 構成

```
comfyui-explore-gallery/
├── __init__.py   # ルート定義・サムネ生成・メタ/ワークフロー抽出
├── js/
│   └── explore_gallery.js   # ComfyUI 本体側に読み込まれる。Ctrl+V でギャラリーのワークフローを取り込む
└── web/                     # ギャラリー画面のフロント（/explore_gallery で配信）
    ├── index.html
    ├── style.css
    └── app.js
```

