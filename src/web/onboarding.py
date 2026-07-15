"""
========================================
web/onboarding.py — 首次部署向导页面与 API
========================================

提供独立 `/onboarding` 页面，以及部署模式目录、当前实际配置报告、预检和保存接口。
配置仍写入现有 config.yaml；本模块只编排，不创建第二套配置真源。

不做什么：不保存密码、不管理 OAuth token、不直接重启服务、不修改平台环境变量。
对外暴露：register(mcp)。
========================================
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Mapping

import yaml
from starlette.requests import Request
from starlette.responses import Response

from deployment_profile import (
    build_profile_patch,
    effective_configuration_report,
    profile_catalog,
    validate_profile_patch,
)
from utils import config_file_path

from . import _shared as sh

logger = logging.getLogger("ombre_brain")


def _read_persisted_config(path: str) -> dict[str, Any]:
    """读取原始持久配置；解析失败显式抛出，由路由翻译成人话。"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 顶层必须是对象")
    return raw


def _merge_patch(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """合并向导拥有的顶层字段，保留模型、衰减等其他用户配置。"""
    merged = dict(base)
    for key, value in patch.items():
        if key == "deployment" and isinstance(value, Mapping):
            old = merged.get("deployment")
            deployment = dict(old) if isinstance(old, Mapping) else {}
            deployment.update(dict(value))
            merged[key] = deployment
        else:
            merged[key] = value
    return merged


def _atomic_write_yaml(path: str, payload: Mapping[str, Any]) -> None:
    """在同目录原子替换配置，避免容器重启时读到半份 YAML。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=parent, prefix=".config-", suffix=".tmp", delete=False
        ) as handle:
            temp_path = handle.name
            yaml.safe_dump(dict(payload), handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError as exc:
            logger.warning("无法收紧临时配置文件权限，将继续原子替换: %s", exc)
        os.replace(temp_path, path)
    except Exception:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError as cleanup_exc:
                logger.warning("清理向导临时配置失败: %s", cleanup_exc)
        raise


def _report(path: str, persisted: Mapping[str, Any]) -> dict[str, Any]:
    """组合部署报告，统一复用现有持久卷探测。"""
    persistence = sh.data_dir_persistence(str(sh.config.get("buckets_dir") or ""))
    return effective_configuration_report(
        sh.config,
        persisted,
        environment=os.environ,
        config_path=path,
        persistence=persistence,
    )


def register(mcp: Any) -> None:
    """注册首次部署页面与受 Dashboard 会话保护的向导 API。"""

    @mcp.custom_route("/onboarding", methods=["GET"])
    async def onboarding_page(request: Request) -> Response:
        from starlette.responses import HTMLResponse

        page_path = os.path.join(sh.repo_root, "frontend", "onboarding.html")
        try:
            with open(page_path, "r", encoding="utf-8") as handle:
                page = handle.read()
            return HTMLResponse(page, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        except FileNotFoundError:
            return HTMLResponse("<h1>onboarding.html not found</h1>", status_code=404)
        except OSError as exc:
            logger.error("读取首次部署页面失败: %s", exc)
            return HTMLResponse("<h1>无法读取首次部署页面</h1>", status_code=500)

    @mcp.custom_route("/api/onboarding/profile", methods=["GET"])
    async def onboarding_profile(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err
        path = config_file_path()
        try:
            persisted = _read_persisted_config(path)
            return JSONResponse({"ok": True, "profiles": profile_catalog(), "report": _report(path, persisted)})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.error("读取首次部署配置失败: %s", exc)
            return JSONResponse({"ok": False, "error": f"无法读取配置：{exc}"}, status_code=500)

    @mcp.custom_route("/api/onboarding/preflight", methods=["POST"])
    async def onboarding_preflight(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
            patch = build_profile_patch(body.get("profile"), body.get("options"))
            issues = validate_profile_patch(patch)
            return JSONResponse({"ok": not issues, "issues": issues, "patch": patch})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc), "issues": [str(exc)]}, status_code=400)
        except Exception as exc:
            logger.exception("首次部署预检失败")
            return JSONResponse({"ok": False, "error": f"预检失败：{exc}"}, status_code=500)

    @mcp.custom_route("/api/onboarding/apply", methods=["POST"])
    async def onboarding_apply(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
            if body.get("confirm") is not True:
                return JSONResponse({"ok": False, "error": "confirm=true required"}, status_code=400)
            patch = build_profile_patch(body.get("profile"), body.get("options"))
            issues = validate_profile_patch(patch)
            if issues:
                return JSONResponse({"ok": False, "issues": issues}, status_code=400)
            path = config_file_path()
            persisted = _read_persisted_config(path)
            merged = _merge_patch(persisted, patch)
            _atomic_write_yaml(path, merged)
            # OAuth/transport 都在启动时绑定；这里只更新“期望值”，不谎报热切换成功。
            report = _report(path, merged)
            return JSONResponse({
                "ok": True,
                "saved": patch,
                "report": report,
                "restart_required": True,
                "message": "部署模式已保存，重启服务后生效。",
            })
        except (ValueError, TypeError, OSError, yaml.YAMLError) as exc:
            logger.error("保存首次部署配置失败: %s", exc)
            return JSONResponse({"ok": False, "error": f"保存失败：{exc}"}, status_code=500)
        except Exception as exc:
            logger.exception("首次部署保存发生未预期错误")
            return JSONResponse({"ok": False, "error": f"保存失败：{exc}"}, status_code=500)
