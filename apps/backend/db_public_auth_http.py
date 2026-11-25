"""Opt-in SQL public authentication; no production or mail configuration."""
from flask import g, jsonify, request

from db_public_auth import GENERIC_RESET, PublicAuthError


def register_public_auth_routes(app, service, *, error, success, authenticated_response):
    @app.errorhandler(PublicAuthError)
    def auth_error(exc):
        response = jsonify(detail=exc.detail, request_id=g.request_id, **exc.extra)
        if exc.retry_after is not None:
            response.headers['Retry-After'] = str(exc.retry_after)
        return response, exc.status

    def body():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            raise PublicAuthError('Request body must be an object')
        return data

    def ip():
        return request.remote_addr or '127.0.0.1'

    def detail(text):
        return jsonify(request_id=g.request_id, detail=text)

    @app.get('/api/v1/auth/register-config')
    def auth_register_config():
        return success(dict(invite_required=True, contact_email=service.contact_email,
                            invite_code_length=service.invite_length))

    @app.post('/api/v1/auth/invite-code/check')
    def auth_invite_check():
        service.check_invite(body().get('invite_code'), ip=ip())
        return success({'valid': True})

    @app.post('/api/v1/auth/invite-code/send-email-code')
    def auth_register_code():
        data = body()
        service.request_code('register', data.get('email'), ip=ip(), invite_code=data.get('invite_code'))
        return detail('验证码已发送，请查收邮件。')

    @app.post('/api/v1/auth/email-code/send')
    def auth_email_code():
        # Compatibility alias, with the same mandatory invite gate.
        return auth_register_code()

    @app.post('/api/v1/auth/register')
    def auth_register():
        data = body()
        user = service.register(data.get('username'), data.get('password'), data.get('email'),
            data.get('code'), data.get('invite_code'), ip=ip(), request_id=g.request_id)
        return authenticated_response(user)

    @app.get('/api/v1/auth/captcha')
    def auth_captcha():
        from captcha_image import render_data_uri
        identity, code = service.captcha(ip=ip())
        return success(dict(captcha_id=identity, image=render_data_uri(code), ttl_seconds=300))

    @app.post('/api/v1/auth/reset-password/request')
    def auth_reset_request():
        service.request_code('reset', body().get('email'), ip=ip())
        return detail(GENERIC_RESET)

    @app.post('/api/v1/auth/reset-password/confirm')
    def auth_reset_confirm():
        data = body()
        service.recover('reset', data.get('email'), data.get('code'), password=data.get('new_password'),
                        ip=ip(), request_id=g.request_id)
        return success({'message': '密码重置成功，请使用新密码登录。'})

    @app.post('/api/v1/auth/unfreeze/send-code')
    def auth_unfreeze_request():
        data = body()
        service.request_code('unfreeze', data.get('email'), ip=ip(), username=data.get('username'))
        return detail('解冻验证码已发送，请查收邮件。')

    @app.post('/api/v1/auth/unfreeze')
    def auth_unfreeze_confirm():
        data = body()
        user = service.recover('unfreeze', data.get('email'), data.get('code'), password=data.get('password'),
            username=data.get('username'), ip=ip(), request_id=g.request_id)
        return authenticated_response(user, message='账号已解冻，欢迎回来。')
