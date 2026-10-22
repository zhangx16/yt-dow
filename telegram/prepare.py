"""Verify Drive write/hash capability, then migrate this dedicated bot to local API."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import uuid


def rc(*args):
    p = subprocess.run(["rclone", "--config", os.environ["RCLONE_CONFIG"], *args], capture_output=True)
    if p.returncode:
        raise SystemExit("Drive 访问或校验失败；未启动 Bot，未清理任何视频")
    return p.stdout


def cloud(method):
    req = urllib.request.Request("https://api.telegram.org/bot" + os.environ["TG_BOT_TOKEN"] + "/" + method,
                                 data=b"{}", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
    except Exception:
        raise SystemExit("Telegram 云端验证/迁移失败（不输出含 Token 的请求地址）") from None
    if not result.get("ok"):
        raise SystemExit("Telegram 云端验证/迁移未成功")
    return result["result"]


if __name__ == "__main__":
    info = json.loads(rc("about", "gdrive:", "--json"))
    print("Drive remaining bytes:", info.get("free", "unknown"))
    target = os.environ["DRIVE_TARGET"] + "/.probe-" + uuid.uuid4().hex
    payload = b"yt-dow archive verification\n"
    with tempfile.TemporaryDirectory() as temp:
        p = Path(temp) / "probe.txt"
        p.write_bytes(payload)
        rc("copyto", str(p), target, "--immutable", "--checksum")
        listing = json.loads(rc("lsjson", target, "--stat", "--hash", "--hash-type", "MD5"))
        md5 = {k.lower(): v for k, v in listing.get("Hashes", {}).items()}.get("md5", "")
        if listing.get("Size") != len(payload) or md5 != hashlib.md5(payload).hexdigest():
            raise SystemExit("Drive 写入校验失败；保留测试文件供检查")
        rc("deletefile", target)
    print("Drive upload and MD5 verification passed")
    marker = Path("/state/cloud-logout.done")
    if not marker.exists():
        me = cloud("getMe")
        print("Telegram bot:", me.get("username"))
        if cloud("getWebhookInfo").get("url"):
            raise SystemExit("Bot 已有 webhook，请先迁移现有消费者，停止启动")
        cloud("logOut")
        marker.write_text(str(me["id"]))
        marker.chmod(0o600)
        print("Cloud logout completed; local API can now take over")
