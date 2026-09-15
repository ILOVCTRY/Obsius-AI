#!/usr/bin/env node
// gate_bash.js — PreToolUse hook: Bash/PowerShell 默认放行，命中危险关键词才 ask
// 输入: stdin JSON {tool_name, tool_input:{command}}
// 输出: {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow|ask",...}}
const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', () => {
  const raw = Buffer.concat(chunks).toString('utf8');
  let cmd = '';
  try {
    const input = JSON.parse(raw);
    cmd = (input.tool_input && input.tool_input.command) || '';
  } catch (e) {
    // JSON 不合法时降级为原文提取，绝不 fail-open 到无判定
    const m = raw.match(/"command"\s*:\s*"([\s\S]*?)"\s*[\}]/);
    if (m) cmd = m[1];
  }

  const dangerRules = [
    [/\brm\s+-[a-z]*[rf]/i,            'rm 递归/强制删除'],
    [/\bgit\s+push\b/i,                'git push（外发）'],
    [/\bgit\s+reset\s+--hard\b/i,      'git reset --hard'],
    [/\bgit\s+clean\s+-[a-z]*f/i,      'git clean 强制'],
    [/:\|:&/,                          'fork bomb'],
    [/\bmkfs/i,                        'mkfs 格式化'],
    [/\bdd\s+if=/i,                    'dd 磁盘写入'],
    [/\bformat\s+[a-zA-Z]:/i,          'Windows format 磁盘'],
    [/\bdel\s+\/[sq]\b/i,              'del /s /q'],
    [/\brd\s+\/s\b/i,                  'rd /s'],
    [/Remove-Item\s[^|;&]*-Recurse/i,  'Remove-Item 递归删除'],
    [/\bshutdown\b/i,                  'shutdown'],
    [/\btaskkill\s+\/f/i,              'taskkill /f']
  ];

  const hit = cmd ? dangerRules.find(([re]) => re.test(cmd)) : null;
  const out = hit
    ? {
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'ask',
          permissionDecisionReason: '[hook] 危险命令: ' + hit[1]
        }
      }
    : {
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'allow',
          permissionDecisionReason: '[hook] 安全探测命令放行'
        }
      };
  process.stdout.write(JSON.stringify(out));
});
