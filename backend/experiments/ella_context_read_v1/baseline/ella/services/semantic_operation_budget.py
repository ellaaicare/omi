"""One exact trusted worker attempt's existing wall budget; no wire surface."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import time

_CURRENT = ContextVar('ella_semantic_operation_budget', default=None)


def attempt_scope(job):
    keys = ('job_id', 'uid', 'conversation_id', 'attempt_count', 'lease_token')
    values = tuple(job.get(key) for key in keys)
    if any(type(value) is not str or not value for value in values[:3] + values[4:]):
        raise ValueError('semantic_operation_scope_invalid')
    if type(values[3]) is not int or not 1 <= values[3] <= 3:
        raise ValueError('semantic_operation_scope_invalid')
    return values


@dataclass(frozen=True, init=False)
class OperationBudget:
    scope: tuple
    deadline: float
    clock: object

    def __init__(self, scope, seconds=600, *, clock=time.monotonic):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 600:
            raise ValueError('semantic_operation_budget_invalid')
        if type(scope) is not tuple or len(scope) != 5:
            raise ValueError('semantic_operation_scope_invalid')
        # Validate exactly the same scope as the trusted worker entry.
        if (
            attempt_scope(dict(zip(('job_id', 'uid', 'conversation_id', 'attempt_count', 'lease_token'), scope)))
            != scope
        ):
            raise ValueError('semantic_operation_scope_invalid')
        object.__setattr__(self, 'scope', scope)
        object.__setattr__(self, 'clock', clock)
        object.__setattr__(self, 'deadline', clock() + seconds)

    def remaining(self):
        value = self.deadline - self.clock()
        if value <= 0:
            raise TimeoutError('semantic_operation_budget_exhausted')
        return value

    def require_source(self, uid, conversation_id):
        if (uid, conversation_id) != self.scope[1:3]:
            raise ValueError('semantic_operation_scope_changed')
        self.remaining()


def current_budget():
    value = _CURRENT.get()
    if value is not None and type(value) is not OperationBudget:
        raise ValueError('semantic_operation_budget_invalid')
    return value


def remaining_timeout(cap):
    budget = current_budget()
    return min(cap, budget.remaining()) if budget is not None else cap


@contextmanager
def operation_budget_scope(job, seconds=600):
    scope = attempt_scope(job)
    existing = current_budget()
    if existing is not None:
        if existing.scope != scope:
            raise ValueError('semantic_operation_scope_changed')
        existing.remaining()
        yield existing
        return
    budget = OperationBudget(scope, seconds)
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(token)
