# AstrBot TheDivision2 数据查询插件

```
/数据查询+玩家名称+平台名称（不填则默认PC端，支持ubi/uplay/ps/psn/xbox/xbl）
/天赋+天赋名（/天赋 完美拳拳到肉）
/武器+武器名（/武器 鸟，/武器 F2000，/武器 胶喷）支持别名查询
/装备+装备名（/装备 陷阱，/装备 天平包，/装备 超载手）支持别名查询
/套装+套装名（/套装 天平，/套装 锤子，/套装 影子）支持别名查询
/周商
```

玩家数据查询功能依赖后端，目前未考虑提供公共的后端服务器，如有需要请自行部署UBI-GO项目（Docker）

[alexanderthegreat96/ubi-go: Ubisoft Connect API Wrapper](https://github.com/alexanderthegreat96/ubi-go)

[MuFen-mker/ubi-go: Ubisoft Connect API Wrapper](https://github.com/MuFen-mker/ubi-go/tree/origin)（我自己构建的）

使用docker部署后，在插件配置中配置服务后端

建议自部署文转图服务使用[自行部署文转图服务 | AstrBot](https://docs.astrbot.app/others/self-host-t2i.html)

目前支持QQ/OneBot

已搭载此插件的QQbot:

```
Yukina Minato:4015249404
```

结构

```
astrbot_plugin_TheDivision2/
├── data/
│   └── data.db          # 数据库
├── templates/
│   ├── equipment_card.html      # 套装模板
│   ├── gear_card.html           # 装备模板
│   ├── player_info.html         # 玩家信息模板
│   ├── talent_card.html         # 天赋模板
│   ├── weapon_card.html         # 武器模板
│   └── weekly_report.html       # 周商模板
├── .gitignore
├── README.md
├── _conf_schema.json            # 配置定义文件
├── logo.png
├── main.py                      # 插件核心入口
├── metadata.yaml
├── requirements.txt
└── translations.json            # 周商翻译字典
```
