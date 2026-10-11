"""持久化 QQ 布局模板；只依赖标准库和 Pillow，不访问桌面。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from PIL import Image

DEFAULT_ID = "qq-9.9.35-52804-x64-white"
TEMPLATES = {
    "search_bar_left": "搜索栏左侧", "search_bar_right": "搜索栏右侧",
    "inputbox_top_left": "输入区左上角", "inputbox_top_right1": "输入区右上角（样式一）",
    "inputbox_top_right2": "输入区右上角（样式二）", "messagebox_top": "消息区顶部",
    "input_buttom_active": "发送按钮（可用）", "input_buttom_deactive": "发送按钮（不可用）",
    "copy_icon": "复制按钮", "switch_to_big": "切换经典模式",
    "switch_to_small": "切换效率模式", "QQ_NotifyIconOverflowWindow": "旧版托盘图标（未使用）",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class LayoutError(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.status = status


def validate_png(data: bytes) -> tuple[int, int]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise LayoutError("LAYOUT_IMAGE_INVALID")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or not (1 <= image.width <= 4096 and 1 <= image.height <= 4096):
                raise ValueError("dimensions or format")
            image.load()
            if image.convert("RGBA").getchannel("A").getextrema()[1] == 0:
                raise ValueError("transparent")
            return image.size
    except Exception as exc:
        raise LayoutError("LAYOUT_IMAGE_INVALID") from exc


class LayoutStore:
    def __init__(self, path: Path, template_dir: Path | None = None):
        self.path = path
        self.boot_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.template_dir = template_dir or Path(__file__).resolve().parents[2] / "templates"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise LayoutError("LAYOUT_DATABASE_VERSION_UNSUPPORTED", 503)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, qq_version TEXT NOT NULL,
                    theme TEXT NOT NULL, architecture TEXT NOT NULL, description TEXT NOT NULL,
                    source_id TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS images (
                    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                    key TEXT NOT NULL, png BLOB NOT NULL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(profile_id,key));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recognition (
                    profile_id TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
            db.execute("INSERT OR IGNORE INTO profiles VALUES (?,?,?,?,?,?,?,?)", (
                DEFAULT_ID, "极简白 · 9.9.35-52804（64 位）", "9.9.35-52804", "极简白", "x64", "内置默认版本", None, time.time()))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('active',?)", (DEFAULT_ID,))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _profile(self, db, profile_id):
        row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise LayoutError("LAYOUT_NOT_FOUND", 404)
        return dict(id=row["id"], name=row["name"], qqVersion=row["qq_version"], theme=row["theme"],
                    architecture=row["architecture"], description=row["description"], sourceId=row["source_id"],
                    createdAt=row["created_at"], builtin=row["id"] == DEFAULT_ID)

    def active_id(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT value FROM settings WHERE key='active'").fetchone()[0]

    def list(self):
        with closing(self.connect()) as db:
            return {"activeId": self.active_id(), "profiles": [self._profile(db, r[0]) for r in db.execute("SELECT id FROM profiles ORDER BY created_at")]}

    def image(self, profile_id, key):
        if key not in TEMPLATES:
            raise LayoutError("LAYOUT_TEMPLATE_NOT_FOUND", 404)
        with closing(self.connect()) as db:
            self._profile(db, profile_id)
            if profile_id == DEFAULT_ID:
                try:
                    return (self.template_dir / f"{key}.png").read_bytes()
                except OSError as exc:
                    raise LayoutError("LAYOUT_TEMPLATE_MISSING", 503) from exc
            row = db.execute("SELECT png FROM images WHERE profile_id=? AND key=?", (profile_id, key)).fetchone()
            if row is None:
                raise LayoutError("LAYOUT_TEMPLATE_MISSING", 503)
            return bytes(row[0])

    def detail(self, profile_id):
        with closing(self.connect()) as db:
            result = self._profile(db, profile_id)
        images = []
        for key, label in TEMPLATES.items():
            data = self.image(profile_id, key)
            width, height = validate_png(data)
            images.append(dict(key=key, label=label, width=width, height=height, sha256=hashlib.sha256(data).hexdigest()))
        return {**result, "templates": images}

    def create(self, *, sourceId, name, qqVersion, theme, architecture, description="", replacements=None):
        replacements = replacements or {}
        if set(replacements) - TEMPLATES.keys():
            raise LayoutError("LAYOUT_TEMPLATE_NOT_FOUND", 404)
        # 完整副本与元数据一次提交；来源删除与复制串行，图片永不原地覆盖。
        with self.lock, closing(self.connect()) as db, db:
            self._profile(db, sourceId)
            prepared = []
            for key in TEMPLATES:
                try:
                    data = base64.b64decode(replacements[key], validate=True) if key in replacements else self.image(sourceId, key)
                except (ValueError, TypeError) as exc:
                    raise LayoutError("LAYOUT_IMAGE_INVALID") from exc
                width, height = validate_png(data)
                prepared.append((key, data, width, height, hashlib.sha256(data).hexdigest()))
            profile_id = str(uuid.uuid4())
            db.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?)", (profile_id, name, qqVersion, theme, architecture, description, sourceId, time.time()))
            db.executemany("INSERT INTO images VALUES (?,?,?,?,?,?)", [(profile_id, *row) for row in prepared])
        return self.detail(profile_id)

    def update(self, profile_id, name, description):
        with self.lock, closing(self.connect()) as db, db:
            self._profile(db, profile_id)
            if profile_id == DEFAULT_ID:
                raise LayoutError("LAYOUT_DEFAULT_PROTECTED", 409)
            db.execute("UPDATE profiles SET name=?, description=? WHERE id=?", (name, description, profile_id))
        return self.detail(profile_id)

    def delete(self, profile_id):
        with self.lock, closing(self.connect()) as db, db:
            self._profile(db, profile_id)
            if profile_id == DEFAULT_ID:
                raise LayoutError("LAYOUT_DEFAULT_PROTECTED", 409)
            if self.active_id() == profile_id:
                raise LayoutError("LAYOUT_ACTIVE_PROTECTED", 409)
            db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

    def activate(self, profile_id):
        with self.lock:
            self.detail(profile_id)  # 缺图/损坏时不改变当前选择。
            with closing(self.connect()) as db, db:
                db.execute("UPDATE settings SET value=? WHERE key='active'", (profile_id,))

    def save_recognition(self, profile_id, payload):
        with self.lock, closing(self.connect()) as db, db:
            self._profile(db, profile_id)
            db.execute("INSERT OR REPLACE INTO recognition VALUES (?,?)", (profile_id, json.dumps({**payload, "bootId": self.boot_id})))

    def mark_recognition_stale(self, profile_id, reason):
        """保留上一次坐标，只更新其状态；失败不能抹掉可供核对的历史定位。"""
        with self.lock, closing(self.connect()) as db, db:
            self._profile(db, profile_id)
            row = db.execute("SELECT payload FROM recognition WHERE profile_id=?", (profile_id,)).fetchone()
            payload = json.loads(row[0]) if row else dict(profileId=profile_id, templates=[], regions={})
            payload.update(valid=False, staleReason=reason, bootId=self.boot_id)
            db.execute("INSERT OR REPLACE INTO recognition VALUES (?,?)", (profile_id, json.dumps(payload)))

    def recognition(self, profile_id):
        with closing(self.connect()) as db:
            self._profile(db, profile_id)
            row = db.execute("SELECT payload FROM recognition WHERE profile_id=?", (profile_id,)).fetchone()
        return json.loads(row[0]) if row else None
