"""DDL таблиц SQLite."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    author_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    flair TEXT,
    num_comments INTEGER NOT NULL DEFAULT 0,
    stickied INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    comments_fetched_at TEXT,
    skip_reason TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    thread_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    parent_id TEXT,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (thread_id, msg_id)
);
CREATE TABLE IF NOT EXISTS stage_results (
    thread_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, stage, prompt_version)
);
CREATE TABLE IF NOT EXISTS analyses (
    thread_id TEXT PRIMARY KEY,
    prompt_version TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    max_score INTEGER NOT NULL DEFAULT 0,
    analysis_json TEXT NOT NULL,
    run_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_results_stage ON stage_results(stage, status);
CREATE INDEX IF NOT EXISTS idx_analyses_run ON analyses(run_id, max_score DESC);

-- Учёт отклика (переписки на Reddit) и снимков кармы аккаунта — см. leadparser/outreach.py.
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,                    -- ссылка на threads, может быть NULL
    url TEXT NOT NULL,                 -- ссылка на тред всегда, даже если thread_id известен
    subreddit TEXT,                    -- для разбивки воронки по сабреддитам
    channel TEXT NOT NULL,             -- comment | dm
    sent_at TEXT NOT NULL,
    message TEXT,                      -- что отправили
    status TEXT NOT NULL,              -- sent|replied|negotiating|won|lost|ghosted
    amount REAL, currency TEXT,        -- заполняются при статусе won
    due_date TEXT, paid_at TEXT,
    note TEXT,
    last_checked_at TEXT,              -- автопроверка
    last_seen_entries INTEGER,         -- сколько записей было в ленте треда при прошлой проверке
    comment_visible INTEGER,           -- 1/0/NULL: виден ли наш комментарий в публичной ленте
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS karma (
    recorded_on TEXT PRIMARY KEY,      -- дата, YYYY-MM-DD
    karma INTEGER NOT NULL,
    note TEXT
);

-- Дашборд (leadparser/outreach_dashboard.py): вырос ли тред с прошлой
-- автопроверки. Отдельная таблица, а не колонка в outreach — record_check
-- каждый раз перезаписывает last_seen_entries, "было" негде хранить отдельно
-- от "стало"; и колонку в уже существующую таблицу `CREATE TABLE IF NOT
-- EXISTS` не добавит на базе владельца, где outreach уже есть, а вот новая
-- таблица там создастся безопасно, как и все таблицы этого файла раньше.
CREATE TABLE IF NOT EXISTS outreach_movement (
    outreach_id INTEGER PRIMARY KEY,
    grew INTEGER NOT NULL DEFAULT 0,
    entries_before INTEGER,
    entries_now INTEGER,
    checked_at TEXT NOT NULL
);
"""
