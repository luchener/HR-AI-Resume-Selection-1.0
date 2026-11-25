"""HTTP routes for the explicit PostgreSQL quota adapter."""
from flask import g, request

from db_quota_service import QuotaServiceError


def register_quota_routes(app, service, *, error, success):
    @app.errorhandler(QuotaServiceError)
    def quota_error(exc):
        detail = {403: 'Administrator required',
                  404: 'User or quota policy not found',
                  422: 'Quota fields are invalid'}.get(exc.status, 'DB preview unavailable')
        return error(detail, exc.status)

    def actor_args():
        return dict(password_version=g.preview_user.password_version, request_id=g.request_id)

    @app.get('/api/v1/admin/quota/settings')
    def quota_settings():
        return success(service.get_settings(str(g.preview_user.user_id), **actor_args()))

    @app.put('/api/v1/admin/quota/settings')
    def quota_update_settings():
        return success(service.update_settings(str(g.preview_user.user_id),
                                               request.get_json(silent=True), **actor_args()))

    @app.put('/api/v1/admin/quota/users/<username>')
    def quota_update_user(username):
        return success(service.set_user_quota(str(g.preview_user.user_id), username,
                                              request.get_json(silent=True), **actor_args()))

    @app.delete('/api/v1/admin/quota/users/<username>')
    def quota_reset_user(username):
        return success(service.reset_user_quota(str(g.preview_user.user_id), username,
                                                **actor_args()))

    @app.get('/api/v1/quota/me')
    def quota_me():
        return success(service.get_my_quota(str(g.preview_user.user_id),
                                            password_version=g.preview_user.password_version))
