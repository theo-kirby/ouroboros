# Ouroboros

A loop runner for agent harnesses (Claude Code, Codex, Pi).

You give it a goal. It runs the agent again and again. It never blocks on a
question, never believes "done" too early, and leaves a memory trail for the
morning.

See [DESIGN.md](DESIGN.md).

```bash
uv tool install .
ouroboros init            # writes .ouroboros/config.yml + goal.md
ouroboros run --for 8h    # runs inside tmux
ouroboros status
ouroboros report
```
