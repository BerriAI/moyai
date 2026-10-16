import React from "react"
import { $applyNodeReplacement, $getNodeByKey, DecoratorNode, type LexicalNode, type NodeKey, type SerializedLexicalNode } from "lexical"
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext"
import { SkillMention } from "./skill-content"
import { skillMarker, skillReferencePattern, type SkillReference } from "./skill-mentions"

type SerializedSkill = SerializedLexicalNode & { reference: string; icon: string }

/** One atomic inline node, with the scoped identity separate from its visible name. */
export class SkillNode extends DecoratorNode<React.JSX.Element> {
  __reference: string
  __icon: string

  static getType() { return "moyai-skill" }
  static clone(node: SkillNode) { return new SkillNode(node.__reference, node.__icon, node.__key) }
  constructor(reference: string, icon = "auto", key?: NodeKey) {
    super(key)
    this.__reference = reference
    this.__icon = icon
  }
  static importJSON(value: SerializedSkill) {
    if (!skillReferencePattern.test(value.reference)) throw new Error("Invalid skill reference.")
    return $createSkillNode({ reference: value.reference, name: value.reference.split(":")[1], icon: value.icon })
  }
  exportJSON(): SerializedSkill {
    return { ...super.exportJSON(), type: "moyai-skill", version: 1, reference: this.__reference, icon: this.__icon }
  }
  getSkill(): SkillReference {
    const node = this.getLatest()
    return { reference: node.__reference, name: node.__reference.split(":")[1], icon: node.__icon }
  }
  getTextContent() { return skillMarker(this.getSkill()) }
  createDOM() {
    const element = document.createElement("span")
    element.className = "skill-token"
    element.contentEditable = "false"
    return element
  }
  updateDOM() { return false }
  isInline() { return true }
  isKeyboardSelectable() { return true }
  decorate() { return <SelectedSkill nodeKey={this.getKey()} skill={this.getSkill()} /> }
}

function SelectedSkill({ nodeKey, skill }: { nodeKey: NodeKey; skill: SkillReference }) {
  const [editor] = useLexicalComposerContext()
  return <span onMouseDown={event => {
    if (event.button !== 0) return
    event.preventDefault()
    editor.update(() => {
      const node = $getNodeByKey(nodeKey), parent = node?.getParent()
      if (node && parent) { const index = node.getIndexWithinParent(); parent.select(index, index + 1) }
    })
    editor.focus()
  }}><SkillMention skill={skill} /></span>
}

export function $createSkillNode(skill: SkillReference) {
  return $applyNodeReplacement(new SkillNode(skill.reference, skill.icon || "auto"))
}
export function $isSkillNode(node: LexicalNode | null | undefined): node is SkillNode { return node instanceof SkillNode }
