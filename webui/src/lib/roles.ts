// 角色显示名统一出口（yaml name 行 = 中文显示名，与文件 stem=角色 id 解耦）。
// 设置页用 roleLabel（中文名＋去下划线英文 id 对照）；其余界面用 roleName（只中文名）。

/** 设置页标签：「通用（generalist）」；name 缺失或与 id 相同时只显 id（前导 _ 剥掉） */
export function roleLabel(r: { name?: string | null; file: string }): string {
  const id = r.file.replace(/^_/, "")
  return r.name && r.name !== r.file ? `${r.name}（${id}）` : id
}

/** 其余界面（下拉/标签页/事件流）：只显中文名，缺失兜底 id */
export function roleName(name: string | null | undefined, id: string): string {
  return name || id
}
