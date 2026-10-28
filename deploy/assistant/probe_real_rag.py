"""Real provider/gateway/model RAG integration; no mocked model responses."""
import argparse
import asyncio
import io
import time
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize',action='store_true')
    args=parser.parse_args()
    from app.db.session import engine, SessionLocal
    from app.db.base import Base
    from app.db.models import AssistantComputeGate
    from sqlalchemy import text
    for attempt in range(60):
        try:
            with engine.connect() as connection: connection.execute(text('SELECT 1'))
            break
        except Exception:
            if attempt==59: raise
            time.sleep(1)
    if args.initialize:
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add(AssistantComputeGate(id=1,generation=0,cancel_requested=False))
            db.commit()
        print('REAL_RAG_DATABASE_INITIALIZED')
        return
    from app.core.config import get_settings
    from app.services.anythingllm import AnythingLLM
    settings=get_settings()
    client=AnythingLLM(settings.assistant_anythingllm_url,
        settings.assistant_anythingllm_api_key.get_secret_value(),timeout=120)
    owner,conversation=str(uuid4()),str(uuid4())
    client.ensure_thread(owner,conversation,'RAG integration',True)
    locations=[]
    for filename,content in [('calibration.txt','Project Borealis calibration value is 73. The device requires two retries.'),
                             ('ownership.txt','Project Borealis owner is team Cedar. The verification marker is CEDAR-2048.')]:
        locations.extend(client.upload_document(str(uuid4()),filename,io.BytesIO(content.encode())))
    client.embed_documents(owner,locations)
    async def chat():
        events=[event async for event in client.stream_chat(owner,conversation,True,
            'According to the uploaded Project Borealis documents, what is the calibration value and verification marker?')]
        assert events and events[-1]['event']=='done', events
        assert not any(event['event']=='error' for event in events), events
        answer=''.join(event['text'] for event in events if event['event']=='delta')
        sources=[source for event in events if event['event']=='sources' for source in event['sources']]
        assert '73' in answer and 'CEDAR-2048' in answer, answer
        assert sources, 'real RAG response has no sources'
    asyncio.run(chat())
    history=client.history(owner,conversation,True)
    assert any(message['role']=='assistant' and 'CEDAR-2048' in message['content'] for message in history)
    with SessionLocal() as db:
        gate=db.get(AssistantComputeGate,1)
        assert gate.owner_token is None and gate.fault is None
    print('REAL_PROVIDER_GATEWAY_QWEN_JINA_RAG_AND_HISTORY_OK')


if __name__=='__main__':
    main()
