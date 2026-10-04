// 模型供应商预设（2026-10-04，对齐 cc-haha 的 providerPresets）：「添加供应商」
// 时点一个预设即自动填好名称 / 根地址 / 兼容格式 / Key 获取链接，免手填易错的地址。
//
// **根地址口径**（必须与后端一致，见 core/llm/openai_compat.py:71 与
// anthropic_compat.py:193）：base_url 是**根地址**，后端自动追加
// `/v1/chat/completions`（openai-chat-completions）/ `/v1/responses`（openai-responses）
// / `/v1/messages`（anthropic-messages）。故这里给的地址要能接上 `/v1/…`——
// 例如 OpenRouter 的 OpenAI 端点是 `https://openrouter.ai/api/v1/chat/completions`，
// 根就填 `https://openrouter.ai/api`。
//
// 模型清单**刻意留空**：各家模型 id 变动频繁，手填易错；建好后用卡片里的
// 「发现模型」拉取，或在弹窗里手填。避免硬编码过时 id 误导。

import type { LlmProviderFormat } from "@/lib/types"

export interface ProviderPreset {
  id: string
  /** 显示名（同时作为供应商默认名，可改） */
  name: string
  /** API 根地址（后端自动追加 /v1/…）；自定义预设留空 */
  baseUrl: string
  format: LlmProviderFormat
  /** 是否必须 API Key（本地端点如 LM Studio / Ollama 不需要） */
  needsKey: boolean
  /** 「获取 API Key」跳转 */
  keyUrl?: string
  /** 一句话说明 */
  note?: string
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: "custom", name: "自定义", baseUrl: "",
    format: "openai-chat-completions", needsKey: true,
    note: "手填地址与兼容格式",
  },
  {
    id: "deepseek", name: "DeepSeek", baseUrl: "https://api.deepseek.com",
    format: "openai-chat-completions", needsKey: true,
    keyUrl: "https://platform.deepseek.com/api_keys",
  },
  {
    id: "deepseek-anthropic", name: "DeepSeek（Anthropic 兼容）",
    baseUrl: "https://api.deepseek.com/anthropic",
    format: "anthropic-messages", needsKey: true,
    keyUrl: "https://platform.deepseek.com/api_keys",
  },
  {
    id: "zhipu", name: "智谱 GLM", baseUrl: "https://open.bigmodel.cn/api/anthropic",
    format: "anthropic-messages", needsKey: true,
    keyUrl: "https://open.bigmodel.cn/usercenter/apikeys",
  },
  {
    id: "moonshot", name: "Moonshot / Kimi", baseUrl: "https://api.moonshot.cn",
    format: "openai-chat-completions", needsKey: true,
    keyUrl: "https://platform.moonshot.cn/console/api-keys",
  },
  {
    id: "openrouter", name: "OpenRouter", baseUrl: "https://openrouter.ai/api",
    format: "openai-chat-completions", needsKey: true,
    keyUrl: "https://openrouter.ai/keys",
  },
  {
    id: "lmstudio", name: "LM Studio（本地）", baseUrl: "http://127.0.0.1:1234",
    format: "openai-chat-completions", needsKey: false,
    note: "本机 LM Studio 需开启 Local Server",
  },
  {
    id: "ollama", name: "Ollama（本地）", baseUrl: "http://127.0.0.1:11434",
    format: "openai-chat-completions", needsKey: false,
    note: "本机 Ollama 默认端口 11434",
  },
]

/** 兼容格式的展示名（列表徽章 + 弹窗下拉共用） */
export const FORMAT_LABEL: Record<LlmProviderFormat, string> = {
  "openai-chat-completions": "OpenAI Chat",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
}
