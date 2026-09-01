"""``python -m story_lifecycle.butler_proxy`` 启动入口(shim)。

实现全部在 :mod:`story_lifecycle.infra.butler_proxy`(部署形态/env 清单/白名单
路由见其模块 docstring);本文件只让 ``-m`` 能从包顶层找到入口 —— 与
``orchestrator/mcp/butler_server.py`` 的 ``-m`` 启动方式同款品味。
"""

from story_lifecycle.infra.butler_proxy import main

if __name__ == "__main__":
    raise SystemExit(main())
