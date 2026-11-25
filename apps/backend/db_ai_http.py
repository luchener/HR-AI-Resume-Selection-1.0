"""HTTP registration for the opt-in SQL AI adapter."""
from flask import g, request, Response
import json

from db_ai_service import AIServiceError


def register_ai_routes(app, service, *, error, success):
    @app.errorhandler(AIServiceError)
    def ai_error(exc):
        return error(exc.detail, exc.status)

    def actor_args():
        return {"password_version": g.preview_user.password_version}

    @app.post("/api/v1/ai/test")
    def ai_test():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            return error("Request body must be an object", 422)
        config = service.normalize(data.get("ai_config"))
        return success(service.test(str(g.preview_user.user_id), runtime_config=config, **actor_args()))

    from db_analysis_workflow import AnalysisWorkflow
    if isinstance(service.workflow, AnalysisWorkflow):
        if not any(rule.rule == '/api/v1/resumes/improve' for rule in app.url_map.iter_rules()):
            from db_analysis_http import register_analysis_routes
            register_analysis_routes(app, service.workflow, error, success)
        return

    @app.post("/api/v1/resumes/improve")
    def improve():
        data = request.get_json(silent=True)
        if type(data) is not dict or not data.get("resume_id") or not data.get("job_id"):
            return error("resume_id and job_id are required", 422)
        config = service.normalize(data.get("ai_config")) if data.get("ai_config") is not None else None
        request_id = request.headers.get("Idempotency-Key")
        if request_id is not None:
            try:
                from uuid import UUID
                UUID(request_id)
            except (TypeError, ValueError):
                return error("Idempotency-Key must be a UUID", 422)
        result = service.improve(str(g.preview_user.user_id), resume_id=data.get("resume_id"), job_id=data.get("job_id"),
            runtime_config=config, request_id=request_id, **actor_args())
        if request.args.get("stream", "false").lower() in {"true", "1", "yes"}:
            payload = "data: " + json.dumps({"status": "completed", "result": {"data": result}}, ensure_ascii=False) + "\n\n"
            return Response(payload, mimetype="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})
        return success(result)
