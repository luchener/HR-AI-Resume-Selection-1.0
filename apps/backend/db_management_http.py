"""Opt-in local account/config routes; only explicitly supplied DB services."""
from flask import g, request

from db_accounts import AccountError
from db_config_service import ConfigPermissionError, ConfigValidationError
from db_config_diagnostics import ConfigProbeError


def register_management_routes(app, account_service, config_service, *, config_writes, error, success, config_diagnostics=None):
    def actor_arguments():
        return {'password_version': g.preview_user.password_version, 'request_id': g.request_id}

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise AccountError('请求体必须是对象。')
        return value

    @app.errorhandler(AccountError)
    def account_error(exc):
        return error(exc.detail, exc.status)

    @app.errorhandler(ConfigPermissionError)
    def config_permission(exc):
        return error('Superadministrator required', 403)

    @app.errorhandler(ConfigValidationError)
    def config_invalid(exc):
        return error('配置字段不合法。', 422)

    @app.errorhandler(ConfigProbeError)
    def config_probe_error(exc):
        return error(exc.detail, exc.status)

    if config_diagnostics is not None:
        def probe(kind):
            data = payload() if request.get_data() else {}
            return success(config_diagnostics.run(str(g.preview_user.user_id), kind, data, **actor_arguments()))

        @app.post('/api/v1/admin/system/mail/test')
        def mail_probe():
            return probe('mail')

        @app.post('/api/v1/admin/system/llm/test')
        def llm_probe():
            return probe('llm')

        @app.post('/api/v1/admin/system/llm/models')
        def llm_models():
            return probe('models')

    if account_service is not None:
        @app.post('/api/v1/auth/change-password')
        def account_password():
            data = payload()
            return success(account_service.change_password(str(g.preview_user.user_id),
                data.get('old_password'), data.get('new_password'), **actor_arguments()))

        @app.get('/api/v1/admin/users')
        def account_list():
            def integer(name, default, maximum=None):
                try:
                    value = max(1, int(request.args.get(name, default)))
                    return min(maximum, value) if maximum else value
                except (ValueError, TypeError):
                    return default
            return success(account_service.list_accounts(str(g.preview_user.user_id),
                password_version=g.preview_user.password_version, keyword=request.args.get('keyword', '').strip(),
                page=integer('page', 1), size=integer('size', 50, 200)))

        @app.post('/api/v1/admin/users')
        def account_create():
            return success(account_service.create_account(str(g.preview_user.user_id), payload(), **actor_arguments())), 201

        @app.patch('/api/v1/admin/users/<username>')
        def account_update(username):
            return success(account_service.update_account(str(g.preview_user.user_id), username, payload(), **actor_arguments()))

        @app.post('/api/v1/admin/users/<username>/reset-password')
        def account_reset(username):
            return success(account_service.reset_password(str(g.preview_user.user_id), username, **actor_arguments()))

        @app.post('/api/v1/admin/users/<username>/freeze')
        def account_freeze(username):
            data = payload() if request.get_data() else {}
            if set(data) - {'reason'}:
                raise AccountError('冻结字段不合法。')
            return success(account_service.set_frozen(str(g.preview_user.user_id), username, True,
                reason=data.get('reason', ''), **actor_arguments()))

        @app.post('/api/v1/admin/users/<username>/unfreeze')
        def account_unfreeze(username):
            return success(account_service.set_frozen(str(g.preview_user.user_id), username, False, **actor_arguments()))

        @app.delete('/api/v1/admin/users/<username>')
        def account_delete(username):
            data = payload() if request.get_data() else {}
            return success(account_service.delete_account(str(g.preview_user.user_id), username,
                data.get('admin_password'), **actor_arguments()))

        @app.post('/api/v1/admin/users/<username>/restore')
        def account_restore(username):
            return success(account_service.restore_account(str(g.preview_user.user_id), username, **actor_arguments()))

        @app.get('/api/v1/admin/users/frozen')
        def account_frozen_list():
            return success(account_service.list_lifecycle(str(g.preview_user.user_id), 'frozen',
                password_version=g.preview_user.password_version))

        @app.get('/api/v1/admin/users/deleted')
        def account_deleted_list():
            return success(account_service.list_lifecycle(str(g.preview_user.user_id), 'deleted',
                password_version=g.preview_user.password_version))

    if config_writes:
        def config_payload(document):
            value = payload()
            def fields(source, secret, number):
                source = dict(source)
                if 'enabled' in source and type(source['enabled']) is not bool:
                    raise ConfigValidationError('Invalid enabled flag')
                if number in source and (type(source[number]) is bool or not isinstance(source[number], (str, int))):
                    raise ConfigValidationError('Invalid numeric field')
                if secret in source:
                    current = source[secret]
                    if current is not None and not isinstance(current, str):
                        raise ConfigValidationError('Invalid secret type')
                    if isinstance(current, str) and current.strip() in {'********', '\u2022' * 8}:
                        source[secret] = ''
                return source
            if document == 'llm':
                return fields(value, 'api_key', 'timeout')
            slots = value.get('slots')
            if isinstance(slots, dict):
                value['slots'] = {name: fields(slot, 'password', 'port') if isinstance(slot, dict) else slot
                                  for name, slot in slots.items()}
            return value

        def modify(document, value):
            return success(config_service.update(str(g.preview_user.user_id), document, value, **actor_arguments()))

        @app.put('/api/v1/admin/system/mail')
        def mail_update():
            return modify('mail', config_payload('mail'))

        @app.delete('/api/v1/admin/system/mail')
        def mail_clear():
            return modify('mail', None)

        @app.put('/api/v1/admin/system/llm')
        def llm_update():
            return modify('llm', config_payload('llm'))

        @app.delete('/api/v1/admin/system/llm')
        def llm_clear():
            return modify('llm', None)
