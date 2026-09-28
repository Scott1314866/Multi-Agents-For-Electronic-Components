"""Small idempotent PostgreSQL migrations for application-owned tables."""

from sqlalchemy import text

from backend.dependencies import AsyncSessionLocal


async def run_migrations() -> None:
    """Add STEP inputs and distinguish human pauses from terminal outcomes."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                ALTER TABLE step_drawings
                ADD COLUMN IF NOT EXISTS source_image_path VARCHAR(512)
            """))
            await session.execute(text("""
                DO $$
                DECLARE old_constraint RECORD;
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'step_drawings'::regclass
                          AND conname = 'step_drawings_status_hitl_check'
                    ) THEN
                        -- Replace only checks whose sole column is status;
                        -- preserve unrelated constraints and existing rows.
                        FOR old_constraint IN
                            SELECT conname FROM pg_constraint
                            WHERE conrelid = 'step_drawings'::regclass
                              AND contype = 'c'
                              AND conkey = ARRAY[(
                                  SELECT attnum FROM pg_attribute
                                  WHERE attrelid = 'step_drawings'::regclass
                                    AND attname = 'status'
                              )]::smallint[]
                        LOOP
                            EXECUTE format(
                                'ALTER TABLE step_drawings DROP CONSTRAINT %I',
                                old_constraint.conname
                            );
                        END LOOP;
                        ALTER TABLE step_drawings
                            ADD CONSTRAINT step_drawings_status_hitl_check
                            CHECK (status IN (
                                'pending', 'ai_processing', 'awaiting_input',
                                'pending_review', 'reviewed', 'completed',
                                'rejected', 'stopped', 'failed'
                            ));
                    END IF;
                END; $$
            """))
