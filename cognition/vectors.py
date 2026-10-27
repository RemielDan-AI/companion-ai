"""向量库：把每轮对话向量化，按语义检索相关记忆。

嵌入模型：bge-small-zh-v1.5（512 维，f16 GGUF），由 llama-server 以 CPU 模式提供，
默认 http://127.0.0.1:8081/v1/embeddings。

索引落盘在 cognition/vectors/embeddings.json（结构.txt 约定的向量库位置）。
规模到几千条前，暴力余弦足够快；再大再换 faiss/hnswlib。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import threading
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "cognition" / "vectors" / "embeddings.json"
EMBED_URL = "http://127.0.0.1:8081/v1/embeddings"


def embed_headers() -> dict:
    """嵌入请求的鉴权头（嵌入也允许走远程 API）。本地服务返回空 dict。"""
    try:
        from services import models_config
        return models_config.auth_headers("embed")
    except Exception:
        return {}


def embed_url() -> str:
    """嵌入地址：**尊重 `config/models.yaml` 里嵌入角色的端口**。

    ⚠ 原来这里是模块级常量，且被当成函数的默认参数（`url: str = EMBED_URL`）——
      默认参数在**定义时**求值，所以改配置根本不会生效。
      新加的函数是惰性的，调用时才算。
    """
    try:
        from services import models_config
        return models_config.embed_endpoint()
    except Exception:
        return EMBED_URL

try:  # numpy 有就用，没有也能跑
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


class EmbeddingUnavailable(RuntimeError):
    """嵌入服务不可用（检索可以退化为关键词匹配）。"""


def embed(texts: list[str], url: str = EMBED_URL, timeout: int = 60,
          normalize: bool = True) -> list[list[float]]:
    if not texts:
        return []
    payload = json.dumps({"input": texts, "model": "embedding"}, ensure_ascii=False)
    request = urllib.request.Request(
        url, data=payload.encode("utf-8"),
        # 嵌入也允许走远程 API（`source: api` → 云端 embedding），所以带上鉴权头
        headers={"Content-Type": "application/json", **embed_headers()},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise EmbeddingUnavailable(
            f"连不上嵌入服务 {url}（{exc}）。启动方式见 INSTALL.md（`python 启动模型.py`）。"
        ) from exc
    vectors = [item["embedding"] for item in body["data"]]
    if normalize:
        vectors = [_normalize(v) for v in vectors]
    return vectors


def health(url: str = EMBED_URL, timeout: int = 3) -> bool:
    """嵌入服务是否可用（不可用时检索会退化为关键词匹配）。"""
    base = url.split("/v1/")[0]
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout):
            return True
    except Exception:
        return False


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class VectorIndex:
    """id -> 向量 的简单索引，JSON 落盘。

    ⚠ 2026-10-15 加固（为了「索引回填」）：
      ① **加锁**：回填会在后台线程写索引，而检索同时在读。
         原来没有任何同步，`add_texts` 边改 `self.vectors` 边 `save()`，
         和 `search_vector` 遍历 keys 并发。
      ② **原子落盘**：原来 `save()` 是 `write_text`（先截断再写），
         并发 `load()` 可能读到**半截文件** → `json.loads` 抛错 →
         `load()` 里 `except: return` **静默失败** → 那个进程看到的是**空索引**
         （这比慢更糟：检索会以为一条向量都没有）。
         现在写临时文件再 `os.replace`（同盘原子替换）。
    """

    def __init__(self, path: pathlib.Path | str = INDEX_PATH):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dim = 0
        self.updated = ""
        self.vectors: dict[str, list[float]] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return
            self.dim = int(data.get("dim", 0))
            self.updated = data.get("updated", "")
            self.vectors = {str(k): v for k, v in (data.get("vectors") or {}).items()}

    def save(self) -> None:
        """原子落盘：写临时文件后 `os.replace`，避免并发读到半截。"""
        with self._lock:
            self.updated = dt.datetime.now().isoformat(timespec="seconds")
            payload = {"dim": self.dim, "updated": self.updated,
                       "vectors": self.vectors}
            text = json.dumps(payload, ensure_ascii=False)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.path)
            except Exception:
                # 退路：原子替换失败（权限/占用）就退回直接写，别把索引丢了
                self.path.write_text(text, encoding="utf-8")
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    def keys(self) -> set[str]:
        """当前索引里有哪些键（回填要拿它跟库里的条目比对）。"""
        with self._lock:
            return set(self.vectors.keys())

    def add(self, key: int | str, vector: list[float]) -> None:
        with self._lock:
            self.dim = len(vector)
            self.vectors[str(key)] = vector

    def add_texts(self, items: list[tuple[int | str, str]], url: str = EMBED_URL) -> int:
        """批量向量化并写入。返回写入条数。"""
        if not items:
            return 0
        vectors = embed([text for _, text in items], url=url)
        with self._lock:
            for (key, _), vector in zip(items, vectors):
                self.dim = len(vector)
                self.vectors[str(key)] = vector
            self.save()
        return len(items)

    def search_vector(self, query_vector: list[float], top_k: int = 5
                      ) -> list[tuple[str, float]]:
        with self._lock:
            if not self.vectors:
                return []
            query = _normalize(query_vector)
            items = list(self.vectors.items())
        if _np is not None:
            keys = [k for k, _ in items]
            matrix = _np.array([v for _, v in items], dtype="float32")
            q = _np.array(query, dtype="float32")
            sims = matrix @ q
            order = _np.argsort(-sims)[:top_k]
            return [(keys[i], float(sims[i])) for i in order]
        scored = []
        for key, vector in items:
            scored.append((key, sum(a * b for a, b in zip(query, vector))))
        scored.sort(key=lambda item: -item[1])
        return scored[:top_k]

    def search(self, query: str, top_k: int = 5, url: str = EMBED_URL
               ) -> list[tuple[str, float]]:
        vector = embed([query], url=url)[0]
        return self.search_vector(vector, top_k=top_k)

    def clear(self) -> int:
        """清空索引（调试用）。返回清掉了多少条。"""
        count = len(self.vectors)
        self.vectors = {}
        try:
            self.save()
        except Exception:
            pass
        return count

    def __len__(self) -> int:
        return len(self.vectors)
