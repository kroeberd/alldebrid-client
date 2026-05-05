# packaging/pyinstaller_hooks/hook-pydantic_settings.py
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules("pydantic_settings")
datas = collect_data_files("pydantic_settings")
