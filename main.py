#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Десктоп-обёртка: Flask в фоновом потоке + нативное окно pywebview.

Запуск из исходников:  python main.py
Сборка .exe:           см. build.spec / .github/workflows
"""
import socket
import threading
import time
import urllib.request

import webview

from app import app

TITLE = "Вкладыши для аудиокассет"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_flask(port):
    # без reloader/debug — иначе поднимется второй процесс и watcher
    app.run(host="127.0.0.1", port=port, debug=False,
            use_reloader=False, threaded=True)


def _wait_until_up(url, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def main():
    port = _free_port()
    threading.Thread(target=_run_flask, args=(port,), daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    _wait_until_up(url)
    webview.create_window(TITLE, url, width=1100, height=860, min_size=(820, 600))
    webview.start()      # блокирует до закрытия окна; daemon-поток Flask умрёт сам


if __name__ == "__main__":
    main()
