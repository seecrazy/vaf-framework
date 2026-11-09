"""HTTP 网关（V2.5 移动接入层）：把 VafApp 包成 REST API + 移动审核台。

端点（OpenAPI 自动生成于 /docs）：
  GET  /                      H5 审核台（移动优先 PWA，扫码/卡片链接直达）
  GET  /health                健康检查（版本/已注册领域）
  GET  /api/v1/presets        预置领域目录
  POST /api/v1/runs           提交 run {domain, input, request_id?}
  GET  /api/v1/runs?status=   run 列表（waiting_input/wating_review 等过滤）
  GET  /api/v1/runs/{id}      run 排障视图（inspect）
  POST /api/v1/runs/{id}/clarifications   提交澄清回答
  GET  /api/v1/reviews/pending            待审核任务列表
  POST /api/v1/reviews/{task_id}/decision 审核 批准/驳回

鉴权：X-VAF-API-Key（ApiKeyStore 映射 RBAC 用户）；未配置 Key 时为开放模式
（合成 admin，仅限内网/演示——日志与健康检查会明确提示）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..errors import PermissionDeniedError, VafError
from ..security.permissions import Permission, User
from .auth import _ADMIN, ApiKeyStore

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class RunRequest(BaseModel):
    domain: str
    input: Dict[str, Any]
    request_id: Optional[str] = None


class ClarificationRequest(BaseModel):
    answers: Dict[str, Any]


class ReviewDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = ""


def create_gateway(
    app,
    *,
    keys: Optional[ApiKeyStore] = None,
    review_base: str = "http://localhost:8000",
) -> "FastAPI":
    api = FastAPI(
        title="VAF Gateway",
        version="2.5.0",
        description="垂直领域 Agent 框架 HTTP 网关：run 提交 / 澄清 / 人工审核 / 预置领域",
    )
    open_mode = keys is None or not keys.enabled

    @api.exception_handler(PermissionDeniedError)
    def _forbidden(request: Request, exc: PermissionDeniedError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @api.exception_handler(VafError)
    def _bad_request(request: Request, exc: VafError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def resolve_user(request: Request) -> User:
        if open_mode:
            return _ADMIN
        user = keys.authenticate(request.headers.get("X-VAF-API-Key", ""))
        if user is None:
            raise HTTPException(status_code=401, detail="无效或缺失的 X-VAF-API-Key")
        return user

    def _handle_to_dict(handle):
        return handle.to_dict()

    # ------------------------------------------------------------------ 页面
    @api.get("/", include_in_schema=False)
    def index():
        return FileResponse(_STATIC_DIR / "review.html")

    @api.get("/health")
    def health():
        return {
            "status": "ok",
            "version": api.version,
            "auth_mode": "open(仅限内网/演示)" if open_mode else "api-key",
            "domains": sorted(app.engine._domains.keys()),
        }

    @api.get("/api/v1/presets")
    def presets():
        from ..presets import load_index

        return load_index()

    # ------------------------------------------------------------------ runs
    @api.post("/api/v1/runs")
    def submit_run(body: RunRequest, user: User = Depends(resolve_user)):
        app.permissions.require(user, Permission.RUN_PIPELINE, context="api:submit_run")
        request_id = body.request_id or f"api-{body.domain}-{int(app.clock() * 1000)}"
        try:
            handle = app.run(body.domain, body.input, request_id, user=user)
        except VafError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _handle_to_dict(handle)

    @api.get("/api/v1/runs")
    def list_runs(status: Optional[str] = None, pipeline: Optional[str] = None, user: User = Depends(resolve_user)):
        app.permissions.require(user, Permission.AUDIT_READ, context="api:list_runs")
        from ..runtime.state import RunStatus

        try:
            enum_status = RunStatus(status) if status else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"未知状态：{status}") from exc
        states = app.state_store.list(status=enum_status, pipeline=pipeline)
        return [
            {
                "request_id": s.request_id,
                "pipeline": s.pipeline,
                "status": s.status.value,
                "pending_review_task": s.pending_review_task,
                "pending_clarification": (
                    s.pending_clarification.model_dump(mode="json") if s.pending_clarification else None
                ),
                "error": s.error,
                "updated_at": s.updated_at,
            }
            for s in states[-100:]
        ]

    @api.get("/api/v1/runs/{request_id}")
    def inspect_run(request_id: str, user: User = Depends(resolve_user)):
        app.permissions.require(user, Permission.AUDIT_READ, context="api:inspect_run")
        report = app.inspect_run(request_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"run 不存在：{request_id}")
        return report

    @api.post("/api/v1/runs/{request_id}/clarifications")
    def submit_clarification(request_id: str, body: ClarificationRequest, user: User = Depends(resolve_user)):
        app.permissions.require(user, Permission.SUBMIT_REVIEW, context="api:clarify")
        try:
            handle = app.submit_clarification(request_id, body.answers)
        except VafError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _handle_to_dict(handle)

    # ------------------------------------------------------------------ 审核
    @api.get("/api/v1/reviews/pending")
    def pending_reviews(pipeline: Optional[str] = None, user: User = Depends(resolve_user)):
        app.permissions.require(user, Permission.SUBMIT_REVIEW, context="api:pending_reviews")
        return app.list_pending_reviews(pipeline)

    @api.post("/api/v1/reviews/{task_id}/decision")
    def review_decision(task_id: str, body: ReviewDecisionRequest, user: User = Depends(resolve_user)):
        app.permissions.require(user, Permission.SUBMIT_REVIEW, context="api:review_decision")
        from ..errors import PermissionDeniedError, ReviewError

        try:
            handle = app.submit_review(
                task_id, body.decision, reviewer=user, comment=body.comment
            )
        except ReviewError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return _handle_to_dict(handle)

    return api
