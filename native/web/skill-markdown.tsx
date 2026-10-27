import React, { createContext, useContext, useMemo } from "react"
import Markdown, { defaultUrlTransform } from "react-markdown"
import remarkGfm from "remark-gfm"
import type { Root, RootContent, PhrasingContent, Text } from "mdast"
import { decodeString } from "micromark-util-decode-string"
import { appPath } from "./api"
import { CodeBlock } from "./code-block"
import { MessageImage, MessageImageLink } from "./message-image"
import { SkillMention } from "./skill-content"
import { transcriptSkills, skillMentions, type SkillReference } from "./skill-mentions"

const SkillReferences = createContext<readonly SkillReference[]>([])

/** Use only metadata already present in this conversation's authorized messages. */
export function SkillMarkdownProvider({ messages, children }: {
  messages: readonly { role: string; content: string }[]; children: React.ReactNode
}) {
  const skills = useMemo(() => {
    const selected = new Map<string, SkillReference>()
    for (const message of messages) {
      if (message.role !== "user") continue
      for (const skill of transcriptSkills(message.content)) selected.set(skill.reference, skill)
    }
    return [...selected.values()]
  }, [messages])
  return <SkillReferences.Provider value={skills}>{children}</SkillReferences.Provider>
}

function skillNode(skill: SkillReference): PhrasingContent {
  return {
    type: "emphasis", children: [{ type: "text", value: `/${skill.name}` }],
    data: { hName: "span", hProperties: { "data-skill-reference": skill.reference } },
  }
}

/** Transform Markdown text, never HTML, link labels/destinations or code blocks. */
function remarkSkills(skills: readonly SkillReference[], source: string) {
  const references = new Map(skills.map(skill => [skill.reference, skill]))
  const names = new Map<string, SkillReference | null>()
  for (const skill of references.values()) {
    names.set(skill.name, names.has(skill.name) ? null : skill)
  }
  const resolve = (name: string) => references.get(name) ?? names.get(name)
  // Slashes inside URLs, paths and command arguments are not skill mentions.
  const pattern = /(^|[\s([{'"“‘])\/((?:(?:personal|org|shared|bundled):)?[A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?=$|[\s)\]},!?;:'"”’]|\.(?=\s|$))/g
  const raw = (node: RootContent) => source.slice(node.position?.start.offset, node.position?.end.offset)
  function textNodes(node: Text): PhrasingContent[] {
    const value = raw(node)
    // Decode each preserved slice using the same CommonMark escape/entity rules.
    // Skip unusual parser normalizations rather than changing their literal text.
    if (decodeString(value) !== node.value) return [node]
    const parts: PhrasingContent[] = []
    let offset = 0
    for (const match of value.matchAll(pattern)) {
      const name = match[2].replace(/\.$/, "")
      const skill = resolve(name)
      if (!skill) continue
      const start = match.index + match[1].length
      const end = start + name.length + 1
      if (start > offset) parts.push({ type: "text", value: decodeString(value.slice(offset, start)) })
      parts.push(skillNode(skill))
      offset = end
    }
    if (!parts.length) return [node]
    if (offset < value.length) parts.push({ type: "text", value: decodeString(value.slice(offset)) })
    return parts
  }
  function walk(parent: Root | RootContent) {
    if (!("children" in parent) || ["link", "linkReference", "image", "imageReference"].includes(parent.type)) return
    if (parent.children.some(node => node.type === "html")) return
    const children: RootContent[] = []
    for (const node of parent.children) {
      if (node.type === "text") children.push(...textNodes(node))
      else if (node.type === "inlineCode") {
        const skill = node.value.startsWith("/") ? resolve(node.value.slice(1)) : undefined
        children.push(skill ? skillNode(skill) : node)
      } else if (node.type === "link") {
        const mention = skillMentions(raw(node))[0]
        const skill = mention && references.get(mention.reference)
        children.push(skill ? skillNode(skill) : node)
      } else {
        walk(node)
        children.push(node)
      }
    }
    // Every replacement is phrasing content at the same position as the original.
    parent.children = children as typeof parent.children
  }
  return () => (tree: Root) => { walk(tree) }
}

export function SkillMarkdown({ children }: { children: string }) {
  const skills = useContext(SkillReferences)
  const plugin = useMemo(() => remarkSkills(skills, children), [skills, children])
  const references = useMemo(() => new Map(skills.map(skill => [skill.reference, skill])), [skills])
  return <Markdown urlTransform={url => appPath(defaultUrlTransform(url))} remarkPlugins={[remarkGfm, plugin]} components={{
    pre: CodeBlock,
    img: ({ src, alt, title }) => <MessageImage key={typeof src === "string" ? src : undefined} src={typeof src === "string" ? src : undefined} alt={alt} title={title} />,
    a: ({ node, ...props }) => <MessageImageLink {...props} />,
    span: ({ node, children: content }) => {
      const reference = node?.properties["data-skill-reference"] ?? node?.properties.dataSkillReference
      const skill = typeof reference === "string" ? references.get(reference) : undefined
      return skill ? <SkillMention skill={skill} /> : <span>{content}</span>
    },
  }}>{children}</Markdown>
}
