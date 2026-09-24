// @appbuilder-api-dev-server-v1 -- auto-injected by deploy pipeline.
// Loads real api/*.ts handler files into the Vite dev server so that
// `npm run dev` uses the exact same handler code Vercel runs in
// production. Do not edit by hand; the pipeline will replace it on
// the next deploy.

import { existsSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { HandlerRequest, HandlerResponse } from "@appdirect/auth-bff/handlers";
import { loadEnv, type Plugin, type ViteDevServer } from "vite";

const API_ROOT = resolve(process.cwd(), "api");

/**
 * Hydrate `process.env` from `.env*` files on Vite dev start.
 *
 * Rationale: our auto-emitted `api/*` handlers and
 * `api/_lib/supabase-admin.js` read `process.env.SUPABASE_URL` /
 * `SUPABASE_SERVICE_ROLE_KEY` the same way they do under Vercel's
 * runtime in production. Vite, however, only exposes `.env` values
 * via `import.meta.env` (`VITE_`-prefixed in the client bundle); it
 * never populates `process.env` with non-VITE keys. Without this
 * step, `npm run dev` sees `process.env.SUPABASE_URL === undefined`
 * and the admin-client module throws "Missing SUPABASE_URL or
 * SUPABASE_SERVICE_ROLE_KEY" at first request.
 *
 * Idempotent: existing values in `process.env` (shell exports, CI
 * secrets) win over `.env`, matching Vite's and Vercel's own
 * precedence rules.
 */
function hydrateProcessEnvFromDotEnv(mode: string): void {
  const env = loadEnv(mode, process.cwd(), "");
  for (const [key, value] of Object.entries(env)) {
    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}

type ApiRouteRequest = IncomingMessage & HandlerRequest & {
  body: unknown;
  cookies: Record<string, string>;
};

type ApiRouteResponse = ServerResponse & HandlerResponse & {
  send: (body: unknown) => void;
  redirect: (statusOrUrl: number | string, url?: string) => void;
};

async function readRequestBody(req: IncomingMessage): Promise<unknown> {
  // Serverless runtimes auto-parse JSON bodies; mirror that here so
  // handlers can read `req.body` directly. Non-JSON bodies are
  // returned as raw strings.
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(chunk as Buffer);
  }
  if (chunks.length === 0) return undefined;
  const raw = Buffer.concat(chunks).toString("utf8");
  const contentType = (req.headers["content-type"] ?? "").toLowerCase();
  if (contentType.includes("application/json")) {
    try { return JSON.parse(raw); } catch { return raw; }
  }
  return raw;
}

function parseQuery(url: string, params: Record<string, string>): Record<string, string | string[] | undefined> {
  const out: Record<string, string | string[] | undefined> = { ...params };
  const qIdx = url.indexOf("?");
  if (qIdx === -1) return out;
  const search = new URLSearchParams(url.slice(qIdx + 1));
  for (const [k, v] of search.entries()) {
    const existing = out[k];
    if (existing === undefined) out[k] = v;
    else if (Array.isArray(existing)) existing.push(v);
    else out[k] = [existing, v];
  }
  return out;
}

function parseCookies(header: string | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const pair of header.split(";")) {
    const idx = pair.indexOf("=");
    if (idx === -1) continue;
    out[pair.slice(0, idx).trim()] = decodeURIComponent(pair.slice(idx + 1).trim());
  }
  return out;
}

/**
 * Resolve a request pathname (without /api prefix) to a real handler
 * file under api/. Supports:
 *   - Plain routes:          api/todos.ts
 *   - Nested routes:         api/todos/list.ts
 *   - [id] dynamic segments: api/todos/[id].ts (captured into query.id)
 *   - [...rest] catch-alls:  api/public/[...path].ts (captured into query.path)
 *
 * Rejects any request whose decoded segments contain `.`, `..`, or a
 * NUL byte -- those are only meaningful as traversal markers; Drizzle
 * / Vercel route names can't legitimately include them. After `walk()`
 * finds a candidate file, the resolved real path is asserted to be
 * under `API_ROOT`: any slip that would otherwise let a crafted URL
 * load a file outside api/ returns null instead.
 */
function resolveHandler(
  pathname: string,
): { file: string; params: Record<string, string> } | null {
  let decoded = pathname;
  try { decoded = decodeURIComponent(pathname); } catch { return null; }
  const segments = decoded.replace(/^\/+/, "").split("/").filter(Boolean);
  for (const seg of segments) {
    if (seg === "." || seg === ".." || seg.includes("\0") || seg.includes("/")) {
      return null;
    }
  }
  const hit = walk(API_ROOT, segments, {});
  if (!hit) return null;
  // Belt-and-suspenders: after path.join normalisation, confirm the
  // final file really lives under API_ROOT. join()'s `..` handling
  // should have prevented any escape already, but checking the
  // resolved real path is cheap.
  const resolvedFile = resolve(hit.file);
  if (resolvedFile !== API_ROOT && !resolvedFile.startsWith(API_ROOT + "/")) {
    return null;
  }
  return hit;
}

function walk(
  dir: string,
  segments: string[],
  params: Record<string, string>,
): { file: string; params: Record<string, string> } | null {
  if (!existsSync(dir) || !statSync(dir).isDirectory()) return null;
  if (segments.length === 0) {
    // Trailing-slash request on a directory -> try index.ts.
    for (const ext of [".ts", ".tsx", ".js", ".mjs"]) {
      const p = join(dir, `index${ext}`);
      if (existsSync(p)) return { file: p, params };
    }
    return null;
  }
  const [head, ...rest] = segments;
  // Exact file match (highest priority).
  for (const ext of [".ts", ".tsx", ".js", ".mjs"]) {
    const p = join(dir, `${head}${ext}`);
    if (rest.length === 0 && existsSync(p)) return { file: p, params };
  }
  // Subdirectory recursion.
  const sub = join(dir, head);
  if (existsSync(sub) && statSync(sub).isDirectory()) {
    const hit = walk(sub, rest, params);
    if (hit) return hit;
  }
  // [id] match: pick the first bracketed file/dir sibling.
  const entries = readdirSync(dir);
  for (const entry of entries) {
    const dynamic = entry.match(/^\[(\.\.\.)?([A-Za-z_][A-Za-z0-9_]*)](?:\.[a-z]+)?$/);
    if (!dynamic) continue;
    const [, spread, paramName] = dynamic;
    const entryPath = join(dir, entry);
    const isDir = statSync(entryPath).isDirectory();
    if (spread === "...") {
      return { file: isDir ? walkIndex(entryPath) ?? "" : entryPath, params: { ...params, [paramName]: segments.join("/") } };
    }
    if (isDir) {
      const hit = walk(entryPath, rest, { ...params, [paramName]: head });
      if (hit) return hit;
    } else if (rest.length === 0) {
      return { file: entryPath, params: { ...params, [paramName]: head } };
    }
  }
  return null;
}

function walkIndex(dir: string): string | null {
  for (const ext of [".ts", ".tsx", ".js", ".mjs"]) {
    const p = join(dir, `index${ext}`);
    if (existsSync(p)) return p;
  }
  return null;
}

export function appbuilderApiDevServer(): Plugin {
  return {
    name: "appbuilder-api-dev-server",
    configureServer(server: ViteDevServer) {
      // Populate process.env from .env so api/*.ts handlers reading
      // process.env.SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY work in
      // `npm run dev`. Vite only exposes .env via import.meta.env
      // by default.
      hydrateProcessEnvFromDotEnv(server.config.mode ?? "development");

      server.middlewares.use("/api", async (req, res, next) => {
        try {
          const url = req.url ?? "/";
          const pathname = url.split("?")[0] ?? "";
          const resolved = resolveHandler(pathname);
          if (!resolved || !resolved.file) return next();

          const mod = await server.ssrLoadModule(resolved.file);
          const handler = (mod as { default?: unknown }).default;
          if (typeof handler !== "function") return next();

          const body = await readRequestBody(req);
          const query = parseQuery(url, resolved.params);
          const cookies = parseCookies(req.headers.cookie);

          const handlerReq = Object.assign(req, { body, query, cookies }) as ApiRouteRequest;
          const handlerRes = res as ApiRouteResponse;
          handlerRes.status = function (code: number) {
            this.statusCode = code;
            return this;
          };
          handlerRes.json = function (payload: unknown) {
            if (!this.hasHeader("Content-Type")) this.setHeader("Content-Type", "application/json");
            this.end(JSON.stringify(payload));
          };
          handlerRes.send = function (payload: unknown) {
            if (typeof payload === "string") {
              this.end(payload);
            } else if (payload === undefined || payload === null) {
              this.end();
            } else {
              if (!this.hasHeader("Content-Type")) this.setHeader("Content-Type", "application/json");
              this.end(JSON.stringify(payload));
            }
          };
          // Mirror @vercel/node: res.redirect(url) => 307, res.redirect(status, url).
          // Auth handlers (login/logout) end with a redirect and crash without it.
          handlerRes.redirect = function (statusOrUrl: number | string, url?: string) {
            const status = typeof statusOrUrl === "number" ? statusOrUrl : 307;
            const location = typeof statusOrUrl === "string" ? statusOrUrl : (url ?? "/");
            this.statusCode = status;
            this.setHeader("Location", location);
            this.end();
          };

          await (handler as (req: ApiRouteRequest, res: ApiRouteResponse) => unknown)(handlerReq, handlerRes);
        } catch (err) {
          if (!res.headersSent) {
            res.statusCode = 500;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({
              error: err instanceof Error ? err.message : "Server error",
            }));
          } else {
            next(err as Error);
          }
        }
      });
    },
  };
}
