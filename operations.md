# 运行、迁移和排障

## 工具

Python 3.10+ 仅用标准库；另需 aria2c、FFmpeg、FFprobe。脚本不安装软件、不读取 Motrix 的 RPC 密钥、不接管其队列或修改全局设置。

```powershell
Get-Command python, aria2c, ffmpeg, ffprobe -ErrorAction SilentlyContinue
python scripts/hls_download.py --help
```

工具不在 PATH 时传 `--aria2 "实际路径/aria2c.exe"` 等参数。Motrix 自带引擎也可用。TLS 使用系统信任根；必要时指定可信 `--ca-certificate`，不能关闭证书验证。

## 带进度迁移

正常停止旧进程，复制整个工作目录（包括分段和 `.aria2` 控制文件）到新机器，无需复制虚拟环境或证书缓存。

```powershell
python scripts/hls_download.py --work-dir "E:/Transfer/replay.hls-work" --output "E:/Videos/replay.mp4"
Get-Content -Raw -LiteralPath "E:/Transfer/replay.hls-work/status.json"
```

不传 URL 时读取保存的任务元数据。OS 文件锁阻止同目录两个运行器，进程退出自动释放。不要让两台机器同时写同一目录。完整分段复用，未完成分段续传；合并中断可以重建临时 MP4，无需重下分段。

脚本严格比较分段 URL 和时长，拒绝把新视频混入旧目录。签名变化或链接过期时，在浏览器重新确认来源；保留旧目录，不随意篡改元数据绕过身份检查。

## 实际阶段

| stage | 含义 |
| --- | --- |
| downloading | completed_segments/total_segments 为完整分段数 |
| merging | 分段到齐，merged_seconds/expected_seconds 为合并进度 |
| verifying | 检查真实时长与首中尾解码 |
| complete | 正式 MP4 已生成，validation.json 保存结果 |
| paused/error | 本次进程停止，保留目录以便恢复或排障 |

报告状态前同时核对 updated_at、PID、实际进程和文件更新时间。强制终止可能来不及写 paused/error；旧的 downloading 字段不是运行证明。长任务使用可持续的终端/工具进程会话。无会话隐藏进程曾在本次实践中提前消失，不额外创建系统计划任务或自动化掩盖进程管理问题。

## 已验证的陷阱

- **moov atom not found：** 普通 MP4 尚未写完索引时会报错。只交付校验后正式文件，改扩展名、更换播放器不能补齐索引。
- **HEVC 兼容性：** 无损合并保留原编码。先用支持 HEVC 的播放器区分兼容问题，再决定是否 H.264 转码，不默认重编码三小时素材。
- **只下到几百 KB：** 文件头是 EXTM3U 就是清单。IDM 普通 URL 接口、Motrix 直链模式不能据此宣称视频下载成功；IDM 浏览器视频接管需要实测。
- **401/403：** 检查 URL 有效期、是否确需 Referer/登录。CDN 权限与页面登录分开判断，不关闭验证或伪造授权。
- **有连接但很慢：** 看 download.log，按链路降低 `--jobs`，或配置合适的 `--min-speed` 让 aria2 重试。本机 1 MB/s 阈值不能成为其他机器的网速要求，默认不设速度门槛。
- **合并慢：** 看 merge-progress.txt。机械盘同时读写大文件可能受磁盘吞吐限制；`--faststart` 还会搬移媒体数据。下载 ETA 与合并 ETA 分开。
- **脚本拒绝其他 HLS：** 主清单、外置音轨、加密、fMP4、字节范围或缺失片段使用真正支持其格式的成熟工具，不减少原交付要求。

实现校验分段库存、MPEG-TS 结构、时长和抽样解码，不宣称逐帧解码整段。无额外完整性或签名需求时，不计算整份视频的密码学哈希。
