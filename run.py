"""Double-click entry point with a friendly Windows startup error."""
from pathlib import Path
import os
import sys
import urllib.request
import webbrowser


if __name__ == '__main__':
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    if '--image-worker' in sys.argv:
        from converter.image_worker import main as image_worker_main
        sys.exit(image_worker_main(sys.argv[sys.argv.index('--image-worker') + 1]))
    try:
        # Reuse this application's running loopback service.
        try:
            import json
            with urllib.request.urlopen('http://127.0.0.1:18080/api/status', timeout=1) as reply:
                state = json.load(reply)
            if '--port' not in sys.argv and state.get('version') == '0.1.0' and 'models' in state:
                webbrowser.open('http://127.0.0.1:18080')
                sys.exit(0)
        except Exception:
            pass
        from app import main
        main()
    except Exception as exc:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, '프로그램을 시작하지 못했습니다.\n\n' + str(exc)[:400], '스캔편집', 16)
        else:
            raise
