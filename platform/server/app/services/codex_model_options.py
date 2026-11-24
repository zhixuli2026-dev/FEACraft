"""Bounded native turn choices and public CLI catalog fields."""
import re

OPTION_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


def normalize(value):
    if value is None:
        return {}
    if hasattr(value, 'model_dump'):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict) or set(value) - {'effort', 'summary', 'service_tier'}:
        raise ValueError('Invalid native model options')
    result = {}
    for key, selected in value.items():
        if selected is None:
            continue
        if not isinstance(selected, str) or OPTION_TOKEN.fullmatch(selected) is None:
            raise ValueError('Invalid native model option value')
        if key == 'summary' and selected not in {'auto', 'concise', 'detailed', 'none'}:
            raise ValueError('Invalid native reasoning summary')
        result[key] = selected
    return result


def protocol(value):
    return {{'effort': 'effort', 'summary': 'summary', 'service_tier': 'serviceTierForTurn'}[key]: selected
            for key, selected in normalize(value).items()}


def public_model(item):
    fields = {'id', 'model', 'displayName', 'description', 'isDefault', 'defaultReasoningEffort',
              'hidden', 'inputModalities', 'defaultServiceTier'}
    result = {key: value for key, value in item.items() if key in fields}
    for key, value in result.items():
        if key in {'isDefault', 'hidden'}:
            valid = type(value) is bool
        elif key == 'inputModalities':
            valid = isinstance(value, list) and len(value) <= 16 and all(isinstance(v, str) and len(v) <= 80 for v in value)
        else:
            valid = (isinstance(value, str) and len(value) <= 16384) or (key == 'defaultServiceTier' and value is None)
        if not valid:
            raise ValueError('Invalid native model metadata')
    for name, allowed in (('supportedReasoningEfforts', {'reasoningEffort', 'description'}),
                          ('serviceTiers', {'id', 'name', 'description'})):
        if name in item:
            values = item[name]
            if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, dict) for v in values):
                raise ValueError('Invalid native model capabilities')
            result[name] = [{key: value for key, value in v.items() if key in allowed} for v in values]
            if any(not isinstance(value, str) or len(value) > 16384 for v in result[name] for value in v.values()):
                raise ValueError('Invalid native model capability value')
    return result
