from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .models import Message, Post, ThreadAnalysis
from .storage_schema import SCHEMA


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- runs ---
    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (_now(),)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
            (_now(), status, json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    # --- threads ---
    def upsert_post(self, post: Post) -> None:
        self.conn.execute(
            """
            INSERT INTO threads (thread_id, source_key, url, title, body, author,
                author_url, created_at, flair, num_comments, stickied, removed,
                raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET
                title = excluded.title, body = excluded.body,
                num_comments = excluded.num_comments, removed = excluded.removed,
                raw_json = excluded.raw_json, fetched_at = excluded.fetched_at
            """,
            (
                post.thread_id, post.source_key, post.url, post.title, post.body,
                post.author, post.author_url, post.created_at.isoformat(), post.flair,
                post.num_comments, int(post.stickied), int(post.removed),
                json.dumps(post.raw_json, ensure_ascii=False), _now(),
            ),
        )
        self.conn.commit()

    def set_skip_reason(self, thread_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE threads SET skip_reason = ? WHERE thread_id = ?", (reason, thread_id)
        )
        self.conn.commit()

    @staticmethod
    def _row_to_post(row: sqlite3.Row) -> Post:
        return Post(
            thread_id=row["thread_id"], source_key=row["source_key"], url=row["url"],
            title=row["title"], body=row["body"], author=row["author"],
            author_url=row["author_url"],
            created_at=datetime.fromisoformat(row["created_at"]),
            flair=row["flair"], num_comments=row["num_comments"],
            stickied=bool(row["stickied"]), removed=bool(row["removed"]),
            raw_json=json.loads(row["raw_json"]),
        )

    def get_post(self, thread_id: str) -> Post | None:
        row = self.conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._row_to_post(row) if row else None

    def posts_without_stage(
        self, stage: str, prompt_version: str, only_candidates: bool = False,
        candidate_prompt_version: str | None = None,
    ) -> list[Post]:
        sql = """
            SELECT t.* FROM threads t
            LEFT JOIN stage_results sr
                ON sr.thread_id = t.thread_id AND sr.stage = ? AND sr.prompt_version = ?
            WHERE t.skip_reason IS NULL
              AND (sr.status IS NULL OR sr.status != 'ok')
        """
        params: list[object] = [stage, prompt_version]
        if only_candidates:
            # Кандидат — вердикт ИМЕННО текущей версии промпта предфильтра: после
            # бампа на prefilter_v2 строка v1 не должна пускать тред в скоринг.
            if candidate_prompt_version is None:
                raise ValueError("only_candidates требует candidate_prompt_version")
            sql += """
              AND EXISTS (
                SELECT 1 FROM stage_results p
                WHERE p.thread_id = t.thread_id AND p.stage = 'prefilter'
                  AND p.status = 'ok' AND p.prompt_version = ?
                  AND json_extract(p.payload_json, '$.candidate') IN (1, 'true')
              )
            """
            params.append(candidate_prompt_version)
        sql += " ORDER BY t.created_at, t.thread_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_post(r) for r in rows]

    # --- stage results ---
    def save_stage_result(
        self, thread_id: str, stage: str, prompt_version: str, status: str,
        payload: dict | None = None, error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO stage_results
                (thread_id, stage, prompt_version, status, payload_json, error, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(thread_id, stage, prompt_version) DO UPDATE SET
                status = excluded.status, payload_json = excluded.payload_json,
                error = excluded.error, updated_at = excluded.updated_at
            """,
            (
                thread_id, stage, prompt_version, status,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                error, _now(),
            ),
        )
        self.conn.commit()

    def get_stage_result(
        self, thread_id: str, stage: str, prompt_version: str
    ) -> dict | None:
        row = self.conn.execute(
            """SELECT status, payload_json, error FROM stage_results
               WHERE thread_id = ? AND stage = ? AND prompt_version = ?""",
            (thread_id, stage, prompt_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
            "error": row["error"],
        }

    # --- messages ---
    def save_messages(self, thread_id: str, messages: list[Message]) -> None:
        self.conn.executemany(
            """
            INSERT INTO messages
                (thread_id, msg_id, parent_id, author, body, created_at, depth, score)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(thread_id, msg_id) DO UPDATE SET
                body = excluded.body, score = excluded.score
            """,
            [
                (m.thread_id, m.msg_id, m.parent_id, m.author, m.body,
                 m.created_at.isoformat(), m.depth, m.score)
                for m in messages
            ],
        )
        self.conn.execute(
            "UPDATE threads SET comments_fetched_at = ? WHERE thread_id = ?",
            (_now(), thread_id),
        )
        self.conn.commit()

    def get_messages(self, thread_id: str) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at, msg_id",
            (thread_id,),
        ).fetchall()
        return [
            Message(
                thread_id=r["thread_id"], msg_id=r["msg_id"], parent_id=r["parent_id"],
                author=r["author"], body=r["body"], depth=r["depth"], score=r["score"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # --- analyses ---
    def save_analysis(
        self, thread_id: str, prompt_version: str, verified: bool,
        analysis: ThreadAnalysis, run_id: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO analyses (thread_id, prompt_version, verified, max_score,
                analysis_json, run_id, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET
                prompt_version = excluded.prompt_version, verified = excluded.verified,
                max_score = excluded.max_score, analysis_json = excluded.analysis_json,
                run_id = excluded.run_id, updated_at = excluded.updated_at
            """,
            (
                thread_id, prompt_version, int(verified), analysis.max_score,
                analysis.model_dump_json(), run_id, _now(),
            ),
        )
        self.conn.commit()

    def get_analysis(self, thread_id: str) -> ThreadAnalysis | None:
        row = self.conn.execute(
            "SELECT analysis_json FROM analyses WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return ThreadAnalysis.model_validate_json(row["analysis_json"]) if row else None

    def threads_for_report(
        self, min_score: int, since: datetime | None = None
    ) -> list[tuple[Post, ThreadAnalysis, bool]]:
        """Треды для отчёта: окно — дата треда, а не прогон.

        Фильтр по `analyses.run_id` врал: строка анализа одна на тред, её
        `run_id` перезаписывает каждый `save_analysis`. `created_at` треда не
        меняется; даты пишутся `isoformat()` в UTC, их лексикографический
        порядок и есть хронологический.
        """
        sql = """
            SELECT t.*, a.analysis_json, a.verified, a.max_score
            FROM analyses a JOIN threads t ON t.thread_id = a.thread_id
            WHERE a.max_score >= ?
        """
        params: list[object] = [min_score]
        if since is not None:
            sql += " AND t.created_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        sql += " ORDER BY a.max_score DESC, t.created_at DESC, t.thread_id"
        out: list[tuple[Post, ThreadAnalysis, bool]] = []
        for row in self.conn.execute(sql, params).fetchall():
            analysis = ThreadAnalysis.model_validate_json(row["analysis_json"])
            out.append((self._row_to_post(row), analysis, bool(row["verified"])))
        return out

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT thread_id) AS n FROM stage_results "
            "WHERE status IN ('error', 'quota')"
        ).fetchone()
        return int(row["n"])

    def stage_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in self.conn.execute(
            "SELECT stage, status, COUNT(*) AS n FROM stage_results GROUP BY stage, status"
        ):
            counts[row["stage"]][row["status"]] = int(row["n"])
        return {stage: dict(statuses) for stage, statuses in counts.items()}
