import { WorkspaceTypertGenerator } from '@deepseek-ai/dsh-typert-generator'
const root = 'E:/ILOVCTRY/cyberstrike-pro/dsh'
const gen = new WorkspaceTypertGenerator(root, { checkDiagnostics: false })
const discovered = gen.discover()
console.log('discovered:', JSON.stringify(discovered, null, 2))
const arts = gen.generate(['@sec/blackboard-controller'])
for (const a of arts) {
  console.log('artifact face:', a.face, 'remote?', a.remote !== undefined)
}
