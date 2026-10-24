/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { createReadStream, existsSync, statSync, cpSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
// Static_Output lives in the top-level `output/` directory (Req 16.1), outside frontend/.
const outputDir = path.resolve(here, '..', 'output');

const MIME: Record<string, string> = {
  '.json': 'application/json',
  '.geojson': 'application/geo+json',
};

/**
 * Serves ../output at /output in dev and preview, and copies it into dist/output on build.
 * Only files inside output/ are reachable; path traversal is rejected.
 */
function staticOutput(): Plugin {
  const middleware = (
    req: { url?: string },
    res: { statusCode: number; setHeader(k: string, v: string): void; end(): void } & NodeJS.WritableStream,
    next: () => void,
  ) => {
    if (!req.url?.startsWith('/output/')) return next();
    const rel = decodeURIComponent(req.url.split('?')[0].slice('/output/'.length));
    const file = path.resolve(outputDir, rel);
    if (!file.startsWith(outputDir + path.sep) || !existsSync(file) || !statSync(file).isFile()) {
      res.statusCode = 404;
      return res.end();
    }
    res.setHeader('Content-Type', MIME[path.extname(file)] ?? 'application/octet-stream');
    createReadStream(file).pipe(res);
  };
  return {
    name: 'static-output',
    configureServer(server) {
      server.middlewares.use(middleware as never);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware as never);
    },
    closeBundle() {
      if (existsSync(outputDir)) {
        cpSync(outputDir, path.resolve(here, 'dist', 'output'), { recursive: true });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), staticOutput()],
  test: {
    include: ['tests/**/*.test.ts', 'src/**/*.test.ts'],
    environment: 'node',
  },
});
