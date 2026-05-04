"""forms + form_responses — Commons Form Builder.

Backs ``/api/v1/commons/form`` — Surface's open-source replacement for
Typeform / Tally / Google Forms. A form is a JSON schema authored by a
service-key caller (currently AgentNation on behalf of a Surface user);
responses are public-submittable against the form_id.

Two tables ship together because both are needed end-to-end:

  - forms           — one row per form. ``schema`` jsonb holds field defs.
  - form_responses  — one row per submitted response, FK→forms.

ip_hash on form_responses is a SHA-256 over the submitter IP + a daily
salt; used for soft rate-limit / dedupe heuristics, never reversible.

Revision ID: t3f9b1c4d8e6
Revises: s2k1n3i4r5m
Create Date: 2026-05-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "t3f9b1c4d8e6"
down_revision = "s2k1n3i4r5m"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forms",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column(
            "schema", postgresql.JSONB, nullable=False,
            comment="JSON: {fields:[{id,type,label,required,options?}]}",
        ),
        sa.Column(
            "created_by", sa.String(36), nullable=False,
            comment="agents.id of the form owner.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=True,
            comment="Soft-delete tombstone. Responses preserved for audit.",
        ),
    )
    op.create_index("ix_forms_created_by", "forms", ["created_by"])

    op.create_table(
        "form_responses",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "form_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("forms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "answers", postgresql.JSONB, nullable=False,
            comment="JSON map: {field_id: value}",
        ),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "ip_hash", sa.String(64), nullable=True,
            comment="SHA-256(ip + daily-salt). Not reversible.",
        ),
    )
    op.create_index(
        "ix_form_responses_form_id_submitted_at",
        "form_responses",
        ["form_id", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_form_responses_form_id_submitted_at", table_name="form_responses")
    op.drop_table("form_responses")
    op.drop_index("ix_forms_created_by", table_name="forms")
    op.drop_table("forms")
