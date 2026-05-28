# comfyui-explore-gallery

`output` 直下の各フォルダ（explore / selected / Finished / HQ …）と画像をブラウザで一覧・拡大表示し、
選別（selected へ移動）・削除・ダウンロードができる ComfyUI 拡張です。
ComfyUI 本体（8188）のサーバに間借りするので、追加ポート・Jupyter・proxy 設定は不要。

T2I 2分割ワークフロー（`T2I_HQ_A_explore` → `T2I_HQ_B_brushup`）の選別工程用。

## 機能

- **フォルダ切替**: `output` ルート＋全サブフォルダをタブで切替（各タブに画像枚数表示）
- **メタ表示**: 各画像にファイル名・解像度・サイズ・作成日時（mtime）を表示
- **拡大表示**: サムネをダブルクリック（または左上の 🔍）で全解像度プレビュー。拡大画像をクリックで一覧へ戻る
  - キー操作: `←` / `→` 送り、`↑` 選択トグル、`↓` ゴミ箱（`_trash`）へ、`Esc` 閉じる（凡例を画面下部に薄く表示）
  - 画像右上にアイコン（選択 `✓` / ダウンロード `⬇` / 閉じる `✕`）
  - 画像下に**フィルムストリップ**（フォルダ内の前後サムネ・現在は太枠）と「N / 総数」を表示。サムネクリックでジャンプ
- **選別移動**: クリックで選択 → 「→ selectedへ移動」で `output/selected` へ
- **ゴミ箱**: 選択画像を `output/_trash` へ移動（復元可能。`_trash` はタブ非表示）
- **ダウンロード**: 選択1枚はそのまま、複数は ZIP。「フォルダZIP」で表示中フォルダを丸ごと ZIP

## しくみ（エンドポイント）

- `GET  /explore_gallery` … ギャラリー画面（HTML）
- `GET  /explore_gallery/dirs` … `output` 直下のフォルダ一覧（枚数付き）
- `GET  /explore_gallery/list?dir=SUB` … 指定フォルダの画像一覧（名前/解像度/サイズ/日時、新しい順）
- `POST /explore_gallery/move` … `{dir, files}` を `output/selected` へ移動
- `POST /explore_gallery/trash` … `{dir, files}` を `output/_trash` へ移動（復元可能）
- `GET  /explore_gallery/download?dir=SUB&file=NAME` … 単一ファイルDL
- `POST /explore_gallery/zip` … `{dir, files}`（空配列ならフォルダ全体）を ZIP でDL

出力フォルダは `folder_paths.get_output_directory()` で解決するため、ローカル ComfyUI でも
RunPod（`/workspace/runpod-slim/ComfyUI/output`）でも、そのまま動きます。
画像配信は ComfyUI 既存の `/view` を再利用（`preview=webp` で軽量サムネ）。
解像度の取得には Pillow を使用（ComfyUI に同梱）、結果は mtime+サイズでキャッシュ。

## デプロイ（RunPod）

1. このフォルダを RunPod の ComfyUI 配下へ配置:
   ```
   /workspace/runpod-slim/ComfyUI/custom_nodes/comfyui-explore-gallery/
   ```
   （git 同期しているならそのまま。していなければ scp / コピーで配置）
2. **ComfyUI を1回だけ再起動**（拡張ロードのため。以後は常時有効・追加操作不要）。
3. ブラウザで開く:
   ```
   https://[podid]-8188.proxy.runpod.net/explore_gallery
   ```

## 使い方

1. 上部タブで見たいフォルダを選ぶ。
2. 良い画像をクリックで選択（✓ が付く）。`全選択 / 全解除` も可。ダブルクリックで拡大確認。
3. 「→ selectedへ移動」で選別、「🗑 ゴミ箱へ」で破棄、「⬇ 選択をDL / フォルダZIP」で取得。
   拡大中なら `↑` 選択 / `↓` ゴミ箱で手早く処理できる。
4. `T2I_HQ_B_brushup` を Queue（incremental_image が1枚ずつ消化）。

## 安全策

- 受け取ったファイル名は `os.path.basename()` に正規化、フォルダ指定も `output` 配下に限定（`../` 等は拒否）。
- 画像拡張子（png/jpg/jpeg/webp/bmp/gif）のみ対象。`.`/`_` 始まりのフォルダはタブに出さない。
- `selected` / `_trash` に同名がある場合は `_1`, `_2`… の連番を付けて衝突回避。
- 削除は完全削除ではなく `output/_trash` への移動（復元可能）。溜まったら OS のファイラ等で空にする。
