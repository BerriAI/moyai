import React from "react"
import { SkillMessage } from "./skill-content"

const decode = (text: string) => text.replaceAll("&amp;", "&").replaceAll("&lt;", "<").replaceAll("&gt;", ">")

/** Interpret Slack links as links, while keeping code and ordinary text literal. */
export function SlackMessage({ content }: { content: string }) {
  const parts: React.ReactNode[] = []
  let offset = 0
  const pattern = /```[\s\S]*?(?:```|$)|`[^`\n]*`|<(https?:\/\/[^<>|\s]+)(?:\|([^<>\n]*))?>/g
  for (const match of content.matchAll(pattern)) {
    parts.push(<SkillMessage key={`text:${offset}`} content={decode(content.slice(offset, match.index))} />)
    if (match[1]) {
      const url = decode(match[1])
      parts.push(<a key={`link:${match.index}`} href={url} target="_blank" rel="noreferrer">{decode(match[2] || match[1])}</a>)
    } else parts.push(<React.Fragment key={`code:${match.index}`}>{match[0]}</React.Fragment>)
    offset = match.index + match[0].length
  }
  parts.push(<SkillMessage key={`text:${offset}`} content={decode(content.slice(offset))} />)
  return <>{parts}</>
}
