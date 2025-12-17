"""Add NFC UID to User

Revision ID: 59186a157559
Revises: a68dfa86cb1b
Create Date: 2025-06-25 13:53:31.567132

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '59186a157559'
down_revision = 'a68dfa86cb1b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('nfc_uid', sa.String(length=50)))
        batch_op.create_unique_constraint('uq_user_nfc_uid', ['nfc_uid'])
    # ### end Alembic commands ###


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('uq_user_nfc_uid', type_='unique')
        batch_op.drop_column('nfc_uid')

    # ### end Alembic commands ###
