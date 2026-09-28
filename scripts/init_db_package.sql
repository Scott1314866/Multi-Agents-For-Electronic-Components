-- ============================================================
-- AI 元器件封装自动化 · PostgreSQL 初始化脚本
-- 基于 init_db.sql 改造：保留用户/问答/触发器，业务表替换为
-- 封装任务 / PCB封装图 / STEP数模图 / 符号图（引脚图）
-- ============================================================

-- 启用 UUID 自动生成扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- 用户与权限表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    username        VARCHAR(64) NOT NULL,
    email           VARCHAR(128) NOT NULL,
    password_hash   VARCHAR(256) NOT NULL,
    role            VARCHAR(16) NOT NULL CHECK (role IN ('user', 'admin')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX idx_users_tenant_id ON users (tenant_id);
CREATE INDEX idx_users_role ON users (role);

-- ============================================================
-- 封装任务主表：一次上传图纸 → 提取参数 → 派生三类图纸任务
-- ============================================================
CREATE TABLE IF NOT EXISTS package_tasks (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id        VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    student_id       UUID REFERENCES users(id),
    package_name     VARCHAR(64),                          -- 封装名称，如 VFQFPN36
    source_image_path VARCHAR(512),                        -- MinIO 中原始图纸路径
    extract_params   JSONB,                                -- AI 从图纸提取的结构化封装参数
    status           VARCHAR(16) NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'extracting', 'drawing',
                                       'pending_review', 'reviewed', 'failed')),
    error_msg        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_package_tasks_tenant_id ON package_tasks (tenant_id);
CREATE INDEX idx_package_tasks_student_id ON package_tasks (student_id);
CREATE INDEX idx_package_tasks_status ON package_tasks (status);

-- ============================================================
-- PCB 封装图表
-- ============================================================
CREATE TABLE IF NOT EXISTS pcb_drawings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    task_id         UUID REFERENCES package_tasks(id) ON DELETE CASCADE,
    package_params  JSONB,                                -- 生成所用参数快照（与任务参数解耦）
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ai_processing', 'pending_review',
                                      'reviewed', 'failed')),
    output_path     VARCHAR(512),                         -- 生成的封装图文件（MinIO）
    preview_path    VARCHAR(512),                         -- 预览图，便于前端展示/人工复核
    error_msg       TEXT,
    needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_by     UUID REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_pcb_drawings_tenant_id ON pcb_drawings (tenant_id);
CREATE INDEX idx_pcb_drawings_task_id ON pcb_drawings (task_id);
CREATE INDEX idx_pcb_drawings_status ON pcb_drawings (status);
CREATE INDEX idx_pcb_drawings_needs_review ON pcb_drawings (needs_review);

-- ============================================================
-- STEP 数模图表
-- ============================================================
CREATE TABLE IF NOT EXISTS step_drawings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    task_id         UUID REFERENCES package_tasks(id) ON DELETE CASCADE,
    package_params  JSONB,
    source_image_path VARCHAR(512),
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CONSTRAINT step_drawings_status_hitl_check
                    CHECK (status IN ('pending', 'ai_processing', 'awaiting_input',
                                      'pending_review', 'reviewed', 'completed',
                                      'rejected', 'stopped', 'failed')),
    output_path     VARCHAR(512),                         -- .step 文件（MinIO）
    preview_path    VARCHAR(512),                         -- 渲染预览图
    error_msg       TEXT,
    needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_by     UUID REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE step_drawings
    ADD COLUMN IF NOT EXISTS source_image_path VARCHAR(512);
CREATE INDEX idx_step_drawings_tenant_id ON step_drawings (tenant_id);
CREATE INDEX idx_step_drawings_task_id ON step_drawings (task_id);
CREATE INDEX idx_step_drawings_status ON step_drawings (status);
CREATE INDEX idx_step_drawings_needs_review ON step_drawings (needs_review);

-- ============================================================
-- 符号图（引脚图）表
-- ============================================================
CREATE TABLE IF NOT EXISTS symbol_drawings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    task_id         UUID REFERENCES package_tasks(id) ON DELETE CASCADE,
    package_params  JSONB,
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ai_processing', 'pending_review',
                                      'reviewed', 'failed')),
    output_path     VARCHAR(512),                         -- 符号图文件（MinIO）
    preview_path    VARCHAR(512),
    error_msg       TEXT,
    needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_by     UUID REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_symbol_drawings_tenant_id ON symbol_drawings (tenant_id);
CREATE INDEX idx_symbol_drawings_task_id ON symbol_drawings (task_id);
CREATE INDEX idx_symbol_drawings_status ON symbol_drawings (status);
CREATE INDEX idx_symbol_drawings_needs_review ON symbol_drawings (needs_review);

-- ============================================================
-- 问答会话表
-- ============================================================
CREATE TABLE IF NOT EXISTS qa_sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    student_id      UUID REFERENCES users(id),
    thread_id       VARCHAR(128) NOT NULL UNIQUE,         -- LangGraph checkpointer 线程 ID
    summary         TEXT,
    summary_version INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_qa_sessions_tenant_id ON qa_sessions (tenant_id);
CREATE INDEX idx_qa_sessions_student_id ON qa_sessions (student_id);
CREATE INDEX idx_qa_sessions_thread_id ON qa_sessions (thread_id);


-- ============================================================
-- 自动更新 updated_at 触发器
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users',
        'package_tasks',
        'pcb_drawings', 'step_drawings', 'symbol_drawings',
        'qa_sessions'
    ]
    LOOP
        EXECUTE format('
            CREATE TRIGGER trg_%s_updated_at
            BEFORE UPDATE ON %s
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        ', t, t);
    END LOOP;
END;
$$;
