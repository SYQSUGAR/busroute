# 公交线路与站点采集器（AMap Transit Collector）

一个面向 **城市体检 / 交通分析 / GIS 数据整理** 的 Windows 桌面工具。输入高德 Web 服务 API Key 后，可按“单城市”或“省域”选择行政范围，自动发现公交站点、追溯经过站点的公交线路，并获取每条线路的完整站点序列、坐标和站序。

> 本项目只调用高德开放平台公开 Web 服务接口，不包含抓包、绕过鉴权或非公开接口。

## 主要功能

- 高德 Web 服务 Key 输入、测试，可选使用系统凭据库安全记忆 Key。
- 范围模式：
  - **单城市**：搜索结果自动限定为地级市；北京、上海、天津、重庆按城市处理。
  - **省域**：搜索结果自动限定为省级行政区，采集时自动遍历其下地级市/采集单元。
- 公交数据链：
  1. 通过行政区边界建立空间范围；
  2. 通过 POI 多边形搜索发现“公交车站”；
  3. 对高密度网格自动四叉细分，规避单次检索结果上限；
  4. 用公交站关键字接口取得站点 ID 及经过线路 ID；
  5. 用公交线路 ID 接口取得完整线路与 `busstops`；
  6. 保存每个线路站点的坐标和 `sequence`；
  7. 按 **站名 + 空间距离** 合并同一物理站点，形成“站点 → 经过线路”关系。
- SQLite 实时落盘，采集中断或配额不足后可继续，不需要从头开始。
- GUI 显示 API 调用数、POI 数、原始公交站数、线路数、合并站点数、进度和日志。
- 结果预览：线路表、合并站点表、无底图空间散点预览。
- 一键导出：
  - `bus_lines.csv`
  - `bus_line_stops.csv`
  - `stations_merged.csv`
  - `station_lines.csv`
  - `stops_raw.csv`
  - `bus_lines.geojson`
  - `stations_merged.geojson`
  - `公交线路与站点.xlsx`
  - `summary.json`

## 为什么采用“站点优先”的采集方式

高德当前公开的公交信息接口包括：

- 公交站 ID 查询
- 公交站关键字查询
- 公交线路 ID 查询
- 公交线路关键字查询

其中并没有“直接列出某城市全部公交线路”的单一接口。公交路线关键字查询又要求传入关键字，因此仅靠“1路、2路、K1……”枚举线路名会漏数据，也会造成大量无效请求。

本程序改用：

**行政区边界 → 公交站 POI 空间发现 → 公交站线路 ID → 线路 ID 详情**

这样更适合做尽可能完整的城市级底数整理。

同时需要明确：高德 POI 搜索官方文档说明，同一组搜索参数翻页最多获取 200 条，而且搜索服务并不承诺返回“全量底库”。因此本程序通过自动细分空间网格提高覆盖率，但 **无法对第三方地图底库做数学意义上的 100% 全量保证**。用于正式成果前，建议结合公交企业/交通主管部门台账抽检。

## API Key

请在高德开放平台申请 **Web 服务 API Key**。

程序不会把 Key 写进源码。勾选“使用系统凭据库记住 Key”时，使用 Python `keyring` 调用 Windows 凭据系统存储。

高德不同认证等级、产品和接口的日配额 / QPS 不同，公交信息查询属于高级服务。大城市或省域采集可能需要较高配额。程序采用 SQLite 断点续采，配额用尽后第二天或提升配额后可以直接继续。

## 运行

建议 Python 3.11。

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

也可以双击：

```text
run_windows.bat
```

## 打包 Windows 桌面程序

双击：

```text
build_windows.bat
```

或执行：

```bash
pyinstaller --noconfirm --clean --windowed --name TransitCollector --collect-all keyring app.py
```

输出目录：

```text
dist/TransitCollector/
```

仓库内同时包含 GitHub Actions 工作流 `.github/workflows/build-windows.yml`。推送 `v*` 标签或手动触发 workflow 后，会自动生成 Windows 构建产物。

## 操作流程

1. 输入 Web Key，点击“测试 Key”。
2. 选择范围：
   - 单城市：搜索“榆林”；
   - 省域：搜索“陕西”。
3. 在搜索结果中选择行政区。
4. 设置：
   - 请求间隔：默认 `0.35 秒/请求`；
   - 站点合并半径：默认 `120 m`。
5. 选择输出目录。
6. 点击“开始 / 继续采集”。
7. 完成后点击“导出当前数据库”。

如果中途停止、程序关闭、接口限额，重新选择同一范围和同一输出目录，再点“开始 / 继续采集”，会复用：

```text
transit_<adcode>.sqlite
```

已经完成的网格、POI 和线路不会重复采集。

## 数据表关系

### `bus_lines`

一行一条高德公交线路/方向，主要字段：

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

一行表示“某线路的某一个站序”：

- `line_id`
- `line_name`
- `sequence`
- `station_id`：合并后的物理站点 ID
- `raw_stop_id`：高德公交站原始 ID
- `stop_name`
- `longitude`
- `latitude`

### `stations_merged`

按“标准化站名 + 空间距离”聚合后的站点：

- `station_id`
- `name`
- `longitude`
- `latitude`
- `member_count`
- `line_count`
- `line_ids`
- `line_names`
- `raw_stop_ids`

这张表就是“**这个站点有哪些公交线路经过**”的直接结果。

### `station_lines`

标准化的多对多关系：

- `station_id`
- `station_name`
- `line_id`
- `line_name`
- `start_stop`
- `end_stop`

## 站点合并逻辑

公交站在地图底库中常会因为道路两侧、方向不同而出现不同原始站点 ID。程序默认将：

- 标准化名称相同；
- 两点距离不大于 `120 m`

的原始站点聚为一个物理站点。

半径可在 GUI 中修改。对于大型枢纽可适当提高，对于密集街区可降低。

## 坐标

导出的高德境内坐标按 `AMap/GCJ-02` 标记。与 WGS84、CGCS2000 数据叠加前，请先确认坐标系并按你的 GIS 工作流统一处理。

## 正式项目使用建议

- 省域采集前先用一个小城市验证 Key 配额和接口权限。
- 对正式城市体检成果，建议把本工具结果与交通主管部门/公交公司的线路台账进行抽检。
- 如果只关心“公交站点 500 米覆盖率”，一般优先使用 `stations_merged.geojson`，避免道路两侧重复站点造成统计偏差。
- 如果做线路网络分析，使用 `bus_line_stops.csv` + `bus_lines.geojson`。
- 如果做站点换乘/线路共站分析，使用 `station_lines.csv`。
- 不建议把 API Key 提交到 GitHub。

## 目录结构

```text
.
├─ app.py
├─ transit_collector/
│  ├─ api/amap.py
│  ├─ crawler.py
│  ├─ db.py
│  ├─ exporter.py
│  ├─ utils.py
│  └─ ui/main_window.py
├─ requirements.txt
├─ build_windows.bat
├─ run_windows.bat
├─ tests/test_core.py
└─ .github/workflows/build-windows.yml
```

## License

MIT
