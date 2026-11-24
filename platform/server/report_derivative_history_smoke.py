"""Real HTTP history pagination and ownership, using failed metadata reservations."""
import json
from testsupport import IsolatedServer
from latex_history_smoke import FIXTURE as BASE

FIXTURE=BASE.replace("kind=latex_artifacts.channel(data['main'])","kind=data['kind']").replace(
 "{'main':data['main'],'recipe':'pdflatex'}","{'output':data['output']}")


def main():
    checks=[];api=IsolatedServer('derivative-history',app='history_fixture:app')
    (api.root/'history_fixture.py').write_text(FIXTURE,encoding='utf8')
    with api:
        api.create_case('derivative metadata');api.put('Ref/source.txt','metadata only');prefix=api.prefix
        for kind,path,count in [('report-source','E-report/report.md',55),
                ('report-source-generated','E-report/report.generated.md',3),('ppt:'+'a'*40,'deck.pptx',4),
                ('pdf-number:'+'b'*40,'numbered.pdf',2)]:
            api.call('POST',f'/fixture/latex-history/{api.case_id}',{'kind':kind,'output':path,'count':count})
        query=prefix+'/report/derivatives';all_rows=api.call('GET',query+'?limit=100')
        assert len(all_rows)==64 and all(r['output'] for r in all_rows)
        expected=sorted([r['id'] for r in all_rows],reverse=True);assert [r['id'] for r in all_rows]==expected
        seen=[];cursor=None
        while True:
            rows=api.call('GET',query+'?limit=11'+('&before_id='+cursor if cursor else ''))
            if not rows:break
            seen.extend(r['id'] for r in rows);cursor=rows[-1]['id']
        assert seen==expected
        checks.append('all_families_same_timestamp_total_order_and_pagination_beyond_50')
        drafts=api.call('GET',prefix+'/report/compositions?limit=100');assert len(drafts)==58
        assert all(r['family']=='draft' for r in drafts)
        output_query=prefix+'/report/compositions?output=E-report/report.md&limit=100'
        ordinary=api.call('GET',output_query);assert len(ordinary)==55
        cursor=ordinary[20]['id']
        assert api.call('GET',output_query+'&before_id='+cursor)==ordinary[21:]
        generated=next(r for r in drafts if r['output'].endswith('.generated.md'))
        api.call('GET',output_query+'&before_id='+generated['id'],expect=400)
        checks.append('legacy_compositions_array_and_output_scoped_cursor')
        deck=next(r for r in all_rows if r['family']=='ppt')
        assert len(api.call('GET',query+'?family=ppt'))==4
        api.call('GET',query+'?family=draft&before_id='+deck['id'],expect=400)
        for suffix in ('limit=0','limit=101','before_id='+'x'*37,'family=geometry'):
            api.call('GET',query+'?'+suffix,expect=422)
        api.call('GET',query+'?before_id=missing',expect=400)
        api.call('GET',prefix+'/report/compositions?output=bad',expect=400)
        checks.append('family_cursor_and_parameter_bounds_rejected')
        api.call('GET',prefix+'/report/derivative?publication_id='+deck['id'],expect=409)
        checks.append('failed_reservation_cannot_be_read_as_published_artifact')
        owner=api.token
        user=api.call('POST','/auth/register',{'email':'derivative-other@example.com','name':'Other','password':'isolated-derivative-123'})
        api.token=user['token']
        for path in (query,prefix+'/report/derivative?publication_id='+deck['id'],
                prefix+'/report/derivative-artifact?publication_id='+deck['id']+'&path=deck.pptx'):
            api.call('GET',path,expect=404)
        api.token=owner;workspace=api.call('GET','/workspaces')[0]
        case=api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'same owner other case'})
        other=f"/cases/{case['id']}/report"
        api.call('GET',other+'/derivatives?before_id='+cursor,expect=400)
        api.call('GET',other+'/derivative?publication_id='+deck['id'],expect=404)
        api.call('GET',other+'/derivative-artifact?publication_id='+deck['id']+'&path=deck.pptx',expect=404)
        checks.append('foreign_owner_and_same_owner_cross_case_cannot_read_or_page')
    result={'passed':True,'count':len(checks),'checks':checks,'scope':'metadata/permissions, not native output proof'}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('DERIVATIVE_HISTORY_OK '+json.dumps({'evidence':str(api.root),**result}),flush=True)


if __name__=='__main__':main()
