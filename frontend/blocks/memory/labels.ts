import type { MemoryNote } from "../../api/generated/types.gen"

export const memoryKinds = { preference: "Preference", feedback: "Correction", project: "Project context", reference: "Reference" } as const satisfies Record<MemoryNote["kind"], string>

export const isMemoryKind = (value: string): value is MemoryNote["kind"] => Object.hasOwn(memoryKinds, value)

export const memorySources = { chat: "Learned from your message", observation: "Observed during work", manual: "Added by you" } as const satisfies Record<MemoryNote["source"]["type"], string>
