# 公交数据采集与 GIS 分析工作台

面向 **城市体检 / 交通分析 / GIS 数据生产** 的 Windows 桌面工具。当前版本为 `v0.4.0`。

当前支持两套公交数据源：

- **TransBigData / 百度线路（默认、推荐）**：不需要高德 Key；
- **高德 Web 服务（备用）**：使用高德 Web Service Key，通过行政区、公交站 POI、线路 ID 链路采集。

采集完成后统一进入：

```text
SQLite
→ 线路/站点关系整理
→ 合并物理站点
→ GeoPandas / Shapely
→ GeoPackage / Shapefile
→ 公交站服务覆盖分析
```

## v0.4 的核心变化：从“按钮工具”改成“任务工作流”

v0.4 不再只关注“能不能爬、能不能导出”，而是增加项目状态、任务锁定、依赖检查、断点重试、部分数据保护、日志和成果登记。

每个公交 SQLite 数据库旁会自动创建项目目录：

```text
<database_stem>_project/
├─ project.json
├─ logs/
├─ exports/
├─ gis/
└─ backups/
```

旧版 `v0.3` 的 SQLite 可以直接打开，不需要迁移数据库。

项目顶部会显示：

```text
当前项目
数据源
数据库
任务状态
站点数据是否最新
```

主采集页和 GIS 页共用同一个当前数据库，避免出现“主页面操作榆林、GIS 页面却指向汉中数据库”的状态错位。

完整建设说明见：`docs/V0.4_WORKFLOW.md`。

## 任务状态

当前任务状态统一管理为：

```text
NEW
READY
DISCOVERING
CRAWLING
PAUSING
PAUSED
POSTPROCESSING
COMPLETED
PARTIAL
FAILED
```

用户开始采集以后，影响任务的配置会锁定，包括：

- 数据源
- 城市/行政区
- 扫描模式
- 额外线路关键词
- 请求参数
- 站点合并半径
- 输出目录

这样不会出现“界面改成 80m，但后台仍按 120m 运行”的情况。

## 打开已有项目 / 数据库

v0.4 可以直接打开已有：

```text
*.sqlite
*.db
project.json
```

打开后整个应用统一切换到该数据库，并恢复线路、合并站点、GIS 当前数据库上下文。

软件还会记录最近打开的数据库，下次启动自动恢复项目上下文。

## TransBigData 线路发现

旧脚本的核心逻辑是：

```python
lines = getALLbusline(cityE)  # 8684.cn 获取全市线路名称
line, stop = transbigdata.getbusdata(
    city=city,
    keywords=lines,
    accurate=True,
    timeout=20,
)
```

其中真正获取公交线路几何和站点序列的是 `transbigdata.getbusdata`。旧脚本失效的主要原因是 8684.cn 的城市线路目录不再可靠。

新版保留 TransBigData 抓线路/站点的方式，把线路名称发现替换为：

```text
输入城市
   ↓
百度地图分页宽泛搜索
公交 / 专线 / 城际 / 机场 / 环线 / 旅游 / 高铁等
   ↓
数字线路 + 常见前缀扫描
   ↓
线路关键词去重
   ↓
transbigdata.getbusdata(...)
```

### 完整扫描

- `1路` ～ `999路`
- `K1路` ～ `K199路`
- `B1路` ～ `B199路`
- `Y1路` ～ `Y199路`
- `夜1路` ～ `夜199路`
- `游1路` ～ `游199路`
- `快1路` ～ `快199路`
- 同时分页搜索公交、专线、城际、机场、环线、快线、夜班、旅游、高铁等命名线路
- 支持人工补充特殊线路关键词

### 快速扫描

- 数字线路扫描到 `300路`
- 常见前缀扫描到 `99路`
- 用于先验证城市是否能正常抓取

## v0.4 断点与失败重试

TransBigData 任务现在分层保存状态。

线路名称精确探测：

```text
tbd_probe
```

命名线路分页搜索：

```text
tbd_broad_probe
```

真正线路抓取：

```text
tbd_fetch_state
```

宽泛搜索按 `关键词 + 页码` 单独记录状态，不再因为某一页失败就把整个阶段错误标记为完成。

失败任务自动重试，默认最多 3 次；多次失败后标记为：

```text
failed_final
```

TransBigData 批次抓取失败时会自动拆分批次，例如：

```text
12 条失败
→ 6 + 6
→ 3 + 3
→ 最终隔离到单条异常线路
```

如果最终仍有失败项，数据库会保留为 `PARTIAL`，不会错误标记成完整成果。

## 安全停止与退出

运行中点击“停止”不会强杀进程，而是请求安全停止。

运行中直接关闭软件时会询问：

```text
安全停止并退出？
```

程序停止提交新请求并等待当前请求结束。如果无法在限定时间内安全结束，则取消退出，避免数据库处于不明确状态。

## 站点合并与数据依赖

地图数据常把道路两侧同名站作为不同记录。程序按照：

```text
标准化站名相同
+
空间距离 <= 合并半径
```

生成物理公交站，默认半径 `120m`。

v0.4 为站点合并增加依赖检查，记录：

- 当前原始线路/站点数据签名
- 当前合并半径
- 当前合并站点数量

如果：

```text
120m → 80m
```

或者继续采集导致原始站点发生变化，界面会提示：

```text
站点数据：⚠ 需更新
```

执行导出或 GIS 分析前会统一检查，不再依赖“先点一次导出”才能让 GIS 使用新站点结果。

## 部分数据保护

如果任务尚未完成，仍可以基于当前数据库导出或做分析，但程序会明确提示：

```text
当前为部分数据
不能视为完整成果
```

采集正在运行时，不允许直接执行正式 GIS 分析。

## 数据关系

SQLite 核心关系为：

```text
bus_lines
    ↓
line_stops
    ↓
raw_stops
    ↓
station_members
    ↓
station_groups
    ↓
station_lines
```

因此既能回答：

- 某个公交站经过哪些线路；
- 某条线路经过哪些站；
- 每个站在线路中的站序是多少。

## GIS 输出

基础 GIS 输出：

```text
transit_gis.gpkg
├─ bus_stations
├─ bus_routes
└─ route_stops
```

兼容输出：

```text
shp/
├─ bus_stations.shp
├─ bus_routes.shp
└─ route_stops.shp
```

同时保留 CSV、Excel 和 GeoJSON。

### bus_stations

合并后的物理公交站：

- `station_id`
- 站点名称
- 经过线路数量
- 经过线路名称/ID
- Point geometry

### bus_routes

一条线路/方向一条记录：

- `line_id`
- 线路名称
- 起终点
- 方向
- LineString geometry

### route_stops

保留线路—站点—站序：

- `line_id`
- `station_id`
- `raw_stop_id`
- `sequence`
- `stop_name`
- Point geometry

## 坐标处理

TransBigData `getbusdata` 返回 WGS84 数据；高德 Web Service 原始数据为 GCJ-02。

当前两套来源共用同一 SQLite / GIS 处理链，对外 GIS 基础成果统一生成：

```text
EPSG:4326 / WGS84
```

500m Buffer、面积和覆盖率不会直接在经纬度上计算，而是转换至本地米制投影。程序默认自动估算 UTM，也支持用户手动指定米制 CRS。

## 公交站服务覆盖分析

GIS 页可以输入：

- 建成区
- 居住用地
- 社区
- 街区
- 其他 Polygon / MultiPolygon

程序执行：

```text
公交站点
   ↓
米制投影
   ↓
Buffer（默认 500m）
   ↓
Union
   ↓
与分析范围 Intersection
   ↓
覆盖面积 / 总面积
   ↓
覆盖率
```

并输出：

```text
coverage_analysis.gpkg
├─ analysis_zones
├─ service_area
├─ covered_area
└─ uncovered_area
```

如果输入范围包含多个社区/街区，还会逐要素统计覆盖面积和覆盖率。

## 高德 Web 服务模式

高德模式保留原采集链：

```text
行政区边界
→ 公交站 POI 空间发现
→ 站点公交线路 ID
→ 线路 ID 详情
```

必须使用：

```text
服务平台 = Web服务
```

类型的高德 Key。

JS API Key / `securityJsCode` 不能替代 Web Service Key。

## 日志与成果登记

每次采集自动创建独立日志文件：

```text
<project>/logs/
```

`project.json` 同时记录：

- 当前任务状态
- 最后一次运行参数
- 日志文件
- 生成的 CSV / Excel / GIS / 分析成果
- 生成时间
- 数据源签名

便于项目追溯。

## 安装运行

建议 Python 3.11：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

也可双击：

```text
run_windows.bat
```

## Windows 打包

```text
build_windows.bat
```

PyInstaller 会收集：

- PySide6
- GeoPandas
- Shapely
- pyproj
- pyogrio
- TransBigData

输出：

```text
dist/TransitCollector/
```

## 项目结构

```text
.
├─ app.py
├─ transit_collector/
│  ├─ api/amap.py
│  ├─ crawler.py
│  ├─ db.py
│  ├─ exporter.py
│  ├─ gis.py
│  ├─ project.py
│  ├─ workflow.py
│  ├─ transbigdata_backend.py
│  ├─ transbigdata_adapter.py
│  ├─ transbigdata_v4.py
│  └─ ui/
│     ├─ main_window.py
│     ├─ integrated_window.py
│     ├─ transbigdata_window.py
│     └─ workbench_v4.py
├─ tests/
│  ├─ test_core.py
│  ├─ test_gis.py
│  ├─ test_transbigdata_backend.py
│  └─ test_workflow_v4.py
├─ docs/
│  └─ V0.4_WORKFLOW.md
├─ requirements.txt
└─ build_windows.bat
```

## 后续建设

v0.5 计划继续完善：

- 完整数据版本号和 dirty 依赖传播
- 成果历史版本管理
- 项目级 CRS / CGCS2000
- 数据质量检查
- 站点人工合并/拆分
- 更完整的项目首页

v0.6 再扩展城市体检交通 GIS 工具箱，例如公交服务空白区、公交站密度、公交线路密度、社区公交服务、行政村公交覆盖等。

## License

MIT（本项目自身代码）。TransBigData 使用其自身 BSD 许可证。
