"""Select a checkpoint under the serial solver's sealed restart policy."""

import json

from .case_execution_settings import RESTART_POLICIES
from .workflow_state import ExecutionConflict


def policy_for(descriptor):
    policy=descriptor.get('restart_policy','explicit')
    if policy not in RESTART_POLICIES:
        raise ExecutionConflict('evidence_invalid','原求解尝试的续算策略无效')
    return policy


def preview(attempt):
    try:
        return policy_for(json.loads(attempt.publication_json)['descriptor'])
    except (ValueError,TypeError,KeyError) as error:
        raise ExecutionConflict('evidence_invalid','原求解尝试的续算策略无法读取') from error


def select_time(descriptor, requested):
    policy=policy_for(descriptor)
    if policy=='disabled':
        raise ExecutionConflict('checkpoint_disabled','原求解尝试的算例策略禁用了检查点续算')
    if requested=='latest' and policy!='latestWritten':
        raise ExecutionConflict('checkpoint_required','原求解策略要求提供明确写出的时刻')
    return requested
