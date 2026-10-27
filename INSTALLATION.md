# 安装与版本

当前 Skill/核心 `2.2.0`，schema `skill_v2_002`。此源码包不含本机安装记录、环境变量、数据库、业务资料或导出文件。

需要 Python 3.12 和 PostgreSQL 16，连接配置使用自己的本地环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
```

将 `.env.example` 复制为本地 `.env` 并填写自己的 PostgreSQL 连接参数，然后执行：

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py capabilities
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install-b2b-leads.ps1
```

安装器按当前源码生成独立发布标识；机器上的源码路径、业务数据库与安装目标由使用者配置。使用已安装 Skill 的 `scripts/control.py` 入口执行研究。

测试：`.venv/Scripts/python.exe -m pytest backend/tests -q`。数据库测试使用临时独立库，需要创建测试库权限；跳过不算验收通过。

本版提速实施、受控基准及已知边界见 [实施记录](docs/IMPLEMENTATION-speed-first-v2.md)，规范见 [优化 spec](docs/specs/SPEC-speed-first-v2.md)。这些基准不代表真实 200 家任务的交付时效已达标。
