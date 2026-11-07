"""「人格运行时」插件核心包。

本包尽量只依赖 Python 标准库，便于在没有 MaiBot Host 的环境下做单元测试；
与 SDK 的耦合集中在 :mod:`persona.plugin_impl` 与 :mod:`persona.config_model`。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
