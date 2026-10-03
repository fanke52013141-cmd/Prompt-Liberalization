# Stitch 设计流程与复现说明

日期：2026-10-04

## 用途

用 Google Stitch（MCP）为「提示词优化实验室」生成统一的 UI 设计方向，并把设计语言落地到本仓库的 `web/style.css`。所有生成的界面均为设计参考；线上运行的是本地实现，无任何 Stitch 运行时依赖。

## 连接方式

ZCode 客户端已在 `~/.zcode/cli/config.json` 配置 stitch MCP（`https://stitch.googleapis.com/mcp` + API Key）。
当会话未把 stitch 加载为原生工具时，可用仓库内的直连客户端（等价 JSON-RPC over HTTP）：

```bash
python scripts/stitch_client.py list_tools
python scripts/stitch_client.py call list_projects '{}'
```

## 本项目的设计资产

- Stitch 项目：`projects/18064636938297398645`（提示词优化实验室，PRIVATE）
- 设计系统：`assets/4194675638610718646`
  - 浅色模式，品牌蓝 `#2456D6`，成功 `#178A4C`，警示 `#B25E09`，中性底 `#F4F6FA`
  - 字体：PLUS_JAKARTA_SANS（标题）/ IBM_PLEX_SANS（正文），圆角 ROUND_EIGHT
  - 设计规范（designMd）：中文优先、卡片骨架、左侧深蓝导航、五步流程胶囊、统计小卡、提示块；禁止暴露内部编号与英文状态码
- 已生成页面：
  - `项目列表首页`（screen `461f9ad8…`）：三列软彩底清单（它会/它不会/你要准备）、流程高亮链、隐私信任框
  - `验证与使用·独立验证结果报告`（screen `967ca5c3…`）：五类结论图例卡、怎么看这份报告条、建议下一步条、区块化统计

本地参考副本：`data/stitch_home.html`、`data/stitch_verify.html`（data/ 目录不入库，可用 `get_screen` 重新下载）。

## 已落地到本仓库的设计元素

| Stitch 设计元素 | 本地实现 |
|---|---|
| 三列软彩底清单 + 头部徽标 | 首页值主张 `.vp-col.green/.orange/.blue` + `.vp-badge` |
| 流程高亮链（先测现状→…） | 首页 `.flow-chips/.flow-chip` |
| 五类结论图例卡 | 验证页 `.legend-row/.legend-chip`（中文结论+通俗解释） |
| 侧边栏「本地计算·隐私零上传」信任框 | `index.html` `.side-note` |
| 怎么看这份报告 / 建议下一步 | 报告页 `.tip` / `.next-step-box`（v1.1 已有，v1.2 保留） |

注意：生成稿里的 Material Symbols 图标字体未采用——本地应用必须完全离线可用，图标一律不用外部字体。

## 复现 / 继续生成

```bash
# 列出已有屏幕
python scripts/stitch_client.py call list_screens '{"projectId": "18064636938297398645"}'
# 生成新页面（耗时数分钟，勿重试）
python scripts/stitch_client.py call generate_screen_from_text '{"projectId": "18064636938297398645", "prompt": "……", "deviceType": "DESKTOP", "designSystem": "assets/4194675638610718646", "modelId": "GEMINI_3_8_FLASH"}'
```
