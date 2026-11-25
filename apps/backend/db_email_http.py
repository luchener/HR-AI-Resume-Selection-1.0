"""Explicitly injected SQL preview routes for administrator email notifications."""
from flask import g, request

from db_email_notifications import EmailNotificationError


def register_email_routes(app, service, *, error, success):
    def actor_args():
        return {"actor_id": str(g.preview_user.user_id), "password_version": g.preview_user.password_version}

    @app.errorhandler(EmailNotificationError)
    def email_error(exc):
        return error(exc.detail, exc.status)

    @app.post("/api/v1/admin/notifications/email/preview")
    def email_preview():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            return error("Request body must be an object", 422)
        html = service.preview(payload=data, **actor_args())
        return success({"html": html})

    @app.get("/api/v1/admin/notifications/email/image/<image_id>/data")
    def email_image_data(image_id):
        return success({"data_uri": service.image_data_uri(image_id=image_id, **actor_args())})

    @app.get("/api/v1/admin/notifications/email/images")
    def email_images():
        return success({"items": service.list_images(**actor_args())})

    @app.post("/api/v1/admin/notifications/email/image")
    def email_image_upload():
        uploaded = request.files.get("file")
        if uploaded is None:
            return error("No file provided", 422)
        content = uploaded.stream.read(2 * 1024 * 1024 + 1)
        item = service.save_image(content=content, filename=uploaded.filename or "image.png", request_id=g.request_id, **actor_args())
        return success(item), 201

    @app.delete("/api/v1/admin/notifications/email/image/<image_id>")
    def email_image_delete(image_id):
        return success(service.delete_image(image_id=image_id, request_id=g.request_id, **actor_args()))

    @app.get("/api/v1/admin/notifications/email/recipients")
    def email_recipients():
        return success(service.recipients(keyword=(request.args.get("keyword") or ""), **actor_args()))

    @app.post("/api/v1/admin/notifications/email")
    def email_send():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            return error("Request body must be an object", 422)
        key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
        return success(service.send(payload=data, request_id=g.request_id, idempotency_key=key, **actor_args()))

    @app.get("/api/v1/admin/notifications/email/logs")
    def email_logs():
        return success(service.list_logs(page=request.args.get("page", 1), size=request.args.get("size", 10), **actor_args()))

    @app.get("/api/v1/admin/notifications/email/logs/<log_id>")
    def email_log_detail(log_id):
        return success(service.get_log(log_id, **actor_args()))
