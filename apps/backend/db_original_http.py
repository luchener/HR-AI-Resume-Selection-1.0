"""Explicit opt-in original viewing and attachment download; audited per call."""
from flask import Response, g

from db_content_service import ContentError

_MIME = {'pdf': 'application/pdf',
         'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         'doc': 'application/msword'}


def register_original_routes(app, service, *, error, success):
    @app.errorhandler(ContentError)
    def original_error(exc):
        return error(exc.detail, exc.status)

    def view(identity, *, page=None):
        return service.view(str(g.preview_user.user_id), identity, page=page,
                            password_version=g.preview_user.password_version, request_id=g.request_id)

    @app.get('/api/v1/admin/resumes/<resume_id>/original')
    def original_info(resume_id):
        return success(view(resume_id))

    @app.get('/api/v1/admin/resumes/<resume_id>/original/pages/<int:page>')
    def original_page(resume_id, page):
        png, total = view(resume_id, page=page)
        response = Response(png, mimetype='image/png')
        response.headers.update({'Content-Disposition': 'inline', 'Cache-Control': 'private, no-store, max-age=0',
            'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'none',
            'X-Original-Page': str(page), 'X-Original-Pages': str(total)})
        return response

    @app.get('/api/v1/admin/resumes/<resume_id>/original/content')
    def original_content(resume_id):
        return success(service.preview_content(str(g.preview_user.user_id), resume_id,
                                               password_version=g.preview_user.password_version,
                                               request_id=g.request_id))

    @app.get('/api/v1/admin/resumes/<resume_id>/original/download')
    def original_download(resume_id):
        data, meta = service.download(str(g.preview_user.user_id), resume_id,
                                      password_version=g.preview_user.password_version,
                                      request_id=g.request_id)
        ext = str(meta.get('ext', '')).lower()
        name = str(meta.get('name') or (resume_id + '.' + ext))
        from io import BytesIO
        from flask import send_file
        response = send_file(BytesIO(data), mimetype=_MIME.get(ext, 'application/octet-stream'),
                             as_attachment=True, download_name=name, max_age=0)
        response.headers.update({'Cache-Control': 'private, no-store, max-age=0', 'Pragma': 'no-cache',
                                 'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'none',
                                 'X-Original-SHA256': str(meta.get('sha256', ''))})
        return response
