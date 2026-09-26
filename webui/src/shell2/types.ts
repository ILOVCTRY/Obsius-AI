// TRAE 化新壳内部类型（webui-trae-shell 方案，docs/plans/webui-trae-shell.md）

/** 三姿态：工作台=对话干活 / 画布=空间关系 / 工具=专用动作界面 */
export type Posture = "work" | "canvas" | "tools"

/** 侧栏资源入口 */
export type ResourceKey = "templates" | "plugins" | "intel" | "files"

/** 主区屏幕：姿态页 / 资源页（覆盖） / 设置 / 审批（M1 兜底，M2 由对话卡替代） */
export type Screen =
  | { kind: "posture"; posture: Posture }
  | { kind: "resource"; resource: ResourceKey }
  | { kind: "settings" }
  | { kind: "approvals" }

/** 当前对话定位；编排器 sid 固定 "__orch"，其余=session id */
export interface Target {
  pid: string
  sid: string
}

/** localStorage 持久化形态（ui.shell2.state） */
export interface ShellState {
  posture: Posture
  target: Target | null
  expanded: string[]
}
