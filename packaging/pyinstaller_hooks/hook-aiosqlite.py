# packaging/pyinstaller_hooks/hook-aiosqlite.py
# Ensure aiosqlite's bundled sqlite3 is collected
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("aiosqlite")
hiddenimports = collect_submodules("aiosqlite")
