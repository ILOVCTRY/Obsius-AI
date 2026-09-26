import { defineConfig } from 'tsdown'

export default defineConfig({
  entry: ['src/index.ts'],
  format: 'esm',
  platform: 'node',
  target: 'node22',
  outDir: 'lib',
  fixedExtension: false,
  dts: false,
  clean: true,
  deps: { neverBundle: [/^node:/, /^@deepseek-ai\//] },
})
