# cyRouter

LLM API Router — 多供应商路由网关（模型级有序供应商链、故障转移、额度监控、成本记账）。

- 下载：见 [Releases](https://github.com/cy7372/cyRouter/releases)
  - Windows：`cyRouter-Setup-v<ver>.exe`
  - Linux (deb)：`cyRouter-linux-v<ver>_amd64.deb`

## 快速开始（Linux）

```bash
sudo dpkg -i cyRouter-linux-v<ver>_amd64.deb
sudo systemctl enable --now cyrouter.service
# 面板: http://127.0.0.1:20100
```
