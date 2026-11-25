'''Exact unfinished-task checks for source deletion.

An analysis task is relevant to a source only when durable task data ties it
to that source. Owner-wide blocking is intentionally avoided: otherwise an
unrelated running analysis prevents every resume and job from being deleted.
'''
import sqlalchemy as sa

from db_models import Analysis, AnalysisTask
from db_task_inputs import task_inputs


UNFINISHED_STATUSES = ('pending', 'running')


def has_unlinked_legacy_unfinished_task(session, owner_user_id, kind):
    '''Return whether an unfinished task has no durable source scope.

    Legacy tasks predating task_inputs have neither a frozen input snapshot nor
    an Analysis row. Their resume scope cannot be determined, so source deletion
    must fail closed rather than silently allowing a worker/source orphan.
    '''
    if kind not in {'resume', 'job'}:
        raise ValueError('Invalid source type')

    task_input_match = sa.exists().where(
        task_inputs.c.task_id == AnalysisTask.id,
        task_inputs.c.owner_user_id == owner_user_id,
    )
    analysis_match = sa.exists().where(
        Analysis.task_id == AnalysisTask.id,
        Analysis.owner_user_id == owner_user_id,
    )
    conditions = [
        AnalysisTask.owner_user_id == owner_user_id,
        AnalysisTask.status.in_(UNFINISHED_STATUSES),
        ~task_input_match,
        ~analysis_match,
    ]
    if kind == 'job':
        # A legacy task with job_id is already covered by direct_match. Only a
        # task with no job identity needs an owner-wide job-deletion block.
        conditions.append(AnalysisTask.job_id.is_(None))
    statement = sa.select(sa.exists().where(*conditions))
    return bool(session.scalar(statement))


def has_unfinished_task_for_source(session, owner_user_id, kind, identity):
    '''Return whether an unfinished task has a durable link to one source.'''
    if kind not in {'resume', 'job'}:
        raise ValueError('Invalid source type')

    if has_unlinked_legacy_unfinished_task(session, owner_user_id, kind):
        return True

    snapshot = task_inputs.c.snapshot
    if kind == 'resume':
        snapshot_match = snapshot['resumes'].contains(
            [{'resume_id': str(identity)}]
        )
        analysis_identity = Analysis.resume_id == identity
        direct_match = sa.false()
    else:
        snapshot_match = snapshot['job']['job_id'].astext == str(identity)
        analysis_identity = Analysis.job_id == identity
        direct_match = AnalysisTask.job_id == identity

    task_input_match = sa.exists().where(
        task_inputs.c.task_id == AnalysisTask.id,
        task_inputs.c.owner_user_id == owner_user_id,
        snapshot_match,
    )
    analysis_match = sa.exists().where(
        Analysis.task_id == AnalysisTask.id,
        Analysis.owner_user_id == owner_user_id,
        analysis_identity,
    )
    statement = sa.select(sa.exists().where(
        AnalysisTask.owner_user_id == owner_user_id,
        AnalysisTask.status.in_(UNFINISHED_STATUSES),
        sa.or_(direct_match, task_input_match, analysis_match),
    ))
    return bool(session.scalar(statement))
