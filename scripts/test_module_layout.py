#!/usr/bin/env python3
"""拆分后的模块布局约束。

http_bridge.py 正在按领域拆进 delivery_bridge/。搬迁过程中最容易出的错是
「在新模块里重新写一份常量，却忘了删掉 http_bridge 里的旧定义」：两份取值
一致时测试照样全绿，但旧的那份已经没有读者，而针对它的打桩会静默失效。
更糟的情况是手打时写错了值——曾经把 5/16 写成 10/20，悄悄放宽了附件上限。

所以这条约束是：同一个常量名在整个包里只能有一处定义。
"""

import ast
import importlib.util
import os
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def top_level_constants(path: Path) -> dict[str, str]:
    """模块级的大写常量名 -> 它的字面表达式。"""
    constants = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id.isupper():
            constants[target.id] = ast.unparse(node.value)
    return constants


class ModuleLayoutTest(unittest.TestCase):
    def source_files(self) -> list[Path]:
        return sorted((PLUGIN_ROOT / "delivery_bridge").rglob("*.py")) + [PLUGIN_ROOT / "http_bridge.py"]

    def test_no_constant_is_defined_in_two_modules(self):
        owners = defaultdict(list)
        for path in self.source_files():
            for name, value in top_level_constants(path).items():
                owners[name].append((path.relative_to(PLUGIN_ROOT).as_posix(), value))

        duplicates = {name: places for name, places in owners.items() if len(places) > 1}
        self.assertEqual(
            {},
            duplicates,
            "常量只能有一处定义。搬迁时请把原定义删掉并改成再导出，不要在新模块里手打一份：\n"
            + "\n".join(
                f"  {name}: " + ", ".join(f"{where}={value}" for where, value in places)
                for name, places in sorted(duplicates.items())
            ),
        )


class GetRouteTableTest(unittest.TestCase):
    """do_GET 由 if 链改成了路由表，表和方法必须对得上。

    表里写错方法名不会有任何静态报错，只有真的请求那条路径时才 AttributeError；
    反过来，写了处理方法却忘了登记路由，那个接口就静悄悄地 404。
    """

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("DELIVERY_TASK_PLANNER_RUNTIME_DIR", tempfile.mkdtemp())
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        spec = importlib.util.spec_from_file_location(
            "delivery_task_http_bridge_layout", PLUGIN_ROOT / "http_bridge.py"
        )
        cls.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)

    def test_every_route_points_at_an_existing_handler(self):
        handler = self.bridge.BridgeHandler
        missing = sorted(
            f"{path} -> {name}"
            for path, name in handler.GET_ROUTES.items()
            if not callable(getattr(handler, name, None))
        )
        self.assertEqual([], missing, "路由表指向了不存在的处理方法")

    def test_every_get_handler_is_reachable(self):
        handler = self.bridge.BridgeHandler
        registered = set(handler.GET_ROUTES.values())
        # 这两条路径带参数，匹配不出常量表，由 do_GET 单独调用。
        registered.update({"_get_attachment", "_get_artifact"})
        defined = {name for name in vars(handler) if name.startswith("_get_")}
        self.assertEqual(set(), defined - registered, "处理方法没有登记进路由表，接口会静默 404")


class PostRouteTableTest(unittest.TestCase):
    """do_POST 的统一形状接口也收进了路由表，表里的方法名必须真的存在。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = GetRouteTableTest.bridge

    def test_every_route_points_at_an_existing_bridge_method(self):
        executor = self.bridge.ExecutionBridge
        missing = sorted(
            f"{path} -> {method}"
            for path, (_status, _target, method, _kw) in self.bridge.BridgeHandler.POST_ROUTES.items()
            if not callable(getattr(executor, method, None))
        )
        self.assertEqual([], missing, "POST 路由表指向了 ExecutionBridge 上不存在的方法")

    def test_targets_are_known(self):
        targets = {target for _s, target, _m, _k in self.bridge.BridgeHandler.POST_ROUTES.values()}
        self.assertEqual(set(), targets - {"workspace", "process"}, "只认识这两种桥")


if __name__ == "__main__":
    unittest.main()
