"""CAD Worker 命令行入口。

API 会把 CanonicalPlan 写成 JSON，然后通过这个入口启动独立进程执行。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from template_core.models import CanonicalPlan

from .geometry import execute_plan


# 读取 plan、执行几何生成、写出 result，并用退出码表达是否成功。
def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one immutable canonical CAD plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args()

    plan = CanonicalPlan.model_validate_json(Path(arguments.plan).read_text(encoding="utf-8"))
    result = execute_plan(plan, Path(arguments.output))
    Path(arguments.result).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
