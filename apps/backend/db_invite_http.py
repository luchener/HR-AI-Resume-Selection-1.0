"""Explicit invitation routes for the isolated SQL composition only."""
from flask import g, request
from db_public_auth import PublicAuthError


def register_invite_routes(app, service, *, success):
    def body():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            raise PublicAuthError('Request body must be an object')
        return data

    def actor():
        user = g.preview_user
        return dict(actor_id=str(user.user_id), password_version=user.password_version)

    def paging():
        try:
            return dict(page=int(request.args.get('page', '1')), size=int(request.args.get('size', '200')))
        except ValueError:
            raise PublicAuthError('分页参数不正确。') from None

    @app.post('/api/v1/invite-request')
    def invite_submit():
        data = body()
        return success(service.submit(data.get('email'), data.get('note'),
            ip=request.remote_addr or '127.0.0.1', request_id=g.request_id))

    @app.get('/api/v1/admin/invite-requests')
    def invite_requests():
        return success(service.list_requests(**actor(), status=request.args.get('status', 'pending'), **paging()))

    @app.post('/api/v1/admin/invite-requests/<identity>/approve')
    def invite_approve(identity):
        return success(service.review(**actor(), identity=identity, action='approve', request_id=g.request_id))

    @app.post('/api/v1/admin/invite-requests/<identity>/reject')
    def invite_reject(identity):
        return success(service.review(**actor(), identity=identity, action='reject', reason=body().get('reason',''),
                                      request_id=g.request_id))

    @app.post('/api/v1/admin/invite-requests/<identity>/resend')
    def invite_resend(identity):
        return success(service.review(**actor(), identity=identity, action='resend', request_id=g.request_id))

    @app.get('/api/v1/admin/invite-codes')
    def invite_codes():
        return success(service.list_codes(**actor(), status=request.args.get('status','all'), **paging()))

    @app.post('/api/v1/admin/invite-codes/generate')
    def invite_generate():
        data = body()
        return success(service.generate(**actor(), count=data.get('count',1), note=data.get('note',''),
            email=data.get('email',''), request_id=g.request_id))

    @app.post('/api/v1/admin/invite-codes/<code_or_hash>/revoke')
    def invite_revoke(code_or_hash):
        return success(service.revoke(**actor(), code_or_hash=code_or_hash, request_id=g.request_id))
