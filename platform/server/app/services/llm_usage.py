"""Normalize provider counters without borrowing from estimates or earlier calls."""

MAX_COUNT = 2**53 - 1
FIELDS = ('input_tokens', 'uncached_input_tokens', 'cache_read_input_tokens',
          'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
CORE = {'input_tokens', 'output_tokens', 'total_tokens'}


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def _object(value):
    return value if isinstance(value, dict) else {}


def validate(value):
    """Expose only count fields. Zero and an absent breakdown stay distinct."""
    if not isinstance(value, dict):
        return None
    counts = {key: _count(value.get(key)) for key in FIELDS}
    if any(counts[key] is None for key in CORE):
        return None
    if counts['input_tokens'] + counts['output_tokens'] != counts['total_tokens']:
        return None
    input_parts = FIELDS[1:4]
    if sum(counts[key] or 0 for key in input_parts) > counts['input_tokens']:
        return None
    if (counts['reasoning_output_tokens'] or 0) > counts['output_tokens']:
        return None
    return counts


def chat_completion(raw):
    raw = _object(raw)
    prompt = _count(raw.get('prompt_tokens'))
    output = _count(raw.get('completion_tokens'))
    if prompt is None or output is None:
        return None
    read = raw.get('prompt_cache_hit_tokens', _object(raw.get('prompt_tokens_details')).get('cached_tokens'))
    write = _object(raw.get('prompt_tokens_details')).get('cache_write_tokens')
    uncached = raw.get('prompt_cache_miss_tokens')
    if uncached is None and _count(read) is not None:
        uncached = prompt - read - (_count(write) or 0)
    return validate({'input_tokens':prompt, 'uncached_input_tokens':uncached, 'cache_read_input_tokens':read,
        'cache_write_input_tokens':write, 'output_tokens':output,
        'reasoning_output_tokens':_object(raw.get('completion_tokens_details')).get('reasoning_tokens'),
        'total_tokens':raw.get('total_tokens', prompt+output)})


def messages(raw):
    raw = _object(raw)
    uncached = _count(raw.get('input_tokens'))
    output = _count(raw.get('output_tokens'))
    if uncached is None or output is None:
        return None
    read = _count(raw.get('cache_read_input_tokens'))
    write = _count(raw.get('cache_creation_input_tokens'))
    if ((raw.get('cache_read_input_tokens') is not None and read is None)
            or (raw.get('cache_creation_input_tokens') is not None and write is None)):
        return None
    prompt = uncached + (read or 0) + (write or 0)
    return validate({'input_tokens':prompt, 'uncached_input_tokens':uncached, 'cache_read_input_tokens':read,
        'cache_write_input_tokens':write, 'output_tokens':output,
        'reasoning_output_tokens':_object(raw.get('output_tokens_details')).get('reasoning_tokens'),
        'total_tokens':prompt+output})
