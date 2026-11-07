# 玄幻小说创作项目

## 项目简介

这是一部玄幻爽文小说创作项目，采用"扮猪吃虎+打脸升级"的核心爽点模式。

**世界观**：玄幻世界（非修仙世界）

## 项目结构

```
ai-novel/
├── CLAUDE.md                    # 项目配置和写作流程
├── .claudeignore                # Claude 忽略规则
├── .gitignore                   # Git 忽略规则
├── 世界设定规则/
│   ├── 脚本/
│   │   └── novel_kb.py          # 知识库查询脚本（唯一入口）
│   └── 设定/
│       ├── 00_大纲/             # 卷大纲
│       ├── 01_设定集/           # 只读核心设定
│       ├── 02_工作区/           # 归档数据
│       └── 章节追踪/            # 写作追踪文件
│           ├── 章节追踪.yaml    # 时间线、金钱、物品、人物、事件、伏笔
│           ├── 主角状态.yaml    # 修为、身份、能力、经济、物品、人际关系
│           ├── 主角状态_精简.yaml
│           ├── 时间线.md        # 背景事件、异步推演、主线节点
│           ├── 钩子.md          # 悬念、伏笔、剧情关联、节奏表
│           └── 势力.md          # 势力总览、金字塔结构
└── 正文/                        # 章节正文
```

## 环境安装

### 1. Python 依赖

```bash
pip install -r requirements.txt
```

**依赖说明**（完整清单见 `世界设定规则/脚本/requirements.txt`）：
- `torch` - PyTorch 深度学习框架（sentence-transformers 必需）
- `sentence-transformers` - 句子向量模型库（含 bge 嵌入与重排）
- `transformers` - HuggingFace 模型库
- `lancedb` - 向量数据库（已锁定 0.33.x，避免 FTS API 弃用）
- `pyyaml` - YAML 文件解析
- 其余依赖（numpy / pandas / onnxruntime / optimum / safetensors / tokenizers / psutil）随 `requirements.txt` 一并安装

> 注：依赖已锁定版本区间；模型权重需按下方「模型下载」手动获取，不在 pip 依赖内。装好后请先运行 `python 世界设定规则/脚本/novel_kb.py index --force` 建立向量索引。

### 2. 模型下载

项目使用两个模型，存放在 `世界设定规则/脚本/模型/` 目录：

**嵌入模型**（用于向量检索）：
- 模型：`bge-small-zh-v1.5`
- 大小：约 95MB
- 下载地址：https://huggingface.co/BAAI/bge-small-zh-v1.5

**重排模型**（用于结果精排）：
- 模型：`bge-reranker-v2-m3`
- 大小：约 2.2GB
- 下载地址：https://huggingface.co/BAAI/bge-reranker-v2-m3

**下载方式**：
```bash
# 方式一：使用 huggingface-cli（推荐）
pip install huggingface_hub
huggingface-cli download BAAI/bge-small-zh-v1.5 --local-dir 世界设定规则/脚本/模型/bge-small-zh-v1.5
huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir 世界设定规则/脚本/模型/bge-reranker-v2-m3

# 方式二：使用 Python 脚本
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
model.save("世界设定规则/脚本/模型/bge-small-zh-v1.5")

from sentence_transformers import CrossEncoder
model = CrossEncoder("BAAI/bge-reranker-v2-m3")
model.save("世界设定规则/脚本/模型/bge-reranker-v2-m3")
```

### 3. 数据库初始化

项目使用 LanceDB 作为向量数据库，数据库文件存放在 `世界设定规则/脚本/.lancedb_data/` 目录。

**首次使用**：
```bash
# 索引所有设定文件到数据库
python 世界设定规则/脚本/novel_kb.py index

# 校验索引完整性
python 世界设定规则/脚本/novel_kb.py verify --fix
```

**数据库维护**：
```bash
# 查看所有表
python 世界设定规则/脚本/novel_kb.py collections

# 清理缓存
python 世界设定规则/脚本/novel_kb.py cleanup

# 强制重建索引（文件变化后）
python 世界设定规则/脚本/novel_kb.py index --force
```

## 使用方法

### 写作前（必做）

```bash
# 1. 查询设定（关键词检索）
python 世界设定规则/脚本/novel_kb.py query "关键词"

# 2. 查看变量快照（金钱、物品、人物状态）
python 世界设定规则/脚本/novel_kb.py check

# 3. 查询+显示主角状态
python 世界设定规则/脚本/novel_kb.py query "关键词" --state
```

### 写作后（每章必做）

```bash
# 1. 一致性校验（9项自动校验）
python 世界设定规则/脚本/novel_kb.py check --chapter 第X章

# 2. 统计字数
python 世界设定规则/脚本/novel_kb.py wc 正文/第X章.md

# 3. 更新追踪文件（手动）
# - 世界设定规则/设定/章节追踪/章节追踪.yaml
# - 世界设定规则/设定/章节追踪/主角状态.yaml
# - 世界设定规则/设定/章节追踪/时间线.md
# - 世界设定规则/设定/章节追踪/钩子.md
# - 世界设定规则/设定/章节追踪/势力.md
```

### 卷结束时

```bash
# 归档当前卷数据
python 世界设定规则/脚本/novel_kb.py check --archive
```

## 写作流程

1. **写前准备**：查询知识库 + 查看变量快照
2. **初稿写作**：遵循白描铁律和AI痕迹禁区
3. **AI痕迹消除**：检测→分析→优化→重写（最大迭代6次）
4. **一致性校验**：9项自动校验
5. **更新追踪文件**：章节追踪.yaml + 主角状态.yaml + 时间线.md + 钩子.md + 势力.md

## 字数规则

- 最低基准线：2500字（绝对下限2300字）
- 写作目标：2800字（2600-3000字）
- 上限基准线：3500字（绝对上限3700字）
- 高潮/卷首/卷尾：可触及上限

## 技能文件

项目包含两个专用技能文件：

- `.claude/skills/AI小说写作技能.md` - 写作规则和技巧
- `.claude/skills/AI小说校验技能.md` - 校验检查清单

## 注意事项

### 写作规范
- 写作前必须查询知识库，不要直接读取设定文件
- `01_设定集/` 为只读，不可修改
- 渊印（货币标记）与本源印记（身份印记）禁止混用
- 每章写完后必须更新追踪文件

### 数据库相关
- 首次使用必须运行 `index` 建立索引
- 修改设定文件后运行 `index` 更新索引
- 数据库文件 `.lancedb_data/` 不要手动修改
- 如遇问题运行 `cleanup` 清理缓存

### 模型相关
- 模型文件存放在 `世界设定规则/脚本/模型/` 目录
- 模型文件较大（约 2.3GB），首次下载需要时间
- 如无 GPU，模型会自动使用 CPU（速度较慢）
- 模型加载后会缓存，后续使用更快

### 文件编码
- 所有文件使用 UTF-8 编码
- 文件名使用中文，路径中不要有特殊字符

## 许可证

私有项目，仅供个人使用。
