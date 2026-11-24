"""Select whole conversation turns before loading attachments or sending inference."""
from dataclasses import dataclass
import json
import os
from urllib.parse import urlsplit


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 44 * 1024 * 1024
DEEPSEEK_WINDOW = 1_000_000
DEEPSEEK_OUTPUT = 384_000
DEEPSEEK_IMAGES = 1024
DEEPSEEK_MODELS = {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}


class ContextTooLarge(ValueError):
    pass


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


@dataclass(frozen=True)
class Budget:
    model_window: int | None
    output: int | None
    input_limit: int | None
    image_tokens: int


def budget_for(endpoint, model):
    known = urlsplit(endpoint).hostname == 'api.deepseek.com' and model in DEEPSEEK_MODELS
    window = DEEPSEEK_WINDOW if known else None
    output = DEEPSEEK_OUTPUT if known else None
    limit = window - output - 4096 if known else None
    # A deployment can choose a tighter preflight estimate for a known or custom model.
    configured = os.getenv('VIBEFLOW_AGENT_CONTEXT_INPUT_TOKEN_CAP', '').strip()
    if configured:
        requested = int(configured)
        if not 1024 <= requested <= 2_000_000:
            raise ValueError('VIBEFLOW_AGENT_CONTEXT_INPUT_TOKEN_CAP is outside the supported range')
        limit = min(requested, limit) if limit is not None else requested
    return Budget(window, output, limit, DEEPSEEK_IMAGES if known else 16384)


def _weight(events, image_tokens):
    wire = estimated = 0
    for event in events:
        payload = event['payload']
        image_rows = payload.get('images') or []
        text = _size({k: v for k, v in payload.items() if k != 'images'}) + 64
        bytes_for_images = 0
        for image in image_rows:
            encoded = image.get('data')
            size = image.get('size')
            if isinstance(encoded, str):
                bytes_for_images += len(encoded) + 128
            else:
                raw_bytes = size if type(size) is int and 0 < size <= MAX_IMAGE_BYTES else MAX_IMAGE_BYTES
                bytes_for_images += 4 * ((raw_bytes + 2) // 3) + 128
        wire += text + bytes_for_images
        estimated += text + image_tokens * len(image_rows)
    return estimated, wire


def select(events, budget, system, tools):
    """Prefer current input and whole recent turns; retain a summary when it fits."""
    groups = []
    summary = None
    for event in events:
        kind = event['type']
        role = event['payload'].get('role')
        if kind == 'message' and role == 'user' and event['id'] == 0:
            summary = event
        elif kind == 'message' and role == 'user':
            groups.append([event])
        elif groups and (kind == 'tool_result' or (kind == 'message' and role in ('assistant', 'system'))):
            groups[-1].append(event)

    used = _size(system) + _size(tools) + 256
    wire = used

    def fits(tokens, size):
        return wire + size <= MAX_REQUEST_BYTES and (budget.input_limit is None or used + tokens <= budget.input_limit)

    if groups:
        tokens, size = _weight(groups[-1], budget.image_tokens)
        if not fits(tokens, size):
            raise ContextTooLarge('当前消息及其工具结果超出模型输入预算')
        used += tokens
        wire += size
    elif not fits(0, 0):
        raise ContextTooLarge('系统上下文超出模型输入预算')

    keep_summary = False
    if summary is not None:
        tokens, size = _weight([summary], budget.image_tokens)
        if fits(tokens, size):
            used += tokens
            wire += size
            keep_summary = True

    start = max(0, len(groups) - 1)
    for group in reversed(groups[:-1]):
        tokens, size = _weight(group, budget.image_tokens)
        if not fits(tokens, size):
            break
        used += tokens
        wire += size
        start -= 1

    selected = [summary] if keep_summary else []
    selected.extend(event for group in groups[start:] for event in group)
    return selected, {'input_estimate': used, 'input_limit': budget.input_limit, 'wire_estimate': wire,
                      'model_window': budget.model_window, 'output_reserve': budget.output,
                      'retained_turns': len(groups) - start, 'excluded_turns': start,
                      'summary_included': keep_summary}
