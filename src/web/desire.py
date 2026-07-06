"""
========================================
web/desire.py — 欲望引擎只读状态接口
========================================

「关：状态接口照常返回 drive / scores / intent，但不覆盖行为（能观察、不动手）。」
前端拿这个渲染「我的内心」面板：此刻最想做的事 + 8 维条 + 念头池 + 自己的发动机。

端点：
  GET /api/desire/state — 只读快照（cookie 鉴权，同其它 /api/*）

不做什么（边界）：
- 没有任何写端点。欲望不接受外部手调——它只被真实事件驱动。
  gate 调整走环境变量（OMBRE_DESIRE_*），改完重启生效。

对外暴露：register(mcp)
========================================
"""

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import _shared as sh


def register(mcp) -> None:

    @mcp.custom_route("/api/desire/state", methods=["GET"])
    async def api_desire_state(request: Request):
        auth = sh._require_auth(request)
        if auth:
            return auth
        rt = sh.desire_runtime
        if rt is None:
            return JSONResponse({"enabled": False, "error": "desire runtime not wired"})
        return JSONResponse(rt.api_state())
