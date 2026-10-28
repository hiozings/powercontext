"""向量化：默认用可离线复现的概念向量，可选接入 fastembed 本地模型。"""

import math
import os

CONCEPTS = [
    "longdoc",
    "chunking",
    "boundary",
    "evaluation",
    "hybrid",
    "retrieval",
    "citation",
    "meeting",
    "memory",
    "rag",
]


def _unit(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _literal(vec):
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


class ConceptEmbedder:
    """把文本映射到 10 个主题维度。

    草稿阶段没有可用的 Embedding API，这里用人工标注的主题权重代替模型输出，
    结果确定、可离线复现。换成真实模型时只需实现同样的 `query_vector` / `doc_vector`。
    """

    name = "concept-10d"
    dim = len(CONCEPTS)

    def __init__(self, data):
        self._terms = data["termConcepts"]

    def doc_vector(self, concepts):
        return _literal(_unit([float(concepts.get(c, 0.0)) for c in CONCEPTS]))

    def query_vector(self, query, expanded_terms=()):
        vec = [0.0] * self.dim
        query_l = query.lower()
        expanded_l = " ".join(expanded_terms).lower()
        for entry in self._terms:
            for term in entry["terms"]:
                if term in query_l:
                    for key, weight in entry["c"].items():
                        vec[CONCEPTS.index(key)] += weight
                    break
                if expanded_l and term in expanded_l:
                    for key, weight in entry["c"].items():
                        vec[CONCEPTS.index(key)] += weight * 0.5
                    break
        return _literal(_unit(vec))


class FastEmbedder:
    name = "fastembed"
    _MODEL = "BAAI/bge-small-zh-v1.5"

    def __init__(self):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(self._MODEL)
        self.dim = len(next(iter(self._model.embed(["dimension probe"]))))

    def doc_vector(self, text):
        return _literal(_unit(next(iter(self._model.embed([text]))).tolist()))

    def query_vector(self, query, expanded_terms=()):
        text = query if not expanded_terms else query + " " + " ".join(expanded_terms)
        return _literal(_unit(next(iter(self._model.embed([text]))).tolist()))


def build_embedder(data):
    if os.environ.get("EMBEDDER", "concept") == "fastembed":
        try:
            return FastEmbedder()
        except Exception as exc:  # 模型缺失时退回离线实现
            print(f"[embedding] fastembed 不可用，改用概念向量：{exc}")
    return ConceptEmbedder(data)
