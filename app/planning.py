"""Per-message planning intent shared by normal, durable and live inputs."""


def message_prompt(message):
    content = message['content']
    if not message.get('plan_mode'):
        return content
    return (
        "PLAN MODE — applies only to this request. Investigate and produce a useful "
        "plan for the user to carry out. Do not implement changes, publish, deploy, "
        "or modify external services. You may read source and create planning artifacts "
        "under /workspace/plans (Markdown plans, checklists, specifications or diagrams). "
        "Include concrete steps, relevant files, acceptance criteria, risks and open "
        "questions as appropriate. Link each saved artifact using an absolute workspace "
        "Markdown link so the user can open or download it from Files. If artifact "
        "creation is unavailable, provide the plan inline and explain that limitation. "
        "A later message without plan mode follows its own instructions.\n\n"
        + content
    )
