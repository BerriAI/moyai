import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react"
import { createRoot } from "react-dom/client"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowUp, Check, ChevronDown, CircleAlert, FlaskConical, LoaderCircle, Menu, MessageSquare, Plus, Terminal, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import "@/theme.css"
import "./style.css"

type Question = { header: string; question: string; options?: { label: string; description?: string }[] }
type Tool = { name: string; args?: unknown; result?: unknown }
type Message = {
  id: string
  role: "user" | "assistant"
  text: string
  status?: "running" | "complete" | "waiting" | "error"
  tools?: Tool[]
  elapsed_seconds?: number
  questions?: Question[]
  error?: string
}
type Chat = { id: string; title: string; status: "idle" | "running" | "waiting" | "error"; messages: Message[]; error?: string }
type RuntimeStatus = { ready: boolean; phase: string; model?: string; context?: string; error?: string }

const example = "do we support **DeepSeek-V4.1-Flash-1017 is here**"

class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { ...options.headers, ...(options.method === "POST" ? { "Content-Type": "application/json", "X-Moyai-Local": "1" } : {}) },
  })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const data = body as { detail?: unknown; error?: unknown } | null
    const detail = data?.detail ?? data?.error
    throw new RequestError(typeof detail === "string" ? detail : `The local server returned ${response.status}. Check its terminal and retry.`, response.status)
  }
  if (body === null) throw new Error("The server returned an unexpected response. Check that the Moyai local chat server is running.")
  return body as T
}

function errorText(error: unknown): string {
  if (error instanceof TypeError) return "Cannot reach the local server. Start or restart the Moyai chat command, then retry."
  return error instanceof Error ? error.message : "Something went wrong. Please retry."
}

function pretty(value: unknown): string {
  if (typeof value === "string") return value
  return JSON.stringify(value, null, 2) ?? ""
}

function chatFromLocation(): string | null {
  return new URLSearchParams(window.location.hash.slice(1)).get("chat")
}

function Markdown({ text }: { text: string }) {
  return <div className="local-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    h1: ({ children }) => <h2>{children}</h2>,
    a: ({ children, href }) => /^https?:\/\//i.test(href ?? "")
      ? <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>
      : <span title={href || undefined}>{children}</span>,
    img: ({ alt }) => <span className="local-image-placeholder">[Image{alt ? `: ${alt}` : ""}]</span>,
    table: ({ children }) => <div className="local-table-scroll" tabIndex={0} role="region" aria-label="Response table"><table>{children}</table></div>,
  }}>{text}</ReactMarkdown></div>
}

function ToolCalls({ tools }: { tools: Tool[] }) {
  return <details className="local-tools">
    <summary><Terminal size={14} /><span>{tools.length} tool {tools.length === 1 ? "call" : "calls"}</span><ChevronDown size={14} /></summary>
    <div className="local-tool-list">{tools.map((tool, index) => <details className="local-tool" key={`${index}-${tool.name}`}>
      <summary><span className="local-tool-index">{String(index + 1).padStart(2, "0")}</span><span>{tool.name}</span><ChevronDown size={13} /></summary>
      {tool.args !== undefined && <div className="local-tool-data"><strong>Input</strong><pre>{pretty(tool.args)}</pre></div>}
      {tool.result !== undefined && <div className="local-tool-data"><strong>Result</strong><pre>{pretty(tool.result)}</pre></div>}
      {tool.result === undefined && <p className="local-muted">Waiting for a result.</p>}
    </details>)}</div>
  </details>
}

function Questions({ questions, disabled, onAnswer }: { questions: Question[]; disabled: boolean; onAnswer: (answers: Record<string, string>) => Promise<void> }) {
  const inputPrefix = useId()
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const complete = questions.every((question) => answers[question.header]?.trim())
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (complete && !disabled) void onAnswer(answers)
  }
  return <form className="local-questions" onSubmit={submit}>
    <div className="local-question-title"><CircleAlert size={16} />Moyai needs your input</div>
    {questions.map((question, index) => <fieldset key={`${index}-${question.header}`} disabled={disabled}>
      <legend>{question.question}</legend>
      <div className="local-options">{question.options?.map((option) => <Button type="button" variant="outline" key={option.label} aria-pressed={answers[question.header] === option.label} onClick={() => setAnswers((current) => ({ ...current, [question.header]: option.label }))}>
        <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>{answers[question.header] === option.label && <Check size={14} />}
      </Button>)}</div>
      <label htmlFor={`${inputPrefix}-answer-${index}`}>Your answer</label>
      <Input id={`${inputPrefix}-answer-${index}`} value={answers[question.header] ?? ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.header]: event.target.value }))} placeholder="Choose an option or type a response" autoComplete="off" />
    </fieldset>)}
    <Button type="submit" disabled={!complete || disabled}>Continue conversation <ArrowUp size={15} /></Button>
  </form>
}

function ChatMessage({ message, canAnswer, onAnswer }: { message: Message; canAnswer: boolean; onAnswer: (answers: Record<string, string>) => Promise<void> }) {
  const isUser = message.role === "user"
  return <article className={`local-message ${isUser ? "local-user" : "local-assistant"}`} aria-label={isUser ? "Your message" : "Moyai response"}>
    <div className="local-message-heading"><span className={isUser ? "local-avatar user" : "local-avatar"}>{isUser ? "Y" : "M"}</span><strong>{isUser ? "You" : "Moyai"}</strong>{!isUser && message.elapsed_seconds !== undefined && <span className="local-duration">{Math.round(message.elapsed_seconds)}s</span>}</div>
    <div className="local-message-body">
      {message.text && (isUser ? <p className="local-user-text">{message.text}</p> : <Markdown text={message.text} />)}
      {!!message.tools?.length && <ToolCalls tools={message.tools} />}
      {message.status === "running" && <div className="local-working" role="status"><LoaderCircle className="local-spin" size={15} />Working<span className="local-working-detail">Reply and tool details appear when this turn finishes.</span></div>}
      {message.status === "waiting" && !!message.questions?.length && <Questions questions={message.questions} disabled={!canAnswer} onAnswer={onAnswer} />}
      {message.error && <div className="local-inline-error" role="alert"><CircleAlert size={15} /><span>{message.error}</span></div>}
    </div>
  </article>
}

function App() {
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null)
  const [chats, setChats] = useState<Chat[]>([])
  const [activeId, setActiveId] = useState<string | null>(chatFromLocation)
  const [activeChat, setActiveChat] = useState<Chat | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({ new: example })
  const [submitting, setSubmitting] = useState(false)
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [retry, setRetry] = useState(0)
  const [loadingChat, setLoadingChat] = useState(Boolean(activeId))
  const activeRef = useRef(activeId)
  const transcriptRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const nearBottomRef = useRef(true)
  const busyRef = useRef(false)
  const mutationRef = useRef(0)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const pendingRequestRef = useRef<{ payload: string; id: string } | null>(null)

  const selectChat = useCallback((id: string | null) => {
    activeRef.current = id
    setActiveId(id)
    setActiveChat(null)
    setLoadingChat(Boolean(id))
    setActionError(null)
    setSidebarOpen(false)
    nearBottomRef.current = true
    window.history.replaceState(null, "", id ? `#chat=${encodeURIComponent(id)}` : window.location.pathname)
    requestAnimationFrame(() => headingRef.current?.focus())
  }, [])

  useEffect(() => {
    const onHashChange = () => selectChat(chatFromLocation())
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [selectChat])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    const poll = async () => {
      const generation = mutationRef.current
      try {
        const [nextStatus, nextChats, nextChat] = await Promise.all([
          request<RuntimeStatus>("/api/status", { signal: controller.signal }),
          request<{ chats: Chat[] }>("/api/chats", { signal: controller.signal }),
          activeId ? request<Chat>(`/api/chats/${encodeURIComponent(activeId)}`, { signal: controller.signal }) : Promise.resolve(null),
        ])
        if (cancelled) return
        setRuntime(nextStatus)
        // A POST response is newer than a GET which began before it.
        if (!busyRef.current && generation === mutationRef.current) {
          setChats(nextChats.chats)
          if (activeRef.current === activeId) setActiveChat(nextChat)
        }
        setLoadingChat(false)
        setConnectionError(null)
      } catch (error) {
        if (!cancelled && error instanceof RequestError && error.status === 404 && activeId) {
          selectChat(null)
          setActionError("This conversation is no longer available. The server may have restarted; start a new chat below.")
          return
        }
        if (!cancelled) { setConnectionError(errorText(error)); setLoadingChat(false) }
      } finally {
        if (!cancelled) timer = setTimeout(() => void poll(), 1000)
      }
    }
    void poll()
    return () => { cancelled = true; controller.abort(); clearTimeout(timer) }
  }, [activeId, retry, selectChat])

  useEffect(() => {
    document.title = `${activeChat?.title || "Local chat"} · Moyai`
    if (nearBottomRef.current && transcriptRef.current) transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight
  }, [activeChat])

  const currentChat = activeChat?.id === activeId ? activeChat : null
  const runningChat = chats.find((chat) => chat.status === "running" || chat.status === "waiting")
  const occupiedElsewhere = !!runningChat && runningChat.id !== activeId
  const running = currentChat?.status === "running"
  const waiting = currentChat?.status === "waiting"
  const pendingQuestionId = waiting ? [...currentChat.messages].reverse().find((message) => message.status === "waiting" && !!message.questions?.length)?.id : undefined
  const draftKey = activeId ?? "new"
  const draft = drafts[draftKey] ?? ""
  const unavailable = !runtime?.ready || !!connectionError
  const sendDisabled = unavailable || submitting || occupiedElsewhere || running || waiting || loadingChat

  async function newChat() {
    if (busyRef.current || submitting || runningChat || unavailable) return
    setSubmitting(true)
    busyRef.current = true
    mutationRef.current += 1
    setActionError(null)
    try {
      const chat = await request<Chat>("/api/chats", { method: "POST", body: "{}" })
      setChats((current) => [chat, ...current.filter((item) => item.id !== chat.id)])
      selectChat(chat.id)
      setActiveChat(chat)
      setLoadingChat(false)
      setDrafts((current) => ({ ...current, [chat.id]: "" }))
    } catch (error) { setActionError(errorText(error)) }
    finally { setSubmitting(false); busyRef.current = false; mutationRef.current += 1 }
  }

  async function send(answers?: Record<string, string>) {
    const text = answers ? "" : draft.trim()
    if (busyRef.current || submitting || unavailable || occupiedElsewhere || running || (!answers && (waiting || !text))) return
    setSubmitting(true)
    busyRef.current = true
    mutationRef.current += 1
    setActionError(null)
    const originalKey = draftKey
    let id = activeId
    try {
      if (!id) {
        const chat = await request<Chat>("/api/chats", { method: "POST", body: "{}" })
        id = chat.id
        selectChat(id)
        setActiveChat(chat)
        setLoadingChat(false)
        setDrafts((current) => ({ ...current, [chat.id]: text }))
      }
      const payload = JSON.stringify({ chat: id, message: text, answers })
      if (pendingRequestRef.current?.payload !== payload) pendingRequestRef.current = { payload, id: crypto.randomUUID() }
      const nextChat = await request<Chat>(`/api/chats/${encodeURIComponent(id)}/messages`, {
        method: "POST",
        body: JSON.stringify({ message: text, request_id: pendingRequestRef.current.id, ...(answers ? { answers } : {}) }),
      })
      pendingRequestRef.current = null
      if (activeRef.current === id) setActiveChat(nextChat)
      setChats((current) => [nextChat, ...current.filter((chat) => chat.id !== id)])
      if (!answers) setDrafts((current) => ({ ...current, [originalKey]: "", [id!]: "" }))
      nearBottomRef.current = true
    } catch (error) { setActionError(errorText(error)) }
    finally { setSubmitting(false); busyRef.current = false; mutationRef.current += 1; setRetry((value) => value + 1) }
  }

  const navigation = <>
    <div className="local-brand"><span className="local-brand-glyph">m</span><span>MOYAI</span><span className="local-local-badge">LOCAL</span></div>
    <Button className="local-new-chat" variant="outline" onClick={() => void newChat()} disabled={submitting || !!runningChat || unavailable}><Plus size={16} />New chat</Button>
    <div className="local-history-label">CONVERSATIONS <span>{chats.length.toString().padStart(2, "0")}</span></div>
    <nav className="local-history" aria-label="Chat history">
      {!chats.length && <p className="local-history-empty">Your test conversations<br />will appear here.</p>}
      {chats.map((chat) => <Button variant="ghost" key={chat.id} className={`local-chat-link ${activeId === chat.id ? "selected" : ""}`} onClick={() => selectChat(chat.id)} aria-current={activeId === chat.id ? "page" : undefined}>
        <MessageSquare size={15} /><span>{chat.title || "New chat"}</span>{chat.status === "running" && <LoaderCircle size={13} className="local-spin" aria-label="Working" />}{chat.status === "waiting" && <CircleAlert size={13} className="local-warning" aria-label="Needs input" />}
      </Button>)}
    </nav>
    <div className="local-sidebar-footer"><FlaskConical size={17} /><div><strong>A place to try things</strong><span>Chats stay on this local server.<br />History resets when it restarts.</span></div></div>
  </>

  const statusLabel = connectionError ? "Disconnected" : runtime?.error ? "Setup error" : runtime?.ready ? "Ready" : runtime?.phase || "Connecting"

  return <div className="local-app">
    <aside className="local-sidebar">{navigation}</aside>
    <Dialog open={sidebarOpen} onOpenChange={setSidebarOpen}><DialogContent className="local-mobile-sidebar" showCloseButton={false}><DialogTitle className="local-sr-only">Conversations</DialogTitle><DialogDescription className="local-sr-only">Choose a previous conversation or start a new chat.</DialogDescription><Button className="local-close-nav" size="icon" variant="ghost" onClick={() => setSidebarOpen(false)} aria-label="Close conversations"><X size={18} /></Button>{navigation}</DialogContent></Dialog>
    <main className="local-main">
      <header className="local-header"><div className="local-header-title"><Button className="local-mobile-menu" variant="ghost" size="icon" aria-label="Open conversations" onClick={() => setSidebarOpen(true)}><Menu size={19} /></Button><div><h1 tabIndex={-1} ref={headingRef}>Local chat</h1><span>Test the way Moyai thinks.</span></div></div><div className={`local-runtime-status ${connectionError || runtime?.error ? "error" : runtime?.ready ? "ready" : "starting"}`} role="status"><span className="local-status-dot" />{statusLabel}</div></header>
      <div className="local-context-bar"><FlaskConical size={15} /><span>Real Moyai agent <span className="local-context-dot">·</span> DeepSeek test workspace</span><span className="local-simulation">Provider responses are simulated</span></div>
      {(connectionError || runtime?.error) && <div className="local-banner" role="alert"><CircleAlert size={18} /><div><strong>{connectionError ? "Local server unavailable" : "The test workspace needs attention"}</strong><p>{connectionError || runtime?.error}</p></div><Button variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)}>Retry</Button></div>}
      {!runtime?.ready && !runtime?.error && !connectionError && runtime && <div className="local-setup" role="status"><LoaderCircle size={16} className="local-spin" /><span>Preparing the test workspace. {runtime.phase && `${runtime.phase}.`} You can write your message while it starts.</span></div>}
      <div className="local-transcript" ref={transcriptRef} onScroll={() => { const el = transcriptRef.current; if (el) nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90 }}>
        <div className={`local-conversation ${currentChat?.messages.length ? "has-messages" : ""}`}>
          {loadingChat ? <div className="local-loading" role="status"><LoaderCircle className="local-spin" size={20} />Loading conversation…</div> : !currentChat?.messages.length ? <div className="local-welcome"><span className="local-welcome-eyebrow">THE LOCAL PLAYGROUND</span><div className="local-orbit" aria-hidden="true"><span>m</span><i /><b /></div><h2>Try a conversation.</h2><p>Ask a real question. See how Moyai investigates,<br className="local-desktop-break" /> follows up, and puts an answer together.</p><div className="local-welcome-note"><Terminal size={14} /><span>Open any tool call to see what happened.</span></div></div> : currentChat.messages.map((message) => <ChatMessage key={message.id} message={message} canAnswer={message.id === pendingQuestionId && !unavailable && !submitting && !occupiedElsewhere} onAnswer={(answers) => send(answers)} />)}
          {running && !currentChat?.messages.some((message) => message.status === "running") && <div className="local-working" role="status"><LoaderCircle className="local-spin" size={15} />Moyai is working</div>}
          {currentChat?.error && !currentChat.messages.some((message) => message.error === currentChat.error) && <div className="local-inline-error" role="alert"><CircleAlert size={16} /><span>{currentChat.error}</span></div>}
        </div>
      </div>
      <div className="local-composer-region">
        {occupiedElsewhere && <div className="local-busy-note"><span>Another conversation {runningChat?.status === "waiting" ? "needs your input" : "is running"}.</span><Button variant="link" size="sm" onClick={() => selectChat(runningChat!.id)}>Open conversation</Button></div>}
        {waiting && <p className="local-busy-note">Answer Moyai’s question above to continue.</p>}
        {actionError && <div className="local-action-error" role="alert"><CircleAlert size={16} /><span>{actionError} Your draft has been kept.</span><Button size="icon-sm" variant="ghost" aria-label="Dismiss error" onClick={() => setActionError(null)}><X size={14} /></Button></div>}
        <form className="local-composer" onSubmit={(event) => { event.preventDefault(); void send() }}>
          <label className="local-sr-only" htmlFor="chat-message">Message Moyai</label>
          <Textarea id="chat-message" ref={composerRef} value={draft} onChange={(event) => { pendingRequestRef.current = null; setDrafts((current) => ({ ...current, [draftKey]: event.target.value })) }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (!sendDisabled) void send() } }} placeholder="Message Moyai…" rows={2} className="local-message-input" disabled={loadingChat} />
          <div className="local-composer-bottom"><span className="local-model"><span className="local-model-glyph">✳</span>{runtime?.model || "Moyai agent"}</span><Button type="submit" size="icon" className="local-send" disabled={sendDisabled || !draft.trim()} aria-label={submitting ? "Sending message" : "Send message"}>{submitting ? <LoaderCircle className="local-spin" size={18} /> : <ArrowUp size={19} />}</Button></div>
        </form>
        <div className="local-composer-help"><span>Test workspace · Results aren’t a live support check.</span><span>Enter to send <span>·</span> Shift + Enter for a new line</span></div>
      </div>
    </main>
  </div>
}

createRoot(document.getElementById("root")!).render(<App />)
