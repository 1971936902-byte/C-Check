"""Seed and inspect failure state only inside the disposable Compose probe."""
import argparse
import json
import os
from pathlib import Path
import time

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import AssistantComputeGate, AssistantDocument, ReviewTask, TaskStatus
from app.db.session import SessionLocal
from app.services import assistant_gate as gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['seed', 'clear-busy', 'verify'])
    parser.add_argument('--identity', required=True)
    args = parser.parse_args()
    if not args.identity.startswith('ccheck-assistant-compose-probe-') or os.environ.get('PROBE_ISOLATED_ASSISTANT_RECOVERY') != args.identity:
        raise RuntimeError('refusing a non-probe environment')
    metadata = get_settings().storage_path / 'recovery-probe-state.json'
    if args.operation == 'seed':
        with SessionLocal() as db:
            assert gate.status(db)['available']
            document = db.scalar(select(AssistantDocument).where(AssistantDocument.status == 'ready'))
            review = db.scalar(select(ReviewTask).where(ReviewTask.status == TaskStatus.COMPLETED))
            assert document is not None and review is not None
            owner = gate.claim(db, 'chat')
            gate.checkpoint_upstream(db, owner, 'f' * 64, 'dead-process-before-recovery')
            gate.fail_closed(db, owner, 'simulated interrupted inference')
            document.status = 'processing'
            review.status = TaskStatus.RUNNING
            db.commit()
            metadata.write_text(json.dumps({'owner': owner, 'document': document.id, 'review': review.id}))
        print('RECOVERY_PROBE_INTERRUPTED_STATE_SEEDED')
        return
    state = json.loads(metadata.read_text())
    if args.operation == 'clear-busy':
        with SessionLocal() as db:
            db.get(ReviewTask, state['review']).status = TaskStatus.COMPLETED
            db.commit()
        return
    with SessionLocal() as db:
        assert gate.status(db)['available']
        document = db.get(AssistantDocument, state['document'])
        assert document.status == 'paused' and document.locations
        assert db.get(AssistantComputeGate, 1).control_epoch is None
        fresh = gate.claim(db, 'chat')
        assert not gate.acknowledge_idle(db, state['owner'])
        db.commit()
        assert db.get(AssistantComputeGate, 1).owner_token == fresh
        assert gate.acknowledge_idle(db, fresh)
        db.commit()
    with httpx.Client(base_url='http://probe-api:8000/api', timeout=180, trust_env=False) as client:
        response = client.post('/auth/login', json={'username': 'admin',
            'password': Path('/run/secrets/probe_admin').read_text().strip()})
        response.raise_for_status()
        headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
        response = client.post('/assistant/documents/' + state['document'] + '/retry', headers=headers)
        assert response.status_code == 202, response.text
        for attempt in range(90):
            response = client.get('/assistant/documents', headers=headers)
            response.raise_for_status()
            document = next(row for row in response.json() if row['id'] == state['document'])
            assert document['status'] not in {'error', 'recovery_required'}, document
            if document['status'] == 'ready':
                break
            time.sleep(1)
        else:
            raise AssertionError('recovered document did not become ready')
        response = client.post('/assistant/conversations', headers=headers,
            json={'title': 'Recovery RAG acceptance', 'rag_enabled': True})
        assert response.status_code == 201, response.text
        conversation = response.json()['id']
        response = client.post('/assistant/conversations/' + conversation + '/messages', headers=headers,
            json={'message': 'According to the Borealis documents, give the calibration value and verification marker.'})
        response.raise_for_status()
        events = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
        assert events[-1]['event'] == 'done', events
        answer = ''.join(event['text'] for event in events if event['event'] == 'delta')
        assert '73' in answer and 'CEDAR-2048' in answer, answer
        assert client.delete('/assistant/conversations/' + conversation, headers=headers).status_code == 204
    print('ADMIN_RECOVERY_STALE_EPOCH_DOCUMENT_RETRY_REAL_RAG_AND_LATE_OWNER_FENCING_OK')


if __name__ == '__main__':
    main()
