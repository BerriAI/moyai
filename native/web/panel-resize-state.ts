export const DEFAULT_CHAT_RATIO = 0.65
export const MIN_CHAT_WIDTH = 320
export const MIN_PANEL_WIDTH = 300
export const PANEL_DIVIDER_WIDTH = 8
export const PANEL_COMPACT_BREAKPOINT = 1000

export type PanelLayout = {
  width: number; available: number; chat: number; panel: number; minRatio: number; maxRatio: number; split: boolean
}
export type RatioStorage = Pick<Storage, "getItem" | "setItem">
export const validChatRatio = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value > 0 && value < 1

/** Auth tokens, session IDs and roles deliberately do not participate in the preference key. */
export function panelRatioStorageKey(scope: string, userId: string): string | null {
  return scope && userId ? `moyai:panel-ratio:v1:${JSON.stringify([scope, userId])}` : null
}
export function decodePanelRatio(raw: string | null): number {
  if (!raw || raw.length > 200) return DEFAULT_CHAT_RATIO
  try {
    const value = JSON.parse(raw)
    return value && value.version === 1 && validChatRatio(value.chatRatio) ? value.chatRatio : DEFAULT_CHAT_RATIO
  } catch { return DEFAULT_CHAT_RATIO }
}
export function readPanelRatio(storage: RatioStorage | undefined, key: string | null): number {
  try { return storage && key ? decodePanelRatio(storage.getItem(key)) : DEFAULT_CHAT_RATIO } catch { return DEFAULT_CHAT_RATIO }
}
export function writePanelRatio(storage: RatioStorage | undefined, key: string | null, ratio: number): boolean {
  if (!storage || !key || !validChatRatio(ratio)) return false
  try { storage.setItem(key, JSON.stringify({ version: 1, chatRatio: ratio })); return true } catch { return false }
}
/** Resolve a preferred ratio without changing it when the viewport forces a clamp. */
export function panelLayout(width: number, preferred: number, viewportWidth = Infinity): PanelLayout {
  const safeWidth = Number.isFinite(width) && width > 0 ? width : 0
  const available = Math.max(0, safeWidth - PANEL_DIVIDER_WIDTH)
  const split = viewportWidth > PANEL_COMPACT_BREAKPOINT && available >= MIN_CHAT_WIDTH + MIN_PANEL_WIDTH
  if (!split) return { width: safeWidth, available, chat: safeWidth, panel: safeWidth, minRatio: 0, maxRatio: 1, split: false }
  const minRatio = MIN_CHAT_WIDTH / available, maxRatio = 1 - MIN_PANEL_WIDTH / available
  const ratio = validChatRatio(preferred) ? preferred : DEFAULT_CHAT_RATIO
  const chat = Math.min(available - MIN_PANEL_WIDTH, Math.max(MIN_CHAT_WIDTH, available * ratio))
  return { width: safeWidth, available, chat, panel: available - chat, minRatio, maxRatio, split: true }
}
export function panelKeyboardRatio(key: string, preferred: number, width: number, viewportWidth: number, largeStep = false): number | null {
  const layout = panelLayout(width, preferred, viewportWidth)
  if (!layout.split) return null
  if (key === "Enter") return DEFAULT_CHAT_RATIO
  if (key === "Home") return layout.minRatio
  if (key === "End") return layout.maxRatio
  if (key !== "ArrowLeft" && key !== "ArrowRight") return null
  const pixels = layout.chat + (key === "ArrowLeft" ? -1 : 1) * (largeStep ? 50 : 10)
  const chat = Math.max(MIN_CHAT_WIDTH, Math.min(layout.available - MIN_PANEL_WIDTH, pixels))
  return Math.abs(chat - layout.chat) < 0.01 ? null : chat / layout.available
}

/** One pointer owns one resize. Clicks, cancellation and viewport changes never save a clamp. */
export class PanelResizeDrag {
  private active = true
  private finalChat: number
  private readonly grabOffset: number
  private readonly initial: PanelLayout
  constructor(
    readonly pointerId: number, readonly preferred: number, readonly width: number,
    startX: number, left: number, viewportWidth: number,
  ) {
    this.initial = panelLayout(width, preferred, viewportWidth)
    this.active = this.initial.split
    this.finalChat = this.initial.chat
    this.grabOffset = startX - left - this.initial.chat - PANEL_DIVIDER_WIDTH / 2
  }
  move(pointerId: number, x: number, left: number, width: number): number | null {
    if (!this.active || pointerId !== this.pointerId || !Number.isFinite(x) || !Number.isFinite(left)) return null
    if (Math.abs(width - this.width) > 0.5) { this.cancel(); return null }
    this.finalChat = Math.max(MIN_CHAT_WIDTH, Math.min(this.initial.available - MIN_PANEL_WIDTH, x - left - PANEL_DIVIDER_WIDTH / 2 - this.grabOffset))
    return this.finalChat / this.initial.available
  }
  finish(pointerId: number): number | null {
    if (!this.active || pointerId !== this.pointerId) return null
    this.active = false
    return Math.abs(this.finalChat - this.initial.chat) < 0.5 ? null : this.finalChat / this.initial.available
  }
  cancel(): void { this.active = false }
}
