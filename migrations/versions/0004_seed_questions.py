"""create questions table + seed a non-authoritative starter question set

What this migration does, in order:

1. Creates the `questions` table (ADR-0005): one row per question, each
   linked to exactly one control. Identity is the stable `code`; answers
   stored in `evidence` reference a question by `code`, never by `id`.

2. Grants the `pdpl_app` application role SELECT/INSERT/UPDATE/DELETE on
   `questions`, matching the grant pattern from ADR-0003 (questions is a
   normal mutable table — only `audit_log` is append-only).

3. Seeds a starter set of yes/no questions for FOUR of the ten seeded
   controls (ADR-0006 wires deterministic rules for exactly these four):
     - PDPL-ART12-PRIVACY-NOTICE  (4 questions → can drive partial)
     - PDPL-ART4-DSR-ACCESS       (2 questions)
     - PDPL-ART20-BREACH-NOTIFY-72H (2 questions)
     - PDPL-ART31-ROPA            (1 question)
   Each question resolves its control_id by `code` via a subquery, so the
   seed does not depend on the controls' (v4) UUIDs.

4. Writes one `audit_log` row (actor_type='migration') recording the seed
   and the `non_authoritative` flag, traceable through the same audit
   pipeline as the application.

NON-AUTHORITATIVE STARTER SET — IMPORTANT:
    The questions below have NOT been legally reviewed. They are a working
    approximation intended only to exercise the input -> decision -> finding
    pipeline (ADR-0005, ADR-0006). They MUST NOT be presented to a customer
    as an authoritative PDPL self-assessment. When the SDAIA-reviewed control
    catalogue lands (deferred ADR), these questions are replaced wholesale
    alongside it, not amended in place.

Revision ID: 0004_seed_questions
Revises: 0003_seed_controls
Create Date: 2026-06-13
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_seed_questions"
down_revision: Union[str, None] = "0003_seed_controls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Single source of truth for the question codes we seed. Used by both
# upgrade (INSERT) and downgrade (DELETE WHERE code IN (...)) so the
# downgrade removes EXACTLY what the upgrade added — never user-added rows.
_SEED_QUESTION_CODES: tuple[str, ...] = (
    "Q-ART12-NOTICE-EXISTS",
    "Q-ART12-NOTICE-PURPOSES",
    "Q-ART12-NOTICE-RECIPIENTS",
    "Q-ART12-NOTICE-RIGHTS",
    "Q-ART4-ACCESS-PROCESS",
    "Q-ART4-ACCESS-TIMEFRAME",
    "Q-ART20-BREACH-PROCEDURE",
    "Q-ART20-BREACH-72H",
    "Q-ART31-ROPA-MAINTAINED",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. questions table.
    # ------------------------------------------------------------------
    op.create_table(
        "questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "control_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("controls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("prompt_en", sa.Text(), nullable=False),
        sa.Column("prompt_ar", sa.Text(), nullable=False),
        sa.Column(
            "answer_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'yes_no'"),
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("code", name="uq_questions_code"),
        sa.CheckConstraint("code LIKE 'Q-%'", name="ck_questions_code_prefix"),
        sa.CheckConstraint(
            "answer_type IN ('yes_no')", name="ck_questions_answer_type"
        ),
    )
    # Questions for a control, in display order — the questionnaire render path.
    op.create_index(
        "idx_questions_control_order",
        "questions",
        ["control_id", "display_order"],
    )

    # ------------------------------------------------------------------
    # 2. Grant pdpl_app the same DML it has on the other mutable tables.
    # ------------------------------------------------------------------
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON questions TO pdpl_app;"
    )

    # ------------------------------------------------------------------
    # 3. Seed the starter questions. control_id resolves by control code,
    #    so the seed is independent of the controls' (v4) UUIDs. id uses
    #    gen_random_uuid() per the migration-insert convention; the natural
    #    key is `code` and the downgrade matches on it.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO questions (
            id, control_id, code, prompt_en, prompt_ar, display_order
        ) VALUES
        -- PDPL-ART12-PRIVACY-NOTICE — 4 questions (can drive a 'partial').
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART12-PRIVACY-NOTICE'),
            'Q-ART12-NOTICE-EXISTS',
            'Do you publish a privacy notice to data subjects before collecting their personal data?',
            'هل تنشر إشعار خصوصية لأصحاب البيانات قبل جمع بياناتهم الشخصية؟',
            1
        ),
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART12-PRIVACY-NOTICE'),
            'Q-ART12-NOTICE-PURPOSES',
            'Does the privacy notice state the purposes for which personal data is processed?',
            'هل يوضح إشعار الخصوصية أغراض معالجة البيانات الشخصية؟',
            2
        ),
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART12-PRIVACY-NOTICE'),
            'Q-ART12-NOTICE-RECIPIENTS',
            'Does the privacy notice identify the recipients or categories of recipients of the data?',
            'هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟',
            3
        ),
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART12-PRIVACY-NOTICE'),
            'Q-ART12-NOTICE-RIGHTS',
            'Does the privacy notice explain the data subject''s rights and how to exercise them?',
            'هل يبيّن إشعار الخصوصية حقوق صاحب البيانات وكيفية ممارستها؟',
            4
        ),
        -- PDPL-ART4-DSR-ACCESS — 2 questions.
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART4-DSR-ACCESS'),
            'Q-ART4-ACCESS-PROCESS',
            'Do you have a documented process for handling data-subject access requests?',
            'هل لديك إجراء موثّق للتعامل مع طلبات وصول أصحاب البيانات إلى بياناتهم؟',
            1
        ),
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART4-DSR-ACCESS'),
            'Q-ART4-ACCESS-TIMEFRAME',
            'Do you respond to access requests within a defined timeframe?',
            'هل تستجيب لطلبات الوصول خلال مدة زمنية محددة؟',
            2
        ),
        -- PDPL-ART20-BREACH-NOTIFY-72H — 2 questions.
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART20-BREACH-NOTIFY-72H'),
            'Q-ART20-BREACH-PROCEDURE',
            'Do you have a documented personal-data breach response procedure?',
            'هل لديك إجراء موثّق للاستجابة لتسرب البيانات الشخصية؟',
            1
        ),
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART20-BREACH-NOTIFY-72H'),
            'Q-ART20-BREACH-72H',
            'Does the procedure commit to notifying the competent authority within 72 hours of becoming aware of a breach?',
            'هل يلتزم الإجراء بإبلاغ الجهة المختصة خلال 72 ساعة من العلم بالتسرب؟',
            2
        ),
        -- PDPL-ART31-ROPA — 1 question.
        (
            gen_random_uuid(),
            (SELECT id FROM controls WHERE code = 'PDPL-ART31-ROPA'),
            'Q-ART31-ROPA-MAINTAINED',
            'Do you maintain a record of personal-data processing activities (RoPA)?',
            'هل تحتفظ بسجل لعمليات معالجة البيانات الشخصية؟',
            1
        );
        """
    )

    # ------------------------------------------------------------------
    # 4. Audit-log row marking the seed event. actor_type='migration' is
    #    one of the four allowed values in ck_audit_log_actor_type. Payload
    #    carries the codes for traceability and the non_authoritative flag.
    # ------------------------------------------------------------------
    codes_json = "[" + ", ".join(f'"{c}"' for c in _SEED_QUESTION_CODES) + "]"
    op.execute(
        f"""
        INSERT INTO audit_log (
            id, actor_type, actor_id, event_type,
            entity_type, payload
        ) VALUES (
            gen_random_uuid(),
            'migration',
            'migration:0004_seed_questions',
            'question.seeded',
            'question',
            jsonb_build_object(
                'codes', '{codes_json}'::jsonb,
                'count', {len(_SEED_QUESTION_CODES)},
                'non_authoritative', true,
                'note', 'starter question set pending SDAIA review — not legal advice'
            )
        );
        """
    )


def downgrade() -> None:
    # Reverse precisely what upgrade() added. Delete only the seeded codes
    # (never user-added questions), then drop the table.
    code_list = ", ".join(f"'{c}'" for c in _SEED_QUESTION_CODES)
    op.execute(f"DELETE FROM questions WHERE code IN ({code_list});")

    op.execute("REVOKE ALL ON questions FROM pdpl_app;")
    op.drop_index("idx_questions_control_order", table_name="questions")
    op.drop_table("questions")
