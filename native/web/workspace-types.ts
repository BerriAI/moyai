export type ActionSelection = {
  sessionId: string
  turnId: string
  toolId: string
  agentId?: string
  name: string
}

/** A file reference is authorized by its session preview endpoint. Arbitrary URLs are never fetched by the server. */
export type OutputResource = {
  kind: "artifact" | "upload" | "link"
  id?: string
  name: string
  url: string
  media_type?: string
  size?: number
}

export type WorkspaceTab = {
  id: string
  title: string
} & (
  | { kind: "action"; action: ActionSelection }
  | { kind: "pull-request"; url: string }
  | { kind: "subagent"; runId: string; name: string; status: string; avatar: number }
  | { kind: "output"; resource: OutputResource; sessionId?: string }
  | { kind: "computer" | "activity" | "plan" | "files" }
  | { kind: "side-chat"; sessionId?: string }
)
