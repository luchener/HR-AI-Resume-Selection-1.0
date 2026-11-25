"""Durable original removal intents, committed with source redaction."""
import sqlalchemy as sa
from db_models import User

metadata = sa.MetaData()
User.__table__.to_metadata(metadata)
original_gc = sa.Table(
    'original_gc', metadata,
    sa.Column('storage_id', sa.Uuid(), primary_key=True),
    sa.Column('object_key', sa.Text(), primary_key=True),
    sa.Column('owner_user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("storage_id <> '00000000-0000-0000-0000-000000000000'::uuid", name='ck_original_gc_storage'),
    sa.CheckConstraint("object_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.(pdf|docx)$'",
                       name='ck_original_gc_key'),
)
