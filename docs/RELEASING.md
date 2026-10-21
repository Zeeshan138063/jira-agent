# Releasing a new version

For the admin who maintains the shared repository.

1. **Decide the version** (semantic versioning): fixes bump the last number, new features the middle one.
2. **Update the version everywhere it appears.** A test fails if these disagree:
   - `.claude-plugin/plugin.json` (`version`)
   - `.claude-plugin/marketplace.json` (the plugin's `version`)
   - `pyproject.toml` (`version`)
   - `src/jira_agent/__init__.py` (`__version__`)
3. **Add a section to `CHANGELOG.md`** with the same version as its first heading.
4. **Run the checks:**

   ```bash
   uv run pytest
   uv run ruff check . && uv run ruff format --check .
   claude plugin validate . --profile directory --strict
   ```

5. **Try the installed copy**, ideally in a scratch project so your own settings are untouched:

   ```bash
   mkdir /tmp/try && cd /tmp/try
   claude plugin marketplace add /path/to/jira-agent --scope project
   claude plugin install jira-agent@team-plugins --scope project
   claude plugin list
   # clean up
   claude plugin uninstall jira-agent@team-plugins --scope project
   claude plugin marketplace remove team-plugins --scope project
   ```

6. **Commit, tag and push:**

   ```bash
   git commit -am "Release 0.9.1"
   git tag v0.9.1
   git push && git push --tags
   ```

7. **Tell the team** to run `claude plugin marketplace update team-plugins` and
   `claude plugin update jira-agent@team-plugins`, then restart Claude.

## Before the very first release

- Run `uv run pytest` (it includes guards against personal data, such as emails, home-directory paths,
  real Atlassian sites and token-shaped strings, in the repository).
- Check `git log --format='%an <%ae>'`. Commits carry whoever made them. If you want a team identity
  on the published history, make the release commit from a clean clone with that identity.
- Fill in `repository` and `homepage` in `.claude-plugin/plugin.json` once the repository has a URL.
- If you regenerate the icon: `uv run --with pillow python scripts/make_icon.py`.
