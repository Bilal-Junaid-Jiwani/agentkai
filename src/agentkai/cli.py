"""CLI: `agentkai` command."""
from __future__ import annotations

import threading

import typer

from .agent import Agent
from .permissions import PermissionGate
from .providers import ALIASES, ProviderConfig, resolve_model

app = typer.Typer(help="agentkai — model-agnostic personal AI agent")


def _build_agent(model: str, auto_approve: bool) -> Agent:
    """Shared agent construction for CLI commands."""
    config = ProviderConfig.load()
    if auto_approve:
        gate = PermissionGate(policy={"risk:medium": "allow",
                                      "risk:high": "allow"})
    else:
        gate = PermissionGate()  # prompts on medium/high via cli_ask
    return Agent(model=model, config=config, gate=gate)


@app.command()
def run(prompt: str = typer.Argument(..., help="What to ask the agent"),
        model: str = typer.Option("claude", "--model", "-m",
                                  help="Model alias (claude, gemini, gpt, "
                                       "glm, local) or LiteLLM model string"),
        auto_approve: bool = typer.Option(
            False, "--auto-approve", "-y",
            help="Auto-approve medium/high-risk tool calls (no prompts)"),
        max_iterations: int = typer.Option(25, "--max-steps",
                                           help="Max ReAct loop iterations")):
    """Run one agent task with live streaming output."""
    agent = _build_agent(model, auto_approve)
    agent.max_iterations = max_iterations

    def on_text(delta: str) -> None:
        # Streamed raw: no extra formatting per chunk.
        print(delta, end="", flush=True)

    typer.echo(f"[agentkai · {resolve_model(model, agent.config.extra_aliases)}]",
               err=True)
    result = agent.run(prompt, on_text=on_text)
    print()  # end the streamed line
    if result.status != "done":
        typer.echo(f"\n[run {result.status}: {result.error}]", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"\n[run {result.run_id} · {result.iterations} steps · "
               f"log: {result.events_path}]", err=True)


@app.command()
def chat(model: str = typer.Option("claude", "--model", "-m",
                                   help="Model alias or LiteLLM model string"),
         prompt: str = typer.Argument(..., help="What to ask the agent"),
         auto_approve: bool = typer.Option(
             False, "--auto-approve", "-y",
             help="Auto-approve medium/high-risk tool calls (no prompts)")):
    """Send one prompt to the agent and print the final answer."""
    agent = _build_agent(model, auto_approve)
    result = agent.run(prompt)
    typer.echo(result.text)
    if result.status != "done":
        typer.echo(f"\n[run {result.status}: {result.error}]", err=True)
        raise typer.Exit(code=1)


@app.command()
def models():
    """Show model aliases and what they resolve to."""
    config = ProviderConfig.load()
    for alias in sorted({**ALIASES, **config.extra_aliases}):
        typer.echo(f"{alias:12} -> "
                   f"{resolve_model(alias, config.extra_aliases)}")
    typer.echo("\nkey status:")
    for provider, status in config.key_status().items():
        typer.echo(f"  {provider:10} {status}")


@app.command()
def dashboard(port: int = typer.Option(8931, "--port", "-p",
                                       help="Local port to listen on")):
    """Launch the local web dashboard (127.0.0.1 only, token-secured)."""
    from .dashboard import run as run_dashboard
    run_dashboard(port=port)


@app.command()
def doctor(json_output: bool = typer.Option(
        False, "--json", help="Machine-readable JSON report")):
    """Check the environment: config, provider keys, scheduler DB, memory,
    tools, media providers, dashboard assets. Exits 1 on any [FAIL]."""
    from . import doctor as doctor_mod
    checks = doctor_mod.run_checks()
    typer.echo(doctor_mod.format_json(checks)
               if json_output else doctor_mod.format_text(checks))
    if doctor_mod.has_failures(checks):
        raise typer.Exit(code=1)


# -- runs --------------------------------------------------------------------

runs_app = typer.Typer(help="Inspect past agent runs (event logs)")
app.add_typer(runs_app, name="runs")


def _runs_root():
    from .media import agentkai_home
    return agentkai_home() / "runs"


def _short_text(value, n: int = 80) -> str:
    s = str(value).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _describe_event(ev) -> str:
    """One-line rendering of a replayed event for `runs show`."""
    d = ev.data
    head = f"#{ev.seq:<4} {ev.ts}  {ev.type}"
    if ev.type == "run_start":
        return (f"{head}  model={d.get('model', '?')} "
                f"prompt={_short_text(d.get('prompt', ''))!r}")
    if ev.type == "llm_message":
        return f"{head}  {_short_text(d.get('text', ''))!r}"
    if ev.type == "tool_call":
        return f"{head}  {d.get('name', '?')}({_short_text(d.get('args', {}), 60)})"
    if ev.type == "tool_result":
        state = "ok" if d.get("ok", True) else "FAILED"
        return (f"{head}  {d.get('name', '?')} [{state}] "
                f"{_short_text(d.get('output', ''))!r}")
    if ev.type == "approval":
        return f"{head}  {d.get('tool')}:{d.get('decision')}"
    if ev.type == "run_end":
        return (f"{head}  status={d.get('status', '?')} "
                f"final={_short_text(d.get('final_text', ''))!r}")
    return head


@runs_app.command("list")
def runs_list(limit: int = typer.Option(20, "--limit", "-n",
                                        help="Max runs (newest first)"),
              json_output: bool = typer.Option(
                  False, "--json", help="Machine-readable JSON output")):
    """List past runs, newest first (status, model, tools used)."""
    import json as _json

    from . import events

    root = _runs_root()
    run_ids = events.list_runs(root=root)[:max(limit, 0)]
    rows = [events.summarize(run_id, root=root) for run_id in run_ids]
    if json_output:
        typer.echo(_json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        typer.echo('no runs yet — `agentkai run "..."` creates one')
        return
    for r in rows:
        tools = ", ".join(r["tool_calls"][:6]) or "-"
        if len(r["tool_calls"]) > 6:
            tools += ", …"
        typer.echo(f"{r['run_id']:14} [{r['status']}] {r['model'] or '?'} "
                   f"tools: {tools}")


@runs_app.command("show")
def runs_show(run_id: str = typer.Argument(..., help="Run id"),
              json_output: bool = typer.Option(
                  False, "--json", help="Machine-readable JSON output")):
    """Replay one run's event log, in order."""
    import json as _json

    from . import events

    root = _runs_root()
    try:
        evs = list(events.replay(run_id, root=root))
    except FileNotFoundError:
        typer.echo(f"no run {run_id!r} (looked in {root})", err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(_json.dumps([e.to_dict() for e in evs],
                               indent=2, default=str))
        return
    for ev in evs:
        typer.echo(_describe_event(ev))


# -- scheduler ---------------------------------------------------------------

scheduler_app = typer.Typer(help="Scheduled agent jobs: cron, one-shot, "
                                 "heartbeat, dreaming")
app.add_typer(scheduler_app, name="scheduler")


def _get_scheduler():
    from .scheduler import Scheduler
    return Scheduler()


@scheduler_app.command("add")
def scheduler_add(
        name: str = typer.Argument(..., help="Job name (unique)"),
        schedule: str = typer.Option(..., "--schedule", "-s",
                                     help='Cron "m h dom mon dow", e.g. '
                                          '"*/15 * * * *"'),
        prompt: str = typer.Option("", "--prompt", "-p",
                                   help="Agent prompt to run"),
        model: str = typer.Option("claude", "--model", "-m",
                                  help="Model alias for the job"),
        job_type: str = typer.Option("cron", "--type", "-t",
                                     help="cron | heartbeat | dreaming"),
        at: str = typer.Option("", "--at",
                               help="One-shot ISO datetime instead of cron, "
                                    "e.g. 2026-09-30T09:00"),
        command: str = typer.Option("", "--command",
                                    help="Legacy shell command (no agent)")):
    """Add a scheduled job. Either --prompt (agent job) or --command."""
    from .scheduler import Job
    if at:
        schedule = f"@at:{at}"
        job_type = "at"
    if job_type not in ("cron", "at", "heartbeat", "dreaming"):
        typer.echo(f"unknown job type: {job_type}", err=True)
        raise typer.Exit(code=2)
    if not prompt and not command and job_type in ("cron", "at"):
        typer.echo("give --prompt (agent job) or --command (shell job)",
                   err=True)
        raise typer.Exit(code=2)
    sched = _get_scheduler()
    sched.add(Job(name=name, schedule=schedule, command=command,
                  prompt=prompt, model_alias=model, job_type=job_type))
    typer.echo(f"added job {name!r} [{job_type}] schedule={schedule}")
    sched.close()


@scheduler_app.command("list")
def scheduler_list():
    """List all scheduled jobs."""
    sched = _get_scheduler()
    for job in sched.list():
        state = "on " if job.enabled else "off"
        typer.echo(f"{state} {job.name:24} {job.job_type:10} "
                   f"{job.schedule:20} last={job.last_run or '-'}")
    sched.close()


@scheduler_app.command("remove")
def scheduler_remove(name: str = typer.Argument(..., help="Job name")):
    """Remove a scheduled job."""
    sched = _get_scheduler()
    if sched.remove(name):
        typer.echo(f"removed {name!r}")
    else:
        typer.echo(f"no job named {name!r}", err=True)
        raise typer.Exit(code=1)
    sched.close()


@scheduler_app.command("run-once")
def scheduler_run_once(name: str = typer.Argument(..., help="Job name")):
    """Run one job now, regardless of schedule."""
    sched = _get_scheduler()
    result = sched.run_job(name)
    typer.echo(f"{result['name']}: {result['status']}")
    if result.get("summary"):
        typer.echo(result["summary"][:2000])
    sched.close()
    if result["status"] == "error":
        raise typer.Exit(code=1)


@scheduler_app.command("run-due")
def scheduler_run_due():
    """Run every job whose schedule is due now."""
    sched = _get_scheduler()
    results = sched.run_due()
    if not results:
        typer.echo("nothing due")
    for r in results:
        typer.echo(f"{r['name']}: {r['status']} — "
                   f"{(r.get('summary') or '')[:120]}")
    sched.close()


# -- devices -------------------------------------------------------------------

devices_app = typer.Typer(help="Pair phones/laptops and reach them from the agent")
app.add_typer(devices_app, name="devices")


def _device_manager():
    from .devices import DeviceManager
    return DeviceManager()


@devices_app.command("pair")
def devices_pair(name: str = typer.Option("phone", "--name", "-n",
                                          help="Friendly name for the device")):
    """Print a pairing code; enter it in the companion app within 10 min."""
    mgr = _device_manager()
    info = mgr.generate_pairing_code(name)
    typer.echo(f"pairing code: {info['code']}")
    typer.echo(f"valid for {info['expires_in_seconds'] // 60} minutes. "
               "In the companion app, enter this code (see "
               "src/agentkai/devices/README.md for the app contract).")


@devices_app.command("list")
def devices_list():
    """List paired devices."""
    mgr = _device_manager()
    devices = mgr.list_devices()
    if not devices:
        typer.echo("no paired devices — run `agentkai devices pair`")
        return
    for d in devices:
        caps = ",".join(k for k, v in d.capabilities.items() if v) or "-"
        typer.echo(f"#{d.id} {d.name:16} {d.device_type:8} caps=[{caps}] "
                   f"api={'set' if d.api_base else 'not set'}")


@devices_app.command("revoke")
def devices_revoke(device_id: int = typer.Argument(..., help="Device id")):
    """Revoke a paired device (its token stops working immediately)."""
    mgr = _device_manager()
    if mgr.revoke(device_id):
        typer.echo(f"revoked device #{device_id}")
    else:
        typer.echo(f"no device #{device_id}", err=True)
        raise typer.Exit(code=1)


@devices_app.command("notify")
def devices_notify(title: str = typer.Argument(...),
                   body: str = typer.Argument(...),
                   device: str = typer.Option("", "--device", "-d",
                                              help="Device id or name")):
    """Queue a push notification for a paired device (manual test)."""
    from .devices import DeviceError
    mgr = _device_manager()
    try:
        d = mgr.resolve_device(device or None)
    except DeviceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    nid = mgr.enqueue_notification(title, body, d.id)
    typer.echo(f"queued notification #{nid} for {d.name!r} "
               "(companion app delivers on next poll)")


# -- goals ---------------------------------------------------------------------

goals_app = typer.Typer(help="Durable user goals: create, track, close")
app.add_typer(goals_app, name="goals")


def _goal_store():
    from .goals import GoalStore
    return GoalStore()


@goals_app.command("list")
def goals_list(status: str = typer.Option("active", "--status", "-s",
                                          help="active|paused|completed|abandoned|all")):
    """List goals."""
    from .goals import GoalError
    store = _goal_store()
    try:
        goals = store.list_goals(None if status == "all" else status)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    if not goals:
        typer.echo("no goals")
        return
    for g in goals:
        typer.echo(f"#{g.id} [{g.status}] {g.title} "
                   f"({g.activity_count} activities)")
        for s in g.subgoals:
            typer.echo(f"    #{s.id} [{s.status}] {s.title}")


@goals_app.command("create")
def goals_create(title: str = typer.Argument(...),
                 description: str = typer.Option("", "--desc", "-d",
                                                 help="Longer description"),
                 parent: int = typer.Option(0, "--parent", "-p",
                                            help="Parent goal id (subgoal)")):
    """Create a new goal."""
    from .goals import GoalError
    store = _goal_store()
    try:
        g = store.create_goal(title, description, parent or None)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"created goal #{g.id}: {g.title}")


@goals_app.command("show")
def goals_show(goal_id: int = typer.Argument(...)):
    """Show a goal with recent activity."""
    store = _goal_store()
    g = store.get_goal(goal_id)
    if g is None:
        typer.echo(f"no goal #{goal_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"#{g.id} [{g.status}] {g.title}")
    if g.description:
        typer.echo(f"  {g.description}")
    for s in g.subgoals:
        typer.echo(f"  sub #{s.id} [{s.status}] {s.title}")
    for a in store.recent_activity(goal_id, limit=10):
        from datetime import datetime
        ts = datetime.fromtimestamp(a.ts).strftime("%Y-%m-%d %H:%M")
        typer.echo(f"  [{ts}] {a.kind}: {a.text[:120]}")


@goals_app.command("log")
def goals_log(goal_id: int = typer.Argument(...),
              kind: str = typer.Option("note", "--kind", "-k",
                                       help="progress|setback|note"),
              text: str = typer.Argument(...)):
    """Log activity on a goal."""
    from .goals import GoalError
    store = _goal_store()
    try:
        a = store.log_activity(goal_id, kind, text)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"logged activity #{a.id} on goal #{goal_id}")


@goals_app.command("close")
def goals_close(goal_id: int = typer.Argument(...),
                outcome: str = typer.Option("completed", "--outcome", "-o",
                                            help="completed|abandoned"),
                note: str = typer.Option("", "--note", "-n")):
    """Close a goal."""
    from .goals import GoalError
    store = _goal_store()
    try:
        g = store.close_goal(goal_id, outcome, note)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"goal #{g.id} closed as {g.status}")


@goals_app.command("briefing")
def goals_briefing():
    """Print today's goal-briefing prompt (for a scheduler job)."""
    from .goals import build_goal_briefing_prompt
    typer.echo(build_goal_briefing_prompt())


# -- tracked items ---------------------------------------------------------------

track_app = typer.Typer(help="Tracked items: reservations, deliveries, reminders")
app.add_typer(track_app, name="track")


@track_app.command("open")
def track_open(title: str = typer.Argument(...),
               kind: str = typer.Option("other", "--kind", "-k",
                                        help="reservation|delivery|reminder|commitment|other")):
    """Open a tracked item."""
    from .goals import GoalError
    store = _goal_store()
    try:
        t = store.track_open(title, kind)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"tracking #{t.id} [{t.kind}]: {t.title}")


@track_app.command("close")
def track_close(item_id: int = typer.Argument(...),
                evidence: str = typer.Option("", "--evidence", "-e",
                                             help="Proof of the outcome")):
    """Close a tracked item with evidence."""
    from .goals import GoalError
    store = _goal_store()
    try:
        t = store.track_close(item_id, evidence)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"closed #{t.id}: {t.title}")


@track_app.command("list")
def track_list(status: str = typer.Option("open", "--status", "-s",
                                          help="open|closed|all")):
    """List tracked items."""
    from .goals import GoalError
    store = _goal_store()
    try:
        items = store.list_tracked(None if status == "all" else status)
    except GoalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    if not items:
        typer.echo("nothing tracked")
        return
    for t in items:
        typer.echo(f"#{t.id} [{t.status}/{t.kind}] {t.title}"
                   + (f" — {t.evidence[:80]}" if t.evidence else ""))


def main() -> None:
    app()


# ---- skills -----------------------------------------------------------------

skills_app = typer.Typer(help="Manage agent skills (SKILL.md playbooks)")
app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list():
    """List bundled and installed skills."""
    from .skills import list_skills

    rows = list_skills()
    if not rows:
        typer.echo("no skills found")
        return
    for r in rows:
        if "error" in r:
            typer.echo(f"{r['name']:18} [{r['source']}] ERROR: {r['error']}")
            continue
        tools = r["tools"]
        tools_s = f"{tools} tools" if tools is not None else "tools: ?"
        typer.echo(f"{r['name']:18} v{r['version']} [{r['source']}] "
                   f"{tools_s}\n  {r['description']}")


@skills_app.command("show")
def skills_show(name: str = typer.Argument(..., help="Skill name")):
    """Print a skill's SKILL.md instructions."""
    from .skills import SkillError, SkillLoader

    try:
        skill = SkillLoader().load(name)
    except SkillError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"# {skill.name} v{skill.version} [{skill.source}]\n")
    typer.echo(skill.instructions)
    if skill.tools:
        typer.echo("\ntools: " + ", ".join(t.name for t in skill.tools))


@skills_app.command("install")
def skills_install(
        source: str = typer.Argument(...,
            help="Local skill directory or git URL (owner/repo works too)"),
        name: str = typer.Option(None, "--name", "-n",
                                 help="Install under this name"),
        force: bool = typer.Option(False, "--force", "-f",
                                    help="Overwrite an existing install"),
        trust_remote: bool = typer.Option(False, "--trust-remote",
                                    help="Trust a remote git source: code "
                                         "from this repo will run inside "
                                         "agentkai"),
        pin: str = typer.Option(None, "--pin",
                                help="Pin remote install to this commit SHA")):
    """Install a skill into ~/.agentkai/skills/."""
    from .skills import SkillError, install_skill

    if trust_remote or pin:
        typer.echo("warning: code from this repo will run inside agentkai")
    try:
        dest = install_skill(source, name=name, force=force,
                             trust_remote=trust_remote, pin=pin)
    except SkillError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"installed skill at {dest}")


@skills_app.command("remove")
def skills_remove(name: str = typer.Argument(..., help="Skill name")):
    """Remove a user-installed skill (bundled skills stay)."""
    from .skills import SkillError, remove_skill

    try:
        remove_skill(name)
    except SkillError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"removed skill {name!r}")


@app.command()
def gateway(port: int = typer.Option(None, "--port", "-p",
                                     help="Override the webchat port"),
            config: str = typer.Option(None, "--config",
                                       help="Path to channels.yaml")):
    """Run the messaging gateway daemon (Telegram/Discord/WebChat/WhatsApp)."""
    from .channels import run_gateway

    run_gateway(config_path=config, port=port)


if __name__ == "__main__":
    main()
