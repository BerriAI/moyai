export type SpaceNode = {
  id: string; parentId: string | null; x: number; y: number; status: string
}
export type SpaceGraph = { nodes: SpaceNode[]; active: boolean }

declare global {
  interface Window {
    MoyaiSpace?: {
      mount(canvas: HTMLCanvasElement, options?: {
        anchor?: HTMLElement; graph?: () => SpaceGraph
      }): (() => void) & { resize?: () => void }
    }
  }
}
