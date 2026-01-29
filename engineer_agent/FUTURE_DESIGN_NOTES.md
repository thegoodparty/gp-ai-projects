# Engineer Agent - Design Notes

## Human-in-the-Loop Possibilities (Side-tabled)

The Claude Agent SDK supports human-in-the-loop patterns we may want to explore later:

### Sessions (Most Promising)
- Sessions are stored on **Anthropic's servers**, not locally
- Can resume from different containers using just `session_id`
- Flow:
  1. Fargate Task 1: Analyze → save `session_id` → post findings → exit
  2. Human reviews in ClickUp, approves
  3. Fargate Task 2: `resume=session_id` → full context restored → implement fix
- Solves the "summary loses context" problem between phases

### Permission Callbacks
- `can_use_tool` callback - approve/reject/modify tool calls
- But requires task to stay awake waiting (expensive for Fargate)
- Could work with webhook-based approval + timeout

### Permission Modes
- `default` - ask for everything
- `acceptEdits` - auto-approve file ops, ask for bash
- `bypassPermissions` - approve all (current mode)

### Hooks
- `PreToolUse` / `PostToolUse` - intercept actions
- Could log to audit trail or post updates to ClickUp

### References
- Sessions: https://docs.anthropic.com/en/agent-sdk/sessions
- Permissions: https://docs.anthropic.com/en/agent-sdk/permissions
- Hooks: https://docs.anthropic.com/en/agent-sdk/hooks
