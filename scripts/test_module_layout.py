#!/usr/bin/env python3
"""拆分后的模块布局约束。

http_bridge.py 正在按领域拆进 delivery_bridge/。搬迁过程中最容易出的错是
「在新模块里重新写一份常量，却忘了删掉 http_bridge 里的旧定义」：两份取值
一致时测试照样全绿，但旧的那份已经没有读者，而针对它的打桩会静默失效。
更糟的情况是手打时写错了值——曾经把 5/16 写成 10/20，悄悄放宽了附件上限。

所以这条约束是：同一个常量名在整个包里只能有一处定义。
"""

import ast
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


if __name__ == "__main__":
    unittest.main()
