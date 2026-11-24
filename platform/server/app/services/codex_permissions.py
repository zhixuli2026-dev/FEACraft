"""Map conversation permissions to native thread policy."""

READ_ONLY = frozenset({'ask', 'plan'})

THREAD_POLICIES = {
    'ask': ('on-request', 'read-only'),
    'plan': ('on-request', 'read-only'),
    'approve_writes': ('on-request', 'workspace-write'),
    'full_access': ('never', 'danger-full-access'),
}

SANDBOX_TYPES = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                 'danger-full-access': 'dangerFullAccess'}


def turn_policy(permission, *, docker=False):
    approval, sandbox = THREAD_POLICIES[permission]
    if docker and permission not in READ_ONLY:
        return {'approval_policy':approval,'sandbox_policy':{'type':'externalSandbox','networkAccess':'restricted'}}
    return {'approval_policy': approval, 'sandbox_policy': {'type': SANDBOX_TYPES[sandbox]}}
