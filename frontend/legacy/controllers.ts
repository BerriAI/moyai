import type { ControllerBridge } from "./bridge"

// The only reader of the controllers' global. It is deliberately not declared
// on `window`, so any other access fails the typecheck.
export const controllers: ControllerBridge = new Proxy({} as ControllerBridge, {
  get(_, method: keyof ControllerBridge) {
    const bridge = (globalThis as { MoyaiControllers?: ControllerBridge }).MoyaiControllers
    if (!bridge) throw new Error("Workspace controllers have not loaded")
    return bridge[method]
  },
})
