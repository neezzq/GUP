# Game Updater

- Use Python 3.12. Run `python -m pytest` from the repository root.
- Update games through installed official clients. Record launcher build, locale and UI Automation evidence when adding automation rules.
- Require an isolated Windows maintenance account. Stop on authentication challenges, unknown dialogs, and ambiguous update states.
- Never log account credentials, device tokens, enrollment codes or raw external exception details.
- See the applicable workflow under `.agents/skills/`.
- After verifying a requested project update, commit the scoped changes and push them to `origin/main`. Check the Windows workflow and confirm that its GitHub release contains the EXE and checksum.
