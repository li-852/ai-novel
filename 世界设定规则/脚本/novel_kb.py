#!/usr/bin/env python3
"""
小说知识库管理器 - 单一入口
用途：将小说设定文件索引到 LanceDB，写作时按需检索，减少 token 消耗。

用法（在项目根目录 ai-novel 下运行）：

  查询设定（写作前必做）：
    python 世界设定规则/脚本/novel_kb.py query "关键词"        关键词查询
    python 世界设定规则/脚本/novel_kb.py query "关键词" -n 5   返回5条
    python 世界设定规则/脚本/novel_kb.py query "关键词" --state 查询+显示状态
    python 世界设定规则/脚本/novel_kb.py query                 全量查询
    python 世界设定规则/脚本/novel_kb.py query -n 100           全量查询返回100条

  状态与索引：
    python 世界设定规则/脚本/novel_kb.py state [-q]            主角状态（-q精简）
    python 世界设定规则/脚本/novel_kb.py index                 增量更新（文件未变化则跳过）
    python 世界设定规则/脚本/novel_kb.py index --force          强制重建（忽略哈希，全量重写）
    python 世界设定规则/脚本/novel_kb.py index --clear-cache    清理缓存后退出

  工具命令：
    python 世界设定规则/脚本/novel_kb.py collections           列出所有表
    python 世界设定规则/脚本/novel_kb.py verify [--fix]        校验索引（--fix自动修复）
    python 世界设定规则/脚本/novel_kb.py merge <目录> [输出]    整合目录为单个md
    python 世界设定规则/脚本/novel_kb.py merge 世界设定规则/设定    整合目录为单个md
    python 世界设定规则/脚本/novel_kb.py cleanup               清理缓存和进程锁
    python 世界设定规则/脚本/novel_kb.py wc <文件>             统计字数（默认中文字符）
    python 世界设定规则/脚本/novel_kb.py wc <文件> --all        统计所有字符（含标点）

  章节校验：
    python 世界设定规则/脚本/novel_kb.py check                 输出变量快照
    python 世界设定规则/脚本/novel_kb.py check --chapter 第2章  交叉校验指定章节
"""

import argparse
import hashlib
import json
import re
import sys
import io
import signal
import atexit
import os
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta

# Windows 控制台 UTF-8 输出（仅直接运行时，避免 import 时重复包装）
if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

import yaml

# ---- 第三方库集中导入（避免函数内重复 import，O8）----
# 轻量/高频：统一在此导入；极重且按需的（torch/onnxruntime/optimum/safetensors/tokenizers）
# 仍保留在 get_embedding_fn 内的惰性导入，避免无 GPU 环境下拖慢非检索命令。
import threading
import traceback
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import numpy as np
import jieba

# ---- 热点正则预编译（避免高频调用反复编译，O13）----
RE_CHAPTER = re.compile(r'第\s*(\d+)\s*章')
RE_VOLUME = re.compile(r'第\s*(\d+)\s*卷')
# 卷段字：一卷拆成上/中/下时，目录名与坐标都带段字（如「第5卷上 第046章」「第5卷中_丹阁惊变」）
RE_VOLUME_SEG = re.compile(r'第\s*(\d+)\s*卷\s*([上中下])?')
RE_HANZI = re.compile(r'[一-鿿]{2,4}')
RE_HANZI_CHAR = re.compile(r'[一-鿿]')


def vol_seg(text):
    """解析坐标里的卷号与段字，返回 (卷号或 None, 段字或空串)。

    段字为空串表示未标：旧卷（一卷一目录）与只写数字的坐标都走空串，
    行为与改动前一致；只有坐标显式写了「上／中／下」时，才按段字过滤。
    """
    m = RE_VOLUME_SEG.search(text or "")
    if not m:
        return None, ""
    return int(m.group(1)), m.group(2) or ""

# 版本号
__version__ = "4.6.0"

# 设置离线模式，避免联网超时
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

# ============================================================
# 颜色输出
# ============================================================


class Colors:
    """ANSI 颜色代码"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @staticmethod
    def disable():
        for attr in ('RESET', 'BOLD', 'DIM', 'RED', 'GREEN', 'YELLOW', 'BLUE', 'MAGENTA', 'CYAN'):
            setattr(Colors, attr, '')


def _init_colors():
    if not hasattr(sys.stdout, 'isatty') or not sys.stdout.isatty():
        Colors.disable()
        return
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            Colors.disable()


_init_colors()


def print_success(msg: str):
    print(f"  {Colors.GREEN}✓{Colors.RESET} {msg}")


def print_warning(msg: str):
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {msg}")


def print_error(msg: str):
    print(f"  {Colors.RED}✗{Colors.RESET} {msg}")


def print_info(msg: str):
    print(f"  {Colors.BLUE}ℹ{Colors.RESET} {msg}")


def print_header(msg: str):
    print(f"\n{Colors.BOLD}{Colors.CYAN}## {msg}{Colors.RESET}")


# ============================================================
# 进程管理
# ============================================================

_PROCESS_LOCK_FILE = Path(__file__).resolve().parent / ".process.lock"


def _acquire_process_lock():
    """获取进程锁，防止重复运行（原子操作）"""
    lock_file = _PROCESS_LOCK_FILE
    my_pid = os.getpid()
    my_timestamp = time.time()

    try:
        if lock_file.exists():
            try:
                lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
                lock_time = lock_data.get("timestamp", 0)
                lock_pid = lock_data.get("pid")

                if my_timestamp - lock_time > 300:
                    print("  [进程] 检测到过期锁文件，清理中...")
                elif lock_pid and _is_process_running(lock_pid):
                    print(f"  [进程] 另一个实例正在运行 (PID: {lock_pid})，退出")
                    sys.exit(1)
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        lock_data = {"pid": my_pid, "timestamp": my_timestamp}
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_PROCESS_LOCK_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(lock_data, f)
            os.replace(tmp_path, str(lock_file))
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def _release_process_lock():
    """释放进程锁"""
    try:
        _PROCESS_LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _is_process_running(pid: int) -> bool:
    """检查进程是否在运行"""
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    exit_code = ctypes.c_ulong()
                    if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return exit_code.value == 259
                    return True
                finally:
                    kernel32.CloseHandle(handle)
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, ValueError):
        return False


_SHUTDOWN_REQUESTED = False


def _setup_signal_handlers():
    """设置信号处理器，确保优雅退出"""
    def _cleanup(signum, frame):
        global _SHUTDOWN_REQUESTED
        _SHUTDOWN_REQUESTED = True
        print("\n  [进程] 收到终止信号，正在清理...")
        try:
            _release_process_lock()
        except Exception:
            pass
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _cleanup)
        signal.signal(signal.SIGTERM, _cleanup)
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, _cleanup)
    except (OSError, AttributeError):
        pass


atexit.register(_release_process_lock)

# ============================================================
# 全局变量
# ============================================================

_embedding_fn = None
_embedding_model_name = None
_reranker = None
_resources = None
_db = None

# ============================================================
# 配置常量
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_MODEL_DIR = SCRIPT_DIR / "模型"
PROJECT_ROOT = SCRIPT_DIR.parent
PROJECT_REAL_ROOT = PROJECT_ROOT.parent
WORKSPACE = PROJECT_ROOT / "设定" / "章节追踪"
TABLE_PREFIX = "novel_"
DB_PATH = str(SCRIPT_DIR / ".lancedb_data")
# 单表化：所有设定统一写入一张表，通过 type 列过滤（替代旧的多文件多表结构）
MAIN_TABLE = f"{TABLE_PREFIX}main"
TABLE_VERSION = 2  # 旧的多表结构(version1)会在 index 时自动清理

EMBEDDING_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
EMBEDDING_BATCH_SIZE = 64

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
RERANKER_ENABLED = True
RERANKER_TOP_K = 5

EMBEDDING_MODEL_OVERRIDE = "BAAI/bge-small-zh-v1.5"

MODEL_TIERS = [
    ("BAAI/bge-m3",              4000, 3000, 1024, "最佳中文+多粒度+8192上下文"),
    ("BAAI/bge-large-zh-v1.5",   3000, 2000, 1024, "高精度中文"),
    ("BAAI/bge-base-zh-v1.5",    1500,  800,  768, "均衡"),
    ("BAAI/bge-small-zh-v1.5",    800,  400,  512, "快速"),
    (None,                          0,    0,  384, "默认(all-MiniLM-L6-v2)"),
]

# 分块配置
CHUNK_OVERLAP_CHARS = 200
SEMANTIC_CHUNK_MAX = 800
SEMANTIC_SPLIT_THRESHOLD = 0.5
CHUNK_SIZE_MIN = 200
CHUNK_SIZE_MAX = 1200
SCENE_DEDUP_LOOKBACK = 20  # 场景去重仅回溯前 N 章，控制扫描成本并避免跨卷误判

COLLECTION_WEIGHTS = {
    "characters": 1.5, "world_settings": 1.4, "rules": 1.3,
    "story_board": 1.2, "mc_state": 1.2,
    "outline_main": 1.1, "outline_factions": 1.1, "outline_growth": 1.1,
}

SYNONYM_MAP = {
    "十倍体": "十倍体质", "体质": "十倍体质",
    "体质觉醒": "十倍体质 上古本源印记",
    "修为": "境界", "实力": "境界 战斗力", "战力": "战斗力",
    "等级": "境界", "突破": "境界 突破 天罚",
    "境界提升": "突破 天罚",
    "修炼速度": "十倍体质 境界",
    "宗门": "三宗 宗门 天陨阁", "门派": "宗门", "势力": "宗门 王朝",
    "三宗": "玄天宗 炽天宗 归泉文廷",
    "天陨": "天陨阁 执事堂 丹阁 器阁",
    "执事堂": "天陨阁 沈渊 沈玄策",
    "丹阁": "天陨阁 炼丹 鉴无遗",
    "大陆": "曦和大陆 烛阴大陆 不死洲",
    "王朝": "曦和王朝 烛阴王朝",
    "曦和": "曦和大陆 曦和王朝",
    "烛阴": "烛阴大陆 烛阴王朝",
    "法器": "灵器 法器 芥子坠", "灵器": "法器 灵器",
    "丹药": "丹药 炼丹 养元丹 洗髓丹", "功法": "功法 武技 潜龙诀",
    "灵药": "丹药 炼丹", "丹方": "丹药 炼丹",
    "主角": "沈默 陆沉", "养父": "沈重云",
    "小默": "沈默", "陆沉": "沈默",
    "堂主": "沈渊 沈玄策 执事堂",
    "阁主": "天陨阁 沈沧溟",
    "货币": "渊石 渊坊 飞票",
    "秘境": "秘境 天罚",
    "阵法": "阵法 阵法师",
    "灵兽": "灵兽 驯兽",
    "历史": "天陨阁 三宗 无矩",
}

# 缓存统一隔离到 .cache/（O20 产物与源码分离；O18 限权 0o700）
CACHE_DIR = SCRIPT_DIR / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(CACHE_DIR, 0o700)  # 仅当前用户可读写（Windows 忽略）
except OSError:
    pass

SEMANTIC_CACHE_FILE = CACHE_DIR / ".semantic_cache.json"
SEMANTIC_CACHE_TTL = 3600
SEMANTIC_CACHE_MAX = 500  # LRU 上限：超出移除最旧（O9 已具备 LRU，此处放宽提升命中率）
SEMANTIC_CACHE_THRESHOLD = 0.95

HASH_CACHE_FILE = CACHE_DIR / ".hash_cache.json"

HISTORY_FILE = CACHE_DIR / ".query_history.json"
CONFIG_FILE = SCRIPT_DIR / ".ask_config.json"  # 配置类非缓存，留在脚本目录

DEFAULT_CONFIG = {"top": 2, "min_score": 0.5, "max_len": 150, "mode": "brief"}

# 特殊分块配置（yaml_characters / yaml_world 需要特定文件结构）
_MANUAL_INDEX_CONFIG = {
    "mc_state":          {"file": "设定/章节追踪/主角状态.yaml",   "type": "workspace",  "chunk_fn": "yaml_flat"},
    "story_board":       {"file": "设定/章节追踪/故事看板.md",     "type": "workspace",  "chunk_fn": "markdown_sections"},
    "chapter_tracking":  {"file": "设定/章节追踪/章节追踪.yaml",  "type": "workspace",  "chunk_fn": "yaml_flat"},
}

# 角色类 yaml 文件使用 yaml_characters 分块
_CHARACTER_YAMLS = {
    "设定/01_设定集/角色_主角与核心.yaml",
    "设定/01_设定集/角色_大宗门与丹阁.yaml",
    "设定/01_设定集/角色_曦和势力.yaml",
    "设定/01_设定集/角色_烛阴势力.yaml",
    "设定/01_设定集/角色_烛阴未设定.yaml",
}

# 灵兽类 yaml 使用 yaml_world 分块（结构与世界设定类似）
_BEAST_YAMLS = {
    "设定/01_设定集/灵兽_飞禽.yaml",
    "设定/01_设定集/灵兽_走兽.yaml",
    "设定/01_设定集/灵兽_水族.yaml",
    "设定/01_设定集/灵兽_虫类.yaml",
    "设定/01_设定集/灵兽妖兽体系_补充.yaml",
}

# 不索引的文件/目录
_EXCLUDE_PATTERNS = {"地图编辑器.json", "更新日志.md", "世界地图.html"}

# 查询过滤类型白名单（防止注入）
VALID_FILTER_TYPES = frozenset({"character", "world", "rule", "outline", "workspace", "reference", "beast", "setting", "project"})

# 主角状态精简显示的关键字段
_STATE_SUMMARY_KEYS = ["最后更新:", "姓名:", "真实境界:", "伪装境界:", "位置:", "身份:", "暴露风险:"]


def _sanitize_table_key(raw: str) -> str:
    """将含中文的文件路径转为合法的 LanceDB 表名（仅字母数字下划线）"""
    result = []
    for ch in raw:
        if ch.isascii() and (ch.isalnum() or ch == '_'):
            result.append(ch)
        elif ch in ('/', '\\', ' ', '-', '.'):
            result.append('_')
        else:
            result.append(f"u{ord(ch):x}")
    key = "_".join("".join(result).split("_"))
    return key.strip("_")


def _build_index_config() -> dict:
    """动态构建索引配置：手动配置 + 设定目录扫描 + CLAUDE.md"""
    config = dict(_MANUAL_INDEX_CONFIG)
    indexed_files = {v["file"] for v in config.values()}

    # 扫描 CLAUDE.md
    claude_rel = "../CLAUDE.md"
    claude_path = PROJECT_REAL_ROOT / "CLAUDE.md"
    if claude_path.exists():
        config["claude_md"] = {"file": claude_rel, "type": "project", "chunk_fn": "markdown_sections"}
        indexed_files.add(claude_rel)

    # 扫描 设定/ 目录
    settings_dir = PROJECT_ROOT / "设定"
    for fpath in sorted(settings_dir.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.suffix not in (".md", ".yaml", ".yml"):
            continue
        if fpath.name in _EXCLUDE_PATTERNS:
            continue
        rel = str(fpath.relative_to(PROJECT_ROOT)).replace("\\", "/")
        if rel in indexed_files:
            continue

        # 生成唯一 key（仅允许字母数字下划线）
        stem = fpath.stem
        rel_dir = fpath.parent.relative_to(settings_dir)
        if rel_dir == Path("."):
            key = stem
        else:
            key = f"{str(rel_dir).replace(chr(92), '/').replace('/', '_')}_{stem}"
        # 中文字符转为拼音首字母缩写，其余非法字符替换为下划线
        key = _sanitize_table_key(key)

        # 确定 chunk_fn 和 type
        if rel in _CHARACTER_YAMLS:
            chunk_fn, ctype = "yaml_characters", "character"
        elif rel in _BEAST_YAMLS:
            chunk_fn, ctype = "yaml_world", "beast"
        elif fpath.suffix in (".yaml", ".yml"):
            chunk_fn, ctype = "yaml_world", "world"
        else:
            chunk_fn, ctype = "markdown_sections", "setting"

        config[key] = {"file": rel, "type": ctype, "chunk_fn": chunk_fn}
        indexed_files.add(rel)

    return config


INDEX_CONFIG = _build_index_config()

CHINESE_STOPWORDS = frozenset([
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "自己", "别人", "大家",
    "这", "那", "这个", "那个", "这些", "那些", "这里", "那里", "什么", "哪", "谁",
    "是", "有", "在", "说", "要", "去", "来", "看", "做", "让", "给", "把", "被",
    "会", "能", "可以", "应该", "需要", "想", "知道", "觉得", "认为", "没有",
    "的", "了", "着", "过", "地", "得", "和", "与", "及", "或", "而", "但", "但是",
    "然而", "不过", "虽然", "尽管", "因为", "由于", "所以", "因此", "如果", "假如",
    "只要", "只有", "除非", "无论", "不管", "即使", "哪怕", "既然", "以及",
    "就", "也", "都", "还", "又", "再", "才", "已", "已经", "曾", "曾经",
    "很", "太", "非常", "十分", "特别", "最", "更", "比较", "相当",
    "不", "没", "没有", "别", "不要", "不能", "不会", "不是",
    "一", "一个", "一些", "一点", "二", "三", "几", "多", "多少", "每", "各",
    "上", "下", "前", "后", "里", "外", "中", "内", "左", "右", "东", "西", "南", "北",
    "时", "时候", "之后", "之前", "以后", "以前", "现在", "目前", "刚才",
    "对", "从", "到", "向", "往", "关于", "对于", "至于", "通过", "经过",
    "用", "以", "为", "所", "如", "之", "其", "则", "乃", "于",
    "并", "且", "或者", "还是", "即", "即为", "等", "等等", "之类",
    "吧", "呢", "啊", "哦", "嗯", "哈", "呀", "嘛",
    "就是", "这样", "那样", "怎么", "如何", "为什么", "哪里", "哪个",
    "可能", "或许", "大概", "也许", "确实", "的确", "当然", "反正",
])

JIEBA_CUSTOM_WORDS = [
    ("十倍体质", 100, "n"), ("潜龙诀", 100, "n"), ("上古本源印记", 100, "n"),
    ("万法归源", 50, "n"), ("凝元诀", 50, "n"), ("噬灵术", 50, "n"),
    ("焚天诀", 50, "n"), ("玄冰诀", 50, "n"), ("厚土诀", 50, "n"),
    ("聚灵诀", 50, "n"), ("破灭诀", 50, "n"), ("归元诀", 50, "n"),
    ("天陨阁", 100, "nt"), ("执事堂", 80, "nt"), ("丹阁", 80, "nt"),
    ("器阁", 80, "nt"), ("长老阁", 80, "nt"), ("炼丹堂", 80, "nt"),
    ("玄天宗", 100, "nt"), ("炽天宗", 100, "nt"), ("归泉文廷", 100, "nt"),
    ("云垂剑庭", 100, "nt"), ("影会", 80, "nt"), ("潜影守廷", 80, "nt"),
    ("暗影议会", 60, "nt"), ("归墟宗", 60, "nt"),
    ("曦和大陆", 100, "ns"), ("烛阴大陆", 100, "ns"), ("不死洲", 80, "ns"),
    ("梧栖国", 80, "ns"), ("沧汐王朝", 80, "ns"), ("云垂剑庭", 80, "ns"),
    ("曦和王朝", 80, "ns"), ("烛阴王朝", 80, "ns"), ("天陨三洲", 60, "ns"),
    ("蜕凡", 100, "n"), ("凝真", 100, "n"), ("铭纹", 100, "n"),
    ("掌道", 100, "n"), ("道种", 100, "n"), ("燃灯", 100, "n"),
    ("源初", 100, "n"), ("无矩", 100, "n"), ("天罚", 80, "n"),
    ("蜕凡前期", 60, "n"), ("蜕凡中期", 60, "n"), ("蜕凡后期", 60, "n"),
    ("凝真前期", 50, "n"), ("凝真中期", 50, "n"), ("凝真后期", 50, "n"),
    ("铭纹前期", 50, "n"), ("铭纹中期", 50, "n"), ("铭纹后期", 50, "n"),
    ("渊石", 80, "n"), ("渊能", 60, "n"), ("渊印", 60, "n"),
    ("养元丹", 80, "n"), ("洗髓丹", 80, "n"), ("破境丹", 80, "n"),
    ("疗伤丹", 60, "n"), ("聚气丹", 60, "n"), ("回气丹", 60, "n"),
    ("续脉丹", 60, "n"), ("生肌丹", 60, "n"), ("固元丹", 60, "n"),
    ("凝真丹", 60, "n"), ("聚灵丹", 60, "n"), ("延寿丹", 60, "n"),
    ("芥子坠", 80, "n"), ("隐息衣", 80, "n"), ("影字令牌", 60, "n"),
    ("飞票", 50, "n"), ("渊坊", 50, "n"),
    ("沈默", 100, "nr"), ("沈重云", 100, "nr"), ("沈渊", 100, "nr"),
    ("沈玄策", 80, "nr"), ("沈沧溟", 80, "nr"), ("沈焚渊", 60, "nr"),
    ("陆沉", 80, "nr"), ("苏若笙", 80, "nr"), ("陆九霄", 80, "nr"),
    ("鉴无遗", 60, "nr"), ("焰青", 60, "nr"),
    ("丹道", 60, "n"), ("阵法", 60, "n"), ("法器", 60, "n"),
    ("灵兽", 60, "n"), ("秘境", 60, "n"), ("功法", 60, "n"),
    ("炼丹", 60, "n"), ("炼器", 60, "n"), ("驯兽", 60, "n"),
    ("伪装规则", 50, "n"), ("宗门体系", 50, "n"),
]

_jieba_loaded = False

# ============================================================
# 工具函数
# ============================================================


def _atomic_write_json(path: Path, data):
    """原子写入 JSON 文件"""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(path))
    except OSError:
        try:
            os.unlink(tmp_path)
        except (OSError, NameError):
            pass


def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return config


def read_file(path: Path, label: str = "文件") -> str:
    if not path.exists():
        return f"[{label}] 不存在: {path.name}"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        return f"[{label}] 读取失败: {e}"


def truncate_text(text: str, max_len: int = 2000) -> str:
    return text if len(text) <= max_len else text[:max_len] + f"\n...(原长 {len(text)} 字符)"


def file_hash(path: Path) -> str:
    """计算文件SHA256哈希"""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def load_hash_cache() -> dict:
    if HASH_CACHE_FILE.exists():
        try:
            return json.loads(HASH_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_hash_cache(cache: dict):
    _atomic_write_json(HASH_CACHE_FILE, cache)


# ============================================================
# 中文处理
# ============================================================


def _init_jieba():
    global _jieba_loaded
    if _jieba_loaded:
        return
    try:
        for word, freq, tag in JIEBA_CUSTOM_WORDS:
            jieba.add_word(word, freq=freq, tag=tag)
        _jieba_loaded = True
    except ImportError:
        pass


def _tokenize_text(text: str) -> str:
    """用 jieba 对文本分词，返回空格分隔的词串（供 FTS 索引）"""
    if not _jieba_loaded:
        _init_jieba()
    try:
        words = jieba.cut(text, cut_all=False)
        return " ".join(w.strip() for w in words if len(w.strip()) >= 1)
    except ImportError:
        return text


def normalize_chinese(text: str) -> str:
    """中文文本规范化：全角→半角，去除多余空白"""
    text = re.sub(r'[！-～]', lambda m: chr(ord(m.group()) - 0xfee0), text)
    text = text.replace('　', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def extract_keywords(text: str) -> list[str]:
    """jieba 中文分词，提取 >=2 字的词，过滤停用词"""
    if not _jieba_loaded:
        _init_jieba()
    try:
        text = normalize_chinese(text)
        words = jieba.cut(text, cut_all=False)
        return [w.strip() for w in words if len(w.strip()) >= 2 and w.strip() not in CHINESE_STOPWORDS]
    except ImportError:
        return []


def expand_query(query: str) -> str:
    """查询扩展：同义词映射 + jieba 分词扩展（BFS 两轮扩散）"""
    query = normalize_chinese(query)
    words = extract_keywords(query)
    if not words:
        words = re.findall(r'[一-鿿]{2,}', query)
    expanded = set(words)
    # BFS 两轮同义词扩散：第一轮扩展原始词，第二轮扩展新加入的词
    frontier = set(words)
    for _ in range(2):
        next_frontier = set()
        for word in frontier:
            if word in SYNONYM_MAP:
                synonyms = set(SYNONYM_MAP[word].split())
                next_frontier |= synonyms - expanded
        expanded |= next_frontier
        frontier = next_frontier
    expanded.add(query)
    return " ".join(expanded)


def _bm25_score(query_keywords: list[str], doc_text: str, doc_keywords: str) -> float:
    """BM25 风格关键词评分"""
    if not query_keywords:
        return 0.0
    doc_lower = doc_text.lower()
    doc_kw_set = set(doc_keywords.split(",")) if doc_keywords else set()
    score = 0.0
    for kw in query_keywords:
        kw_lower = kw.lower()
        pos = doc_lower.find(kw_lower)
        if pos >= 0:
            tf_weight = min(len(kw) * 0.12, 0.6)
            if pos < 200:
                tf_weight *= 1.3
            score += tf_weight
        if kw in doc_kw_set:
            score += 0.4
        for dk in doc_kw_set:
            if kw in dk or dk in kw:
                score += 0.15
                break
    return score


# ============================================================
# 资源检测与模型选择
# ============================================================


def detect_resources():
    """检测系统资源"""
    global _resources
    if _resources is not None:
        return _resources

    info = {"ram_mb": 0, "vram_mb": 0, "cpu_cores": 1, "gpu_name": None, "has_onnx": False}

    try:
        import psutil
        info["ram_mb"] = psutil.virtual_memory().available // (1024 * 1024)
    except ImportError:
        try:
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(stat)
                if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    info["ram_mb"] = stat.ullAvailPhys // (1024 * 1024)
                else:
                    info["ram_mb"] = 2048
            else:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemAvailable:"):
                            info["ram_mb"] = int(line.split()[1]) // 1024
                            break
        except Exception:
            info["ram_mb"] = 2048

    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            info["vram_mb"] = getattr(props, 'total_memory', getattr(props, 'total_mem', 0)) // (1024 * 1024)
            info["gpu_name"] = torch.cuda.get_device_name(0)
    except (ImportError, Exception):
        pass

    info["cpu_cores"] = os.cpu_count() or 1

    try:
        import onnxruntime
        info["has_onnx"] = True
    except ImportError:
        pass

    _resources = info
    return info


def auto_select_model():
    """根据系统资源自动选择最优模型"""
    if EMBEDDING_MODEL_OVERRIDE:
        return EMBEDDING_MODEL_OVERRIDE, EMBEDDING_BATCH_SIZE, False

    res = detect_resources()
    available_mb = max(res["ram_mb"], res["vram_mb"])

    for model_name, min_ram, min_vram, dim, desc in MODEL_TIERS:
        if available_mb >= min_ram:
            if res["vram_mb"] > 0 and res["vram_mb"] < min_vram:
                continue
            batch_size = 64 if res["cpu_cores"] >= 8 else (32 if res["cpu_cores"] >= 4 else 16)
            if available_mb < 1000:
                batch_size = min(batch_size, 16)
            use_onnx = res["has_onnx"] and res["vram_mb"] == 0 and model_name is not None
            return model_name, batch_size, use_onnx

    return None, 16, False


# ============================================================
# LanceDB 操作
# ============================================================


def make_schema(dim: int):
    import pyarrow as pa
    return pa.schema([
        pa.field("id",              pa.string()),
        pa.field("text",            pa.string()),
        pa.field("text_tokenized",  pa.string()),
        pa.field("vector",          pa.list_(pa.float32(), list_size=dim)),
        pa.field("source",          pa.string()),
        pa.field("section",         pa.string()),
        pa.field("type",            pa.string()),
        pa.field("character_id",    pa.string()),
        pa.field("character_name",  pa.string()),
        pa.field("keywords",        pa.string()),
        pa.field("weight",          pa.float32()),
    ])


def get_db():
    global _db
    if _db is None:
        import lancedb
        _db = lancedb.connect(DB_PATH)
    return _db


class IndexNotReadyError(RuntimeError):
    """向量表缺失/为空：知识库检索根本不可用。

    历史事故：单表化重构后未重建索引，_query_main_table 把 open_table 的异常
    吞掉并 return []，导致「写作前必须查询知识库」这条强制流程长期零命中且无报错，
    第 010 章的宗门名错误因此未被拦住。查询类命令今后一律显式失败。
    """


def _rebuild_cmd() -> str:
    return f"python {Path(__file__).resolve()} index --force"


def _open_main_table(db=None, required: bool = True):
    """打开单表 novel_main，并区分「索引不存在」与「查无结果」。

    required=True  → 表不存在或为空时抛 IndexNotReadyError（查询类命令必须响亮失败，
                     否则调用方会把工具故障误读成设定缺失）
    required=False → 返回 None，供 verify / collections / stats 这类诊断命令自行提示
    """
    if db is None:
        db = get_db()
    try:
        exists = MAIN_TABLE in _get_table_names(db)
    except Exception as e:
        if required:
            raise IndexNotReadyError(f"无法读取 LanceDB 表清单（{DB_PATH}）: {e}")
        return None
    if not exists:
        if required:
            raise IndexNotReadyError(
                f"向量表 {MAIN_TABLE} 不存在，知识库尚未建立索引 → {_rebuild_cmd()}"
            )
        return None
    try:
        table = db.open_table(MAIN_TABLE)
        count = table.count_rows()
    except Exception as e:
        if required:
            raise IndexNotReadyError(f"无法读取向量表 {MAIN_TABLE}: {e}")
        return None
    if count == 0:
        if required:
            raise IndexNotReadyError(
                f"向量表 {MAIN_TABLE} 为空（0 个块），知识库检索不可用 → {_rebuild_cmd()}"
            )
        return None
    return table


def _chunk_to_row(chunk: dict, vector: list) -> dict:
    """将 chunk dict + vector 转为 LanceDB 行"""
    meta = chunk.get("metadata", {})
    text = chunk["text"]
    return {
        "id":              chunk["id"],
        "text":            text,
        "text_tokenized":  _tokenize_text(text),
        "vector":          vector,
        "source":          meta.get("source", ""),
        "section":         meta.get("section", ""),
        "type":            meta.get("type", ""),
        "character_id":    meta.get("character_id", ""),
        "character_name":  meta.get("character_name", ""),
        "keywords":        meta.get("keywords", ""),
        "weight":          float(meta.get("weight", 1.0)),
    }


def _get_table_names(db) -> list:
    """获取所有表名（兼容 LanceDB API 变更）"""
    resp = db.list_tables()
    return resp.tables if hasattr(resp, 'tables') else list(resp)


# ============================================================
# 分块策略
# ============================================================

_split_model_cache = None


def _semantic_split_section(text: str, emb_fn=None) -> list[str]:
    global _split_model_cache
    sentences = re.split(r'(?<=[。！？\n])', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 1:
        return [text]
    try:
        if emb_fn is None:
            from sentence_transformers import SentenceTransformer
            if _split_model_cache is None:
                model_name, _, _ = auto_select_model()
                _split_model_cache = SentenceTransformer(model_name or "all-MiniLM-L6-v2")
            model = _split_model_cache
        else:
            model = None
        if model is not None:
            embeddings = model.encode(sentences, batch_size=EMBEDDING_BATCH_SIZE)
        else:
            embeddings = emb_fn.embed_documents(sentences)
        embeddings = np.array(embeddings, dtype=np.float32)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        np.clip(norms, a_min=1e-9, a_max=None, out=norms)
        embeddings = embeddings / norms
        splits = [0]
        for i in range(len(sentences) - 1):
            sim = float(np.dot(embeddings[i], embeddings[i + 1]))
            if sim < SEMANTIC_SPLIT_THRESHOLD:
                splits.append(i + 1)
        splits.append(len(sentences))
        chunks = []
        for j in range(len(splits) - 1):
            chunk = "".join(sentences[splits[j]:splits[j + 1]])
            if chunk.strip():
                chunks.append(chunk.strip())
        return chunks if chunks else [text]
    except (ImportError, RuntimeError, OSError) as e:
        # 模型加载失败或内存不足时回退到固定窗口分块
        chunks = []
        step = 500
        for i in range(0, len(text), step - CHUNK_OVERLAP_CHARS):
            chunk = text[i:i + step]
            if chunk.strip():
                chunks.append(chunk.strip())
        return chunks if chunks else [text]


def _flatten_keywords(val) -> list[str]:
    """递归展平关键词，支持嵌套的 dict/list/str 结构"""
    if val is None or val == "null":
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, (int, float, bool)):
        return [str(val)]

    result = []
    stack = [val]
    while stack:
        item = stack.pop()
        if item is None or item == "null":
            continue
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, (int, float, bool)):
            result.append(str(item))
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
    return result


def _format_char_field(key: str, val) -> str:
    """格式化角色字段为文本"""
    if isinstance(val, dict):
        inner = ", ".join(f"{k}: {v}" for k, v in val.items() if v and v != "null")
        return f"{key}: {inner}" if inner else ""
    elif isinstance(val, list):
        flat = _flatten_keywords(val)
        return f"{key}: {', '.join(str(v) for v in flat)}" if flat else ""
    else:
        return f"{key}: {val}" if val and val != "null" else ""


def _extract_aliases_from_note(text: str) -> list[str]:
    aliases = []
    for m in re.finditer(r'(?:化名|别名)[：:]\s*(\S+)', text):
        aliases.append(m.group(1))
    return aliases


def _split_long_text(text: str, max_len: int = CHUNK_SIZE_MAX) -> list[str]:
    """按段落/句子拆分超长文本"""
    if len(text) <= max_len:
        return [text]
    paragraphs = text.split("\n\n")
    if len(paragraphs) > 1:
        chunks, buf = [], ""
        for p in paragraphs:
            if len(buf) + len(p) > max_len and buf:
                chunks.append(buf.strip())
                buf = p
            else:
                buf = f"{buf}\n\n{p}" if buf else p
        if buf.strip():
            chunks.append(buf.strip())
        return chunks if chunks else [text]
    sentences = re.split(r'(?<=[。！？\n])', text)
    chunks, buf = [], ""
    for s in sentences:
        if len(buf) + len(s) > max_len and buf:
            chunks.append(buf.strip())
            buf = s
        else:
            buf += s
    if buf.strip():
        chunks.append(buf.strip())
    return chunks if chunks else [text]


def chunk_yaml_characters(filepath: Path) -> list[dict]:
    """角色分块：大角色按字段拆分，小角色合并"""
    try:
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        print(f"  ⚠ YAML解析失败: {filepath.name}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    chunks = []
    source = str(filepath.relative_to(PROJECT_ROOT))

    core_keys = ["姓名", "别名", "称呼", "身份", "宗门", "境界", "年龄", "与主角关系", "首次出场", "状态"]
    split_keys = ["背景", "备注", "技能"]

    for char in data.get("角色", []):
        cid = char.get("编号", "unknown")
        name = char.get("姓名", "未知")
        keywords = [name]

        备注_val = char.get("备注")
        备注_text = str(备注_val) if 备注_val else ""
        extra_aliases = _extract_aliases_from_note(备注_text)
        keywords.extend(extra_aliases)

        别名_val = char.get("别名")
        if 别名_val:
            keywords.extend(_flatten_keywords(别名_val))
        if char.get("身份"):
            keywords.extend(char["身份"].split("，"))
        kw_str = ",".join(set(keywords))

        meta_base = {"source": source, "type": "character",
                     "character_id": cid, "character_name": name, "keywords": kw_str}

        core_parts = []
        if extra_aliases:
            core_parts.append(f"化名: {', '.join(extra_aliases)}")
        for key in core_keys + ["动机", "性格", "外貌"]:
            line = _format_char_field(key, char.get(key))
            if line:
                core_parts.append(line)
        core_text = "\n".join(core_parts)
        chunks.append({
            "id": f"char_{cid}_core",
            "text": core_text,
            "metadata": {**meta_base, "section": f"角色_{name}"},
        })

        for key in split_keys:
            val = char.get(key)
            if not val or val == "null":
                continue
            text = _format_char_field(key, val)
            if not text:
                continue
            for i, sub_text in enumerate(_split_long_text(text)):
                suffix = f"_{key}_{i}" if i > 0 else f"_{key}"
                chunks.append({
                    "id": f"char_{cid}{suffix}",
                    "text": f"[{name}] {sub_text}",
                    "metadata": {**meta_base, "section": f"角色_{name}_{key}"},
                })

    return chunks


def chunk_yaml_world(filepath: Path) -> list[dict]:
    """世界设定分块：动态遍历所有顶级 key，自动分块"""
    try:
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        print(f"  ⚠ YAML解析失败: {filepath.name}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    chunks = []
    source = str(filepath.relative_to(PROJECT_ROOT))

    def _extract_keywords_from_obj(obj) -> list[str]:
        kws = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and len(v) < 30:
                    kws.append(v)
                elif isinstance(v, str):
                    kws.extend(extract_keywords(v))
        return kws

    def _add_chunk(chunk_id, section, obj, extra_keywords=None):
        if isinstance(obj, (dict, list)):
            text = yaml.dump(obj, allow_unicode=True, default_flow_style=False)
        else:
            text = str(obj)
        kws = _extract_keywords_from_obj(obj) if isinstance(obj, dict) else []
        if extra_keywords:
            kws.extend(extra_keywords)
        meta = {"source": source, "section": section, "type": "world"}
        if kws:
            meta["keywords"] = ",".join(set(kws))
        if len(text) > CHUNK_SIZE_MAX:
            for i, sub in enumerate(_split_long_text(text)):
                suffix = f"_{i}" if i > 0 else ""
                chunks.append({"id": f"{chunk_id}{suffix}", "text": sub, "metadata": meta})
        else:
            chunks.append({"id": chunk_id, "text": text, "metadata": meta})

    def _process_dict(chunk_id_prefix, section_prefix, d, parent_keywords=None):
        for key, value in d.items():
            kws = parent_keywords or []
            if isinstance(value, list):
                for i, item in enumerate(value):
                    name = None
                    if isinstance(item, dict):
                        name = item.get("境界") or item.get("姓名") or item.get("名称") or item.get("统治者")
                    if name:
                        _add_chunk(f"{chunk_id_prefix}_{key}_{name}", f"{section_prefix}_{key}_{name}",
                                   item, kws + [key, str(name)])
                    else:
                        _add_chunk(f"{chunk_id_prefix}_{key}_{i}", f"{section_prefix}_{key}_{i}",
                                   item, kws + [key])
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, list) and len(sub_value) > 0:
                        for i, item in enumerate(sub_value):
                            item_name = None
                            if isinstance(item, dict):
                                item_name = item.get("境界") or item.get("姓名") or item.get("名称")
                            cid = f"{chunk_id_prefix}_{key}_{sub_key}_{item_name or i}"
                            sec = f"{section_prefix}_{key}_{sub_key}_{item_name or i}"
                            _add_chunk(cid, sec, item, kws + [key, sub_key])
                    else:
                        _add_chunk(f"{chunk_id_prefix}_{key}_{sub_key}",
                                   f"{section_prefix}_{key}_{sub_key}", sub_value, kws + [key, sub_key])
            else:
                _add_chunk(f"{chunk_id_prefix}_{key}", f"{section_prefix}_{key}",
                           {key: value}, kws + [key])

    for top_key, top_value in data.items():
        if isinstance(top_value, dict):
            _process_dict(top_key, top_key, top_value, [top_key])
        elif isinstance(top_value, list):
            for i, item in enumerate(top_value):
                name = None
                if isinstance(item, dict):
                    name = item.get("境界") or item.get("姓名") or item.get("名称")
                cid = f"{top_key}_{name or i}"
                sec = f"{top_key}_{name or i}"
                _add_chunk(cid, sec, item, [top_key])
        else:
            _add_chunk(top_key, top_key, {top_key: top_value}, [top_key])

    return chunks


def chunk_yaml_flat(filepath: Path) -> list[dict]:
    try:
        text = filepath.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (yaml.YAMLError, OSError) as e:
        print(f"  ⚠ YAML解析失败: {filepath.name}: {e}")
        return []
    if not isinstance(data, dict):
        return [{"id": f"flat_{filepath.stem}", "text": text,
                 "metadata": {"source": str(filepath.relative_to(PROJECT_ROOT)), "section": filepath.stem, "type": "workspace"}}]
    chunks = []
    basic_keys = ["最后更新", "姓名", "别名", "年龄", "出身", "外貌", "动机"]
    realm_keys = ["真实境界", "伪装境界", "体质"]
    identity_keys = ["位置", "身份", "名声", "暴露风险"]
    groups = [("基本信息", basic_keys), ("修为状态", realm_keys), ("当前身份", identity_keys)]
    for group_name, group_keys in groups:
        group_data = {k: data[k] for k in group_keys if k in data}
        if group_data:
            chunk_text = yaml.dump(group_data, allow_unicode=True, default_flow_style=False)
            keywords = []
            for val in group_data.values():
                if isinstance(val, list):
                    keywords.extend(str(v) for v in val)
                elif isinstance(val, str):
                    keywords.append(val)
                    keywords.extend(extract_keywords(val))
            keywords.extend(extract_keywords(text))
            chunks.append({"id": f"flat_{filepath.stem}_{group_name}", "text": chunk_text,
                           "metadata": {"source": str(filepath.relative_to(PROJECT_ROOT)),
                                        "section": f"{filepath.stem}_{group_name}", "type": "workspace",
                                        "keywords": ",".join(set(keywords))}})
    processed_keys = set(basic_keys + realm_keys + identity_keys)
    for key, val in data.items():
        if key in processed_keys:
            continue
        chunk_text = yaml.dump({key: val}, allow_unicode=True, default_flow_style=False)
        keywords = extract_keywords(chunk_text)
        if isinstance(val, str):
            keywords.extend(extract_keywords(val))
        chunks.append({"id": f"flat_{filepath.stem}_{key}", "text": chunk_text,
                        "metadata": {"source": str(filepath.relative_to(PROJECT_ROOT)),
                                     "section": f"{filepath.stem}_{key}", "type": "workspace",
                                     "keywords": ",".join(set(keywords))}})
    return chunks


def _align_overlap(text: str, max_chars: int) -> str:
    """按句子边界对齐 overlap，返回可用于下一块开头的尾部文本"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    # 从尾部找第一个句子边界，返回边界后的文本
    for i, ch in enumerate(tail):
        if ch in "。！？\n":
            return tail[i + 1:]
    return tail


def chunk_markdown_sections(filepath: Path, emb_fn=None) -> list[dict]:
    """Markdown 分块：标题拆分 + 关键词提取 + 语义对齐 overlap"""
    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  ⚠ 文件读取失败: {filepath.name}: {e}")
        return []
    lines = text.split("\n")
    raw_sections = []
    current_title = filepath.stem
    current_lines = []
    for line in lines:
        if re.match(r"^#{1,3}\s+", line):
            if current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    raw_sections.append((current_title, content))
            current_title = re.sub(r"^#{1,3}\s+", "", line).strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            raw_sections.append((current_title, content))

    chunks = []
    section_idx = 0
    # 单表化后 INDEX_CONFIG 的 key 是 sanitize 表名，stem 反查不到嵌套文件（会全部落 unknown）；
    # 统一改按正斜杠相对路径匹配（与 _build_index_config 的 file 字段一致）
    rel_key = str(filepath.relative_to(PROJECT_ROOT)).replace("\\", "/")
    file_type = next(
        (cfg.get("type", "unknown") for cfg in INDEX_CONFIG.values() if cfg.get("file") == rel_key),
        "unknown",
    )
    source_rel = str(filepath.relative_to(PROJECT_ROOT))
    prev_tail = ""

    for title, content in raw_sections:
        if len(content) > SEMANTIC_CHUNK_MAX:
            sub_chunks = _semantic_split_section(content, emb_fn)
        else:
            sub_chunks = [content]

        for sub_content in sub_chunks:
            if prev_tail and CHUNK_OVERLAP_CHARS > 0:
                display_text = f"{prev_tail}\n{sub_content}"
            else:
                display_text = sub_content

            kws = extract_keywords(title) + extract_keywords(sub_content[:500])

            chunks.append({
                "id": f"md_{filepath.stem}_{section_idx}",
                "text": display_text,
                "metadata": {"source": source_rel, "section": title, "type": file_type,
                             "keywords": ",".join(set(kws))},
            })
            prev_tail = _align_overlap(sub_content, CHUNK_OVERLAP_CHARS)
            section_idx += 1

    return chunks


CHUNK_FUNCTIONS = {
    "yaml_characters": chunk_yaml_characters,
    "yaml_world": chunk_yaml_world,
    "yaml_flat": chunk_yaml_flat,
    "markdown_sections": chunk_markdown_sections,
}


# ============================================================
# Embedding / Reranker
# ============================================================


def _try_load_single_model(model_name: str, batch_size: int = 64, use_onnx: bool = False):
    try:
        if use_onnx:
            try:
                from optimum.onnxruntime import ORTModelForFeatureExtraction
                from transformers import AutoTokenizer
                tokenizer = AutoTokenizer.from_pretrained(model_name)
                model = ORTModelForFeatureExtraction.from_pretrained(model_name, export=True)

                class ONNXEmbeddingFn:
                    def _encode(self, texts):
                        inputs = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="np")
                        outputs = model(**inputs)
                        attention_mask = inputs["attention_mask"]
                        token_embs = outputs.last_hidden_state
                        mask_expanded = np.expand_dims(attention_mask, -1).astype(float)
                        sum_embs = np.sum(token_embs * mask_expanded, axis=1)
                        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
                        return (sum_embs / sum_mask).tolist()

                    def embed_query(self, texts):
                        if isinstance(texts, list):
                            return self._encode([f"{EMBEDDING_QUERY_PREFIX}{t}" for t in texts])
                        return self._encode([f"{EMBEDDING_QUERY_PREFIX}{texts}"])[0]

                    def embed_documents(self, texts):
                        return self._encode(texts)

                    def name(self):
                        return f"{model_name} (ONNX)"

                print(f"  [embedding] ONNX 加速: {model_name}")
                return ONNXEmbeddingFn()
            except Exception as e:
                print(f"  [embedding] ONNX 加载失败，回退 PyTorch: {e}")

        from sentence_transformers import SentenceTransformer

        model_short = model_name.split("/")[-1]
        local_path = LOCAL_MODEL_DIR / model_short

        model_file = local_path / "model.safetensors"
        if not local_path.exists() or not model_file.exists():
            raise FileNotFoundError(f"本地模型不存在: {local_path}")
        # 完整性校验：模型文件过小时视为下载不完整/损坏（O12）
        if model_file.stat().st_size < 1024 * 1024:
            raise FileNotFoundError(f"模型文件疑似未下载完整或已损坏（体积过小）: {model_file}")

        import torch
        device = "cpu"
        model = SentenceTransformer(str(local_path), device=device)

        class BGEEmbeddingFn:
            def embed_query(self, texts):
                if isinstance(texts, list):
                    prefixed = [f"{EMBEDDING_QUERY_PREFIX}{text}" for text in texts]
                    return model.encode(prefixed, batch_size=batch_size)
                return model.encode([f"{EMBEDDING_QUERY_PREFIX}{texts}"])[0]

            def embed_documents(self, texts):
                return model.encode(texts, batch_size=batch_size)

            def name(self):
                return f"{model_name} (CPU)"

        return BGEEmbeddingFn()
    except Exception as e:
        print(f"  [embedding] {model_name} 加载失败: {e}")
        return None


def get_embedding_fn():
    global _embedding_fn, _embedding_model_name
    if _embedding_fn is not None:
        return _embedding_fn

    model_name, batch_size, use_onnx = auto_select_model()
    res = detect_resources()
    if model_name:
        print(f"  [资源] 内存:{res['ram_mb']}MB GPU:{res['gpu_name'] or '无'} CPU:{res['cpu_cores']}核 ONNX:{'可用' if res['has_onnx'] else '不可用'}")
        print(f"  [选择] {model_name}, batch={batch_size}, ONNX={'是' if use_onnx else '否'}")

    # 优先使用 sentence-transformers（batch 优化，速度更快）
    result = _try_load_single_model(model_name or "BAAI/bge-small-zh-v1.5", batch_size, use_onnx=False)
    if result is not None:
        _embedding_fn = result
        _embedding_model_name = "sentence-transformers"
        return _embedding_fn

    # 回退到 numpy 手动实现（无 torch 兜底路径）
    print("  [embedding] sentence-transformers 加载失败，回退到 numpy 实现")
    try:
        _embedding_fn = _load_numpy_bert(model_name or "BAAI/bge-small-zh-v1.5")
        _embedding_model_name = "numpy"
        return _embedding_fn
    except Exception as e:
        print(f"  [embedding] numpy BERT 推理加载失败: {e}")
        raise RuntimeError("无法加载任何 embedding 模型，请检查模型缓存")




def _load_numpy_bert(model_name: str):
    """无 torch 兜底：从 safetensors + tokenizer.json 手写 BERT 推理（O4 抽取；O12 含完整性校验）"""
    from safetensors import safe_open
    from tokenizers import Tokenizer

    model_name = model_name or "BAAI/bge-small-zh-v1.5"
    model_short = model_name.split("/")[-1]
    snapshot_path = str(LOCAL_MODEL_DIR / model_short)

    # 检查模型文件是否存在
    model_file = os.path.join(snapshot_path, "model.safetensors")
    tokenizer_path = os.path.join(snapshot_path, "tokenizer.json")

    if not os.path.exists(model_file):
        raise FileNotFoundError(f"模型文件不存在: {model_file}")
    if not os.path.exists(tokenizer_path):
        raise FileNotFoundError(f"Tokenizer 文件不存在: {tokenizer_path}")

    tokenizer = Tokenizer.from_file(tokenizer_path)

    with safe_open(model_file, framework="numpy") as f:
        weights = {}
        for key in f.keys():
            weights[key] = f.get_tensor(key)

    encoder_keys = [k for k in weights.keys() if k.startswith("encoder.layer.")]
    layer_indices = set()
    for key in encoder_keys:
        parts = key.split(".")
        if len(parts) > 2 and parts[2].isdigit():
            layer_indices.add(int(parts[2]))
    num_layers = len(layer_indices)

    def gelu(x):
        return 0.5 * x * (1 + np.tanh(np.sqrt(2 / np.pi) * (x + 0.044715 * x**3)))

    def layer_norm(x, weight, bias, eps=1e-12):
        mean = np.mean(x, axis=-1, keepdims=True)
        std = np.std(x, axis=-1, keepdims=True)
        return (x - mean) / (std + eps) * weight + bias

    def linear(x, weight, bias=None):
        result = np.dot(x, weight.T)
        if bias is not None:
            result = result + bias
        return result

    def attention(query, key, value, attention_mask=None):
        d_k = query.shape[-1]
        scores = np.matmul(query, key.transpose(0, 1, 3, 2)) / np.sqrt(d_k)
        if attention_mask is not None:
            attention_mask = attention_mask[:, np.newaxis, np.newaxis, :]
            scores = scores + (1 - attention_mask) * -1e9
        scores_max = np.max(scores, axis=-1, keepdims=True)
        exp_scores = np.exp(scores - scores_max)
        attention_probs = exp_scores / np.sum(exp_scores, axis=-1, keepdims=True)
        return np.matmul(attention_probs, value)

    def transformer_layer(hidden_states, attention_mask, weights, layer_idx):
        prefix = f"encoder.layer.{layer_idx}."

        query = linear(hidden_states, weights[prefix + "attention.self.query.weight"], weights[prefix + "attention.self.query.bias"])
        key = linear(hidden_states, weights[prefix + "attention.self.key.weight"], weights[prefix + "attention.self.key.bias"])
        value = linear(hidden_states, weights[prefix + "attention.self.value.weight"], weights[prefix + "attention.self.value.bias"])

        batch_size = query.shape[0]
        seq_len = query.shape[1]
        num_heads = 8
        head_dim = query.shape[-1] // num_heads

        query = query.reshape(batch_size, seq_len, num_heads, head_dim).transpose(0, 2, 1, 3)
        key = key.reshape(batch_size, seq_len, num_heads, head_dim).transpose(0, 2, 1, 3)
        value = value.reshape(batch_size, seq_len, num_heads, head_dim).transpose(0, 2, 1, 3)

        attention_output = attention(query, key, value, attention_mask)
        attention_output = attention_output.transpose(0, 2, 1, 3).reshape(batch_size, seq_len, -1)

        attention_output = linear(attention_output, weights[prefix + "attention.output.dense.weight"], weights[prefix + "attention.output.dense.bias"])
        hidden_states = layer_norm(hidden_states + attention_output,
                                   weights[prefix + "attention.output.LayerNorm.weight"],
                                   weights[prefix + "attention.output.LayerNorm.bias"])

        intermediate = linear(hidden_states, weights[prefix + "intermediate.dense.weight"], weights[prefix + "intermediate.dense.bias"])
        intermediate = gelu(intermediate)

        output = linear(intermediate, weights[prefix + "output.dense.weight"], weights[prefix + "output.dense.bias"])
        hidden_states = layer_norm(hidden_states + output,
                                   weights[prefix + "output.LayerNorm.weight"],
                                   weights[prefix + "output.LayerNorm.bias"])

        return hidden_states

    def bert_encode(input_ids, attention_mask):
        max_len = 512
        if input_ids.shape[1] > max_len:
            input_ids = input_ids[:, :max_len]
            attention_mask = attention_mask[:, :max_len]

        word_embeddings = weights["embeddings.word_embeddings.weight"]
        position_embeddings = weights["embeddings.position_embeddings.weight"]
        token_type_embeddings = weights["embeddings.token_type_embeddings.weight"]

        word_embeds = word_embeddings[input_ids]
        position_ids = np.arange(input_ids.shape[1])
        position_embeds = position_embeddings[position_ids]
        token_type_ids = np.zeros_like(input_ids)
        token_type_embeds = token_type_embeddings[token_type_ids]

        hidden_states = word_embeds + position_embeds + token_type_embeds
        hidden_states = layer_norm(hidden_states,
                                   weights["embeddings.LayerNorm.weight"],
                                   weights["embeddings.LayerNorm.bias"])

        for i in range(num_layers):
            hidden_states = transformer_layer(hidden_states, attention_mask, weights, i)

        input_mask_expanded = np.expand_dims(attention_mask, -1).astype(float)
        sum_embs = np.sum(hidden_states * input_mask_expanded, axis=1)
        sum_mask = np.clip(np.sum(input_mask_expanded, axis=1), a_min=1e-9, a_max=None)
        pooled = sum_embs / sum_mask

        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1
        pooled = pooled / norms

        return pooled.tolist()

    class NumpyEmbeddingFn:
        def embed_query(self, texts):
            if isinstance(texts, list):
                query_texts = [f"{EMBEDDING_QUERY_PREFIX}{t}" for t in texts]
            else:
                query_texts = [f"{EMBEDDING_QUERY_PREFIX}{texts}"]
            results = []
            for text in query_texts:
                encoded = tokenizer.encode(text)
                input_ids = np.array([encoded.ids])
                attention_mask = np.array([encoded.attention_mask])
                result = bert_encode(input_ids, attention_mask)
                results.extend(result)
            return results if len(results) > 1 else results[0]

        def embed_documents(self, texts):
            results = []
            for text in texts:
                encoded = tokenizer.encode(text)
                input_ids = np.array([encoded.ids])
                attention_mask = np.array([encoded.attention_mask])
                result = bert_encode(input_ids, attention_mask)
                results.extend(result)
            return results

        def name(self):
            return f"{model_name} (numpy)"

    _embedding_fn = NumpyEmbeddingFn()
    _embedding_model_name = "numpy"
    print(f"  [embedding] 使用 numpy BERT 推理: {model_name}")
    return _embedding_fn




def get_reranker():
    global _reranker
    if _reranker is not None:
        return _reranker
    if not RERANKER_ENABLED:
        return None
    try:
        from sentence_transformers import CrossEncoder
        reranker_short = RERANKER_MODEL.split("/")[-1]
        local_reranker = LOCAL_MODEL_DIR / reranker_short
        if local_reranker.exists() and (local_reranker / "model.safetensors").exists():
            _reranker = CrossEncoder(str(local_reranker), max_length=512)
            print(f"  [reranker] 使用本地模型: {local_reranker}")
        else:
            _reranker = CrossEncoder(RERANKER_MODEL, max_length=512)
            print(f"  [reranker] 使用 {RERANKER_MODEL}")
        return _reranker
    except Exception as e:
        print(f"  [reranker] 加载失败，跳过重排序: {e}")
        return None


def preload_models():
    """预加载 embedding + reranker"""
    get_embedding_fn()
    get_reranker()


# ============================================================
# 语义缓存
# ============================================================


class SemanticQueryCache:
    def __init__(self):
        self._entries = []
        self._query_index = {}  # 精确匹配快速路径
        self._meta = {"model": EMBEDDING_MODEL_OVERRIDE}  # 模型指纹，变更即失效
        self._load()

    def _load(self):
        if SEMANTIC_CACHE_FILE.exists():
            try:
                raw = json.loads(SEMANTIC_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "entries" in raw:
                    self._entries = raw.get("entries", []) or []
                    self._meta = raw.get("_meta", {}) or {}
                elif isinstance(raw, list):
                    # 兼容旧版纯列表格式
                    self._entries = raw
                    self._meta = {}
                else:
                    self._entries = []
                    self._meta = {}
                # 嵌入模型变更 → 缓存整体失效（避免陈旧向量/结果）
                if self._meta.get("model") != EMBEDDING_MODEL_OVERRIDE:
                    self._entries = []
                    self._meta = {"model": EMBEDDING_MODEL_OVERRIDE}
                # 构建索引
                self._rebuild_index()
            except (json.JSONDecodeError, OSError):
                self._entries = []
                self._meta = {"model": EMBEDDING_MODEL_OVERRIDE}

    def _rebuild_index(self):
        """重建精确匹配索引"""
        self._query_index = {}
        for entry in self._entries:
            query = entry.get("query")
            if query:
                self._query_index[query] = entry

    def _save(self):
        try:
            _atomic_write_json(SEMANTIC_CACHE_FILE, {"_meta": self._meta, "entries": self._entries})
        except OSError:
            pass

    def _clean_expired(self):
        now = time.time()
        old_count = len(self._entries)
        self._entries = [e for e in self._entries if now - e.get("timestamp", 0) < SEMANTIC_CACHE_TTL]
        if len(self._entries) != old_count:
            self._rebuild_index()

    def get_by_query(self, query: str):
        """精确匹配查询（O(1) 复杂度）"""
        self._clean_expired()
        entry = self._query_index.get(query)
        if entry:
            return entry.get("result")
        return None

    def get(self, query: str, emb_fn):
        """语义相似度匹配查询"""
        if not self._entries:
            return None
        self._clean_expired()
        if not self._entries:
            return None
        try:
                query_emb = np.array(emb_fn.embed_query(query))
                query_emb = query_emb / (np.linalg.norm(query_emb) or 1)
                best_sim, best_result = 0.0, None
                for entry in self._entries:
                    cached_emb = np.array(entry.get("embedding", []))
                    if len(cached_emb) == 0:
                        continue
                    cached_emb = cached_emb / (np.linalg.norm(cached_emb) or 1)
                    sim = float(np.dot(query_emb, cached_emb))
                    if sim > best_sim:
                        best_sim = sim
                        best_result = entry.get("result")
                if best_sim >= SEMANTIC_CACHE_THRESHOLD and best_result:
                    return best_result
        except Exception:
            pass
        return None

    def put(self, query: str, emb_fn, result):
        """添加缓存条目"""
        try:
            emb = emb_fn.embed_query(query)
            if hasattr(emb, 'tolist'):
                emb = emb.tolist()
        except Exception:
            emb = []
        entry = {"query": query, "embedding": emb, "result": result, "timestamp": time.time()}
        self._entries.append(entry)
        self._query_index[query] = entry
        if len(self._entries) > SEMANTIC_CACHE_MAX:
            # 移除最旧的条目
            removed = self._entries.pop(0)
            removed_query = removed.get("query")
            if removed_query and removed_query in self._query_index:
                del self._query_index[removed_query]
        self._save()


_query_cache = None


def get_query_cache():
    global _query_cache
    if _query_cache is None:
        _query_cache = SemanticQueryCache()
    return _query_cache


# ============================================================
# 索引操作
# ============================================================


def _prepare_chunks(name: str, config: dict, force: bool, hash_cache: dict, emb_fn=None):
    filepath = PROJECT_ROOT / config["file"]
    if not filepath.exists():
        return name, None, None, "missing"
    current_hash = file_hash(filepath)
    if not current_hash:
        return name, None, None, "error"
    if not force and hash_cache.get(config["file"]) == current_hash:
        return name, None, None, "unchanged"
    try:
        chunk_fn = CHUNK_FUNCTIONS[config["chunk_fn"]]
        if config["chunk_fn"] == "markdown_sections" and emb_fn:
            chunks = chunk_fn(filepath, emb_fn=emb_fn)
        else:
            chunks = chunk_fn(filepath)
    except Exception as e:
        print(f"  ⚠ 分块失败: {name}: {e}")
        return name, None, None, "error"
    if not chunks:
        return name, None, None, "empty"
    return name, chunks, current_hash, "ok"


def index_all(force: bool = False):

    if not _acquire_process_lock():
        print("  [进程] 无法获取进程锁，可能有另一个实例在运行")
        return

    try:
        _index_all_impl(force, emb_fn=None)
    finally:
        _release_process_lock()


def _index_all_impl(force: bool = False, emb_fn=None):
    """实际的索引实现（批量 embedding 优化）"""

    if emb_fn is None:
        emb_fn = get_embedding_fn()
    db = get_db()
    hash_cache = load_hash_cache()
    total_chunks = 0

    # 阶段1：并行分块
    prepared = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_prepare_chunks, name, cfg, force, hash_cache, emb_fn): name
                   for name, cfg in INDEX_CONFIG.items()}
        for future in as_completed(futures):
            name, chunks, current_hash, status = future.result()
            if status == "missing":
                print(f"  ⚠ 文件不存在，跳过: {INDEX_CONFIG[name]['file']}")
            elif status == "unchanged":
                print(f"  ✓ {name}: 未变化，跳过")
            elif status == "empty":
                print(f"  ⚠ {name}: 无内容，跳过")
            elif status == "error":
                print(f"  ✗ {name}: 处理失败")
            elif status == "ok" and chunks:
                prepared[name] = (chunks, current_hash)

    if not prepared:
        save_hash_cache(hash_cache)
        print("\n完成，无需更新")
        return

    # 阶段2+3：分批 embedding + 逐批写入（O5 降低全量 embed 内存峰值）
    total = len(prepared)
    # 清理旧的多文件多表结构（version1），仅保留单表，避免旧表残留
    for t in _get_table_names(db):
        if t.startswith(TABLE_PREFIX) and t != MAIN_TABLE:
            try:
                db.drop_table(t)
            except Exception:
                pass
    try:
        db.drop_table(MAIN_TABLE)
    except Exception:
        pass

    table = None
    schema = None
    done = 0
    for name, (chunks, current_hash) in prepared.items():
        done += 1
        texts = [c["text"] for c in chunks]
        if not texts:
            print(f"  [{done}/{total}] - {name}: 无内容，跳过")
            continue
        # 逐集合 embed（单集合规模可控，避免一次性驻留全部向量于内存）
        vectors = emb_fn.embed_documents(texts)
        if hasattr(vectors, 'tolist'):
            vectors = vectors.tolist()
        w = COLLECTION_WEIGHTS.get(name, 1.0)
        rows = []
        for i in range(len(chunks)):
            row = _chunk_to_row(chunks[i], vectors[i])
            row["weight"] = w
            rows.append(row)
        if table is None:
            schema = make_schema(len(rows[0]["vector"]))
            table = db.create_table(MAIN_TABLE, data=rows, schema=schema, mode="overwrite")
        else:
            table.add(rows)
        hash_cache[INDEX_CONFIG[name]["file"]] = current_hash
        total_chunks += len(chunks)
        print(f"  [{done}/{total}] ✓ {name}: 索引 {len(chunks)} 个块")

    if table is not None:
        try:
            table.create_fts_index("text_tokenized", base_tokenizer="whitespace", replace=True)
        except Exception as e:
            print(f"  ⚠ FTS 索引创建失败: {e}")

    save_hash_cache(hash_cache)
    if total_chunks > 0 and SEMANTIC_CACHE_FILE.exists():
        SEMANTIC_CACHE_FILE.unlink()
    print(f"\n完成，共索引 {total_chunks} 个块 → 单表 {MAIN_TABLE}")


def force_reindex():
    if not _acquire_process_lock():
        print("  [进程] 无法获取进程锁，可能有另一个实例在运行")
        return

    try:
        # 先索引（内部使用 mode="overwrite" 覆盖旧表），成功后再清理缓存
        _index_all_impl(force=True)
        if SEMANTIC_CACHE_FILE.exists():
            SEMANTIC_CACHE_FILE.unlink()
            print(f"  ✓ 已删除 语义缓存: {SEMANTIC_CACHE_FILE.name}")
    finally:
        _release_process_lock()


# ============================================================
# 查询操作
# ============================================================


def _query_main_table(query_vec, query_text, query_keywords, filter_type, n_candidates, db=None):
    """查询单表（向量 + 全文检索），支持 type 过滤与按来源加权

    注：原 _query_single_table 的 FTS 分支引用了未传入的 query_keywords，
    导致全文检索每次抛 NameError 被静默吞掉；此处显式传入并修复。
    索引缺失不再返回空列表，改由 _open_main_table 抛 IndexNotReadyError。
    """
    table = _open_main_table(db)
    count = table.count_rows()

    items = []
    seen_ids = set()
    effective_limit = min(n_candidates, count)

    # 白名单验证，防止注入
    where_clause = None
    if filter_type:
        if filter_type not in VALID_FILTER_TYPES:
            print_warning(f"无效的过滤类型: {filter_type}，允许的类型: {', '.join(sorted(VALID_FILTER_TYPES))}")
            return []
        where_clause = f"type = '{filter_type}'"

    try:
        q = table.search(query_vec).metric("cosine").limit(effective_limit)
        if where_clause:
            q = q.where(where_clause)
        df = q.to_pandas()
        for _, row in df.iterrows():
            rid = row.get("id", "")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            weight = float(row.get("weight", 1.0))
            score = (1 - row.get("_distance", 0)) * weight
            items.append({
                "id": rid, "score": score,
                "text": row.get("text", ""),
                "source": row.get("source", ""),
                "section": row.get("section", ""),
                "type": row.get("type", ""),
                "keywords": row.get("keywords", ""),
            })
    except Exception as e:
        print(f"  ⚠ 向量搜索失败: {e}")

    try:
        tokenized_query = _tokenize_text(query_text)
        q = table.search(tokenized_query, query_type="fts").limit(effective_limit)
        if where_clause:
            q = q.where(where_clause)
        df = q.to_pandas()
        for _, row in df.iterrows():
            rid = row.get("id", "")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            matched_count = sum(1 for kw in query_keywords if kw in (row.get("text", "") + row.get("keywords", "")))
            weight = float(row.get("weight", 1.0))
            score = (0.3 + 0.1 * matched_count) * weight
            items.append({
                "id": rid, "score": score,
                "text": row.get("text", ""),
                "source": row.get("source", ""),
                "section": row.get("section", ""),
                "type": row.get("type", ""),
                "keywords": row.get("keywords", ""),
            })
    except Exception as e:
        print(f"  ⚠ FTS搜索失败: {e}")

    return items


def query_knowledge(query: str, top_k: int = 5, filter_type: str = None, min_score: float = 0.0) -> list:
    """查询知识库，返回结构化结果"""

    # 先确认索引就绪：语义缓存命中不应掩盖「表不存在」这一工具故障
    _open_main_table(get_db())

    cache = get_query_cache()
    cached = cache.get_by_query(query)
    if cached:
        return cached

    emb_fn = get_embedding_fn()
    cached = cache.get(query, emb_fn)
    if cached:
        return cached

    query = normalize_chinese(query)
    expanded = expand_query(query)
    query_keywords = set(extract_keywords(expanded))
    if not query_keywords:
        cn_keywords = RE_HANZI.findall(expanded)
        en_keywords = re.findall(r'[a-zA-Z]+', expanded)
        query_keywords = set(cn_keywords + en_keywords)
    query_keywords.add(query)

    query_vec = emb_fn.embed_query(query)
    if hasattr(query_vec, 'tolist'):
        query_vec = query_vec.tolist()

    n_candidates = min(max(RERANKER_TOP_K * 2, 30), 100)
    db = get_db()
    all_results = _query_main_table(query_vec, query, query_keywords, filter_type, n_candidates, db)
    if not all_results:
        return []

    seen_char_sets = set()
    results = []
    for item in all_results:
        doc = item["text"]
        # BM25 分数在去重前累加，确保去重使用包含 BM25 的总分
        item["score"] += _bm25_score(list(query_keywords), doc, item.get("keywords", ""))
        chars = frozenset(RE_HANZI_CHAR.findall(doc))
        is_dup = False
        for seen in seen_char_sets:
            if chars and seen and len(chars & seen) / min(len(chars), len(seen)) > 0.85:
                is_dup = True
                break
        if is_dup:
            continue
        seen_char_sets.add(chars)
        results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)
    candidates = results[:RERANKER_TOP_K]

    reranker = get_reranker()
    if reranker and len(candidates) > top_k:
        try:
            pairs = [(query, r["text"]) for r in candidates]
            rerank_scores = reranker.predict(pairs)
            for i, score in enumerate(rerank_scores):
                candidates[i]["rerank_score"] = float(score)
            max_sem = max((r["score"] for r in candidates), default=1) or 1
            max_rer = max((r["rerank_score"] for r in candidates), default=1) or 1
            for r in candidates:
                r["final_score"] = (r["score"] / max_sem) * 0.4 + (r["rerank_score"] / max_rer) * 0.6
            candidates.sort(key=lambda x: x.get("final_score", x["score"]), reverse=True)
        except Exception as e:
            print(f"  [查询] 重排序失败，使用原始排序: {e}")

    score_key = "final_score" if reranker and len(candidates) > top_k else "score"
    candidates = [r for r in candidates if r.get(score_key, r["score"]) >= min_score][:top_k]

    if not candidates:
        return []

    parsed = []
    for r in candidates:
        score = r.get("final_score", r["score"])
        parsed.append({"section": r["section"], "source": r["source"], "score": round(score, 2), "content": r["text"]})

    cache.put(query, emb_fn, parsed)
    return parsed


def format_results(results: list, mode: str = "brief", max_len: int = 150) -> str:
    """格式化查询结果"""
    if not results:
        return "未找到相关上下文。"
    if mode == "summary":
        return "\n".join(f"【{r['section']}】(相关度:{r['score']:.2f})" for r in results)
    parts = []
    for r in results:
        parts.append(f"【{r['section']}】(相关度:{r['score']:.2f})")
        content = r["content"][:max_len] + "..." if mode == "brief" and len(r["content"]) > max_len else r["content"]
        parts.append(content)
        parts.append("---")
    return "\n".join(parts[:-1])


def show_stats():
    table = _open_main_table(get_db(), required=False)
    print("LanceDB 表统计：\n")
    if table is None:
        print(f"  {MAIN_TABLE}: 不存在或为空 → 请执行 {_rebuild_cmd()}")
        return
    try:
        count = table.count_rows()
        print(f"  {MAIN_TABLE}: {count} 个块（单表）")
        df = table.to_pandas()
        type_counts = df["type"].value_counts()
        for t, c in type_counts.items():
            print(f"    └ {t}: {c}")
    except Exception:
        print(f"  {MAIN_TABLE}: 读取失败")


def verify_index(fix: bool = False):
    db = get_db()
    hash_cache = load_hash_cache()

    model_name, _, _ = auto_select_model()
    current_dim = 0
    for name, _, _, dim, _ in MODEL_TIERS:
        if name == model_name:
            current_dim = dim
            break
    if current_dim == 0 and model_name is None:
        current_dim = 384

    # 主表存在性 / 维度 / 各来源入表情况
    missing, dim_mismatch, outdated, ok = [], [], [], []
    indexed_sources = set()
    table_exists = True
    try:
        table = db.open_table(MAIN_TABLE)
        count = table.count_rows()
        if count == 0:
            missing.append((MAIN_TABLE, "表为空"))
        else:
            if current_dim > 0:
                try:
                    sample = table.head(1).to_pydict()
                    vecs = sample.get("vector", [])
                    if vecs and len(vecs[0]) != current_dim:
                        dim_mismatch.append((MAIN_TABLE, len(vecs[0]), current_dim))
                except Exception:
                    pass
            try:
                df = table.to_pandas()
                if "source" in df.columns:
                    # 分块器在 Windows 上写入反斜杠路径，统一成正斜杠再与 INDEX_CONFIG 比对
                    indexed_sources = {str(s).replace("\\", "/") for s in df["source"].dropna().tolist()}
            except Exception:
                indexed_sources = set()
    except Exception:
        table_exists = False
        missing.append((MAIN_TABLE, "表不存在"))

    if table_exists:
        for name, config in INDEX_CONFIG.items():
            src = config.get("file", "")
            filepath = PROJECT_ROOT / config["file"]
            if not table_exists or src not in indexed_sources:
                outdated.append((name, "未入表/缺失"))
            elif filepath.exists() and hash_cache.get(src) != file_hash(filepath):
                outdated.append((name, "文件已变更"))
            else:
                ok.append((name, count))

    print("索引完整性校验：\n")
    for name, count in ok:
        print(f"  ✓ {name}: 已索引")
    for name, reason in outdated:
        print(f"  ⚠ {name}: {reason}")
    for name, stored_dim, cur_dim in dim_mismatch:
        print(f"  ⚠ {name}: 维度不匹配（索引{stored_dim}维 vs 模型{cur_dim}维），需重建")
    for name, reason in missing:
        print(f"  ✗ {name}: {reason}")

    total_issues = len(missing) + len(outdated) + len(dim_mismatch)
    print(f"\n总计: {len(ok)} 正常, {len(dim_mismatch)} 维度不匹配, {len(outdated)} 过期/缺失, {len(missing)} 缺失表")

    if total_issues > 0 and fix:
        print("\n开始自动修复（重建主表）...")
        index_all(force=True)
    elif total_issues > 0:
        print("\n运行 --fix 自动修复。")


# ============================================================
# 子命令处理函数
# ============================================================


def cmd_query(args):
    config = load_config()

    if args.state:
        state_file = WORKSPACE / "主角状态_精简.yaml"
        print_header("主角状态")
        print(truncate_text(read_file(state_file, '状态'), args.max_len))
        print()

    # 全量查询：无关键词时返回所有表的全部内容
    try:
        if not args.query:
            results = _query_all(args.filter_type, args.top)
            print_header("知识库全量检索")
        else:
            results = query_knowledge(args.query, args.top, args.filter_type, args.min_score)
            print_header(f"知识库检索（{args.query}）")
    except IndexNotReadyError as e:
        print_error(f"知识库检索已中止：{e}")
        print_error("设定文件本身完好，重建索引后即可恢复；不要用「查不到」当作「设定没有」。")
        sys.exit(3)

    # 兼容旧缓存返回字符串的情况
    if isinstance(results, str):
        print(results)
        return

    results.sort(key=lambda x: x["score"], reverse=True)
    output = format_results(results, args.mode, args.max_len)
    print(output)


def _query_all(filter_type: str = None, limit: int = 50) -> list:
    """全量查询：返回单表全部内容（可按 type 过滤）"""
    table = _open_main_table(get_db())
    try:
        df = table.to_pandas()
    except Exception as e:
        print_warning(f"读取主表失败: {e}")
        return []
    all_results = []
    for _, row in df.iterrows():
        all_results.append({
            "section": row.get("section", ""),
            "source": row.get("source", ""),
            "score": 1.0,
            "content": row.get("text", ""),
            "type": row.get("type", ""),
            "table": MAIN_TABLE,
        })
    if filter_type:
        all_results = [r for r in all_results if r.get("type") == filter_type]
    return all_results[:limit]


def _load_validation_rules():
    """加载校验规则（外置于 validation_rules.yaml，代码内置默认值兜底）"""
    default_rules = {
        "state_keywords": ['突破', '修炼', '受伤', '离开', '到达', '获得', '学会', '发现',
                            '消失', '出现', '教导', '训练', '死亡', '失踪'],
        "realm_keywords": ['蜕凡', '铭纹', '凝真', '神藏', '道种', '燃灯', '开天', '掌道',
                            '源初', '无矩', '突破', '晋级', '进阶', '修为提升'],
        "location_groups": {
            '村中': ['村子', '村里', '院子', '灶房', '家门口', '苞谷地', '院门'],
            '镇上': ['镇上', '镇子', '街上', '铺面', '粮铺', '杂货铺'],
            '山上': ['山上', '山梁', '山坳', '山脊', '垭口', '松林'],
            '路上': ['路上', '走了', '往回走', '出门'],
        },
        "known_items": ['养元丹', '疗伤丹', '聚气丹', '影字令牌', '令牌', '芥子坠',
                        '隐息衣', '潜龙诀', '粗布'],
        "hook_keywords": {
            '薄底靴': '灰袍人/供奉步法', '脚跟不着地': '灰袍人/供奉步法',
            '养元丹': '沈渊礼认识养元丹', '别问': '沈渊礼的秘密',
            '供奉': '供奉与灰袍人关联', '灰袍': '灰袍人身份',
        },
    }
    path = WORKSPACE / "validation_rules.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for k, v in data.items():
                if v is not None:
                    default_rules[k] = v
        except Exception as e:
            print_warning(f"读取 validation_rules.yaml 失败: {e}")
    return default_rules




def _check_archive(tracking_path, archive_dir):
    # 归档当前卷：将章节追踪.yaml 写入归档目录并重置主文件为新卷空白模板（O3 抽取）
    tracking_raw = read_file(tracking_path, "章节追踪")
    if "不存在" in tracking_raw or "读取失败" in tracking_raw:
        print(f"{Colors.RED}错误: {tracking_raw}{Colors.RESET}")
        return
    try:
        data = yaml.safe_load(tracking_raw)
    except Exception as e:
        print(f"{Colors.RED}YAML 解析失败: {e}{Colors.RESET}")
        return

    current_vol = data.get("当前卷", "未知卷")
    safe_name = current_vol.replace(" ", "_").replace("/", "_")

    # 创建归档目录
    archive_dir.mkdir(exist_ok=True)
    archive_path = archive_dir / f"章节追踪_{safe_name}.yaml"

    # 写入归档文件（完整数据）
    archive_data = {
        "卷": current_vol,
        "归档时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "时间线": data.get("时间线", []),
        "变量": data.get("变量", {}),
        "人物状态": data.get("人物状态", {}),
        "待回收伏笔": data.get("待回收伏笔", []),
    }
    archive_path.write_text(yaml.dump(archive_data, allow_unicode=True, default_flow_style=False, sort_keys=False), encoding="utf-8")
    print_success(f"已归档: {archive_path.relative_to(PROJECT_REAL_ROOT)}")

    # 生成摘要
    timeline = data.get("时间线", [])
    ch_range = f"{timeline[0].get('章节', '?')}~{timeline[-1].get('章节', '?')}" if timeline else "?"
    summary = {
        "卷": current_vol,
        "章节范围": ch_range,
        "结束金钱": data.get("变量", {}).get("金钱", {}).get("当前", "?"),
        "结束物品": {k: v for k, v in data.get("变量", {}).get("物品", {}).items() if "无" not in str(v)},
        "遗留伏笔": [h.get("内容", "") for h in data.get("待回收伏笔", []) if isinstance(h, dict) and "待解" in str(h.get("状态", ""))],
    }

    # 读取现有归档列表
    existing_archives = data.get("卷归档", [])
    if not isinstance(existing_archives, list):
        existing_archives = []
    existing_archives.append(summary)

    # 重置主文件为新卷空白模板
    new_data = {
        "当前卷": "待填写",
        "最后更新": "待填写",
        "卷归档": existing_archives,
        "时间线": [],
        "变量": {"金钱": {"当前": 0, "变动": []}, "物品": {}, "价格": {}},
        "人物状态": {},
        "待回收伏笔": [h for h in data.get("待回收伏笔", []) if isinstance(h, dict) and "待解" in str(h.get("状态", ""))],
    }
    tracking_path.write_text(yaml.dump(new_data, allow_unicode=True, default_flow_style=False, sort_keys=False), encoding="utf-8")
    print_success(f"已重置主文件，保留 {len(new_data['待回收伏笔'])} 条未解伏笔")
    print(f"\n{Colors.YELLOW}请编辑 {tracking_path.name} 填写新卷信息{Colors.RESET}")
    return


def _check_snapshot(data):
    # 输出章节追踪变量快照：时间线/变量/人物状态/伏笔/卷归档（O3 抽取）
    print_header("章节追踪快照")
    print(f"  最后更新: {data.get('最后更新', '未知')}")

    # 时间线
    timeline = data.get("时间线", [])
    if timeline:
        print(f"\n{Colors.BOLD}时间线:{Colors.RESET}")
        for t in timeline:
            ch = t.get("章节", "?")
            time_str = t.get("时间", "")
            summary = t.get("摘要", "")
            print(f"  {ch}  {time_str}")
            print(f"    {summary}")

    # 变量
    variables = data.get("变量", {})
    if variables:
        print(f"\n{Colors.BOLD}变量:{Colors.RESET}")
        money = variables.get("金钱", {})
        if money:
            print(f"  金钱: 当前 {money.get('当前', '?')}")
            for m in money.get("变动", []):
                print(f"    - {m}")

        items = variables.get("物品", {})
        if items:
            print(f"  物品:")
            for k, v in items.items():
                print(f"    {k}: {v}")

        prices = variables.get("价格", {})
        if prices:
            print(f"  价格:")
            for k, v in prices.items():
                print(f"    {k}: {v}")

    # 人物状态
    char_states = data.get("人物状态", {})
    if char_states:
        print(f"\n{Colors.BOLD}人物状态:{Colors.RESET}")
        for name, info in char_states.items():
            if isinstance(info, dict):
                loc = info.get("位置", "")
                state = info.get("状态", "")
                print(f"  {name}: {loc} | {state}")
                for evt in info.get("关键事件", []) if isinstance(info.get("关键事件"), list) else []:
                    print(f"    事件: {evt}")

    # 待回收伏笔
    hooks = data.get("待回收伏笔", [])
    if hooks:
        print(f"\n{Colors.BOLD}待回收伏笔:{Colors.RESET}")
        for h in hooks:
            if isinstance(h, dict):
                content = h.get("内容", "")
                status = h.get("状态", "")
                planted = h.get("埋下", "")
                echo = h.get("呼应", "")
                line = f"  [{status}] {content}（{planted}）"
                if echo:
                    line += f" → {echo}"
                print(line)

    # 卷归档
    archives = data.get("卷归档", [])
    if archives:
        print(f"\n{Colors.BOLD}卷归档:{Colors.RESET}")
        for a in archives:
            if isinstance(a, dict):
                vol = a.get("卷", "")
                summary = a.get("摘要", "")
                print(f"  {vol}: {summary}")



def _chapter_num_key(raw) -> str:
    """把「第024章」/「第24章」/「24」统一成无前导零的章号字符串。

    旧实现用子串匹配判断时间线是否存在（"第24章" in "第024章" 为 False），
    导致只要追踪文件按不带零的写法记录，check 就永久误报「时间线缺失」。
    """
    m = re.search(r"第\s*0*(\d+)\s*章", str(raw))
    if m:
        return m.group(1)
    m = re.fullmatch(r"\s*0*(\d+)\s*", str(raw))
    return m.group(1) if m else ""


_PROTAGONIST_STATE_CACHE = {}


def _load_protagonist_state() -> dict:
    """读 主角状态.yaml（境界与功法法器等物品的权威追踪源），本次运行内缓存。"""
    if "data" not in _PROTAGONIST_STATE_CACHE:
        loaded = {}
        try:
            raw = read_file(WORKSPACE / "主角状态.yaml", "主角状态")
            if "不存在" not in raw and "读取失败" not in raw:
                parsed = yaml.safe_load(raw)
                if isinstance(parsed, dict):
                    loaded = parsed
        except Exception:
            loaded = {}
        _PROTAGONIST_STATE_CACHE["data"] = loaded
    return _PROTAGONIST_STATE_CACHE["data"]


def _load_protagonist_realm() -> str:
    """读境界。唯一追踪源是 主角状态.yaml 顶层「真实境界」。

    旧实现只查 章节追踪.yaml 的 人物状态.沈默，而该结构里根本没有境界字段，
    于是恒为空、每次都以「当前追踪境界=未设定」告警，等于境界校验长期空转。
    """
    state = _load_protagonist_state()
    for key in ("真实境界", "境界"):
        v = state.get(key)
        if v:
            return str(v).strip()
    return ""


def _load_protagonist_items_text() -> str:
    """主角状态.yaml 的物品清单文本。

    旧实现只把 章节追踪.yaml 的 变量.物品（盐/苞谷面一类消耗品）当已追踪池，
    导致记在 主角状态.yaml 里的功法法器（潜龙诀、芥子坠、隐息衣等）被反复误报「新物品未追踪」。
    """
    return str(_load_protagonist_state().get("物品", ""))


# 句子级跨章重叠阈值（整段复制检测）
SCENE_SENT_RATIO_WARN = 0.15
SCENE_SENT_RATIO_STRONG = 0.30


def cmd_check(args):
    """章节一致性校验：读取章节追踪.yaml，输出变量快照；--chapter 模式扫描章文件做交叉校验；--archive 归档当前卷"""
    import glob as glob_mod
    import shutil

    tracking_path = WORKSPACE / "章节追踪.yaml"
    archive_dir = PROJECT_ROOT / "设定" / "02_工作区"

    # --- --archive 模式：归档当前卷 ---
    if args.archive:
        _check_archive(tracking_path, archive_dir)
        return

    tracking_raw = read_file(tracking_path, "章节追踪")
    if "不存在" in tracking_raw or "读取失败" in tracking_raw:
        print(f"{Colors.RED}错误: {tracking_raw}{Colors.RESET}")
        return

    try:
        data = yaml.safe_load(tracking_raw)
    except Exception as e:
        print(f"{Colors.RED}YAML 解析失败: {e}{Colors.RESET}")
        return

    if not data:
        print(f"{Colors.YELLOW}章节追踪.yaml 为空{Colors.RESET}")
        return

    # 默认模式：输出变量快照
    _check_snapshot(data)
    # --chapter 模式所需的追踪数据（从 data 重新派生；_check_snapshot 内部持有独立副本，避免作用域回归）
    variables = data.get("变量", {})
    timeline = data.get("时间线", [])
    char_states = data.get("人物状态", {})
    # --chapter 模式：扫描章节文件做交叉校验
    if args.chapter:
        print_header(f"校验: {args.chapter}")
        chapter_name = args.chapter
        rules = _load_validation_rules()

        # 解析卷/章号（支持"第1卷 第2章" / "第2章" / "第002章_集市偶遇"；三段制写"第5卷上 第046章"）
        vol_match = RE_VOLUME.search(chapter_name)
        ch_match = RE_CHAPTER.search(chapter_name)
        want_vol = int(vol_match.group(1)) if vol_match else None
        want_ch = int(ch_match.group(1)) if ch_match else None
        _, want_seg = vol_seg(chapter_name)

        # 仅给章号时，用"当前卷"推断卷号，避免跨卷误匹配
        if want_vol is None and want_ch is not None:
            _m = re.search(r'当前卷:\s*第\s*(\d+)\s*卷', tracking_raw)
            if _m:
                want_vol = int(_m.group(1))

        # 收集候选：卷目录名含卷号 + 文件名含章号
        candidates = []
        for f in (PROJECT_REAL_ROOT / "正文").rglob("*.md"):
            fn = RE_CHAPTER.search(f.name)
            if not fn:
                continue
            fch = int(fn.group(1))
            if want_ch is not None and fch != want_ch:
                continue
            if want_vol is not None:
                fvol, fseg = vol_seg(f.parent.name)
                if fvol is not None and fvol != want_vol:
                    continue
                if want_seg and fseg != want_seg:
                    continue
            if want_ch is None and chapter_name not in f.name:
                continue
            candidates.append(f)

        # 歧义：仍命中多卷同名章 → 列出候选，要求精确指定（不再静默取第一个）
        if len(candidates) > 1:
            print(f"{Colors.YELLOW}检测到多个匹配章节，请指定卷号以避免歧义:{Colors.RESET}")
            for c in candidates:
                print(f"  - {c.relative_to(PROJECT_REAL_ROOT)}")
            print(f'{Colors.YELLOW}示例: novel_kb.py check --chapter "第1卷 第2章"；三段制写 --chapter "第5卷上 第046章"{Colors.RESET}')
            return

        if not candidates:
            print(f"{Colors.YELLOW}未找到章节文件: {chapter_name}{Colors.RESET}")
            return

        chapter_path = candidates[0]
        chapter_text = read_file(chapter_path, chapter_name)
        if "不存在" in chapter_text or "读取失败" in chapter_text:
            print(f"{Colors.RED}{chapter_text}{Colors.RESET}")
            return

        current_ch_num = want_ch or 0
        current_vol = want_vol
        current_seg = want_seg

        issues = []
        warnings = []

        # --- 辅助：中文数字转阿拉伯数字 ---
        _CN_NUM = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,
                    '十':10,'百':100,'千':1000,'万':10000}
        def _cn_to_int(s: str):
            """将中文数字字符串转为int，失败返回None"""
            if not s:
                return None
            if s.isdigit():
                return int(s)
            total = 0
            current = 0
            for c in s:
                if c not in _CN_NUM:
                    continue
                val = _CN_NUM[c]
                if val >= 10000:
                    current = (current if current > 0 else 1) * val
                    total += current
                    current = 0
                elif val >= 10:
                    current = (current if current > 0 else 1) * val
                else:
                    current += val
            total += current
            return total if total > 0 else None

        # 提取所有铜子提及（支持阿拉伯数字和中文数字）
        copper_pattern = r'([\d一二两三四五六七八九十百千万]+)\s*铜子'
        copper_mentions = re.findall(copper_pattern, chapter_text)
        if copper_mentions:
            copper_values = []
            for m in copper_mentions:
                v = _cn_to_int(m)
                copper_values.append(str(v) if v else m)
            print(f"  铜子提及: {', '.join(copper_values)}")

        # --- 1. 价格校验：支持中文数字 ---
        for item_name, tracked_price in variables.get("价格", {}).items():
            price_match = re.search(rf'{item_name}.{{0,20}}([\d一二两三四五六七八九十百千万]+)\s*铜子', chapter_text)
            if price_match:
                mentioned_price = _cn_to_int(price_match.group(1))
                tracked_num = re.search(r'(\d+)', str(tracked_price))
                if mentioned_price and tracked_num and mentioned_price != int(tracked_num.group(1)):
                    issues.append(f"价格不一致: {item_name} 追踪={tracked_num.group(1)}铜子, 章节中={mentioned_price}铜子")

        # --- 1.5 金钱变动校验：章节中消费/收入与追踪金钱对比 ---
        money_info = variables.get("金钱", {})
        if money_info:
            current_money = money_info.get("当前", None)
            if current_money is not None:
                # 提取章节中的消费模式
                spend_patterns = [
                    r'花了\s*([\d一二两三四五六七八九十百千万]+)\s*铜子',
                    r'花掉\s*([\d一二两三四五六七八九十百千万]+)\s*铜子',
                    r'付了\s*([\d一二两三四五六七八九十百千万]+)\s*铜子',
                    r'给了\s*([\d一二两三四五六七八九十百千万]+)\s*铜子',
                ]
                for pat in spend_patterns:
                    for m in re.finditer(pat, chapter_text):
                        spent = _cn_to_int(m.group(1))
                        if spent and spent > 100:
                            warnings.append(f"大额消费: 花费{spent}铜子，当前追踪金钱={current_money}铜子，请确认已更新")

        # --- 2. 时间线校验：检查本章是否有追踪条目（按归一章号比对，消除零填充误报）---
        cur_ch_key = _chapter_num_key(chapter_name)
        tracked_chapters = {k for k in (_chapter_num_key(t.get("章节", "")) for t in timeline) if k}
        chapter_in_tracking = bool(cur_ch_key) and cur_ch_key in tracked_chapters
        if not chapter_in_tracking and timeline and cur_ch_key:
            warnings.append(f"时间线缺失: 章节追踪.yaml 中没有 {chapter_name} 的时间线条目")

        # 检查时间衔接
        if timeline:
            last_timeline = timeline[-1]
            last_time = last_timeline.get("时间", "")
            if "→" in last_time:
                last_end = last_time.split("→")[-1].strip()
                head = chapter_text[:300]
                time_transitions = {
                    "夜晚": ["天亮", "天蒙蒙亮", "天没亮", "第二天", "早上", "天快亮"],
                    "傍晚": ["天没亮", "天蒙蒙亮", "第二天", "早上"],
                }
                if last_end in time_transitions:
                    for t_word in time_transitions[last_end]:
                        if t_word in head:
                            print(f"  时间衔接: 上章结尾={last_end}, 本章开头={t_word}")
                            break

        # --- 3. 新物品检测：扫描已知物品关键词（规则文件 + 动态追踪物品）---
        # 候选物品 = 规则兜底 known_items ∪ 追踪文件中的物品键（新增物品自动纳入）
        _items_raw = variables.get("物品", {})
        if isinstance(_items_raw, dict):
            tracked_item_names = set(_items_raw.keys())
        elif isinstance(_items_raw, list):
            # 主角状态.yaml 的物品是列表（"潜龙诀 x 1（…）"），取条目名
            tracked_item_names = {re.split(r"\s*x\s*|[（(]", str(x))[0].strip() for x in _items_raw}
        else:
            tracked_item_names = set()
        known_item_keywords = list(rules.get("known_items", [])) + list(tracked_item_names)
        # 收集所有已追踪的文本（物品+伏笔+归档），并并入 主角状态.yaml 的物品清单
        all_tracked_text = str(variables.get("物品", {}))
        all_tracked_text += _load_protagonist_items_text()
        all_tracked_text += str(data.get("待回收伏笔", []))
        all_tracked_text += str(data.get("卷归档", []))
        for kw in known_item_keywords:
            if kw and kw in chapter_text:
                already_tracked = kw in all_tracked_text or kw in tracked_item_names
                if not already_tracked:
                    warnings.append(f"新物品出现但未追踪: {kw}")

        # --- 4. 人物状态变更检测 ---
        _DEFAULT_STATE_KEYWORDS = rules.get("state_keywords", [])
        for char_name in char_states:
            char_info = char_states.get(char_name, {})
            if not isinstance(char_info, dict):
                continue
            tracked_state = char_info.get("状态", "")
            tracked_events = char_info.get("关键事件", [])
            # 将状态+关键事件+归档合并为一个文本池
            char_tracked_text = tracked_state + " ".join(str(e) for e in tracked_events)
            char_tracked_text += str(data.get("卷归档", []))
            for kw in _DEFAULT_STATE_KEYWORDS:
                if kw in chapter_text and kw not in char_tracked_text:
                    warnings.append(f"人物状态变更: {char_name} 出现「{kw}」，当前追踪状态: {tracked_state or '无'}")

        # --- 4.5 境界变更校验：章节中修为变化与主角状态交叉校验 ---
        _REALM_KEYWORDS = rules.get("realm_keywords", [])
        shenmo_state = char_states.get('沈默', {})
        # 境界取自 主角状态.yaml（唯一追踪源）；缺失时退回解析「状态」首段
        real_realm = _load_protagonist_realm()
        if not real_realm and isinstance(shenmo_state, dict):
            real_realm = str(shenmo_state.get('境界', shenmo_state.get('真实境界', ''))).strip()
        if not real_realm and isinstance(shenmo_state, dict):
            # 例：状态 "蜕凡后期，玄天宗内门弟子（内门大比第二名）" → "蜕凡后期"
            real_realm = str(shenmo_state.get('状态', '')).split('，')[0].strip()
        if isinstance(shenmo_state, dict):
            for kw in _REALM_KEYWORDS:
                if kw in chapter_text and kw not in real_realm and kw not in str(data.get('卷归档', [])):
                    warnings.append(f"境界变更: 章节出现「{kw}」，当前追踪境界={real_realm or '未设定'}，请确认是否已更新")

        # --- 5. 伏笔关键词交叉扫描 ---
        hook_keywords = rules.get("hook_keywords", {})
        hooks = data.get("待回收伏笔", [])
        # 将所有伏笔条目展开为文本池（内容+呼应+状态+埋下）
        hook_text_pool = ""
        for h in hooks:
            if isinstance(h, dict):
                hook_text_pool += " ".join(str(v) for v in h.values())
        hook_text_pool += str(data.get("卷归档", []))
        for kw, desc in hook_keywords.items():
            if kw in chapter_text:
                # 检查关键词或其关联词是否已在伏笔追踪文本中
                if kw not in hook_text_pool:
                    warnings.append(f"伏笔关键词「{kw}」出现（关联: {desc}），建议更新伏笔追踪")

        # --- 6. 跨章场景去重：对话/场景短语 + 句子级逐字重叠 ---
        # 旧实现只看 6–30 字的对话短语，对「整段位移复制」几乎无感：
        # 实测第23↔24章 91% 句子逐字相同，旧逻辑只报「9 个相同短语」。
        def _dedup_sentences(text: str) -> set:
            out = set()
            for s in re.split(r'[。！？…；\n]+', text):
                s = s.strip()
                if len(s) >= 12 and sum(1 for c in s if '\u4e00' <= c <= '\u9fff') >= 8:
                    out.add(s)
            return out

        cur_sents = _dedup_sentences(chapter_text)
        event_phrases = set()
        for m in re.finditer(r'[""「」]([^""「」]{6,30})[""「」]', chapter_text):
            phrase = m.group(1)
            # 排除太短或太通用的
            if len(phrase) >= 6 and not any(w in phrase for w in ['嗯', '啊', '哦', '吧', '呢', '嘛']):
                event_phrases.add(phrase)
        # 从叙事中提取特定场景短语（含地点+动作的组合）
        for m in re.finditer(r'(山坳|柿子林|松林垭口|苞谷地|溪沟|灶房|院门口|粮铺|杂货铺|铁匠铺)[一-鿿]{2,6}', chapter_text):
            event_phrases.add(m.group())
        # 扫描前面的章节
        if (event_phrases or cur_sents) and current_ch_num > 1:
            prev_files = []
            for f in (PROJECT_REAL_ROOT / "正文").rglob("*.md"):
                file_num = RE_CHAPTER.search(f.name)
                if not file_num:
                    continue
                fch = int(file_num.group(1))
                if not (0 < fch < current_ch_num):
                    continue
                # 仅同卷（同段）比较，避免跨卷误判
                if current_vol is not None:
                    fvol, fseg = vol_seg(f.parent.name)
                    if fvol is not None and fvol != current_vol:
                        continue
                    if current_seg and fseg != current_seg:
                        continue
                # 限制回溯窗口，控制扫描成本
                if current_ch_num - fch > SCENE_DEDUP_LOOKBACK:
                    continue
                prev_files.append((fch, f))
            for prev_num, prev_path in sorted(prev_files):
                prev_text = read_file(prev_path, f"第{prev_num}章")
                if "不存在" in prev_text or "读取失败" in prev_text:
                    continue
                matched = [p for p in event_phrases if p in prev_text]
                shared = cur_sents & _dedup_sentences(prev_text)
                ratio = len(shared) / max(1, len(cur_sents)) if cur_sents else 0.0
                if ratio >= SCENE_SENT_RATIO_STRONG:
                    ex = ' / '.join(sorted(shared, key=len, reverse=True)[:1])[:40]
                    warnings.append(
                        f"整段复制嫌疑: 第{prev_num}章与本章有 {len(shared)} 句逐字相同"
                        f"（占本章 {ratio:.0%}），例：{ex}…——需合并循环或改写，勿直接删空")
                elif len(matched) >= 3 or ratio >= SCENE_SENT_RATIO_WARN:
                    extra = f"，另有 {len(shared)} 句逐字相同（{ratio:.0%}）" if shared else ""
                    warnings.append(f"场景重复嫌疑: 第{prev_num}章与本章有{len(matched)}个相同对话/场景短语{extra}（{', '.join(list(matched)[:3])}等）")

        # --- 7. 伏笔过期检测：埋下超过10章未呼应 ---
        STALE_HOOK_THRESHOLD = 10
        for h in hooks:
            if not isinstance(h, dict):
                continue
            status = str(h.get("状态", ""))
            planted = str(h.get("埋下", ""))
            content = str(h.get("内容", ""))
            # 已解或已呼应的跳过
            if "已解" in status or "已回收" in status:
                continue
            # 提取埋下章节号
            planted_num = RE_CHAPTER.search(planted)
            if planted_num and current_ch_num > 0:
                planted_ch = int(planted_num.group(1))
                # 跨卷不比较章节差（各卷编号不连续，差值无意义）；段字不同同样视为跨卷
                pvol, pseg = vol_seg(planted)
                if current_vol is not None and pvol is not None and pvol != current_vol:
                    continue
                if current_seg and pseg != current_seg:
                    continue
                stale_chapters = current_ch_num - planted_ch
                if stale_chapters >= STALE_HOOK_THRESHOLD:
                    warnings.append(f"伏笔过期: 「{content}」埋下于{planted}，已过{stale_chapters}章未回收")
                elif stale_chapters >= STALE_HOOK_THRESHOLD // 2 and "呼应" not in status:
                    warnings.append(f"伏笔老化: 「{content}」埋下于{planted}，已过{stale_chapters}章无呼应")

        # --- 8. 时间线摘要一致性：检查追踪条目的结构完整性 ---
        for t in timeline:
            if not isinstance(t, dict):
                continue
            t_ch = t.get("章节", "")
            if t_ch and t_ch in chapter_name:
                summary = t.get("摘要", "")
                time_str = t.get("时间", "")
                if not summary:
                    warnings.append(f"时间线条目不完整: {t_ch} 有条目但摘要为空")
                if not time_str:
                    warnings.append(f"时间线条目不完整: {t_ch} 有条目但时间为空")

        # --- 9. 人物位置一致性：对比追踪位置与章节内容 ---
        for char_name, char_info in char_states.items():
            if not isinstance(char_info, dict):
                continue
            tracked_loc = char_info.get("位置", "")
            if not tracked_loc:
                continue
            # 位置归类（外置于 validation_rules.yaml 的 location_groups）
            location_keywords = rules.get("location_groups", {})
            # 检查章节中是否出现了与追踪位置不符的描述
            if tracked_loc in location_keywords:
                loc_words = location_keywords[tracked_loc]
                has_loc_match = any(w in chapter_text for w in loc_words)
                # 检查是否有明确的位置变更词
                move_keywords = ['到了', '离开', '出发', '走进', '走出', '翻过', '来到']
                has_move = any(w in chapter_text[:500] for w in move_keywords)
                if not has_loc_match and has_move:
                    warnings.append(f"人物位置变更: {char_name} 追踪位置={tracked_loc}，但章节开头有位置移动描述")

        # --- 输出结果 ---
        # 物品出现
        for item_name, item_val in variables.get("物品", {}).items():
            if item_name in chapter_text and "无" not in str(item_val):
                print(f"  物品出现: {item_name}（追踪: {item_val}）")

        if warnings:
            print(f"\n{Colors.YELLOW}⚠ {len(warnings)} 条提醒:{Colors.RESET}")
            for i, w in enumerate(warnings, 1):
                print(f"  {i}. {w}")

        if issues:
            print(f"\n{Colors.RED}✗ 发现 {len(issues)} 个问题:{Colors.RESET}")
            for i, issue in enumerate(issues, 1):
                print(f"  {i}. {issue}")
        elif not warnings:
            print(f"\n{Colors.GREEN}✓ 校验通过，未发现不一致{Colors.RESET}")
        else:
            print(f"\n{Colors.GREEN}✓ 数据一致，但有 {len(warnings)} 条待更新提醒{Colors.RESET}")


def cmd_state(args):
    state = read_file(WORKSPACE / "主角状态.yaml", "状态")
    if args.max_len:
        state = truncate_text(state, args.max_len)
    if args.quiet:
        lines = [l for l in state.split("\n") if any(k in l for k in _STATE_SUMMARY_KEYS)]
        print_header("主角状态（精简）")
        print("\n".join(lines))
    else:
        print_header("主角状态")
        print(state)


def cmd_update(args):
    if args.clear_cache:
        print_header("清理缓存")
        for f in [HISTORY_FILE, HASH_CACHE_FILE, SEMANTIC_CACHE_FILE]:
            if f.exists():
                f.unlink()
                print_success(f"已删除: {f.name}")
        print_success("缓存清理完成")
        return
    _setup_signal_handlers()
    if args.force:
        print_header("强制重建索引")
        force_reindex()
    else:
        print_header("更新索引")
        index_all()


def cmd_collections(args):
    table = _open_main_table(get_db(), required=False)
    if table is None:
        print_header("LanceDB 表统计（0 个）")
        print_error(f"{MAIN_TABLE} 不存在或为空 → 请执行 {_rebuild_cmd()}")
        return
    print_header(f"LanceDB 表统计（1 个：{MAIN_TABLE}）")
    try:
        count = table.count_rows()
        print(f"  {Colors.CYAN}{MAIN_TABLE}{Colors.RESET}: {count} 个块")
        df = table.to_pandas()
        type_counts = df["type"].value_counts()
        for t, c in type_counts.items():
            print(f"    └ {t}: {c}")
    except Exception:
        print_error(f"{MAIN_TABLE}: 读取失败")


def cmd_cleanup(args):
    """清理残留进程锁和缓存文件"""
    print_header("清理临时文件")

    # 清理进程锁
    lock_file = _PROCESS_LOCK_FILE
    if lock_file.exists():
        try:
            lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = lock_data.get("pid")
            if pid and not _is_process_running(pid):
                lock_file.unlink()
                print_success(f"已清理过期进程锁 (PID: {pid})")
            else:
                print_warning(f"进程锁存在且进程仍在运行 (PID: {pid})，跳过")
        except (json.JSONDecodeError, OSError):
            lock_file.unlink(missing_ok=True)
            print_success("已清理损坏的进程锁文件")
    else:
        print_success("无残留进程锁")

    # 清理查询历史
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
        print_success(f"已删除: {HISTORY_FILE.name}")
    else:
        print_success("无查询历史文件")

    # 清理语义缓存
    if SEMANTIC_CACHE_FILE.exists():
        SEMANTIC_CACHE_FILE.unlink()
        print_success(f"已删除: {SEMANTIC_CACHE_FILE.name}")
    else:
        print_success("无语义缓存文件")

    # 清理哈希缓存
    if HASH_CACHE_FILE.exists():
        HASH_CACHE_FILE.unlink()
        print_success(f"已删除: {HASH_CACHE_FILE.name}")
    else:
        print_success("无哈希缓存文件")

    print_success("清理完成")


def cmd_wc(args):
    """统计文件字数"""
    filepath = Path(args.file).resolve()
    if not filepath.exists():
        print_error(f"文件不存在: {filepath}")
        sys.exit(1)

    try:
        text = filepath.read_text(encoding="utf-8").strip()
    except Exception as e:
        print_error(f"读取失败: {e}")
        sys.exit(1)

    # 去掉标题行（# 开头的行）
    lines = text.split("\n")
    content = "\n".join(l for l in lines if not l.startswith("#"))

    # 统计中文字符
    chinese_chars = RE_HANZI_CHAR.findall(content)
    chinese_count = len(chinese_chars)

    # 统计中文字符+中文标点
    all_cn_chars = re.findall(r'[一-鿿　-〿＀-￯]', content)
    all_cn_count = len(all_cn_chars)

    # 统计所有非空白字符
    all_chars = re.findall(r'\S', content)
    all_count = len(all_chars)

    print_header(f"字数统计: {filepath.name}")

    if args.all:
        # 显示所有统计
        print(f"  中文字符:      {Colors.CYAN}{chinese_count}{Colors.RESET}")
        print(f"  中文字符+标点:  {Colors.CYAN}{all_cn_count}{Colors.RESET}")
        print(f"  所有非空白字符: {Colors.CYAN}{all_count}{Colors.RESET}")
    else:
        # 默认只显示中文字符
        print(f"  中文字符: {Colors.CYAN}{chinese_count}{Colors.RESET}")


MERGE_EXTENSIONS = {".md", ".yaml", ".yml", ".json", ".html"}
MERGE_SEPARATOR = "\n\n================================================================================\n\n"

# 文件类型映射（后缀 → 人类可读类型）
_FILE_TYPE_MAP = {
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".html": "html",
}


def _file_metadata_yaml(rel_path: str, abs_path: Path, content: str) -> str:
    """为单个文件片段生成 YAML Frontmatter 元数据"""
    stat = abs_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    size_kb = round(stat.st_size / 1024, 1)
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    file_type = _FILE_TYPE_MAP.get(abs_path.suffix.lower(), "unknown")

    return (
        f"---\n"
        f"source: \"{rel_path}\"\n"
        f"type: \"{file_type}\"\n"
        f"modified: \"{mtime}\"\n"
        f"size_kb: {size_kb}\n"
        f"sha256: \"{sha256}\"\n"
        f"---"
    )


def _build_doc_frontmatter(input_dir: Path, files: list[Path], file_count: int) -> str:
    """为整个合并文档生成顶层 YAML Frontmatter"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dir_rel = input_dir.name
    max_list = 20  # 最多列出的文件数，避免 frontmatter 过长

    file_list = [f'  - "{f.name}"' for f in files[:max_list]]
    if len(files) > max_list:
        file_list.append(f"  # ... 省略 {len(files) - max_list} 个文件")

    return (
        f"---\n"
        f'title: "{dir_rel} 设定整合文档"\n'
        f'generated: "{now}"\n'
        f'source_dir: "{dir_rel}"\n'
        f"file_count: {file_count}\n"
        f'generator: "novel_kb.py v{__version__}"\n'
        f"files:\n"
        + "\n".join(file_list) + "\n"
        f"---"
    )


def _detect_yaml_overlap_keys(files: list[Path]) -> dict[Path, set[str]]:
    """检测主文件与子文件之间的 YAML 顶级 key 重叠。

    主文件命名: XXX.yaml  子文件命名: XXX_子名.yaml
    返回 {主文件路径: 需跳过的keys集合}
    """
    yaml_exts = {".yaml", ".yml"}
    yaml_files = [f for f in files if f.suffix.lower() in yaml_exts]

    # 按前缀分组：去掉最后一个 _xxx 后缀得到主文件名
    prefix_groups: dict[str, list[Path]] = {}
    for f in yaml_files:
        stem = f.stem
        # 尝试拆分：如果存在 _ 分割且去掉最后部分后有对应主文件，则归为子文件
        parts = stem.rsplit("_", 1)
        if len(parts) == 2:
            parent_stem = parts[0]
            parent_path = f.parent / f"{parent_stem}{f.suffix}"
            if parent_path in yaml_files:
                prefix_groups.setdefault(str(parent_path), []).append(f)

    # 对每组提取重叠 keys
    skip_map: dict[Path, set[str]] = {}
    for parent_str, children in prefix_groups.items():
        parent_path = Path(parent_str)
        try:
            parent_data = yaml.safe_load(parent_path.read_text(encoding="utf-8"))
            if not isinstance(parent_data, dict):
                continue
            parent_keys = set(parent_data.keys())
        except Exception:
            continue

        child_keys: set[str] = set()
        for child in children:
            try:
                child_data = yaml.safe_load(child.read_text(encoding="utf-8"))
                if isinstance(child_data, dict):
                    child_keys |= set(child_data.keys())
            except Exception:
                continue

        overlap = parent_keys & child_keys
        if overlap:
            skip_map[parent_path] = overlap
            print_info(f"主文件 {parent_path.name} 与 {len(children)} 个子文件重叠 {len(overlap)} 个 key，去重")

    return skip_map


def _strip_yaml_keys(content: str, keys_to_remove: set[str]) -> str:
    """从 YAML 文本中移除指定的顶级 key（保留注释和非重叠内容）"""
    lines = content.split("\n")
    result = []
    skip_indent = -1

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if indent == 0 and stripped and not stripped.startswith("#"):
            key = stripped.split(":")[0].strip()
            if key in keys_to_remove:
                skip_indent = 0
                continue
            else:
                skip_indent = -1

        # 跳过属于被移除 key 的子行
        # 空行不重置 skip 状态（YAML 中空行常出现在 key 之间）
        if skip_indent >= 0:
            if stripped and indent > skip_indent:
                continue
            elif not stripped:
                continue  # 跳过被移除 key 之间的空行

        result.append(line)

    return "\n".join(result)


def cmd_merge(args: argparse.Namespace) -> None:
    """将目录下所有设定文件整合为一个 md 文档，附带 YAML Frontmatter 溯源标记"""
    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        print_error(f"目录不存在: {input_dir}")
        sys.exit(1)

    # 收集文件
    files = sorted(
        f for f in input_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in MERGE_EXTENSIONS
    )

    if not files:
        print_warning("目录下无匹配文件")
        return

    # 输出路径
    if args.output:
        output_path = Path(args.output).resolve()
        # 安全校验：输出必须落在项目目录内，防止写穿到系统路径（O11）
        if PROJECT_REAL_ROOT != output_path and PROJECT_REAL_ROOT not in output_path.parents:
            print_error(f"输出路径必须在项目目录内: {output_path}")
            sys.exit(1)
    else:
        output_path = PROJECT_ROOT / f"{input_dir.name}_整合.md"

    # 检测主/子文件 YAML key 重叠，生成去重映射
    yaml_skip_map = _detect_yaml_overlap_keys(files)

    # 拼接（CLAUDE.md 排在最前）
    parts = []
    all_files_for_frontmatter = []

    claude_md = PROJECT_REAL_ROOT / "CLAUDE.md"
    if claude_md.exists():
        try:
            content = claude_md.read_text(encoding="utf-8").rstrip()
            fm = _file_metadata_yaml("CLAUDE.md", claude_md, content)
            parts.append(f"{fm}\n\n<!-- 文件: CLAUDE.md -->\n\n# \U0001f4c4 CLAUDE.md\n\n{content}")
            all_files_for_frontmatter.append(claude_md)
        except Exception as e:
            print_warning(f"读取 CLAUDE.md 失败，已跳过: {e}")

    for f in files:
        rel = f.relative_to(input_dir).as_posix()
        try:
            content = f.read_text(encoding="utf-8").rstrip()
        except Exception as e:
            print_warning(f"跳过 {rel}: {e}")
            continue

        # 对主文件去除与子文件重叠的 YAML keys
        if f in yaml_skip_map:
            content = _strip_yaml_keys(content, yaml_skip_map[f])

        fm = _file_metadata_yaml(rel, f, content)
        parts.append(f"{fm}\n\n<!-- 文件: {rel} -->\n\n# \U0001f4c4 {rel}\n\n{content}")
        all_files_for_frontmatter.append(f)

    # 顶层 Frontmatter + 正文
    doc_fm = _build_doc_frontmatter(input_dir, all_files_for_frontmatter, len(parts))
    output_text = f"{doc_fm}\n\n{MERGE_SEPARATOR.join(parts)}\n"

    output_path.write_text(output_text, encoding="utf-8")
    print_success(f"已整合 {len(parts)} 个文件 → {output_path}")
    print_info(f"溯源标记: YAML Frontmatter（顶层文档 + 每个文件片段）")


# ============================================================
# 主函数
# ============================================================


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="novel_kb.py",
        description="小说知识库管理器 — 索引设定文件到 LanceDB，写作时按需检索",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例（在项目根目录下运行）：

  查询：
  python 世界设定规则/脚本/novel_kb.py query "沈默的境界"              关键词查询
  python 世界设定规则/脚本/novel_kb.py query "丹药" -n 5 --mode full   返回5条完整内容
  python 世界设定规则/脚本/novel_kb.py query "秘境" --state             查询+显示主角状态
  python 世界设定规则/脚本/novel_kb.py query --type character           只查角色类设定
  python 世界设定规则/脚本/novel_kb.py query                            全量查询所有设定
  python 世界设定规则/脚本/novel_kb.py query -n 100                     全量查询返回100条

  状态与索引：
  python 世界设定规则/脚本/novel_kb.py state -q                         精简显示主角状态
  python 世界设定规则/脚本/novel_kb.py index                            增量更新（文件未变化则跳过）
  python 世界设定规则/脚本/novel_kb.py index --force                    强制重建（忽略哈希，全量重写）
  python 世界设定规则/脚本/novel_kb.py index --clear-cache              清理缓存后退出

  工具：
  python 世界设定规则/脚本/novel_kb.py verify --fix                     校验并自动修复索引
  python 世界设定规则/脚本/novel_kb.py collections                      列出所有表及块数
  python 世界设定规则/脚本/novel_kb.py merge 设定/01_设定集 out.md       整合目录为单个md
  python 世界设定规则/脚本/novel_kb.py cleanup                          清理进程锁和缓存
  python 世界设定规则/脚本/novel_kb.py wc 正文/第1卷_潜龙在渊/第001章.md 统计字数
  python 世界设定规则/脚本/novel_kb.py wc 正文/第1卷_潜龙在渊/第001章.md --all  统计所有字符

  章节校验：
  python 世界设定规则/脚本/novel_kb.py check                            输出变量快照
  python 世界设定规则/脚本/novel_kb.py check --chapter 第2章             交叉校验指定章节
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # query
    p = sub.add_parser("query", help="查询设定", description="向知识库检索相关设定，支持同义词扩展和重排序。不传关键词则全量查询")
    p.add_argument("query", nargs="?", default=None, help="查询关键词（支持自然语言，不传则全量查询）")
    p.add_argument("-n", "--top", type=int, default=DEFAULT_CONFIG["top"], help="返回条数（默认 %(default)s，全量查询默认50）")
    p.add_argument("--min-score", type=float, default=DEFAULT_CONFIG["min_score"], help="最低相关度阈值（默认 %(default)s）")
    p.add_argument("--max-len", type=int, default=DEFAULT_CONFIG["max_len"], help="每条结果截断长度（默认 %(default)s）")
    p.add_argument("--type", dest="filter_type",
                   help="按类型过滤: character(角色) / world(世界) / rule(规则) / outline(大纲) / workspace(工作区) / reference(速查)")
    p.add_argument("--state", action="store_true", help="同时显示主角当前状态")
    p.add_argument("--mode", choices=["brief", "full", "summary"], default="brief",
                   help="输出模式: brief=截断(默认) / full=完整 / summary=仅标题")
    p.set_defaults(func=cmd_query)

    # state
    p = sub.add_parser("state", help="读取主角状态", description="读取主角状态文件（主角状态.yaml）")
    p.add_argument("-q", "--quiet", action="store_true", help="精简模式，只显示关键字段")
    p.add_argument("--max-len", type=int, default=0, help="截断长度（默认不截断）")
    p.set_defaults(func=cmd_state)

    # check
    p = sub.add_parser("check", help="章节一致性校验", description="读取章节追踪.yaml，输出变量快照；--chapter 模式扫描章文件做交叉校验；--archive 归档当前卷")
    p.add_argument("--chapter", type=str, default=None, help="指定章节名进行交叉校验（如：第2章）")
    p.add_argument("--archive", action="store_true", help="归档当前卷数据到归档/目录，重置主文件为新卷空白模板")
    p.set_defaults(func=cmd_check)

    # index
    p = sub.add_parser("index", help="索引/更新所有文件", description="扫描设定文件，增量更新 LanceDB 索引（文件未变化时自动跳过）")
    p.add_argument("--force", action="store_true", help="强制重建所有表（忽略哈希缓存）")
    p.add_argument("--clear-cache", action="store_true", help="清理查询历史、语义缓存、哈希缓存后退出")
    p.set_defaults(func=cmd_update)

    # collections
    p = sub.add_parser("collections", help="列出所有 LanceDB 表", description="显示所有已索引的表及其块数")
    p.set_defaults(func=cmd_collections)

    # verify
    p = sub.add_parser("verify", help="校验索引完整性", description="检查索引是否完整、过期或维度不匹配")
    p.add_argument("--fix", action="store_true", help="自动修复缺失/过期/维度不匹配的表")
    p.set_defaults(func=lambda args: verify_index(fix=args.fix))

    # cleanup
    p = sub.add_parser("cleanup", help="清理缓存文件", description="清理残留进程锁、查询历史、语义缓存、哈希缓存")
    p.set_defaults(func=cmd_cleanup)

    # merge
    p = sub.add_parser("merge", help="整合目录为单个 md 文件", description="递归扫描目录，将所有设定文件合并为一个 markdown 文档（CLAUDE.md 自动置顶）")
    p.add_argument("input_dir", help="要整合的目录路径（如 设定/01_设定集）")
    p.add_argument("output", nargs="?", default=None, help="输出文件路径（默认: <目录名>_整合.md，位于项目根目录）")
    p.set_defaults(func=cmd_merge)

    # wc
    p = sub.add_parser("wc", help="统计文件字数", description="统计文件的中文字数，默认只统计中文字符")
    p.add_argument("file", help="要统计的文件路径")
    p.add_argument("--all", action="store_true", help="显示所有统计（中文字符、中文字符+标点、所有非空白字符）")
    p.set_defaults(func=cmd_wc)

    return parser


def main() -> None:
    _setup_signal_handlers()
    args = _build_parser().parse_args()
    if not args.command:
        _build_parser().print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
