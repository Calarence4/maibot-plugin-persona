"""MaiBot 插件入口。

Runner 以文件路径加载本文件，因此需要把插件目录加入 ``sys.path`` 后才能导入
``persona`` 包；重复加载时不会重复插入。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from persona.plugin_impl import PersonaPlugin, create_plugin  # noqa: E402

__all__ = ["PersonaPlugin", "create_plugin"]
