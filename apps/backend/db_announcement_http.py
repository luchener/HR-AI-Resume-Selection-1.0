"""Existing announcement HTTP contracts for explicitly injected SQL previews."""
from flask import g, request
from db_announcements import AnnouncementError


def register_announcement_routes(app, service, *, success, error):
    @app.errorhandler(AnnouncementError)
    def announcement_error(exc):
        return error(exc.detail, exc.status)

    def actor():
        return dict(actor_id=str(g.preview_user.user_id), password_version=g.preview_user.password_version)

    @app.get('/api/v1/admin/announcements')
    def announcement_list():
        return success(service.list_admin(**actor(), page=request.args.get('page', 1), size=request.args.get('size', 10)))

    @app.post('/api/v1/admin/announcements')
    def announcement_create():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            raise AnnouncementError('请求体必须为对象。')
        return success(service.create(**actor(), payload=data, request_id=g.request_id)), 201

    @app.post('/api/v1/admin/announcements/<announcement_id>/cancel')
    def announcement_cancel(announcement_id):
        return success(service.cancel(**actor(), announcement_id=announcement_id, request_id=g.request_id))

    @app.delete('/api/v1/admin/announcements/<announcement_id>')
    def announcement_delete(announcement_id):
        return success(service.delete(**actor(), announcement_id=announcement_id, request_id=g.request_id))

    @app.get('/api/v1/notifications/announcements')
    def announcement_active():
        return success(service.active_for_user(**actor()))

    @app.post('/api/v1/notifications/announcements/<announcement_id>/dismiss')
    def announcement_dismiss(announcement_id):
        return success(service.dismiss(**actor(), announcement_id=announcement_id, request_id=g.request_id))
