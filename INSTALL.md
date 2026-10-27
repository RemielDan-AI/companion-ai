# 安装说明

从零到能对话，大约 10 分钟（模型下载时间不算）。

系统要求：**Python 3.10+**（开发和测试用的是 3.13）、Windows（界面用 Tk，Linux/macOS 理论上能跑，但没测过）。

---

## 一、装依赖

```bash
git clone https://github.com/RemielDan-AI/companion-ai.git
cd companion-ai
python -m venv .venv
.venv\Scripts\activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt
```

可选依赖：

```bash
pip install -r requirements-optional.txt
```

| 不装 | 后果 |
|---|---|
| `numpy` | 记忆检索退化成纯词法（BM25），能用，召回差一些 |
| `edge-tts` | 语音功能不可用，文字对话不受影响 |

**装了 `requirements.txt` 就能启动界面了**——下面第二节是为了让它真的能说话。

---

## 二、准备模型（llama.cpp 的 GGUF）

本项目**不包含任何模型**。需要你自己下 GGUF 文件。

### 1. 装 llama.cpp

到 [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases) 下载 Windows 预编译包，
把 `llama-server.exe` 放到 `PATH` 里（或随便一个目录，启动器会用 `llama-server` 这个名字去找它）。

### 2. 放模型文件

把 GGUF 放在**任何一个目录**里，用环境变量指过去：

```bash
set COMPANION_MODEL_DIRS=D:\models;E:\more-models     # Windows（多个用 ; 分隔）
export COMPANION_MODEL_DIRS=/home/me/models           # Linux/macOS
```

不设这个变量时，默认找 `~/ai/models` 和 `./models`。

目录结构随意，能扫到 `*.gguf` 就行，例如：

```
D:\models\
  weiyang-v1.5-Q4_K_M\weiyang-v1.5-Q4_K_M.gguf
  Qwen3.5-2B\Qwen3.5-2B-Q4_K_M.gguf
  bge-small-zh-v1.5\bge-small-zh-v1.5-f16.gguf
```

### 3. 在配置里指定用哪个

编辑 `config/models.yaml`，把每个角色的 `model:` 改成你那个文件名里的**关键词**：

```yaml
roles:
  chat:
    model: Qwen3-4B          # 只要能唯一匹配到一个 .gguf 就行
    port: 8080
    ngl: 20                  # 放进显存的层数；显存小就调小
```

五个角色（对话 / 嵌入 / 记忆 / 思考 / 判官）都可以用同一个模型——
它们会**自动复用同一个服务**，不会重复起进程。

> 显存紧张的话可以只配 `chat` + `embed` 两个，其它留空。
> 参考：4 GB 显存上五个服务全开是**放不下**的（实测只剩 30 MB），
> 详见 `docs/设计取舍与实测.md` 第 2 条。

---

## 三、启动

### 1. 起模型服务

```bash
python 启动模型.py            # 交互式：选模型 → 改参数 → 启动
```

其它用法：

```bash
python 启动模型.py --list                  # 看看扫到了哪些模型和预设
python 启动模型.py --model Qwen3-4B --port 8080 --ngl 20
python 启动模型.py --status                # 看正在跑的服务
python 启动模型.py --stop 8080             # 停掉某个端口
```

> ⚠ 这个窗口要**一直开着**：关掉它（或 Ctrl+C）会顺手把服务停掉，
> 不会留下后台残留进程。

### 2. 开界面

```bash
python gui.py
```

或者双击 `启动界面.bat`。

---

## 四、不配模型也能开界面

可以。界面能起，只是**发消息会提示连不上模型服务**。

嵌入服务没起时，记忆检索自动退化成关键词匹配——**照常能用**，只是召回质量差一些。

所以想先看看界面长什么样，跳过第二节直接 `python gui.py` 就行。

---

## 五、立绘（可选）

仓库里**不含任何立绘素材**（授权原因）。不放图时界面用的是代码里自绘的矢量形象，功能不受影响。

想换成自己的图：

1. 把图片（`.png` / `.webp` / `.jpg`）放到**项目根目录**，或者 `assets/`、`头像/`、`立绘/` 目录
2. 或者在 `config/gui.yaml` 里指定：
   ```yaml
   character_asset: 我的立绘.png
   skin_dir: ""            # 皮肤包目录；留空 = 不找
   ```

命名里带「头像 / 立绘 / avatar」的图会被优先选中。
白底图会自动抠掉（按边缘泛洪，不误伤浅色主体）。

---

## 六、语音（可选）

- **Edge TTS**：装了 `edge-tts` 就能用（在线），界面里选音色即可
- **本地 GSV / GPT-SoVITS**：本项目不内置、不启动，需要你自己起服务
  （默认连 `127.0.0.1:9880`，在 `config/voice.yaml` 里改）

---

## 七、跑测试

```bash
python tests/run_all.py --quick      # 离线用例，不需要模型，约 1 分钟
python tests/run_all.py              # 全量，部分用例需要本地模型/服务
```

有些用例在**没有模型 / 没有训练样本 / 没有立绘**时会打印 `⏭ 跳过 xxx` 并算通过——
这是设计如此（这些是部署产物，不该要求每个 clone 都有）。

---

## 八、常见问题

**Q：发消息没反应，或者提示连不上服务**

模型服务没起。先 `python 启动模型.py --status` 看看，或者界面右上角的服务状态。
另外注意：本机**端口没开**时，`connect` 不会立刻失败，而是**挂满整个超时**
（实测 1503 ms）——所以探活都设了 0.35 s 超时，偶尔显示"未连接"是正常的，
点一下重试即可。

**Q：`--list` 扫不到模型**

`COMPANION_MODEL_DIRS` 没设对，或者文件名不是 `.gguf`。
试 `python 启动模型.py --list` 看它到底扫了哪些目录。

**Q：显存不够 / 爆内存**

把 `config/models.yaml` 里各角色的 `ngl` 调小（0 = 纯 CPU），
或者干脆只留 `chat` + `embed`。
`ngl` 不是越大越好：实测 2B 模型在 `ngl=12` 最快，再往上是负收益
（层在 GPU/CPU 来回搬比多算几层还贵）。

**Q：界面字体是方框 / 乱码**

在「设置 → 字体」里换一个本机装了的中文字体（微软雅黑 / 思源黑体都行）。

**Q：桌宠怎么联动**

见 `桌宠联动协议.md`。

---

## 配置在哪

全部在 `config/` 下，都是带注释的 YAML，改完**重启程序**生效
（`config/replies.yaml` 除外——它按文件修改时间热加载，改完存盘下一次说话就生效）：

| 文件 | 管什么 |
|---|---|
| `models.yaml` | 五个角色各用哪个模型、端口、ngl、ctx |
| `persona.yaml` | 人物设定（名字 / 关系 / 语气） |
| `replies.yaml` | **固定话术**（数组，每次随机取一句） |
| `style.yaml` | 回复风格与长度约束 |
| `values.yaml` | 价值观与边界 |
| `memory.yaml` | 记忆的抽取 / 衰减 / 注入 |
| `thinking.yaml` | 思考档位与 token 上限 |
| `gui.yaml` | 界面：立绘、字号、皮肤目录 |
| `voice.yaml` | 语音服务地址与音色 |
| `api.yaml` | **云端 API（默认全关）**，见下 |

## 关于云端 API

`config/api.yaml` 默认 `enabled: false`，**所有外发一律拒绝**。

要用得自己在设置里打开，并且要过外发网关——`profile / memory / values /
history / persona` 这些字段在网关里是**写死禁止外发**的。
换句话说：打开云端 API 会发出去的是**你的原话和未央的回复**，
不是你的画像和记忆库。

详见 `cloud/gateway.py`（那里有完整的禁止清单）。
