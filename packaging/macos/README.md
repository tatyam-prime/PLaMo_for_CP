# macOS アプリの開発ビルド

Apple Silicon Mac 上でビルドします。アプリ本体に Python・翻訳ランタイムを含み、利用者の Python / uv インストールやターミナル操作は不要です。モデルの重みは初回設定で別に取得します。

```sh
uv sync --locked --group packaging --no-install-project --python 3.13
node scripts/build_userscript.mjs
uv run --no-sync python -m scripts.build_macos_app
```

`dist/macos/PLaMo for 競プロ.app` と ZIP を生成します。uv・Xcodeのコマンドラインツールは開発者側で必要です。uvは必要に応じて `python -m pip install --require-hashes --no-deps --only-binary=:all: -r packaging/uv-bootstrap.txt` で固定版を導入できます。実行・ビルド依存はuv.lockのpackagingグループまで固定します。ビルドスクリプト自身は依存パッケージもモデルもダウンロードしません。

同梱する依存のライセンス・NOTICEと出所一覧は、バックエンドの `_internal/third_party_licenses/INDEX.json` 以下に保存します。文書の不足はビルドエラーにします。静的リンクされたRust/C++依存の網羅性は別途確認が必要です。

Pythonコードに変更がなく、管理画面や翻訳スクリプトだけを更新した場合は `scripts/build_macos_app.py --skip-backend` で実行環境を再利用できます。この場合も最新のHTML・JS・`dist/plamo-for-cp.user.js`を取り込み、アプリを再署名してZIPを作り直します。

アイコンの原画は `packaging/icons/app-icon.png` です。変更時はMacで `.venv/bin/python scripts/build_app_icons.py` を実行し、生成したiconset・ICNS・ICOも一緒に保存します。macOS標準のsips/iconutilとPython標準ライブラリだけで形式変換し、透過を保ちます。ビルド時には生成済みICNSをアプリへ同梱し、FinderとDockで使います。

起動するとネイティブウィンドウを表示し、翻訳サーバーを開始してブラウザーの管理画面を開きます。モデルの取得・使用・解除・削除、保存先の選択、訳文キャッシュの確認や変更ができます。Tampermonkeyスクリプトを導入すると、同じ画面でブラウザー側の設定も変更できます。終了ボタン・Dock の終了操作で、このアプリが開始したサーバーを停止します。既存の PLaMo サーバーへ接続した場合、そのサーバーの終了は行いません。

アプリを使う場所へ移動して一度通常起動すると、LaunchServicesに `plamo-for-cp` スキームを登録します。翻訳バーの起動リンク `plamo-for-cp://open` は、このアプリのウィンドウを表示して翻訳サーバーを開始・再利用するだけです。モデルの取得・読み込み・翻訳は開始せず、管理画面のタブも追加しません。通常の直接起動は従来どおり管理画面を開きます。アプリの移動後はもう一度通常起動してください。ブラウザーやOSが表示する外部アプリ起動の確認には利用者が応答する必要があります。

受け付けるURLはこの固定の1種類だけです。クエリー、別のパス、ファイルURL、コマンドは処理しません。URLイベントがウィンドウ作成前に届いても、起動後のウィンドウ表示まで安全に引き継ぎます。

ログは `~/.cache/plamo-cp/logs/desktop.log` に保存し、起動中も128KiBごとにローテーションします。現在分と1本のバックアップで最大256KiBです。通常のHTTPアクセスログは出しません。ログ表示ボタンから Finder で開けます。

開発用成果物はアドホック署名です。一般配布版にするには、開発者が Developer ID で署名し、Apple の notarization を実施する必要があります。このビルドは署名済み一般配布版と区別してください。モデルの初回取得・容量の確認は設定画面で行います。

ビルドスクリプトは同梱する Mach-O バイナリーの最低OS版を検査し、アプリの `LSMinimumSystemVersion` に反映します。新しいOS向けにビルドされた Python / MLX を同梱して、macOS 14で動作すると表示することは避けます。現在このMacにあるPythonはmacOS 27.0、MLXは26.2を最低版としているため、ローカル成果物の最低版は27.0になります。

GitHub Actions の手動実行ワークフローは macOS 26 の arm64 ランナーと Windows x64 ランナーを使います。[GitHub のランナー一覧](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) に合わせ、廃止済みの macOS 14 ラベルを使用していません。ワークフローはリポジトリに追加しただけで、実行・公開はしていません。
