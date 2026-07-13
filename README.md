# yt-dow

一个精简的自托管视频下载服务。前端只提供单视频解析、清晰度选择、下载进度、设备下载和服务器文件删除；后端使用 Go 标准库管理任务，并调用 `yt-dlp` 与 FFmpeg 完成实际下载。

线上实例：<https://yt.996616.xyz>

## 功能

- 登录保护，默认仅监听 `127.0.0.1`
- 单视频解析与下载，不处理播放列表
- 最佳、4K、1080P、720P、480P 清晰度预设
- 下载队列、进度、速度、取消和任务清理
- 已完成文件下载到设备
- 二次确认后删除服务器文件
- 删除路径限制在 `/downloads`，拒绝目录越界和符号链接逃逸

项目有意不包含直播监控、订阅、Twitch、模板、日志查看器、归档管理、数据库或 WebSocket。空闲容器内存实测约 `8.7 MiB`。

## 架构

```text
浏览器
  ├─ /                      Nginx 静态前端
  └─ /auth /api /rpc
     /filebrowser           Go 后端 :3033
                                  └─ yt-dlp + FFmpeg
                                         └─ ./downloads
```

下载流程是服务器先保存视频，再由浏览器将服务器文件下载到设备。设备下载完成后，服务器副本会继续保留，直到在“已完成”列表中手动删除。

## 快速部署

要求：Docker、Docker Compose、Nginx。HTTPS 示例还需要 Certbot/Let's Encrypt 证书。

```bash
git clone git@github.com:zhangx16/yt-dow.git
cd yt-dow
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少修改 `WEBUI_PASSWORD`，然后启动后端：

```bash
docker compose up -d --build
docker compose ps
```

发布前端：

```bash
sudo install -d -m 755 /var/www/yt-dlp-simple
sudo install -m 644 frontend/index.html frontend/styles.css frontend/app.js frontend/favicon.svg /var/www/yt-dlp-simple/
```

安装并调整 Nginx 示例中的域名、证书路径和静态目录：

```bash
sudo install -m 644 deploy/nginx/yt.996616.xyz.conf /etc/nginx/sites-available/yt.996616.xyz
sudo ln -sfn /etc/nginx/sites-available/yt.996616.xyz /etc/nginx/sites-enabled/yt.996616.xyz
sudo nginx -t
sudo systemctl reload nginx
```

也可以在配置好 `.env` 和 Nginx 后运行：

```bash
sudo ./scripts/deploy.sh
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BIND_ADDR` | `127.0.0.1` | 后端宿主机监听地址 |
| `HOST_PORT` | `3033` | 后端宿主机端口 |
| `WEBUI_USER` | `admin` | 登录用户名 |
| `WEBUI_PASSWORD` | 无 | 登录密码，必须设置 |
| `QUEUE_SIZE` | `2` | 并发下载数 |

登录会话只保存在内存中，容器重启后需要重新登录。下载文件通过 `./downloads:/downloads` 持久化。

## 常用命令

```bash
docker compose up -d --build
docker compose logs -f
docker compose ps
docker compose down
```

更新 `yt-dlp` 版本时，修改 [Dockerfile](Dockerfile) 中的固定版本并重新构建镜像。

## 项目结构

```text
backend/          精简 Go 后端
frontend/         无构建依赖的静态前端
deploy/nginx/     Nginx 站点示例
scripts/          部署脚本
compose.yml       Docker Compose 配置
Dockerfile        多阶段运行镜像
```

## 安全说明

- 不要提交 `.env`，仓库只提供 `.env.example`。
- 建议保持 `BIND_ADDR=127.0.0.1`，通过 HTTPS 反向代理访问。
- 文件删除接口要求登录，并只允许删除下载目录内的普通文件。
- 本项目仅应用于你有权下载和保存的内容。
