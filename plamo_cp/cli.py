"""Commands for setup, a persistent local API, and a real-model smoke test."""

import argparse
import json
import logging

from .desktop import DEFAULT_PORT
from .engine import DEFAULT_MODEL_ID, SUPPORTED_MODEL_IDS
from .runtime import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PLaMo 3 Translate for CP (Windows / Apple Silicon)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="localhost の翻訳APIを起動")
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    fetch = commands.add_parser("download", help="このOS用の公式モデルを事前ダウンロード")
    fetch.add_argument("--model", choices=SUPPORTED_MODEL_IDS, default=DEFAULT_MODEL_ID)
    translate = commands.add_parser("translate", help="端末で翻訳を実行")
    translate.add_argument("text")
    translate.add_argument("--model", choices=SUPPORTED_MODEL_IDS, default=DEFAULT_MODEL_ID)
    translate.add_argument("--source", default="English")
    translate.add_argument("--target", default="Japanese")
    commands.add_parser("models", help="モデルとダウンロードサイズを表示")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "download":
        try:
            print(make_engine().download(args.model))
        except (ValueError, RuntimeError, OSError) as exc:
            parser.exit(1, f"ダウンロードエラー: {exc}\n")
    elif args.command == "models":
        specs = make_engine().model_specs
        print(
            json.dumps(
                {model: specs[model] for model in SUPPORTED_MODEL_IDS},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        try:
            print(make_engine().translate(args.text, args.model, args.source, args.target))
        except (ValueError, RuntimeError, OSError) as exc:
            parser.exit(1, f"翻訳エラー: {exc}\n")


if __name__ == "__main__":
    main()
