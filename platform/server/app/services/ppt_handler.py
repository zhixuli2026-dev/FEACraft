"""PPTX generation from a private, sealed request and resource snapshot."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import uuid

from pptx import Presentation
from pptx.enum.dml import MSO_FILL
from pptx.enum.shapes import MSO_SHAPE_TYPE

from .. import storage
from . import attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .durable_events import json_text
from .pdf_number_handler import require_absent
from .geometry_handler import GeometryHandler
from .ppt_builder import ppt_geometry, ppt_preset, ppt_slide_layout, reconstruction_bounds, validate_ppt_document
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('ppt_worker.py')
LIBRARY=Path(__file__).with_name('ppt_builder.py')
CONFIG='.vibeflow/prepared/ppt.json'


def receipt_path(identifier):return '.vibeflow/ppt-receipts/'+str(uuid.UUID(identifier))+'.json'


def source(root,paths):
    return {'files':{path:workspace.digest_file(workspace.checked_path(root,path)) for path in paths}}


class PptHandler:
    key='ppt.build'
    version='1'
    job_type='convert'
    publication_kind='ppt'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    def publication_channel(self,descriptor):
        return 'ppt:'+hashlib.sha256(workspace.canonical_path(descriptor['output']).casefold().encode('utf8')).hexdigest()[:40]

    async def prepare(self,db,case,user,*,parameters,budget):
        document=validate_ppt_document(parameters)
        document['output']=workspace.canonical_path(document['output'])
        if document['template']:
            document['template']=workspace.canonical_path(document['template'])
        for slide in document['slides']:
            if slide['image']:
                slide['image']['path']=workspace.canonical_path(slide['image']['path'])
        workspace.assert_mutable_path(case.s3_prefix,document['output'])
        resources=sorted(set(([document['template']] if document['template'] else [])+
            [slide['image']['path'] for slide in document['slides'] if slide['image']]))
        resources=[workspace.canonical_path(path) for path in resources]
        if any(path.casefold()==document['output'].casefold() for path in resources):
            raise ValueError('PPT输出不能覆盖模板或图片')
        from .case_read_view import inspect
        def read_inputs(root):
            require_absent(root,document['output'])
            return source(root,resources)
        try:observed=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read_inputs))
        except FileExistsError as error:raise ExecutionConflict('output_exists','PPT输出已存在；不会覆盖已有文件') from error
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        data=json_text(document).encode('utf8');identifier=str(uuid.uuid4())
        spec={'command':'PPTX build','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--spec',CONFIG,'--execution-id',identifier],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'ppt.v1','handler_version':self.version,'case_id':case.id,
            'output':document['output'],'request':document,'request_sha256':hashlib.sha256(data).hexdigest(),
            'execution_id':identifier,'source':observed,'resources':resources,
            'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            'library_sha256':hashlib.sha256(LIBRARY.read_bytes()).hexdigest()}
        return PreparedStep(self.key,self.version,self.job_type,spec,resources,None,
            self.publication_channel(descriptor),descriptor,generated_inputs={CONFIG:data})

    def validate_spec(self,spec):
        args=spec.get('argv')
        if (spec.get('command')!='PPTX build' or spec.get('runtime')!='windows'
                or spec.get('evidence_run_dir') is not None or not isinstance(args,list) or len(args)!=6
                or args[:5]!=[sys.executable,str(WORKER),'--spec',CONFIG,'--execution-id']):
            raise ExecutionConflict('evidence_invalid','PPT command differs from its registered worker')
        try:
            if str(uuid.UUID(args[5]))!=args[5]:raise ValueError('Noncanonical identity')
        except (ValueError,TypeError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','PPT execution identity is invalid') from error

    def source(self,prefix,descriptor):
        from .case_read_view import inspect
        return inspect(prefix,source,descriptor['resources'],paths=descriptor['resources'])

    def before_publish(self,prefix,descriptor):
        from .case_read_view import inspect
        inspect(prefix,require_absent,descriptor['output'])

    def artifacts(self,run,descriptor,output,frozen_files):
        from .postprocess_artifacts import context
        manifest=context(run);document=descriptor['request']
        if manifest['source']['case_id']!=descriptor['case_id']:raise ValueError('PPT belongs to another case')
        config=workspace.checked_path(run,CONFIG).read_bytes()
        if (hashlib.sha256(config).hexdigest()!=descriptor['request_sha256']
                or config!=json_text(document).encode('utf8') or json.loads(config)!=validate_ppt_document(json.loads(config))):
            raise ValueError('PPT request differs from its original validated configuration')
        for path,record in frozen_files.items():
            actual=workspace.digest_file(workspace.checked_path(run,path))
            if any(record[k]!=actual[k] for k in actual):raise ValueError('PPT changed a frozen input: '+path)
        if source(run,descriptor['resources'])!=descriptor['source']:raise ValueError('PPT resources changed')
        result=workspace.digest_file(workspace.checked_path(run,descriptor['output']))
        if any(output['files'][descriptor['output']][k]!=result[k] for k in result):
            raise ValueError('PPT output changed after capture')
        receipt=json.loads(workspace.checked_path(run,receipt_path(descriptor['execution_id'])).read_text(encoding='utf8'))
        expected={'schema':'vibeflow.ppt-build.v1','execution_id':descriptor['execution_id'],
            'attempt_id':manifest['attempt_id'],'job_id':manifest['source']['job_id'],'case_id':descriptor['case_id'],
            'request_sha256':descriptor['request_sha256'],'output':descriptor['output'],'output_record':result,
            'worker_sha256':descriptor['worker_sha256'],'library_sha256':descriptor['library_sha256']}
        if any(receipt.get(k)!=v for k,v in expected.items()):raise ValueError('PPT has no matching worker receipt')
        prs=Presentation(str(run/descriptor['output']))
        template=Presentation(str(run/document['template'])) if document['template'] else None
        template_count=len(template.slides) if template is not None else 0
        if (len(prs.slides)!=template_count+len(document['slides']) or receipt.get('slides')!=len(prs.slides)
                or prs.core_properties.title!=document['title']):raise ValueError('PPT slide count or metadata changed')
        def named(slide,name):
            matches=[shape for shape in slide.shapes if shape.name==name]
            if not matches:raise ValueError('PPT lacks its editable '+name)
            return matches[-1]
        for index,spec in enumerate(document['slides'],template_count):
            slide=prs.slides[index]
            if spec.get('notes') and (not slide.has_notes_slide or slide.notes_slide.notes_text_frame is None
                                      or slide.notes_slide.notes_text_frame.text.replace('\v','\n')!=spec['notes']):
                raise ValueError('PPT speaker notes differ from the request')
            for position,item in enumerate(spec.get('native_shapes',()),1):
                shape=named(slide,f'VibeFlowPreset{position}')
                bounds=reconstruction_bounds(item,prs.slide_width,prs.slide_height)
                if (ppt_geometry(shape)!=item['preset'] or (shape.left,shape.top,shape.width,shape.height)!=bounds
                        or str(shape.line.color.rgb)!=item['stroke'][1:]):
                    raise ValueError('PPT preset geometry, bounds or stroke differs from its request')
                if shape.shape_type!=MSO_SHAPE_TYPE.LINE:
                    if item['fill'] is None:
                        if shape.fill.type!=MSO_FILL.BACKGROUND:
                            raise ValueError('PPT shape lacks its transparent fill')
                    elif shape.fill.type!=MSO_FILL.SOLID or str(shape.fill.fore_color.rgb)!=item['fill'][1:]:
                        raise ValueError('PPT shape lacks its native solid fill')
            if details:=spec.get('reconstruction'):
                if (slide.background.fill.type!=MSO_FILL.SOLID
                        or str(slide.background.fill.fore_color.rgb)!=details['background'][1:]):
                    raise ValueError('PPT reconstruction background differs from the request')
                for position,item in enumerate(details['text_regions'],1):
                    box=named(slide,f'VibeFlowOCR{position}')
                    if (box.shape_type!=MSO_SHAPE_TYPE.TEXT_BOX or box.text.replace('\v','\n')!=item['text']
                            or (box.left,box.top,box.width,box.height)!=reconstruction_bounds(item,prs.slide_width,prs.slide_height)
                            or str(box.text_frame.paragraphs[0].font.color.rgb)!=item['color'][1:]):
                        raise ValueError('PPT reconstruction text cannot be edited or differs from its request')
                continue
            if named(slide,'VibeFlowTitle').text.replace('\v','\n')!=spec['title']:
                raise ValueError('PPT title is not editable or changed')
            if spec.get('beautify') and (slide.background.fill.type!=MSO_FILL.SOLID
                                        or str(slide.background.fill.fore_color.rgb)!='F5F8FC'):
                raise ValueError('PPT styling differs from its request')
            if 'template_layout' in spec:
                selected=ppt_slide_layout(template,spec['template_layout'])
                if slide.slide_layout.part.partname!=selected.part.partname:
                    raise ValueError('PPT slide does not use its requested template layout')
            if ppt_geometry(named(slide,'VibeFlowAccent'))!=ppt_preset(spec['shape']):
                raise ValueError('PPT lacks its requested native shape')
            if spec['bullets'] or spec['text']:
                body=named(slide,'VibeFlowBody')
                lines=(spec['bullets'] if body.is_placeholder else ['• '+line for line in spec['bullets']])
                lines=lines+([spec['text']] if spec['text'] else [])
                if body.text.replace('\v','\n')!='\n'.join(lines):
                    raise ValueError('PPT body differs from its request')
            if spec['table']:
                table=named(slide,'VibeFlowTable').table;rows=[spec['table']['headers'],*spec['table']['rows']]
                if len(table.rows)!=len(rows) or len(table.columns)!=len(rows[0]):raise ValueError('PPT table dimensions changed')
                if any(table.cell(i,j).text.replace('\v','\n')!=value for i,row in enumerate(rows) for j,value in enumerate(row)):
                    raise ValueError('PPT editable table values changed')
            if spec['image']:
                wanted=workspace.checked_path(run,spec['image']['path']).read_bytes()
                if not any(shape.shape_type==MSO_SHAPE_TYPE.PICTURE and shape.image.blob==wanted for shape in slide.shapes):
                    raise ValueError('PPT does not contain its original image')
        descriptor['receipt']=receipt
        return {descriptor['output']:{**result,'executable':False}}
