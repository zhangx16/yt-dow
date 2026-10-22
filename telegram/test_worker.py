import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bot = object.__new__(worker.Bot)
        self.bot.root = root / "work"
        self.bot.root.mkdir()
        self.bot.state = root
        self.bot.namespace = "test"
        self.bot.remote = "gdrive:Media/Test"
        self.bot.limit = 100
        self.bot.allowed = {12}
        self.bot.notify = lambda *_: None
        self.bot.tg = lambda *_args, **_kwargs: {"message_id": 10}
        self.bot.disk_ok = lambda: True
        self.db = worker.Store(root / "jobs.sqlite")
        self.db.db.execute("INSERT INTO jobs(update_id,chat,url) VALUES(1,12,'https://youtu.be/test')")
        self.db.db.commit()
        self.folder = self.bot.workpath(1)
        self.folder.mkdir()
        self.file = self.folder / "video.mp4"
        self.file.write_bytes(b"video")
        (self.folder / "metadata.json").write_text('{}')
        self.db.update(1, state="downloaded", file=str(self.file),
                       meta=json.dumps({"title": "Title", "author": "Author"}))

    def test_cloud_mismatch_never_advances_or_deletes(self):
        self.bot.rc = lambda *args: b'[{"Name":"video.mp4","Size":5,"Hashes":{"MD5":"wrong"}}]' if args[0] == 'lsjson' else b''
        with self.assertRaises(worker.Hold):
            self.bot.archive(self.db, self.db.get(1))
        self.assertEqual(self.db.get(1)["state"], "downloaded")
        self.assertTrue(self.file.exists())

    def test_archive_verifies_upper_and_lower_case_hash_keys(self):
        import hashlib
        def rc(*args):
            if args[0] != "lsjson":
                return b""
            return json.dumps([{"Name": "Title.mp4" if p == self.file else "Title.metadata.json", "Size": p.stat().st_size,
                "Hashes": {"MD5" if p.name == "video.mp4" else "md5": hashlib.md5(p.read_bytes()).hexdigest()}}
                for p in (self.file, self.folder / 'metadata.json')]).encode()
        self.bot.rc = rc
        self.bot.archive(self.db, self.db.get(1))
        self.assertEqual(self.db.get(1)["state"], "archived")
        self.assertEqual(self.db.get(1)["remote"], "gdrive:Media/Test/Author")
        self.assertIn("Title.metadata.json", json.loads(self.db.get(1)["manifest"]))
        self.assertTrue(self.file.exists())

    def test_archive_title_sanitizing_preserves_chinese(self):
        self.assertEqual(worker.archive_video_name('视频标题/作者:测试?', '.mp4'), '视频标题_作者_测试_.mp4')
        self.assertLessEqual(len(worker.archive_video_name('长标题' * 100, '.mp4').encode()), 204)
        self.assertEqual(worker.archive_video_name('..', '.mp4'), '未命名视频.mp4')

    def test_author_folder_keeps_existing_same_title_video(self):
        import hashlib
        remote = {'Title.mp4': b'existing different video'}
        targets = []
        def rc(*args):
            if args[0] == 'mkdir':
                targets.append(args[1])
            if args[0] == 'copyto':
                name = args[2].rsplit('/', 1)[1]
                self.assertNotIn(name, remote)
                remote[name] = Path(args[1]).read_bytes()
            if args[0] == 'lsjson':
                return json.dumps([{'Name': name, 'Size': len(data),
                    'Hashes': {'md5': hashlib.md5(data).hexdigest()}} for name, data in remote.items()]).encode()
            return b''
        self.bot.rc = rc
        self.bot.archive(self.db, self.db.get(1))
        self.assertEqual(targets, ['gdrive:Media/Test/Author'])
        self.assertEqual(remote['Title.mp4'], b'existing different video')
        self.assertIn('Title [test-1].mp4', remote)
        self.assertIn('Title [test-1].metadata.json', remote)

    def test_quality_selection_is_private_persistent_and_only_applied_once(self):
        self.db.update(1, state='choosing')
        query = {'id': 'callback', 'from': {'id': 99}, 'data': 'quality:1:720',
                 'message': {'message_id': 10, 'chat': {'id': 12, 'type': 'private'}}}
        self.bot.handle(self.db, {'callback_query': query})
        self.assertEqual(self.db.get(1)['state'], 'choosing')
        query['from']['id'] = 12
        self.bot.handle(self.db, {'callback_query': query})
        self.assertEqual(self.db.get(1)['state'], 'queued')
        self.assertEqual(self.db.get(1)['quality'], 720)
        query['data'] = 'quality:1:2160'
        self.bot.handle(self.db, {'callback_query': query})
        self.assertEqual(self.db.get(1)['quality'], 720)

    def test_send_failure_is_ambiguous_and_retains_video(self):
        self.db.update(1, state="archived", parts=json.dumps([str(self.file)]))
        self.bot.run = lambda *_args, **_kwargs: b'{"streams":[{"codec_name":"h264"}]}'
        def fail(*_args, **_kwargs):
            raise worker.Hold("timeout")
        self.bot.tg = fail
        with self.assertRaises(worker.Hold):
            self.bot.deliver(self.db, self.db.get(1))
        self.assertEqual(self.db.get(1)["state"], "sending")
        self.assertTrue(self.file.exists())

    def test_partial_send_resumes_without_repeating_confirmed_part(self):
        self.db.update(1, state="archived", parts=json.dumps([str(self.file), str(self.file)]),
                       sent=json.dumps([{"message_id": 1, "file_id": "old"}]))
        self.bot.run = lambda *_args, **_kwargs: b'{"streams":[{"codec_name":"h264"}]}'
        calls = []
        def tg(method, **fields):
            calls.append((method, fields))
            return {"message_id": 2, "video": {"file_id": "new"}}
        self.bot.tg = tg
        self.bot.deliver(self.db, self.db.get(1))
        self.assertEqual(len(calls), 1)
        self.assertIn("作者：Author", calls[0][1]["caption"])
        self.assertEqual(self.db.get(1)["state"], "sent")
        self.assertEqual(len(json.loads(self.db.get(1)["sent"])), 2)

    def test_cleanup_requires_fresh_cloud_verification(self):
        self.db.update(1, state="sent", remote="gdrive:test", manifest='{}')
        def fail(*_):
            raise worker.Hold("remote missing")
        self.bot.verify = fail
        with self.assertRaises(worker.Hold):
            self.bot.process(self.db, self.db.get(1))
        self.assertTrue(self.file.exists())
        self.bot.verify = lambda *_: None
        self.bot.process(self.db, self.db.get(1))
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.db.get(1)["state"], "done")

    def test_duplicate_update_and_unauthorized_chat(self):
        update = {"update_id": 77, "message": {"from": {"id": 12},
                  "chat": {"id": 12, "type": "private"}, "text": "https://youtu.be/new"}}
        with patch.object(worker, "valid_url", lambda x: x):
            self.bot.handle(self.db, update)
            self.bot.handle(self.db, update)
            update["update_id"] = 78
            update["message"]["from"]["id"] = 99
            self.bot.handle(self.db, update)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)

    def test_rejects_non_video_or_internal_urls(self):
        for url in ('https://127.0.0.1/a', 'http://youtube.com/x', 'https://youtube.com.evil.test/a',
                    'https://user:pass@youtube.com/a', 'https://youtube.com:888/a'):
            with self.subTest(url=url), self.assertRaises(worker.Hold):
                worker.valid_url(url)
        with patch('socket.getaddrinfo', return_value=[(0, 0, 0, '', ('127.0.0.1', 443))]):
            with self.assertRaises(worker.Hold):
                worker.valid_url('https://youtube.com/a')

    def test_interrupted_send_is_held_on_restart(self):
        self.db.update(1, state="sending")
        worker.STOP.set()
        try:
            self.bot.worker()
        finally:
            worker.STOP.clear()
        self.assertEqual(self.db.get(1)["state"], "held")

    def test_disk_guard_terminates_running_download(self):
        self.bot.disk_ok = lambda: False
        with self.assertRaisesRegex(worker.Hold, "空间"):
            self.bot.run([sys.executable, "-c", "import time; time.sleep(20)"], guard=True)

    def test_missing_work_copy_restores_from_verified_archive(self):
        import hashlib
        payload = self.file.read_bytes()
        manifest = {"视频标题.mp4": {"size": len(payload), "md5": hashlib.md5(payload).hexdigest(),
                                    "local_name": self.file.name}}
        self.db.update(1, state="archived", remote="gdrive:test", manifest=json.dumps(manifest))
        self.file.unlink()
        self.bot.rconfig = "/unused"
        self.bot.budget = 1000
        self.bot.reserve = 0
        verified = []
        self.bot.verify = lambda *args: verified.append(args)
        def run(args, **kwargs):
            Path(args[-2]).write_bytes(payload)
            return b''
        self.bot.run = run
        self.bot.restore(self.db.get(1))
        self.assertEqual(self.file.read_bytes(), payload)
        self.assertEqual(len(verified), 1)

    def test_incompatible_codec_is_delivered_as_document(self):
        self.db.update(1, state="archived", parts=json.dumps([str(self.file)]))
        self.bot.run = lambda *_args, **_kwargs: b'{"streams":[{"codec_name":"hevc","codec_type":"video"}]}'
        calls = []
        def tg(method, **fields):
            calls.append(method)
            return {"message_id": 1, "document": {"file_id": "file"}}
        self.bot.tg = tg
        self.bot.deliver(self.db, self.db.get(1))
        self.assertEqual(calls, ["sendDocument"])
        self.assertEqual(json.loads(self.db.get(1)["sent"])[0]["file_id"], "file")

    def test_progress_edits_same_message_and_survives_edit_failure(self):
        calls = []
        self.bot.notify = lambda *_: {"message_id": 45}
        def tg(method, **fields):
            calls.append((method, fields))
            return True
        self.bot.tg = tg
        self.bot.progress(self.db, self.db.get(1), "开始下载", 0)
        self.bot.download_progress(self.db, self.db.get(1), "tgprogress: 42.5%|2MiB/s|00:30")
        self.assertEqual(self.db.get(1)["progress_message"], 45)
        self.assertEqual(calls[0][0], "editMessageText")
        self.assertIn("████░░░░░░ 42.5%", calls[0][1]["text"])
        self.assertIn("作者：Author", calls[0][1]["text"])
        def fail(*_, **__):
            raise worker.Hold("limited")
        self.bot.tg = fail
        self.bot.progress(self.db, self.db.get(1), "下载中", 50)
        self.assertTrue(self.file.exists())

    def test_progress_process_reads_all_stdout_without_corrupting_final_path(self):
        calls = []
        result = self.bot.run([sys.executable, "-c",
            "print('tgprogress: 50%|1MiB/s|00:01', flush=True); print('filepath:/work/1/video.mp4', flush=True)"],
            progress=calls.append)
        self.assertEqual(calls, ["tgprogress: 50%|1MiB/s|00:01"])
        self.assertIn(b"filepath:/work/1/video.mp4", result)

    def test_command_menu_and_list_errors(self):
        calls = []
        self.bot.tg = lambda method, **fields: calls.append((method, fields))
        self.bot.setup_menu()
        self.assertIn("list", [c["command"] for c in calls[0][1]["commands"]])
        self.assertEqual(calls[1][1]["menu_button"], {"type": "commands"})
        self.db.update(1, state="held", error="测试错误")
        messages = []
        self.bot.notify = lambda _chat, text: messages.append(text)
        for command in ("/list@mifun_storage_bot", "/errors", "/retry"):
            self.bot.handle(self.db, {"update_id": 99, "message": {"from": {"id": 12},
                "chat": {"id": 12, "type": "private"}, "text": command}})
        self.assertIn("已暂停", messages[0])
        self.assertIn("测试错误", messages[1])
        self.assertIn("/retry 1", messages[2])


if __name__ == '__main__':
    unittest.main()
