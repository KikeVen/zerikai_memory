# IDE Agent Rules

Common rules for all IDE agents, regardless of the underlying LLM or agent architecture. These rules are designed to ensure that agents effectively leverage the `universal-brain` MCP as their primary source of information about the codebase, and only escalate to raw file searches when necessary.

Apply these rules after registering the universal-brain MCP server in your IDE. Once applied, they will guide the behavior of your IDE agents when interacting with the `universal-brain` MCP and performing codebase exploration tasks.

```markdown
1. UNIVERSAL-BRAIN FIRST: Before ANY codebase exploration, always query the
   `universal-brain` MCP first using `query_memory` with the active workspace.
   - Use it for: functions, files, architecture, patterns, task flows, and integrations.
   - Treat its response as your L2 starting point (file + line references).
   - If the MCP returns a source citation (e.g., `file.py:136`), use it as the
     entry point for deeper digging via `view_file` or `grep_search`.
   - Only escalate to raw file searches when the MCP has no memory of the topic.
     Always state that this triggered the escalation.

2. SOURCE DISCIPLINE: Always surface the full source citation from `universal-brain`
   in every answer. This serves both the agent (as an L2 entry point for deeper digging)
   and the user (to navigate directly to the relevant code). Each citation must include:
   - A clickable file link with line number (e.g., `file.py:136`)
   - The confidence score returned by the MCP (e.g., `0.80`)
   Never fabricate answers — if the MCP doesn't know, say so and escalate.
```

**Visual Studio Code GitHub Copilot:**

documentation: <https://code.visualstudio.com/docs/copilot/customization/custom-instructions>

In the agent Chat window, click on the settings icon (gear) in the upper right corner. This will open the settings panel where you can find a section for "instructions", in the upper right corner click on the down arrow in the blue button, and select "New Instructions (User)". This will open the instructions editor where you can input the above markdown content. Make sure to save your changes.

**pi.dev:**

documentation: <https://pi.dev/docs/latest/extensions>

You can ask the agent to "add a new extension" and provide the above markdown content as the extension's content. This will create a new extension that contains these rules, and you can then activate this extension for your IDE agents.

**Google Antigravity:**

documentation: <https://antigravity.google/docs/rules-workflows>

In the agent Chat window, in the upper right corner, click on the three dots and select "Rules". This will open the rules editor where you can input the above markdown content. Make sure to save your changes.

**Claude Desktop (free version):**

documentation: <https://code.claude.com/docs/en/desktop>

In the upper left corner, click on the three horizontal lines to open the menu, and select "Settings". In the settings menu, navigate to the "General" section. Under 'Instructions for Claude', input the above markdown content as the rule's content and save your changes.