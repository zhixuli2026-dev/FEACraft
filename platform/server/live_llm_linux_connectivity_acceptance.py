"""Actual official DeepSeek request inside the deployed container; never prints credentials."""
import argparse,asyncio,hashlib,json,subprocess
from pathlib import Path
from live_llm_acceptance import source
ROOT=Path(__file__).resolve().parents[2]
CODE='''import asyncio,httpx,json,sys,time
c=json.load(sys.stdin)
async def main():
 started=time.monotonic();result={'provider':'deepseek','host':'api.deepseek.com','requested_model':c['model'],'mock_llm_calls':0,'passed':False}
 try:
  async with httpx.AsyncClient(timeout=httpx.Timeout(90,connect=20),trust_env=True) as client:
   async with client.stream('POST','https://api.deepseek.com/v1/chat/completions',headers={'Authorization':'Bearer '+c['key']},json={'model':c['model'],'stream':True,'max_tokens':32,'messages':[{'role':'user','content':'Reply only CONNECTION_OK.'}]}) as response:
    result.update(status=response.status_code,request_id=response.headers.get('x-request-id'),date=response.headers.get('date'))
    text=[]
    async for line in response.aiter_lines():
     if not line.startswith('data:') or line[5:].strip()=='[DONE]':continue
     data=json.loads(line[5:])
     for key in ['id','model','usage']:
      if data.get(key) is not None:result[key]=data[key]
     for choice in data.get('choices',[]):text.append(choice.get('delta',{}).get('content') or '')
    result['answer']=''.join(text);result['passed']=response.status_code==200 and 'CONNECTION_OK' in result['answer']
 except Exception as error:result['failure']={'type':type(error).__name__,'repr':repr(error),'cause':repr(error.__cause__)}
 result['elapsed_ms']=round((time.monotonic()-started)*1000)
 raw=json.dumps(result);assert c['key'] not in raw;print(raw)
asyncio.run(main())
'''
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('output',type=Path);a=p.parse_args();review=json.loads(a.review.read_text('utf8'));assert review['classification']=='real_llm'
 for n,h in review['sources'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h,n
 c=asyncio.run(source(26));process=subprocess.run(['docker','exec','-i','vf-report-s3-20261117-api','python','-c',CODE],input=json.dumps(c),capture_output=True,text=True,encoding='utf8',timeout=110)
 assert c['key'] not in process.stdout+process.stderr
 if process.returncode:raise RuntimeError('Container diagnostic process failed; credentials withheld')
 result=json.loads(process.stdout);a.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf8');print(json.dumps(result))
