"""Fail closed on legacy regression selections until their LLM behavior is reviewed.

This is a review register, not a detector of arbitrary Python behavior. Reviewers
must include imported fixtures/helpers in the pinned dependency map. The register
never authorizes simulated inference, and no command-line bypass is provided.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTER = Path(__file__).with_name('regression-llm-review.json')


def violations(scripts, *, root=ROOT, register=REGISTER):
    root = root.resolve()
    try:
        reviews = json.loads(register.read_text(encoding='utf8'))['reviews']
    except (OSError, ValueError, KeyError):
        return ['LLM review register is missing or invalid; no suites may run']
    errors = []
    for script in scripts:
        try:
            name = script.resolve().relative_to(root).as_posix()
        except ValueError:
            errors.append(str(script) + ': outside platform root')
            continue
        review = reviews.get(name, {})
        if review.get('classification') != 'no_llm':
            errors.append(name + ': ' + review.get('reason', 'not yet reviewed for simulated LLM/CLI inference'))
            continue
        pins = review.get('sources', {})
        if name not in pins:
            errors.append(name + ': review does not pin its entrypoint')
            continue
        for relative, digest in pins.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                errors.append(name + ': reviewed source missing or changed: ' + relative)
    return errors


def enforce(scripts):
    errors = violations(scripts)
    if errors:
        raise SystemExit('REGRESSION_BLOCKED: AGENTS.md prohibits simulated LLM calls. '
                         'No selected suite was started. Review/migrate blocked suites first; '
                         'real DeepSeek acceptance has explicit live_llm_* entrypoints.\n' + '\n'.join(errors))
