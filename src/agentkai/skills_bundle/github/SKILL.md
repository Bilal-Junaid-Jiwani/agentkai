---
name: github
description: Work with GitHub repos, issues, pull requests, and Actions workflow runs.
version: 0.1.0
when: github, repo, repository, issue, pull request, pr, workflow, actions, ci
---

# GitHub

Thin wrapper over the GitHub REST API. Reads are low risk
(`github_list_repos`, `github_list_issues`, `github_list_prs`,
`github_list_workflow_runs`); everything that publishes — creating issues,
opening PRs, commenting — is **high risk** and goes through the permission
gate.

## Setup (required — nothing works without this)

1. Create a personal access token at
   https://github.com/settings/tokens (classic: `repo` + `workflow` scopes;
   or a fine-grained token with contents/issues/pull-requests/actions
   permissions on the repos you need).
2. Provide it as the `GITHUB_TOKEN` environment variable, or as
   `~/.agentkai/github_token.json` containing `{"access_token": "..."}`.

Without a token every tool returns a configuration error — there is no
anonymous fallback.

## Usage notes

- `owner`/`repo` are the `owner/name` pair from the repo URL
  (e.g. owner `paperclipai`, repo `paperclip`).
- Comments and PRs are public actions: always confirm the exact text with
  the user before calling; the gate will ask anyway.
- `github_open_pr` needs `head` (source branch) to already exist on the
  remote.
