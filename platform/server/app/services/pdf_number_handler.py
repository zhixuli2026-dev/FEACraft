"""Owned PDF numbering with frozen source, validated output and exclusive aliases."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from pypdf import PdfReader

from .. import storage
from . import attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .geometry_handler import GeometryHandler
from .pdf_pages import safe_pdf_relative, numbered_pdf_path, page_number_content
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('pdf_number_worker.py')
LIBRARY=Path(__file__).with_name('pdf_pages.py')


def receipt_path(execution_id):return '.vibeflow/pdf-receipts/'+str(uuid.UUID(execution_id))+'.json'


def source(root,input_path):
    record=workspace.digest_file(workspace.checked_path(root,input_path))
    return {'files':{input_path:record}}


def require_absent(root,path):
    try:workspace.checked_path(root,path)
    except FileNotFoundError:return
    raise FileExistsError('页码输出已存在；不会覆盖已有文件')


class PdfNumberHandler:
    key='pdf.number'
    version='1'
    job_type='convert'
    publication_kind='pdf-number'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    def publication_channel(self,descriptor):
        path=workspace.canonical_path(safe_pdf_relative(descriptor['output']))
        return 'pdf-number:'+hashlib.sha256(path.casefold().encode('utf8')).hexdigest()[:40]

    async def prepare(self,db,case,user,*,parameters,budget):
        if set(parameters)!={'input','output'}:raise ValueError('PDF numbering parameters are invalid')
        input_path=workspace.canonical_path(safe_pdf_relative(parameters['input']))
        output_path=workspace.canonical_path(safe_pdf_relative(parameters['output'] or numbered_pdf_path(input_path)))
        workspace.assert_mutable_path(case.s3_prefix,output_path)
        if input_path.casefold()==output_path.casefold():raise ValueError('页码输出不能覆盖源 PDF')
        from .case_read_view import inspect
        def read_inputs(root):
            require_absent(root,output_path)
            return source(root,input_path)
        try:observed=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read_inputs))
        except FileExistsError as error:raise ExecutionConflict('output_exists',str(error)) from error
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        identifier=str(uuid.uuid4())
        argv=[sys.executable,str(WORKER),'--input',input_path,'--output',output_path,'--execution-id',identifier]
        spec={'command':'PDF page numbering','case_prefix':case.s3_prefix,'runtime':'windows','argv':argv,
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'pdf-number.v1','handler_version':self.version,'case_id':case.id,
            'input':input_path,'output':output_path,'execution_id':identifier,'source':observed,
            'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            'library_sha256':hashlib.sha256(LIBRARY.read_bytes()).hexdigest()}
        return PreparedStep(self.key,self.version,self.job_type,spec,[input_path],None,
            self.publication_channel(descriptor),descriptor)

    def validate_spec(self,spec):
        args=spec.get('argv')
        if (spec.get('command')!='PDF page numbering' or spec.get('runtime')!='windows'
                or spec.get('evidence_run_dir') is not None or not isinstance(args,list) or len(args)!=8
                or args[:3]!=[sys.executable,str(WORKER),'--input'] or args[4]!='--output' or args[6]!='--execution-id'):
            raise ExecutionConflict('evidence_invalid','PDF numbering differs from its registered worker')
        if any(workspace.canonical_path(safe_pdf_relative(args[i]))!=args[i] for i in (3,5)) or str(uuid.UUID(args[7]))!=args[7]:
            raise ExecutionConflict('evidence_invalid','PDF paths or execution identity are invalid')

    def source(self,prefix,descriptor):
        from .case_read_view import inspect
        return inspect(prefix,source,descriptor['input'],paths=[descriptor['input']])

    def before_publish(self,prefix,descriptor):
        # Output exclusivity is a commit precondition, not an input dependency:
        # the alias created by this publication must not stale its own source.
        from .case_read_view import inspect
        inspect(prefix,require_absent,descriptor['output'])

    def artifacts(self,run,descriptor,output,frozen_files):
        from .postprocess_artifacts import context
        manifest=context(run)
        if manifest['source']['case_id']!=descriptor['case_id']:raise ValueError('PDF belongs to another case')
        input_path,output_path=descriptor['input'],descriptor['output']
        expected=descriptor['source']['files'][input_path]
        actual=workspace.digest_file(workspace.checked_path(run,input_path))
        if actual!=expected or any(frozen_files[input_path][k]!=actual[k] for k in actual):
            raise ValueError('PDF source no longer matches its sealed input')
        result=workspace.digest_file(workspace.checked_path(run,output_path))
        if any(output['files'][output_path][k]!=result[k] for k in result):raise ValueError('PDF output changed after capture')
        receipt=json.loads(workspace.checked_path(run,receipt_path(descriptor['execution_id'])).read_text(encoding='utf8'))
        required={'schema':'vibeflow.pdf-number.v1','execution_id':descriptor['execution_id'],
            'attempt_id':manifest['attempt_id'],'job_id':manifest['source']['job_id'],'case_id':descriptor['case_id'],
            'input':input_path,'output':output_path,'input_record':expected,'output_record':result,
            'worker_sha256':descriptor['worker_sha256'],'library_sha256':descriptor['library_sha256']}
        if any(receipt.get(k)!=v for k,v in required.items()):raise ValueError('PDF has no matching worker receipt')
        original=PdfReader(str(run/input_path));numbered=PdfReader(str(run/output_path))
        count=len(original.pages)
        if count<=0 or len(numbered.pages)!=count or receipt.get('pages')!=count:
            raise ValueError('PDF numbering changed page count')
        for index,page in enumerate(numbered.pages,1):
            from pypdf.generic import ArrayObject
            contents=page['/Contents'];last=contents[-1] if isinstance(contents,ArrayObject) else contents
            data=last.get_object().get_data()
            font = re.match(rb'q\nBT\n(/VFPageNumberFont\d*) 9 Tf\n', data)
            if font is None:
                raise ValueError('Numbered PDF lacks its actual page labels')
            font_name = font[1].decode('ascii')
            if (data != page_number_content(index, count, page, font_name)
                    or page['/Resources']['/Font'][font_name]['/BaseFont'] != '/Helvetica'):
                raise ValueError('Numbered PDF labels differ from its visible page geometry')
        # The bundle retains the original worker receipt without exposing a
        # private runtime metadata file as a mutable user-facing alias.
        descriptor['receipt']=receipt
        return {output_path:{**result,'executable':False}}
