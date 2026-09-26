-- QQ 聊天记录存储 schema（与 service/command_ledger.py 同约定：标准库 sqlite3）
-- 建库：sqlite3 qq-chat-history.sqlite3 < chat-schema.sql

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_name TEXT    NOT NULL,                -- 联系人名（OCR 规范名，冗余存储）
    direction    TEXT    NOT NULL
                 CHECK (direction IN ('in', 'out')),  -- in=对方发的, out=自己发的
    text         TEXT    NOT NULL,                -- 消息文本（OCR 结果 / 发送原文）
    seq          INTEGER NOT NULL,                -- 会话内序号（按联系人递增，稳定排序）
    fingerprint  TEXT    NOT NULL,                -- 去重指纹 sha256(contact|direction|text)
    command_id   TEXT,                            -- out 消息关联的发送命令 ID（与账本打通）
    created_at   TEXT    NOT NULL
                 DEFAULT (datetime('now', 'localtime')),  -- 本地时间写入时刻
    CONSTRAINT uq_contact_seq UNIQUE (contact_name, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_contact_time
    ON messages (contact_name, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_contact_text_time
    ON messages (contact_name, direction, text, created_at);
