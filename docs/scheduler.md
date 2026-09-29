# Scheduler

Background jobs in SQLite (`~/.open-agent/scheduler.db`).

- One-shot and cron-style recurring jobs
- A runner executes due jobs (shell commands or agent prompts)
- Delivery rule: stay silent unless the job produced something worth the
  user's attention — no noise for routine runs

Example: a price-watch job that messages you only when the condition hits.
