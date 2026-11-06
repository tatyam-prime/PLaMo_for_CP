import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--server-child" in sys.argv:
        sys.argv.remove("--server-child")
        from plamo_cp.desktop import main

        raise SystemExit(main())
    from plamo_cp.windows_app import main

    main()
