/**
 * 资产类型自动识别：对齐 Python detect_type。
 * url / IPv4→host / host:port→service / 完整域名→domain / 64hex→binary；
 * 识别不出返回 null（调用方报错提示手选）。
 */

const LABEL_RE = /^[A-Za-z0-9_-]{1,63}$/

/**
 * @param value - 原始资产值。
 * @returns 识别出的类型，或 null。
 */
export function detectType(value: string): string | null {
  const v = value.trim()
  if (!v || v.length > 2048) return null
  if (v.includes('://')) {
    try {
      return new URL(v).hostname ? 'url' : null
    } catch {
      return null
    }
  }
  if (v.includes('/') || v.includes(' ') || v.includes('@') || v.includes('?') || v.includes('#')) {
    return null
  }
  // IPv4/IPv6 均按 host（Python ipaddress 同口径）。
  if (isIp(v)) return 'host'
  if (v.length === 64 && /^[0-9a-fA-F]{64}$/.test(v)) return 'binary'
  if (v.includes(':')) {
    const idx = v.lastIndexOf(':')
    const host = v.slice(0, idx)
    const port = v.slice(idx + 1)
    if (host && !host.includes(':') && /^\d+$/.test(port)) return 'service'
    return null
  }
  if (v.includes('.') && v.length <= 253 && v.split('.').every(label => LABEL_RE.test(label))) {
    return 'domain'
  }
  return null
}

/**
 * 是否字面 IP 地址。
 * @param value - 待判定字符串。
 * @returns 是 IP 返回 true。
 */
export function isIp(value: string): boolean {
  // URL 构造可识别方括号 IPv6；裸 IPv6 用正则兜底。
  if (value.includes(':')) {
    return /^[0-9a-fA-F:]+$/.test(value) && value.split(':').length >= 3
  }
  if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(value)) return false
  return value.split('.').every(part => Number(part) <= 255)
}

/**
 * domain 值规范化：剥 scheme / 尾斜 / 尾部端口；空串抛错。
 * @param value - 原始 domain 值。
 * @returns 裸域名。
 */
export function normalizeDomain(value: string): string {
  let v = value.trim()
  if (v.includes('://')) v = new URL(v).hostname
  v = v.replace(/\/+$/, '')
  if (v.includes(':')) {
    const idx = v.lastIndexOf(':')
    const head = v.slice(0, idx)
    const tail = v.slice(idx + 1)
    if (head && /^\d{1,5}$/.test(tail)) v = head
  }
  if (!v) throw new Error('domain 值规范化后为空（请传裸域名，不带 http:// 或端口）')
  return v
}
