# 用浏览器定位回放

以已安装的 agent-browser-cli 为例，保持当前登录态：

```powershell
agent-browser-cli tabs
agent-browser-cli exec --tab TAB_ID 'return {url:location.href,videos:Array.from(document.querySelectorAll("video")).map(v=>({src:v.currentSrc,duration:v.duration,time:v.currentTime,paused:v.paused,muted:v.muted}))}'
agent-browser-cli network start --tab TAB_ID
```

保存原播放状态，轻量播放或跳转触发网络请求：

```powershell
agent-browser-cli network list --tab TAB_ID --filter m3u8
agent-browser-cli network list --tab TAB_ID --filter .ts
```

清单可能只在首次加载时出现，需要在监听后刷新；刷新必须与页面当前操作兼容。`performance.getEntriesByType('resource')` 的缓冲区有限，找不到不代表没有请求。

用实际 `.ts` 请求验证主机和场次；video.duration、清单 EXTINF 总和和成品时长应相符。用 `network detail REQUEST_ID` 时只输出必要字段，大响应体留在本地。

## React 属性作为补充线索

播放器 DOM 和媒体请求没有清单时，把下面的只读 JS 保存成临时文件，再用 `agent-browser-cli exec --tab TAB_ID --file FILE`。只沿视频父组件检查 URL，不遍历账号状态或整棵应用树。

```javascript
const video = document.querySelector('video');
if (!video) return [];
let node = video;
while (node && !Object.keys(node).some(k => k.startsWith('__reactFiber$'))) {
  node = node.parentElement;
}
let fiber = node && node[Object.keys(node).find(k => k.startsWith('__reactFiber$'))];
const found = new Set();
for (let i = 0; fiber && i < 25; i++, fiber = fiber.return) {
  const props = fiber.memoizedProps || {};
  for (const key of ['url', 'sourceUrl', 'src']) {
    const value = props[key];
    if (typeof value === 'string' && /^https?:/.test(value) && /m3u8|\.mp4(?:\?|$)/i.test(value)) {
      found.add(value);
    }
  }
}
return [...found];
```

签名链接只保存在该任务工作目录。不要把具体房间、账号、Cookie、页面 URL 或整份抓包发布到 skill 仓库。

## 结束监听

```powershell
agent-browser-cli network stop --tab TAB_ID
```

恢复本次改变的暂停、静音和播放位置，不停掉用户已有的其他监听或下载。
