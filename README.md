# cyRouter

LLM API 网关（OpenAI/Anthropic 兼容路由，多 provider 聚合、脱敏、配额/用量跟踪）。

## 安装

- **Windows**：从 GitHub Releases（私库，需凭据）下载 `cyRouter-Setup-v*.exe`，双击安装
  （免管理员，装到 `{userappdata}\cyRouter`；卸载保留 `data\` 配置与密钥）。
- **Linux**：`curl -fsSL <releases>/cyrouter-linux-installer.sh | bash`
  （git clone 优先，私库无凭据时 `--token` 走 zip；systemd user 服务）。

## 更新（内置更新检查）

服务内置更新检查：启动后查一次 + 每 24h 周期。结果暴露在 `GET /health` 的 `latest` 字段
（`latest_version` / `update_available` / `checked_at` / `error` / `hint`），有新版时日志提示。

- **版本清单端点**：自托管 `https://pub.cyyu.me:8443/release/files/version.json`（发布链
  build.ps1 自动产出，nginx 免鉴权白名单）。可用 env `CYROUTER_UPDATE_URL` 覆盖（如内网镜像）。
- **升级方式**：Windows 下载新版 `cyRouter-Setup-v*.exe` 覆盖装（配置/数据自动保留）；
  Linux `bash cyrouter-linux-installer.sh --update`。
- 检查失败静默（离线机器不影响服务），`error` 字段透明可诊断。

## 从源码跑

```powershell
pip install -r requirements.txt
python launcher.py            # 默认 :20100，--port 覆盖
```

配置：`data/config.json`（首启自动生成骨架；provider key、路由规则、脱敏开关）。
