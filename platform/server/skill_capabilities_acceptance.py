"""Check published skill capability statements and detect actual local tools."""
import json
from pathlib import Path
import sys

from testsupport import IsolatedServer, SERVER


def verify():
    api = IsolatedServer('ff-f66-skill-capabilities')
    receipt = {'passed': False, 'model_calls': 0, 'published': {}}
    try:
        with api:
            api.call('GET', '/skills', expect=401)
            api.create_case('Capability statements')
            rows = api.call('GET', '/skills')['skills']
            bundled = {path.parent.name for path in (SERVER / 'skills').glob('*/SKILL.md')}
            assert len(rows) == len(bundled) == 41
            assert {row['name'] for row in rows} == bundled
            for row in rows:
                name = row['name']
                detail = api.call('GET', '/skills/' + name)
                support, = [line.removeprefix('**支持范围**　') for line in detail['body'].splitlines()
                            if line.startswith('**支持范围**　')]
                reference = support.startswith('自配环境参考。')
                assert reference == ('自配环境参考' in row['description']), name
                if reference and 'default_prompt' in row:
                    assert 'verify' in row['default_prompt'], name
                receipt['published'][name] = {'reference': reference, 'support': support}
            assert sum(item['reference'] for item in receipt['published'].values()) == 24
            for name, expected in {'vibeflow-moose': ('moose:solve', '`.exodus`'),
                                   'vibeflow-code-aster': ('run_aster xxx.export', 'code-aster:solve'),
                                   'vibeflow-PPT': ('ppt:build',),
                                   'vibeflow-latex': ('latex:build', '-no-shell-escape -norc')}.items():
                body = api.call('GET', '/skills/' + name)['body']
                assert all(fragment in body for fragment in expected), name

            runtime = api.call('GET', '/runtime/capabilities')
            registry = {tool['tool']: tool for tool in runtime['environment_registry']}
            receipt['native_discovery'] = {key: registry[key]['available'] for key in
                                           ('calculix', 'code_aster', 'elmer', 'moose', 'su2', 'pandoc', 'pvpython')}
            receipt['wsl_discovery'] = {key: runtime['wsl'].get(key) for key in
                                        ('reachable', 'openfoam', 'gmsh', 'latexmk')}
            receipt['python_discovery'] = {key: bool(runtime['python']['packages'].get(key))
                                           for key in ('cadquery', 'vtk', 'numpy', 'matplotlib')}

            api.call('POST', '/skills', {'name': 'vibeflow-moose', 'description': 'Private notes',
                                        'body': 'Private definition'}, expect=(200, 201))
            assert api.call('GET', '/skills/vibeflow-moose')['body'] == 'Private definition'
            assert api.call('GET', '/skills/unknown-skill', expect=404)
        receipt['passed'] = True
    finally:
        receipt['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'skill-capabilities.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2),
                                                          encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    verify()
