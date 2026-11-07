# WeMaa

**把已登录的 Windows 微信桌面接入 MaiBot：一套核心、一套管理页面、一条可追踪的收发链。**

WeMaa 是个人开发并公开的集成项目，基于 MaiBot 与 MaaFramework，提供微信桌面适配器、联系人身份管理、原生 HTTP API、媒体交付、定时任务和统一 WebUI。可选的网站插件通过独立接口连接你自己的业务系统，默认关闭。

项目不包含微信账号、联系人、聊天记录、运行数据库、模型密钥、公司业务数据或原部署配置。代码以一次干净快照发布，未公开原私有仓库的提交历史。

## 可以做什么

- 使用 MaaFramework 操作官方 Windows 微信客户端，采集消息、核验联系人和发送文字/媒体。
- 保留 MaiBot 的对话、记忆、表达、插件与模型配置能力，并在统一 Dashboard 中管理微信运行台。
- 通过原生 API 提交请求、查看状态和接入自己的服务。
- 同一会话按顺序处理；发送前检查目标、草稿与画面证据，结果不确定时停止自动重发。

## 组件

| 目录 | 内容 |
| --- | --- |
| `apps/core` | MaiBot 核心及 WeMaa 平台适配 |
| `apps/webui` | 统一管理页面 |
| `apps/adapter/maa_wechat` | Windows Maa 微信适配器 |
| `wemaa` | 身份、API、媒体、调度及运行时 |
| `plugins/website-agent` | 可选的网站服务插件 |
| `deploy` | Docker 构建与本地启动配置 |

## Linux 核心

需要 Docker Compose，以及另行准备的 Windows 已登录微信桌面。Linux 容器负责核心与管理页面，不能代替 Windows GUI Worker。

```sh
git clone https://github.com/Muchuaner/WeMaa-OpenSource.git
cd WeMaa-OpenSource
cp .env.example .env
# 编辑 .env：设置随机数据库密码、独立机器令牌和 API 令牌
mkdir -p private/config private/data private/logs private/plugins private/depends-data
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

管理页面默认仅监听本机 `http://127.0.0.1:18003/`。首次启动按核心日志生成并配置 `private/config` 内的机器人与模型配置，使用你自己的模型渠道；未配置模型不能正常对话。对象存储需自行部署 S3/MinIO 并创建 `wemaa-maa` 桶，另行设置访问凭据。自动回复、创作发送及网站插件默认关闭，确认配置与接收目标后再启用。

## Windows 适配器

将 `apps/adapter/maa_wechat` 复制到 Windows 工作目录，在 Python 3.12 或更新版本的虚拟环境中安装其 `requirements.txt`。需登录实际桌面并保持微信可见；当前识别基线为 1920×1080 桌面、96 DPI、1920×1040 微信客户区，其他布局需自行适配。

受核心管理时，在适配器工作目录创建 `private/admin-agent.json`，用自己的 HTTPS 域名及与核心一致的机器令牌：

```json
{
  "endpoint": "https://chat.example.org/api/v1/wechat-maa",
  "trusted_host": "chat.example.org",
  "token": "REPLACE_WITH_A_RANDOM_TOKEN_AT_LEAST_32_CHARACTERS"
}
```

请通过你自己的反向代理提供 HTTPS，不要关闭证书校验。先运行 `python runner.py --help` 查看模式，完成联系人、目标身份与草稿检查后再启用持续自动回复。Worker 只能有一个实例；不要通过重放历史未知状态来补发消息。历史视觉/会话夹具不随源码发布；相关测试在未提供自有夹具时跳过，Windows 实机验收需在自己的账号环境执行。

## 开发检查

```sh
python -m pip install -e . pytest
python -m pytest tests -q
cd apps/adapter/maa_wechat
python -m unittest discover -s . -p 'test_endpoint_config.py'
cd ../../webui
npm ci --ignore-scripts
npm run build
```

本次公开快照已通过 250 项核心测试（另 1 项私有夹具测试跳过）、2 项 HTTPS 地址校验测试、28 项引导组件测试及 WebUI 构建。更新了公开版的前端依赖锁定与安全补丁；生产依赖审计仍有 6 项低危提示，部署前请重新审计。

完整 Windows 视觉测试依赖操作系统、Maa、官方微信和对应画面。纯逻辑单元测试不代表真实收发或长期无人值守验收。

## 来源和许可

MaiBot 核心基线：1.2.5，提交 `b8c39400dcc9940791346503af24f2060649402d`；Dashboard 基线：1.7.4。源码来源与原始文件摘要见 `docs/source-baseline.json`，第三方声明见 `NOTICE` 和各组件许可文件。

本仓库按 **GPL-3.0** 发布并保留第三方原有版权与许可。WeMaa 不是微信或腾讯的官方项目，也不承诺规避平台限制。请只在你有权使用的账号和会话中配置自动化。
