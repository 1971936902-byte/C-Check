"""Run only inside the disposable MySQL probe network, never a business DB."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import os
import secrets
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import AssistantComputeGate, ModelNode, ReviewTask, TaskStatus, User
from app.services import assistant_gate as gate


def main():
    host = os.environ['PROBE_MYSQL_HOST']
    if not host.startswith('ccheck-assistant-probe-'):
        raise RuntimeError('refusing a non-probe database host')
    password = Path('/run/secrets/probe-mysql').read_text().strip()
    engine = create_engine(URL.create('mysql+pymysql', username='probe', password=password,
        host=host, database='ccheck_assistant_probe'), pool_size=10, max_overflow=0,
        connect_args={'connect_timeout': 3}, isolation_level='REPEATABLE READ')
    for attempt in range(60):
        try:
            with engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            break
        except Exception:
            if attempt == 59:
                raise RuntimeError('isolated MySQL did not become ready') from None
            time.sleep(2)
    # Model the existing pre-assistant schema, then exercise Alembic's real
    # version table and upgrade path rather than merely creating the new tables.
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
                                           if not table.name.startswith('assistant_')])
    os.environ['DATABASE_URL'] = engine.url.render_as_string(hide_password=False)
    os.environ['JWT_SECRET'] = secrets.token_hex(32)
    os.environ['ADMIN_PASSWORD'] = secrets.token_hex(24)
    from app.core.config import get_settings
    get_settings.cache_clear()
    migration = Config()
    migration.set_main_option('script_location', '/probe/backend/alembic')
    command.stamp(migration, '0017_review_scan_progress')
    command.upgrade(migration, 'head')
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        assert db.get(AssistantComputeGate, 1).owner_token is None
    start = Barrier(8)
    def acquire(index):
        start.wait(timeout=10)
        with sessions() as db:
            try:
                token = gate.claim(db, 'chat' if index % 2 else 'embedding')
                db.commit()
                return token
            except gate.AssistantUnavailable as error:
                db.rollback()
                return error.reason
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(acquire, range(8)))
    assert results.count('assistant_busy') == 7, results
    token = next(item for item in results if item != 'assistant_busy')
    with sessions() as db:
        gate.request_preemption(db)
        review = ReviewTask(owner=User(username='probe', password_hash='unused'),
            model_node=ModelNode(display_name='Qwen', model_identifier='qwen2.5', base_url='http://unused'),
            input_mode='text', display_name='probe.c', status=TaskStatus.QUEUED, file_count=1)
        db.add(review)
        db.commit()
        identifier = review.id
    with sessions() as db:
        assert not gate.heartbeat(db, token)
        db.commit()
        assert not gate.review_may_start(db)
        db.commit()
        assert gate.acknowledge_idle(db, token)
        db.commit()
        assert gate.review_may_start(db)
        db.commit()
        try:
            gate.claim(db, 'chat')
        except gate.AssistantUnavailable as error:
            assert error.reason == 'review_busy'
            db.rollback()
        else:
            raise AssertionError('queued review must prevent assistant admission')
        db.get(ReviewTask, identifier).status = TaskStatus.COMPLETED
        db.commit()
        new_token = gate.claim(db, 'chat')
        db.commit()
        assert not gate.acknowledge_idle(db, token)
        db.commit()
        assert db.get(AssistantComputeGate, 1).owner_token == new_token
        assert gate.acknowledge_idle(db, new_token)
        db.get(ReviewTask, identifier).graph_build_status = 'running'
        db.commit()
    with sessions() as graph:
        graph.execute(select(ReviewTask).where(ReviewTask.id == identifier).with_for_update())
        with sessions() as contender:
            contender.execute(text('SET SESSION innodb_lock_wait_timeout = 2'))
            began = time.monotonic()
            try:
                gate.claim(contender, 'embedding')
            except gate.AssistantUnavailable as error:
                assert error.reason == 'review_busy'
                contender.rollback()
            else:
                raise AssertionError('a locked active graph must block assistant admission')
            assert time.monotonic() - began < 1.5, 'graph row lock delayed priority admission'
        graph.rollback()
    print('MYSQL_LOCKED_GRAPH_FAST_REJECTION_OK')
    with sessions() as db:
        db.get(ReviewTask, identifier).graph_build_status = 'idle'
        db.commit()
    for operation in ('chat', 'embedding'):
        with sessions() as db:
            owner = gate.claim(db, operation)
            gate.checkpoint_upstream(db, owner, 'a' * 64, operation=operation)
            gate.fail_closed(db, owner, 'simulated interrupted reservation')
            db.commit()
        with sessions() as db:
            assert gate.recover_unsubmitted(db, operation=operation)
            assert gate.review_may_start(db)
            db.commit()
        with sessions() as db:
            try:
                gate.checkpoint_upstream(db, owner, 'a' * 64, 'late-engine', operation=operation)
            except gate.AssistantUnavailable as error:
                assert error.reason == 'assistant_ownership_lost'
                db.rollback()
            else:
                raise AssertionError('recovered owner must not submit late inference')
    print('MYSQL_UNSUBMITTED_RECOVERY_AND_LATE_GATEWAY_FENCING_OK')
    command.downgrade(migration, '0017_review_scan_progress')
    command.upgrade(migration, 'head')
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM users WHERE username='probe'")).scalar_one() == 1
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0021_assistant_control'
    engine.dispose()
    print('MYSQL_ASSISTANT_MIGRATION_ROUNDTRIP_OK: existing users preserved')
    print('MYSQL_REPEATABLE_READ_PRIORITY_PROBE_OK: 8 contenders, one owner; review preemption; late-owner fencing')


if __name__ == '__main__':
    main()
