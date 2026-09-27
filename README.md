# 出图助手

用手机远程驱动电脑上的 ComfyUI 出图。支持两种模式:

- **文生图**:发一段提示词,出一张图
- **改图**:发 1~6 张参考图 + 提示词,按提示词修改

---

## 一、启动

1. 先确认 ComfyUI 开着(`run_nvidia_gpu.bat`)
2. 双击本目录下的 `run.bat`
3. 窗口里会打印两行关键信息:
   - `手机端地址(同一 WiFi): http://192.168.x.x:8899`
   - `访问口令: xxxxxxxx`

> 第一次启动会自动生成口令,写在配置文件 `config.json` 的 `access_token` 里,随时可以自己改。

## 二、手机连接

1. 手机连**和电脑同一个 WiFi**
2. 浏览器打开窗口里显示的那个地址
3. 输入访问口令进入

**加到桌面**:浏览器菜单里选"添加到主屏幕",之后就像 App 一样打开。

## 三、出门也能用(推荐 Tailscale)

电脑和手机都装上 Tailscale,登录同一个账号:

- 电脑:https://tailscale.com/download/windows
- 手机:应用商店搜 Tailscale

装好后手机会拿到一个 `100.x.x.x` 的地址,把上面的 `192.168.x.x` 换成它,就能在任何网络下访问,不需要开路由器端口。

> 备选:Cloudflare Tunnel(需要域名)、frp(需要服务器)。
> **不要**用路由器端口转发直接暴露 8899。

## 四、说明

- 同一时刻只跑一个任务,后面的会排队 —— 这是为了保护显存,不是故障。
- 任务完成时手机会收到浏览器通知(需要允许通知权限)。
- 电脑不能休眠,否则任务会中断。建议把电源计划设为"从不睡眠"。
- 生成过的图存在 ComfyUI 的 `output` 目录里,手机里也能翻看历史记录。

## 五、目录说明

| 路径 | 用途 |
|---|---|
| `server.py` | 中转服务主程序 |
| `config.json` | 端口、口令、模型名、默认参数 |
| `static/` | 手机网页界面 |
| `templates/` | 已验证的 ComfyUI 工作流模板(参考用) |
| `work/` | 运行记录与历史数据 |

## 六、改默认值

编辑 `config.json`:

- `port`:换端口
- `access_token`:换口令(改完重启生效)
- `defaults.steps`:默认步数
- `models`:换模型文件时改这里

---

## 七、关于 HTTPS 和通知(重要)

用 `http://192.168.1.5:8899` 这种地址访问时,浏览器会把它当成"不安全来源"。后果:

- **浏览器通知用不了**(需要 HTTPS)
- "添加到主屏幕"只能生成一个普通快捷方式,不是完整的 App 体验

两种解决办法:

### 方案 A:Tailscale Serve(推荐)

装完 Tailscale 后,在电脑上执行:

```
tailscale serve --bg 8899
```

它会给你一个 `https://电脑名.xxx.ts.net` 的地址,自带合法证书。手机用这个地址访问,通知和"添加到主屏幕"就都能用了。

### 方案 B:Cloudflare Tunnel

需要一个域名,好处是不用装客户端,任何浏览器都能开。

> 顺带一提:如果只是在家里 WiFi 用、也不在乎通知,直接用 IP 访问完全没问题,功能一样齐全。

---

## 八、推理加速(已内置)

出图链路里接了你自己工作流用的那两个加速节点:

```
UnetLoaderGGUF → TESpeedQwenImage21 → QwenImage21Cache → KSampler
                      加速跳步            模型缓存
```

参数与你工作流一致:`kitchen_int8` / `te_predictor` / 阈值 0.06 / 误差上限 0.08。

**实测(30 步、1024×1024、同一种子)**

| | 用时 | 每步 |
|---|---|---|
| 开加速 | 60.3 秒 | 2.01 秒 |
| 关加速 | 70.3 秒 | 2.34 秒 |

约快 **14%**,两张图肉眼无差别。注意:**步数少的任务几乎没收益**(它靠跳过部分步来省时间,8 步时没有多少可跳的),30 步以上才明显。

想关掉:把 `config.json` 里 `acceleration.enabled` 改成 `false`,重启生效。

## 九、提示词扩写(PE)

两个页签的提示词上方都有 `✨ AI 扩写` 按钮,用的是官方 Qwen 的 PE 模型(你 `models/text_encoders` 里那两个 8.8G 的 `pe_t2i` / `pe_i2i`)。

用法:
1. 写一句简单的话,比如「一个女孩站在雨中的霓虹街道上」
2. 点 `✨ AI 扩写`,等约 20 秒
3. 弹出的框里是扩写结果,**可以直接改**,满意就点「替换提示词」

几点注意:
- **改图页签要先选参考图**再点扩写,因为图生图用的 PE 模型需要看图才能判断你要改什么(文生图页签没有这个限制)
- 每次扩写都要重新加载 PE 模型,所以会比出图本身还慢一点;跑完会自动卸载释放显存
- 扩写结果大约 300~700 字,信息量比一句话大得多,画质提升比较明显

想关掉:把 `config.json` 里 `prompt_enhancer.enabled` 改成 `false`。

### 9.1 扩写引擎与模型

`config.json` 里 `prompt_enhancer.engine` 有三个选项:

| 取值 | 含义 | 实测速度 |
|---|---|---|
| `本地` | 用本地 GGUF 大模型 | 约 11 秒 |
| `本地官方PE` | 用官方 PE 模型(自带对齐,会软化部分措辞) | 约 20~24 秒 |
| `API` | 走云端接口,需填 `api_key` | 取决于网络 |

**当前设置:`本地` + 解限版模型**

- 主模型 `Q35-4B-U-HauhauCS\Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf`(3.2G)
- mmproj `Q35-4B-U-HauhauCS\mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf`(读参考图用)

同目录下还有两个量化版本可以换着用,改 `main_model` 即可:

- `...Aggressive-Q4_K_M.gguf`(2.5G,最快)
- `...Aggressive-Q8_0.gguf`(4.2G,质量最好)

想切回官方 PE:把 `engine` 改回 `本地官方PE` 就行,PE 模型字段一直留着,不用重新填。

---

## 十、历史记录:按设备隔离

`config.json` 里的 `history_scope` 控制记录可见范围:

| 取值 | 效果 |
|---|---|
| `device`(当前) | 每台设备只看得到自己出的图,互相看不到 |
| `shared` | 所有设备共用一份记录 |

判断依据是登录时下发的一个设备标识(浏览器 Cookie),持续一年。

两点说明:
- 改这个值需要重启服务
- 切换之前就存在的旧记录没有设备标识,`device` 模式下所有人仍然看得到

## 十一、输出目录自动清理

工具生成的图带 `phone_t2i_` / `phone_edit_` 前缀,`config.json` 里控制保留策略:

```json
"output_cleanup": { "enabled": true, "keep": 300, "max_age_days": 30 }
```

- `keep`:最多保留最近多少张
- `max_age_days`:超过多少天的直接删(设 0 表示不按时间清)
- 两个条件是「或」的关系,任意一条不满足就删

触发时机:每次启动时,以及每完成一个任务后。

**只删这两个前缀的文件**,你自己手动出的图、别的地方存的图一律不碰。被删掉的图对应的记录会保留,只是缩略图不显示。

## 十二、修改访问口令

三种方式,任选:

1. **主机设置页**(推荐):服务启动日志里会打印 `http://127.0.0.1:8899/admin`,在那台电脑的浏览器里打开即可查看和修改口令。**这个页面只接受本机访问**,从手机或局域网打开会返回 403。

2. **命令行**:在项目目录执行

   ```
   "D:\qwen mou\ComfyUI_windows_portable\python_embeded\python.exe" server.py --set-token 你的新口令
   "D:\qwen mou\ComfyUI_windows_portable\python_embeded\python.exe" server.py --show-token
   ```

3. **直接改配置**:编辑 `config.json` 里的 `access_token`,然后重启。

改完口令后,所有已登录的设备都需要重新输入新口令。

---

## 十三、清空记录

「记录」页签右上角有 **清空记录** 按钮,点开后会问你要不要连磁盘上的图片一起删:

- **只清记录**:列表清空,图片留在磁盘(ComfyUI 的输出目录)
- **连图片一起删**:记录和对应的图片文件一起清掉,这一步不可恢复

两点行为说明:

- `history_scope = device` 时,**只会清掉你自己设备的记录**,别人设备的图和记录不受影响
- 删除图片时**只删 `phone_t2i_` / `phone_edit_` 前缀的文件**,你自己手动出的图不会被误删

另外,工具生成的旧图还会按第十一节里的保留策略自动清理(默认留最近 300 张、最多 30 天)。

---

## 十四、完成通知

**为什么做在服务端**:纯 HTTP 访问时浏览器自带的通知是被禁用的(浏览器的安全限制),而且页面一关就收不到。所以通知由电脑主动推送到你手机上,锁屏、关页面都能收到。

### 支持的渠道

在 `config.json` 的 `notify` 段里选一个 `channel`,填好地址或密钥即可:

| channel | 要填什么 | 说明 |
|---|---|---|
| `wecom` | `webhook` | 企业微信群机器人地址 |
| `feishu` | `webhook` | 飞书群自定义机器人地址 |
| `dingtalk` | `webhook` | 钉钉机器人地址(用「自定义关键词」模式,关键词随意) |
| `serverchan` | `token` | Server酱 SendKey,推送到微信 |
| `pushplus` | `token` | PushPlus 的 token,推送到微信 |
| `bark` | `token` | Bark 的 key(iOS);`webhook` 可换成自建服务器 |
| `ntfy` | `token` | ntfy 的 topic 名(安卓);`webhook` 可换成自建服务器 |
| `custom` | `webhook` | 自定义地址,会 POST 一份 JSON:`{title, body, job}` |

### 什么时候会推

- **出图/改图成功** → 推送类型、步数、耗时和提示词摘要
- **生成失败** → 推送失败原因,省得你回来看是白等还是报错
- **中途取消** → 不推送(你自己取消的,不打扰)
- **提示词扩写** → 不推送(属于即时交互)

想只留一种,把 `on_success` / `on_failure` 改成 `false` 即可。

### 怎么验证

「记录」页签里有 **测试通知** 按钮,点一下就会往你配置的渠道推一条测试消息。命令行方式:

```
curl -X POST http://127.0.0.1:8899/api/notify/test
```

### 关闭通知

把 `notify.enabled` 改成 `false`。

### 关于 Server酱的 key 格式(实测)

Server酱的新老版本 key 不同,推送地址也不一样,程序会自动识别:

| key 开头 | 实际使用的地址 |
|---|---|
| `SCT...`(老版 Turbo) | `https://sctapi.ftqq.com/<key>.send` |
| `sctp...`(新版 Server酱³) | `https://<编号>.push.ft07.com/send/<key>.send` |

所以只管把 key 填进 `notify.token` 就行,不用管地址。如果以后换了别的推送服务,把完整地址填进 `notify.webhook` 会优先使用它。

当前配置(已实测收到过消息):

```json
"notify": {
  "enabled": true,
  "channel": "serverchan",
  "token": "你的 SendKey",
  "on_success": true,
  "on_failure": true,
  "include_prompt": true
}
```

---

## 十五、手机随时查主机地址(Gist 地址簿)

**要解决的问题**:家里的公网 IPv6 地址可能随运营商重新分配而变化,人在外面时需要有个地方能查到"现在该连哪个地址"。

**做法**:主机每 15 分钟把当前地址写进一个私密 Gist,手机收藏那个页面即可。

创建时会自动生成 Gist(私密,不公开列出),并把 id 写回 `config.json`,之后只做更新。

### 两个收藏链接

| 用途 | 链接 |
|---|---|
| 手机浏览器打开(排版正常,推荐) | `https://gist.github.com/<用户名>/<gist_id>` |
| 备用(网页版打不开时) | `https://api.github.com/gists/<gist_id>` |

主机启动日志里每次都会打印这两条。

### 网络实测结论(2026-09-28,本机)

| 域名 | 电脑 | 手机流量 |
|---|---|---|
| `api.github.com` | 稳定 0.3s | 可用 |
| `gist.github.com` | 3/3 超时 | **可打开** |
| `gist.githubusercontent.com` | 超时 | 未测 |
| `raw.githubusercontent.com` | 超时 | 未测 |
| `cdn.jsdelivr.net`(gist) | 不支持 | — |
| `cdn.statically.io` | 超时 | — |

所以"纯文本/raw"这类更干净的读法在本地网络不可用,网页版是能用的最优解。

### 配置

```json
"address_book": {
  "enabled": true,
  "provider": "gist",
  "token": "GitHub PAT,只勾 gist 权限",
  "item_id": "创建后自动写回",
  "filename": "qwen-remote-address.txt",
  "interval_sec": 900
}
```

- `provider` 可选 `gist` / `gitee`(Gitee 版本已实现,但当前网络下 GitHub 网页版可用,故未启用)
- `token` 只勾 `gist` 权限即可,存储在 `config.json`(已加入 `.gitignore`)
- Gist 是**私密**的:不公开列出,只有拿到含随机 id 的链接才能访问
