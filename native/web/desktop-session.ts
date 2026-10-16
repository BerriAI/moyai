/** Mount/visibility generations fence late HTTP responses and user input. */
export class DesktopLifetime {
  private generation = 0
  alive = true
  constructor(public visible: boolean) {}
  setVisible(value: boolean) {
    if (value !== this.visible) { this.visible = value; this.invalidate() }
  }
  invalidate() { return ++this.generation }
  accepts(version: number) { return this.alive && this.visible && version === this.generation }
  dispose() { this.alive = false; this.invalidate() }
}

export type KeyboardRemote = {
  viewOnly: boolean
  sendKey: (keysym: number, code: string, down?: boolean) => void
  clipboardPasteFrom: (text: string) => void
  focus: (options?: FocusOptions) => void
}

export function pasteRemote(remote: KeyboardRemote, text: string, allowed: boolean) {
  if (!allowed || remote.viewOnly) return false
  if (text.length > 20000) throw new Error("Paste up to 20,000 characters at a time.")
  remote.clipboardPasteFrom(text)
  remote.sendKey(0xffe3, "ControlLeft", true)
  remote.sendKey(0x76, "KeyV")
  // Clipboard focus transfer already released physical modifiers. Always
  // release our injected Control, even if a native key-up never arrives.
  remote.sendKey(0xffe3, "ControlLeft", false)
  remote.focus({ preventScroll: true })
  return true
}

export type HeldKey = { key: string; code: string; location: number }

export function releaseRemoteKeys(remote: KeyboardRemote, held: Map<string, HeldKey>, keyUp: (key: HeldKey) => void) {
  // Let noVNC apply its own complete key mapping and QEMU extension handling.
  for (const key of held.values()) keyUp(key)
  held.clear()
  remote.sendKey(0xffe3, "ControlLeft", false)
}

export function pauseRemote(remote: KeyboardRemote, held: Map<string, HeldKey>, keyUp: (key: HeldKey) => void) {
  releaseRemoteKeys(remote, held, keyUp)
  remote.viewOnly = true
}
