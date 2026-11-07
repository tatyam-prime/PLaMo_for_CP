import multiprocessing
import sys


def main():
    from plamo_cp.launch_protocol import launch_mode

    # Preserve PyInstaller's own worker startup. The installed protocol command
    # supplies fixed arguments and never substitutes the external URI (%1).
    multiprocessing.freeze_support()
    # Validate each normal mode before dispatching the private server branch.
    try:
        mode = launch_mode(sys.argv[1:])
    except ValueError:
        return 2
    if mode == "native":
        sys.argv = [sys.argv[0]]
        from plamo_cp.native_worker import main

        return main()
    if mode == "server":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from plamo_cp.desktop import main

        return main()
    from plamo_cp.windows_app import main

    main(protocol_launch=mode == "open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
