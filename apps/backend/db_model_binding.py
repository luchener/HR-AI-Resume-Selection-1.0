"""Bind non-secret audit metadata under the live task/worker fence."""
import re
from uuid import UUID
import sqlalchemy as sa
from db_task_inputs import task_inputs
from db_task_lifecycle import _RECOVERY_FENCES, _require_tx, _task


def bind_model(conn, *, task_id, owner_user_id, worker_token, binding):
    _require_tx(conn)
    if type(binding) is not dict or set(binding) != {'binding_id', 'config_revision', 'policy_revision'}:
        raise ValueError('Invalid model binding')
    if not isinstance(binding['binding_id'], str) or str(UUID(binding['binding_id'])) != binding['binding_id']:
        raise ValueError('Invalid binding identifier')
    if any(not isinstance(binding[key], str) or not re.fullmatch('[0-9a-f]{64}', binding[key])
           for key in ('config_revision', 'policy_revision')):
        raise ValueError('Invalid binding revision')
    task = _task(conn, task_id, owner_user_id)
    if task is None or task['status'] != 'running' or task['error_code'] in _RECOVERY_FENCES:
        raise ValueError('Task cannot bind model settings')
    changed = conn.execute(task_inputs.update().where(task_inputs.c.task_id == task_id,
        task_inputs.c.owner_user_id == owner_user_id, task_inputs.c.worker_token == worker_token,
        task_inputs.c.lease_until > sa.func.clock_timestamp(), task_inputs.c.model_binding.is_(None)
    ).values(model_binding=binding))
    if changed.rowcount != 1:
        raise ValueError('Model binding requires an unbound live worker claim')
