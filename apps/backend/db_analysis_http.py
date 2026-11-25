"""Explicit analysis routes for the local DB rehearsal app, never production."""
import json
from threading import Event
from time import monotonic

from flask import Response, g, request

from db_analysis_workflow import TERMINAL, WorkflowError
from db_drain import DrainingError
from db_quota import QuotaDenied


def register_analysis_routes(app, workflow, error, success):
    @app.errorhandler(WorkflowError)
    def workflow_error(exc):
        return error(str(exc), exc.status_code)

    @app.errorhandler(QuotaDenied)
    def quota_error(exc):
        return error(str(exc), exc.status_code)

    @app.errorhandler(DrainingError)
    def draining_error(exc):
        return error('Analysis preview is draining', 503)

    def read_task(task_id, user):
        return workflow.snapshot(task_id=task_id, owner_user_id=user.user_id,
                                 password_version=user.password_version)

    def finish(snapshot, request_id):
        # Read only committed persisted results, never an in-memory model return.
        if snapshot['status'] == 'failed' or snapshot['completed_candidates'] == 0:
            return {'request_id': request_id, 'data': snapshot,
                    'detail': 'All analyses failed or were stopped'}, 502
        return {'request_id': request_id, 'data': snapshot}, 200

    @app.get('/api/v1/analysis-tasks/<task_id>')
    def analysis_task(task_id):
        try:
            return success(read_task(task_id, g.preview_user))
        except ValueError:
            return error('Invalid UUID', 400)

    @app.post('/api/v1/resumes/hr-analysis')
    def analysis(kind='screening'):
        data = request.get_json(silent=True)
        allowed = {'resume_id', 'job_id'} if kind == 'improvement' else {'resume_id', 'resume_ids', 'job_id', 'agent_config'}
        if (not isinstance(data, dict) or set(data) - allowed or
                ('resume_id' in data and 'resume_ids' in data)):
            return error('Provide only resume_id/resume_ids and job_id', 422)
        options = data.get('agent_config', {})
        if type(options) is not dict or set(options) - {'web_search'} or type(options.get('web_search', False)) is not bool:
            return error('Provide a boolean web_search option', 422)
        if request.args.get('stream', 'false').lower() not in {'true', 'false', '1', '0', 'yes', 'no'}:
            return error('Invalid stream mode', 422)
        streaming = request.args.get('stream', 'false').lower() in {'true', '1', 'yes'}
        close_stream = workflow.tracker.enter('streams') if streaming else None
        user, request_id = g.preview_user, g.request_id
        try:
            try:
                task_id, future = workflow.submit(
                    owner_user_id=user.user_id, password_version=user.password_version,
                    request_id=request.headers.get('Idempotency-Key'), job_id=data.get('job_id'),
                    resume_ids=data.get('resume_ids') if 'resume_ids' in data else [data.get('resume_id')],
                    kind=kind, web_search=options.get('web_search', False))
            except ValueError:
                return error('Valid job/resume UUIDs and an Idempotency-Key UUID are required', 422)
            if not streaming:
                deadline = monotonic() + 15
                while True:
                    snapshot = read_task(task_id, user)
                    if snapshot['status'] in TERMINAL:
                        payload, status = finish(snapshot, request_id)
                        return payload, status
                    if future.done():
                        # Infrastructure failures leave durable unfinished state.
                        future.result()
                    if monotonic() >= deadline:
                        return {'request_id': request_id, 'data': snapshot}, 202
                    Event().wait(0.05)

            stream_close = close_stream
            def events():
                def sse(payload):
                    return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
                try:
                    yield sse({'status': 'starting', 'task_id': task_id})
                    cursor = 0
                    while True:
                        page = workflow.event_page(task_id=task_id, owner_user_id=user.user_id,
                            password_version=user.password_version, after=cursor)
                        for event in page['events']:
                            yield sse(event)
                        cursor = page['next_cursor']
                        if page['has_more']:
                            continue
                        snapshot = read_task(task_id, user)
                        if snapshot['status'] in TERMINAL:
                            # A worker may have committed between page and
                            # snapshot. Drain its final events before completion.
                            final_page = workflow.event_page(task_id=task_id, owner_user_id=user.user_id,
                                password_version=user.password_version, after=cursor)
                            if final_page['events']:
                                for event in final_page['events']:
                                    yield sse(event)
                                cursor = final_page['next_cursor']
                                if final_page['has_more']:
                                    continue
                            payload, status = finish(snapshot, request_id)
                            yield sse({'status': 'completed' if status == 200 else 'error',
                                       'result': payload})
                            return
                        if future.done():
                            future.result()
                        yield sse({'status': 'processing', 'task_id': task_id,
                                   'completed_candidates': snapshot['completed_candidates'],
                                   'failed_candidates': snapshot['failed_candidates']})
                        Event().wait(0.2)
                except Exception:
                    # In particular, never expose provider/SQL exception details.
                    yield sse({'status': 'error', 'task_id': task_id,
                               'message': 'Analysis preview unavailable; check persisted task state'})
                finally:
                    stream_close()
            response = Response(events(), mimetype='text/event-stream', headers={
                'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})
            response.call_on_close(stream_close)
            close_stream = None  # Ownership moved to generator/response callbacks.
            return response
        finally:
            if close_stream is not None:
                close_stream()

    app.add_url_rule('/api/v1/resumes/improve', endpoint='improvement_task', view_func=analysis,
                     methods=['POST'], defaults={'kind': 'improvement'})

    @app.get('/api/v1/analysis-tasks/<task_id>/events')
    def analysis_task_events(task_id):
        user = g.preview_user
        if request.args.get('stream', 'false') not in {'true', 'false'}:
            return error('Invalid stream mode', 422)
        raw = request.headers.get('Last-Event-ID', request.args.get('after', '0'))
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 3:
            return error('Invalid event cursor', 422)
        cursor = int(raw)
        try:
            first = workflow.event_page(task_id=task_id, owner_user_id=user.user_id,
                                       password_version=user.password_version, after=cursor)
        except ValueError:
            return error('Invalid task or event cursor', 422)
        if request.args.get('stream', 'false') != 'true':
            return success(first)
        close = workflow.tracker.enter('streams')
        def replay():
            position, page = cursor, first
            deadline = monotonic() + 30
            try:
                while True:
                    for event in page['events']:
                        yield 'id: ' + str(event['id']) + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                    position = page['next_cursor']
                    if not page['has_more'] and page['status'] in TERMINAL:
                        snapshot = read_task(task_id, user)
                        payload, status = finish(snapshot, g_request_id)
                        yield 'data: ' + json.dumps({'status': 'completed' if status == 200 else 'error',
                            'result': payload}, ensure_ascii=False) + '\n\n'
                        return
                    if monotonic() >= deadline:
                        return
                    if not page['has_more']:
                        yield ': keepalive\n\n'
                        Event().wait(0.2)
                    page = workflow.event_page(task_id=task_id, owner_user_id=user.user_id,
                        password_version=user.password_version, after=position)
            except Exception:
                yield 'data: {"status":"error","message":"Task events unavailable"}\n\n'
            finally:
                close()
        g_request_id = g.request_id
        response = Response(replay(), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        response.call_on_close(close)
        return response
