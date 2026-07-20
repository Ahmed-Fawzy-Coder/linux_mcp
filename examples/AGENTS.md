## Global Linux MCP workflow

- For local project files and commands, use the global Linux MCP gateway first when it is available.
- The canonical tool id is `mcp__linux_mcp__workspace`; call only this gateway, not the legacy individual tools.
- Start with `search_files`, then use bounded `read_file` calls with explicit `offset` and `length`.
- Pass the active project's absolute root as `path` for searches and `cwd` for commands and tests.
- Keep command and log output bounded to the latest 100 lines and at most 12,000 characters unless more is necessary.
- Retry once during the MCP startup grace period before falling back to native tools.
- Do not repeatedly read unchanged files. Batch independent operations when safe.
