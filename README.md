# AstrBot TheDivision2 数据查询插件

[![AstrBot](https://img.shields.io/badge/AstrBot-插件-blue?style=flat&logo=github)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat&logo=python)](https://www.python.org/)

> 为 AstrBot 提供《全境封锁2》国际服玩家数据、装备、天赋、周商等查询功能，支持用户名/UID 查询，内置智能搜索建议。

---

## 📖 功能介绍

- **玩家数据查询**：支持通过玩家名或 ProfileId 查询，自动缓存数据，提升响应速度。
- **天赋查询**：支持模糊搜索，输入不完整或相似名称时自动推荐。
- **武器/装备/套装查询**：支持别名及模糊搜索，可快速找到所需道具。
- **周商信息**：自动获取每周商人商品列表，并生成长图分享。
- **智能搜索建议**：输入不准确或部分关键词时，系统会基于相似度推荐可能的目标。
- **多平台支持**：QQ、OneBot 等 AstrBot 支持的平台均可使用。

### 部署UBI-GO（Docker）

> ⚠️ **本插件本身不直接调用育碧 API，数据查询依赖 `ubi-go` 后端服务。** 使用前请先部署 `ubi-go`。

`ubi-go` 是一个 Go 语言编写的育碧 Connect API 封装服务，负责处理育碧的风控认证和数据获取。

克隆 `ubi-go` 仓库：

```bash
git clone https://github.com/MuFen-mker/ubi-go.git](https://github.com/alexanderthegreat96/ubi-go.git
cd ubi-go
```

配置环境变量（复制 `env_sample` 为 `.env`）

```
cp env_sample .env
```

编辑 `.env` 文件，填入你的育碧账号（建议使用专用小号）：

```
UBISOFT_ACCOUNTS=[{"email": "your_email@example.com", "password": "your_password"}]
API_PORT=8080
```

启动服务

```
docker-compose up -d
```

验证服务是否正常运行

```
curl http://localhost:8080/health
```

返回 `{"success": true}` 即表示启动成功。

### 配置插件

在 AstrBot 的插件配置界面，填写以下信息：

| 配置项               | 说明                                                          |
| -------------------- | ------------------------------------------------------------- |
| `ubi-go服务器地址`    | 用于数据查询功能                   |
| `默认查询平台（当用户未指定时使用）` | （`uplay` / `xbl` / `psn`，默认 `uplay`） |

### 部署文转图服务（可选）

插件生成图片依赖于 AstrBot 的文转图服务。推荐**自行部署**以获得更稳定、更快速的体验：

👉 [自行部署文转图服务 | AstrBot 文档](https://docs.astrbot.app/others/self-host-t2i.html)

---

## 📋 命令列表

| 命令格式                      | 说明                                                       | 示例                           |
| ----------------------------- | ---------------------------------------------------------- | ------------------------------ |
| `/数据查询 <玩家名> [平台]` | 查询玩家数据，支持 UID。平台可选 `uplay`/`xbl`/`psn` | `/数据查询 name/uid psn`     |
| `/天赋 <天赋名>`            | 查询天赋详情（支持模糊搜索）                               | `/天赋 完美拳拳到肉`         |
| `/武器 <武器名>`            | 查询武器详情（支持别名、模糊搜索）                         | `/武器 鸟` / `/武器 F2000` |
| `/装备 <装备名>`            | 查询装备详情（支持别名）                                   | `/装备 天平包`               |
| `/套装 <套装名>`            | 查询装备组/品牌详情（支持别名）                            | `/套装 影子`                 |
| `/周商`                     | 获取本周商人商品长图                                       | `/周商`                      |

---

## 🧩 文件结构

```
astrbot_plugin_TheDivision2/
├── data/
│   └── data.db                  # 本地数据库（天赋/武器/装备数据）
├── templates/                   # HTML 渲染模板
│   ├── equipment_card.html
│   ├── gear_card.html
│   ├── player_info.html
│   ├── talent_card.html
│   ├── weapon_card.html
│   └── weekly_report.html
├── .gitignore
├── README.md
├── _conf_schema.json            # 插件配置定义
├── logo.png
├── main.py                      # 插件主入口
├── metadata.yaml                # 插件元信息
├── requirements.txt             # 依赖清单
├── ubi_api_client.py            # 育碧 API 客户端
└── translations.json            # 周商翻译映射
```

---

## 🤖 已搭载此插件的 Bot

- **Yukina Minato** (QQ: `4015249404`)

---

## 📚 依赖

* `aiohttp` —— 异步 HTTP 客户端
* `jinja2` —— 模板渲染引擎
* `aiosqlite` —— 异步 SQLite 驱动
* `difflib` —— 内置模糊匹配（Python 标准库）

---

## 🙏 致谢

* 数据来源：[育碧](https://www.ubisoft.com/)
* 数据查询后端：[alexanderthegreat96/ubi-go: Ubisoft Connect API Wrapper](https://github.com/alexanderthegreat96/ubi-go)
* 部分数据整理自社区玩家及 [Division 2 Gear Spreadsheet](https://docs.google.com/spreadsheets/d/1nrPBmOrtpkEW1j5fbcRT7L-AXgsGOqMqxXoVtopsiGM/edit?usp=sharing)
* 感谢 AstrBot 提供的机器人框架支持

---

## 💬 交流反馈

- GitHub Issues: [提交问题](https://github.com/MuFen-mker/astrbot_plugin_TheDivision2/issues)

