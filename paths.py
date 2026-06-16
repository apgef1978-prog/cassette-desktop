#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пути для десктоп-сборки.

В обычном запуске ресурсы лежат рядом со скриптом; в PyInstaller --onefile —
во временной распаковке sys._MEIPASS. Вывод и рабочие каталоги всегда пишем
в пользовательскую папку «Документы\\Кассетные вкладыши» (каталог рядом с .exe
может быть недоступен на запись).
"""
import os
import sys


def resource_path(name):
    """Путь к упакованному ресурсу (шаблон .docx и т.п.)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def output_dir():
    """Пользовательская папка для готовых вкладышей (создаётся при первом обращении)."""
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    if not os.path.isdir(docs):
        docs = home
    d = os.path.join(docs, "Кассетные вкладыши")
    os.makedirs(d, exist_ok=True)
    return d
