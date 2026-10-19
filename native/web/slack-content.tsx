import React from "react"
import { SkillMessage } from "./skill-content"
import type { SessionMessage } from "./generated/api"

const decode = (text: string) => text.replaceAll("&amp;", "&").replaceAll("&lt;", "<").replaceAll("&gt;", ">")

/** Resolve Slack markup only outside code; names are always plain React text. */
export function SlackMessage({ content, mentions = {} }: { content: string; mentions?: SessionMessage["slack_mentions"] }) {
  const parts: React.ReactNode[] = []
  let offset = 0
  const pattern = /```[\s\S]*?(?:```|$)|`[^`\n]*`|<(https?:\/\/[^<>|\s]+)(?:\|([^<>\n]*))?>|<@([UW][A-Z0-9]{7,30})(?:\|[^<>\n]*)?>/g
  for (const match of content.matchAll(pattern)) {
    parts.push(<SkillMessage key={`text:${offset}`} content={decode(content.slice(offset, match.index))} />)
    if (match[1]) {
      const url = decode(match[1])
      parts.push(<a key={`link:${match.index}`} href={url} target="_blank" rel="noreferrer">{decode(match[2] || match[1])}</a>)
    } else parts.push(<React.Fragment key={`literal:${match.index}`}>{match[3] && mentions[match[3]] ? `@${mentions[match[3]]}` : match[0]}</React.Fragment>)
    offset = match.index + match[0].length
  }
  parts.push(<SkillMessage key={`text:${offset}`} content={decode(content.slice(offset))} />)
  return <>{parts}</>
}
