import React, { memo, useEffect, useRef, useState } from "react"

const lines = ["> what are we", "  working on?"] as const
const sentence = lines.join("\n")
const glyphs = "!<>-_\\/[]{}=+*^?#01ABCDEFX"
const finished = { lines, done: true }

export function decodeFrame(tick: number) {
  const resolved = Math.min(tick, sentence.length)
  return {
    lines: [...sentence].map((character, index) => {
      if (" \n>".includes(character) || index < resolved) return character
      return index < resolved + 6 ? glyphs[(index * 7 + tick * 3) % glyphs.length] : " "
    }).join("").split("\n"),
    done: resolved === sentence.length,
  }
}

/** Only this small component renders on a decode tick; the composer never does. */
export const DecodeHeadline = memo(function DecodeHeadline({ paused }: { paused: boolean }) {
  const root = useRef<HTMLHeadingElement>(null)
  const [frame, setFrame] = useState(() => paused || matchMedia("(prefers-reduced-motion: reduce)").matches
    ? finished : decodeFrame(0))

  useEffect(() => {
    const motion = matchMedia("(prefers-reduced-motion: reduce)")
    let timer: ReturnType<typeof setTimeout> | undefined
    let visible = false, tick = 0
    const stop = () => clearTimeout(timer)
    const step = () => {
      const next = decodeFrame(tick++)
      setFrame(next)
      if (next.done) tick = 0
      timer = setTimeout(step, next.done ? 2400 : 60)
    }
    const sync = () => {
      stop()
      if (paused || motion.matches) { setFrame(finished); return }
      if (document.hidden || !visible) return
      tick = 0
      step()
    }
    const observer = new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting)
      sync()
    })
    if (root.current) observer.observe(root.current)
    motion.addEventListener("change", sync)
    document.addEventListener("visibilitychange", sync)
    sync()
    return () => {
      stop()
      observer.disconnect()
      motion.removeEventListener("change", sync)
      document.removeEventListener("visibilitychange", sync)
    }
  }, [paused])

  return <h1 ref={root} className={`decode-headline ${frame.done && !paused ? "decoded" : ""}`} aria-label="What are we working on?">
    {lines.map((line, index) => <span key={line} className="decode-line" aria-hidden="true">
      <span className="decode-reserve">{line}{index === 1 ? "▌" : ""}</span>
      <span className="decode-text">{frame.lines[index]}{index === 1 && <span className="decode-caret">▌</span>}</span>
    </span>)}
  </h1>
})
