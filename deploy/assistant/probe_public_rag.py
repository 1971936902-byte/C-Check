"""Public HTTP API, real Celery upload processing, model RAG and user isolation."""
import json
from pathlib import Path
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx


def main():
    with httpx.Client(base_url='http://probe-api:8000/api',timeout=180,trust_env=False) as client:
        for attempt in range(60):
            try:
                response=client.get('/assistant/status')
                if response.status_code==401: break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else: raise RuntimeError('public API not ready')
        password=Path('/run/secrets/probe_admin').read_text().strip()
        login=client.post('/auth/login',json={'username':'admin','password':password})
        login.raise_for_status()
        alice={'Authorization':'Bearer '+login.json()['access_token']}
        bob_password=secrets.token_hex(24)
        created=client.post('/admin/users',headers=alice,
            json={'username':'private-bob','password':bob_password,'role':'user'})
        created.raise_for_status()
        login=client.post('/auth/login',json={'username':'private-bob','password':bob_password})
        login.raise_for_status()
        bob={'Authorization':'Bearer '+login.json()['access_token']}
        response=client.post('/assistant/documents',headers=alice,files=[
            ('files',('calibration.txt',b'Project Borealis calibration value is 73.','text/plain')),
            ('files',('marker.txt',b'Project Borealis verification marker is CEDAR-2048.','text/plain'))])
        assert response.status_code==202, response.text
        identifiers={document['id'] for document in response.json()}
        assert len(identifiers)==2
        for attempt in range(90):
            response=client.get('/assistant/documents',headers=alice)
            response.raise_for_status()
            documents=[document for document in response.json() if document['id'] in identifiers]
            assert all(document['status'] not in ('error','recovery_required','paused') for document in documents), documents
            if len(documents)==2 and all(document['status']=='ready' for document in documents): break
            time.sleep(1)
        else: raise AssertionError('Celery document processing did not complete')
        assert client.get('/assistant/documents',headers=bob).json()==[]
        assert client.post('/assistant/documents/'+next(iter(identifiers))+'/retry',headers=bob).status_code==404
        response=client.post('/assistant/conversations',headers=alice,
            json={'title':'Public RAG acceptance','rag_enabled':True})
        assert response.status_code==201, response.text
        conversation=response.json()['id']
        path='/assistant/conversations/'+conversation+'/messages'
        response=client.post(path,headers=alice,json={'message':
            'According to the uploaded Borealis documents, give the calibration value and verification marker.'})
        response.raise_for_status()
        events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
        assert events and events[-1]['event']=='done', events
        answer=''.join(event['text'] for event in events if event['event']=='delta')
        assert '73' in answer and 'CEDAR-2048' in answer, answer
        assert any(event['event']=='sources' and event['sources'] for event in events)
        history=client.get(path,headers=alice)
        history.raise_for_status()
        assert any('CEDAR-2048' in message['content'] for message in history.json()['messages'])
        assert client.get('/assistant/conversations',headers=bob).json()==[]
        assert client.get(path,headers=bob).status_code==404
        assert client.post(path,headers=bob,json={'message':'read private data'}).status_code==404
        assert client.delete('/assistant/conversations/'+conversation,headers=bob).status_code==404
        assert client.get('/assistant/status',headers=alice).json()['available'] is True
        if __import__('os').environ.get('PROBE_REVIEW_PREEMPTION')=='1':
            check_review_preemption(client,alice)
        assert client.delete('/assistant/conversations/'+conversation,headers=alice).status_code==204
        assert client.get('/assistant/conversations',headers=alice).json()==[]
        print('PUBLIC_API_CELERY_BATCH_UPLOAD_REAL_RAG_HISTORY_AND_USER_ISOLATION_OK')


def check_review_preemption(client,headers):
    response=client.post('/admin/models',headers=headers,json={
        'display_name':'Probe Qwen FP8','model_identifier':'qwen2.5-coder-32b-instruct-fp8',
        'base_url':'http://probe-qwen:8001','api_key':Path('/run/secrets/probe_api').read_text().strip(),
        'timeout_seconds':120,'is_enabled':True})
    response.raise_for_status()
    model=response.json()['id']
    response=client.post('/assistant/conversations',headers=headers,
        json={'title':'Review preemption acceptance','rag_enabled':False})
    response.raise_for_status()
    conversation=response.json()['id']
    path='/assistant/conversations/'+conversation+'/messages'
    first_token=Event()
    def consume():
        events=[]
        with httpx.Client(base_url=str(client.base_url),headers=headers,timeout=180,trust_env=False) as stream_client:
            with stream_client.stream('POST',path,json={'message':'Write 500 numbered C programming tips, with explanations.'}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:'): continue
                    event=json.loads(line[5:].strip())
                    events.append(event)
                    if event['event']=='delta' and event['text']: first_token.set()
        return events
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(consume)
        assert first_token.wait(60), 'chat did not produce a real token'
        response=client.post('/reviews/text',headers=headers,json={
            'model_node_id':model,'display_name':'preemption-test.c','check_types':['buffer_overflow'],
            'source_text':'#include <string.h>\nvoid demo(void) {\n char dst[2];\n strcpy(dst, "overflow");\n}\n'})
        assert response.status_code==201, response.text
        task=response.json()['id']
        busy=client.get('/assistant/status',headers=headers).json()
        assert not busy['available'] and busy['reason']=='review_busy', busy
        assert client.post(path,headers=headers,json={'message':'must not run during review'}).status_code==409
        events=future.result(timeout=40)
        assert any(event.get('code')=='review_priority_interrupted' for event in events), events
        assert not any(event['event']=='done' for event in events), events
    for attempt in range(180):
        response=client.get('/reviews/'+task,headers=headers)
        response.raise_for_status()
        result=response.json()
        assert result['status']!='failed', result.get('error_message')
        if result['status']=='completed': break
        time.sleep(1)
    else: raise AssertionError('real review worker did not finish')
    model_log = result.get('model_log') or ''
    assert 'format_failed_batches=0' in model_log, model_log[-4000:]
    assert 'semantic_failed_batches=0' in model_log, model_log[-4000:]
    report=result['report_id']
    assert report
    response=client.get('/reports/'+report,headers=headers)
    response.raise_for_status()
    assert response.json()['task_id']==task
    markdown=client.get('/reports/'+report+'/markdown',headers=headers)
    markdown.raise_for_status()
    assert markdown.content
    pdf=client.get('/reports/'+report+'/pdf',headers=headers)
    pdf.raise_for_status()
    assert pdf.content.startswith(b'%PDF')
    print('PUBLIC_REVIEW_COMPLETED_AND_REPORT_DOWNLOADS_OK', flush=True)
    history=client.get(path,headers=headers)
    history.raise_for_status()
    assert any(message.get('interrupted') for message in history.json()['messages']), history.text
    assert client.get('/assistant/status',headers=headers).json()['available'] is True
    response=client.post(path,headers=headers,json={'message':'Reply with OK.'})
    response.raise_for_status()
    resumed_events = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
    assert any(event.get('event') == 'done' for event in resumed_events), resumed_events
    assert client.delete('/assistant/conversations/'+conversation,headers=headers).status_code==204
    print('PUBLIC_CHAT_REVIEW_PREEMPTION_WORKER_REPORT_PDF_HISTORY_AND_RESUME_OK')


if __name__=='__main__':
    main()
