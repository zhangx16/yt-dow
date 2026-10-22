"""Private Telegram downloader. SQLite records every external side effect.

Requires a local Bot API server sharing /work, yt-dlp, ffmpeg and rclone.
An interrupted Telegram upload is held for manual retry to avoid duplicates.
"""
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("video-bot")
STOP = threading.Event()
GIB = 1024 ** 3
QUALITIES = {10000: "原画", 2160: "最高 4K", 1080: "最高 1080P", 720: "最高 720P", 480: "最高 480P"}


class Hold(RuntimeError):
    pass


def archive_video_name(title, suffix):
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", str(title or "未命名视频"))
    title = title.strip().strip(".") or "未命名视频"
    # Keep the full path component below filesystem byte limits, preserving Unicode.
    title = title.encode("utf-8")[:200].decode("utf-8", errors="ignore").rstrip(" .")
    return (title or "未命名视频") + suffix


def valid_url(value):
    u = urllib.parse.urlsplit(value)
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        raise Hold("请发送 HTTPS 视频页面链接")
    # Only known video sites; never accept arbitrary internal HTTP endpoints.
    hosts = ("youtube.com", "youtu.be", "bilibili.com", "b23.tv", "douyin.com",
             "tiktok.com", "vimeo.com", "twitter.com", "x.com", "instagram.com")
    host = (u.hostname or "").lower().rstrip(".")
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise Hold("暂不支持该站点")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise Hold("视频地址解析异常")
    return value


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE, chat INTEGER, url TEXT,
          state TEXT DEFAULT 'queued', meta TEXT DEFAULT '{}', file TEXT DEFAULT '',
          remote TEXT DEFAULT '', manifest TEXT DEFAULT '{}', parts TEXT DEFAULT '[]',
          sent TEXT DEFAULT '[]', error TEXT DEFAULT '', retry_at REAL DEFAULT 0,
          attempts INTEGER DEFAULT 0, notified INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for name, definition in (("progress_message", "INTEGER DEFAULT 0"),
                                 ("progress_text", "TEXT DEFAULT ''"),
                                 ("quality", "INTEGER DEFAULT 1080")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.db.commit()

    def update(self, job_id, **kw):
        self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in kw) + " WHERE id=?",
                        [*kw.values(), job_id])
        self.db.commit()

    def get(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


class Bot:
    def __init__(self):
        self.root = Path(os.environ.get("WORK_DIR", "/work"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = Path(os.environ.get("STATE_DIR", "/state"))
        self.state.mkdir(parents=True, exist_ok=True)
        self.token = os.environ["TG_BOT_TOKEN"]
        self.allowed = {int(x) for x in os.environ["TG_ALLOWED_USERS"].split(",") if x.strip()}
        if not self.allowed:
            raise ValueError("TG_ALLOWED_USERS is required")
        self.api = os.environ.get("TG_API_BASE", "http://telegram-api:8081").rstrip("/")
        self.remote = os.environ.get("DRIVE_TARGET", "gdrive:Media/TelegramVideos").rstrip("/")
        self.rconfig = os.environ.get("RCLONE_CONFIG", "/rclone/rclone.conf")
        self.budget = float(os.environ.get("WORK_BUDGET_GIB", "15")) * GIB
        self.reserve = float(os.environ.get("MIN_FREE_GIB", "15")) * GIB
        self.limit = int(os.environ.get("TG_MAX_BYTES", "1900000000"))
        self.height = int(os.environ.get("VIDEO_HEIGHT", "1080"))
        self.lock = open(self.state / "worker.lock", "a+")
        import fcntl
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tg(self, method, **fields):
        data = json.dumps(fields).encode()
        req = urllib.request.Request(f"{self.api}/bot{self.token}/{method}", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1800 if method.startswith("send") else 50) as r:
                result = json.load(r)
        except urllib.error.HTTPError as e:
            raise Hold(f"Telegram {method} HTTP {e.code}") from None
        except (OSError, ValueError):
            raise Hold(f"Telegram {method} 连接失败") from None
        if not result.get("ok"):
            raise Hold(f"Telegram {method} 请求失败")
        return result["result"]

    def notify(self, chat, text):
        try:
            return self.tg("sendMessage", chat_id=chat, text=text[:4000])
        except Hold:
            LOG.warning("notification failed")

    def setup_menu(self):
        self.tg("setMyCommands", commands=[
            {"command": "start", "description": "开始使用 / 发送视频链接"},
            {"command": "list", "description": "任务列表与下载进度"},
            {"command": "status", "description": "查看最近任务状态"},
            {"command": "errors", "description": "查看报错和暂停任务"},
            {"command": "retry", "description": "重试任务：/retry 任务编号"},
            {"command": "help", "description": "使用帮助"}])
        self.tg("setChatMenuButton", menu_button={"type": "commands"})

    def progress(self, db, row, stage, percent=None, speed="", eta=""):
        current = db.get(row["id"])
        meta = json.loads(current["meta"])
        text = f"任务 {row['id']} · {stage}\n{meta.get('title', '正在解析视频')}\n作者：{meta.get('author', '未知作者')}"
        if percent is not None:
            percent = max(0, min(100, float(percent)))
            filled = min(10, int(percent / 10))
            text += f"\n{'█' * filled}{'░' * (10 - filled)} {percent:.1f}%"
        if speed and speed != "NA":
            text += f"\n速度：{speed}"
        if eta and eta != "NA":
            text += f" · 剩余：{eta}"
        if text == current["progress_text"]:
            return
        try:
            if current["progress_message"]:
                self.tg("editMessageText", chat_id=row["chat"], message_id=current["progress_message"], text=text[:4000])
            else:
                message = self.notify(row["chat"], text)
                if message:
                    db.update(row["id"], progress_message=message["message_id"])
            db.update(row["id"], progress_text=text)
        except Hold:
            # A deleted message or a transient rate limit must never fail a download.
            LOG.warning("progress update failed for job %s", row["id"])

    def download_progress(self, db, row, line):
        if not line.startswith("tgprogress:"):
            return
        parts = line.removeprefix("tgprogress:").split("|")
        try:
            percent = float(parts[0].strip().rstrip("%"))
        except ValueError:
            return
        self.progress(db, row, "下载中（当前音视频分轨）", percent,
                      parts[1].strip() if len(parts) > 1 else "",
                      parts[2].strip() if len(parts) > 2 else "")

    def disk_ok(self):
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        return used < self.budget and shutil.disk_usage(self.root).free > self.reserve

    def run(self, args, timeout=7200, guard=False, progress=None):
        # Capture on disk, not in RAM; never log URLs, OAuth data or bot credentials.
        import tempfile
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(args, stdout=out, stderr=err, start_new_session=True)
            start = time.monotonic()
            cursor, pending, latest, updated = 0, b"", "", 0
            def drain(force=False):
                nonlocal cursor, pending, latest, updated
                if progress is None:
                    return
                while True:
                    chunk = os.pread(out.fileno(), 65536, cursor)
                    if not chunk:
                        break
                    cursor += len(chunk)
                    lines = (pending + chunk).split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        if line.startswith(b"tgprogress:"):
                            latest = line.decode(errors="replace")
                now = time.monotonic()
                if latest and (force or now - updated >= 4):
                    progress(latest)
                    latest, updated = "", now
            try:
                while p.poll() is None:
                    if STOP.wait(.5) or time.monotonic() - start > timeout:
                        raise Hold("任务中断或超时")
                    if guard and not self.disk_ok():
                        raise Hold("临时空间达到上限，任务已暂停")
                    drain()
                drain(force=True)
                if p.returncode:
                    raise Hold(f"{Path(args[0]).name} 执行失败")
                out.seek(0)
                return out.read()
            finally:
                if p.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(p.pid, signal.SIGKILL)
                    p.wait()

    def rc(self, *args):
        return self.run(["rclone", "--config", self.rconfig, *args], timeout=14400)

    def workpath(self, job_id):
        return self.root / str(int(job_id))

    def download(self, db, row):
        if not self.disk_ok():
            raise Hold("磁盘预留空间不足")
        valid_url(row["url"])
        folder = self.workpath(row["id"])
        folder.mkdir(exist_ok=True)
        common = ["yt-dlp", "--ignore-config", "--js-runtimes", "node", "--no-playlist", "--no-warnings", "--no-colors",
                  "--socket-timeout", "30", "--retries", "3", "--"]
        raw = self.run(common[:-1] + ["--dump-single-json", "--skip-download", "--", row["url"]], 120)
        meta = json.loads(raw)
        if meta.get("is_live") or meta.get("_type") == "playlist":
            raise Hold("仅支持非直播的单视频")
        info = {"title": meta.get("title") or "未命名视频",
                "author": meta.get("uploader") or meta.get("channel") or "未知作者",
                "source": row["url"], "id": meta.get("id"), "duration": meta.get("duration")}
        db.update(row["id"], meta=json.dumps(info, ensure_ascii=False))
        self.progress(db, row, "开始下载", 0)
        height = row.get("quality", self.height)
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]"
        output = self.run(common[:-1] + ["-f", fmt, "--merge-output-format", "mp4",
            "--format-sort", "res,vcodec:h264,acodec:aac",
            "--remux-video", "mp4", "--max-filesize", str(int(self.budget / 4)),
            "--progress", "--newline", "--progress-delta", "1",
            "--progress-template", "download:tgprogress:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
            "--print", "after_move:filepath:%(filepath)s", "-o", str(folder / "video.%(ext)s"),
            "--", row["url"]], guard=True, progress=lambda line: self.download_progress(db, row, line)).decode()
        paths = [Path(x.removeprefix("filepath:")) for x in output.splitlines() if x.startswith("filepath:")]
        if len(paths) != 1 or paths[0].resolve().parent != folder.resolve() or not paths[0].is_file():
            raise Hold("下载未生成完整文件")
        video = paths[0]
        (folder / "metadata.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        db.update(row["id"], state="downloaded", file=str(video), attempts=0)
        self.progress(db, row, "下载及合并完成，准备备份", 100)

    def archive(self, db, row):
        folder = self.workpath(row["id"])
        files = [Path(row["file"]), folder / "metadata.json"]
        meta = json.loads(row["meta"])
        target = row["remote"] or f"{self.remote}/{archive_video_name(meta.get('author') or '未知作者', '')}"
        video_name = archive_video_name(meta.get("title"), files[0].suffix)
        # Each video has its own sidecar within the shared author directory.
        metadata_name = Path(video_name).stem + ".metadata.json"
        manifest = {}
        names = {}
        for p in files:
            if not p.is_file():
                raise Hold("本地归档文件缺失")
            with p.open("rb") as f:
                md5 = hashlib.file_digest(f, "md5").hexdigest()
            name = video_name if p == files[0] else metadata_name
            names[p] = name
            manifest[name] = {"size": p.stat().st_size, "md5": md5, "local_name": p.name}
        self.rc("mkdir", target)
        existing = json.loads(self.rc("lsjson", target, "--files-only", "--hash", "--hash-type", "MD5"))
        collisions = False
        for item in existing:
            expected = manifest.get(item["Name"])
            if expected:
                digest = {k.lower(): v for k, v in item.get("Hashes", {}).items()}.get("md5", "")
                if item["Size"] != expected["size"] or digest.lower() != expected["md5"]:
                    collisions = True
        if collisions:
            unique = f" [{self.namespace}-{row['id']}]"
            renamed = {}
            for p, name in names.items():
                suffix = p.suffix if p == files[0] else ".metadata.json"
                name = archive_video_name(meta.get("title"), unique + suffix)
                renamed[name] = manifest[names[p]]
                names[p] = name
            manifest = renamed
        db.update(row["id"], remote=target, manifest=json.dumps(manifest))
        for p in files:
            self.rc("copyto", str(p), f"{target}/{names[p]}", "--immutable", "--checksum", "--transfers", "1")
        self.verify(target, manifest)
        db.update(row["id"], state="archived", attempts=0)

    def verify(self, target, manifest):
        listing = json.loads(self.rc("lsjson", target, "--files-only", "--hash", "--hash-type", "MD5"))
        actual = {}
        for item in listing:
            if item["Name"] in actual:
                raise Hold("云盘发现同名文件，停止清理")
            actual[item["Name"]] = item
        for name, expected in manifest.items():
            item = actual.get(name, {})
            md5 = {k.lower(): v for k, v in item.get("Hashes", {}).items()}.get("md5", "")
            if item.get("Size") != expected["size"] or md5.lower() != expected["md5"]:
                raise Hold("云盘大小或 MD5 校验未通过")

    def prepare_parts(self, db, row):
        video = Path(row["file"])
        if video.stat().st_size <= self.limit:
            parts = [str(video)]
        else:
            duration = float(json.loads(row["meta"]).get("duration") or 0)
            if duration <= 0:
                raise Hold("视频过大且缺少时长，无法自动分段")
            folder = self.workpath(row["id"]) / "parts"
            folder.mkdir(exist_ok=True)
            for p in folder.glob("*.mp4"):
                p.unlink()
            seconds = max(1, int(duration * self.limit / video.stat().st_size * .65))
            self.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", "0:v:0",
                      "-map", "0:a?", "-c", "copy", "-f", "segment", "-segment_time", str(seconds),
                      "-reset_timestamps", "1", str(folder / "%04d.mp4")], guard=True)
            parts = [str(p) for p in sorted(folder.glob("*.mp4"))]
            if not parts or any(Path(p).stat().st_size > self.limit for p in parts):
                raise Hold("分段仍超过 TG 限制，原视频已备份，请降低清晰度后重试")
        db.update(row["id"], parts=json.dumps(parts))
        return parts

    def restore(self, row):
        """Recover a missing working copy from the verified Drive archive."""
        manifest = json.loads(row["manifest"])
        if not manifest or not row["remote"]:
            raise Hold("云盘恢复记录缺失")
        self.verify(row["remote"], manifest)
        folder = self.workpath(row["id"])
        folder.mkdir(exist_ok=True)
        needed = sum(item["size"] for item in manifest.values())
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        if used + needed > self.budget or shutil.disk_usage(self.root).free - needed < self.reserve:
            raise Hold("恢复视频所需的临时空间不足")
        for name, expected in manifest.items():
            local_name = expected.get("local_name", name)
            if any(Path(n).name != n or n in (".", "..") for n in (name, local_name)):
                raise Hold("云盘恢复路径异常")
            path = folder / local_name
            self.run(["rclone", "--config", self.rconfig, "copyto", row["remote"] + "/" + name,
                      str(path), "--checksum"], timeout=14400, guard=True)
            with path.open("rb") as f:
                md5 = hashlib.file_digest(f, "md5").hexdigest()
            if path.stat().st_size != expected["size"] or md5 != expected["md5"]:
                raise Hold("从云盘恢复的文件校验失败")

    def deliver(self, db, row):
        parts = json.loads(row["parts"]) or self.prepare_parts(db, row)
        sent = json.loads(row["sent"])
        meta = json.loads(row["meta"])
        for index in range(len(sent), len(parts)):
            path = Path(parts[index])
            if not path.is_file():
                raise Hold("待发送文件缺失")
            caption = f"{meta['title'][:500]}\n作者：{meta['author'][:200]}\n{row['url'][:250]}"
            if len(parts) > 1:
                caption += f"\n分段 {index + 1}/{len(parts)}"
            db.update(row["id"], state="sending")
            # Server reads this exact path via a shared, read-only /work mount.
            probe = json.loads(self.run(["ffprobe", "-v", "error", "-show_streams",
                                         "-of", "json", str(path)], timeout=60))
            streams = probe.get("streams", [])
            playable = (any(s.get("codec_name") == "h264" for s in streams)
                        and all(s.get("codec_name") in ("aac", "mp3")
                                for s in streams if s.get("codec_type") == "audio"))
            method, field = ("sendVideo", "video") if playable else ("sendDocument", "document")
            # Telegram caption limits count UTF-16 code units, including emoji pairs.
            caption = caption.encode("utf-16-le")[:2000].decode("utf-16-le", errors="ignore")
            message = self.tg(method, chat_id=row["chat"], **{field: path.as_uri()}, caption=caption)
            sent.append({"message_id": message["message_id"],
                         "file_id": message.get(field, {}).get("file_id", "")})
            db.update(row["id"], sent=json.dumps(sent), state="archived")
        db.update(row["id"], state="sent", attempts=0)

    def process(self, db, row):
        job_id = row["id"]
        if row["state"] in ("queued", "downloading"):
            db.update(job_id, state="downloading")
            self.download(db, row)
        row = db.get(job_id)
        if row["state"] == "downloaded":
            self.progress(db, row, "正在备份 Google Drive，完成后回传 TG")
            self.archive(db, row)
        row = db.get(job_id)
        if row["state"] == "archived":
            self.progress(db, row, "云盘备份已验证，正在准备回传 TG")
            if not Path(row["file"]).is_file():
                self.restore(row)
                db.update(job_id, parts="[]")
                # Rebuild missing segments but retain the count already delivered.
                row = db.get(job_id)
            self.deliver(db, row)
        row = db.get(job_id)
        if row["state"] == "sent":
            self.verify(row["remote"], json.loads(row["manifest"]))
            folder = self.workpath(job_id)
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=False)
            db.update(job_id, state="done", error="")
            self.progress(db, row, "已回传 TG · 云盘已备份 · 本地已清理", 100)
            self.notify(row["chat"], f"任务 {job_id} 已发送并完成云盘备份，本地临时文件已清理。")

    def worker(self):
        db = Store(self.state / "jobs.sqlite")
        # Telegram has no idempotency key. Never automatically repeat an uncertain send.
        db.db.execute("UPDATE jobs SET state='held', error='上次发送结果不确定，请检查聊天后 /retry ID' WHERE state='sending'")
        db.db.commit()
        while not STOP.is_set():
            # Finish every existing file before admitting another download.
            row = db.db.execute("""SELECT * FROM jobs WHERE state NOT IN ('done','held','choosing')
              ORDER BY CASE WHEN state='queued' THEN 1 ELSE 0 END,id LIMIT 1""").fetchone()
            if not row or row["retry_at"] > time.time():
                STOP.wait(2)
                continue
            row = dict(row)
            if row["state"] == "queued":
                held = db.db.execute("SELECT id FROM jobs WHERE state='held'").fetchall()
                if any(self.workpath(r["id"]).exists() for r in held):
                    STOP.wait(2)
                    continue
            try:
                self.process(db, row)
            except Exception as e:
                current = db.get(row["id"])
                ambiguous = current["state"] == "sending"
                attempts = current["attempts"] + 1
                # Never expose subprocess stderr / URLs containing credentials.
                reason = str(e)[:200] if isinstance(e, Hold) else type(e).__name__
                db.update(row["id"], state="held" if ambiguous or attempts >= 5 else current["state"],
                          error=reason, attempts=attempts, retry_at=time.time() + min(1800, 30 * 2 ** attempts))
                self.progress(db, row, f"{'已暂停' if ambiguous or attempts >= 5 else '等待重试'}：{reason}")
                if attempts == 1 or ambiguous or attempts == 5:
                    self.notify(row["chat"], f"任务 {row['id']}：{reason}。用 /status 查询；暂停任务可 /retry {row['id']}。")
                LOG.warning("job %s failed: %s", row["id"], reason)

    def handle_quality(self, db, query):
        msg = query.get("message", {})
        if query.get("from", {}).get("id") not in self.allowed or msg.get("chat", {}).get("type") != "private":
            return
        try:
            prefix, job_id, height = query.get("data", "").split(":")
            job_id, height = int(job_id), int(height)
            if prefix != "quality" or height not in QUALITIES:
                raise ValueError
        except ValueError:
            return
        row = db.get(job_id)
        if not row or row["chat"] != msg["chat"]["id"]:
            return
        changed = db.db.execute("UPDATE jobs SET state='queued',quality=? WHERE id=? AND state='choosing'",
                                (height, job_id)).rowcount
        db.db.commit()
        try:
            self.tg("answerCallbackQuery", callback_query_id=query["id"],
                    text="已加入下载队列" if changed else "此任务已经选择过清晰度")
            if changed:
                self.tg("editMessageText", chat_id=row["chat"], message_id=msg["message_id"],
                        text=f"任务 {job_id} · 已选择 {QUALITIES[height]}，等待下载\n{row['url']}",
                        reply_markup={"inline_keyboard": []})
        except Hold:
            LOG.warning("quality confirmation failed for job %s", job_id)

    def handle(self, db, update):
        if update.get("callback_query"):
            return self.handle_quality(db, update["callback_query"])
        msg = update.get("message", {})
        if msg.get("from", {}).get("id") not in self.allowed or msg.get("chat", {}).get("type") != "private":
            return
        chat = msg["chat"]["id"]
        text = msg.get("text", "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if command in ("/start", "/help"):
            self.notify(chat, "发送单个视频的 HTTPS 链接，再点击按钮选择清晰度。源视频不足所选清晰度时使用较低的可用画质。视频和标题作者回传 TG，原文件按作者备份 Drive。\n下载通知每约 4 秒更新进度（音视频分轨分别计数）。\n/list 任务列表\n/status 任务状态\n/errors 报错任务\n/retry ID 重试暂停任务（发送结果不确定时可能重复）")
        elif command in ("/list", "/status", "/errors"):
            extra = " AND (state='held' OR error!='')" if command == "/errors" else ""
            rows = db.db.execute("SELECT * FROM jobs WHERE chat=?" + extra + " ORDER BY id DESC LIMIT 10", (chat,))
            labels = {"choosing": "等待选择清晰度", "queued": "排队中", "downloading": "下载中", "downloaded": "等待备份",
                      "archived": "已备份，等待发送", "sending": "正在回传", "sent": "正在清理",
                      "done": "已完成", "held": "已暂停"}
            entries = []
            for r in rows:
                title = json.loads(r["meta"]).get("title", "待解析视频")
                entry = f"任务 {r['id']} · {labels.get(r['state'], r['state'])}\n{title[:100]}"
                if r["state"] != "done" and r["progress_text"]:
                    entry += "\n" + "\n".join(r["progress_text"].splitlines()[3:])
                if r["error"]:
                    entry += f"\n报错：{r['error']}"
                if r["state"] == "held":
                    entry += f"\n重试：/retry {r['id']}"
                entries.append(entry)
            self.notify(chat, "\n\n".join(entries) or ("暂无报错任务" if command == "/errors" else "暂无任务"))
        elif command == "/retry" and len(text.split()) == 1:
            self.notify(chat, "请发送 /retry 任务编号，例如 /retry 1。可先用 /errors 查看暂停任务。")
        elif text.startswith("/retry "):
            try:
                row = db.get(int(text.split()[1]))
            except (ValueError, IndexError):
                row = None
            if not row or row["chat"] != chat or row["state"] != "held":
                self.notify(chat, "未找到暂停任务")
                return
            state = "archived" if row["manifest"] != "{}" and row["remote"] else "queued"
            # A manifest is saved before upload: verify before resuming at archived.
            if state == "archived":
                try:
                    self.verify(row["remote"], json.loads(row["manifest"]))
                except Exception:
                    state = "downloaded"
            db.update(row["id"], state=state, attempts=0, retry_at=0, error="")
            self.notify(chat, "任务已恢复")
        else:
            try:
                valid_url(text)
            except (Hold, ValueError, OSError):
                self.notify(chat, "请发送支持站点的单个 HTTPS 视频链接：YouTube、B站、抖音、TikTok、Vimeo、X、Instagram。")
                return
            active = db.db.execute("SELECT count(*) FROM jobs WHERE state!='done'").fetchone()[0]
            if active >= 10 or not self.disk_ok():
                self.notify(chat, "队列或临时空间已满，请稍后再试")
                return
            cur = db.db.execute("INSERT OR IGNORE INTO jobs(update_id,chat,url,state) VALUES(?,?,?,'choosing')",
                                (update["update_id"], chat, text))
            db.db.commit()
            if cur.rowcount:
                buttons = [{"text": label, "callback_data": f"quality:{cur.lastrowid}:{height}"}
                           for height, label in QUALITIES.items()]
                try:
                    self.tg("sendMessage", chat_id=chat,
                            text=f"任务 {cur.lastrowid} · 请选择清晰度\n源视频不足时选择较低可用画质，原画或 4K 可能需要分段回传。",
                            reply_markup={"inline_keyboard": [buttons[:2], buttons[2:]]})
                except Hold:
                    db.update(cur.lastrowid, state="held", error="清晰度选择消息发送失败；请重新发送链接")

    def main(self):
        db = Store(self.state / "jobs.sqlite")
        row = db.db.execute("SELECT value FROM settings WHERE key='namespace'").fetchone()
        if row:
            self.namespace = row[0]
        else:
            import uuid
            self.namespace = uuid.uuid4().hex[:12]
            db.db.execute("INSERT INTO settings VALUES('namespace',?)", (self.namespace,))
            db.db.commit()
        self.tg("getMe")
        self.setup_menu()
        if self.tg("getWebhookInfo").get("url"):
            raise Hold("Bot 已有 webhook，请为本服务使用专用 Bot 或先迁移 webhook")
        thread = threading.Thread(target=self.worker, daemon=True)
        thread.start()
        while not STOP.is_set():
            try:
                row = db.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
                updates = self.tg("getUpdates", offset=int(row[0]) if row else 0, timeout=30,
                                  allowed_updates=["message", "callback_query"])
                for update in updates:
                    self.handle(db, update)
                    db.db.execute("INSERT OR REPLACE INTO settings VALUES('offset',?)", (str(update["update_id"] + 1),))
                    db.db.commit()
            except Exception as e:
                LOG.warning("poll failed: %s", type(e).__name__)
                STOP.wait(5)
        thread.join(timeout=20)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    Bot().main()
