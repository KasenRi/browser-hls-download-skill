# 浏览器 HLS 回放下载 skill

用已登录浏览器找到真实媒体清单，aria2 分段断点续传，FFmpeg 无损合并，校验后才生成正式 MP4。适合 blob 播放地址、直播后台回放，以及 IDM/Motrix 只下载到清单的场景。

附带脚本处理有限、未加密的 MPEG-TS 媒体清单；不处理 DRM、持续直播、fMP4、字节范围或外置音轨主清单。

## 在另一台电脑安装

整个仓库根目录就是一个 skill：

```powershell
git clone https://github.com/KasenRi/browser-hls-download-skill.git "$env:USERPROFILE/.codex/skills/browser-hls-download"
cd "$env:USERPROFILE/.codex/skills/browser-hls-download"
```

macOS/Linux：

```bash
git clone https://github.com/KasenRi/browser-hls-download-skill.git ~/.codex/skills/browser-hls-download
cd ~/.codex/skills/browser-hls-download
```

安装 Python 3.10+、aria2、FFmpeg（含 FFprobe）。抓取链接时，在新机器登录目标网站并连接浏览器 bridge；可使用已有 agent-browser-cli 技能或其他支持登录态和网络请求的工具。下载脚本本身不依赖浏览器工具。

在 Codex 中输入：

> 用 $browser-hls-download 下载这个页面里时长为 03:04:39 的回放，保存到下载目录，完成合并和校验后给我文件。

## 已有媒体清单时直接运行

```powershell
python scripts/hls_download.py --url "https://media.example/replay.m3u8" --output "D:/Downloads/replay.mp4"
python scripts/hls_download.py --help
```

工具不在 PATH 时传 `--aria2`、`--ffmpeg`、`--ffprobe` 的真实路径。

`replay.hls-work/status.json` 显示阶段。普通 MP4 在写完前可能缺少索引，运行器先写 `replay.partial.mp4`，通过时长、视频轨道和首中尾解码检查后才改成 `replay.mp4`。日志和源链接保存在工作目录，不要提交到 GitHub。

## 带着进度换机器

正常停止旧进程，复制整个 `replay.hls-work`，然后在新机器运行：

```powershell
python scripts/hls_download.py --work-dir "E:/Transfer/replay.hls-work" --output "E:/Videos/replay.mp4"
```

完整分段复用，未完成分段续传。保持终端/工具进程会话运行；状态仍显示 downloading 但实际进程消失时，需要重新执行命令。

详见 [SKILL.md](SKILL.md)、[迁移排障](references/operations.md)、[浏览器抓取](references/browser-capture.md)。

## 验证实现

本地合成媒体测试不访问真实账号、站点或直播素材：

```powershell
python -m unittest discover -s tests -v
```
