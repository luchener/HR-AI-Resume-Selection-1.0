"""Local SQL content routes, registered only with explicit ContentService."""
from flask import g, jsonify, request

from db_content_service import ContentError
from db_original_objects import MAX_ORIGINAL_BYTES


def register_content_routes(app, service, *, error, success):
    def actor():
        return str(g.preview_user.user_id)

    def arguments():
        return dict(password_version=g.preview_user.password_version, request_id=g.request_id)

    def payload():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            raise ContentError('Request body must be an object')
        return data

    def message(text):
        return jsonify(request_id=g.request_id, message=text)

    @app.errorhandler(ContentError)
    def content_error(exc):
        return error(exc.detail, exc.status)

    @app.post('/api/v1/resumes/upload')
    def content_upload_resume():
        file = request.files.get('file')
        if file is None or not file.filename:
            return error('No file provided', 400)
        # A bounded read also covers chunked bodies without Content-Length.
        data = file.stream.read(MAX_ORIGINAL_BYTES + 1)
        result = service.upload_resume(actor(), data, file.filename, **arguments())
        return jsonify(request_id=g.request_id, message='Resume uploaded and processed as MD successfully', **result)

    @app.post('/api/v1/jobs/upload')
    def content_upload_jobs():
        if request.mimetype != 'application/json':
            return error('Content-Type must be application/json', 400)
        data = payload()
        ids = service.upload_jobs(actor(), data.get('resume_id'), data.get('job_descriptions'), **arguments())
        return jsonify(request_id=g.request_id, message='data successfully processed', job_id=ids)

    @app.post('/api/v1/archives')
    def content_create_archive():
        from db_http_preview import _archive_payload
        row, created = service.create_archive(actor(), payload(), **arguments())
        return jsonify(request_id=g.request_id, message='归档成功' if created else '该候选人已归档',
                       data=_archive_payload(row)), 201 if created else 200

    def mutate(identity, action, value=None):
        return service.mutate_archive(actor(), identity, action, value, **arguments())

    @app.patch('/api/v1/archives/<archive_id>/tags')
    def content_tags(archive_id):
        from db_http_preview import _archive_payload
        return success(_archive_payload(mutate(archive_id, 'tags', payload().get('custom_tags'))))

    @app.patch('/api/v1/archives/<archive_id>/category')
    def content_category(archive_id):
        from db_http_preview import _archive_payload
        return success(_archive_payload(mutate(archive_id, 'category', payload().get('category'))))

    @app.delete('/api/v1/archives/<archive_id>')
    def content_trash(archive_id):
        mutate(archive_id, 'trash')
        return message('已移入回收站，可在回收站恢复')

    @app.post('/api/v1/archives/<archive_id>/restore')
    def content_restore(archive_id):
        mutate(archive_id, 'restore')
        return message('已恢复到人才库')

    @app.delete('/api/v1/archives/trash/<archive_id>')
    def content_purge(archive_id):
        mutate(archive_id, 'purge')
        return message('已彻底删除')

    @app.delete('/api/v1/archives/trash')
    def content_empty():
        count = service.empty_trash(actor(), **arguments())
        return message(f'已清空回收站（{count} 条）')

    def delete_source(kind, identity):
        count = service.delete_source(actor(), kind, identity, **arguments())
        return success({'message': '简历已删除。' if kind == 'resume' else '岗位已删除。',
                        kind + '_id': identity, 'archived_to_trash': count})

    @app.delete('/api/v1/resumes/<resume_id>')
    def content_delete_resume(resume_id):
        return delete_source('resume', resume_id)

    @app.delete('/api/v1/jobs/<job_id>')
    def content_delete_job(job_id):
        return delete_source('job', job_id)
