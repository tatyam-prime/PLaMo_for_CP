# Windows アプリのビルド

Windows 10 / 11 のx64版と、Python公式配布の Python 3.13.18（Tkを含む）でビルドします。開発用マシンには CMake と Visual Studio C++ ビルドツールが必要ですが、利用者に Python / uv / コンパイラーのインストールは不要です。

開発環境の依存パッケージと PyInstaller を用意して、次を実行します。

```powershell
uv sync --locked --group packaging --no-install-project --python 3.13.18
node scripts/build_userscript.mjs
uv run --no-sync python -m plamo_cp.llama_backend --output .local/windows-runtime/llama
uv run --no-sync python -m scripts.build_windows_app
```

Inno Setup 6 が入っている開発用Windowsでは、`uv run --no-sync python -m scripts.build_windows_app --installer` で `PLaMo-for-競プロ-Windows-Setup.exe` も生成します。一般の利用者にはこのインストーラーを渡す構成です。ユーザー領域へのインストールなので、管理者権限を要求しません。GitHub Actions のWindowsランナーには [Inno Setup が含まれています](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md)。CIの定義は追加済みで、実行はしていません。

uvは必要に応じて `python -m pip install --require-hashes --no-deps --only-binary=:all: -r packaging/uv-bootstrap.txt` で固定版を導入できます。実行・ビルド依存はuv.lockのpackagingグループまで固定します。

ランタイム準備コマンドは、固定した公式 llama.cpp の実行ファイルとDLLを取得し、SHA-256を検証します。このランタイムと検証元の固定ZIPを配布物に同梱します。実行前にはZIPの固定ハッシュと展開後の全ファイルを照合し、書き換えられたサイドカーだけでは信頼しません。モデルの重みは同梱しません。依存のライセンスと出所一覧は `_internal/third_party_licenses/INDEX.json` 以下に保存し、公式ランタイム内の文書も保持します。静的リンクされたC++依存の網羅性はバイナリ公開前の追加確認が必要です。

標準ビルドは、さらに公式 [llama.cpp b12119](https://github.com/ggml-org/llama.cpp/releases/tag/b12119) の Vulkan x64 ZIP を固定 SHA-256 で検証し、同じ版の公開ヘッダーで小さな `cp_llama_bridge.dll` をローカルビルドします。展開した DLL・ブリッジ・ライセンス文書の全ファイルのハッシュを、ビルド用コピーの Python コードへ埋め込みます。起動時はこの固定値と照合し、余分なファイル、欠落・改変、シンボリックリンクやジャンクションを拒否します。配布物内の JSON だけを信用して DLL を実行する構成にはしません。元のソースにある `_native_runtime_pins.py` は未設定のままに保ちます。

Vulkan 版は検証済みの DLL を専用の子プロセスで使い、対応する GPU が使えない場合は CPU を使います。ネイティブ実行部の検証や起動に失敗した場合は、別に検証する CPU サーバーへ切り替え、管理画面に実際の実行先と理由を表示します。CUDA 専用パッケージは今回同梱しません。`--without-native` は開発用の CPU サーバーのみのビルドです。Vulkan ZIP には ARM64 の固定値も用意していますが、現在のブリッジと CI は x64 を対象とし、ARM64 版を検証済みとは扱いません。

アイコンは保存済みの `packaging/icons/app-icon.ico` を実行ファイル、Tkウィンドウ、インストーラーで使います。Windowsでのビルド時にMacの変換ツールや画像ライブラリは必要ありません。原画を変更する場合の変換はmacOS側の `scripts/build_app_icons.py` で行い、生成済みICOも一緒に保存します。

`dist/windows/PLaMo-for-競プロ-Windows.zip` を展開し、フォルダー内の `PLaMo for 競プロ.exe` をダブルクリックします。GUIが翻訳サーバーを起動し、ブラウザーで管理画面を開きます。起動ウィンドウには読込中のモデルと推論の稼働率を表示し、管理画面で変更した内容を自動で反映します。モデルの取得・使用・解除・削除、保存先の選択、訳文キャッシュの確認や変更ができます。利用者は明示的なボタンで初回モデル取得を開始し、Tampermonkeyスクリプトを追加します。標準出力と標準エラーは常時回収し、各ログは現在分と1バックアップ、各128KiB、1種類あたり256KiBに制限します。

Windows 版も、共通の段落数設定・生成途中の表示・段落ごとの取消に対応します。同じモデルの依頼を最大32枠で連続処理し、32,768トークンの共有KVキャッシュに入力と最大出力の予約合計が収まるよう待機させます。32枠分の最大文脈を別々に確保する設定にはしません。GPU 実行部は同期後に対象の系列だけを取り除いて取り消し、CPU サーバーへの切替後は対象の HTTP 接続だけを取り消します。Mac 版と同じ速度を保証するものではありません。

モデルを使わない Windows SSE ジョブ契約テストは `tests/test_windows_translation_jobs.py` です。CI では Python テストに加えて、固定 CPU ランタイムの `--version`、ブリッジ ABI と CPU デバイスの列挙、凍結アプリのモデル未読込起動と専用ワーカーの通信を確認する定義です。モデルの読込みや GPU 推論は実行しません。この新しい Windows ビルド・実機・ドライバーでの実行はまだ確認していないため、Vulkan の互換性や速度を確認済みとは扱いません。

翻訳バーの `plamo-for-cp://open` からアプリを起動するには、Windows用インストーラーを使ってください。インストーラーは現在の利用者の `HKCU\Software\Classes\plamo-for-cp` に登録し、アンインストール時に削除します。ZIP版はこの登録を変更せず、直接起動する形です。リンクからの起動はネイティブウィンドウを表示してサーバーを開始・再利用し、管理画面のタブやモデルの取得・読み込み・翻訳は自動で開始しません。ブラウザーの外部アプリ起動確認への応答は必要です。

登録するコマンドは、引用符付きのEXEと固定の `--open-url "plamo-for-cp://open"` だけです。外部URIの `%1` をコマンドへ埋め込まず、URI内の引用符を追加引数へ変える攻撃を避けます。アプリ側も通常起動・固定URL起動・内部サーバー起動の引数を区別し、混在した引数を拒否します。PyInstallerの標準 `multiprocessing.freeze_support()` は内部ワーカー用に維持します。

このビルド定義はWindowsで実行する必要があります。Mac上でWindows実行ファイルを生成・動作検証したものではありません。一般公開する場合は、Windows実機で初回取得・翻訳・終了を検証し、配布元署名を行ってください。
