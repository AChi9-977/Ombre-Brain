"""
========================================
web/timeline.py — 线性时间线的 dashboard 接口
========================================

- /api/timeline (GET)：全部条目，按日期升序
- /api/timeline (POST)：加一条 {text, date?, author?}
- /api/timeline/{id} (PATCH)：改 text / date
- /api/timeline/{id} (DELETE)：删一条（?confirm=true）

存储与格式约定见 src/timeline_store.py。对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    import timeline_store as ts  # type: ignore
except ImportError:  # pragma: no cover
    from .. import timeline_store as ts  # type: ignore


def _buckets_dir() -> str:
    return sh.config.get("buckets_dir", "buckets")


def register(mcp) -> None:

    @mcp.custom_route("/api/timeline", methods=["GET"])
    async def api_timeline_list(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            entries = ts.load_entries(_buckets_dir())
            return JSONResponse({"entries": entries, "total": len(entries)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/timeline", methods=["POST"])
    async def api_timeline_create(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        try:
            entry = ts.add_entry(
                _buckets_dir(),
                text=str(body.get("text") or ""),
                date=str(body.get("date") or ""),
                author=str(body.get("author") or "user"),
            )
            return JSONResponse({"ok": True, "entry": entry})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/timeline/{entry_id}", methods=["PATCH"])
    async def api_timeline_edit(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        entry_id = request.path_params["entry_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        try:
            entry = ts.update_entry(
                _buckets_dir(),
                entry_id,
                text=str(body.get("text") or ""),
                date=str(body.get("date") or ""),
            )
            if not entry:
                return JSONResponse({"error": "entry not found"}, status_code=404)
            return JSONResponse({"ok": True, "entry": entry})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/timeline/{entry_id}", methods=["DELETE"])
    async def api_timeline_delete(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.query_params.get("confirm", "").lower() not in ("true", "1", "yes"):
            return JSONResponse({"error": "confirm=true required"}, status_code=400)
        entry_id = request.path_params["entry_id"]
        try:
            ok = ts.delete_entry(_buckets_dir(), entry_id)
            if not ok:
                return JSONResponse({"error": "entry not found"}, status_code=404)
            return JSONResponse({"ok": True, "deleted": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
