// 左导航列内的「工作台侧栏槽位」（2026-10-16）。
//
// 背景：智能体工作台的左栏（agent 切换器 + 会话线程列表）原本独占左数第二列，
// 而左导航列下半部分一直空着 —— 两列都很窄、主区被挤掉一整列宽。现让二者共用
// 同一列：NavRail 在导航分组下方留出槽位 DOM，AgentWorkbenchView 用 createPortal
// 把自己的左栏投递进去（状态与事件仍留在工作台自己的 React 树里，无需上提）。
//
// 槽位不存在时（导航收起 / 窄栏 / 无项目 / 非 agents 视图）context 为 null，
// 工作台退回原来的「左栏 + 分割线 + 主区」两列布局，行为与改造前一致。
import { createContext, useContext } from "react"

export const NavRailSlotContext = createContext<HTMLElement | null>(null)

/** 左导航列里可用于停靠工作台侧栏的槽位节点；null = 没有槽位（走原来的两列布局） */
export function useNavRailSlot(): HTMLElement | null {
  return useContext(NavRailSlotContext)
}
