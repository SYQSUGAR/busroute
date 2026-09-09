# 公交线路与站点 GIS 采集分析器

面向 **城市体检 / 交通分析 / GIS 数据生产** 的 Windows 桌面工具。当前版本 `v0.3.0` 支持两套公交数据源：

- **TransBigData / 百度线路（默认、推荐）**：公交线路和站点抓取不需要高德 Key；
- **高德 Web 服务（保留原模式）**：使用高德 Web Service Key，通过行政区、公交站 POI、公交线路 ID 链路采集。

采集完成后统一进入 SQLite → GeoPandas/Shapely → GeoPackage / Shapefile → 服务覆盖分析的 GIS 流程。

## 为什么改成 TransBigData 模式

旧版脚本的核心逻辑是：

```python
lines = getALLbusline(cityE)           # 8684.cn 获取全市线路名称
line, stop = transbigdata.getbusdata(
    city=city,
    keywords=lines,
    accurate=True,
    timeout=20,
)
```

其中真正获取公交线路几何和站点序列的是 `transbigdata.getbusdata`。旧脚本失效的主要原因是 **8684.cn 的城市公交线路目录不再可可靠使用**，并不是 TransBigData 的线路抓取逻辑本身必须依赖 8684。

新版因此移除了 `8684.cn`，改成：

```text
输入城市名
   ↓
百度地图分页宽泛搜索
（公交/专线/城际/机场/环线/旅游/高铁等）
   ↓
数字线路 + 常见前缀扫描
1-999 路 + K/B/Y/夜/游/快
   ↓
线路关键词去重
   ↓
transbigdata.getbusdata(...)
   ↓
线路 LineString + 站点序列 Point
```

也就是说，**仍然使用你原来 TransBigData 的公交抓取方式**，只是把失效的“线路名称清单来源”换掉了。

## TransBigData 线路发现模式

桌面端新增“数据源”和“线路扫描”设置。

### 完整扫描（默认）

- `1路` ～ `999路`
- `K1路` ～ `K199路`
- `B1路` ～ `B199路`
- `Y1路` ～ `Y199路`
- `夜1路` ～ `夜199路`
- `游1路` ～ `游199路`
- `快1路` ～ `快199路`
- 同时分页检索公交、专线、城际、机场、环线、快线、夜班、旅游、高铁等命名线路
- 支持用户额外填写当地特殊线路名称，例如 `榆横城际公交`

### 快速扫描

用于先测试城市是否可正常抓取：

- 数字线路扫描到 `300路`
- 常见前缀扫描到 `99路`
- 命名线路分页深度较低

完整扫描覆盖面更高，但请求数量更大。

## 断点续采

TransBigData 模式不再一次性把所有工作放在内存中。SQLite 新增状态表：

- `tbd_probe`：记录哪些线路关键词已经探测；
- `tbd_line_keywords`：记录已经发现的实际线路关键词；
- `tbd_fetch_state`：记录哪些线路已经交给 TransBigData 获取。

中途停止或程序关闭后，再次选择同一城市和同一输出目录即可继续。

数据库文件示例：

```text
transit_tbd_榆林市.sqlite
```

## TransBigData 的数据来源说明

当前固定使用：

```text
transbigdata==0.5.3
```

`getbusdata` 内部使用百度地图网页搜索获取城市代码、公交线路 UID、线路几何和站点信息。该方法不要求用户提供高德 Web Key。

需要注意：这属于 TransBigData 已有的数据获取实现，其依赖的百度网页端请求不是面向第三方长期稳定承诺的正式开放 API，未来百度网页接口变化时仍可能需要适配。因此软件保留高德 Web 服务采集模式作为另一条数据链。

同时，不论是关键词扫描还是第三方地图底库，都不能承诺数学意义上的“100% 全量”。正式成果建议与公交企业/交通主管部门线路台账抽检。

## 坐标处理

TransBigData `getbusdata` 返回 WGS84 数据。

为让两套采集源共用同一个 SQLite 和 GIS 处理流程，程序内部会把 TransBigData WGS84 临时标准化到 GCJ-02 数据库存储体系；GIS 导出时再通过统一的坐标转换流程生成 `EPSG:4326` 图层。

因此对外 GIS 成果仍统一为：

```text
EPSG:4326 / WGS84
```

500m Buffer、面积和覆盖率分析不会直接在经纬度上计算，而是自动估算当地 UTM 米制投影，或使用用户指定的米制 CRS。

## GIS 输出

一键导出会生成：

```text
CSV
├─ bus_lines.csv
├─ bus_line_stops.csv
├─ stations_merged.csv
├─ station_lines.csv
└─ stops_raw.csv

Excel
└─ 公交线路与站点.xlsx

GeoPackage
└─ transit_gis.gpkg
   ├─ bus_stations
   ├─ bus_routes
   └─ route_stops

Shapefile
└─ shp/
   ├─ bus_stations.shp
   ├─ bus_routes.shp
   └─ route_stops.shp
```

### `bus_stations`

合并后的物理公交站点：

- `station_id`
- 站点名称
- 经过线路数
- 经过线路名称/ID
- Point geometry

### `bus_routes`

一条线路/方向一条记录：

- `line_id`
- 线路名称
- 起终点
- 方向
- LineString geometry

### `route_stops`

保留线路—站点—站序关系：

- `line_id`
- `station_id`
- `raw_stop_id`
- `sequence`
- `stop_name`
- Point geometry

因此既能回答“这个站经过哪些线路”，也能回答“一条线路按什么顺序经过哪些站”。

## 站点合并

地图数据常把道路两侧同名站点作为不同记录。程序当前按照：

```text
标准化站名相同 + 空间距离 <= 合并半径
```

聚合物理站点，默认半径 `120m`，桌面端可调整。

## 公交站 500m 覆盖分析

“GIS分析”页签可以选择：

- 建成区
- 居住用地
- 社区
- 街区
- 其他 Polygon / MultiPolygon 图层

程序自动执行：

```text
公交站点
   ↓
投影至米制 CRS
   ↓
500m Buffer
   ↓
Union
   ↓
与分析范围 Intersection
   ↓
覆盖面积 / 总面积
   ↓
覆盖率
```

并输出 `coverage_analysis.gpkg`、统计 JSON 以及可选 SHP。

## 高德 Web 服务模式

原来的高德采集链仍然保留：

```text
行政区边界
→ 公交站 POI 空间发现
→ 站点公交线路 ID
→ 线路 ID 详情
```

该模式必须使用 **服务平台=Web服务** 的高德 Key。JS API Key / `securityJsCode` 不能替代 Web Service Key。

TransBigData 模式则无需填写高德 Key。

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

PyInstaller 会一起收集：

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

GitHub Actions 也保留自动测试和 Windows 构建流程。

## 项目结构

```text
.
├─ app.py
├─ transit_collector/
│  ├─ api/amap.py
│  ├─ crawler.py
│  ├─ transbigdata_backend.py
│  ├─ transbigdata_adapter.py
│  ├─ db.py
│  ├─ exporter.py
│  ├─ gis.py
│  ├─ utils.py
│  └─ ui/
│     ├─ main_window.py
│     ├─ integrated_window.py
│     └─ transbigdata_window.py
├─ tests/
│  ├─ test_core.py
│  ├─ test_gis.py
│  └─ test_transbigdata_backend.py
├─ requirements.txt
├─ build_windows.bat
└─ .github/workflows/
```

## License

MIT（本项目自身代码）。TransBigData 使用其自身 BSD 许可证。
