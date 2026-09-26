import { defineConfig } from 'tsdown'
import { typertPlugin } from '@deepseek-ai/dsh-typert-generator/tsdown'

export default defineConfig({
  entry: ['src/index.ts'],
  format: 'esm',
  platform: 'node',
  target: 'node22',
  outDir: 'lib',
  fixedExtension: false,
  dts: false,
  clean: true,
  plugins: [typertPlugin()],
  deps: { neverBundle: [/^node:/, /^@deepseek-ai\//, /^@sec\//] },
})
