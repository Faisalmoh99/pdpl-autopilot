"""seed a non-authoritative PDPL starter control set + add AR columns
+ extend findings.status enum with 'not_assessed'

What this migration does, in order:

1. Renames `controls.title` -> `controls.title_en` and
   `controls.description` -> `controls.description_en`, then adds
   `controls.title_ar` and `controls.description_ar` as NOT NULL TEXT.
   The two new columns are added nullable, populated by the seed, then
   altered to NOT NULL. This survives any pre-existing rows; in practice
   the table is empty per docs/02-data-model.md prior to this migration.

2. Extends `findings.status` CHECK constraint by ADDING the value
   `'not_assessed'`. The new value is the baseline-run status emitted
   before any evidence exists. `'unknown'` stays reserved for the
   future case "the deterministic engine ran and could not decide".

3. Inserts 10 starter controls covering the most cited PDPL articles
   (consent, transparency, data-subject rights, breach notification,
   security, cross-border transfer, retention, records of processing).

4. Writes one `audit_log` row with `actor_type='migration'` recording
   the seed event and the codes inserted, so the seed is traceable
   end-to-end through the same audit pipeline as the application.

NON-AUTHORITATIVE STARTER SET — IMPORTANT:
    The 10 controls below have NOT been legally reviewed. They are a
    working approximation of widely cited PDPL obligations, intended
    only to exercise the check-run pipeline and the SCD Type 2
    transition mechanics. They MUST NOT be presented to a customer
    as a complete or authoritative reading of PDPL. The full reviewed
    catalogue is a deferred ADR (see docs/02-data-model.md
    "Open questions"). When that lands, this seed is replaced
    wholesale, not amended in place.

Revision ID: 0003_seed_controls
Revises: 0002_pdpl_app_login
Create Date: 2026-06-12
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0003_seed_controls"
down_revision: Union[str, None] = "0002_pdpl_app_login"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Single source of truth for the codes we seed. Used by both upgrade
# (INSERT) and downgrade (DELETE WHERE code IN (...)) so the downgrade
# removes EXACTLY what the upgrade added — never user-added rows.
_SEED_CONTROL_CODES: tuple[str, ...] = (
    "PDPL-ART4-DSR-ACCESS",
    "PDPL-ART4-DSR-CORRECT",
    "PDPL-ART4-DSR-DELETE",
    "PDPL-ART5-LAWFUL-BASIS",
    "PDPL-ART12-PRIVACY-NOTICE",
    "PDPL-ART19-SECURITY-MEASURES",
    "PDPL-ART20-BREACH-NOTIFY-72H",
    "PDPL-ART25-RETENTION-LIMITS",
    "PDPL-ART29-CROSS-BORDER",
    "PDPL-ART31-ROPA",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Controls: rename title/description to _en and add _ar columns.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE controls RENAME COLUMN title TO title_en;")
    op.execute("ALTER TABLE controls RENAME COLUMN description TO description_en;")

    # Add as nullable, populate via the seed below, then set NOT NULL.
    # If a row pre-exists this migration without a seed value, the final
    # SET NOT NULL fails and the whole migration rolls back — intentional.
    op.execute("ALTER TABLE controls ADD COLUMN title_ar TEXT;")
    op.execute("ALTER TABLE controls ADD COLUMN description_ar TEXT;")

    # ------------------------------------------------------------------
    # 2. Findings: extend status CHECK with 'not_assessed'.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE findings DROP CONSTRAINT ck_findings_status;")
    op.execute(
        """
        ALTER TABLE findings ADD CONSTRAINT ck_findings_status
        CHECK (status IN (
            'compliant',
            'partial',
            'non_compliant',
            'not_applicable',
            'unknown',
            'not_assessed'
        ));
        """
    )

    # ------------------------------------------------------------------
    # 3. Seed the 10 starter controls.
    #
    # gen_random_uuid() is used here (not uuid7) per the noted decision
    # in docs/02-data-model.md: ad-hoc / migration inserts use v4 UUIDs;
    # the natural key in this seed is `code`. The downgrade matches on
    # `code` so v4 vs v7 is irrelevant for reversal.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO controls (
            id, code,
            title_en, title_ar,
            description_en, description_ar,
            category, severity_weight, effective_from
        ) VALUES
        (
            gen_random_uuid(),
            'PDPL-ART4-DSR-ACCESS',
            'Right of access to personal data',
            'حق الوصول إلى البيانات الشخصية',
            'The data subject has the right to obtain access to their personal data held by the controller, including the categories of data, purposes of processing, and recipients.',
            'لصاحب البيانات الشخصية الحق في الوصول إلى بياناته الشخصية المحفوظة لدى جهة التحكم، بما في ذلك فئات البيانات وأغراض المعالجة والجهات المستلمة.',
            'data_subject_rights', 7.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART4-DSR-CORRECT',
            'Right to correction of personal data',
            'حق تصحيح البيانات الشخصية',
            'The data subject has the right to request correction of their personal data when it is inaccurate, incomplete, or outdated.',
            'لصاحب البيانات الشخصية الحق في طلب تصحيح بياناته الشخصية إذا كانت غير صحيحة أو غير مكتملة أو غير محدثة.',
            'data_subject_rights', 6.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART4-DSR-DELETE',
            'Right to deletion of personal data',
            'حق حذف البيانات الشخصية',
            'The data subject has the right to request deletion of their personal data when it is no longer necessary for the purposes for which it was collected, subject to legal retention obligations.',
            'لصاحب البيانات الشخصية الحق في طلب حذف بياناته الشخصية متى انتفت الحاجة إليها للغرض الذي جمعت من أجله، مع مراعاة الالتزامات النظامية للاحتفاظ.',
            'data_subject_rights', 7.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART5-LAWFUL-BASIS',
            'Lawful basis for processing personal data',
            'الأساس النظامي لمعالجة البيانات الشخصية',
            'Personal data may only be processed for a specific, declared, and legitimate purpose, with a lawful basis such as explicit consent, performance of a contract, or compliance with a legal obligation.',
            'لا يجوز معالجة البيانات الشخصية إلا لغرض محدد ومعلن ومشروع، استناداً إلى أساس نظامي كالموافقة الصريحة أو تنفيذ عقد أو الالتزام بنص نظامي.',
            'lawful_basis', 9.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART12-PRIVACY-NOTICE',
            'Privacy notice disclosure to data subjects',
            'إفصاح إشعار الخصوصية لأصحاب البيانات',
            'The controller must disclose to data subjects the purposes of processing, categories of data collected, recipients, retention periods, and rights, in clear and accessible language prior to collection.',
            'يجب على جهة التحكم إفصاح أغراض المعالجة وفئات البيانات المجموعة والجهات المستلمة ومدد الاحتفاظ وحقوق صاحب البيانات بلغة واضحة وسهلة الوصول قبل عملية الجمع.',
            'transparency', 7.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART19-SECURITY-MEASURES',
            'Technical and organisational security measures',
            'التدابير الفنية والتنظيمية لحماية البيانات',
            'The controller must implement technical and organisational measures appropriate to the risk to protect personal data against unauthorised access, disclosure, loss, alteration, or destruction.',
            'يجب على جهة التحكم اتخاذ التدابير الفنية والتنظيمية الملائمة لمستوى الخطر لحماية البيانات الشخصية من الوصول غير المصرح به والإفصاح والفقد والتعديل والإتلاف.',
            'security', 9.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART20-BREACH-NOTIFY-72H',
            'Personal data breach notification within 72 hours',
            'إشعار تسرب البيانات الشخصية خلال 72 ساعة',
            'In the event of a personal data breach likely to harm data subjects, the controller must notify the competent authority within 72 hours of becoming aware of the breach, and notify affected data subjects without undue delay.',
            'في حال وقوع تسرب للبيانات الشخصية قد يضر بأصحابها، يجب على جهة التحكم إبلاغ الجهة المختصة خلال 72 ساعة من علمها بالحادثة، وإبلاغ أصحاب البيانات المتأثرين دون تأخير غير مبرر.',
            'breach_notification', 10.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART25-RETENTION-LIMITS',
            'Retention limits for personal data',
            'حدود الاحتفاظ بالبيانات الشخصية',
            'Personal data must not be retained beyond the period necessary for the purpose of processing, unless a separate legal or regulatory obligation requires longer retention.',
            'لا يجوز الاحتفاظ بالبيانات الشخصية لمدة تتجاوز ما تستلزمه أغراض المعالجة، ما لم يوجد التزام نظامي مستقل يستوجب مدة احتفاظ أطول.',
            'retention', 6.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART29-CROSS-BORDER',
            'Cross-border transfer of personal data',
            'نقل البيانات الشخصية خارج المملكة',
            'Transfer of personal data outside the Kingdom requires an appropriate legal basis and adequate safeguards, in accordance with the conditions set by the competent authority.',
            'يستلزم نقل البيانات الشخصية خارج المملكة وجود أساس نظامي مناسب وضمانات كافية وفق الضوابط التي تحددها الجهة المختصة.',
            'cross_border_transfer', 8.0, DATE '2023-09-14'
        ),
        (
            gen_random_uuid(),
            'PDPL-ART31-ROPA',
            'Records of processing activities',
            'سجل عمليات معالجة البيانات الشخصية',
            'The controller must maintain a record of personal-data processing activities including purposes, categories of data subjects and data, recipients, retention periods, and security measures, and make it available to the competent authority on request.',
            'يجب على جهة التحكم مسك سجل عمليات معالجة البيانات الشخصية شاملاً الأغراض وفئات أصحاب البيانات وفئات البيانات والجهات المستلمة ومدد الاحتفاظ والتدابير الأمنية، وإتاحته للجهة المختصة عند الطلب.',
            'records_of_processing', 5.0, DATE '2023-09-14'
        );
        """
    )

    # ------------------------------------------------------------------
    # 4. Now that every row has values, enforce NOT NULL on the AR cols.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE controls ALTER COLUMN title_ar SET NOT NULL;")
    op.execute("ALTER TABLE controls ALTER COLUMN description_ar SET NOT NULL;")

    # ------------------------------------------------------------------
    # 5. Audit-log row marking the seed event. actor_type='migration'
    #    is one of the four allowed values in ck_audit_log_actor_type.
    #    Payload carries the codes for traceability and the
    #    non_authoritative flag for downstream consumers.
    # ------------------------------------------------------------------
    codes_json = '[' + ', '.join(f'"{c}"' for c in _SEED_CONTROL_CODES) + ']'
    op.execute(
        f"""
        INSERT INTO audit_log (
            id, actor_type, actor_id, event_type,
            entity_type, payload
        ) VALUES (
            gen_random_uuid(),
            'migration',
            'migration:0003_seed_controls',
            'control.seeded',
            'control',
            jsonb_build_object(
                'codes', '{codes_json}'::jsonb,
                'count', {len(_SEED_CONTROL_CODES)},
                'non_authoritative', true,
                'note', 'starter set pending SDAIA review — not legal advice'
            )
        );
        """
    )


def downgrade() -> None:
    # Reverse precisely what upgrade() added. Order matters: drop the
    # seeded controls before reverting the schema changes, so the
    # column drops don't have to deal with rows.

    # If any findings were written referencing these controls, the
    # findings FK is ON DELETE RESTRICT — the DELETE below will fail
    # loudly. That is the intended behaviour: downgrading past a seed
    # that has been used in a real check_run requires the operator to
    # explicitly truncate findings first. We do NOT cascade.
    code_list = ', '.join(f"'{c}'" for c in _SEED_CONTROL_CODES)
    op.execute(f"DELETE FROM controls WHERE code IN ({code_list});")

    # Revert findings.status CHECK to the original set (without 'not_assessed').
    # If any finding row currently has status='not_assessed', this fails —
    # intentional. Operator must resolve those rows before downgrading.
    op.execute("ALTER TABLE findings DROP CONSTRAINT ck_findings_status;")
    op.execute(
        """
        ALTER TABLE findings ADD CONSTRAINT ck_findings_status
        CHECK (status IN (
            'compliant',
            'partial',
            'non_compliant',
            'not_applicable',
            'unknown'
        ));
        """
    )

    # Drop the AR columns, then rename the EN columns back.
    op.execute("ALTER TABLE controls DROP COLUMN description_ar;")
    op.execute("ALTER TABLE controls DROP COLUMN title_ar;")
    op.execute("ALTER TABLE controls RENAME COLUMN description_en TO description;")
    op.execute("ALTER TABLE controls RENAME COLUMN title_en TO title;")
