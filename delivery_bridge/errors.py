"""桥接层统一的业务失败异常。

单独成模块是为了让 git、工作区、提示词这些底层模块都能引用它，
而不必反过来 import 入口模块 http_bridge。
"""

from __future__ import annotations


class BridgeFailure(Exception):
    pass
