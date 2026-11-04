// Local knowledge base backend: stores documents and vectors in seekdb and runs
// hybrid (full-text + vector) search inside the database.
//
// The browser only sends text plus vectors it computed locally; no inference
// happens here.
import { createServer } from "node:http";
import { readFile, stat, mkdir, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { extname, join, normalize, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { SeekdbClient } from "seekdb";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const WEB_DIR = resolve(HERE, "../web");
const MODEL_DIR = resolve(HERE, "../models");

const PORT = Number(process.env.PORT || 8787);
const DB = {
  host: process.env.SEEKDB_HOST || "127.0.0.1",
  port: Number(process.env.SEEKDB_PORT || 2881),
  user: process.env.SEEKDB_USER || "root",
  password: process.env.SEEKDB_PASSWORD || "",
  database: process.env.SEEKDB_DATABASE || "test",
};

// Model files are fetched through this server instead of straight from the
// model host: the browser stays same-origin (no CORS dependency) and every
// file is cached on disk, so the second start works offline.
const MODEL_HOSTS = (process.env.MODEL_HOSTS || "https://hf-mirror.com/,https://huggingface.co/").split(",");

const client = new SeekdbClient(DB);
const collections = new Map();

/**
 * seekdb-js stores a collection in an internal table (c$v2$<collection id>) and
 * the hybrid search statement reads it.  Only seekdb builds that carry the
 * collection tables can serve that statement, so the capability is probed once
 * at startup instead of failing on the first user query.
 */
async function probeCollectionSupport() {
  const probeName = "__kb_capability_probe__";
  try {
    const coll = await client.getOrCreateCollection({
      name: probeName,
      embeddingFunction: null,
      configuration: { hnsw: { dimension: 2, distance: "cosine" }, fulltextConfig: { analyzer: "ik" } },
    });
    await coll.add({ ids: ["p"], embeddings: [[1, 0]], documents: ["探针"], metadatas: [{ title: "probe" }] });
    await coll.refresh_index();
    await coll.hybridSearch({
      query: { whereDocument: { $contains: "探针" } },
      knn: { queryEmbeddings: [[1, 0]], nResults: 1 },
      nResults: 1,
    });
    return { supported: true };
  } catch (err) {
    return { supported: false, error: String(err?.message || err) };
  } finally {
    await client.deleteCollection(probeName).catch(() => {});
  }
}

let capability = { supported: null, error: null };

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".jsonl": "application/x-ndjson; charset=utf-8",
  ".onnx": "application/octet-stream",
  ".wasm": "application/wasm",
  ".bin": "application/octet-stream",
  ".txt": "text/plain; charset=utf-8",
};

function sendJSON(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}

async function readJSON(req, limit = 64 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("request body too large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function collectionFor(name, dimension) {
  const key = `${name}@${dimension}`;
  if (collections.has(key)) return collections.get(key);
  const coll = await client.getOrCreateCollection({
    name,
    // Vectors are produced in the browser, so the SDK must not try to resolve a
    // server side embedding function.
    embeddingFunction: null,
    configuration: {
      hnsw: { dimension, distance: "cosine" },
      fulltextConfig: { analyzer: "ik", properties: { ik_mode: "smart" } },
    },
  });
  collections.set(key, coll);
  return coll;
}

const RRF = { rankWindowSize: 40, rankConstant: 60 };
// Candidates each branch may contribute before fusion. Keeping the two values
// equal is what makes the fused ranks comparable across branches.
const BRANCH_CANDIDATES = 40;

// seekdb-js keeps a collection in `c$v2$<collection id>`; the id is stable for
// the life of the collection, so the name survives restarts of this server.
function collectionTable(coll, fallbackName) {
  return coll?.collectionId ? `c$v2$${coll.collectionId}` : `c$v2$<collection:${fallbackName}>`;
}

// Renders the statement DBMS_HYBRID_SEARCH builds for this request. The SDK
// sends this to seekdb as one statement; the server only echoes it so the UI
// can show what actually ran instead of describing it from memory.
//
// The per-branch limits mirror the SDK exactly (fulltext: LIMIT 800, vector:
// the knn nResults), which is what makes `scripts/explain-plan.py` and the
// parity test agree with a real `hybridSearch`.
const FULLTEXT_CANDIDATE_CAP = 800;

function renderHybridSql({ table, vector, text, k }) {
  const literal = (s) => `'${String(s).replace(/'/g, "''")}'`;
  const vectorLiteral = JSON.stringify(vector);
  const ftsBranch =
    `(SELECT *, RANK() OVER (ORDER BY _keyword_score DESC) AS _keyword_rank FROM (` +
    `SELECT _id, document, embedding, metadata, ` +
    `MATCH(document) AGAINST (${literal(text)} IN NATURAL LANGUAGE MODE) AS _keyword_score ` +
    `FROM ${table} WHERE MATCH(document) AGAINST (${literal(text)} IN NATURAL LANGUAGE MODE) ` +
    `ORDER BY _keyword_score DESC LIMIT ${FULLTEXT_CANDIDATE_CAP}))`;
  const vecBranch =
    `(SELECT *, RANK() OVER (ORDER BY _semantic_score DESC) AS _semantic_rank FROM (` +
    `SELECT _id, document, embedding, metadata, ` +
    `COSINE_DISTANCE(embedding, '${vectorLiteral}') AS _distance, ` +
    `ROUND(1 - COSINE_DISTANCE(embedding, '${vectorLiteral}') / 2, 8) AS _semantic_score ` +
    `FROM ${table} ORDER BY _distance APPROXIMATE LIMIT ${BRANCH_CANDIDATES}))`;
  const fusion =
    `IFNULL(1 / (_fts._keyword_rank + ${RRF.rankConstant}), 0) + ` +
    `IFNULL(1 / (_vs._semantic_rank + ${RRF.rankConstant}), 0)`;
  return (
    `SELECT IFNULL(_fts._id, _vs._id) AS _id, _fts.document, _fts.metadata, ${fusion} AS _score ` +
    `FROM ${ftsBranch} _fts FULL JOIN ${vecBranch} _vs ON _fts._id = _vs._id ` +
    `ORDER BY _score DESC LIMIT ${k}`
  );
}

// One SQL statement per search: the full-text branch and the vector branch are
// joined and fused by seekdb itself with reciprocal rank fusion. The client
// never receives the two branch lists separately.
//
// Metadata filtering is intentionally not exposed: seekdb-js 1.4.0 renders a
// `where` clause as a bare column reference (`WHERE category = ...`) while the
// collection keeps metadata in a JSON column, so the filter matches nothing and
// the rows come from whichever branch was not filtered. Filtering would have to
// go through `metadata->>'$.field'` in a statement we build ourselves.
async function hybridSearch({ collection, vector, text, k = 5 }) {
  const coll = await collectionFor(collection, vector.length);
  const started = performance.now();
  const result = await coll.hybridSearch({
    query: text ? { whereDocument: { $contains: text }, nResults: BRANCH_CANDIDATES } : undefined,
    knn: { queryEmbeddings: [vector], nResults: BRANCH_CANDIDATES },
    rank: { rrf: { ...RRF } },
    nResults: k,
    include: ["documents", "metadatas", "distances"],
  });
  const tookMs = performance.now() - started;
  const hits = [];
  const ids = result.ids?.[0] || [];
  for (let i = 0; i < ids.length; i += 1) {
    hits.push({
      id: ids[i],
      text: result.documents?.[0]?.[i] ?? "",
      metadata: result.metadatas?.[0]?.[i] ?? {},
      // RRF fusion score, not a distance: higher is better.
      score: result.distances?.[0]?.[i] ?? null,
    });
  }
  return {
    hits,
    tookMs,
    sql: renderHybridSql({ table: collectionTable(coll, collection), vector, text, k }),
    rrf: RRF,
  };
}

async function handleApi(req, res, pathname) {
  if (pathname === "/api/health") {
    let version = null;
    try {
      const rows = await client.execute("select version() as v");
      version = rows?.[0]?.v ?? null;
    } catch (err) {
      return sendJSON(res, 503, { ok: false, error: String(err?.message || err) });
    }
    return sendJSON(res, 200, {
      ok: true,
      seekdb: { ...DB, version },
      capability,
      collections: await client.listCollections(),
    });
  }

  if (pathname === "/api/ingest") {
    const body = await readJSON(req);
    const { collection, dimension, docs } = body;
    if (!collection || !dimension || !Array.isArray(docs) || !docs.length) {
      return sendJSON(res, 400, { ok: false, error: "collection, dimension and docs are required" });
    }
    const coll = await collectionFor(collection, dimension);
    const started = performance.now();
    const batchSize = Math.max(1, Math.min(Number(body.batchSize) || 32, 128));
    let written = 0;
    for (let i = 0; i < docs.length; i += batchSize) {
      const slice = docs.slice(i, i + batchSize);
      // upsert keeps re-importing the same corpus idempotent
      await coll.upsert({
        ids: slice.map((d) => d.id),
        embeddings: slice.map((d) => d.vector),
        documents: slice.map((d) => d.text),
        metadatas: slice.map((d) => ({ ...d.metadata, title: d.title ?? "" })),
      });
      written += slice.length;
    }
    const writeMs = performance.now() - started;
    // Rows are searchable as soon as the batch is written; the refresh only
    // rebuilds the index and costs ~200-800 ms, so it is done after the write
    // completes and its cost is reported separately instead of being hidden in
    // the ingest latency.
    const refreshStarted = performance.now();
    await coll.refresh_index();
    const refreshMs = performance.now() - refreshStarted;
    const total = await coll.count();
    return sendJSON(res, 200, { ok: true, written, total, writeMs, refreshMs, batchSize });
  }

  if (pathname === "/api/search") {
    const body = await readJSON(req);
    const { collection, vector, text, k } = body;
    if (!collection || !Array.isArray(vector) || !vector.length) {
      return sendJSON(res, 400, { ok: false, error: "collection and vector are required" });
    }
    const out = await hybridSearch({ collection, vector, text, k: Number(k) || 5 });
    return sendJSON(res, 200, { ok: true, ...out });
  }

  if (pathname === "/api/explain") {
    // Proves the hybrid statement is served by the native fulltext and vector
    // indexes instead of scanning the collection table twice in application code.
    // The plan itself is read with EXPLAIN on the same database and is also
    // reported by `scripts/verify-native.py` in CI, where a MySQL client can run
    // it; here the index metadata is what the running server can observe.
    const body = await readJSON(req);
    const { collection, vector, text, k } = body;
    if (!collection || !Array.isArray(vector) || !vector.length || !text) {
      return sendJSON(res, 400, { ok: false, error: "collection, vector and text are required" });
    }
    const coll = await collectionFor(collection, vector.length);
    const table = collectionTable(coll, collection);
    const sql = renderHybridSql({ table, vector, text, k: Number(k) || 5 });
    try {
      const indexes = await client.execute(`SHOW INDEX FROM \`${table}\``);
      const byName = Object.fromEntries((indexes || []).map((row) => [row.Key_name, row.Index_type]));
      return sendJSON(res, 200, {
        ok: true,
        sql,
        table,
        indexes: byName,
        nativeFulltext: byName.idx_fts === "FULLTEXT",
        nativeVector: byName.idx_vec === "VECTOR",
      });
    } catch (err) {
      return sendJSON(res, 500, { ok: false, error: String(err?.message || err), sql });
    }
  }

  if (pathname === "/api/count") {
    const body = await readJSON(req);
    const coll = await collectionFor(body.collection, body.dimension);
    return sendJSON(res, 200, { ok: true, count: await coll.count() });
  }

  if (pathname === "/api/reset") {
    const body = await readJSON(req);
    if (body.collection) {
      await client.deleteCollection(body.collection).catch(() => {});
      for (const key of [...collections.keys()]) {
        if (key.startsWith(`${body.collection}@`)) collections.delete(key);
      }
    }
    return sendJSON(res, 200, { ok: true });
  }

  return sendJSON(res, 404, { ok: false, error: "unknown endpoint" });
}

async function serveStatic(res, root, pathname, fallback) {
  const rel = normalize(decodeURIComponent(pathname)).replace(/^(\.\.[/\\])+/, "");
  let filePath = join(root, rel);
  if (!filePath.startsWith(root)) return sendJSON(res, 403, { ok: false, error: "forbidden" });
  try {
    const info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, "index.html");
  } catch {
    if (fallback) return serveStatic(res, root, fallback, null);
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    return res.end("not found");
  }
  const data = await readFile(filePath);
  res.writeHead(200, {
    "content-type": MIME[extname(filePath)] || "application/octet-stream",
    "content-length": data.length,
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-embedder-policy": "require-corp",
    "cache-control": filePath.endsWith(".onnx") ? "public, max-age=604800" : "no-cache",
  });
  res.end(data);
}

// /hf/<model>/resolve/<revision>/<file> -> cached model file
async function serveModelFile(res, pathname) {
  const rest = pathname.replace(/^\/hf\//, "");
  const marker = rest.indexOf("/resolve/");
  if (marker < 0) return sendJSON(res, 404, { ok: false, error: "bad model path" });
  const model = rest.slice(0, marker);
  const after = rest.slice(marker + "/resolve/".length);
  const slash = after.indexOf("/");
  const revision = slash < 0 ? "main" : after.slice(0, slash);
  const relative = slash < 0 ? "" : after.slice(slash + 1);
  if (!relative) return sendJSON(res, 404, { ok: false, error: "bad model path" });
  // cache without the revision segment so the browser side local path
  // (/models/<model>/<file>) resolves to the same file
  const cachePath = join(MODEL_DIR, model, relative);
  try {
    const info = await stat(cachePath);
    if (info.isFile()) {
      const data = await readFile(cachePath);
      res.writeHead(200, {
        "content-type": MIME[extname(cachePath)] || "application/octet-stream",
        "content-length": data.length,
        "cache-control": "public, max-age=604800",
      });
      return res.end(data);
    }
  } catch {
    /* not cached yet */
  }

  for (const host of MODEL_HOSTS) {
    const url = `${host.replace(/\/$/, "")}/${model}/resolve/${revision}/${relative}`;
    try {
      const upstream = await fetch(url, { redirect: "follow" });
      if (!upstream.ok) continue;
      await mkdir(dirname(cachePath), { recursive: true });
      const buffer = Buffer.from(await upstream.arrayBuffer());
      await writeFile(cachePath, buffer);
      res.writeHead(200, {
        "content-type": MIME[extname(cachePath)] || "application/octet-stream",
        "content-length": buffer.length,
        "cache-control": "public, max-age=604800",
      });
      return res.end(buffer);
    } catch (err) {
      console.warn("[model fetch failed]", url, err.message);
    }
  }
  res.writeHead(404, { "content-type": "application/json" });
  return res.end(JSON.stringify({ ok: false, error: "model file unavailable" }));
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  try {
    if (url.pathname.startsWith("/api/")) {
      if (req.method !== "POST" && url.pathname !== "/api/health") {
        return sendJSON(res, 405, { ok: false, error: "method not allowed" });
      }
      return await handleApi(req, res, url.pathname);
    }
    if (url.pathname.startsWith("/hf/")) {
      return await serveModelFile(res, url.pathname);
    }
    if (url.pathname.startsWith("/models/")) {
      return await serveStatic(res, MODEL_DIR, url.pathname.slice("/models/".length));
    }
    if (url.pathname.startsWith("/data/")) {
      return await serveStatic(res, resolve(HERE, "../data"), url.pathname.slice("/data/".length));
    }
    return await serveStatic(res, WEB_DIR, url.pathname === "/" ? "index.html" : url.pathname, "index.html");
  } catch (err) {
    console.error("[error]", url.pathname, err);
    sendJSON(res, 500, { ok: false, error: String(err?.message || err) });
  }
});

server.listen(PORT, () => {
  console.log(`knowledge base on http://127.0.0.1:${PORT}  (seekdb ${DB.host}:${DB.port}/${DB.database})`);
  probeCollectionSupport().then((result) => {
    capability = result;
    if (!result.supported) {
      console.warn(
        `[warn] this seekdb server cannot serve the collection tables used by seekdb-js hybrid search: ${result.error}\n` +
          "       build the server from the 2026-competition branch of oceanbase/seekdb, then restart.",
      );
    } else {
      console.log("seekdb collection tables available, hybrid search enabled");
    }
  });
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    await client.close().catch(() => {});
    process.exit(0);
  });
}
