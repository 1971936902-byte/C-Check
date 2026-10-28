import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_assistant_migrations_upgrade_and_downgrade_preserve_users():
    engine = sa.create_engine('sqlite://')
    versions = Path(__file__).resolve().parents[1] / 'alembic/versions'
    names = ['0018_assistant_compute_gate', '0019_assistant_conversations', '0020_assistant_documents',
             '0021_assistant_control_checkpoint']
    with engine.begin() as connection:
        metadata = sa.MetaData()
        users = sa.Table('users', metadata, sa.Column('id', sa.String(36), primary_key=True))
        metadata.create_all(connection)
        connection.execute(users.insert().values(id='existing-user'))
        operations = Operations(MigrationContext.configure(connection))
        modules = []
        for name in names:
            spec = importlib.util.spec_from_file_location(name, versions / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert len(module.revision) <= 32, 'Alembic default version_num is VARCHAR(32)'
            module.op = operations
            module.upgrade()
            modules.append(module)
        assert connection.execute(sa.text('SELECT id FROM assistant_compute_gate')).scalar_one() == 1
        assert {'control_token', 'control_epoch'}.issubset(
            {column['name'] for column in sa.inspect(connection).get_columns('assistant_compute_gate')})
        assert {'assistant_compute_gate', 'assistant_conversations', 'assistant_documents'}.issubset(sa.inspect(connection).get_table_names())
        for module in reversed(modules):
            module.downgrade()
        assert sa.inspect(connection).get_table_names() == ['users']
        assert connection.execute(sa.select(users.c.id)).scalar_one() == 'existing-user'
    engine.dispose()
