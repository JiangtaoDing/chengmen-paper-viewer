import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const entryFile = path.join(__dirname, 'bridge.mjs');
const outDir = path.join(__dirname, 'dist');
const outFile = path.join(outDir, 'mcp_bridge.js');

fs.mkdirSync(outDir, { recursive: true });

await esbuild.build({
  entryPoints: [entryFile],
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2020'],
  minify: true,
  sourcemap: false,
  outfile: outFile,
  define: {
    'process.env.NODE_ENV': '"production"',
  },
  legalComments: 'none',
});

const stat = fs.statSync(outFile);
console.log(`[mcp_app/build] Bundled ${path.relative(__dirname, outFile)} (${(stat.size / 1024).toFixed(1)} KiB)`);
