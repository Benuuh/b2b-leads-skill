from alembic import context
from sqlalchemy import create_engine, pool
from app.persistence.models import Base
from app.config import settings

target_metadata = Base.metadata
if context.is_offline_mode():
    context.configure(url=settings.database_url,target_metadata=target_metadata,literal_binds=True)
    with context.begin_transaction():context.run_migrations()
else:
    with create_engine(settings.database_url,poolclass=pool.NullPool).connect() as connection:
        from sqlalchemy import inspect, text
        names = set(inspect(connection).get_table_names())
        if names:
            revision = connection.scalar(text('SELECT version_num FROM alembic_version')) if 'alembic_version' in names else None
            if revision not in {'skill_v2_001', 'skill_v2_002'} or names - set(Base.metadata.tables) - {'alembic_version'}:
                raise RuntimeError('Refusing legacy database: configure an isolated SCA_DATABASE_URL')
            added = {'skill_host_limits', 'skill_pending_artifact_deletes', 'skill_robots_cache'} if revision == 'skill_v2_001' else set()
            if set(Base.metadata.tables) - added - names:
                raise RuntimeError('Refusing incomplete Skill database; restore a verified backup')
        connection.rollback()
        context.configure(connection=connection,target_metadata=target_metadata,compare_type=True)
        with context.begin_transaction():context.run_migrations()
