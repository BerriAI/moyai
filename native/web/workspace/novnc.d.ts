// Public noVNC 1.7 API used by this panel. The similarly named DefinitelyTyped
// packages describe the incompatible pre-1.0 API.
declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, channel: string | WebSocket, options?: { shared?: boolean })
    scaleViewport: boolean
    resizeSession: boolean
    background: string
    viewOnly: boolean
    disconnect(): void
    focus(options?: FocusOptions): void
    sendKey(keysym: number, code: string, down?: boolean): void
    clipboardPasteFrom(text: string): void
    getImageData(): ImageData
  }
}
