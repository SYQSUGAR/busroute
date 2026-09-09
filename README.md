# 公交线路与站点 GIS 采集分析器（AMap Transit Collector）

一个面向 **城市体检 / 交通分析 / GIS 数据生产** 的 Windows 桌面工具。输入高德 Web 服务 API Key 后，可按“单城市”或“省域”选择行政范围，自动采集公交站点和公交线路，并进一步生成 GeoPackage / Shapefile 矢量数据，完成公交站点服务半径覆盖率分析。

> 本项目只调用高德开放平台公开 Web 服务接口，不包含抓包、绕过鉴权或非公开接口。

## v0.2 架构

程序现在分为两部分：

```text
高德 API
   ↓
requests + SQLite
公交线路 / 公交站 / 站序 / 线路-站点关系
   ↓
GeoPandas + Shapely + pyogrio + pyproj
   ↓
Point / LineString / Polygon
   ↓
GeoPackage / SHP / GeoJSON
   ↓
Buffer / Union / Intersection / Area
   ↓
公交站服务覆盖率与分区统计
```

**SQLite 负责采集、关系数据和断点续采；GeoPandas/Shapely 负责矢量建库和空间分析。**

## 主要功能

### 1. 公交数据采集

- 高德 Web 服务 Key 输入、测试，可选使用系统凭据库安全记忆 Key。
- 范围模式：
  - **单城市**：搜索结果自动限定为城市级行政区；北京、上海、天津、重庆按城市处理。
  - **省域**：搜索结果限定为省级行政区，采集时自动遍历省内城市。
- 公交数据链：
  1. 获取行政区边界；
  2. 通过 POI 多边形搜索发现公交站；
  3. 高密度区域自动四叉细分网格；
  4. 通过公交站接口取得站点 ID 和经过线路 ID；
  5. 通过线路 ID 获取完整公交线路；
  6. 保存线路全部站点、坐标、站点序号 `sequence`；
  7. 按“标准化站名 + 空间距离”合并同一物理站点；
  8. 建立站点—线路多对多关系。
- SQLite 实时落盘，可停止、关闭、配额恢复后继续采集。
- GUI 显示 API 调用数、POI、原始公交站、线路、合并站点、进度和日志。

### 2. GIS 矢量建库

一键把数据库转换为标准 GIS 图层：

- `bus_stations`：合并后的实际公交站，Point
- `bus_routes`：公交线路，LineString
- `route_stops`：线路中的逐站点序列，Point

主输出：

```text
transit_gis.gpkg
```

兼容输出：

```text
shp/
├─ bus_stations.shp
├─ bus_routes.shp
└─ route_stops.shp
```

同时输出：

```text
stations_wgs84.geojson
bus_routes_wgs84.geojson
gis_metadata.json
```

### 3. 公交站服务覆盖率分析

桌面端增加 **“GIS分析”** 标签页，可以选择：

- 公交 SQLite 数据库
- 建成区 / 居住用地 / 街区 / 社区 / 自定义 Polygon 图层
- 服务半径，默认 `500 m`
- GPKG 多图层时的 layer 名
- 自动投影或手动指定米制投影 CRS
- 是否同时输出 Shapefile

分析过程：

```text
公交站
   ↓
投影到本地米制 CRS
   ↓
Buffer 500m
   ↓
Union / Dissolve
   ↓
与分析范围 Intersection
   ↓
面积统计
```

总体覆盖率：

```text
覆盖率 = 公交站服务范围与分析范围重叠面积 / 分析范围总面积 × 100%
```

输入范围图层是什么，分母就是什么。例如：

- 输入“建成区” → 建成区公交站覆盖率
- 输入“居住用地” → 居住用地公交服务覆盖率
- 输入“社区” → 自动计算各社区覆盖率
- 输入“街区” → 自动计算各街区覆盖率

如果输入图层包含多个 Polygon，程序会同时形成逐要素覆盖率统计。

覆盖分析输出：

```text
coverage_analysis.gpkg
├─ analysis_zones
├─ service_area
├─ covered_area
└─ uncovered_area
```

以及：

```text
shp/
├─ coverage_zones.shp
├─ service_area.shp
├─ covered_area.shp
└─ uncovered_area.shp

coverage_summary.json
```

## 一键导出内容

点击主界面的“导出 CSV / Excel / GIS”后，会生成：

```text
bus_lines.csv
bus_line_stops.csv
stations_merged.csv
station_lines.csv
stops_raw.csv
公交线路与站点.xlsx

transit_gis.gpkg
stations_wgs84.geojson
bus_routes_wgs84.geojson
gis_metadata.json

shp/
├─ bus_stations.shp
├─ bus_routes.shp
└─ route_stops.shp

summary.json
```

## 数据关系

### `bus_lines`

一行一条公交线路 / 方向：

- `line_id`
- `name`
- `type`
- `start_stop`
- `end_stop`
- `start_time`
- `end_time`
- `distance`
- `loop`
- `status`
- `direc`
- `company`
- `polyline`

### `bus_line_stops`

一行表示“某条线路的某个站序”：

- `line_id`
- `line_name`
- `sequence`
- `station_id`
- `raw_stop_id`
- `stop_name`
- `longitude`
- `latitude`

### `stations_merged`

按“标准化站名 + 空间距离”聚合后的物理站点：

- `station_id`
- `name`
- `longitude`
- `latitude`
- `member_count`
- `line_count`
- `line_ids`
- `line_names`
- `raw_stop_ids`

这张表可直接回答：

> 这个站点有哪些公交线路经过？

### `station_lines`

标准化的站点—线路关系：

- `station_id`
- `station_name`
- `line_id`
- `line_name`
- `start_stop`
- `end_stop`

## 为什么仍然保留 SQLite

不建议在采集阶段直接把 SHP 当数据库。

SQLite 更适合：

- 断点续采
- API 请求进度
- 一条线路多个站点
- 一个站点多条线路
- 原始站点与合并站点关系
- 去重
- 查询和更新

空间成果生成时，再把数据库转换为 GeoDataFrame。

因此程序采用：

```text
采集和关系管理：SQLite
空间几何与分析：GeoPandas / Shapely
最终成果：GeoPackage + SHP
```

## 为什么 GeoPackage 是主格式

虽然程序会自动输出 SHP，但推荐正式工作优先使用：

```text
transit_gis.gpkg
```

原因：

- 一个文件可包含多个图层
- 字段名没有 SHP 的 10 字符限制
- 长文本限制更少
- 中文属性更稳定
- 更适合站点、线路、线路站点等多图层数据
- ArcGIS Pro 和 QGIS 均可直接读取

Shapefile 主要作为传统 ArcGIS 工作流的兼容格式。

## 坐标系处理

### 原始数据

高德境内坐标属于：

```text
GCJ-02
```

GCJ-02 没有标准 EPSG 编码，因此不能直接把原始高德坐标错误标记为 `EPSG:4326`。

### GIS 输出

程序在生成标准 GIS 图层时执行：

```text
GCJ-02
  ↓
迭代近似反算
  ↓
WGS84
  ↓
EPSG:4326
```

因此：

- 原始 SQLite / CSV 坐标仍是高德 GCJ-02
- `transit_gis.gpkg`、SHP、`*_wgs84.geojson` 使用标准 WGS84 几何

GCJ-02 → WGS84 属于近似反算，不应把它理解为国家测绘成果的法定坐标转换。如果项目有严格坐标精度要求，应使用项目统一的正式坐标成果进行校核。

### 距离与面积

程序绝不会直接在经纬度上执行：

```python
point.buffer(500)
```

覆盖分析会自动：

```text
WGS84
  ↓
estimate_utm_crs()
  ↓
本地 UTM 米制投影
  ↓
500m Buffer / Area
```

也可以在 GUI 中手动指定，例如：

```text
EPSG:32649
```

手动 CRS 必须是以米为线性单位的投影坐标系。

## 为什么采用“站点优先”的采集方式

高德公开公交接口没有一个“列出某城市全部公交线路”的单一接口。

如果简单枚举：

```text
1路
2路
3路
K1路
……
```

会漏掉很多线路，也会产生大量无效请求。

本程序使用：

```text
行政区边界
  ↓
公交站 POI 空间发现
  ↓
公交站线路 ID
  ↓
公交线路 ID 详情
```

高密度 POI 区域会自动细分空间网格，以提高城市级覆盖率。

需要注意：第三方地图搜索接口不能对底库“数学意义上的 100% 全量”作保证。正式成果建议与交通主管部门或公交企业线路台账抽检。

## 安装

建议：

```text
Python 3.11
```

Windows：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

主要依赖：

```text
PySide6
requests
SQLite (Python 内置)
GeoPandas
Shapely
pyproj
pyogrio
openpyxl
keyring
```

## Windows 打包

双击：

```text
build_windows.bat
```

脚本会把：

- GeoPandas
- Shapely
- pyproj
- pyogrio
- keyring

相关数据文件一起交给 PyInstaller 收集。

也可以执行：

```bash
pyinstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name TransitCollector ^
  --collect-all keyring ^
  --collect-all geopandas ^
  --collect-all pyogrio ^
  --collect-all pyproj ^
  --collect-all shapely ^
  app.py
```

输出：

```text
dist/TransitCollector/
```

仓库内 `.github/workflows/build-windows.yml` 也会在 Windows Runner 上运行测试并打包。

## 操作流程

### 采集

1. 输入 Web Key。
2. 测试 Key。
3. 选择“单城市”或“省域”。
4. 搜索并选择行政区。
5. 设置请求间隔、站点合并半径。
6. 选择输出目录。
7. 开始 / 继续采集。
8. 完成后导出 CSV / Excel / GIS。

### GIS 分析

1. 打开“GIS分析”标签页。
2. 使用当前公交数据库，或选择已有 `.sqlite`。
3. 选择 Polygon 分析范围。
4. GPKG 如有多个图层，可输入 layer 名。
5. 设置服务半径，例如 `500 m`。
6. 分析 CRS 留空即可自动选择。
7. 点击“计算公交站服务覆盖率”。
8. 查看总体覆盖率和各分区覆盖率。
9. 在输出目录中获取 GPKG / SHP。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：

- 站点合并
- 表格导出
- GCJ-02 / WGS84 转换回归
- Point / LineString GIS 图层生成
- GPKG / SHP 输出
- 500m 服务范围覆盖分析

## 目录结构

```text
.
├─ app.py
├─ transit_collector/
│  ├─ api/
│  │  └─ amap.py
│  ├─ ui/
│  │  ├─ main_window.py
│  │  └─ integrated_window.py
│  ├─ crawler.py
│  ├─ db.py
│  ├─ exporter.py
│  ├─ gis.py
│  └─ utils.py
├─ tests/
│  ├─ test_core.py
│  └─ test_gis.py
├─ requirements.txt
├─ pyproject.toml
├─ build_windows.bat
├─ run_windows.bat
└─ .github/workflows/build-windows.yml
```

## License

MIT
