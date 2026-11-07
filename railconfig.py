#!/usr/bin/env python3
"""railmap 配置加载 —— 读取项目根目录的 config.json（可选，git 忽略）。

不存在 config.json 时使用内置默认值，开箱即用：
  port        Web 服务端口（默认 8650）
  amap_key    高德 Web 服务 key（仅补站名 geocoding 脚本使用；占位符视为未配置）
  data_dir    数据目录（相对路径按项目根目录解析，默认 data/）
  cache_dir   查询缓存目录（默认 cache/）
  mcp_python  mcp_12306 数据层的 Python 解释器（默认 venv/bin/python）
  maps_client 外部地理编码 CLI 路径（fill_station_geo.py 可选依赖，默认不启用）

用法：cp config.example.json config.json 后按需修改。
"""

import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

# 占位符/空值视为未配置
_PLACEHOLDERS = {"", "YOUR_AMAP_KEY"}


def _resolve(path):
    """相对路径按项目根目录解析。"""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(BASE, path)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


CFG = load_config()

PORT = int(CFG.get("port", 8650))
DATA_DIR = _resolve(CFG.get("data_dir") or "data")
CACHE_DIR = _resolve(CFG.get("cache_dir") or "cache")
MCP_PYTHON = _resolve(CFG.get("mcp_python") or "venv/bin/python")
MAPS_CLIENT = _resolve(CFG.get("maps_client") or "")


def amap_key():
    """高德 key：config.json 的 amap_key 优先，其次环境变量 AMAP_API_KEY。"""
    k = str(CFG.get("amap_key") or "").strip()
    if k and k not in _PLACEHOLDERS:
        return k
    return os.environ.get("AMAP_API_KEY", "").strip()
