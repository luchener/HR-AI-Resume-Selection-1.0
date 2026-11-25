"""HTTP registration for explicit SQL administrator adapters."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from flask import Response, g, request

from db_admin_service import AdminServiceError


def register_admin_routes(app, service, *, error, success):
    @app.errorhandler(AdminServiceError)
    def admin_error(exc):
        return error(exc.detail, exc.status)

    def args():
        return {"password_version": g.preview_user.password_version}

    def actor():
        return str(g.preview_user.user_id)

    @app.get("/api/v1/admin/ops")
    def admin_ops():
        return success(service.list_ops(actor(), op=request.args.get("op", ""), keyword=request.args.get("keyword", ""),
            page=request.args.get("page", 1), size=request.args.get("size", 50), **args()))

    @app.get("/api/v1/admin/ops/export")
    def admin_ops_export():
        data = service.export_ops(actor(), op=request.args.get("op", ""), keyword=request.args.get("keyword", ""), **args())
        fmt = request.args.get("format", "csv").lower()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        if fmt == "json":
            return Response(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "items": data["items"]}, ensure_ascii=False),
                mimetype="application/json", headers={"Content-Disposition": f'attachment; filename="admin-ops-{stamp}.json"'})
        buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(["时间(UTC)", "操作类型", "操作员", "目标用户", "详情", "request_id"])
        for item in data["items"]:
            writer.writerow([item.get("created_at", ""), item.get("op", ""), item.get("operator_name", ""), item.get("target_username", ""), item.get("detail", ""), item.get("request_id", "")])
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="admin-ops-{stamp}.csv"'})

    @app.get("/api/v1/admin/resumes")
    def admin_resumes():
        return success(service.list_resumes(actor(), keyword=request.args.get("keyword", ""), owner_user_id=request.args.get("user_id"),
            page=request.args.get("page", 1), size=request.args.get("size", 20), **args()))

    @app.get("/api/v1/admin/resumes/<resume_id>")
    def admin_resume_detail(resume_id):
        return success(service.get_resume(actor(), resume_id, request_id=g.request_id, **args()))

    @app.delete("/api/v1/admin/resumes/<resume_id>")
    def admin_resume_delete(resume_id):
        return success(service.delete_resume(actor(), resume_id, request_id=g.request_id, **args()))

    @app.get("/api/v1/admin/users/<username>/usage")
    def admin_user_usage(username):
        return success({"username": username, **service.user_usage(actor(), username, granularity=request.args.get("granularity", "day"),
            buckets=request.args.get("buckets", 30), **args())})

    @app.get("/api/v1/admin/users/usage-ranking")
    def admin_usage_ranking():
        items = service.usage_ranking(actor(), limit=request.args.get("limit", 20), **args())
        return success({"items": items, "total": len(items)})
