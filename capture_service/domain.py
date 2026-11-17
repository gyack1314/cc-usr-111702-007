"""解释追溯码的分页与流通事件。"""
import json
def decode(raw,receipt):
    d=json.loads(raw.decode('utf-8'))
    rows=[{'type':'page','id':d['code']+'/'+str(d['page']),'version':'received',
           'payload':{'next':d['next'],'code':d['code']}}]
    for e in d['events']:
        rows.append({'type':'event','id':e['event_id'],'version':e['event_time'],
                     'payload':{'code':d['code'],'kind':e['kind'],'batch':e['batch'],
                                'parent':e['parent'],'received_at':e['received_at']}})
    return rows
def assess(rows):
    pages=[r for r in rows if r['type']=='page'];events=[r for r in rows if r['type']=='event']
    page_ids={r['id'] for r in pages}
    reasons=[]
    for page in pages:
        next_page=page['payload'].get('next')
        if next_page and page['payload']['code']+'/'+next_page.split('=')[-1] not in page_ids:
            reasons.append('追溯分页未闭合:'+next_page)
    if any(e['payload']['kind']=='return' for e in events):reasons.append('退货后去向尚待复核')
    return {'publishable':not reasons,'reasons':reasons,'event_count':len(events)}
