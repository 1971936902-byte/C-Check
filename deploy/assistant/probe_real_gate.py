"""Run production submission/gate/chat code against real MySQL and Qwen GPU."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def main():
    host = os.environ['PROBE_MYSQL_HOST']
    if not host.startswith('ccheck-assistant-probe-'):
        raise RuntimeError('refusing non-probe database')
    password = Path('/run/secrets/probe-mysql').read_text().strip()
    url = URL.create('mysql+pymysql', username='probe', password=password,
                     host=host, database='ccheck_assistant_probe')
    engine = create_engine(url, connect_args={'connect_timeout': 3})
    for attempt in range(60):
        try:
            with engine.connect() as connection: connection.execute(text('SELECT 1'))
            break
        except Exception:
            if attempt == 59: raise
            time.sleep(1)
    engine.dispose()
    os.environ.update(DATABASE_URL=url.render_as_string(hide_password=False),
                      ASSISTANT_ENABLED='true', JWT_SECRET=secrets.token_hex(32),
                      ADMIN_PASSWORD=secrets.token_hex(32),
                      STORAGE_PATH='/tmp/probe-storage')
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.db.models import AssistantComputeGate, User, ModelNode, ReviewTask, TaskStatus
    from app.services import assistant_gate as gate
    from app.services.assistant_chat import ChatGateway
    from app.services.submissions import create_review_task, collect_text_submission
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        owner = User(username='probe-owner', password_hash='unused')
        node = ModelNode(display_name='Qwen', model_identifier='qwen2.5-coder-32b-instruct-fp8',
                         base_url='http://probe-qwen:8001/v1', is_enabled=True)
        db.add_all([owner, node])
        db.commit()
        owner_id, node_id = owner.id, node.id
    api_key = Path('/run/secrets/probe_api').read_text().strip()
    control_key = Path('/run/secrets/probe_control').read_text().strip()

    async def scenario():
        gateway = ChatGateway(SessionLocal, url='http://probe-qwen:8001',
            api_key=api_key, control_key=control_key, heartbeat_interval=.1)
        stream = await gateway.start({'model':'qwen2.5-coder-32b-instruct-fp8',
            'messages':[{'role':'user','content':'Write 500 numbered C programming tips.'}], 'max_tokens':2048})
        try:
            async with asyncio.timeout(60):
                async for chunk in stream:
                    data=json.loads(chunk.decode().removeprefix('data: ').strip())
                    if any(choice.get('delta',{}).get('content') for choice in data.get('choices',[])):
                        break
                else: raise AssertionError('no real generated token')
            with SessionLocal() as db:
                before=db.get(AssistantComputeGate,1)
                assert before.owner_token and before.control_epoch
                task=create_review_task(db,owner=db.get(User,owner_id),model_node_id=node_id,
                    submission=collect_text_submission('int main(void) { return 0; }'),
                    check_types=['memory_safety'])
                task_id=task.id
                assert task.status==TaskStatus.QUEUED
                assert not gate.review_may_start(db), 'review admitted before model idle proof'
                db.commit()
            async with asyncio.timeout(30):
                rest=b''.join([chunk async for chunk in stream])
            assert b'review_priority_interrupted' in rest and b'[DONE]' not in rest
            with SessionLocal() as db:
                assert db.get(AssistantComputeGate,1).owner_token is None
                assert gate.review_may_start(db)
                db.commit()
                assert gate.status(db)['reason']=='review_busy'
                try: gate.claim(db,'chat')
                except gate.AssistantUnavailable as error: assert error.reason=='review_busy'
                else: raise AssertionError('assistant admitted during queued review')
                db.rollback()
                db.get(ReviewTask,task_id).status=TaskStatus.COMPLETED
                db.commit()
                assert gate.status(db)['available']
            resumed=await gateway.start({'model':'qwen2.5-coder-32b-instruct-fp8',
                'messages':[{'role':'user','content':'Reply with OK.'}],'max_tokens':8})
            async with asyncio.timeout(30):
                result=b''.join([chunk async for chunk in resumed])
            assert b'[DONE]' in result and b'"error"' not in result
            print('REAL_MYSQL_SUBMISSION_PREEMPTS_QWEN_GATE_IDLE_AND_RESUME_OK')
        finally:
            await stream.aclose()
            if gateway.jobs: await asyncio.gather(*gateway.jobs, return_exceptions=True)
    asyncio.run(scenario())


if __name__ == '__main__':
    main()
