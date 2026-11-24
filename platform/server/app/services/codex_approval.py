"""Version-matched approval replies; grants cannot exceed the pending request."""
import copy
import json

METHODS = {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
           'item/permissions/requestApproval', 'item/tool/requestUserInput'}
DECISIONS = {'accept', 'acceptForSession', 'decline', 'cancel'}


def request_key(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or (isinstance(value, str) and not 1 <= len(value) <= 128):
        raise ValueError('Invalid approval request ID')
    if isinstance(value, int) and not -(2**63) <= value < 2**63:
        raise ValueError('Invalid numeric approval ID')
    return ('s:' if isinstance(value, str) else 'n:') + str(value)


def subset(granted, requested):
    if isinstance(granted, dict) and isinstance(requested, dict):
        return all(key in requested and subset(value, requested[key]) for key, value in granted.items())
    if isinstance(granted, list) and isinstance(requested, list):
        return len(granted) <= len(requested) and all(item in requested for item in granted)
    return type(granted) is type(requested) and granted == requested


def response(method, params, body):
    """Return native reply, public audit reply and new secret values."""
    if method not in METHODS or not isinstance(body, dict):
        raise ValueError('Unsupported approval response')
    secrets = []
    if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
        if set(body) != {'decision'}:
            raise ValueError('Command/file approval requires a decision')
        decision = body['decision']
        if isinstance(decision, str):
            if decision not in DECISIONS:
                raise ValueError('Unknown approval decision')
            available = params.get('availableDecisions')
            if available is not None and decision not in available:
                raise ValueError('Decision was not offered by this request')
        elif method == 'item/commandExecution/requestApproval' and isinstance(decision, dict):
            if set(decision) == {'acceptWithExecpolicyAmendment'}:
                expected = {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': params.get('proposedExecpolicyAmendment')}}
                if params.get('proposedExecpolicyAmendment') is None or decision != expected:
                    raise ValueError('Exec policy amendment differs from the proposed rule')
            elif set(decision) == {'applyNetworkPolicyAmendment'}:
                value = decision['applyNetworkPolicyAmendment']
                if (not isinstance(value, dict) or set(value) != {'network_policy_amendment'}
                        or value['network_policy_amendment'] not in (params.get('proposedNetworkPolicyAmendments') or [])):
                    raise ValueError('Network policy rule was not proposed')
            else:
                raise ValueError('Unknown command policy decision')
        else:
            raise ValueError('Invalid approval decision')
        result = {'decision': copy.deepcopy(decision)}
        return result, result, secrets
    if method == 'item/permissions/requestApproval':
        if not set(body) <= {'permissions', 'scope', 'strictAutoReview'} or 'permissions' not in body:
            raise ValueError('Permission approval requires a permission profile')
        permissions = body['permissions']
        if not isinstance(permissions, dict) or not subset(permissions, params.get('permissions', {})):
            raise ValueError('Permission grant exceeds the requested profile')
        if body.get('scope', 'turn') not in {'turn', 'session'}:
            raise ValueError('Invalid permission grant scope')
        if body.get('strictAutoReview') is not None and type(body['strictAutoReview']) is not bool:
            raise ValueError('Invalid strict review selection')
        result = {'permissions': copy.deepcopy(permissions), 'scope': body.get('scope', 'turn')}
        if 'strictAutoReview' in body:
            result['strictAutoReview'] = body['strictAutoReview']
        return result, result, secrets
    if set(body) != {'answers'} or not isinstance(body['answers'], dict):
        raise ValueError('User input requires question answers')
    questions = params.get('questions', [])
    if (not 1 <= len(questions) <= 64 or len({question.get('id') for question in questions}) != len(questions)
            or set(body['answers']) != {question.get('id') for question in questions}):
        raise ValueError('Answers must match every pending question ID')
    answers, public = {}, {}
    for question in questions:
        identifier = question['id']
        values = body['answers'][identifier]
        if (not isinstance(values, list) or not 1 <= len(values) <= 8
                or any(not isinstance(value, str) or not value.strip() or len(value) > 4096 or '\0' in value for value in values)):
            raise ValueError('Invalid user input answer')
        options = question.get('options')
        if options and not question.get('isOther') and not question.get('isSecret') and any(value not in {item['label'] for item in options} for value in values):
            raise ValueError('Answer is outside the offered choices')
        answers[identifier] = {'answers': list(values)}
        if question.get('isSecret'):
            secrets.extend(values)
            public[identifier] = {'answered': True, 'secret': True}
        else:
            public[identifier] = {'answers': list(values)}
    return {'answers': answers}, {'answers': public}, secrets


def bounded(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')) > 64 * 1024:
        raise ValueError('Approval exceeds its size limit')
    return value
