"""Graph semantics with explicit immutable bundles and controlled DB/source fixtures."""
import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import patch
import uuid

from app.models import ArtifactPublication, ArtifactPublicationHead
from app.services import dependency_status as graph


class Reads:
    def __init__(self):
        self.rows, self.heads = {}, {}
    async def get(self, model, key, **kwargs):
        return (self.rows if model is ArtifactPublication else self.heads).get(key)
    async def scalar(self, query):
        return NS(case_id=1, status='succeeded')
    def seal(self):
        pass


async def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix='dependency-graph-') as directory:
        root = Path(directory)
        reads, bundles, calls = Reads(), {}, []
        case = NS(id=1, s3_prefix='case/')
        def add(kind, *, current=True, dependencies=None, changed=False):
            identifier = str(uuid.uuid4())
            folder = root/identifier
            (folder/'files').mkdir(parents=True)
            (folder/'files/result.txt').write_bytes(b'evidence')
            files = {'result.txt': {'size': 8, 'sha256': hashlib.sha256(b'evidence').hexdigest()}}
            descriptor = {'source': {'input': 1}, 'observed': {'input': 2 if changed else 1},
                          'kind': kind, 'input_publications': dependencies or []}
            bundles[identifier] = (folder, {'descriptor': descriptor, 'files': files})
            reads.rows[identifier] = NS(id=identifier, case_id=1, kind=kind, generation=1, status='committed',
                                       attempt_id=identifier, manifest_sha256='f'*64)
            if current:
                reads.heads[(1, kind)] = NS(active_publication_id=identifier)
            return identifier
        def ref(identifier, current=True, paths=None):
            return {'publication_id': identifier, 'require_current': current, 'paths': paths}
        def source(prefix, descriptor):
            calls.append(descriptor['kind'])
            return descriptor['observed']
        handler = NS(key='fixture', source=source)
        with patch('app.services.artifact_publication.read_manifest', side_effect=lambda prefix,row: bundles[row.id]), \
             patch('app.services.step_handlers.publication_handler', return_value=handler), \
             patch('app.services.step_handlers.kind_for', side_effect=lambda h,d: d['kind']):
            old = add('geometry', current=False, changed=True)
            fresh = add('geometry')
            consumer = add('mesh', dependencies=[ref(old)])
            inspector = graph.Inspector(case, reads, None)
            assert (await inspector.visit(consumer))['status'] == 'stale'
            assert (await inspector.visit(fresh))['status'] == 'fresh'
            assert 'mesh' not in calls
            checks.append('superseded_ancestor_stales_consumer_without_staling_fresh_replacement')
            historical = add('historical', dependencies=[ref(old, False)])
            result = await inspector.visit(historical)
            assert result['status'] == 'fresh' and result['dependencies'][0]['status'] == 'historical_available'
            checks.append('explicit_historical_selection_ignores_current_source_and_head')
            bad = add('bad-selection', dependencies=[ref(fresh, paths=['missing.txt'])])
            assert (await inspector.visit(bad))['status'] == 'unverifiable'
            checks.append('missing_selected_file_is_not_a_fresh_dependency')
            foreign = add('foreign')
            reads.rows[foreign].case_id = 2
            owned = add('owned', dependencies=[ref(foreign)])
            assert (await inspector.visit(owned))['status'] == 'unverifiable'
            assert foreign not in inspector.nodes
            checks.append('foreign_publication_never_exposes_its_node_or_source')
            first = add('cycle-one')
            second = add('cycle-two', dependencies=[ref(first)])
            bundles[first][1]['descriptor']['input_publications'] = [ref(second)]
            assert (await inspector.visit(first))['status'] == 'unverifiable'
            assert (await inspector.visit(second))['status'] == 'unverifiable'
            checks.append('publication_cycle_is_unverifiable_and_terminates')
            before = len(calls)
            await inspector.visit(fresh)
            await inspector.visit(fresh)
            assert len(calls) == before
            checks.append('shared_ancestor_source_is_evaluated_once')
            # An edited geometry output clears its head while existing mesh
            # still references that version. The missing producer comes first.
            reads.heads.pop((1, 'geometry'))
            snapshot = {'heads': [{'kind': 'geometry', 'publication_id': None},
                                 {'kind': 'mesh', 'publication_id': consumer}],
                        'revision': 1, 'event_sequence': 1}
            with patch.object(graph, 'PublicationReadSet', return_value=reads):
                result, _ = await graph.inspect(case, snapshot)
            assert result['recompute'] == list(graph.CORE), result
            checks.append('missing_producer_precedes_existing_stale_consumers')
            # Valid native command bundles may include sealed job evidence;
            # mounting that metadata as an input remains forbidden.
            meta = add('metadata')
            folder, manifest = bundles[meta]
            (folder/'files/.vibeflow/jobs').mkdir(parents=True)
            (folder/'files/.vibeflow/jobs/evidence.json').write_bytes(b'evidence')
            manifest['files']['.vibeflow/jobs/evidence.json'] = manifest['files']['result.txt']
            inspector = graph.Inspector(case, reads, None)
            assert (await inspector.visit(meta))['status'] == 'fresh'
            mounted = add('metadata-consumer', dependencies=[ref(meta)])
            assert (await inspector.visit(mounted))['status'] == 'unverifiable'
            checks.append('native_evidence_bundle_valid_but_metadata_mount_rejected')
    print('DEPENDENCY_GRAPH_OK '+str({'count': len(checks), 'checks': checks}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
