import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEST = path.join(ROOT, "web/public/models/bge-small-zh-v1.5");
const BASE =
  process.env.MODEL_BASE ||
  "https://modelscope.cn/models/Xenova/bge-small-zh-v1.5/resolve/master";
const WANT_FP32 = process.argv.includes("--fp32");

const SMALL_FILES = [
  "config.json",
  "tokenizer.json",
  "tokenizer_config.json",
  "special_tokens_map.json",
  "vocab.txt",
];

// 上游 ONNX 导出的实际文件信息，用于校准与体积对比。
const KNOWN = {
  q8: {
    path: "onnx/model_quantized.onnx",
    bytes: 24010842,
    sha256: "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc",
  },
  fp32: {
    path: "onnx/model.onnx",
    bytes: 94851877,
    sha256: "69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a",
  },
};

async function sha256(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    fs.createReadStream(file)
      .on("data", (chunk) => hash.update(chunk))
      .on("end", () => resolve(hash.digest("hex")))
      .on("error", reject);
  });
}

async function download(rel) {
  const out = path.join(DEST, rel);
  if (fs.existsSync(out) && fs.statSync(out).size > 0) {
    console.log(`skip ${rel}`);
    return out;
  }
  const url = `${BASE}/${rel}`;
  console.log(`get  ${rel}`);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const tmp = `${out}.part`;
  fs.writeFileSync(tmp, Buffer.from(await res.arrayBuffer()));
  fs.renameSync(tmp, out);
  return out;
}

const manifest = {
  model: "BAAI/bge-small-zh-v1.5",
  dim: 512,
  source: BASE,
  files: {},
};

for (const rel of SMALL_FILES) await download(rel);

for (const [key, known] of Object.entries(KNOWN)) {
  if (key === "fp32" && !WANT_FP32) {
    manifest.files[key] = { ...known, downloaded: false };
    continue;
  }
  const file = await download(known.path);
  const bytes = fs.statSync(file).size;
  const digest = await sha256(file);
  if (digest !== known.sha256) {
    throw new Error(`${known.path} sha256 不匹配：${digest}`);
  }
  manifest.files[key] = { path: known.path, bytes, sha256: digest };
  console.log(`ok   ${known.path} ${(bytes / 1048576).toFixed(1)} MB`);
}

fs.writeFileSync(
  path.join(DEST, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`
);
console.log(
  `写入 ${path.relative(ROOT, path.join(DEST, "manifest.json"))}；FP32 ${
    WANT_FP32 ? "已下载" : "未下载，可用 --fp32 获取"
  }`
);
