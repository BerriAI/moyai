import { useCallback, useEffect, useRef, useState } from "react"
import { ApiError } from "../api"
import { listConnections, listHistorySessions, listSessionFolders, type ConnectionView, type HistorySessionList, type Me, type SourcedHistorySession } from "../generated/api"
import type { Session } from "./conversation-state"
import { activeSession, pollVisible } from "../polling"
import { sessionIsReadOnly, sessionKey, type SidebarSession } from "./session-list"
import type { SessionFolder, SessionListView } from "./session-list"
import type { SessionUpdates } from "./session-updates"

const emptyInventory = () => ({ hasMore: false, loading: false, loaded: false, notice: "" })

/** History rows have no live workspace or save warning; unread markers are browser-local. */
function historyRow(row: SourcedHistorySession): Session {
  return { ...row, unread: row.unread ?? undefined, workspace: undefined, save_warning: undefined }
}

/** Own the shared sidebar/home inventory, including reads invalidated by access or view changes. */
export function useSessionInventory(me: Me | null, updates: SessionUpdates) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [folders, setFolders] = useState<SessionFolder[]>([])
  const [connections, setConnections] = useState<ConnectionView[]>([])
  const [view, setView] = useState<SessionListView>("active")
  const [pageCount, setPageCount] = useState(1)
  const [state, setState] = useState(emptyInventory)
  const [error, setError] = useState("")
  const version = useRef(0)
  const viewNow = useRef(view)
  viewNow.current = view
  const sessionsNow = useRef(sessions)
  sessionsNow.current = sessions

  // Refresh errors belong to the inventory. A successful mutation must not become
  // a failed send/rename merely because its subsequent inventory read failed.
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const request = ++version.current
    const checkpoint = updates.checkpoint()
    // Background reads leave an already loaded list quiet; pagination sets loading.
    setState(previous => previous.loaded ? previous : { ...previous, loading: true })
    const current = () => !signal?.aborted && request === version.current && viewNow.current === view
    const inventory = async () => {
      const rows = new Map<string, Session>(), notices = new Set<string>()
      let cursor: string | null = null, hasMore = false
      for (let page = 0; page < pageCount; page++) {
        const { data: next }: { data: HistorySessionList } = await listHistorySessions({ query: {
          source: "all", scope: "mine", limit: 20, archived: view === "archived", deleted: view === "deleted", cursor: cursor ?? undefined,
        }, signal })
        if (!current()) return null
        for (const row of next.sessions.map(historyRow)) {
          let unread = row.unread
          if (row.source === "legacy" && row.read_marker_key && row.completion_marker) {
            try { unread = localStorage.getItem(row.read_marker_key) !== row.completion_marker } catch { unread = false }
          }
          rows.set(sessionKey(row), { ...row, unread })
        }
        for (const row of next.ancestors.map(historyRow)) {
          if (!rows.has(sessionKey(row))) rows.set(sessionKey(row), row)
        }
        if (next.notice) notices.add(next.notice)
        hasMore = Boolean(next.has_more && next.next_cursor)
        if (!hasMore || next.next_cursor === cursor) break
        cursor = next.next_cursor!
      }
      return { rows: [...rows.values()], hasMore, notice: [...notices].join(" ") }
    }
    try {
      // Independent catalogs cannot delay publication of the authenticated list.
      const results = await Promise.allSettled([
        inventory().then(result => {
          if (result && current()) {
            updates.acceptRead(checkpoint, result.rows)
            setSessions(result.rows)
            setState({ loading: false, loaded: true, hasMore: result.hasMore, notice: result.notice })
          }
        }).catch(reason => {
          if (current() && reason instanceof ApiError && [401, 403, 404].includes(reason.status)) setSessions([])
          throw reason
        }),
        listConnections({ signal }).then(({ data: accounts }) => { if (current()) setConnections(accounts) }),
        listSessionFolders({ signal }).then(({ data: groups }) => { if (current()) setFolders(groups) }),
      ])
      if (!current()) return
      const failure = results.find(result => result.status === "rejected")
      if (failure?.status === "rejected") throw failure.reason
      setError("")
    } catch (reason) {
      if (signal?.aborted || request !== version.current) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) setSessions([])
      setError(String(reason))
    } finally {
      if (request === version.current) setState(previous => ({ ...previous, loading: false }))
    }
  }, [view, pageCount, updates])

  useEffect(() => {
    const clear = () => {
      ++version.current
      setSessions([]); setFolders([]); setConnections([])
      setPageCount(1); setState(emptyInventory())
    }
    window.addEventListener("moyai:signed-out", clear)
    window.addEventListener("moyai:access-changed", clear)
    return () => {
      window.removeEventListener("moyai:signed-out", clear)
      window.removeEventListener("moyai:access-changed", clear)
    }
  }, [])

  useEffect(() => {
    if (!me) return
    const stop = pollVisible(refresh, () => sessionsNow.current.some(row => activeSession(row.status)) ? 4000 : 15000, reason => setError(String(reason)))
    return () => { stop(); ++version.current }
  }, [me, refresh])

  function loadMore() {
    ++version.current
    setState(previous => ({ ...previous, loading: true }))
    setPageCount(value => value + 1)
  }

  function changeView(next: SessionListView) {
    ++version.current
    setSessions([]); setPageCount(1); setState(emptyInventory()); setView(next)
  }

  const latestRows = new Map(sessions.filter(row => !sessionIsReadOnly(row)).map(row => [row.id, row]))
  function optimisticSession<T extends SidebarSession>(row: T): T {
    const latest = latestRows.get(row.id)
    return updates.apply(sessionIsReadOnly(row) || !latest ? row : { ...row, pinned: latest.pinned, folder: latest.folder })
  }

  return { sessions, folders, connections, view, state, error, refresh, loadMore, changeView, optimisticSession, dismissError: () => setError("") }
}
