"""Run inside video-bot via stdin to migrate completed legacy archives safely."""
import json
import os
import sqlite3
from worker import Bot, archive_video_name

b = object.__new__(Bot)
b.rconfig = os.environ['RCLONE_CONFIG']
c = sqlite3.connect('/state/jobs.sqlite')
c.row_factory = sqlite3.Row
for raw in c.execute("SELECT * FROM jobs WHERE state='done'").fetchall():
    row = dict(raw)
    manifest, meta = json.loads(row['manifest']), json.loads(row['meta'])
    target = os.environ['DRIVE_TARGET'].rstrip('/') + '/' + archive_video_name(meta.get('author') or '未知作者', '')
    if row['remote'] == target:
        continue
    b.verify(row['remote'], manifest)
    new, mapping = {}, {}
    for name, entry in manifest.items():
        local = entry.get('local_name', name)
        dest = archive_video_name(meta.get('title'), '.metadata.json') if local == 'metadata.json' else name
        new[dest] = {**entry, 'local_name': local}
        mapping[name] = dest
    for name, dest in mapping.items():
        b.rc('copyto', row['remote'] + '/' + name, target + '/' + dest, '--immutable', '--checksum')
    b.verify(target, new)
    c.execute('UPDATE jobs SET remote=?,manifest=? WHERE id=?',
              (target, json.dumps(new, ensure_ascii=False), row['id']))
    c.commit()
    # Delete only the verified originals, after the durable record points to the new archive.
    for name in manifest:
        b.rc('deletefile', row['remote'] + '/' + name)
    b.rc('rmdir', row['remote'])
    print(json.dumps({'task': row['id'], 'author_folder': target, 'verified': True}, ensure_ascii=False))
