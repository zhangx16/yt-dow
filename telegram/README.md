# Telegram 视频下载与 Drive 归档

私聊发送 HTTPS 视频链接，点击原画、4K、1080P、720P 或 480P 按钮后加入单任务下载队列。
选择的是清晰度上限，源视频不足时使用较低的可用清晰度。同等清晰度优先选择 H.264/AAC。
先将视频和标题作者元数据归档至
`gdrive:Media/TelegramVideos/<作者名>/`，校验大小与 MD5，再发送至 Telegram。
Drive 中视频以视频标题命名，保留扩展名；替换路径特殊字符并截短过长标题。同一作者归入同一目录，
每个视频的元数据保存为 `<视频标题>.metadata.json`。同名但内容不同的文件追加任务标识，避免覆盖。
超出 1.9 GB 的视频尝试无损分段，每段附标题、作者和原始链接。无法内嵌播放的编码以文件发送。
完成发送后重新校验云盘，再删除本任务的临时目录。不会删除 Drive 中的归档。

本模块使用独立持久化队列与 yt-dlp/FFmpeg 进程，网页项目原有任务队列不受管理。
二者和 BT 下载仍共享服务器磁盘，应同时考虑所有服务的空间占用。

## 部署

配置 `telegram/.env`（权限 600），参考 `.env.example`。不要提交或打印真实配置。
需要专用 Bot Token、数字用户 ID、Telegram API ID/Hash。凭据更换后需要重新验证迁移。

在具备 Docker 权限的服务器终端执行：

```sh
cd /root/yt-dlp-web-ui/telegram
./deploy.sh
```

脚本先检查 Docker 与 Compose 配置、构建 worker，测试 Drive 写入和 MD5（只删除本次随机测试文件），
然后首次将专用 Bot 从官方云端登出，交由本地 Bot API 接管。不会自动移除已有 webhook。
首次迁移会影响该 Token 的其他消费者，必须确保这是专用 Bot。
本地 API 由 aiogram 维护的第三方镜像运行，无公网端口映射；建议成功验证后固定镜像 digest。

现有 rclone 凭据目录挂载为可写，以允许 OAuth 刷新；不要将其分享给其他容器。
`work/` 必须保持 755，API 容器通过相同 `/work` 路径只读访问视频，避免二次上传缓存。
`state/` 保存 SQLite、实例 ID、迁移记录；此目录也需另行备份。

```sh
docker compose --env-file .env ps
docker compose --env-file .env logs --tail 50 video-bot
docker compose --env-file .env stop
```

## 使用与恢复

- 仅白名单用户私聊可用，不响应其他用户和群聊。
- 发送一个支持站点的 HTTPS 视频页面链接。
- 点击清晰度按钮才会下载，未选择的任务重启后仍保留。
- `/status` 显示最近十个任务，`/retry ID` 恢复暂停任务。
- 输入框左侧菜单提供 `/start`、`/list`、`/status`、`/errors`、`/retry`、`/help`。
- 开始下载通知会在同一条消息中更新进度条、速度与剩余时间（约四秒一次）。音视频分轨分别计数；
  后续显示备份、回传、完成或报错阶段，云盘和 TG 上传阶段不显示虚构的上传百分比。
- 自动退避重试，连续五次失败暂停；已暂停任务仍占用临时目录时，不启动后续下载。
- Telegram 上传结果不确定或进程在发送阶段重启时暂停，不自动重发；检查聊天后再 `/retry ID`，可能重复最后一段。
- 已确认发送的分段记录 message_id/file_id，重试时跳过。暂不提供跨任务自动去重。
- 归档成功且工作副本缺失时，可以从 Drive 恢复并校验后重试发送。
- 云盘故障时保留本地文件；磁盘不足时停止处理，不删除未备份视频。
- 不要手动删除单个分段或修改 SQLite。永久失败任务暂需管理员处理。

## 磁盘和格式限制

默认临时目录预算 15 GiB、整盘预留 15 GiB，执行期间每 0.5 秒检查。
这是程序保护，不是文件系统硬配额；其他进程、短时写入和容器日志仍可能额外占用空间。
下载分轨各自限制为预算的四分之一，以给合并和分段留余量。
未知大小视频或大码率分段可能触发暂停；不能保证所有视频都能无损分成指定大小。
需要时可降低 `VIDEO_HEIGHT`；修改该值不改变已下载的文件。
Google Drive 减少长期磁盘占用，但不能替代合并阶段的本机临时空间。

## 验证

```sh
python3 -m unittest -v test_worker.py
```

离线测试覆盖权限、重复更新、云盘校验失败不清理、发送中断不重发和分段恢复。
真实验收需用本人有权下载的短视频完成：私聊发链接 → 收到标题作者及视频 → 云盘存在视频和元数据
→ `/status` 显示 done → 本任务临时目录已清理。离线测试不代表真实链路已经通过。

参考：[Telegram 本地 API](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)、
[容器配置](https://github.com/aiogram/telegram-bot-api)、
[rclone 校验](https://rclone.org/commands/rclone_check/)。
