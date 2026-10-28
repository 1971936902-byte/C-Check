"""Real Jina CUDA embedding and completion-fenced cancellation probe."""
from probe_qwen_gpu import main


CHECK = r'''
import asyncio, math, pathlib, secrets
import httpx
async def check():
    key=pathlib.Path('/run/secrets/probe_api').read_text().strip()
    control=pathlib.Path('/run/secrets/probe_control').read_text().strip()
    headers={'Authorization':'Bearer '+key,'X-CCheck-Control-Key':control}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8002',headers=headers,timeout=60) as client:
        health=(await client.get('/health')).json()
        assert health['device'].startswith('cuda') and health['dimension']==1536
        token=secrets.token_hex(32)
        path='/internal/assistant/'+token
        response=await client.post(path+'/reserve')
        response.raise_for_status()
        epoch=response.json()['epoch']
        body={'model':'jina-code-embeddings-1.5b','input':['int validate(int value) { return value > 0; }\n'*250]}
        request=asyncio.create_task(client.post(path+'/embeddings',params={'epoch':epoch},json=body))
        await asyncio.sleep(.1)
        response=await client.post(path+'/cancel',params={'epoch':epoch})
        response.raise_for_status()
        ack=response.json()
        assert ack['epoch']==epoch and ack['idle'] is True
        embedding=await request
        embedding.raise_for_status()
        vector=embedding.json()['data'][0]['embedding']
        assert len(vector)==1536 and all(math.isfinite(v) for v in vector)
        assert abs(sum(v*v for v in vector)-1)<.01
        late=await client.post(path+'/embeddings',params={'epoch':epoch},json=body)
        assert late.status_code==409, 'cancelled reservation admitted late work'
        response=await client.delete(path,params={'epoch':epoch})
        response.raise_for_status()
        normal=await client.post('/v1/embeddings',json={'model':body['model'],'input':['int main(void) { return 0; }']})
        normal.raise_for_status()
        assert len(normal.json()['data'][0]['embedding'])==1536
        print('JINA_REAL_GPU_EMBEDDING_CANCEL_IDLE_LATE_REJECT_AND_REVIEW_OK')
asyncio.run(check())
'''


if __name__ == '__main__':
    main(workload='jina', check=CHECK)
