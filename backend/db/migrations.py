"""Small idempotent PostgreSQL migrations for application-owned tables."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies import AsyncSessionLocal

#: STEP 的九态：把"人工暂停"与"终态"分开，stopped 与 failed 泾渭分明。
STEP_STATUSES: tuple[str, ...] = (
    "pending",
    "ai_processing",
    "awaiting_input",
    "pending_review",
    "reviewed",
    "completed",
    "rejected",
    "stopped",
    "failed",
)

#: PCB / 符号两张表原本只有五态（pending / ai_processing / pending_review /
#: reviewed / failed），支持 HITL 暂停后同样要扩到九态。
HITL_STATUSES: tuple[str, ...] = STEP_STATUSES


async def _replace_status_check(
    session: AsyncSession,
    table: str,
    constraint: str,
    statuses: tuple[str, ...],
) -> None:
    """幂等地把某表 ``status`` 列的 CHECK 约束换成给定集合。

    表名与约束名来自本模块常量，不接受外部输入。只替换"唯一列为 status"
    的 CHECK 约束 —— 其他约束与既有数据一律保留。
    """
    allowed = ", ".join(f"'{status}'" for status in statuses)
    await session.execute(
        text(f"""
            DO $$
            DECLARE old_constraint RECORD;
            BEGIN
                IF to_regclass('{table}') IS NULL THEN
                    RETURN;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = '{table}'::regclass
                      AND conname = '{constraint}'
                ) THEN
                    FOR old_constraint IN
                        SELECT conname FROM pg_constraint
                        WHERE conrelid = '{table}'::regclass
                          AND contype = 'c'
                          AND conkey = ARRAY[(
                              SELECT attnum FROM pg_attribute
                              WHERE attrelid = '{table}'::regclass
                                AND attname = 'status'
                          )]::smallint[]
                    LOOP
                        EXECUTE format(
                            'ALTER TABLE {table} DROP CONSTRAINT %I',
                            old_constraint.conname
                        );
                    END LOOP;
                    ALTER TABLE {table}
                        ADD CONSTRAINT {constraint}
                        CHECK (status IN ({allowed}));
                END IF;
            END; $$
        """)
    )


async def run_migrations() -> None:
    """补齐 STEP 的输入列，并让三张绘图表都支持人工暂停。

    失败即抛错，让启动流程中止 —— 宁可起不来，也不要接收了任务却存不下
    暂停点。
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                    ALTER TABLE step_drawings
                    ADD COLUMN IF NOT EXISTS source_image_path VARCHAR(512)
                """)
            )
            await _replace_status_check(
                session, "step_drawings", "step_drawings_status_hitl_check", STEP_STATUSES
            )
            for table in ("pcb_drawings", "symbol_drawings"):
                await _replace_status_check(
                    session, table, f"{table}_status_hitl_check", HITL_STATUSES
                )
