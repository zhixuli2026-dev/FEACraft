"""Preview/apply offline retention of a case's immutable live S3 versions."""
import argparse
import json
import os
from pathlib import Path
import sys

SERVER = Path(__file__).resolve().parent
if not (SERVER/'app').is_dir():
    SERVER = SERVER.parent/'server'
sys.path.insert(0, str(SERVER))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan','apply'])
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--case-prefix')
    parser.add_argument('--retention-days', type=int, default=30)
    parser.add_argument('--confirm-offline', action='store_true')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--result', type=Path)
    args = parser.parse_args()
    args.plan = args.plan.resolve()
    if args.result: args.result = args.result.resolve()
    if args.operation=='plan' and not args.case_prefix:
        parser.error('plan requires --case-prefix')
    if args.operation=='apply' and (not args.confirm_offline or not args.plan_sha256 or not args.result):
        parser.error('apply requires --confirm-offline, --plan-sha256 and --result')
    os.chdir(SERVER)
    from app.config import get_settings
    from app import storage
    from app.services.s3_version_maintenance import plan, apply
    settings = get_settings()
    if settings.storage_backend != 's3':
        parser.error('Version maintenance requires the explicitly configured S3 backend')
    client = storage.s3()
    try:
        if args.operation=='plan':
            value = plan(client, settings.s3_bucket, args.case_prefix, retention_days=args.retention_days)
            with args.plan.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
            print(json.dumps({k:value[k] for k in ['plan_sha256','retention_days','candidate_bytes','tombstones','retained']}))
            return 0
        value = json.loads(args.plan.read_text(encoding='utf-8'))
        if value.get('bucket') != settings.s3_bucket or value.get('endpoint') != client.meta.endpoint_url:
            raise ValueError('Plan belongs to another configured storage target')
        if value.get('plan_sha256') != args.plan_sha256:
            raise ValueError('Plan does not match the explicitly approved SHA-256')
        # Reserve the result path before mutating storage; an interrupted file
        # remains "started", never a forged success receipt.
        with args.result.open('x', encoding='utf-8') as stream:
            json.dump({'status':'started','plan_sha256':args.plan_sha256},stream)
        try:
            result = apply(client, value, confirm_offline=True)
        except BaseException as error:
            args.result.write_text(json.dumps({'status':'incomplete','plan_sha256':args.plan_sha256,
                'failure_type':type(error).__name__,'next_step':'Inspect actual HEAD/objects and generate a new plan; do not restore the old HEAD.'}),encoding='utf-8')
            raise
        args.result.write_text(json.dumps({'status':'partial' if result['errors'] else 'complete',**result},
                                         ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:result[k] for k in ['deleted_objects','deleted_bytes','compacted_tombstones']}))
        return 1 if result['errors'] else 0
    finally:
        client.close()


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Maintenance failed: '+type(error).__name__+'; retain the plan/result and inspect before retrying.',file=sys.stderr)
        sys.exit(1)
