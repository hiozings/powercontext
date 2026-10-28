import { env, pipeline } from "@huggingface/transformers";

const MODEL_ID = "bge-small-zh-v1.5";
export const QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章：";

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.useBrowserCache = false;
env.localModelPath = "/models/";
env.backends.onnx.wasm.wasmPaths = "/ort/";
env.backends.onnx.wasm.numThreads = Math.max(
  1,
  Math.min(4, navigator.hardwareConcurrency || 2)
);

async function pickDevice() {
  if (!("gpu" in navigator)) {
    return { device: "wasm", reason: "此浏览器未暴露 WebGPU" };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) return { device: "wasm", reason: "未能获取 GPU 适配器" };
    return { device: "webgpu", reason: "" };
  } catch {
    return { device: "wasm", reason: "请求 GPU 适配器失败" };
  }
}

export async function createEmbedder({ onProgress, dtype = "q8" } = {}) {
  const preference = await pickDevice();
  const started = performance.now();
  let extractor;
  let device = preference.device;
  let reason = preference.reason;

  const progress_callback = (p) => {
    if (p.status === "progress" && p.file && onProgress) {
      onProgress({ file: p.file, progress: p.progress ?? 0 });
    }
  };

  if (device === "webgpu") {
    try {
      extractor = await pipeline("feature-extraction", MODEL_ID, {
        dtype,
        device: "webgpu",
        progress_callback,
      });
    } catch (err) {
      device = "wasm";
      reason = `WebGPU 初始化失败：${err?.message || "未知错误"}`;
    }
  }
  if (!extractor) {
    extractor = await pipeline("feature-extraction", MODEL_ID, {
      dtype,
      device: "wasm",
      progress_callback,
    });
  }

  const loadMs = performance.now() - started;
  const warmStart = performance.now();
  await extractor(["预热"], { pooling: "mean", normalize: true });
  const warmMs = performance.now() - warmStart;

  return {
    device,
    reason,
    loadMs,
    warmMs,
    async embed(texts) {
      const output = await extractor(texts, {
        pooling: "mean",
        normalize: true,
      });
      return output.tolist();
    },
  };
}
