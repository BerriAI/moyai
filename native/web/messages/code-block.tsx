import React, { useMemo, useState } from "react"
import parse from "html-react-parser"
import hljs from "highlight.js/lib/core"
import javascript from "highlight.js/lib/languages/javascript"
import typescript from "highlight.js/lib/languages/typescript"
import python from "highlight.js/lib/languages/python"
import bash from "highlight.js/lib/languages/bash"
import json from "highlight.js/lib/languages/json"
import css from "highlight.js/lib/languages/css"
import xml from "highlight.js/lib/languages/xml"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import "./code-block.css"

for (const [name, language] of Object.entries({ javascript, typescript, python, bash, json, css, xml })) hljs.registerLanguage(name, language)

export function CodeBlock({ children }: { children?: React.ReactNode }) {
  const code = React.Children.toArray(children)[0]
  const props = React.isValidElement<{ className?: string; children?: React.ReactNode }>(code) ? code.props : undefined
  const text = typeof props?.children === "string" ? props.children : ""
  const language = /(?:^|\s)language-([\w-]+)/.exec(props?.className ?? "")?.[1] ?? "text"
  const [copiedText, setCopiedText] = useState<string | null>(null)
  const [failedText, setFailedText] = useState<string | null>(null)
  const copied = copiedText === text
  const copyError = failedText === text
  const lineCount = Math.max(1, text.split("\n").length - Number(text.endsWith("\n")))
  const highlighted = useMemo(() => {
    if (text.length > 50_000 || !hljs.getLanguage(language)) return text
    // Highlight.js escapes source text; only its generated token spans are parsed.
    return parse(hljs.highlight(text, { language, ignoreIllegals: true }).value)
  }, [text, language])
  if (!props || typeof props.children !== "string") return <pre>{children}</pre>
  return <div className="code-block" role="group" aria-label={`${language} code block`}>
    <div className="code-block-header"><span>{language}</span><Button type="button" variant="ghost" size="icon-sm" aria-label={copied ? "Code copied" : "Copy code"} title={copied ? "Copied" : "Copy code"} onClick={async () => {
      try { await navigator.clipboard.writeText(text); setCopiedText(text); setFailedText(null) }
      catch { setCopiedText(null); setFailedText(text) }
    }}>{copied ? <Check size={14} /> : <Copy size={14} />}</Button></div>
    <pre tabIndex={0} aria-label={`${language} code`}><span className="code-line-numbers" aria-hidden="true">{Array.from({ length: lineCount }, (_, index) => index + 1).join("\n")}</span><code className={props.className}>{highlighted}</code></pre>
    {copyError && <p className="code-copy-error" role="status">Couldn’t copy. Select the code and copy it manually.</p>}
  </div>
}
