/* 站内演示语料：虚构的样例文档，仅用于展示检索交互与排序逻辑。 */
window.XZ = {
  docs: [
    {
      code: "A",
      title: "长文档问答中的分块边界与上下文割裂",
      year: 2024,
      venue: "样例论文集",
      kind: "论文",
      tags: ["长文档", "切块", "RAG"],
      chunks: [
        {
          page: 3,
          text: "固定长度切块会把一个完整的论证拆成两半。当答案横跨两个相邻片段时，单块召回往往只能取回半句话，生成结果因此出现方向正确、细节缺失的回答。",
          concepts: { longdoc: 0.9, chunking: 0.8, boundary: 0.9, retrieval: 0.5, citation: 0.1, evaluation: 0.2 }
        },
        {
          page: 4,
          text: "一个实用的做法是加入 10%–20% 的重叠窗口，并优先在段落或标题的边界处切分。重叠会增加索引体积，但能明显降低跨块答案的遗漏率。",
          concepts: { longdoc: 0.7, chunking: 0.9, boundary: 1.0, retrieval: 0.4, memory: 0.1, evaluation: 0.2 }
        },
        {
          page: 6,
          text: "评估切块策略时不要只看命中率，还要记录答案完整性（是否包含全部关键句）和引用准确率（页码是否指向真正的出处）。",
          concepts: { evaluation: 0.9, citation: 0.9, chunking: 0.7, longdoc: 0.5, retrieval: 0.4 }
        }
      ]
    },
    {
      code: "B",
      title: "混合检索：向量召回与关键词召回的融合",
      year: 2023,
      venue: "样例技术报告",
      kind: "报告",
      tags: ["混合检索", "向量", "BM25"],
      chunks: [
        {
          page: 2,
          text: "稠密向量擅长语义相近但措辞不同的表达，例如把「召回率」与 recall 对齐；BM25 则对专有名词、缩写和精确短语更可靠。",
          concepts: { hybrid: 1.0, retrieval: 0.8, evaluation: 0.2, longdoc: 0.2 }
        },
        {
          page: 5,
          text: "常见的融合方式是分别取 top-k，再把两路分数归一化后加权求和。权重需要按语料调整，代码类语料通常更依赖关键词信号。",
          concepts: { hybrid: 1.0, retrieval: 0.7, evaluation: 0.3, citation: 0.1 }
        },
        {
          page: 7,
          text: "三路召回（向量、全文、元数据过滤）比两路更容易解释：当一条结果只由语义命中时，可以明确告诉用户它来自哪一路信号。",
          concepts: { hybrid: 0.9, retrieval: 0.9, citation: 0.3, evaluation: 0.2 }
        }
      ]
    },
    {
      code: "C",
      title: "RAG 系统的评估：召回、忠实度与延迟",
      year: 2024,
      venue: "样例课程笔记",
      kind: "笔记",
      tags: ["RAG", "评估", "指标"],
      chunks: [
        {
          page: 1,
          text: "评估 RAG 至少要看三层：检索层看召回率与命中位置，生成层看忠实度与引用一致性，系统层看端到端延迟与成本。",
          concepts: { evaluation: 1.0, retrieval: 0.7, citation: 0.7, longdoc: 0.3 }
        },
        {
          page: 4,
          text: "只看最终答案的流畅度会掩盖检索失败。一个常用的诊断方法是固定生成模型，只替换检索结果，观察答案如何变化。",
          concepts: { evaluation: 0.9, retrieval: 0.7, longdoc: 0.4 }
        }
      ]
    },
    {
      code: "D",
      title: "组会笔记：检索增强项目 10–14 周",
      year: 2025,
      venue: "个人笔记",
      kind: "笔记",
      tags: ["组会", "baseline", "进度"],
      chunks: [
        {
          page: 1,
          text: "第 10 周：baseline 确定为 BM25 + bge-small 向量，top-20 召回后重排到 top-5；数据先用 3 篇综述加 12 篇实验论文。",
          concepts: { meeting: 1.0, retrieval: 0.5, hybrid: 0.5, memory: 0.3, longdoc: 0.2 }
        },
        {
          page: 2,
          text: "老师建议：先把 chunk 边界和评估指标定下来，再调模型；否则每次改动都无法判断是检索变好，还是生成变好。",
          concepts: { meeting: 0.9, chunking: 0.6, evaluation: 0.6, retrieval: 0.4, memory: 0.2 }
        }
      ]
    },
    {
      code: "E",
      title: "实验记录：chunk size 与重叠对召回的影响",
      year: 2025,
      venue: "实验日志",
      kind: "日志",
      tags: ["实验", "切块", "召回"],
      chunks: [
        {
          page: 1,
          text: "设置：chunk size 取 256、512、768，overlap 取 0、64、128，固定向量模型与 top-k=10。",
          concepts: { meeting: 0.3, chunking: 0.8, retrieval: 0.5, evaluation: 0.5 }
        },
        {
          page: 2,
          text: "初步结果：512/64 在长文档问答上召回最稳；256 的片段过短，答案常被拆散；768 的片段噪声变多，重排压力上升。",
          concepts: { chunking: 0.9, boundary: 0.8, longdoc: 0.8, retrieval: 0.6, evaluation: 0.5 }
        }
      ]
    }
  ],

  profile: {
    topic: "RAG 检索增强生成",
    methods: ["混合检索", "重排"],
    focus: ["切块边界", "召回评估"],
    read: 3,
    total: 5
  },

  /* 演示用记忆：当研究记忆开启时，这些同义表达会被加入查询。 */
  expansions: {
    "切块": ["chunk", "分块", "片段"],
    "分块": ["chunk", "切块"],
    "边界": ["overlap", "重叠", "boundary"],
    "重叠": ["overlap", "边界"],
    "评估": ["evaluation", "忠实度", "指标"],
    "指标": ["metric", "评估"],
    "融合": ["hybrid", "混合", "fusion"],
    "混合": ["hybrid", "融合"],
    "召回": ["recall", "检索"],
    "检索": ["recall", "召回"],
    "baseline": ["基线", "对照"],
    "组会": ["会议", "笔记", "进度"],
    "实验": ["日志", "设置", "记录"],
    "长文档": ["长文本", "long document"],
    "出处": ["页码", "引用", "citation"],
    "记忆": ["偏好", "进度", "profile"]
  },

  /* 查询概念 → 语义维度，用于浏览器内的简化语义相似度。 */
  termConcepts: [
    { terms: ["长文档", "长文本", "long document"], c: { longdoc: 1, chunking: 0.2 } },
    { terms: ["切块", "分块", "chunk", "chunking", "片段"], c: { chunking: 1 } },
    { terms: ["边界", "boundary"], c: { boundary: 1, chunking: 0.4 } },
    { terms: ["重叠", "overlap"], c: { boundary: 0.9, chunking: 0.6 } },
    { terms: ["评估", "评测", "指标", "evaluation", "metric", "忠实度", "faithfulness"], c: { evaluation: 1, citation: 0.3 } },
    { terms: ["融合", "混合", "hybrid", "fusion", "向量", "vector", "bm25"], c: { hybrid: 1, retrieval: 0.5 } },
    { terms: ["召回", "检索", "retrieval", "recall", "命中"], c: { retrieval: 1 } },
    { terms: ["出处", "页码", "引用", "citation"], c: { citation: 1 } },
    { terms: ["组会", "会议", "meeting", "baseline", "基线", "进度", "实验"], c: { meeting: 1, memory: 0.2 } },
    { terms: ["记忆", "偏好", "memory", "profile"], c: { memory: 1 } },
    { terms: ["rag"], c: { retrieval: 0.8, evaluation: 0.3 } }
  ]
};
