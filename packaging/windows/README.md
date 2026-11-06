# Windows アプリのビルド

Windows 10 / 11 の64bit版と、Python公式配布の Python 3.13.18（Tkを含む）でビルドします。利用者に Python / uv のインストールは不要です。

開発環境の依存パッケージと PyInstaller を用意して、次を実行します。

```powershell
uv sync --locked --group packaging --no-install-project --python 3.13.18
node scripts/build_userscript.mjs
uv run --no-sync python -m plamo_cp.llama_backend --output .local/windows-runtime/llama
uv run --no-sync python -m scripts.build_windows_app
```

Inno Setup 6 が入っている開発用Windowsでは、`uv run --no-sync python -m scripts.build_windows_app --installer` で `PLaMo-for-CP-Windows-Setup.exe` も生成します。一般の利用者にはこのインストーラーを渡す構成です。ユーザー領域へのインストールなので、管理者権限を要求しません。GitHub Actions のWindowsランナーには [Inno Setup が含まれています](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md)。CIの定義は追加済みで、実行はしていません。

uvは必要に応じて `python -m pip install --require-hashes --no-deps --only-binary=:all: -r packaging/uv-bootstrap.txt` で固定版を導入できます。実行・ビルド依存はuv.lockのpackagingグループまで固定します。

ランタイム準備コマンドは、固定した公式 llama.cpp の実行ファイルとDLLを取得し、SHA-256を検証します。このランタイムと検証元の固定ZIPを配布物に同梱します。実行前にはZIPの固定ハッシュと展開後の全ファイルを照合し、書き換えられたサイドカーだけでは信頼しません。モデルの重みは同梱しません。依存のライセンスと出所一覧は `_internal/third_party_licenses/INDEX.json` 以下に保存し、公式ランタイム内の文書も保持します。静的リンクされたC++依存の網羅性はバイナリ公開前の追加確認が必要です。

アイコンは保存済みの `packaging/icons/app-icon.ico` を実行ファイル、Tkウィンドウ、インストーラーで使います。Windowsでのビルド時にMacの変換ツールや画像ライブラリは必要ありません。原画を変更する場合の変換はmacOS側の `scripts/build_app_icons.py` で行い、生成済みICOも一緒に保存します。

`dist/windows/PLaMo-for-CP-Windows.zip` を展開し、フォルダー内の `PLaMo for CP.exe` をダブルクリックします。GUIが翻訳サーバーを起動し、ブラウザーで管理画面を開きます。モデルの取得・使用・解除・削除、保存先の選択、訳文キャッシュの確認や変更ができます。利用者は明示的なボタンで初回モデル取得を開始し、Tampermonkeyスクリプトを追加します。標準出力と標準エラーは常時回収し、各ログは現在分と1バックアップ、各128KiB、1種類あたり256KiBに制限します。

このビルド定義はWindowsで実行する必要があります。Mac上でWindows実行ファイルを生成・動作検証したものではありません。一般公開する場合は、Windows実機で初回取得・翻訳・終了を検証し、配布元署名を行ってください。
